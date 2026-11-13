"""Cleaning rules for the relationship graph, tested on real examples taken from stored articles."""
from newspaper_pipeline.graph.clean import (clean_author, clean_authors, clean_claims, clean_event, clean_keywords, clean_section,
                                            norm, short_hash, slug)

# bylines that OCR actually produced in the stored articles (none of these is an author)
JUNK_BYLINES = ["&CONSUMER LOANS", "(319), and Sonepat (304)allin", "(CPCB) shows.", "(FORMERLYNETAJISUBHASINSTITUTEOFTECHNOLOGY)",
                "(their ideological supporters)", "+91 91488 62559", "1987", "2006", "23 Champion of the pack (3)", "298 234",
                "3925/2026", "he added.", "country", "letterschd@hindustan times. com", "", None, "x" * 80]


def test_every_junk_byline_is_rejected():
    assert [j for j in JUNK_BYLINES if clean_authors(j)] == []


def test_real_bylines_become_authors_and_desks():
    assert clean_authors("By Neeraj Mohan") == [{"name": "Neeraj Mohan", "kind": "person"}]
    assert clean_authors("Neeraj Mohan and Bhavey Nagpal") == [{"name": "Neeraj Mohan", "kind": "person"}, {"name": "Bhavey Nagpal", "kind": "person"}]
    assert clean_authors("By A. K. Singh") == [{"name": "A. K. Singh", "kind": "person"}]
    assert clean_authors("SANJEEV KUMAR") == [{"name": "Sanjeev Kumar", "kind": "person"}]
    assert clean_authors("STAFF REPORTER") == [{"name": "Staff Reporter", "kind": "desk"}]
    assert clean_authors("HT Correspondent") == [{"name": "HT Correspondent", "kind": "desk"}]
    assert clean_authors("Press Trust of India") == [{"name": "PTI", "kind": "desk"}] == clean_authors("PTI")
    assert clean_authors("Karam Prakash & Sharp drop") == [{"name": "Karam Prakash", "kind": "person"}]     # the bad part is dropped, not the whole line
    assert clean_author("Singh") is None and clean_author("A. K.") is None                                    # one word / initials only


def test_keywords_drop_generic_numeric_duplicate_and_entity_phrases_and_are_capped():
    out = clean_keywords(["Punjab Police arrest", "Gurwinder Afridi", "Ethiopia", "said", "2026", "Delhi", "punjab police ARREST", "new year"],
                         ["Gurwinder Afridi", "Ethiopia"])
    assert out == [{"id": "punjab police arrest", "text": "Punjab Police arrest"}]
    many = clean_keywords([f"distinct phrase {i}" for i in range(20)])
    assert len(many) == 6 and many[0]["id"] == "distinct phrase 0"


def _doc(event, geo="Punjab"):
    return {"event": event, "geo": {"primary_location": geo}}


def test_only_specific_events_with_an_actor_get_a_node_and_the_same_event_gets_the_same_id():
    base = {"type": "Crime.Arrest", "trigger": "arrested", "actor": "Gurwinder Afridi", "institution": "Punjab Police", "date": "2026-11-08"}
    a, b = clean_event(_doc(base)), clean_event(_doc({**base, "actor": "gurwinder  afridi", "trigger": "deported"}))
    assert a and a["id"] == b["id"] and a["type"] == "Crime.Arrest" and a["location"] == "Punjab"      # another paper, same event
    assert clean_event(_doc({**base, "date": "2026-11-09"}))["id"] != a["id"]                          # another day is another event
    assert clean_event(_doc({**base, "type": "General.Report"})) is None
    assert clean_event(_doc({**base, "actor": None, "institution": None})) is None
    assert clean_event({"event": {}, "geo": {}}) is None


def test_claims_keep_only_checkworthy_trimmed_unique_ones_up_to_the_limit():
    long = "The government announced a new scheme worth 500 crore rupees for farmers across the state. " * 6
    claims = [{"claim": long, "checkworthy": True, "verification_status": "unverified", "corroborated_by": ["The Hindu", None]},
              {"claim": "Short.", "checkworthy": True},                                                  # too short to mean anything
              {"claim": "A long enough sentence that is not check-worthy at all, really.", "checkworthy": False},
              {"claim": long.upper(), "checkworthy": True, "verification_status": "unverified"},          # same sentence again
              {"claim": "Eleven people were arrested on Monday in the city centre, police said.", "checkworthy": True},
              {"claim": "Prices rose by four per cent in October compared with last year.", "checkworthy": True},
              {"claim": "A fourth check-worthy claim that goes over the limit of three per article.", "checkworthy": True}]
    out = clean_claims(claims)
    assert len(out) == 3 and len(out[0]["text"]) == 300 and out[0]["supported_by"] == ["The Hindu"]
    assert len({c["id"] for c in out}) == 3 and all(c["checkworthy"] for c in out)
    assert clean_claims(None) == [] and clean_claims([]) == []


