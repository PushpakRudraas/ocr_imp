"""Relationship graph in Neo4j.

    (:Paper)<-[:PUBLISHED_IN {page}]-(:Article)-[:PART_OF]->(:Story)
    (:Article)-[:MENTIONS]->(:Entity:Person|Organization|PoliticalParty|Location)
    (:Entity)-[:RESIGNED_FROM|ASSOCIATED_WITH|LOCATED_IN|... {date, event_ids}]->(:Entity)      # from each article's knowledge_graph

Entity nodes are keyed by slug, so "Kunwar Vijay Pratap Singh" in four newspapers is ONE node with four articles mentioning
it, and a relation reported by several papers is one edge whose `event_ids` lists them all. Writes are MERGEs, so re-running
a stage never duplicates anything; articles whose story changed are re-pointed and orphaned stories removed.

`build_graph_rows` is pure (no driver) so the mapping is unit-testable without a database."""
from __future__ import annotations

import re
from collections import defaultdict

from ..enrich.lexicons import KNOWN_ORGS

ENTITY_LABELS = {"Person", "Organization", "PoliticalParty", "Location"}     # the only labels ever put into Cypher text
_REL_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,40}$")
_SLUG = re.compile(r"[^a-z0-9]+")


def slug(s: str) -> str:
    return _SLUG.sub("_", s.lower()).strip("_")


# "AAP" and "Aam Aadmi Party" are one node: acronym slugs map to the slug of their expansion
_CANON = {slug(ac): slug(full) for ac, full in KNOWN_ORGS.items() if slug(ac) != slug(full)}
_FULL_NAME = {slug(full): full for full in KNOWN_ORGS.values()}


def canon(entity_id: str) -> str:
    return _CANON.get(entity_id, entity_id)


def build_graph_rows(docs: list[dict]) -> dict:
    papers, stories, articles = {}, {}, []
    entities: dict[str, dict] = {}                 # slug -> {id, name, label}
    mentions: list[dict] = []
    rels: dict[str, dict[tuple, dict]] = defaultdict(dict)

    for d in docs:
        nl, ev = d["newspaper"], d["event"]
        sid = d["story"]["story_id"] or d["event_id"]
        papers[nl["paper_id"]] = {"id": nl["paper_id"], "name": nl["paper"], "reputation": d["credibility"]["source_reputation"]}
        story = stories.setdefault(sid, {"story_id": sid, "key": d["dedup_clustering"].get("suggested_story_key"),
                                         "event_type": ev["type"], "location": d["geo"]["primary_location"],
                                         "topic": d["classification"]["topic"], "impact": d["impact_score"]["value"],
                                         "summary": d["summary"]["multi_doc_summary"], "n_papers": d["story"]["n_papers"],
                                         "date": ev["date"]})
        story["impact"] = max(story["impact"], d["impact_score"]["value"])
        articles.append({"event_id": d["event_id"], "paper_id": nl["paper_id"], "story_id": sid, "headline": nl["headline"],
                         "page_no": nl["page_no"], "date": ev["date"], "ocr_confidence": nl["ocr_confidence"],
                         "language": nl["language"], "sentiment": d["sentiment"]["document"]["label"],
                         "event_type": ev["type"]})
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
    return {"papers": list(papers.values()), "stories": list(stories.values()), "articles": articles,
            "entities": dict(by_label), "mentions": mentions, "relations": {k: list(v.values()) for k, v in rels.items()}}


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
                      "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE"):
                s.run(q).consume()

    # ------------------------------------------------------------------------------------------------ write
    def write(self, docs: list[dict]) -> dict:
        rows = build_graph_rows(docs)
        with self._session() as s:
            s.execute_write(self._write_tx, rows)
        return {"papers": len(rows["papers"]), "stories": len(rows["stories"]), "articles": len(rows["articles"]),
                "entities": sum(len(v) for v in rows["entities"].values()),
                "relations": sum(len(v) for v in rows["relations"].values())}

    @staticmethod
    def _write_tx(tx, rows: dict) -> None:
        tx.run("UNWIND $rows AS r MERGE (p:Paper {id: r.id}) SET p.name = r.name, p.reputation = r.reputation",
               rows=rows["papers"])
        tx.run("UNWIND $rows AS r MERGE (s:Story {story_id: r.story_id}) SET s += r", rows=rows["stories"])
        tx.run("""UNWIND $rows AS r
                  MERGE (a:Article {event_id: r.event_id})
                  SET a.headline = r.headline, a.page_no = r.page_no, a.date = r.date, a.ocr_confidence = r.ocr_confidence,
                      a.language = r.language, a.sentiment = r.sentiment, a.event_type = r.event_type
                  WITH a, r
                  OPTIONAL MATCH (a)-[old:PART_OF]->(:Story) DELETE old
                  WITH a, r
                  OPTIONAL MATCH (a)-[oldm:MENTIONS]->(:Entity) DELETE oldm
                  WITH DISTINCT a, r
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
        tx.run("MATCH (s:Story) WHERE NOT ()-[:PART_OF]->(s) DETACH DELETE s")        # stories emptied by a re-cluster

    # ------------------------------------------------------------------------------------------------ read
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

    def close(self) -> None:
        if self._owned:
            self._driver.close()
