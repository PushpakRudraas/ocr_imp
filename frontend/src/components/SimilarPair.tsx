import { useState } from 'react';
import { articleDate, hasContext, percent, similarContext, type Pair, type SharedEntity } from '../lib/similar';
import { humanize } from '../lib/guess';
import type { Article } from '../types';
import { Signals } from './MatchReasons';

type Variant = 'cleaned' | 'original' | 'translated';

const DATE_HINT = 'The date entered when this newspaper was uploaded';

function ArticleBox({ doc, role, runDate }: { doc: Article; role: string; runDate?: string }) {
  const n = doc.newspaper;
  const date = articleDate(doc, runDate);
  const variants: [Variant, string, string][] = [['cleaned', 'Cleaned text', n.text || '']];
  if (n.original_text && n.original_text !== n.text) variants.push(['original', 'Original OCR text', n.original_text]);
  if (n.translated_text) variants.push(['translated', 'English translation', n.translated_text]);
  const [variant, setVariant] = useState<Variant>('cleaned');
  const shown = variants.find((v) => v[0] === variant) ?? variants[0];
  const [copied, setCopied] = useState(false);
  const copy = () => {
    try { void navigator.clipboard?.writeText(doc.event_id).then(() => setCopied(true)); } catch { /* clipboard not available */ }
  };
  return (
    <article className="sim-article" aria-label={`${role}: ${n.paper}`}>
      <header>
        <span className="muted small">{role}</span>
        <h4 style={{ margin: '.1rem 0' }}>{n.paper}</h4>
        <dl className="sim-meta">
          <div><dt>Newspaper</dt><dd>{n.paper}</dd></div>
          <div><dt>Date</dt><dd title={DATE_HINT}>{date ?? 'not recorded'}</dd></div>
          {n.page_no != null && <div><dt>Page</dt><dd>{n.page_no}</dd></div>}
          {n.section && <div><dt>Section</dt><dd>{n.section}</dd></div>}
          {n.language && <div><dt>Language</dt><dd>{n.language}</dd></div>}
          {n.byline && <div><dt>By</dt><dd>{n.byline}</dd></div>}
        </dl>
        <p className="small">
          Event ID: <code className="event-id">{doc.event_id}</code>{' '}
          <button type="button" className="link-btn" onClick={copy}>{copied ? 'Copied' : 'Copy'}</button>
        </p>
      </header>
      <h4 className="sim-heading">{n.headline || <em className="muted">(no heading)</em>}</h4>
      {n.translated_headline && n.translated_headline !== n.headline && <p className="muted small">English: {n.translated_headline}</p>}
      {variants.length > 1 && (
        <div role="group" aria-label="Text version" className="small">
          {variants.map(([v, label]) => (
            <button key={v} type="button" className={variant === v ? 'chip active' : 'chip'} aria-pressed={variant === v} onClick={() => setVariant(v)}>{label}</button>
          ))}
        </div>
      )}
      <div className="sim-text" aria-label="Article text" tabIndex={0}>
        {shown[2] ? shown[2].split(/\n\s*\n|\n/).filter(Boolean).map((p, i) => <p key={i}>{p}</p>) : <p className="muted">(no text was read for this article)</p>}
      </div>
    </article>
  );
}

/** Chips with a real space between them (so the text reads and copies correctly, not as one run-on word). */
function Chips({ items, cls = 'chip' }: { items: { key: string; label: string; title?: string }[]; cls?: string }) {
  return <>{items.map((i) => <span key={i.key}><span className={cls} title={i.title}>{i.label}</span>{' '}</span>)}</>;
}

const TYPE_ORDER = ['PERSON', 'ORG', 'GPE'];
function groupEntities(list: SharedEntity[]): [string, SharedEntity[]][] {
  const by = new Map<string, SharedEntity[]>();
  list.forEach((e) => by.set(e.type, [...(by.get(e.type) ?? []), e]));
  return [...by.entries()].sort((x, y) => (TYPE_ORDER.indexOf(x[0]) + 1 || 9) - (TYPE_ORDER.indexOf(y[0]) + 1 || 9));
}
const TYPE_LABEL: Record<string, string> = { PERSON: 'People', ORG: 'Organisations', GPE: 'Places' };

