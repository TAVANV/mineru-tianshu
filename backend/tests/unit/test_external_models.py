import json

import pytest
import yaml

from check_external_models import check_models
from download_models import MODEL_COMPLETE_MARKER, generate_mineru_config, reuse_models
from offline_models import MODELS, configure_mineru, verify_model


def prepare_snapshot(root, key):
    spec = MODELS[key]
    target = root / spec["target_dir"]
    sizes = {}
    for relative in spec.get("verify", ["model.safetensors"]):
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic weight")
        sizes[relative] = path.stat().st_size
    if spec.get("revision"):
        (target / MODEL_COMPLETE_MARKER).write_text(json.dumps({"version": 1, "files": sizes}))
    return target


@pytest.mark.parametrize("vlm", ["mineru_vlm", "mineru_vlm_pro"])
def test_external_validation_reads_config_without_modifying_models(tmp_path, vlm):
    prepare_snapshot(tmp_path, "mineru_pipeline")
    prepare_snapshot(tmp_path, vlm)
    configure_mineru(tmp_path, vlm)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    check_models(tmp_path)
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_external_validation_rejects_missing_weights_and_shards(tmp_path):
    torch_root = prepare_snapshot(tmp_path, "mineru_pipeline")
    vlm_root = prepare_snapshot(tmp_path, "mineru_vlm")
    configure_mineru(tmp_path, "mineru_vlm")
    ocr = torch_root / "OCR/paddleocr/ch_PP-OCRv6_small_rec_infer.safetensors"
    payload = ocr.read_bytes()
    ocr.unlink()
    with pytest.raises(ValueError, match="pipeline"):
        check_models(tmp_path)
    ocr.write_bytes(payload)
    (vlm_root / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"tensor": "missing-shard.safetensors"}})
    )
    with pytest.raises(ValueError, match="shards"):
        check_models(tmp_path)


def test_invalid_activation_preserves_existing_config(tmp_path):
    prepare_snapshot(tmp_path, "mineru_vlm")
    configure_mineru(tmp_path, "mineru_vlm")
    config = tmp_path / "config.yaml"
    before = config.read_bytes()
    with pytest.raises(ValueError, match="未经验证"):
        configure_mineru(tmp_path, "mineru_vlm_pro")
    assert config.read_bytes() == before
    generate_mineru_config(tmp_path)
    data = yaml.safe_load(config.read_text())
    assert data["model"] == {
        "source": "local",
        "base_dir": "/app/models",
        "small_backend": "torch",
        "vlm": {"engine": "auto", "model": "MinerU-1.2B"},
    }
    assert data["tianshu"]["vllm_model_dir"] == MODELS["mineru_vlm"]["target_dir"]


def test_reuse_copies_unchanged_models_and_leaves_old_mineru_config(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    new.mkdir()
    source = prepare_snapshot(old, "mineru_vlm_pro")
    (old / "SenseVoiceSmall").mkdir()
    (old / "SenseVoiceSmall/model.pt").write_bytes(b"audio")
    (old / "PDF-Extract-Kit-1.0").mkdir()
    (old / "mineru.json").write_text("{}")
    reuse_models(old, new)
    assert (new / "SenseVoiceSmall/model.pt").read_bytes() == b"audio"
    assert (new / source.name / "model.safetensors").read_bytes() == b"synthetic weight"
    assert not (new / source.name / MODEL_COMPLETE_MARKER).exists()
    assert verify_model(old, "mineru_vlm_pro", MODELS["mineru_vlm_pro"])[0]
    assert not (new / "PDF-Extract-Kit-1.0").exists()
    assert not (new / "mineru.json").exists()
