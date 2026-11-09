import { useEffect, useState } from 'react';
import { ApiError, getArticle } from '../api';
import { humanize } from '../lib/guess';
import type { Article, Coverage, Story } from '../types';
import Relationships from './Relationships';

function PaperColumn({ runId, cov }: { runId: string; cov: Coverage }) {
  const [art, setArt] = useState<Article | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [english, setEnglish] = useState(true);
  const [showJson, setShowJson] = useState(false);

  useEffect(() => {
    let live = true;
    setArt(null); setErr(null);
    getArticle(runId, cov.event_id).then((a) => live && setArt(a)).catch((e) => live && setErr(e instanceof ApiError ? e.message : 'Failed to load'));
    return () => { live = false; };
  }, [runId, cov.event_id]);

  const translated = !!cov.translated_headline;
  const text = art ? (english && art.newspaper.translated_text ? art.newspaper.translated_text : art.newspaper.text) : '';
  return (
    <article className="paper-col" aria-label={cov.paper}>
      <header>
        <h3>{cov.paper}</h3>
        <p className="muted small">
          page {cov.pages.length > 1 ? cov.pages.join(' + ') : cov.page_no} - {cov.language}
          {cov.ocr_confidence != null && ` - OCR ${Math.round(cov.ocr_confidence * 100)}%`}
          <span className={`chip sentiment-${cov.sentiment}`}>{cov.sentiment}</span>
        </p>
      </header>
      <h4>{english && translated ? cov.translated_headline : cov.headline}</h4>
      {cov.byline && <p className="muted small">By {cov.byline}</p>}
      {translated && (
        <button type="button" className="link-btn" onClick={() => setEnglish((v) => !v)}>
          {english ? 'Show original text' : 'Show English translation'}
        </button>
      )}
      {err && <p className="notice error">{err}</p>}
      {!art && !err && <p className="muted small">Loading text...</p>}
      {art && (
        <>
          <div className="article-text">{text.split(/\n\s*\n/).map((p, i) => <p key={i}>{p}</p>)}</div>
          {art.claims.length > 0 && (
            <details>
              <summary>Claims ({art.claims.length})</summary>
              <ul className="claims">
                {art.claims.map((c, i) => (
                  <li key={i}><span className={`chip claim-${c.verification_status}`}>{humanize(c.verification_status)}</span> {c.claim}
                    {c.corroborated_by && <span className="muted small"> - also in {c.corroborated_by.join(', ')}</span>}</li>
                ))}
              </ul>
            </details>
          )}
          <button type="button" className="link-btn" onClick={() => setShowJson((v) => !v)}>{showJson ? 'Hide' : 'Show'} enrichment JSON</button>
          {showJson && <pre className="json">{JSON.stringify(Object.fromEntries(Object.entries(art).filter(([k]) => k !== 'newspaper')), null, 2)}</pre>}
        </>
      )}
    </article>
  );
}

export default function StoryDetail({ runId, story }: { runId: string; story: Story }) {
  return (
    <div className="stack">
      <header>
        <h2>{story.suggested_story_key ?? story.story_id}</h2>
        <p className="muted">
          {humanize(story.event_type)}{story.location ? ` - ${story.location}` : ''} - topic {story.topic} - impact {story.impact.toFixed(2)}
          {story.n_papers > 1 && <> - <em>{story.bias_label}</em></>}
        </p>
        <p className="summary">{story.summary.replace(/^single_source:\s*/, '')}</p>
      </header>
      <Relationships runId={runId} storyId={story.story_id} />
      <div className="columns" style={{ gridTemplateColumns: `repeat(${Math.min(story.coverage.length, 3)}, minmax(0, 1fr))` }}>
        {story.coverage.map((c) => <PaperColumn key={c.event_id} runId={runId} cov={c} />)}
      </div>
    </div>
  );
}
