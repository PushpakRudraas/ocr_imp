import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from fixtures import make_papers  # noqa: E402


@pytest.fixture(scope="session")
def papers_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("inbox")
    make_papers.build_all(d)
    return d


@pytest.fixture(scope="session")
def manifest(papers_dir):
    from newspaper_pipeline.input.manifest import PaperSpec, Manifest
    specs = []
    for pid, name, hindi, _ in make_papers.PAPERS:
        f = papers_dir / f"{pid}.pdf"
        if f.exists():
            specs.append(PaperSpec(id=pid, name=name, language="hi" if hindi else "en", edition="Delhi",
                                   reputation="high", pdf=str(f)))
    return Manifest(run_date=dt.date(2026, 11, 2), papers=specs)


# ------------------------------------------------------------------------------------------ throwaway docker services
# Service tests never touch the dev stack: each fixture starts its own container (same image as docker-compose.yml) on a
# random localhost port and removes it afterwards. Skipped when Docker is unavailable.
import os  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except Exception:
        return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_container(request, name: str, image: str, container_port: int, env: dict[str, str]) -> int:
    if not _docker_ok():
        pytest.skip("docker daemon not available")
    port = _free_port()
    cname = f"np-test-{name}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    cmd = ["docker", "run", "-d", "--rm", "--name", cname, "-p", f"127.0.0.1:{port}:{container_port}"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    res = subprocess.run(cmd + [image], capture_output=True, text=True)
    if res.returncode != 0:
        pytest.skip(f"could not start {image}: {res.stderr.strip()[:200]}")
    request.addfinalizer(lambda: subprocess.run(["docker", "rm", "-f", cname], capture_output=True))
    return port


def _wait(fn, what: str, timeout: float = 90):
    end, last = time.time() + timeout, None
    while time.time() < end:
        try:
            return fn()
        except Exception as e:                     # noqa: BLE001 - service still starting
            last = e
            time.sleep(1)
    pytest.fail(f"{what} did not become ready in {timeout}s: {last}")


@pytest.fixture(scope="session")
def mongo_uri(request):
    port = _run_container(request, "mongo", "mongo:7", 27017, {})
    uri = f"mongodb://localhost:{port}"
    from pymongo import MongoClient
    _wait(lambda: MongoClient(uri, serverSelectionTimeoutMS=1000).admin.command("ping"), "mongo")
    return uri


@pytest.fixture(scope="session")
def pg_dsn(request):
    port = _run_container(request, "pg", "pgvector/pgvector:pg16", 5432,
                          {"POSTGRES_USER": "t", "POSTGRES_PASSWORD": "t", "POSTGRES_DB": "t"})
    dsn = f"postgresql://t:t@localhost:{port}/t"
    import psycopg
    _wait(lambda: psycopg.connect(dsn, connect_timeout=2).execute("select 1").fetchone(), "postgres")
    return dsn


@pytest.fixture(scope="session")
def neo4j_conf(request):
    port = _run_container(request, "neo4j", "neo4j:5-community", 7687,
                          {"NEO4J_AUTH": "neo4j/test-password-123", "NEO4J_server_memory_heap_max__size": "512m",
                           "NEO4J_server_memory_pagecache_size": "128m"})
    from neo4j import GraphDatabase
    uri = f"bolt://localhost:{port}"

    def ready():
        d = GraphDatabase.driver(uri, auth=("neo4j", "test-password-123"))
        try:
            d.verify_connectivity()
        finally:
            d.close()
    _wait(ready, "neo4j", timeout=120)
    return {"uri": uri, "user": "neo4j", "password": "test-password-123"}
