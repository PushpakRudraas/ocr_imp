import type { MatchComponents, Story } from '../types';

const LABELS: [keyof MatchComponents, string][] = [
  ['semantic', 'Meaning'], ['entity', 'Names'], ['location', 'Place'], ['keyword', 'Keywords'], ['date', 'Date'],
];
const pct = (v: number) => `${Math.round(v * 100)}%`;

/** The signals behind one match, as small chips: a strong signal is plain, a weak one is highlighted so a reviewer sees what the
 *  decision rested on. "Place" is shown as "not stated" when one article names no place (it then counts for nothing either way). */
export function Signals({ c }: { c: MatchComponents }) {
  return (
    <span className="match-signals">
      {LABELS.map(([k, label]) => {
        const v = c[k];
        return (
          <span key={k} className={`chip${v !== null && v < 0.35 ? ' warn' : ''}`} title={`${label} agreement`}>
            {label} {v === null ? 'not stated' : pct(v)}
          </span>
        );
      })}
    </span>
  );
}

/** "Why these articles were grouped": per paper, which article it matched, how strongly and on what evidence; plus borderline pairs
 *  that were left apart for a person to check. */
export default function MatchReasons({ story }: { story: Story }) {
  const rows = story.coverage.filter((c) => c.match);
  const possible = story.possible_matches ?? [];
  if (rows.length === 0 && possible.length === 0) return null;
  return (
    <section className="card" aria-label="Why these articles were grouped">
      <h3>Why these articles were grouped</h3>
      {rows.length > 0 && (
        <ul className="stack" style={{ listStyle: 'none', padding: 0 }}>
          {rows.map((c) => (
            <li key={c.event_id}>
              <strong>{c.paper}</strong> matched {c.match!.matched_with ? <strong>{c.match!.matched_with}</strong> : 'another article'}{' '}
              <span className="chip">{pct(c.match!.score)} match</span>
              {c.match!.matched_headline && <div className="muted small">&ldquo;{c.match!.matched_headline}&rdquo;</div>}
              {c.match!.components && <div><Signals c={c.match!.components} /></div>}
              {c.match!.veto && <div className="notice warn small">Held back by a rule: {c.match!.veto}</div>}
            </li>
          ))}
        </ul>
      )}
      {possible.length > 0 && (
        <details open={rows.length === 0}>
          <summary>Possibly the same story - please check ({possible.length})</summary>
          <ul className="stack" style={{ listStyle: 'none', padding: 0 }}>
            {possible.map((p) => (
              <li key={`${p.for_event_id}-${p.event_id}`}>
                <strong>{p.paper}</strong>: &ldquo;{p.headline}&rdquo; <span className="chip warn">{pct(p.score)} - not merged</span>
                {p.components && <div><Signals c={p.components} /></div>}
                {p.veto && <div className="muted small">Kept apart by a rule: {p.veto}</div>}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
