import json

import pytest

from output_normalizer import normalize_output
from tests.unit.worker.test_routing import load_worker_module, make_worker


@pytest.mark.parametrize(
    "configured,choice,uploads", [("true", False, False), ("false", None, False), ("false", True, True)]
)
def test_task_storage_choice_overrides_environment(tmp_path, monkeypatch, mocker, configured, choice, uploads):
    monkeypatch.setenv("RUSTFS_ENABLED", configured)
    images = tmp_path / "images"
    images.mkdir()
    (images / "a.png").write_bytes(b"image")
    (tmp_path / "result.md").write_text("![a](images/a.png)")
    upload = mocker.patch(
        "output_normalizer.base_output_normalizer.BaseOutputNormalizer._upload_images_to_rustfs",
        return_value={"a.png": "https://example.com/a.png"},
    )
    result = normalize_output(tmp_path, use_rustfs=choice)
    assert result["images_uploaded"] is uploads
    assert upload.called is uploads


def test_parent_merge_keeps_distinct_local_images_and_json(runtime_paths, monkeypatch, mocker):
    monkeypatch.setenv("RUSTFS_ENABLED", "true")
    worker = make_worker(load_worker_module(monkeypatch), mocker)
    worker.output_dir = runtime_paths["output_path"]
    children = []
    for i in range(2):
        directory = worker.output_dir / f"child-{i}"
        (directory / "images").mkdir(parents=True)
        (directory / "images" / "a (1).png").write_bytes(f"image-{i}".encode())
        (directory / "result.md").write_text("![a](<images/a (1).png>)")
        (directory / "result.json").write_text(json.dumps([{"page_idx": 0, "img_path": "images/a (1).png"}]))
        children.append(
            {"task_id": f"child-{i}", "result_path": str(directory), "status": "completed", "options": "{}"}
        )
    parent = {
        "file_path": str(runtime_paths["upload_path"] / "archive.zip"),
        "options": '{"use_rustfs":false}',
        "children": children,
    }
    worker.task_db.get_task_with_children.return_value = parent
    worker._ensure_pdf_in_output = mocker.Mock()
    upload = mocker.patch("output_normalizer.base_output_normalizer.BaseOutputNormalizer._upload_images_to_rustfs")
    worker._merge_parent_task_results("parent")
    output = worker.output_dir / "archive"
    assert sorted(p.read_bytes() for p in (output / "images").iterdir()) == [b"image-0", b"image-1"]
    data = json.loads((output / "result.json").read_text())
    assert len({row["img_path"] for row in data}) == 2
    for row in data:
        assert (output / row["img_path"]).is_file()
        assert row["img_path"] in (output / "result.md").read_text()
    upload.assert_not_called()
    worker.task_db.update_task_status.assert_called_once_with("parent", "completed", result_path=str(output))
