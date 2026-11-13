"""Relationship graph in Neo4j.

    (:Paper)<-[:PUBLISHED_IN {page}]-(:Article)-[:PART_OF]->(:Story)
    (:Article)-[:MENTIONS]->(:Entity:Person|Organization|PoliticalParty|Location)
    (:Entity)-[:RESIGNED_FROM|ASSOCIATED_WITH|LOCATED_IN|... {date, event_ids}]->(:Entity)      # from each article's knowledge_graph

    (:Edition)-[:OF_PAPER]->(:Paper)          (:Page)-[:IN_EDITION]->(:Edition)     (:Page)-[:ON_DAY]->(:Day)
    (:Article)-[:ON_PAGE]->(:Page)            (:Article)-[:IN_SECTION]->(:Section)  (:Article)-[:WRITTEN_BY]->(:Author)
    (:Article)-[:HAS_TOPIC {confidence}]->(:Topic)    (:Article)-[:REPORTS]->(:Event)
    (:Article)-[:MAKES_CLAIM]->(:Claim)-[:SUPPORTED_BY]->(:Paper)     (:Article)-[:HAS_KEYWORD]->(:Keyword)
    (:Article)-[:SIMILAR_TO {score, ...}]->(:Article)         merged pairs (the matching engine's score, 0.75 or more)
    (:Article)-[:POSSIBLE_MATCH {score, ...}]->(:Article)     borderline pairs (0.50 to 0.75), kept apart for review

Entity nodes are keyed by slug, so "Kunwar Vijay Pratap Singh" in four newspapers is ONE node with four articles mentioning
it, and a relation reported by several papers is one edge whose `event_ids` lists them all. Writes are MERGEs, so re-running
a stage never duplicates anything; articles whose story changed are re-pointed and orphaned nodes removed. The similarity links
copy the scores the matching engine already saved on each article: nothing is recomputed here.

`build_graph_rows` is pure (no driver) so the mapping is unit-testable without a database."""
from __future__ import annotations

import re
from collections import defaultdict

from ..enrich.lexicons import KNOWN_ORGS
from .clean import clean_authors, clean_claims, clean_event, clean_keywords, clean_section, collapse, slug  # noqa: F401  (slug re-exported)

ENTITY_LABELS = {"Person", "Organization", "PoliticalParty", "Location"}     # the only labels ever put into Cypher text
_REL_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,40}$")

# "AAP" and "Aam Aadmi Party" are one node: acronym slugs map to the slug of their expansion
_CANON = {slug(ac): slug(full) for ac, full in KNOWN_ORGS.items() if slug(ac) != slug(full)}
_FULL_NAME = {slug(full): full for full in KNOWN_ORGS.values()}

# node type -> (label, key property, properties returned to the UI). Only these are ever put into query text.
NODE_SPECS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "Paper": ("Paper", "id", ("name", "reputation")),
    "Edition": ("Edition", "id", ("name", "paper_id")),
    "Day": ("Day", "date", ("date",)),
    "Page": ("Page", "id", ("page_no", "date", "paper_id")),
    "Section": ("Section", "id", ("name", "paper_id")),
    "Author": ("Author", "id", ("name", "kind", "paper_id")),
    "Article": ("Article", "event_id", ("headline", "page_no", "date", "published", "ocr_confidence", "language", "sentiment", "event_type")),
    "Story": ("Story", "story_id", ("key", "event_type", "location", "topic", "impact", "n_papers", "date")),
    "Entity": ("Entity", "id", ("name", "label")),
    "Topic": ("Topic", "id", ("name",)),
    "Event": ("Event", "id", ("type", "trigger", "actor", "institution", "date", "location")),
    "Claim": ("Claim", "id", ("text", "status", "checkworthy")),
    "Keyword": ("Keyword", "id", ("text",)),
}
# relationships the UI may ask for: (relationship, from type, to type)
LINK_SPECS = (
    ("PUBLISHED_IN", "Article", "Paper"), ("PART_OF", "Article", "Story"), ("MENTIONS", "Article", "Entity"),
    ("ON_PAGE", "Article", "Page"), ("IN_SECTION", "Article", "Section"), ("WRITTEN_BY", "Article", "Author"),
    ("HAS_TOPIC", "Article", "Topic"), ("REPORTS", "Article", "Event"), ("MAKES_CLAIM", "Article", "Claim"),
    ("HAS_KEYWORD", "Article", "Keyword"), ("IN_EDITION", "Page", "Edition"), ("ON_DAY", "Page", "Day"),
    ("OF_PAPER", "Edition", "Paper"), ("SUPPORTED_BY", "Claim", "Paper"),
)
SIMILARITY_RELS = ("SIMILAR_TO", "POSSIBLE_MATCH")
DERIVED_ARTICLE_RELS = ("PART_OF", "MENTIONS", "ON_PAGE", "IN_SECTION", "WRITTEN_BY", "HAS_TOPIC", "REPORTS", "MAKES_CLAIM",
                        "HAS_KEYWORD", *SIMILARITY_RELS)
