"""使用外置 TorchScript 权重的离线 LaMa 修复器。"""

import os
from pathlib import Path
from typing import Union

import numpy as np
import torch
from PIL import Image


class SimpleLama:
    def __init__(self, device: Union[str, torch.device] = "cpu") -> None:
        model_path = os.getenv("LAMA_MODEL")
        if not model_path or not Path(model_path).is_file():
            raise FileNotFoundError(f"LAMA_MODEL 必须指向已存在的本地 TorchScript 权重: {model_path or '未配置'}")

        self.device = torch.device(device)
        self.model = torch.jit.load(model_path, map_location=self.device)
        self.model.eval()
        self.model.to(self.device)

    def __call__(self, image: Union[Image.Image, np.ndarray], mask: Union[Image.Image, np.ndarray]) -> Image.Image:
        if isinstance(image, np.ndarray):
            image = Image.fromarray(image)
        if isinstance(mask, np.ndarray):
            if mask.ndim == 3 and mask.shape[2] == 1:
                mask = mask[:, :, 0]
            mask = Image.fromarray(mask)
        if image.size != mask.size:
            raise ValueError("LaMa 图像与掩码尺寸必须一致")

        width, height = image.size
        image_array = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
        mask_array = (np.asarray(mask.convert("L")) > 0).astype(np.float32)
        pad_height, pad_width = -height % 8, -width % 8
        # 对称填充同样支持单像素图像，避免反射填充对最小边长的限制。
        image_array = np.pad(image_array, ((0, pad_height), (0, pad_width), (0, 0)), mode="symmetric")
        mask_array = np.pad(mask_array, ((0, pad_height), (0, pad_width)), mode="symmetric")
        image_tensor = torch.from_numpy(image_array.transpose(2, 0, 1)).unsqueeze(0).to(self.device)
        mask_tensor = torch.from_numpy(mask_array).unsqueeze(0).unsqueeze(0).to(self.device)

        with torch.inference_mode():
            output = self.model(image_tensor, mask_tensor)

        result = output[0, :, :height, :width].permute(1, 2, 0).detach().cpu().numpy()
        return Image.fromarray(np.clip(result * 255.0, 0, 255).astype(np.uint8))
