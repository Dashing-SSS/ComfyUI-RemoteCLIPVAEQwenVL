# -*- coding: utf-8 -*-
"""imaging.py —— IMAGE 张量 -> PNG base64（服务端按 data:image/png 收）。"""

from __future__ import annotations

import base64
import io

import numpy as np
import torch
from PIL import Image


def _fit(t: torch.Tensor, max_pixels: int) -> torch.Tensor:
    if max_pixels <= 0:
        return t
    h, w = int(t.shape[1]), int(t.shape[2])
    if h * w <= max_pixels:
        return t
    scale = (max_pixels / float(h * w)) ** 0.5
    nh, nw = max(8, int(h * scale)), max(8, int(w * scale))
    t4 = t.permute(0, 3, 1, 2).float()
    t4 = torch.nn.functional.interpolate(t4, size=(nh, nw), mode="bilinear", antialias=True)
    return t4.permute(0, 2, 3, 1)


def _one(arr: np.ndarray) -> str:
    h, w, c = arr.shape
    mode = "L" if c == 1 else ("RGBA" if c == 4 else "RGB")
    img = Image.fromarray(arr.squeeze(-1) if c == 1 else arr, mode=mode)
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=3)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def tensor_to_png_frames(image, max_pixels: int = 1_500_000) -> list[str]:
    """[B,H,W,C] float(0~1) -> 每帧一张 PNG 的 base64（不含 data: 前缀）。"""
    if not torch.is_tensor(image):
        raise TypeError(f"需要 torch.Tensor，收到 {type(image)!r}")
    t = image.detach()
    if t.device.type != "cpu":
        t = t.cpu()
    if t.dtype != torch.float32:
        t = t.float()
    if t.dim() == 3:
        t = t.unsqueeze(0)
    if t.dim() != 4:
        raise ValueError(f"IMAGE 维度应为 4（B,H,W,C），收到 {tuple(t.shape)}")
    t = _fit(t, max_pixels).clamp(0.0, 1.0).mul_(255.0).round_()
    if t.shape[-1] not in (1, 3, 4):
        t = t[..., :3] if t.shape[-1] > 3 else t.repeat(1, 1, 1, 3)
    arr = t.to(torch.uint8).numpy()
    return [_one(a) for a in arr]
