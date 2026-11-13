"""Rebuild the relationship graph of finished runs from the enriched articles stored in each run's folder.

Use it for runs that were processed before the graph had its newer node types and similarity links. It only READS the stored
articles (`<workdir>/<run>/out/articles/*/*.json`); it runs no OCR, matching or enrichment and touches no other database.
Writing is idempotent, so running it twice changes nothing the second time.

    python -m newspaper_pipeline.graph.rebuild                 # every finished run under the work directory
    python -m newspaper_pipeline.graph.rebuild --run 15e830a36fdc
    python -m newspaper_pipeline.graph.rebuild --dry-run        # count what would be written
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Iterator

BATCH = 200


def run_docs(run_dir: Path) -> list[dict]:
    """The enriched article documents of one run, in a stable order."""
    docs = []
    for f in sorted((run_dir / "out" / "articles").glob("*/*.json")):
        try:
            docs.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            print(f"skipping unreadable file {f}", file=sys.stderr)
    return docs


def finished_runs(workdir: Path, only: str | None = None) -> Iterator[Path]:
    for d in sorted(workdir.iterdir()) if workdir.is_dir() else []:
        if only and d.name != only:
            continue
        f = d / "run.json"
        if not f.exists() or not (d / "out" / "articles").is_dir():
            continue
        try:
            run = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if run.get("status") == "done" and (run.get("enrichment") or {}).get("state") == "done":
            yield d


def rebuild(graph, workdir: Path, only: str | None = None, dry_run: bool = False) -> dict:
    """Write the graph of each finished run; returns {run id: summary} (counts only when `dry_run`)."""
    out: dict[str, dict] = {}
    for d in finished_runs(workdir, only):
        docs = run_docs(d)
        if not docs:
            continue
        if dry_run:
            out[d.name] = {"articles": len(docs)}
            continue
        total: dict[str, int] = {}
        for i in range(0, len(docs), BATCH):                       # similarity links need their partner article, so one run is
            summary = graph.write(docs[i:i + BATCH])               # written in batches of whole articles, links are added per batch
            for k, v in summary.items():
                total[k] = total.get(k, 0) + v
        # a pair whose partner was in a later batch was skipped above; one more pass over the whole run adds those links
        if len(docs) > BATCH:
            graph.write(docs)
        out[d.name] = total
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workdir", default=os.environ.get("NP_WORKDIR", "data/web"), help="folder holding one sub-folder per run")
    p.add_argument("--run", help="rebuild only this run id")
    p.add_argument("--dry-run", action="store_true", help="count the articles that would be written and exit")
    a = p.parse_args(argv)
    workdir = Path(a.workdir)
    graph = None
    if not a.dry_run:
        from ..config import Settings
        from .neo4j_graph import Neo4jGraph
        s = Settings()
        graph = Neo4jGraph(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    try:
        result = rebuild(graph, workdir, a.run, a.dry_run)
    finally:
        if graph is not None:
            graph.close()
    if not result:
        print("no finished runs with stored articles were found in", workdir)
        return 1
    for rid, summary in result.items():
        print(rid, json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
