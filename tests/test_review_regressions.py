"""Regressions reported in the first/second batch release review."""

import ast
import filecmp
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pikepdf
import pytest
from loguru import logger

from test_phase1 import ROOT
from test_phase2 import hook_task, set_status
from output_normalizer import normalize_output
from output_normalizer.base_output_normalizer import BaseOutputNormalizer
from utils.archive_utils import merge_archive_results
from utils.image_references import image_filenames


def make_worker(db, output):
    klass = next(
        n
        for n in ast.parse((ROOT / "backend/litserve_worker.py").read_text()).body
        if isinstance(n, ast.ClassDef) and n.name == "MinerUWorkerAPI"
    )
    names = {
        "_process_task",
        "_split_archive",
        "_should_split_pdf",
        "_merge_parent_task_results",
        "_complete_parent_merge",
        "_cleanup_child_task_files",
        "_ensure_pdf_in_output",
    }
    methods = [n for n in klass.body if isinstance(n, ast.FunctionDef) and n.name in names]
    namespace = dict(
        Path=Path,
        os=os,
        json=json,
        logger=logger,
        shutil=shutil,
        filecmp=filecmp,
        normalize_output=normalize_output,
        MINERU_PIPELINE_AVAILABLE=True,
        FORMAT_ENGINES_AVAILABLE=False,
        SENSEVOICE_AVAILABLE=False,
        VIDEO_ENGINE_AVAILABLE=False,
    )
    exec(compile(ast.Module(body=methods, type_ignores=[]), "<review-worker>", "exec"), namespace)
    obj = SimpleNamespace(
        task_db=db, output_dir=str(output), worker_id="worker-old", mineru_vllm_api=None, watermark_handler=None
    )
    for name in names:
        setattr(obj, name, namespace[name].__get__(obj))
    return obj


def test_reassigned_worker_failure_does_not_fail_parent_or_notify(db, tmp_path):
    root = hook_task(db)
    db.convert_to_parent_task(root)
    child = db.create_child_task(root, "chunk.pdf", str(tmp_path / "chunk.pdf"), backend="pipeline")
    set_status(db, child, "processing")
    with db.get_cursor() as c:
        c.execute("UPDATE tasks SET worker_id='worker-old' WHERE task_id=?", (child,))
    obj = make_worker(db, tmp_path)

    def old_attempt(*args):
        with db.get_cursor() as c:
            c.execute("UPDATE tasks SET worker_id='worker-new',retry_count=retry_count+1 WHERE task_id=?", (child,))
        raise RuntimeError("late failure")

    obj._process_with_mineru = old_attempt
    with pytest.raises(RuntimeError, match="late failure"):
        obj._process_task(db.get_task(child))
    assert db.get_task(child)["worker_id"] == "worker-new"
    assert db.get_task(child)["status"] == db.get_task(root)["status"] == "processing"
    # The database callback also rejects an unaccepted failure if called directly.
    db.on_child_task_failed(child, "late callback")
    assert db.get_task(root)["status"] == "processing"
    with db.get_cursor() as c:
        assert c.execute("SELECT COUNT(*) FROM webhook_deliveries").fetchone()[0] == 0


def test_zip_json_body_survives_parent_rustfs_recovery(tmp_path, monkeypatch):
    child = tmp_path / "child"
    (child / "images").mkdir(parents=True)
    (child / "images/a.png").write_bytes(b"first")
    (child / "images/b.png").write_bytes(b"second")
    body = 'Opening paragraph.\n![a](images/a.png)\nMiddle paragraph.\n<img src="images/b.png">\nClosing paragraph.'
    (child / "result.md").write_text(body)
    (child / "result.json").write_text(
        json.dumps({"img_path": "images/a.png", "caption": "Mention images/a.png literally", "text": body})
    )
    target = tmp_path / "merged"
    merge_archive_results(
        [dict(task_id="member", result_path=str(child), file_name="member.pdf", options="{}")], target
    )
    mapping = {"0_a.png": "https://storage.example/a.png", "0_b.png": "https://storage.example/b.png"}
    monkeypatch.setattr(BaseOutputNormalizer, "_upload_images_to_rustfs", lambda *args: mapping)
    normalize_output(target, use_rustfs=True, enable_caption=False)
    document = json.loads((target / "result.json").read_text())["documents"][0]
    assert all(
        text in document["content"] for text in ["Opening paragraph.", "Middle paragraph.", "Closing paragraph."]
    )
    assert all(url in document["content"] for url in mapping.values())
    assert document["data"]["img_path"] == mapping["0_a.png"]
    assert "Mention" in document["data"]["caption"]
    assert "Closing paragraph." in document["data"]["text"]
    assert set(image_filenames((target / "result.md").read_text())) == {"a.png", "b.png"}


