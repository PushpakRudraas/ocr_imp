import { useEffect, useMemo, useState } from 'react';
import { ApiError, getArticle } from '../api';
import { matchedPairs, percent, possiblePairs, type Pair } from '../lib/similar';
import type { Article, Coverage, Story } from '../types';
import SimilarPair from './SimilarPair';

const POSSIBLE_LIMIT = 10;
const shortId = (id: string) => `${id.slice(0, 10)}...`;
const headlineOf = (c: Coverage) => c.translated_headline ?? c.headline;
const bestScore = (s: Story) => Math.max(0, ...s.coverage.map((c) => c.match?.score ?? 0));

/** Loads the full stored records of some articles (the existing article endpoint) once a card is opened. */
function useArticles(runId: string, ids: string[], enabled: boolean) {
  const [docs, setDocs] = useState<Article[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const key = ids.join('|');
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    setDocs(null); setError(null);
    Promise.all(ids.map((id) => getArticle(runId, id)))
      .then((d) => { if (live) setDocs(d); })
      .catch((e) => { if (live) setError(e instanceof ApiError ? e.message : 'Could not load the articles.'); });
    return () => { live = false; };
  }, [runId, key, enabled]);                                       // eslint-disable-line react-hooks/exhaustive-deps
  return { docs, error };
}

function GroupCard({ runId, story }: { runId: string; story: Story }) {
  const [open, setOpen] = useState(false);
  const { docs, error } = useArticles(runId, story.coverage.map((c) => c.event_id), open);
  const pairs = useMemo(() => (docs ? matchedPairs(docs) : []), [docs]);
  const byId = useMemo(() => new Map((docs ?? []).map((d) => [d.event_id, d])), [docs]);
  const best = bestScore(story);
  return (
    <li className="card sim-card">
      <button type="button" className="sim-toggle" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <span className="sim-pct small-pct">{best > 0 ? percent(best) : '-'}</span>
        <span className="sim-toggle-body">
          <strong>{story.coverage.length} similar articles in {story.n_papers} newspapers</strong>
          <span className="muted small"> - story {story.story_id}</span>
          <ul className="sim-heads">
            {story.coverage.map((c) => (
              <li key={c.event_id}><span className="chip">{c.paper}</span> {headlineOf(c)} <code className="event-id muted small">{shortId(c.event_id)}</code></li>
            ))}
          </ul>
        </span>
        <span aria-hidden="true">{open ? '-' : '+'}</span>
      </button>
      {open && (
        <div className="sim-body stack">
          {error && <p className="notice error" role="alert">{error}</p>}
          {!docs && !error && <p className="muted small">Loading the complete articles...</p>}
          {docs && pairs.length > 0 && pairs.map((p) => {
            const a = byId.get(p.a), b = byId.get(p.b);
            return a && b ? <SimilarPair key={p.key} pair={p} a={a} b={b} /> : null;
          })}
          {docs && pairs.length === 0 && (
            <>
              <p className="notice warn">This run was processed before the match details were saved, so no pair scores are available. The articles that were grouped are shown below.</p>
              <div className="sim-cols">
                {docs.map((d) => (
                  <article key={d.event_id} className="sim-article" aria-label={d.newspaper.paper}>
                    <h4>{d.newspaper.paper}</h4>
                    <p className="small">Event ID: <code className="event-id">{d.event_id}</code></p>
                    <h4 className="sim-heading">{d.newspaper.headline}</h4>
                    <div className="sim-text">{(d.newspaper.text || '').split(/\n+/).filter(Boolean).map((p, i) => <p key={i}>{p}</p>)}</div>
                  </article>
                ))}
              </div>
            </>
          )}
        </div>
      )}
    </li>
  );
}

function PossibleCard({ runId, pair, label }: { runId: string; pair: Pair; label: [string, string] }) {
  const [open, setOpen] = useState(false);
  const { docs, error } = useArticles(runId, [pair.a, pair.b], open);
  return (
    <li className="card sim-card">
      <button type="button" className="sim-toggle" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <span className="sim-pct small-pct">{percent(pair.score)}</span>
        <span className="sim-toggle-body">
          <strong>{label[0]}</strong> <span className="muted">and</span> <strong>{label[1]}</strong>
          <span className="chip warn">not merged</span>
          <span className="muted small"> <code className="event-id">{shortId(pair.a)}</code> / <code className="event-id">{shortId(pair.b)}</code></span>
        </span>
        <span aria-hidden="true">{open ? '-' : '+'}</span>
      </button>
      {open && (
        <div className="sim-body stack">
          {error && <p className="notice error" role="alert">{error}</p>}
          {!docs && !error && <p className="muted small">Loading the complete articles...</p>}
          {docs && docs.length === 2 && <SimilarPair pair={pair} a={docs[0]} b={docs[1]} />}
        </div>
      )}
    </li>
  );
}

