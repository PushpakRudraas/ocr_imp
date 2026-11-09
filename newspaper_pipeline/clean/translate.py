"""Translation to English with pluggable backends.

Behaviour kept from ARGUS etl-pipeline-svc/etl/translate.py: skip when already English (no cost), retry throttling /
server faults with exponential backoff, one failed piece never discards the others, and every article is stamped
with a `translation_status` so failures are visible downstream. The original text is always kept.

Backends: AwsTranslateBackend (same service ARGUS uses), IndicTrans2Backend (local, best quality for Indic
languages, needs a GPU), or any object with `translate(text, src, tgt) -> str`.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Callable, Protocol

from ..config import Settings
from .lang import looks_english

log = logging.getLogger("np.translate")

RETRYABLE_ERROR_CODES = {"ThrottlingException", "TooManyRequestsException", "InternalServerException",
                         "ServiceUnavailableException", "RequestTimeout"}
_SENT_END = re.compile(r"(?<=[.!?।॥])\s+")


class TranslationError(Exception):
    pass


class TranslationBackend(Protocol):
    def translate(self, text: str, src: str, tgt: str) -> str: ...


def truncate_utf8(text: str, max_bytes: int) -> str:
    enc = text.encode("utf-8")
    return text if len(enc) <= max_bytes else enc[:max_bytes].decode("utf-8", errors="ignore")


def chunk_for_translation(text: str, max_chars: int) -> list[str]:
    """Split on paragraph then sentence boundaries into pieces <= max_chars (a single longer sentence is hard-cut
    on whitespace)."""
    pieces: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        cur = ""
        for sent in _SENT_END.split(para):
            while len(sent) > max_chars:
                cut = sent.rfind(" ", 0, max_chars)
                cut = cut if cut > max_chars // 2 else max_chars
                if cur:
                    pieces.append(cur); cur = ""
                pieces.append(sent[:cut].strip()); sent = sent[cut:].strip()
            if cur and len(cur) + 1 + len(sent) > max_chars:
                pieces.append(cur); cur = sent
            else:
                cur = f"{cur} {sent}".strip()
        if cur:
            pieces.append(cur)
        pieces.append("\n\n")        # paragraph marker, re-joined after translation
    if pieces and pieces[-1] == "\n\n":
        pieces.pop()
    return pieces


class AwsTranslateBackend:
    MAX_BYTES = 9000

    def __init__(self, client=None, region: str = "us-east-1", max_retries: int = 5, base_delay: float = 1.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client("translate", region_name=region, config=Config(retries={"max_attempts": 3, "mode": "standard"}))
        self.client, self.max_retries, self.base_delay, self._sleep = client, max_retries, base_delay, sleep

    def translate(self, text: str, src: str, tgt: str) -> str:
        last: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self.client.translate_text(Text=truncate_utf8(text, self.MAX_BYTES),
                                                  SourceLanguageCode=src or "auto", TargetLanguageCode=tgt)
                return resp.get("TranslatedText", "")
            except Exception as e:
                resp = getattr(e, "response", None)          # botocore ClientError carries the error code
                code = resp.get("Error", {}).get("Code", "") if isinstance(resp, dict) else ""
                if code and code not in RETRYABLE_ERROR_CODES:
                    raise TranslationError(f"{code}: {e}") from e
                last = e                                      # throttling / server fault / transport error: retry
                delay = self.base_delay * (2 ** attempt)
                log.warning("AWS Translate %s (attempt %d/%d), retrying in %.1fs", code or e, attempt + 1,
                            self.max_retries, delay)
                self._sleep(delay)
        raise TranslationError(f"exhausted {self.max_retries} retries: {last}")


class IndicTrans2Backend:
    """Local IndicTrans2 (ai4bharat). Requires `transformers`, `sentencepiece`, `IndicTransToolkit`, and a GPU for
    reasonable speed. Model ids default to the distilled 200M checkpoints."""
    FLORES = {"hi": "hin_Deva", "mr": "mar_Deva", "ne": "npi_Deva", "bn": "ben_Beng", "ta": "tam_Taml",
              "te": "tel_Telu", "kn": "kan_Knda", "ml": "mal_Mlym", "gu": "guj_Gujr", "pa": "pan_Guru",
              "or": "ory_Orya", "ur": "urd_Arab", "en": "eng_Latn"}

    def __init__(self, model_id: str = "ai4bharat/indictrans2-indic-en-dist-200M", device: str | None = None) -> None:
        import torch
        from IndicTransToolkit import IndicProcessor
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        self.torch = torch
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.tok = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
        self.model = AutoModelForSeq2SeqLM.from_pretrained(model_id, trust_remote_code=True).to(self.device)
        self.ip = IndicProcessor(inference=True)

    def translate(self, text: str, src: str, tgt: str) -> str:
        s, t = self.FLORES.get(src), self.FLORES.get(tgt)
        if not s or not t:
            raise TranslationError(f"unsupported pair {src}->{tgt}")
        batch = self.ip.preprocess_batch([text], src_lang=s, tgt_lang=t)
        enc = self.tok(batch, truncation=True, padding="longest", return_tensors="pt", max_length=256).to(self.device)
        with self.torch.no_grad():
            out = self.model.generate(**enc, num_beams=4, max_length=256)
        dec = self.tok.batch_decode(out, skip_special_tokens=True)
        return self.ip.postprocess_batch(dec, lang=t)[0]


def translate_text(backend: TranslationBackend, text: str, src: str, tgt: str, max_chars: int) -> str:
    out: list[str] = []
    for piece in chunk_for_translation(text, max_chars):
        out.append(piece if piece == "\n\n" else backend.translate(piece, src, tgt))
    # join sentences with spaces, paragraph markers as blank lines
    res = ""
    for part in out:
        res += part if part == "\n\n" else ((" " if res and not res.endswith("\n\n") else "") + part)
    return res.strip()


def translate_article(rec: dict, backend: TranslationBackend | None, settings: Settings | None = None) -> dict:
    """Adds translated_headline / translated_text / translation_status / translation_error. Returns a new dict."""
    s = settings or Settings()
    out = dict(rec)
    lang = rec.get("detected_language") or "und"
    head, text = rec.get("headline") or "", rec.get("text") or ""
    if not head and not text:
        out["translation_status"] = "skipped_no_text"
        return out
    if lang == s.target_language or (lang in ("und", "en") and looks_english(f"{head} {text}")):
        out["translation_status"] = "skipped_already_target"
        return out
    if backend is None:
        out["translation_status"] = "failed_no_backend"
        return out
    errors = []
    for src_field, dst_field in (("headline", "translated_headline"), ("text", "translated_text")):
        value = rec.get(src_field) or ""
        if not value:
            continue
        try:
            out[dst_field] = translate_text(backend, value, lang, s.target_language, s.translate_chunk_chars)
        except TranslationError as e:
            errors.append(f"{src_field}: {str(e)[:150]}")
    got = [f for f in ("translated_headline", "translated_text") if out.get(f)]
    out["translation_status"] = "translated" if len(got) == (2 if head and text else 1) else ("partial" if got else "failed")
    if errors:
        out["translation_error"] = "; ".join(errors)[:500]
    return out