@pytest.mark.parametrize("rustfs", [False, True])
def test_bracket_caption_actual_image_handoff(api, monkeypatch, rustfs):
    from fastapi.testclient import TestClient
    from auth.dependencies import get_current_active_user
    from image_caption.processor import _write_back_markdown

    output = api.OUTPUT_DIR / "captioned"
    (output / "images").mkdir(parents=True)
    for name in ["a.png", "b.png", "unused.png"]:
        (output / "images" / name).write_bytes(name.encode())
    md = output / "result.md"
    md.write_text("![one](images/a.png)\n![two](images/b.png)")
    _write_back_markdown(md, {"a.png": "[Figure 1] flowchart", "b.png": "second"})
    if rustfs:
        BaseOutputNormalizer()._replace_markdown_urls(md, {"a.png": "https://storage.example/a.png"})
    tid = api.db.create_task("captioned.pdf", "/source.pdf", user_id="owner", options={"use_rustfs": rustfs})
    set_status(api.db, tid, "processing")
    api.db.update_task_status(tid, "completed", result_path=str(output))
    api.app.dependency_overrides[get_current_active_user] = lambda: SimpleNamespace(
        user_id="owner", has_permission=lambda p: False
    )
    try:
        with TestClient(api.app) as client:
            listing = client.get(f"/api/v1/tasks/{tid}/images")
            assert listing.status_code == 200
            assert {i["filename"] for i in listing.json()["images"]} == {"a.png", "b.png"}
            content = client.get(f"/api/v1/tasks/{tid}").json()["data"]["content"]
            assert image_filenames(content) == {"a.png", "b.png"}
            for image in listing.json()["images"]:
                assert client.get(image["download_url"]).content == image["filename"].encode()
    finally:
        api.app.dependency_overrides.clear()


def test_invite_configuration_api_matches_registration_limit(api):
    from fastapi.testclient import TestClient
    from auth.auth_db import AuthDB
    from auth.dependencies import get_auth_db, get_current_active_user
    from auth.system_config import SystemConfig

    auth = AuthDB(api.db.db_path)
    api.app.dependency_overrides[get_auth_db] = lambda: auth
    api.app.dependency_overrides[get_current_active_user] = lambda: SimpleNamespace(
        user_id="admin", has_permission=lambda p: True
    )
    try:
        with TestClient(api.app) as client:
            endpoint = "/api/v1/admin/optional-features"
            assert client.post(endpoint, json={"registration_invite_code": "x" * 101}).status_code == 400
            assert not SystemConfig(api.db.db_path).get_config("registration_invite_code")
            assert client.post(endpoint, json={"registration_invite_code": "x" * 100}).status_code == 200
            response = client.post(
                "/api/v1/auth/register",
                json=dict(
                    username="boundaryuser",
                    email="boundary@example.com",
                    password="test-password123",
                    invite_code="x" * 100,
                ),
            )
            assert response.status_code == 201
    finally:
        api.app.dependency_overrides.clear()


def prepare_zip_pdf(db, tmp_path, monkeypatch, by_size=False):
    monkeypatch.setenv("PDF_SPLIT_ENABLED", "true")
    monkeypatch.setenv("PDF_SPLIT_THRESHOLD_PAGES", "100" if by_size else "2")
    monkeypatch.setenv("PDF_SPLIT_CHUNK_SIZE", "20" if by_size else "2")
    monkeypatch.setenv("PDF_SPLIT_SIZE_MB", "1" if by_size else "20")
    monkeypatch.setenv("RUSTFS_ENABLED", "false")
    pdf = tmp_path / "member.pdf"
    with pikepdf.new() as book:
        for _ in range(2 if by_size == "sparse" else 20 if by_size else 5):
            book.add_blank_page(page_size=(100, 100))
        book.save(pdf)
    if by_size:
        # Oversized but valid PDF; page-count threshold deliberately not reached.
        with pdf.open("ab") as f:
            f.write(b"\n%" + b"x" * (2 * 1024 * 1024) + b"\n")
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as z:
        z.write(pdf, "member.pdf")
    options = {"use_rustfs": False}
    root = db.create_task(
        "bundle.zip",
        str(archive),
        backend="pipeline",
        options=options,
        webhook_config={"url": "https://callback.example", "timeout": 1, "max_attempts": 2},
    )
    output = tmp_path / "outputs"
    output.mkdir()
    obj = make_worker(db, output)
    obj._process_task(db.get_next_task(obj.worker_id))
    member = db.get_child_tasks(root)[0]["task_id"]
    obj._process_with_mineru = lambda *args: pytest.fail("Whole ZIP PDF was parsed without splitting")
    obj._process_task(db.get_next_task(obj.worker_id))
    assert db.get_task(member)["is_parent"]
    chunks = db.get_child_tasks(member)
    assert len(chunks) > 1
    assert all("chunk_info" in json.loads(c["options"]) for c in chunks)
    return obj, root, member, chunks


