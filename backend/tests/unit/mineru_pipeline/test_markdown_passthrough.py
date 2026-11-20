r"""parse() 必须原样保留 MinerU 输出的 Markdown

曾经的 _clean_markdown 会全局替换 ~、剥掉 \mathrm{}、双重反转义 HTML，
并用 (\S+)\s+\1 去重：既删掉表格中相邻的同值单元格，又在长无空白文本上
退化成平方复杂度导致任务卡死。已移除，这里防止回归。
"""

import json
import sys
import types
from pathlib import Path

import pytest

# 每一段都是旧清洗逻辑会改坏的真实内容
SEGMENTS = [
    "温度范围 10~20 ℃，路径 ~/data",
    r"$\mathrm{kg}\cdot\mathrm{m}$",
    "字面量写作 &amp;lt; 表示小于号",
    "<table>\n  <tr>\n    <td>a &lt; b</td>\n    <td>0</td>\n    <td>0</td>\n    <td>5</td>\n  </tr>\n</table>",
    "the the result",
]
RAW_MARKDOWN = "\n\n".join(SEGMENTS)


@pytest.fixture
def sdk(monkeypatch, mocker):
    from mineru_pipeline import engine as engine_module

    middle = types.SimpleNamespace(
        pages=[types.SimpleNamespace(page_idx=0)],
        to_dict=lambda: {"schema_version": "test"},
    )
    settings = types.SimpleNamespace(server_url="", api_key="")

    def vlm_config(server_url, model):
        return types.SimpleNamespace(server_url=server_url.removesuffix("/v1") + "/", model=model, api_key="")

    parser = mocker.Mock(return_value=types.SimpleNamespace(middle_json=middle))
    markdown = mocker.Mock(return_value=RAW_MARKDOWN)
    v1 = mocker.Mock(return_value=[])
    v2 = mocker.Mock(return_value=[[]])
    materialize = mocker.Mock(return_value=(middle, {}))
    for module, attributes in {
        "docvortex.export": {"materialize_middle": materialize, "validate_materialized_assets": mocker.Mock()},
        "mineru.config": {
            "VlmConfig": vlm_config,
            "get_config": lambda: types.SimpleNamespace(model=types.SimpleNamespace(vlm=settings)),
        },
        "mineru.parser": {"parse": parser},
        "mineru.render": {"render_markdown": markdown, "render_content_list": v1, "render_content_list_v2": v2},
    }.items():
        fake = types.ModuleType(module)
        vars(fake).update(attributes)
        monkeypatch.setitem(sys.modules, module, fake)

    monkeypatch.setattr(engine_module.MinerUPipelineEngine, "_instance", None)
    monkeypatch.setattr(engine_module, "threading", types.SimpleNamespace(Thread=mocker.Mock()))
    engine = engine_module.MinerUPipelineEngine()
    monkeypatch.setattr(engine, "_wait_for_server", mocker.Mock())
    return types.SimpleNamespace(engine=engine, parser=parser, middle=middle, v1=v1, v2=v2, materialize=materialize)


def test_markdown_is_returned_and_written_unchanged(sdk, tmp_path):
    source = tmp_path / "中文.pdf"
    source.write_bytes(b"%PDF-1.4")
    output = tmp_path / "out"

    result = sdk.engine.parse(str(source), str(output))
    written = (output / "result.md").read_text(encoding="utf-8")

    sdk_source = sdk.parser.call_args.args[0]
    assert sdk_source.name == "source.pdf"
    assert not sdk_source.exists()
    assert result["markdown"] == written == RAW_MARKDOWN


