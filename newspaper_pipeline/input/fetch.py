"""Fetch step: get each edition PDF into data/raw/{date}/{paper}/ and skip files already ingested.

A local `pdf:` is copied, a `url:` is downloaded. Files are de-duplicated by SHA-256 so re-running
a manifest (or two papers pointing at the same file) never double-processes an edition.
"""
from __future__ import annotations

import hashlib
import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .manifest import Manifest, PaperSpec


class FetchError(RuntimeError):
    pass


@dataclass
class FetchedEdition:
    paper: PaperSpec
    path: Path
    sha256: str
    duplicate_of: str | None = None     # paper id that already supplied identical bytes


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dest: Path, timeout: int = 60) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "newspaper-pipeline/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as out:
            shutil.copyfileobj(resp, out)
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise FetchError(f"download failed for {url}: {e}") from e


def _looks_like_pdf(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(5) == b"%PDF-"


def fetch_editions(manifest: Manifest, data_dir: str | Path, downloader=_download) -> list[FetchedEdition]:
    root = Path(data_dir) / "raw" / manifest.run_date.isoformat()
    seen: dict[str, str] = {}
    out: list[FetchedEdition] = []
    for paper in manifest.papers:
        dest_dir = root / paper.id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "edition.pdf"
        if paper.pdf:
            src = Path(paper.pdf)
            if not src.exists():
                raise FetchError(f"{paper.id}: pdf not found: {src}")
            shutil.copyfile(src, dest)
        else:
            downloader(paper.url, dest)
        if not _looks_like_pdf(dest):
            dest.unlink(missing_ok=True)
            raise FetchError(f"{paper.id}: not a PDF")
        digest = sha256_file(dest)
        out.append(FetchedEdition(paper, dest, digest, duplicate_of=seen.get(digest)))
        seen.setdefault(digest, paper.id)
    return out
