"""Optional LLM refinement on top of the rule-based draft (the 'GPU LLM' box in the architecture).

The rules give a complete, deterministic baseline; the LLM only *improves* a whitelisted set of fields and every
value it returns is type-checked before it is merged. Any failure leaves the draft untouched.
`client` is an anthropic.Anthropic (or anything with `messages.create`)."""
from __future__ import annotations

import json
import re

REFINABLE = ("event", "relations", "keyphrases", "one_line", "tone", "entity_stances")
PROMPT = """You are refining a news-analysis record for one newspaper article. Return ONLY a JSON object with any of
these keys you can improve (omit the rest):
  event: {{"type": "Domain.Type", "trigger": str, "actor": str|null, "institution": str|null}}
  relations: [[subject, relation_snake_case, object], ...]
  keyphrases: [str, ...]   (max 6)
  one_line: str            (one factual sentence, max 240 chars)
  tone: str                (e.g. "factual / hard news")
  entity_stances: [{{"entity": str, "stance": "positive"|"negative"|"neutral", "reason": str}}]
Do not invent facts that are not in the article.

HEADLINE: {headline}
TEXT: {text}

CURRENT DRAFT: {draft}"""


def _is_str(x):
    return isinstance(x, str) and x.strip() != ""


def _valid(key: str, v) -> bool:
    if key == "event":
        return isinstance(v, dict) and _is_str(v.get("type")) and re.fullmatch(r"[A-Za-z]+\.[A-Za-z]+", v["type"]) is not None
    if key == "relations":
        return isinstance(v, list) and all(isinstance(r, list) and len(r) == 3 and all(_is_str(x) for x in r) for r in v)
    if key == "keyphrases":
        return isinstance(v, list) and 0 < len(v) <= 6 and all(_is_str(x) for x in v)
    if key == "one_line":
        return _is_str(v) and len(v) <= 260
    if key == "tone":
        return _is_str(v) and len(v) <= 80
    if key == "entity_stances":
        return isinstance(v, list) and all(isinstance(e, dict) and _is_str(e.get("entity")) and
                                           e.get("stance") in ("positive", "negative", "neutral") for e in v)
    return False


class LlmRefiner:
    def __init__(self, client=None, model: str = "claude-haiku-5-5", max_chars: int = 6000) -> None:
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        self.client, self.model, self.max_chars = client, model, max_chars

    def refine(self, doc: dict) -> dict:
        nl = doc["newspaper"]
        draft = {"event": doc["event"], "relations": doc["relations"], "keyphrases": doc["keyphrases"]}
        prompt = PROMPT.format(headline=nl["translated_headline"] or nl["headline_clean"],
                               text=(nl["translated_text"] or nl["text"])[:self.max_chars], draft=json.dumps(draft))
        try:
            resp = self.client.messages.create(model=self.model, max_tokens=900,
                                               messages=[{"role": "user", "content": prompt}])
            raw = resp.content[0].text.strip()
            raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except Exception:
            doc["provenance"]["llm"] = "refine_failed; rule-based values kept"
            return doc
        applied = []
        for key in REFINABLE:
            if key in data and _valid(key, data[key]):
                v = data[key]
                if key == "event":
                    doc["event"] = {**doc["event"], **{k: v[k] for k in ("type", "trigger", "actor", "institution") if k in v}}
                elif key == "relations":
                    doc["relations"] = v
                elif key == "keyphrases":
                    doc["keyphrases"] = v
                elif key == "one_line":
                    doc["summary"]["one_line"] = v
                elif key == "tone":
                    doc["emotion_tone"]["tone"] = v
                elif key == "entity_stances":
                    doc["sentiment"]["entity_level"] = v
                applied.append(key)
        doc["provenance"]["llm"] = f"{self.model} refined: {', '.join(applied) or 'nothing'}"
        doc["provenance"]["models"] += f"; llm refinement ({self.model})"
        return doc
