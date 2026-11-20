import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from audio_engines.sensevoice_engine import SenseVoiceEngine
from video_engines.video_engine import VideoProcessingEngine


@pytest.fixture
def video_pipeline(tmp_path, monkeypatch):
    video_path = tmp_path / "会议录像.mp4"
    video_path.touch()
    audio_path = tmp_path / "extracted.wav"

    def extract_audio(**kwargs):
        assert kwargs == {"video_path": str(video_path), "audio_format": "wav"}
        audio_path.write_bytes(b"mock audio")
        return str(audio_path)

    audio_engine = object.__new__(SenseVoiceEngine)
    audio_engine.enable_speaker_diarization = False
    model = SimpleNamespace(generate=lambda **kwargs: [{"text": "会议内容", "language": ["zh"]}])
    monkeypatch.setattr(audio_engine, "_load_model", lambda **kwargs: model)

    video_engine = object.__new__(VideoProcessingEngine)
    monkeypatch.setattr(video_engine, "extract_audio", extract_audio)
    monkeypatch.setattr(video_engine, "_load_audio_engine", lambda: audio_engine)

    return video_engine, audio_engine, video_path, audio_path


@pytest.mark.parametrize("enable_keyframe_ocr", [False, True])
@pytest.mark.parametrize("existing_source", [None, {"file_type": "audio", "sample_rate": 16000}])
def test_video_merges_real_audio_output_and_preserves_source(
    video_pipeline, tmp_path, monkeypatch, enable_keyframe_ocr, existing_source
):
    video_engine, audio_engine, video_path, audio_path = video_pipeline
    output_path = tmp_path / "output"
    parse_result = audio_engine._parse_result

    def audio_result(*args):
        result = parse_result(*args)
        assert "source" not in result
        if existing_source is not None:
            result["source"] = existing_source.copy()
        return result

    monkeypatch.setattr(audio_engine, "_parse_result", audio_result)

    keyframe_result = {
        "success": True,
        "total_keyframes": 1,
        "keyframes": [{"timestamp": 1.0, "text": "会议议程"}],
        "markdown_file": str(output_path / "keyframes.md"),
        "json_file": str(output_path / "keyframes.json"),
        "markdown": "# 关键帧 OCR\n\n会议议程",
    }
    if enable_keyframe_ocr:
        keyframe_module = ModuleType("video_engines.keyframe_extractor")
        keyframe_module.VideoOCREngine = lambda **kwargs: SimpleNamespace(process=lambda **kwargs: keyframe_result)
        monkeypatch.setitem(sys.modules, keyframe_module.__name__, keyframe_module)

    result = video_engine.parse(str(video_path), str(output_path), enable_keyframe_ocr=enable_keyframe_ocr)

    expected_source = dict(existing_source or {})
    expected_source.update(file_type="video", video_format="mp4", original_filename=video_path.name)
    assert result["success"] is True
    assert result["json_data"]["type"] == "video"
    assert result["json_data"]["source"] == expected_source
    assert result["json_data"]["transcript"] == "会议内容"
    assert result["json_data"]["metadata"]["language"] == "zh"
    assert result["json_data"]["keyframe_ocr"]["enabled"] is enable_keyframe_ocr
    assert json.loads((output_path / "content.json").read_text(encoding="utf-8")) == result["json_data"]
    assert json.loads((output_path / f"{video_path.stem}.json").read_text(encoding="utf-8")) == result["json_data"]
    assert Path(result["json_file"]) == output_path / "content.json"
    assert (output_path / "content.md").read_text(encoding="utf-8") == result["markdown"]
    assert video_path.name in result["markdown"]
    assert "会议内容" in result["markdown"]
    if enable_keyframe_ocr:
        assert result["json_data"]["keyframe_ocr"]["keyframes"] == keyframe_result["keyframes"]
        assert "会议议程" in result["markdown"]
    assert not audio_path.exists()
