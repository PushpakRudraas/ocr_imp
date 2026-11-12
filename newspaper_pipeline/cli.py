"""Command line.

    python -m newspaper_pipeline demo [--out out/demo]          # synthetic papers, no models needed
    python -m newspaper_pipeline run --manifest manifest.yaml --out out/ [--ocr rapidocr] [--translator indictrans2]
                                     [--embedder bge-m3] [--index qdrant] [--judge claude] [--llm-refine]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
from pathlib import Path

import dataclasses

from .config import Settings
from .input.manifest import Manifest, PaperSpec, load_manifest
from .pipeline import Pipeline
from .store import make_store


def build_pipeline(args, settings: Settings, shared: dict | None = None, phase: str = "all") -> Pipeline:
    """`shared` caches heavy models (OCR engine, translator, embedder) so a long-lived server loads them once.

    `phase` loads only what that part of the flow needs: "extract" = OCR + translator + store (no embedder, NLP models,
    vector index or graph), "enrich" = store + embedder + index + graph + NLP models (no OCR, no translator), "all" = both."""
    extract, enrich = phase in ("all", "extract"), phase in ("all", "enrich")
    shared = shared if shared is not None else {}
    cached = lambda key, make: shared[key] if key in shared else shared.setdefault(key, make())
    from .cluster.clusterer import StoryClusterer
    from .cluster.embed import HashingEmbedder, SentenceTransformerEmbedder, minilm_embedder
    from .cluster.index import InMemoryIndex, QdrantIndex
    from .cluster.scorer import LlmJudge, RuleScorer

    engine = None
    if not extract:
        pass
    elif args.ocr == "rapidocr":
        from .ocr.backends import RapidOcrEngine; engine = cached("ocr", RapidOcrEngine)
    elif args.ocr == "tesseract":
        from .ocr.backends import TesseractEngine; engine = cached("ocr", lambda: TesseractEngine(args.ocr_lang))
    elif args.ocr == "paddle":
        from .ocr.backends import PaddleOcrEngine; engine = cached("ocr", lambda: PaddleOcrEngine(args.ocr_lang))

    translator = None
    if not extract:
        pass
    elif args.translator == "aws":
        from .clean.translate import AwsTranslateBackend; translator = cached("tr", AwsTranslateBackend)
    elif args.translator == "indictrans2":
        from .clean.translate import IndicTrans2Backend; translator = cached("tr", IndicTrans2Backend)

    store_backend = getattr(args, "store", None) or settings.store_backend
    index_backend = getattr(args, "index", None) or settings.index_backend
    graph_backend = getattr(args, "graph", None) or settings.graph_backend
    settings = dataclasses.replace(settings, store_backend=store_backend)
    if not enrich:                         # extraction only reads PDFs and writes clean events: nothing below is needed
        return Pipeline(settings, make_store(settings, shared), translator, engine)
    dim = settings.embed_dim
    from .device import describe, resolve_device
    device = resolve_device(getattr(args, "device", None))      # NP_DEVICE: auto | cuda (fail if no GPU) | cpu
    if "device_logged" not in shared:
        shared["device_logged"] = True
        logging.getLogger("np.device").info("enrichment models run on: %s", describe(device))
    makers = {"bge-m3": lambda: SentenceTransformerEmbedder(device=device), "minilm": lambda: minilm_embedder(device)}
    embedder = cached(f"emb{args.embedder}{dim}{device}", makers.get(args.embedder) or (lambda: HashingEmbedder(dim=dim)))
    if index_backend == "pgvector":
        from .cluster.index import PgVectorIndex
        index = PgVectorIndex(settings.pg_dsn, embedder.dim)
    elif index_backend == "qdrant":
        index = QdrantIndex(embedder.dim, path=getattr(args, "qdrant_path", None))
    else:
        index = InMemoryIndex()
    judge = LlmJudge() if args.judge == "claude" else None
    clusterer = StoryClusterer(embedder, index, RuleScorer(cos_ref=embedder.cos_ref), judge, settings)
    graph = None
    if graph_backend == "neo4j":
        from .graph.neo4j_graph import Neo4jGraph
        graph = Neo4jGraph(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)

    models = None
    if getattr(args, "models", "none") == "hf":
        from .enrich.models import HfModels; models = cached(f"hf_models{device}", lambda: HfModels(device=device))
    refiner = None
    if args.llm_refine:
        from .enrich.llm import LlmRefiner; refiner = LlmRefiner()
    p = Pipeline(settings, make_store(settings, shared), translator, engine, clusterer, refiner=refiner, graph=graph, models=models)
    p.device = device if (models is not None or args.embedder != "hashing") else "cpu"      # what the ML parts actually run on
    return p


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", default="out")
    p.add_argument("--data-dir", default="data")
    p.add_argument("--ocr", choices=["none", "rapidocr", "tesseract", "paddle"], default="none",
                   help="engine for scanned pages (digital PDFs never need one)")
    p.add_argument("--ocr-lang", default="eng")
    p.add_argument("--translator", choices=["none", "aws", "indictrans2"], default="none")
    p.add_argument("--embedder", choices=["hashing", "bge-m3", "minilm"], default="hashing")
    p.add_argument("--models", choices=["none", "hf"], default="none",
                   help="hf = real NER/topic/sentiment/emotion models (HuggingFace) for enrichment")
    p.add_argument("--store", choices=["sqlite", "mongo"], default=None, help="default: NP_STORE or sqlite")
    p.add_argument("--index", choices=["memory", "qdrant", "pgvector"], default=None, help="default: NP_INDEX or memory")
    p.add_argument("--graph", choices=["none", "neo4j"], default=None, help="default: NP_GRAPH or none")
    p.add_argument("--qdrant-path", default=None)
    p.add_argument("--judge", choices=["none", "claude"], default="none")
    p.add_argument("--device", choices=["auto", "cuda", "cpu"], default=None,
                   help="where the embedder / NLP models run (default: NP_DEVICE or auto = GPU if usable)")
    p.add_argument("--llm-refine", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="newspaper_pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run"); _common(run); run.add_argument("--manifest", required=True)
    demo = sub.add_parser("demo"); _common(demo)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    out = Path(args.out)
    settings = Settings(data_dir=args.data_dir, db_path=str(Path(args.data_dir) / "pipeline.sqlite"))
    if args.cmd == "demo":
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
        from fixtures import make_papers
        import datetime as dt
        inbox = Path(args.data_dir) / "inbox"
        pdfs = make_papers.build_all(inbox)
        manifest = Manifest(dt.date(2026, 11, 2), [
            PaperSpec(id=pid, name=name, language="hi" if hindi else "en", edition="Delhi", reputation="high",
                      pdf=str(pdfs[pid])) for pid, name, hindi, _ in make_papers.PAPERS if pid in pdfs])
        if args.translator == "none" and any(p.language != "en" for p in manifest.papers):
            logging.warning("demo includes a Hindi paper but --translator is none: its articles will not be matched "
                            "to the English ones (use --translator aws|indictrans2 or --embedder bge-m3)")
    else:
        manifest = load_manifest(args.manifest)

    result = build_pipeline(args, settings).run(manifest, out)
    print(json.dumps(result.metrics, indent=2))
    print(f"\nwrote {result.export['articles']} articles, {result.export['stories']} stories, "
          f"{result.export['review']} review items -> {out}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
