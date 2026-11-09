import datetime as dt

import pytest

from newspaper_pipeline.input.fetch import FetchError, fetch_editions
from newspaper_pipeline.input.manifest import ManifestError, PaperSpec, load_manifest, parse_manifest
from newspaper_pipeline.input.splitter import split_edition


def test_parse_manifest_ok_and_defaults():
    m = parse_manifest({"run_date": "2026-11-02", "papers": [{"id": "a", "name": "A", "pdf": "x.pdf"}]})
    assert m.run_date == dt.date(2026, 11, 2)
    p = m.papers[0]
    assert (p.language, p.reputation, p.edition) == ("en", "not_rated", "main")


@pytest.mark.parametrize("data,msg", [
    ({}, "run_date"),
    ({"run_date": "nope", "papers": [{"id": "a", "name": "A", "pdf": "x"}]}, "bad run_date"),
    ({"run_date": "2026-11-02", "papers": []}, "at least one"),
    ({"run_date": "2026-11-02", "papers": [{"name": "A", "pdf": "x"}]}, "missing 'id'"),
    ({"run_date": "2026-11-02", "papers": [{"id": "a", "name": "A"}]}, "pdf or url"),
    ({"run_date": "2026-11-02", "papers": [{"id": "a", "name": "A", "pdf": "x", "reputation": "meh"}]}, "reputation"),
    ({"run_date": "2026-11-02", "papers": [{"id": "a", "name": "A", "pdf": "x"}, {"id": "a", "name": "B", "pdf": "y"}]}, "duplicate"),
])
def test_parse_manifest_errors(data, msg):
    with pytest.raises(ManifestError, match=msg):
        parse_manifest(data)


def test_load_manifest_resolves_relative_pdf(tmp_path):
    (tmp_path / "e.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "m.yaml").write_text("run_date: 2026-11-02\npapers:\n  - {id: a, name: A, pdf: e.pdf}\n")
    m = load_manifest(tmp_path / "m.yaml")
    assert m.papers[0].pdf == str(tmp_path / "e.pdf")


def test_page_numbers_selection():
    p = PaperSpec(id="a", name="A", pages="1-2,5,9-20")
    assert p.page_numbers(6) == [1, 2, 5]
    assert PaperSpec(id="a", name="A").page_numbers(3) == [1, 2, 3]


def test_fetch_copies_and_flags_duplicates(manifest, tmp_path):
    eds = fetch_editions(manifest, tmp_path)
    assert len(eds) == len(manifest.papers)
    assert all(e.path.exists() and e.duplicate_of is None for e in eds)
    # same bytes under another id -> duplicate_of
    other = PaperSpec(id="clone", name="Clone", pdf=manifest.papers[0].pdf)
    manifest2 = type(manifest)(run_date=manifest.run_date, papers=[manifest.papers[0], other])
    eds2 = fetch_editions(manifest2, tmp_path / "x")
    assert eds2[1].duplicate_of == manifest.papers[0].id


def test_fetch_rejects_non_pdf_and_missing(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"<html>")
    m = parse_manifest({"run_date": "2026-11-02", "papers": [{"id": "a", "name": "A", "pdf": str(bad)}]})
    with pytest.raises(FetchError, match="not a PDF"):
        fetch_editions(m, tmp_path / "o")
    m2 = parse_manifest({"run_date": "2026-11-02", "papers": [{"id": "a", "name": "A", "pdf": str(tmp_path / "none.pdf")}]})
    with pytest.raises(FetchError, match="not found"):
        fetch_editions(m2, tmp_path / "o2")


def test_fetch_url_uses_downloader(tmp_path, papers_dir):
    src = next(papers_dir.glob("*.pdf"))
    m = parse_manifest({"run_date": "2026-11-02", "papers": [{"id": "u", "name": "U", "url": "http://x/e.pdf"}]})
    calls = []

    def fake(url, dest):
        calls.append(url)
        dest.write_bytes(src.read_bytes())

    eds = fetch_editions(m, tmp_path, downloader=fake)
    assert calls == ["http://x/e.pdf"] and eds[0].path.exists()


def test_split_edition_renders_pages(manifest, tmp_path):
    eds = fetch_editions(manifest, tmp_path)
    recs = split_edition(eds[0], manifest.run_date, tmp_path, dpi=50)
    assert [r.page_no for r in recs] == [1, 2, 3]
    r = recs[0]
    assert r.has_text_layer and r.image_path and r.page_id == f"{r.paper_id}:2026-11-02:p1"
    from pathlib import Path
    assert Path(r.image_path).exists()
    assert r.to_dict()["paper_name"] == manifest.papers[0].name


def test_split_respects_page_limit(manifest, tmp_path):
    p = manifest.papers[0]
    limited = PaperSpec(**{**p.__dict__, "pages": "2-3"})
    m = type(manifest)(run_date=manifest.run_date, papers=[limited])
    recs = split_edition(fetch_editions(m, tmp_path)[0], m.run_date, tmp_path, dpi=40, render=False)
    assert [r.page_no for r in recs] == [2, 3] and recs[0].image_path is None