def test_section_and_small_helpers():
    assert clean_section("  Front   Page ") == "Front Page" and clean_section("Nation") == "Nation"
    assert clean_section(None) is None and clean_section("") is None and clean_section("p. 3 | Business") is None and clean_section("A") is None
    assert slug("The Times of India") == "the_times_of_india" and norm("Hello,  WORLD!") == "hello world"
    assert short_hash("a") == short_hash("a") != short_hash("b") and len(short_hash("a")) == 16


# ------------------------------------------------------------------------------------------------ rebuild tool (no database needed)
def test_rebuild_reads_finished_runs_only_and_writes_each_runs_documents(tmp_path):
    import json
    from newspaper_pipeline.graph.rebuild import BATCH, rebuild

    def make_run(name, status, enrichment, n):
        d = tmp_path / name
        (d / "out" / "articles" / "paper_a").mkdir(parents=True)
        (d / "run.json").write_text(json.dumps({"status": status, "enrichment": {"state": enrichment}}))
        for i in range(n):
            (d / "out" / "articles" / "paper_a" / f"{i:04d}.json").write_text(json.dumps({"event_id": f"{name}-{i}"}))

    make_run("done_run", "done", "done", 3)
    make_run("running", "running", "off", 2)
    make_run("not_enriched", "done", "off", 2)
    (tmp_path / "stray_folder").mkdir()
    (tmp_path / "broken").mkdir(); (tmp_path / "broken" / "run.json").write_text("{not json")

    class Recorder:
        def __init__(self): self.batches = []
        def write(self, docs):
            self.batches.append([d["event_id"] for d in docs])
            return {"articles": len(docs)}

    g = Recorder()
    assert rebuild(g, tmp_path) == {"done_run": {"articles": 3}} and g.batches == [["done_run-0", "done_run-1", "done_run-2"]]
    assert rebuild(None, tmp_path, dry_run=True) == {"done_run": {"articles": 3}}
    assert rebuild(Recorder(), tmp_path, only="running") == {}                              # unfinished runs are never written

    big = Recorder()
    make_run("big_run", "done", "done", BATCH + 5)
    assert rebuild(big, tmp_path, only="big_run")["big_run"]["articles"] == BATCH + 5
    assert [len(b) for b in big.batches] == [BATCH, 5, BATCH + 5]                           # batches, then one pass over the whole run


# ------------------------------------------------------------------------------------------------ which nodes are drawn when there are too many
def test_select_nodes_keeps_every_requested_type_represented():
    from collections import Counter
    from newspaper_pipeline.graph.neo4j_graph import select_nodes

    def make(t, n, prefix=None):
        return [{"id": f"{t}:{i:05d}", "type": t, "label": f"{t} {i}", "props": {}} for i in range(n)]

    # the real worst case seen on the live graph: all runs, every node type, 2000 nodes
    sizes = {"Article": 1386, "Paper": 5, "Edition": 5, "Day": 4, "Topic": 11, "Section": 2, "Story": 1013, "Page": 109, "Event": 243,
             "Author": 94, "Entity": 2697, "Keyword": 205, "Claim": 1182}
    nodes = [n for t, c in sizes.items() for n in make(t, c)]
    degree = {n["id"]: (1000 - int(n["id"].split(":")[1]) % 1000) for n in nodes}               # distinct, deterministic degrees
    keep = select_nodes(nodes, degree, 2000)
    got = Counter(n["type"] for n in keep)
    assert len(keep) == 2000 and len({n["id"] for n in keep}) == 2000
    assert all(got[t] == sizes[t] for t in ("Paper", "Edition", "Day", "Topic", "Section"))       # small structural types are kept whole
    assert all(got[t] > 0 for t in sizes), got                                                    # no requested type is crowded out
    assert got["Article"] == 1386                                                               # every article fits, so every article is kept
    assert got["Entity"] > 0 and got["Claim"] > 0 and got["Story"] > 0

    # the best connected are chosen inside a type
    ents = make("Entity", 50)
    d = {n["id"]: i for i, n in enumerate(ents)}
    top = select_nodes(ents, d, 5)
    assert [n["id"] for n in top] == [f"Entity:{i:05d}" for i in (49, 48, 47, 46, 45)]

    # a tiny cap never lets the small types swallow the articles
    tiny = select_nodes(make("Article", 40) + make("Paper", 5) + make("Day", 3) + make("Topic", 11), {}, 20)
    assert len(tiny) == 20 and sum(n["type"] == "Article" for n in tiny) >= 12

    # fewer nodes than the cap: everything is kept
    assert len(select_nodes(make("Article", 7) + make("Entity", 3), {}, 100)) == 10