@pytest.mark.parametrize(
    ("backend", "url", "local_url", "tier", "expected_url"),
    [
        ("auto", None, None, "basic", ""),
        ("pipeline", None, "http://local:30024/v1", "basic", ""),
        ("vlm-auto-engine", None, "http://local:30024/v1", "standard", "http://local:30024/"),
        ("vlm-http-client", "https://remote/prefix/v1", "http://local/v1", "standard", "https://remote/prefix/"),
        ("hybrid-auto-engine", None, None, "advanced", ""),
        ("hybrid-http-client", None, "http://local:30024/v1", "advanced", "http://local:30024/"),
    ],
)
def test_legacy_backend_uses_public_tier_and_vlm_config(sdk, tmp_path, backend, url, local_url, tier, expected_url):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.4")
    sdk.engine.vlm_api_base = local_url
    sdk.engine.parse(str(source), str(tmp_path / "out"), {"parse_mode": backend, "server_url": url})
    kwargs = sdk.parser.call_args.kwargs
    assert kwargs["tier"] == tier
    config = kwargs["vlm_config"]
    assert (config.server_url if config else "") == expected_url
    if expected_url:
        assert config.model == "MinerU-1.2B"
        sdk.engine._wait_for_server.assert_called_once_with(expected_url)


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, ""),
        ({"start_page": 1, "end_page": 4}, "2-5"),
        ({"start_page_id": "2", "end_page_id": "-1"}, "3-r1"),
        ({"start_page": None, "end_page": 0}, "1-1"),
        ({"start_page": "invalid", "end_page": ""}, ""),
    ],
)
def test_legacy_pdf_page_range_and_force_ocr_are_passed_to_sdk(sdk, tmp_path, options, expected):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.4")
    sdk.engine.parse(
        str(source),
        str(tmp_path / "out"),
        options | {"method": "txt", "force_ocr": True, "lang": "ch", "formula_enable": False, "table_enable": False},
    )
    assert sdk.parser.call_args.kwargs == {
        "tier": "basic",
        "ocr_mode": "ocr",
        "page_range": expected,
        "vlm_config": None,
    }


def test_rendered_paths_keep_image_refs_and_source_page_indices(sdk, tmp_path):
    from output_normalizer import normalize_output

    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.4")
    output = tmp_path / "out"
    sdk.middle.pages[0].page_idx = 2
    v1 = [{"type": "image", "img_path": "images/page_2_image_0.jpg", "page_idx": 2, "bbox": [12, 35, 880, 970]}]
    v2 = [[{"type": "image", "content": {"image_source": {"path": v1[0]["img_path"]}}, "bbox": v1[0]["bbox"]}]]
    sdk.v1.return_value = v1
    sdk.v2.return_value = v2
    sdk.materialize.return_value = (sdk.middle, {v1[0]["img_path"]: b"image bytes"})

    result = sdk.engine.parse(str(source), str(output), {"start_page": 2, "end_page": 2})

    assert result["json_content"] == [[], [], v2[0]]
    files = output / "mineru"
    assert json.loads((files / "result_content_list.json").read_text()) == v1
    assert Path(result["json_path"]) == files / "result_content_list_v2.json"
    assert json.loads(Path(result["json_path"]).read_text()) == result["json_content"]
    assert (output / v1[0]["img_path"]).read_bytes() == b"image bytes"
    sdk.v1.assert_called_once_with(sdk.middle)
    sdk.v2.assert_called_once_with(sdk.middle)

    normalize_output(output, use_rustfs=False)
    assert json.loads((output / "result.json").read_text()) == v1
    assert (output / "result_origin.pdf").read_bytes() == source.read_bytes()


def test_basic_smoke_fixture_matches_existing_bbox_and_page_contract(sdk, tmp_path):
    fixture = Path(__file__).parents[2] / "fixtures" / "mineru_4_2_3_basic_sample.json"
    sample = json.loads(fixture.read_text(encoding="utf-8"))
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.4")
    sdk.middle.pages = [types.SimpleNamespace(page_idx=index) for index in sample["page_indices"]]
    sdk.v1.return_value = sample["content_list"]
    sdk.v2.return_value = sample["content_list_v2"]

    result = sdk.engine.parse(str(source), str(tmp_path / "out"))
    v1_block = json.loads((tmp_path / "out" / "result.json").read_text())[0]
    v2_block = result["json_content"][0][0]
    assert v1_block["page_idx"] == 0
    assert all(0 <= coordinate <= 1000 for coordinate in v1_block["bbox"])
    assert v1_block["bbox"] == v2_block["bbox"]
    assert v1_block["text"] == v2_block["content"]["paragraph_content"][0]["content"] == sample["markdown"]