# order in which node types are considered when the cap is reached (see `select_nodes`)
TYPE_PRIORITY = ("Article", "Paper", "Story", "Edition", "Day", "Topic", "Section", "Page", "Event", "Author", "Entity", "Keyword", "Claim")


def canon(entity_id: str) -> str:
    return _CANON.get(entity_id, entity_id)


def node_id(node_type: str, key) -> str:
    return f"{node_type}:{key}"


def parse_node_id(value: str) -> tuple[str, str] | None:
    """"Article:abc" -> ("Article", "abc"); None unless the type is a known one."""
    t, _, k = (value or "").partition(":")
    return (t, k) if t in NODE_SPECS and k else None


def _day(value) -> str | None:
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", value or "")
    return m.group(1) if m else None


def build_graph_rows(docs: list[dict]) -> dict:
    papers, stories, articles = {}, {}, []
    entities: dict[str, dict] = {}                 # slug -> {id, name, label}
    mentions: list[dict] = []
    rels: dict[str, dict[tuple, dict]] = defaultdict(dict)
    editions, days, pages = {}, set(), {}
    sections, authors, topics, events, claims, keywords = {}, {}, {}, {}, {}, {}
    links: dict[str, list[dict]] = defaultdict(list)       # relationship -> rows {event_id, to, ...}
    similarity: list[dict] = []

    for d in docs:
        nl, ev = d["newspaper"], d["event"]
        sid = d["story"]["story_id"] or d["event_id"]
        eid = d["event_id"]
        pid = nl["paper_id"]
        papers[pid] = {"id": pid, "name": nl["paper"], "reputation": d["credibility"]["source_reputation"]}
        story = stories.setdefault(sid, {"story_id": sid, "key": d["dedup_clustering"].get("suggested_story_key"),
                                         "event_type": ev["type"], "location": d["geo"]["primary_location"],
                                         "topic": d["classification"]["topic"], "impact": d["impact_score"]["value"],
                                         "summary": d["summary"]["multi_doc_summary"], "n_papers": d["story"]["n_papers"],
                                         "date": ev["date"]})
        story["impact"] = max(story["impact"], d["impact_score"]["value"])
        published = _day((d.get("ingestion_metadata") or {}).get("published_at")) or _day(ev.get("date"))
        articles.append({"event_id": eid, "paper_id": pid, "story_id": sid, "headline": nl["headline"],
                         "page_no": nl["page_no"], "date": ev["date"], "published": published, "ocr_confidence": nl["ocr_confidence"],
                         "language": nl["language"], "sentiment": d["sentiment"]["document"]["label"],
                         "event_type": ev["type"]})

        # ---- structure: edition, day, page(s), section, authors
        edition = collapse(nl.get("edition")) or "main"
        edition_id = f"{pid}|{edition}"
        editions[edition_id] = {"id": edition_id, "name": edition, "paper_id": pid}
        if published:
            days.add(published)
            for pg in (nl.get("pages") or [nl["page_no"]]):
                page_id = f"{pid}|{published}|{pg}"
                pages[page_id] = {"id": page_id, "page_no": pg, "date": published, "paper_id": pid, "edition_id": edition_id}
                links["ON_PAGE"].append({"event_id": eid, "to": page_id})
        section = clean_section(nl.get("section"))
        if section:
            sec_id = f"{pid}|{slug(section)}"
            sections[sec_id] = {"id": sec_id, "name": section, "paper_id": pid}
            links["IN_SECTION"].append({"event_id": eid, "to": sec_id})
        for a in clean_authors(nl.get("byline")):
            aid = f"{pid}|{slug(a['name'])}"
            authors[aid] = {"id": aid, "name": a["name"], "kind": a["kind"], "paper_id": pid}
            links["WRITTEN_BY"].append({"event_id": eid, "to": aid})

        # ---- content: topic, event, claims, keywords
        topic = (d["classification"].get("topic") or "").strip()
        if topic:
            topics[topic] = {"id": topic, "name": topic}
            links["HAS_TOPIC"].append({"event_id": eid, "to": topic, "confidence": d["classification"].get("topic_confidence"),
                                       "subtopic": d["classification"].get("subtopic")})
        event_node = clean_event(d)
        if event_node:
            events[event_node["id"]] = event_node
            links["REPORTS"].append({"event_id": eid, "to": event_node["id"]})
        for c in clean_claims(d.get("claims")):
            node = claims.setdefault(c["id"], {"id": c["id"], "text": c["text"], "status": c["status"], "checkworthy": True,
                                               "supported_by": []})
            for name in c["supported_by"]:
                if name not in node["supported_by"]:
                    node["supported_by"].append(name)
            links["MAKES_CLAIM"].append({"event_id": eid, "to": c["id"]})
        ent_names = [e["text"] for e in d.get("entities", []) if e.get("type") != "CARDINAL_GROUP"]
        for k in clean_keywords(d.get("keyphrases"), ent_names):
            keywords[k["id"]] = k
            links["HAS_KEYWORD"].append({"event_id": eid, "to": k["id"]})

        # ---- similarity: copy what the matching engine saved on the article
        dd = d["dedup_clustering"]
        for rel, key in (("SIMILAR_TO", "matches"), ("POSSIBLE_MATCH", "review_candidates")):
            for m in dd.get(key) or []:
                if m.get("event_id") and m["event_id"] != eid and m.get("score") is not None:
                    c = m.get("components") or {}
                    similarity.append({"rel": rel, "a": eid, "b": m["event_id"], "score": m["score"], "semantic": c.get("semantic"),
                                       "entity": c.get("entity"), "keyword": c.get("keyword"), "date": c.get("date"),
                                       "location": c.get("location"), "cosine": m.get("cosine"), "veto": m.get("veto"),
                                       "reasons": [str(r) for r in (m.get("reasons") or [])][:8]})

        # ---- entities and their relations (unchanged)
        names = {slug(e["text"]): e["text"] for e in d["entities"] if e["type"] != "CARDINAL_GROUP"}
        doc_nodes = set()
        for n in d["knowledge_graph"]["nodes"]:
            if n["label"] not in ENTITY_LABELS:
                continue
            cid = canon(n["id"])
            doc_nodes.add(cid)
            entities.setdefault(cid, {"id": cid, "name": _FULL_NAME.get(cid) or names.get(n["id"], n["id"].replace("_", " ").title()),
                                      "label": n["label"]})
            if {"event_id": d["event_id"], "entity_id": cid} not in mentions:
                mentions.append({"event_id": d["event_id"], "entity_id": cid})
        for e in d["knowledge_graph"]["edges"]:
            if not _REL_RE.match(e["rel"]):
                continue                            # never interpolate an unvetted relationship type into Cypher
            a, b = canon(e["from"]), canon(e["to"])
            if a not in doc_nodes or b not in doc_nodes:
                continue                            # an endpoint that is not an entity node
            row = rels[e["rel"]].setdefault((a, b), {"from": a, "to": b, "date": e.get("date"), "event_ids": []})
            if d["event_id"] not in row["event_ids"]:
                row["event_ids"].append(d["event_id"])
    by_label: dict[str, list[dict]] = defaultdict(list)
    for e in entities.values():
        by_label[e["label"]].append(e)
    paper_by_name = {p["name"]: p["id"] for p in papers.values()}
    supported = [{"claim": c["id"], "paper_id": paper_by_name[n]} for c in claims.values() for n in c["supported_by"] if n in paper_by_name]
    return {"papers": list(papers.values()), "stories": list(stories.values()), "articles": articles,
            "entities": dict(by_label), "mentions": mentions, "relations": {k: list(v.values()) for k, v in rels.items()},
            "editions": list(editions.values()), "days": sorted(days), "pages": list(pages.values()),
            "sections": list(sections.values()), "authors": list(authors.values()), "topics": list(topics.values()),
            "events": list(events.values()),
            "claims": [{k: v for k, v in c.items() if k != "supported_by"} for c in claims.values()],
            "keywords": list(keywords.values()), "links": {k: v for k, v in links.items()}, "supported_by": supported,
            "similarity": similarity}


