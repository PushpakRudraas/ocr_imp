"""Manifest: the list of e-paper editions to process in one run.

    run_date: 2026-11-02
    papers:
      - id: the_hindu            # unique slug
        name: The Hindu
        language: en             # ISO code of the print language
        edition: Delhi
        reputation: high         # high | medium | low | not_rated  (feeds credibility)
        pdf: data/inbox/the_hindu.pdf     # local path ...
        url: https://...                  # ... or a URL (pdf wins if both given)
        pages: "1-12"                     # optional page limit
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPUTATIONS = {"high", "medium", "low", "not_rated"}


class ManifestError(ValueError):
    pass


@dataclass
class PaperSpec:
    id: str
    name: str
    language: str = "en"
    edition: str = "main"
    reputation: str = "not_rated"
    country: str = "IN"
    pdf: str | None = None
    url: str | None = None
    pages: str | None = None

    def page_numbers(self, total: int) -> list[int]:
        """1-based page numbers selected by `pages` ("1-12", "1,3,5-7"), clipped to the document."""
        if not self.pages:
            return list(range(1, total + 1))
        out: set[int] = set()
        for part in str(self.pages).split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-", 1)
                out.update(range(int(a), int(b) + 1))
            else:
                out.add(int(part))
        return sorted(p for p in out if 1 <= p <= total)


@dataclass
class Manifest:
    run_date: dt.date
    papers: list[PaperSpec] = field(default_factory=list)

    def paper(self, paper_id: str) -> PaperSpec:
        for p in self.papers:
            if p.id == paper_id:
                return p
        raise KeyError(paper_id)


def load_manifest(path: str | Path) -> Manifest:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return parse_manifest(data, base_dir=Path(path).parent)


def parse_manifest(data: dict, base_dir: Path | None = None) -> Manifest:
    if "run_date" not in data:
        raise ManifestError("manifest needs run_date")
    run_date = data["run_date"]
    if isinstance(run_date, str):
        try:
            run_date = dt.date.fromisoformat(run_date)
        except ValueError as e:
            raise ManifestError(f"bad run_date {run_date!r}") from e
    elif isinstance(run_date, dt.datetime):
        run_date = run_date.date()
    elif not isinstance(run_date, dt.date):
        raise ManifestError(f"bad run_date {run_date!r}")

    raw_papers = data.get("papers") or []
    if not raw_papers:
        raise ManifestError("manifest needs at least one paper")
    papers, seen = [], set()
    for raw in raw_papers:
        for req in ("id", "name"):
            if not raw.get(req):
                raise ManifestError(f"paper missing '{req}': {raw}")
        if raw["id"] in seen:
            raise ManifestError(f"duplicate paper id {raw['id']!r}")
        seen.add(raw["id"])
        if not (raw.get("pdf") or raw.get("url")):
            raise ManifestError(f"paper {raw['id']!r} needs pdf or url")
        rep = raw.get("reputation", "not_rated")
        if rep not in REPUTATIONS:
            raise ManifestError(f"paper {raw['id']!r}: reputation must be one of {sorted(REPUTATIONS)}")
        pdf = raw.get("pdf")
        if pdf and base_dir is not None and not Path(pdf).is_absolute() and not Path(pdf).exists():
            candidate = base_dir / pdf
            pdf = str(candidate) if candidate.exists() else pdf
        papers.append(PaperSpec(
            id=str(raw["id"]), name=str(raw["name"]), language=str(raw.get("language", "en")),
            edition=str(raw.get("edition", "main")), reputation=rep, country=str(raw.get("country", "IN")),
            pdf=pdf, url=raw.get("url"), pages=raw.get("pages"),
        ))
    return Manifest(run_date=run_date, papers=papers)