@pytest.mark.parametrize("by_size", [False, True, "sparse"])
def test_zip_pdf_real_split_and_both_merges(db, tmp_path, monkeypatch, by_size):
    obj, root, member, chunks = prepare_zip_pdf(db, tmp_path, monkeypatch, by_size)
    calls = []

    def parse_chunk(path, options):
        assert options["use_rustfs"] is False
        info = options["chunk_info"]
        with pikepdf.open(path) as pdf:
            assert len(pdf.pages) == info["page_count"]
        calls.append(info["start_page"])
        out = Path(obj.output_dir) / Path(path).stem
        (out / "images").mkdir(parents=True)
        (out / "images/same.png").write_bytes(str(info["start_page"]).encode())
        (out / "result.md").write_text(
            f"Pages {info['start_page']}-{info['end_page']}\n![figure](images/same.png)\nEnd of chunk"
        )
        (out / "result.json").write_text(json.dumps([{"page_idx": 0, "img_path": "images/same.png"}]))
        return {"result_path": str(out)}

    obj._process_with_mineru = parse_chunk
    for _ in chunks:
        obj._process_task(db.get_next_task(obj.worker_id))
    assert len(calls) == len(chunks)
    assert db.get_task(member)["status"] == db.get_task(root)["status"] == "completed"
    assert db.get_task(root)["child_completed"] == 1
    out = Path(db.get_task(root)["result_path"])
    result = json.loads((out / "result.json").read_text())["documents"][0]
    assert result["content"].count("End of chunk") == len(chunks)
    assert len(image_filenames(result["content"])) == len(chunks)
    assert [page["page_idx"] for page in result["data"]] == sorted(start - 1 for start in calls)
    for page in result["data"]:
        assert (out / page["img_path"]).is_file()
    with db.get_cursor() as c:
        rows = c.execute("SELECT task_id,task_status FROM webhook_deliveries").fetchall()
        assert [(r["task_id"], r["task_status"]) for r in rows] == [(root, "completed")]


@pytest.mark.parametrize("failure_stage", ["parse", "pdf_merge", "zip_merge", "timeout"])
def test_nested_pdf_failure_reaches_zip_once(db, tmp_path, monkeypatch, failure_stage):
    obj, root, member, chunks = prepare_zip_pdf(db, tmp_path, monkeypatch)
    if failure_stage == "timeout":
        leaf = chunks[0]["task_id"]
        with db.get_cursor() as c:
            c.execute(
                "UPDATE tasks SET status='processing',retry_count=3,started_at=datetime('now','-120 minutes') WHERE task_id=?",
                (leaf,),
            )
        db.reset_stale_tasks(60)
    else:

        def parse(path, options):
            if failure_stage == "parse":
                raise RuntimeError("accepted parsing failure")
            out = Path(obj.output_dir) / Path(path).stem
            out.mkdir()
            (out / "result.md").write_text("chunk output")
            return {"result_path": str(out)}

        obj._process_with_mineru = parse
        merge = obj._merge_parent_task_results

        def fail_merge(tid):
            if tid == (member if failure_stage == "pdf_merge" else root):
                raise RuntimeError("accepted merge failure")
            return merge(tid)

        if failure_stage != "parse":
            obj._merge_parent_task_results = fail_merge
        for _ in range(1 if failure_stage == "parse" else len(chunks)):
            if failure_stage == "parse":
                with pytest.raises(RuntimeError):
                    obj._process_task(db.get_next_task(obj.worker_id))
            else:
                obj._process_task(db.get_next_task(obj.worker_id))
    assert db.get_task(root)["status"] == "failed"
    assert db.get_task(member)["status"] == ("completed" if failure_stage == "zip_merge" else "failed")
    with db.get_cursor() as c:
        assert [(r[0], r[1]) for r in c.execute("SELECT task_id,task_status FROM webhook_deliveries")] == [
            (root, "failed")
        ]


