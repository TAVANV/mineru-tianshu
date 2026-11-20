import pytest

torch = pytest.importorskip("torch")
np = pytest.importorskip("numpy")
Image = pytest.importorskip("PIL.Image")


class TinyInpainter(torch.nn.Module):
    def forward(self, image: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
        if image.size(2) % 8 != 0 or image.size(3) % 8 != 0:
            raise RuntimeError("模型输入尺寸必须是 8 的倍数")
        return image * (1.0 - mask) + mask * 0.75


@pytest.fixture
def lama_weights(tmp_path, monkeypatch):
    model_path = tmp_path / "tiny-lama.pt"
    torch.jit.script(TinyInpainter()).save(str(model_path))
    monkeypatch.setenv("LAMA_MODEL", str(model_path))
    return model_path


def test_lama_pads_and_crops_without_changing_unmasked_pixels(lama_weights):
    from remove_watermark.lama_inpainter import SimpleLama

    image = Image.new("RGB", (13, 9), (32, 64, 128))
    mask = np.zeros((9, 13), dtype=np.uint8)
    mask[2:5, 3:7] = 1
    result = SimpleLama(device="cpu")(image, Image.fromarray(mask))

    assert result.mode == "RGB"
    assert result.size == (13, 9)
    assert result.getpixel((0, 0)) == (32, 64, 128)
    assert result.getpixel((3, 2)) == (191, 191, 191)
    assert result.getpixel((7, 5)) == (32, 64, 128)


def test_lama_accepts_single_pixel_numpy_input(lama_weights):
    from remove_watermark.lama_inpainter import SimpleLama

    model = SimpleLama(device=torch.device("cpu"))
    result = model(np.zeros((1, 1, 3), dtype=np.uint8), np.full((1, 1), 255, dtype=np.uint8))

    assert model.device == torch.device("cpu")
    assert result.size == (1, 1)
    assert result.getpixel((0, 0)) == (191, 191, 191)


@pytest.mark.parametrize("configured", [False, True])
def test_lama_requires_existing_local_weights(tmp_path, monkeypatch, configured):
    from remove_watermark.lama_inpainter import SimpleLama

    if configured:
        monkeypatch.setenv("LAMA_MODEL", str(tmp_path / "missing.pt"))
    else:
        monkeypatch.delenv("LAMA_MODEL", raising=False)

    with pytest.raises(FileNotFoundError, match="LAMA_MODEL"):
        SimpleLama(device="cpu")


def test_lama_rejects_mismatched_mask_size(lama_weights):
    from remove_watermark.lama_inpainter import SimpleLama

    model = SimpleLama(device="cpu")
    with pytest.raises(ValueError, match="尺寸"):
        model(Image.new("RGB", (13, 9)), Image.new("L", (12, 9)))


def test_watermark_inpaint_uses_configured_device_and_local_lama(lama_weights):
    from remove_watermark.watermark_remover import LAMA_AVAILABLE, WatermarkRemover

    remover = object.__new__(WatermarkRemover)
    remover.device = "cpu"
    remover.use_lama = True
    remover.lama = None
    mask = np.full((9, 13), 255, dtype=np.uint8)

    result = remover.inpaint(Image.new("RGB", (13, 9)), mask)

    assert LAMA_AVAILABLE is True
    assert remover.use_lama is True
    assert remover.lama.device == torch.device("cpu")
    assert result.size == (13, 9)
    assert result.getpixel((0, 0)) == (191, 191, 191)


def test_watermark_inpaint_does_not_hide_missing_lama_weights(tmp_path, monkeypatch):
    from remove_watermark.watermark_remover import WatermarkRemover

    monkeypatch.setenv("LAMA_MODEL", str(tmp_path / "missing.pt"))
    remover = object.__new__(WatermarkRemover)
    remover.device = "cpu"
    remover.use_lama = True
    remover.lama = None

    with pytest.raises(FileNotFoundError, match="LAMA_MODEL"):
        remover.inpaint(Image.new("RGB", (13, 9)), np.full((9, 13), 255, dtype=np.uint8))

    assert remover.use_lama is True
