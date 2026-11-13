"""Rule-based ad / non-news detector for raw OCR article records.

Runs on every raw record BEFORE cleaning and translation. It never deletes anything: it labels each record
`news` / `ad` / `uncertain` with a reason, the label is stored on the raw event, and only non-ads continue to
`clean_events`. A person can override a label in the UI. (A trained text/vision classifier can replace `classify`
later; the contract - rec in, (label, reason, score) out - stays the same.)
"""
from __future__ import annotations

import re

from .quality import garbage_ratio

NEWS, AD, UNCERTAIN = "news", "ad", "uncertain"

_PHONE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)|(?<!\d)1800[\s-]?\d{3}[\s-]?\d{3,4}(?!\d)|\b0\d{2,4}[\s-]\d{6,8}\b")
_URL = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+\.(?:com|in|co\.in|org|net)\b", re.I)
_PRICE = re.compile(r"(?:₹|rs\.?|inr)\s?\d[\d,]*|\b\d[\d,]*\s?(?:/-|lakh|crore)\b|\b\d{1,3}\s?%\s?off\b", re.I)
_AD_WORDS = re.compile(
    r"\b(?:call (?:now|us|today)|contact us|visit us|book now|buy now|enquir(?:y|ies)|toll[- ]free|whatsapp|"
    r"t\s?&\s?c apply|terms (?:and|&) conditions|conditions apply|limited period|offer(?:s)? (?:valid|ends)|"
    r"discount|sale|emi|booking(?:s)? open|admissions? open|apply now|register now|showroom|franchise|"
    r"to[- ]let|for sale|required|wanted|vacancy|vacancies|walk[- ]in|tender notice|public notice|"
    r"lost (?:&|and) found|matrimon|bride|groom|obituary|condolence|shraddhanjali|happy (?:birthday|anniversary)|best wishes|"
    r"authorised dealer|distributor|pvt\.? ltd|enterprises?|traders|associates)\b", re.I)
_REPEAT = re.compile(r"(.{2,4}?)\1{2,}")                       # "ATATA", "ababab": logo / texture read as letters
_PROSE_STOP = re.compile(r"\b(?:the|and|of|to|in|that|said|was|for|with|on|has|have|by|from)\b", re.I)


def _words(s: str) -> list[str]:
    return re.findall(r"[^\W\d_]+", s, re.U)


def _junk_headline(h: str) -> bool:
    h = (h or "").strip()
    if not h:
        return False
    toks = h.split()
    if _REPEAT.search(re.sub(r"[^A-Za-z]", "", h)) and len(toks) <= 4:
        return True
    if len(toks) <= 2 and any(len(t) >= 12 and re.search(r"[a-z][A-Z]", t) for t in toks):     # "ATATAEnterprise"
        return True
    letters = re.sub(r"[^A-Za-z]", "", h)
    if len(letters) >= 5 and not re.search(r"[aeiouAEIOU]", letters):
        return True
    return False


def classify(rec: dict) -> tuple[str, str, float]:
    """(label, reason, score). score = how ad-like the record is (0 news ... 5+ ad)."""
    head = (rec.get("headline") or "").strip()
    text = (rec.get("text") or "").strip()
    blob = f"{head}\n{text}"
    n_chars = len(text)
    reasons: list[str] = []
    score = 0.0

    yolo_ad, yolo_art = rec.get("yolo_ad_frac") or 0.0, rec.get("yolo_article_frac") or 0.0
    if yolo_ad >= 0.5:                                          # the page detector says this block sits inside an ad
        score += 5.0; reasons.append("in_ad_region")
    elif yolo_ad >= 0.2:
        score += 1.5; reasons.append("partly_in_ad_region")
    elif yolo_art >= 0.6:                                       # inside a detected article: trust the text rules less
        score -= 1.5

    if n_chars < 25:
        score += 2.5; reasons.append("no_body_text")
    elif n_chars < 80:
        score += 1.0; reasons.append("very_short_text")
    if _junk_headline(head):
        score += 2.5; reasons.append("junk_headline")
    elif head and len(_words(head)) <= 2 and n_chars < 200:
        score += 0.8; reasons.append("tiny_headline")
    if not head:
        score += 0.5; reasons.append("no_headline")

    phones = len(_PHONE.findall(blob))
    if phones:
        score += min(3.0, 1.5 * phones); reasons.append("phone_numbers")
    if _URL.search(blob):
        score += 1.5; reasons.append("web_address")
    prices = len(_PRICE.findall(blob))
    if prices:
        score += min(2.0, 0.7 * prices); reasons.append("prices")
    words = {m.group(0).lower() for m in _AD_WORDS.finditer(blob)}
    if words:
        score += min(3.0, 1.0 * len(words)); reasons.append("ad_wording")
    if n_chars and garbage_ratio(text) > 0.15:
        score += 1.5; reasons.append("garbled_text")

    # real prose pulls the score down: long text with ordinary function words and sentences
    if n_chars >= 400 and yolo_ad < 0.5:                         # but never overrule the page detector
        stop_density = len(_PROSE_STOP.findall(text)) / max(1, len(text.split()))
        sentences = len(re.findall(r"[.!?]\s", text))
        if stop_density >= 0.12 and sentences >= 3:
            score -= 2.5; reasons.append("reads_like_prose")
        elif stop_density >= 0.08:
            score -= 1.0

    if yolo_ad >= 0.8:
        score = max(score, 5.0)
    score = round(max(0.0, score), 2)
    top = [r for r in reasons if r != "reads_like_prose"][:3]
    reason = ",".join(top) or "ok"
    if score >= 3.0:
        return AD, reason, score
    if score >= 1.5:
        return UNCERTAIN, reason, score
    return NEWS, "ok", score


def label_records(recs: list[dict]) -> list[dict]:
    """Attach ad_label / ad_reason / ad_score to each record in place."""
    for r in recs:
        r["ad_label"], r["ad_reason"], r["ad_score"] = classify(r)
    return recs
