"""Customer-free, actual DOC and image conversion checks for host preparation."""
from __future__ import annotations

from pathlib import Path

from .converter_probe import FORMATS, MARKER, probe_formats
from .image_input import IMAGE_SUFFIXES, prepare_image
from .legacy_doc import convert_legacy_doc
from .markdown_converter import ConversionFailed, ConverterUnavailable, convert_to_markdown
from .ocr_provider import AutoOcrProvider, OcrFailed, OcrUnavailable

INTAKE_FORMATS = (*FORMATS, "doc", "jpg", "jpeg", "png")
PROBE_ROOT = Path(__file__).parent / "assets/intake-probe"


def probe_intake_formats(formats: tuple[str, ...]) -> dict[str, bool]:
    results = probe_formats(tuple(kind for kind in formats if kind in FORMATS))
    for kind in formats:
        if kind in FORMATS:
            continue
        try:
            if kind == "doc":
                converted = convert_legacy_doc((PROBE_ROOT / "legacy.doc").read_bytes())
                results[kind] = MARKER in convert_to_markdown(converted.payload, ".docx").text
            elif "." + kind in IMAGE_SUFFIXES:
                payload = (PROBE_ROOT / ("image.png" if kind == "png" else "image.jpg")).read_bytes()
                page = prepare_image(payload, "." + kind, "SRC-" + "0" * 24).pages[0]
                text = AutoOcrProvider(require_numeric_agreement=True).recognize(page.payload)
                results[kind] = text.confidence >= 0.85 and MARKER in "".join(text.text.split())
            else:
                raise ValueError("unknown intake format")
        except (ConverterUnavailable, ConversionFailed, OcrUnavailable, OcrFailed, OSError, ValueError):
            results[kind] = False
    return results