def test_nested_cancel_and_delete_cover_grandchildren(api, tmp_path, monkeypatch):
    from auth.dependencies import get_current_active_user
    from fastapi.testclient import TestClient

    obj, root, member, chunks = prepare_zip_pdf(api.db, tmp_path, monkeypatch)
    monkeypatch.setattr(api, "OUTPUT_DIR", Path(obj.output_dir))
    leaf_ids = [c["task_id"] for c in chunks]
    assert api.db.cancel_task(leaf_ids[0])
    for tid in [root, member, *leaf_ids]:
        assert api.db.get_task(tid)["status"] == "cancelled"
        assert not api.db.update_task_status(tid, "completed")
    assert api.db.reap_stale_parent_tasks(0) == 0
    api.app.dependency_overrides[get_current_active_user] = lambda: SimpleNamespace(
        user_id="admin", username="admin", has_permission=lambda p: True
    )
    try:
        with TestClient(api.app) as client:
            assert client.delete(f"/api/v1/tasks/{root}").status_code == 200
        assert all(api.db.get_task(tid) is None for tid in [root, member, *leaf_ids])
        assert all(not Path(c["file_path"]).exists() for c in chunks)
        assert not (Path(obj.output_dir) / "splits" / member).exists()
    finally:
        api.app.dependency_overrides.clear()


def test_nested_recovery_and_retry_do_not_orphan_grandchildren(db, tmp_path, monkeypatch):
    obj, root, member, chunks = prepare_zip_pdf(db, tmp_path, monkeypatch)
    with db.get_cursor() as c:
        c.execute("UPDATE tasks SET started_at=datetime('now','-120 minutes') WHERE task_id=?", (member,))
        c.executemany("UPDATE tasks SET status='completed' WHERE task_id=?", [(c["task_id"],) for c in chunks])
    assert db.reap_stale_parent_tasks(60) == 1
    assert db.get_task(root)["status"] == "processing"
    assert db.get_task(member)["status"] == "pending"
    assert all(db.get_task(c["task_id"]) is None for c in chunks)
    obj._process_task(db.get_next_task(obj.worker_id))
    new_chunks = db.get_child_tasks(member)
    assert len(new_chunks) == len(chunks)
    # Even if an intermediate parent failed, active grandchildren prohibit root retry.
    set_status(db, member, "failed")
    set_status(db, root, "failed")
    assert not db.retry_task(root)
    assert db.cancel_task(new_chunks[0]["task_id"])
    assert db.retry_task(root)
    obj._process_task(db.get_next_task(obj.worker_id))
    assert db.get_task(member) is None
    assert all(db.get_task(c["task_id"]) is None for c in new_chunks)
    assert len(db.get_child_tasks(root)) == 1


def test_zip_pdf_split_failure_never_falls_back_to_whole_parse(db, tmp_path, monkeypatch):
    monkeypatch.setenv("PDF_SPLIT_THRESHOLD_PAGES", "1")
    monkeypatch.setenv("PDF_SPLIT_ENABLED", "true")
    root = hook_task(db)
    db.convert_to_parent_task(root)
    pdf = tmp_path / "document.pdf"
    with pikepdf.new() as book:
        book.add_blank_page()
        book.add_blank_page()
        book.save(pdf)
    member = db.create_child_task(root, "document.pdf", str(pdf), backend="pipeline", options={"archive_index": 0})

    def fail_split(*args):
        raise RuntimeError("disk full while splitting")

    monkeypatch.setattr("utils.pdf_utils.split_pdf_file", fail_split)
    obj = make_worker(db, tmp_path)
    obj._process_with_mineru = lambda *a: pytest.fail("Unsafe fallback to whole-PDF parsing")
    with pytest.raises(RuntimeError, match="disk full"):
        obj._process_task(db.get_next_task(obj.worker_id))
    assert db.get_task(member)["status"] == db.get_task(root)["status"] == "failed"