/** The "Similar articles" tab: which articles the matching engine found to be about the same event, with their event IDs, the complete
 *  articles, the similarity score and the context they share. Reads only what the app already stores; nothing here changes any data. */
export default function SimilarArticles({ runId, stories }: { runId: string; stories: Story[] }) {
  const [query, setQuery] = useState('');
  const [paper, setPaper] = useState('');
  const [minScore, setMinScore] = useState(0);
  const [showAllPossible, setShowAllPossible] = useState(false);

  const groups = useMemo(() => stories.filter((s) => s.n_papers >= 2).sort((x, y) => bestScore(y) - bestScore(x)), [stories]);
  const possible = useMemo(() => possiblePairs(stories), [stories]);
  const labels = useMemo(() => {
    const m = new Map<string, string>();
    stories.forEach((s) => s.coverage.forEach((c) => m.set(c.event_id, `${c.paper}: ${headlineOf(c)}`)));
    stories.forEach((s) => (s.possible_matches ?? []).forEach((p) => { if (!m.has(p.event_id)) m.set(p.event_id, `${p.paper}: ${p.headline}`); }));
    return m;
  }, [stories]);
  const papers = useMemo(() => [...new Set(stories.flatMap((s) => s.coverage.map((c) => c.paper)))].sort(), [stories]);

  const q = query.trim().toLowerCase();
  const matches = (text: string) => !q || text.toLowerCase().includes(q);
  const shownGroups = groups.filter((s) =>
    bestScore(s) * 100 >= minScore && (!paper || s.coverage.some((c) => c.paper === paper)) &&
    (!q || s.coverage.some((c) => matches(`${c.headline} ${c.translated_headline ?? ''} ${c.paper} ${c.event_id}`)) || matches(s.story_id)));
  const shownPossible = possible.filter((p) =>
    p.score * 100 >= minScore && (!paper || (labels.get(p.a) ?? '').startsWith(paper) || (labels.get(p.b) ?? '').startsWith(paper)) &&
    (!q || matches(`${labels.get(p.a) ?? ''} ${labels.get(p.b) ?? ''} ${p.a} ${p.b}`)));

  return (
    <div className="stack">
      <section className="card">
        <h2>Similar articles across newspapers</h2>
        <p className="muted">
          {groups.length} group{groups.length === 1 ? '' : 's'} of articles that the matching engine found to be about the same event
          {possible.length > 0 && <>, and {possible.length} possible match{possible.length === 1 ? '' : 'es'} that need a person to decide</>}.
          Open a group to read the complete articles side by side, with their event IDs, the similarity score and the context they share.
        </p>
        <div className="filters">
          <input type="search" placeholder="Search headline, newspaper or event ID" aria-label="Search similar articles" value={query} onChange={(e) => setQuery(e.target.value)} />{' '}
          <label>Newspaper{' '}
            <select value={paper} onChange={(e) => setPaper(e.target.value)} aria-label="Filter by newspaper">
              <option value="">All</option>{papers.map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
          </label>{' '}
          <label>Similarity of at least{' '}
            <select value={minScore} onChange={(e) => setMinScore(Number(e.target.value))} aria-label="Minimum similarity">
              {[0, 60, 70, 75, 80, 90].map((v) => <option key={v} value={v}>{v === 0 ? 'any' : `${v}%`}</option>)}
            </select>
          </label>
        </div>
      </section>

      <section aria-label="Matched articles">
        <h3>Matched articles ({shownGroups.length})</h3>
        {groups.length === 0
          ? <p className="empty">No similar articles were found across the uploaded newspapers.</p>
          : shownGroups.length === 0 ? <p className="empty">Nothing matches these filters.</p>
          : <ul className="stack sim-list">{shownGroups.map((s) => <GroupCard key={s.story_id} runId={runId} story={s} />)}</ul>}
      </section>

      {possible.length > 0 && (
        <section aria-label="Possible matches">
          <h3>Possible matches - please check ({shownPossible.length})</h3>
          <p className="muted small">Close, but not close enough for the engine to merge them. Open one to compare.</p>
          <ul className="stack sim-list">
            {(showAllPossible ? shownPossible : shownPossible.slice(0, POSSIBLE_LIMIT)).map((p) => <PossibleCard key={p.key} runId={runId} pair={p} label={[labels.get(p.a) ?? shortId(p.a), labels.get(p.b) ?? shortId(p.b)]} />)}
          </ul>
          {shownPossible.length > POSSIBLE_LIMIT && (
            <button type="button" className="link-btn" onClick={() => setShowAllPossible((v) => !v)}>
              {showAllPossible ? `Show only the ${POSSIBLE_LIMIT} highest` : `Show all ${shownPossible.length}`}
            </button>
          )}
        </section>
      )}
    </div>
  );
}