/** Two articles side by side with the engine's similarity score and the context they share (computed from the stored articles). */
export default function SimilarPair({ pair, a, b, runDate }: { pair: Pair; a: Article; b: Article; runDate?: string }) {
  const ctx = similarContext(a, b);
  const e = pair.edge;
  const da = articleDate(a, runDate), db = articleDate(b, runDate);
  return (
    <div className="sim-pair stack" role="group" aria-label={`Similar articles ${a.newspaper.paper} and ${b.newspaper.paper}`}>
      <p className="sim-compare" aria-label="Newspapers compared">
        <strong>{a.newspaper.paper}</strong>{da && <span className="muted" title={DATE_HINT}> ({da})</span>}
        <span className="muted"> compared with </span>
        <strong>{b.newspaper.paper}</strong>{db && <span className="muted" title={DATE_HINT}> ({db})</span>}
      </p>
      <section className="sim-verdict" aria-label="Similarity">
        <span className="sim-pct" aria-label={`Similarity ${percent(pair.score)}`}>{percent(pair.score)}</span>
        <div>
          <strong>Similarity score</strong>{' '}
          {pair.kind === 'matched' ? <span className="chip done">merged into one story</span> : <span className="chip warn">possible match - not merged, needs review</span>}
          <p className="muted small">The matching engine's score from 0 to 100%. It is a ranking score, not a calibrated probability.</p>
          {e.components && <Signals c={e.components} />}
          {e.veto && <div className="notice warn small">Held back by a rule: {e.veto}</div>}
          {e.reasons && e.reasons.length > 0 && <p className="muted small">Engine notes: {e.reasons.join(' - ')}</p>}
        </div>
      </section>

      <div className="sim-cols">
        <ArticleBox doc={a} role="Article A" runDate={runDate} />
        <ArticleBox doc={b} role="Article B" runDate={runDate} />
      </div>

      <section className="sim-context card" aria-label="Similar context">
        <h3>Similar context</h3>
        <p className="muted small">Worked out from the two stored articles: what they have in common.</p>
        {!hasContext(ctx) && <p className="muted">No shared names, phrases, figures or wording were found in the stored text of these two articles.</p>}
        {ctx.eventType && (ctx.eventType.same
          ? <p>Same event type: <strong>{humanize(ctx.eventType.a)}</strong></p>
          : <p className="muted">Event type: {humanize(ctx.eventType.a)} (A) and {humanize(ctx.eventType.b)} (B)</p>)}
        {ctx.place.same && <p>Same place: <strong>{ctx.place.a}</strong></p>}
        {!ctx.place.same && ctx.place.a && ctx.place.b && <p className="muted">Main place: {ctx.place.a} (A) and {ctx.place.b} (B)</p>}
        {groupEntities(ctx.entities).map(([type, list]) => (
          <p key={type}><span className="muted">Shared {TYPE_LABEL[type]?.toLowerCase() ?? humanize(type).toLowerCase()}:</span>{' '}
            <Chips cls="chip rel" items={list.map((x) => ({ key: x.text, label: x.text, title: `mentioned ${x.mentionsA}x in A and ${x.mentionsB}x in B` }))} /></p>
        ))}
        {ctx.keyphrases.length > 0 && <p><span className="muted">Shared key phrases:</span> <Chips items={ctx.keyphrases.map((k) => ({ key: k, label: k }))} /></p>}
        {ctx.numbers.length > 0 && <p><span className="muted">Figures in both:</span> <Chips items={ctx.numbers.map((k) => ({ key: k, label: k }))} /></p>}
        {ctx.words.length > 0 && <p><span className="muted">Words used in both:</span> <Chips items={ctx.words.map((k) => ({ key: k, label: k }))} /></p>}
        {ctx.passages.length > 0 && (
          <div>
            <h4>Closest passages</h4>
            {ctx.passages.map((p, i) => (
              <div key={i} className="sim-passage">
                <blockquote>{p.a}</blockquote>
                <blockquote>{p.b}</blockquote>
                <p className="muted small">{p.shared.length} shared words: {p.shared.join(', ')}</p>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
