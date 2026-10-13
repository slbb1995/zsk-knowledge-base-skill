"""Decode a submitted PNG/JPEG into one complete native-resolution evidence page."""
from __future__ import annotations

import hashlib
from io import BytesIO
import warnings

from .contracts import PageArtifact
from .markdown_converter import ConverterUnavailable
from .page_renderer import RenderedPage, RenderedPages

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})


def prepare_image(payload: bytes, suffix: str, source_id: str) -> RenderedPages:
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as exc:
        raise ConverterUnavailable("Pillow is required in the ZSK host Python") from exc
    expected = "PNG" if suffix == ".png" else "JPEG"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as original:
                if original.format != expected or getattr(original, "n_frames", 1) != 1:
                    raise ValueError("image format differs from its name or has multiple frames")
                original.verify()
            with Image.open(BytesIO(payload)) as original:
                original.load()
                image = ImageOps.exif_transpose(original)
                if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                    rgba = image.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, "white")
                    image = Image.alpha_composite(background, rgba).convert("RGB")
                else:
                    image = image.convert("RGB")
                width, height = image.size
                output = BytesIO()
                image.save(output, format="PNG")
                png = output.getvalue()
    except (OSError, ValueError, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("submitted image cannot be decoded safely") from exc
    artifact = PageArtifact(f"{source_id}-PAGE-001", source_id, 1, "page-001.png",
                            hashlib.sha256(png).hexdigest(), width, height)
    return RenderedPages((RenderedPage(artifact, png),), 1, "pillow-native-image")
