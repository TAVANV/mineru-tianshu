import json

import pytest

from check_external_models import check_models
from offline_models import MODELS, generate_mineru_json, verify_model


def prepare_models(root, vlm="MinerU2.5-2509-1.2B"):
    for relative in MODELS["mineru_pipeline"]["verify"]:
        target = root / "PDF-Extract-Kit-1.0" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"synthetic weight")
    target = root / vlm
    target.mkdir()
    (target / "config.json").write_text("{}")
    (target / "model.safetensors").write_bytes(b"synthetic weight")
    (root / "mineru.json").write_text(
        json.dumps(
            {
                "model-source": "local",
                "models-dir": {"pipeline": "/app/models/PDF-Extract-Kit-1.0", "vlm": f"/app/models/{vlm}"},
            }
        )
    )


@pytest.mark.parametrize("vlm", ["MinerU2.5-2509-1.2B", "MinerU2.5-Pro-2605-1.2B"])
def test_external_validation_is_read_only_and_preserves_selected_model(tmp_path, vlm):
    prepare_models(tmp_path, vlm)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    check_models(tmp_path)
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_external_validation_rejects_missing_ocrv6_and_weight_shards(tmp_path):
    prepare_models(tmp_path)
    ocr = tmp_path / "PDF-Extract-Kit-1.0" / MODELS["mineru_pipeline"]["verify"][2]
    ocr.unlink()
    with pytest.raises(ValueError, match="pipeline"):
        check_models(tmp_path)
    ocr.write_bytes(b"synthetic weight")
    (tmp_path / "MinerU2.5-2509-1.2B" / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"tensor": "missing-shard.safetensors"}})
    )
    with pytest.raises(ValueError, match="shards"):
        check_models(tmp_path)


def test_invalid_activation_does_not_change_existing_config(tmp_path):
    prepare_models(tmp_path)
    config = tmp_path / "mineru.json"
    before = config.read_bytes()
    with pytest.raises(ValueError, match="unverified"):
        generate_mineru_json(tmp_path, "mineru_vlm_pro")
    assert config.read_bytes() == before
    generate_mineru_json(tmp_path)
    assert json.loads(config.read_text())["models-dir"]["vlm"].endswith("MinerU2.5-2509-1.2B")


def test_old_pipeline_config_is_corrected_without_changing_vlm(tmp_path):
    prepare_models(tmp_path)
    config = tmp_path / "mineru.json"
    data = json.loads(config.read_text())
    data["models-dir"]["pipeline"] += "/models"
    config.write_text(json.dumps(data))
    generate_mineru_json(tmp_path)
    check_models(tmp_path)
    assert verify_model(tmp_path, "mineru_pipeline", MODELS["mineru_pipeline"])[0]
