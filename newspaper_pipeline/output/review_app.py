"""Streamlit review UI.   streamlit run newspaper_pipeline/output/review_app.py -- --out out/

Pick a story on the left to see how each newspaper covered it side by side (OCR text vs translation), the
cross-paper summary and claim verification, and the enrichment JSON. Needs `pip install streamlit`."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load(out: Path):
    stories = json.loads((out / "stories.json").read_text(encoding="utf-8"))
    review = json.loads((out / "review.json").read_text(encoding="utf-8"))
    docs = {}
    for f in (out / "articles").rglob("*.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        docs[d["event_id"]] = d
    metrics = json.loads((out / "metrics.json").read_text()) if (out / "metrics.json").exists() else {}
    return stories, review, docs, metrics


def main() -> None:
    import streamlit as st
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out")
    args, _ = ap.parse_known_args(sys.argv[1:])
    stories, review, docs, metrics = load(Path(args.out))

    st.set_page_config(page_title="Newspaper stories", layout="wide")
    st.sidebar.title("Stories")
    multi_only = st.sidebar.checkbox("Only stories in 2+ papers", value=True)
    shown = [s for s in stories if s["n_papers"] >= 2 or not multi_only]
    labels = [f"{s['n_papers']}x  {s['coverage'][0]['headline'][:60]}" for s in shown]
    if metrics:
        st.sidebar.json(metrics, expanded=False)
    tab_story, tab_review = st.tabs(["Story comparison", f"Review queue ({len(review)})"])

    with tab_story:
        if not shown:
            st.info("No stories to show.")
        else:
            story = shown[st.sidebar.radio("Story", range(len(shown)), format_func=lambda i: labels[i])]
            st.header(story["suggested_story_key"] or story["story_id"])
            st.caption(f"{story['event_type']} - {story['location']} - topic {story['topic']} - "
                       f"impact {story['impact']} - {story['bias_label']}")
            st.write(story["summary"])
            cols = st.columns(len(story["coverage"]))
            for col, cov in zip(cols, story["coverage"]):
                d = docs[cov["event_id"]]
                with col:
                    st.subheader(cov["paper"])
                    st.caption(f"page {cov['page_no']} - {cov['language']} - OCR {cov['ocr_confidence']} - {cov['sentiment']}")
                    st.markdown(f"**{cov['translated_headline'] or cov['headline']}**")
                    if cov["translated_headline"]:
                        st.caption(f"original: {cov['headline']}")
                    st.write(d["newspaper"]["translated_text"] or d["newspaper"]["text"])
                    with st.expander("Claims"):
                        for c in d["claims"]:
                            st.write(f"- [{c['verification_status']}] {c['claim']}")
                    with st.expander("Full enrichment JSON"):
                        st.json({k: v for k, v in d.items() if k != "newspaper"})

    with tab_review:
        st.dataframe(review)


if __name__ == "__main__":
    main()
