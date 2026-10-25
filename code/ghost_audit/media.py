"""Text extraction for binary evidence: PDF text layers and screenshot OCR.

OCR results are cached on disk (keyed by file content hash) so repeated runs are
fast and deterministic. PDFs use the embedded text layer (pypdf); screenshots use
RapidOCR (ONNX, runs locally, no API key needed).
"""
from __future__ import annotations

import hashlib
import json
import os

from .common import CACHE_DIR

_ocr_engine = None


def _cache_path(path: str, kind: str) -> str:
    with open(path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()[:24]
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{kind}_{digest}.json")


def pdf_text(path: str) -> str:
    cache = _cache_path(path, "pdf")
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as fh:
            return json.load(fh)["text"]
    from pypdf import PdfReader

    text = "\n".join((page.extract_text() or "") for page in PdfReader(path).pages)
    with open(cache, "w", encoding="utf-8") as fh:
        json.dump({"text": text}, fh)
    return text


def image_text(path: str) -> str:
    """OCR a screenshot and return its text, one visual row per line."""
    global _ocr_engine
    cache = _cache_path(path, "ocr")
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as fh:
            return json.load(fh)["text"]
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR

        _ocr_engine = RapidOCR()
    result, _ = _ocr_engine(path)
    boxes = sorted(result or [], key=lambda r: (round(r[0][0][1] / 12), r[0][0][0]))
    lines, current, row = [], [], None
    for box, text, _conf in boxes:
        y = round(box[0][1] / 12)
        if row is not None and y != row:
            lines.append(" | ".join(current))
            current = []
        row = y
        current.append(text)
    if current:
        lines.append(" | ".join(current))
    text = "\n".join(lines)
    with open(cache, "w", encoding="utf-8") as fh:
        json.dump({"text": text}, fh)
    return text
