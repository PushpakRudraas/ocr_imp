"""Which backends this process uses, read from the environment. Shared by the API and the enrichment worker so both
build identical pipelines (the worker simply never needs OCR, and the API never needs the NLP models)."""
from __future__ import annotations

import argparse
import os


def default_components() -> argparse.Namespace:
    """OCR defaults to RapidOCR when it is installed (needed only for scanned PDFs)."""
    ocr = os.getenv("NP_OCR", "auto")
    if ocr == "auto":
        try:
            import rapidocr_onnxruntime  # noqa: F401
            ocr = "rapidocr"
        except Exception:
            ocr = "none"
    return argparse.Namespace(ocr=ocr, ocr_lang=os.getenv("NP_OCR_LANG", "eng"),
                              translator=os.getenv("NP_TRANSLATOR", "none"), embedder=os.getenv("NP_EMBEDDER", "hashing"),
                              store=os.getenv("NP_STORE", "sqlite"), index=os.getenv("NP_INDEX", "memory"),
                              graph=os.getenv("NP_GRAPH", "none"), qdrant_path=None, judge=os.getenv("NP_JUDGE", "none"),
                              llm_refine=os.getenv("NP_LLM_REFINE", "") == "1", models=os.getenv("NP_ENRICH_MODELS", "none"),
                              device=os.getenv("NP_DEVICE", "auto"))
