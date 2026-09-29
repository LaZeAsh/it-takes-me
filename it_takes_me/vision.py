"""Prepare game captures for model vision while preserving full-resolution recordings."""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Literal

from PIL import Image

ScreenHalf = Literal["left", "right"]
FrameDetail = Literal["low", "high"]


@dataclass(frozen=True, slots=True)
class ModelFrame:
    data: bytes
    media_type: str
    width: int
    height: int
    detail: FrameDetail


@dataclass(frozen=True, slots=True)
class ModelFrameEncoder:
    """Crop the split screen and bound pixels before a frame reaches the model."""

    half: ScreenHalf
    low_max_px: int = 512
    high_max_px: int = 1536
    jpeg_quality: int = 85

    def encode(self, png: bytes, *, detail: FrameDetail = "low") -> ModelFrame:
        if detail not in ("low", "high"):
            raise ValueError(f"unknown frame detail: {detail}")
        max_px = self.low_max_px if detail == "low" else self.high_max_px
        if max_px <= 0:
            raise ValueError("model frame maximum size must be positive")

        with Image.open(io.BytesIO(png)) as source:
            rgb = source.convert("RGB")
        split = rgb.width // 2
        box = (
            (0, 0, split, rgb.height) if self.half == "left" else (split, 0, rgb.width, rgb.height)
        )
        view = rgb.crop(box)
        view.thumbnail((max_px, max_px), Image.Resampling.LANCZOS, reducing_gap=2.0)

        output = io.BytesIO()
        view.save(
            output,
            format="JPEG",
            quality=self.jpeg_quality,
            optimize=True,
            progressive=True,
        )
        return ModelFrame(
            data=output.getvalue(),
            media_type="image/jpeg",
            width=view.width,
            height=view.height,
            detail=detail,
        )
