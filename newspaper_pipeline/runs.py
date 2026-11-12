"""Per-run state file (`<workdir>/runs/<id>/run.json`), shared by the API process and the enrichment worker.

Writes are atomic (tmp file + rename) and `patch` is read-modify-write under a process lock, so a reader never sees a
half-written file. The two processes touch a run one after the other (extraction finishes before the job is published),
so there is no concurrent writer to the same run."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

RUN_ID = re.compile(r"^[0-9a-f]{12}$")
_LOCK = threading.Lock()


def _retry(fn, tries: int = 20):
    """Windows refuses to replace / read a file another thread has open for a moment (a polling client reading run.json
    while a stage finishes); on Linux this never loops. Retry briefly instead of failing the run."""
    for i in range(tries):
        try:
            return fn()
        except (PermissionError, json.JSONDecodeError):
            if i == tries - 1:
                raise
            time.sleep(0.02)


class RunNotFound(KeyError):
    pass


class RunStore:
    def __init__(self, workdir: str | Path) -> None:
        self.root = Path(workdir) / "runs"
        self.root.mkdir(parents=True, exist_ok=True)

    def dir(self, run_id: str) -> Path:
        if not RUN_ID.match(run_id):
            raise RunNotFound(run_id)
        return self.root / run_id

    def get(self, run_id: str) -> dict:
        f = self.dir(run_id) / "run.json"
        if not f.exists():
            raise RunNotFound(run_id)
        return _retry(lambda: json.loads(f.read_text(encoding="utf-8")))

    def write(self, run: dict) -> None:
        d = self.root / run["id"]
        with _LOCK:
            tmp = d / "run.json.tmp"
            tmp.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
            _retry(lambda: tmp.replace(d / "run.json"))

    def patch(self, run_id: str, **fields) -> dict:
        """Merge top-level fields into the stored run and return it."""
        run = {**self.get(run_id), **fields}
        self.write(run)
        return run

    def list(self) -> list[dict]:
        runs = []
        for f in self.root.glob("*/run.json"):
            try:
                runs.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                continue
        return sorted(runs, key=lambda r: r["created_at"], reverse=True)