def select_nodes(nodes: list[dict], degree: dict[str, int], max_nodes: int) -> list[dict]:
    """Which nodes to draw when there are more than `max_nodes`. Every requested type must stay represented:
      1. the small structural types (newspapers, editions, days, topics, sections - a handful of nodes each) are kept whole,
         as long as together they take no more than a quarter of the room;
      2. then articles, the best connected first;
      3. the room that is left is shared between the remaining types in turn (stories, pages, events, authors, entities, keywords,
         claims), the best connected first, so that one large type cannot crowd the others out."""
    by_type: dict[str, list[dict]] = defaultdict(list)
    for n in nodes:
        by_type[n["type"]].append(n)
    for lst in by_type.values():
        lst.sort(key=lambda n: (-degree.get(n["id"], 0), n["id"]))
    small_limit, small_budget = max(5, max_nodes // 10), max_nodes // 4
    keep: list[dict] = []
    tiny: set[str] = set()
    for t in TYPE_PRIORITY:
        lst = by_type.get(t, [])
        if t != "Article" and 0 < len(lst) <= small_limit and len(keep) + len(lst) <= small_budget:
            keep += lst
            tiny.add(t)
    keep += by_type.get("Article", [])[:max(max_nodes - len(keep), 0)]
    rest = [by_type[t] for t in TYPE_PRIORITY if t != "Article" and t not in tiny and by_type.get(t)]
    pos = 0
    while len(keep) < max_nodes and any(pos < len(lst) for lst in rest):
        for lst in rest:
            if pos < len(lst) and len(keep) < max_nodes:
                keep.append(lst[pos])
        pos += 1
    return keep


class Neo4jGraph:
    def __init__(self, uri: str, user: str, password: str, database: str | None = None, driver=None) -> None:
        from neo4j import GraphDatabase
        self._owned = driver is None
        self._driver = driver or GraphDatabase.driver(uri, auth=(user, password), connection_acquisition_timeout=15)
        self._db = database
        self.ensure_schema()

    def _session(self):
        return self._driver.session(database=self._db) if self._db else self._driver.session()

    def ensure_schema(self) -> None:
        with self._session() as s:
            for q in ("CREATE CONSTRAINT paper_id IF NOT EXISTS FOR (n:Paper) REQUIRE n.id IS UNIQUE",
                      "CREATE CONSTRAINT article_id IF NOT EXISTS FOR (n:Article) REQUIRE n.event_id IS UNIQUE",
                      "CREATE CONSTRAINT story_id IF NOT EXISTS FOR (n:Story) REQUIRE n.story_id IS UNIQUE",
                      "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE",
                      "CREATE CONSTRAINT edition_id IF NOT EXISTS FOR (n:Edition) REQUIRE n.id IS UNIQUE",
                      "CREATE CONSTRAINT day_date IF NOT EXISTS FOR (n:Day) REQUIRE n.date IS UNIQUE",
                      "CREATE CONSTRAINT page_id IF NOT EXISTS FOR (n:Page) REQUIRE n.id IS UNIQUE",
                      "CREATE CONSTRAINT section_id IF NOT EXISTS FOR (n:Section) REQUIRE n.id IS UNIQUE",
                      "CREATE CONSTRAINT author_id IF NOT EXISTS FOR (n:Author) REQUIRE n.id IS UNIQUE",
                      "CREATE CONSTRAINT topic_id IF NOT EXISTS FOR (n:Topic) REQUIRE n.id IS UNIQUE",
                      "CREATE CONSTRAINT event_node_id IF NOT EXISTS FOR (n:Event) REQUIRE n.id IS UNIQUE",
                      "CREATE CONSTRAINT claim_id IF NOT EXISTS FOR (n:Claim) REQUIRE n.id IS UNIQUE",
                      "CREATE CONSTRAINT keyword_id IF NOT EXISTS FOR (n:Keyword) REQUIRE n.id IS UNIQUE",
                      "CREATE INDEX article_date IF NOT EXISTS FOR (n:Article) ON (n.date)",
                      "CREATE INDEX entity_name IF NOT EXISTS FOR (n:Entity) ON (n.name)"):
                s.run(q).consume()

    # ------------------------------------------------------------------------------------------------ write
    def write(self, docs: list[dict]) -> dict:
        rows = build_graph_rows(docs)
        with self._session() as s:
            s.execute_write(self._write_tx, rows)
        sim = defaultdict(int)
        for r in rows["similarity"]:
            sim[r["rel"]] += 1
        return {"papers": len(rows["papers"]), "stories": len(rows["stories"]), "articles": len(rows["articles"]),
                "entities": sum(len(v) for v in rows["entities"].values()),
                "relations": sum(len(v) for v in rows["relations"].values()),
                "editions": len(rows["editions"]), "days": len(rows["days"]), "pages": len(rows["pages"]),
                "sections": len(rows["sections"]), "authors": len(rows["authors"]), "topics": len(rows["topics"]),
                "events": len(rows["events"]), "claims": len(rows["claims"]), "keywords": len(rows["keywords"]),
                "similar_links": sim["SIMILAR_TO"], "possible_links": sim["POSSIBLE_MATCH"]}

    @staticmethod
    def _write_tx(tx, rows: dict) -> None:
        tx.run("UNWIND $rows AS r MERGE (p:Paper {id: r.id}) SET p.name = r.name, p.reputation = r.reputation",
               rows=rows["papers"])
        tx.run("UNWIND $rows AS r MERGE (s:Story {story_id: r.story_id}) SET s += r", rows=rows["stories"])
        # derived links of the articles being written are replaced, never accumulated (a re-cluster must leave nothing stale);
        # only OUTGOING links are removed: a similarity link is owned by the later article and kept by the earlier one
        tx.run("UNWIND $ids AS id MATCH (a:Article {event_id: id}) "
               "OPTIONAL MATCH (a)-[x:PART_OF|MENTIONS|ON_PAGE|IN_SECTION|WRITTEN_BY|HAS_TOPIC|REPORTS|MAKES_CLAIM|HAS_KEYWORD|SIMILAR_TO|POSSIBLE_MATCH]->() "
               "DELETE x", ids=[a["event_id"] for a in rows["articles"]])
        tx.run("""UNWIND $rows AS r
                  MERGE (a:Article {event_id: r.event_id})
                  SET a.headline = r.headline, a.page_no = r.page_no, a.date = r.date, a.published = r.published,
                      a.ocr_confidence = r.ocr_confidence, a.language = r.language, a.sentiment = r.sentiment,
                      a.event_type = r.event_type
                  WITH a, r
                  MATCH (p:Paper {id: r.paper_id}) MATCH (s:Story {story_id: r.story_id})
                  MERGE (a)-[pi:PUBLISHED_IN]->(p) SET pi.page = r.page_no
                  MERGE (a)-[:PART_OF]->(s)""", rows=rows["articles"])
        for label, ents in rows["entities"].items():
            if label in ENTITY_LABELS:                            # whitelisted, so safe to put in the query text
                tx.run(f"UNWIND $rows AS r MERGE (e:Entity {{id: r.id}}) SET e.name = r.name, e.label = r.label, e:{label}",
                       rows=ents)
        tx.run("""UNWIND $rows AS r MATCH (a:Article {event_id: r.event_id}) MATCH (e:Entity {id: r.entity_id})
                  MERGE (a)-[:MENTIONS]->(e)""", rows=rows["mentions"])
        for rel, edges in rows["relations"].items():
            tx.run(f"""UNWIND $rows AS r MATCH (a:Entity {{id: r.from}}) MATCH (b:Entity {{id: r.to}})
                       MERGE (a)-[x:`{rel}`]->(b)
                       SET x.date = coalesce(r.date, x.date),
                           x.event_ids = coalesce(x.event_ids, []) + [i IN r.event_ids WHERE NOT i IN coalesce(x.event_ids, [])]""",
                   rows=edges)

        # ---- structure
        tx.run("""UNWIND $rows AS r MERGE (e:Edition {id: r.id}) SET e.name = r.name, e.paper_id = r.paper_id
                  WITH e, r MATCH (p:Paper {id: r.paper_id}) MERGE (e)-[:OF_PAPER]->(p)""", rows=rows["editions"])
        tx.run("UNWIND $rows AS d MERGE (:Day {date: d})", rows=rows["days"])
        tx.run("""UNWIND $rows AS r MERGE (pg:Page {id: r.id}) SET pg.page_no = r.page_no, pg.date = r.date, pg.paper_id = r.paper_id
                  WITH pg, r MATCH (e:Edition {id: r.edition_id}) MATCH (d:Day {date: r.date})
                  MERGE (pg)-[:IN_EDITION]->(e) MERGE (pg)-[:ON_DAY]->(d)""", rows=rows["pages"])
        tx.run("UNWIND $rows AS r MERGE (s:Section {id: r.id}) SET s.name = r.name, s.paper_id = r.paper_id", rows=rows["sections"])
        tx.run("UNWIND $rows AS r MERGE (a:Author {id: r.id}) SET a.name = r.name, a.kind = r.kind, a.paper_id = r.paper_id",
               rows=rows["authors"])
        # ---- content
        tx.run("UNWIND $rows AS r MERGE (t:Topic {id: r.id}) SET t.name = r.name", rows=rows["topics"])
        tx.run("""UNWIND $rows AS r MERGE (e:Event {id: r.id})
                  SET e.type = r.type, e.trigger = r.trigger, e.actor = r.actor, e.institution = r.institution,
                      e.date = r.date, e.location = r.location""", rows=rows["events"])
        tx.run("UNWIND $rows AS r MERGE (c:Claim {id: r.id}) SET c.text = r.text, c.status = r.status, c.checkworthy = r.checkworthy",
               rows=rows["claims"])
        tx.run("UNWIND $rows AS r MERGE (k:Keyword {id: r.id}) SET k.text = r.text", rows=rows["keywords"])
        tx.run("""UNWIND $rows AS r MATCH (c:Claim {id: r.claim}) MATCH (p:Paper {id: r.paper_id}) MERGE (c)-[:SUPPORTED_BY]->(p)""",
               rows=rows["supported_by"])
        # ---- article links (each relationship name is a constant from this file, never user input)
        for rel, label, key in (("ON_PAGE", "Page", "id"), ("IN_SECTION", "Section", "id"), ("WRITTEN_BY", "Author", "id"),
                                ("REPORTS", "Event", "id"), ("MAKES_CLAIM", "Claim", "id"), ("HAS_KEYWORD", "Keyword", "id")):
            tx.run(f"UNWIND $rows AS r MATCH (a:Article {{event_id: r.event_id}}) MATCH (n:{label} {{{key}: r.to}}) MERGE (a)-[:{rel}]->(n)",
                   rows=rows["links"].get(rel, []))
        tx.run("""UNWIND $rows AS r MATCH (a:Article {event_id: r.event_id}) MATCH (t:Topic {id: r.to})
                  MERGE (a)-[x:HAS_TOPIC]->(t) SET x.confidence = r.confidence, x.subtopic = r.subtopic""",
               rows=rows["links"].get("HAS_TOPIC", []))
        # ---- similarity (a missing partner article, for example one not stored yet, simply creates no link)
        for rel in SIMILARITY_RELS:
            tx.run(f"""UNWIND $rows AS r MATCH (a:Article {{event_id: r.a}})-[:PUBLISHED_IN]->(pa:Paper)
                       MATCH (b:Article {{event_id: r.b}})-[:PUBLISHED_IN]->(pb:Paper)
                       MERGE (a)-[x:{rel}]->(b)
                       SET x.score = r.score, x.semantic = r.semantic, x.entity = r.entity, x.keyword = r.keyword, x.date = r.date,
                           x.location = r.location, x.cosine = r.cosine, x.veto = r.veto, x.reasons = r.reasons,
                           x.cross_paper = (pa.id <> pb.id)""", rows=[r for r in rows["similarity"] if r["rel"] == rel])
        # ---- clean-up: nodes nothing points to any more (stories emptied by a re-cluster, pages of a removed article, ...)
        tx.run("MATCH (s:Story) WHERE NOT ()-[:PART_OF]->(s) DETACH DELETE s")
        tx.run("MATCH (n) WHERE (n:Page OR n:Section OR n:Author OR n:Topic OR n:Event OR n:Claim OR n:Keyword) "
               "AND NOT ()-[:ON_PAGE|IN_SECTION|WRITTEN_BY|HAS_TOPIC|REPORTS|MAKES_CLAIM|HAS_KEYWORD]->(n) DETACH DELETE n")
        tx.run("MATCH (n) WHERE (n:Edition OR n:Day) AND NOT ()-[:IN_EDITION|ON_DAY]->(n) DETACH DELETE n")

    # ------------------------------------------------------------------------------------------------ read: one story
    def story_graph(self, story_id: str) -> dict:
        """Everything the UI needs about one story: papers/articles, entities, and the relations between entities
        that at least one of the story's articles reported."""
        with self._session() as s:
            arts = [dict(r) for r in s.run(
                """MATCH (:Story {story_id: $sid})<-[:PART_OF]-(a:Article)-[pi:PUBLISHED_IN]->(p:Paper)
                   RETURN a.event_id AS event_id, a.headline AS headline, p.id AS paper_id, p.name AS paper, pi.page AS page
                   ORDER BY p.name""", sid=story_id)]
            ids = [a["event_id"] for a in arts]
            ents = [dict(r) for r in s.run(
                """MATCH (a:Article)-[:MENTIONS]->(e:Entity) WHERE a.event_id IN $ids
                   RETURN e.id AS id, e.name AS name, e.label AS label, count(DISTINCT a) AS mentioned_in
                   ORDER BY mentioned_in DESC, name""", ids=ids)]
            rels = [dict(r) for r in s.run(
                """MATCH (a:Entity)-[r]->(b:Entity) WHERE any(i IN r.event_ids WHERE i IN $ids)
                   RETURN a.id AS from_id, a.name AS from_name, type(r) AS rel, b.id AS to_id, b.name AS to_name,
                          [i IN r.event_ids WHERE i IN $ids] AS event_ids, r.date AS date
                   ORDER BY rel""", ids=ids)]
        names = {a["event_id"]: a["paper"] for a in arts}
        for r in rels:
            r["papers"] = sorted({names[i] for i in r["event_ids"] if i in names})
        return {"story_id": story_id, "articles": arts, "entities": ents, "relations": rels}

    # ------------------------------------------------------------------------------------------------ read: a run's graph
    @staticmethod
    def _label(t: str, p: dict) -> str:
        """The short text drawn on a node."""
        if t == "Article":
            return (p.get("headline") or "(no headline)")[:90]
        if t == "Page":
            return f"p.{p.get('page_no')}"
        if t == "Event":
            return " - ".join(x for x in (p.get("type"), p.get("actor") or p.get("institution")) if x)[:80] or "event"
        if t == "Story":
            return (p.get("key") or p.get("location") or p.get("story_id") or "story")[:60]
        if t in ("Claim", "Keyword"):
            return (p.get("text") or "")[:80]
        return str(p.get("name") or p.get("date") or p.get("id") or t)[:80]

    def run_ids(self, limit: int = 3000) -> list[str]:
        """Event ids of the most recent articles in the whole graph (the "all runs" scope)."""
        with self._session() as s:
            return [r[0] for r in s.run("MATCH (a:Article) RETURN a.event_id ORDER BY a.published DESC, a.event_id LIMIT $n", n=limit)]

    def _link_rows(self, s, ids: list[str], types: set[str], min_score: float) -> list[tuple]:
        """(from node id, to node id, relationship, properties) for the run's articles, restricted to the requested node types."""
        out: list[tuple] = []
        for rel, a_type, b_type in LINK_SPECS:
            if a_type != "Article" or b_type not in types:
                continue
            _, key, _ = NODE_SPECS[b_type]
            for r in s.run(f"MATCH (a:Article) WHERE a.event_id IN $ids MATCH (a)-[x:{rel}]->(n:{b_type}) "
                           f"RETURN a.event_id AS a, n.{key} AS b, properties(x) AS p", ids=ids):
                out.append((node_id("Article", r["a"]), node_id(b_type, r["b"]), rel, dict(r["p"] or {})))
        chain = (("ON_PAGE", "IN_EDITION", "Page", "Edition"), ("ON_PAGE", "ON_DAY", "Page", "Day"))
        for first, rel, mid, end in chain:
            if mid in types and end in types:
                for r in s.run(f"MATCH (a:Article) WHERE a.event_id IN $ids MATCH (a)-[:{first}]->(m:{mid})-[:{rel}]->(e:{end}) "
                               f"RETURN DISTINCT m.id AS a, e.{NODE_SPECS[end][1]} AS b", ids=ids):
                    out.append((node_id(mid, r["a"]), node_id(end, r["b"]), rel, {}))
        if "Edition" in types and "Paper" in types:
            for r in s.run("MATCH (a:Article) WHERE a.event_id IN $ids MATCH (a)-[:ON_PAGE]->(:Page)-[:IN_EDITION]->(e:Edition)-[:OF_PAPER]->(p:Paper) "
                           "RETURN DISTINCT e.id AS a, p.id AS b", ids=ids):
                out.append((node_id("Edition", r["a"]), node_id("Paper", r["b"]), "OF_PAPER", {}))
        if "Claim" in types and "Paper" in types:
            for r in s.run("MATCH (a:Article) WHERE a.event_id IN $ids MATCH (a)-[:MAKES_CLAIM]->(c:Claim)-[:SUPPORTED_BY]->(p:Paper) "
                           "RETURN DISTINCT c.id AS a, p.id AS b", ids=ids):
                out.append((node_id("Claim", r["a"]), node_id("Paper", r["b"]), "SUPPORTED_BY", {}))
        for rel in SIMILARITY_RELS:
            for r in s.run(f"MATCH (a:Article)-[x:{rel}]->(b:Article) WHERE a.event_id IN $ids AND b.event_id IN $ids AND x.score >= $min "
                           f"RETURN a.event_id AS a, b.event_id AS b, properties(x) AS p", ids=ids, min=min_score):
                out.append((node_id("Article", r["a"]), node_id("Article", r["b"]), rel, dict(r["p"] or {})))
        if "Entity" in types:
            for r in s.run("MATCH (x:Entity)-[r]->(y:Entity) WHERE any(i IN r.event_ids WHERE i IN $ids) "
                           "RETURN x.id AS a, y.id AS b, type(r) AS rel, r.date AS date", ids=ids):
                out.append((node_id("Entity", r["a"]), node_id("Entity", r["b"]), r["rel"], {"date": r["date"]}))
        return out

    def _node_props(self, s, wanted: dict[str, set]) -> dict[str, dict]:
        nodes: dict[str, dict] = {}
        for t, keys in wanted.items():
            label, key, props = NODE_SPECS[t]
            if not keys:
                continue
            for r in s.run(f"MATCH (n:{label}) WHERE n.{key} IN $keys RETURN n.{key} AS k, properties(n) AS p", keys=list(keys)):
                p = {k: v for k, v in dict(r["p"]).items() if k in props or k == key}
                nodes[node_id(t, r["k"])] = {"id": node_id(t, r["k"]), "type": t, "label": self._label(t, p), "props": p}
        return nodes

    def run_summary(self, ids: list[str]) -> dict:
        """How many nodes of each type, and links of each kind, belong to these articles (fills the legend and the toggles)."""
        counts: dict[str, int] = {}
        links: dict[str, int] = {}
        with self._session() as s:
            counts["Article"] = s.run("MATCH (a:Article) WHERE a.event_id IN $ids RETURN count(a)", ids=ids).single()[0]
            for rel, a_type, b_type in LINK_SPECS:
                if a_type == "Article":
                    r = s.run(f"MATCH (a:Article) WHERE a.event_id IN $ids MATCH (a)-[x:{rel}]->(n:{b_type}) "
                              f"RETURN count(DISTINCT n) AS nodes, count(x) AS links", ids=ids).single()
                    counts[b_type] = r["nodes"]
                    links[rel] = r["links"]
            counts["Edition"] = s.run("MATCH (a:Article) WHERE a.event_id IN $ids MATCH (a)-[:ON_PAGE]->(:Page)-[:IN_EDITION]->(e:Edition) "
                                      "RETURN count(DISTINCT e)", ids=ids).single()[0]
            counts["Day"] = s.run("MATCH (a:Article) WHERE a.event_id IN $ids MATCH (a)-[:ON_PAGE]->(:Page)-[:ON_DAY]->(d:Day) "
                                  "RETURN count(DISTINCT d)", ids=ids).single()[0]
            for rel in SIMILARITY_RELS:
                links[rel] = s.run(f"MATCH (a:Article)-[x:{rel}]->(b:Article) WHERE a.event_id IN $ids AND b.event_id IN $ids RETURN count(x)",
                                   ids=ids).single()[0]
        return {"nodes_by_type": counts, "links_by_type": links}

    def run_graph(self, ids: list[str], types: set[str], min_score: float = 0.0, max_nodes: int = 300) -> dict:
        """The run's graph as nodes and edges for the UI. Articles are always included; the other node types only when asked for.
        When more than `max_nodes` would be drawn, `select_nodes` keeps every requested type represented."""
        types = (set(types) & set(NODE_SPECS)) | {"Article"}
        with self._session() as s:
            links = self._link_rows(s, ids, types, min_score)
            wanted: dict[str, set] = defaultdict(set)
            wanted["Article"].update(ids)
            for a, b, _, _ in links:
                for nid in (a, b):
                    t, k = parse_node_id(nid)
                    wanted[t].add(k)
            # only articles that exist; every other node must be one the links reach
            nodes = self._node_props(s, wanted)
        degree: dict[str, int] = defaultdict(int)
        for a, b, _, _ in links:
            degree[a] += 1
            degree[b] += 1
        total = len(nodes)
        keep = select_nodes(list(nodes.values()), degree, max_nodes)
        kept = {n["id"] for n in keep}
        edges, seen = [], set()
        for a, b, rel, p in links:
            if a in kept and b in kept and (a, b, rel) not in seen:
                seen.add((a, b, rel))
                edges.append({"id": f"{a}|{rel}|{b}", "source": a, "target": b, "type": rel, "props": p})
        return {"nodes": keep, "edges": edges, "truncated": total > len(keep), "total_nodes": total}

    def neighbors(self, node: str, types: set[str], ids: list[str] | None, limit: int = 50) -> dict:
        """The nodes next to one node (the "expand" action). `ids` restricts article neighbours to a run (None = any run)."""
        parsed = parse_node_id(node)
        if not parsed:
            return {"nodes": [], "edges": []}
        t, k = parsed
        label, key, _ = NODE_SPECS[t]
        types = set(types) & set(NODE_SPECS)
        found, edges = defaultdict(set), []
        with self._session() as s:
            for r in s.run(f"MATCH (n:{label} {{{key}: $k}})-[x]-(m) RETURN type(x) AS rel, startNode(x) = n AS out, labels(m) AS labels, "
                           f"m AS m, properties(x) AS p LIMIT $limit", k=k, limit=limit * 4):
                mt = next((x for x in r["labels"] if x in NODE_SPECS and x != "Entity"), None) or ("Entity" if "Entity" in r["labels"] else None)
                if mt not in types:
                    continue
                mk = r["m"].get(NODE_SPECS[mt][1])
                if mt == "Article" and ids is not None and mk not in ids:
                    continue
                if len(found[mt]) >= limit:
                    continue
                found[mt].add(mk)
                a, b = (node, node_id(mt, mk)) if r["out"] else (node_id(mt, mk), node)
                edges.append({"id": f"{a}|{r['rel']}|{b}", "source": a, "target": b, "type": r["rel"], "props": dict(r["p"] or {})})
            nodes = self._node_props(s, found)
        return {"nodes": list(nodes.values()), "edges": edges}

    def close(self) -> None:
        if self._owned:
            self._driver.close()
