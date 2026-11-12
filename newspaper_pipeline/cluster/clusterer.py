"""Incremental story clustering across newspapers.

For each new article: embed -> ANN candidates (date window) -> pair score (+ optional judge for borderline pairs)
-> join the matching story, merge stories it bridges, or open a new one. Union-find keeps merges consistent, so
the result does not depend on the order articles arrive in (apart from tie-breaking of story ids).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re

from ..config import Settings
from .embed import Embedder, HashingEmbedder, embedding_text
from .index import DateWindow, InMemoryIndex, VectorIndex
from .scorer import PairJudge, RuleScorer
from .signals import jaccard, pair_features, shingles

_SLUG_STOP = set("a an the of in on at to for from by with and or as is are was were be been it its this that "
                 "after over into new says said say".split())


def suggested_story_key(article: dict, n_words: int = 6) -> str:
    """Readable key like 'punjab-assembly-resignation-vijay-pratap-singh-2026-11'."""
    head = re.sub(r"[^a-z0-9\s]", " ", (article.get("working_headline") or "").lower())
    words = [w for w in head.split() if w not in _SLUG_STOP][:n_words]
    return "-".join(words + [article["date"][:7]])


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, x: str) -> None:
        self.parent.setdefault(x, x)

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> str:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return ra
        keep, drop = sorted((ra, rb))          # deterministic: smaller id survives
        self.parent[drop] = keep
        return keep


class StoryClusterer:
    def __init__(self, embedder: Embedder | None = None, index: VectorIndex | None = None,
                 scorer: RuleScorer | None = None, judge: PairJudge | None = None,
                 settings: Settings | None = None) -> None:
        self.s = settings or Settings()
        self.embedder = embedder or HashingEmbedder()
        self.index = index if index is not None else InMemoryIndex()
        self.scorer = scorer or RuleScorer(cos_ref=getattr(self.embedder, "cos_ref", 0.40))
        self.judge = judge
        self._uf = _UnionFind()
        self._articles: dict[str, dict] = {}          # event_id -> article
        self._story_of: dict[str, str] = {}           # event_id -> story id (pre-merge, resolved via union-find)
        self._meta: dict[str, dict] = {}
        self.merges: list[tuple[str, str]] = []
        self._pre_vec: dict = {}                      # event_id -> vector embedded ahead of time in a batch

    # ----------------------------------------------------------------------------------------------- core
    def _window_filter(self, article: dict):
        return DateWindow(dt.date.fromisoformat(article["date"]), max(1, self.s.candidate_window_h // 24))

    @staticmethod
    def _snapshot(article: dict) -> dict:
        """What the pair scorer needs about an article, kept in the index payload so a *later run* can still compare
        against it (the in-memory article dict will not exist then)."""
        return {k: article.get(k) for k in ("event_id", "paper_id", "paper_name", "page_no", "date", "city", "wire_source",
                                           "working_headline")} | {"working_text": (article.get("working_text") or "")[:4000]}

    def _story_for(self, cid: str, payload: dict) -> str:
        sid = self._story_of.get(cid) or payload.get("story_id") or "story-" + hashlib.sha1(cid.encode()).hexdigest()[:10]
        self._uf.add(sid)
        self._story_of.setdefault(cid, sid)
        return sid

    def _decide(self, a: dict, b: dict, cosine: float) -> tuple[float, list[str]]:
        scored = self.scorer.score(pair_features(a, b, cosine))
        score, reasons = scored.score, list(scored.reasons)
        if self.judge and self.s.borderline_threshold <= score < self.s.same_story_threshold:
            j = self.judge.judge(a, b)
            if j is not None:
                score = 0.5 * score + 0.5 * j
                reasons.append(f"judge={j:.2f}")
        return score, reasons

    def add(self, article: dict) -> dict:
        eid = article["event_id"]
        if eid in self._articles:
            return self.result_for(eid)
        vec = self._pre_vec.pop(eid, None)
        if vec is None:
            vec = self.embedder.embed([embedding_text(article)])[0]
        cands = self.index.search(vec, self.s.candidate_top_k, self._window_filter(article))

        matches, borderline, near_dup = [], [], None
        best_shingle = 0.0
        sh_a = shingles(article["working_headline"] + ". " + article["working_text"])
        for cid, cosine, payload in cands:
            if cid == eid:
                continue                                   # a persistent index may already hold this article
            other = self._articles.get(cid) or payload.get("snap")
            if other is None:
                continue
            self._story_for(cid, payload)
            score, reasons = self._decide(article, other, cosine)
            if score >= self.s.same_story_threshold:
                matches.append({"event_id": cid, "score": round(score, 3), "reasons": reasons})
            elif score >= self.s.borderline_threshold:
                borderline.append({"event_id": cid, "score": round(score, 3), "reasons": reasons})
            sj = jaccard(sh_a, shingles((other.get("working_headline") or "") + ". " + (other.get("working_text") or "")))
            if sj >= self.s.near_dup_jaccard and sj > best_shingle:
                best_shingle, near_dup = sj, cid

        self._articles[eid] = article
        if matches:
            roots = {self._uf.find(self._story_of[m["event_id"]]) for m in matches}
            story = min(roots)
            self._story_of[eid] = story
            for r in roots:
                if r != story:
                    self._uf.union(story, r)
                    self.merges.append((r, story))
                    if hasattr(self.index, "relabel_story"):
                        self.index.relabel_story(r, story)     # keep persisted rows pointing at the surviving story id
            status = "merged_stories" if len(roots) > 1 else "joined_story"
        else:
            story = "story-" + hashlib.sha1(eid.encode()).hexdigest()[:10]
            self._uf.add(story)
            self._story_of[eid] = story
            status = "new_story"
        article_meta = {"matches": matches, "borderline": borderline, "near_dup_of": near_dup,
                        "near_dup_jaccard": round(best_shingle, 3), "status": status}
        self._meta[eid] = article_meta
        self.index.add(eid, vec, {"date": article["date"], "paper_id": article["paper_id"], "story_id": story,
                                  "snap": self._snapshot(article)})
        return self.result_for(eid)

    # ----------------------------------------------------------------------------------------------- views
    def story_id(self, event_id: str) -> str:
        return self._uf.find(self._story_of[event_id])

    def result_for(self, event_id: str) -> dict:
        meta = self._meta.get(event_id, {})
        sid = self.story_id(event_id)
        members = [e for e in self._articles if self.story_id(e) == sid]
        return {"story_id": sid, "cluster_status": meta.get("status"), "matches": meta.get("matches", []),
                "review_candidates": meta.get("borderline", []), "near_dup_of": meta.get("near_dup_of"),
                "near_dup_jaccard": meta.get("near_dup_jaccard", 0.0), "cluster_size": len(members)}

    def stories(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for eid, art in self._articles.items():
            sid = self.story_id(eid)
            st = out.setdefault(sid, {"story_id": sid, "members": [], "papers": [], "dates": set()})
            st["members"].append(eid)
            if art["paper_id"] not in st["papers"]:
                st["papers"].append(art["paper_id"])
            st["dates"].add(art["date"])
        for st in out.values():
            first = self._articles[st["members"][0]]
            st["suggested_story_key"] = suggested_story_key(first)
            st["dates"] = sorted(st["dates"])
            st["n_papers"] = len(st["papers"])
        return out

    def cluster_all(self, articles: list[dict]) -> list[dict]:
        """Add a batch (deterministic order) and annotate each article with its final cluster fields."""
        ordered = sorted(articles, key=lambda x: (x["date"], x["paper_id"], x["page_no"], x["event_id"]))
        todo = [a for a in ordered if a["event_id"] not in self._articles]
        for i in range(0, len(todo), 64):                      # embed in batches (one GPU call per 64 articles)
            chunk = todo[i:i + 64]
            vecs = self.embedder.embed([embedding_text(a) for a in chunk])
            self._pre_vec.update({a["event_id"]: v for a, v in zip(chunk, vecs)})
        for a in ordered:
            self.add(a)
        self._pre_vec.clear()
        stories = self.stories()
        for a in articles:
            r = self.result_for(a["event_id"])
            a["story_id"] = r["story_id"]
            a["cluster"] = {**r, "suggested_story_key": stories[r["story_id"]]["suggested_story_key"],
                            "story_papers": stories[r["story_id"]]["papers"]}
        return articles
