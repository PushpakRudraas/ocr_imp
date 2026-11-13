import { useMemo, useState } from 'react';
import { markOcr } from '../api';
import { humanize } from '../lib/guess';
import type { OcrArticle } from '../types';

type Show = 'all' | 'news' | 'ad' | 'uncertain';

/** Every raw OCR result in reading order, each labelled news / ad / uncertain by the ad detector. Ads stay here (raw_events) but are
 *  not pushed to cleaning or enrichment; a reviewer can overrule a label. */
export default function OcrResults({ runId, items, locked }: { runId: string; items: OcrArticle[]; locked?: boolean }) {
  const [show, setShow] = useState<Show>('all');
  const [maxConf, setMaxConf] = useState(100);
  const [open, setOpen] = useState<string | null>(null);
  const [marks, setMarks] = useState<Record<string, 'ad' | 'news'>>({});
  const [err, setErr] = useState<string | null>(null);

  const label = (a: OcrArticle): 'news' | 'ad' | 'uncertain' => marks[a.article_id] ?? a.ad_label ?? 'news';
  const counts = useMemo(() => {
    const c = { news: 0, ad: 0, uncertain: 0 };
    items.forEach((a) => { c[label(a)] += 1; });
    return c;
  }, [items, marks]);                                                 // eslint-disable-line react-hooks/exhaustive-deps
  const shown = useMemo(
    () => items.map((a, i) => ({ a, n: i + 1 })).filter(({ a }) =>
      (show === 'all' || label(a) === show) && Math.round((a.ocr_confidence ?? 0) * 100) <= maxConf),
    [items, show, maxConf, marks],                                    // eslint-disable-line react-hooks/exhaustive-deps
  );

  async function mark(a: OcrArticle, l: 'ad' | 'news') {
    setErr(null);
    try { await markOcr(runId, a.article_id, l); setMarks((m) => ({ ...m, [a.article_id]: l })); }
    catch (e) { setErr(e instanceof Error ? e.message : 'Could not save the mark.'); }
  }

  if (items.length === 0) return <p className="empty">OCR found nothing in these PDFs.</p>;
  return (
    <div className="stack">
      <div className="filters">
        <span role="group" aria-label="Show">
          {(['all', 'news', 'ad', 'uncertain'] as Show[]).map((s) => (
            <button key={s} type="button" className={show === s ? 'chip active' : 'chip'} aria-pressed={show === s} onClick={() => setShow(s)}>
              {s === 'all' ? `All (${items.length})` : s === 'ad' ? `Ads (${counts.ad})` : s === 'news' ? `News (${counts.news})` : `Uncertain (${counts.uncertain})`}
            </button>
          ))}
        </span>
        {' '}
        <label>OCR confidence up to{' '}
          <select value={maxConf} onChange={(e) => setMaxConf(Number(e.target.value))}>
            {[100, 99, 95, 90, 80, 70].map((v) => <option key={v} value={v}>{v}%</option>)}
          </select>
        </label>
        {' '}
        <span className="muted small">{shown.length} shown</span>
      </div>
      {err && <p className="notice warn" role="alert">{err}</p>}
      <table className="table" aria-label="All OCR results">
        <thead><tr><th>#</th><th>Paper</th><th>Page</th><th>Text</th><th>Label</th><th>Cleaning</th><th>OCR</th></tr></thead>
        <tbody>
          {shown.map(({ a, n }) => {
            const l = label(a);
            return (
              <tr key={a.article_id} onClick={() => setOpen(open === a.article_id ? null : a.article_id)} style={{ cursor: 'pointer', opacity: l === 'ad' ? 0.6 : 1 }}>
                <td>{n}</td>
                <td>{a.paper}</td>
                <td>{a.page_no ?? '-'}</td>
                <td>
                  <strong>{a.headline || <em className="muted">(no headline)</em>}</strong>
                  <div className="muted small excerpt" style={open === a.article_id ? { whiteSpace: 'pre-wrap' } : undefined}>
                    {open === a.article_id ? a.text : a.text.slice(0, 200)}
                  </div>
                  {a.quality_issues.map((q) => <span key={q} className="chip warn">{humanize(q)}</span>)}
                </td>
                <td onClick={(e) => e.stopPropagation()}>
                  <span className={`chip${l === 'news' ? '' : ' warn'}`}>{l === 'ad' ? 'Ad' : l === 'uncertain' ? 'Uncertain' : 'News'}</span>
                  {marks[a.article_id] || a.marked ? <div className="muted small">set by reviewer</div> : a.ad_reason && a.ad_reason !== 'ok' ? <div className="muted small">{humanize(a.ad_reason.split(',')[0])}</div> : null}
                  {!locked && (
                    l === 'ad'
                      ? <button type="button" className="link small" onClick={() => mark(a, 'news')}>Not an ad</button>
                      : <button type="button" className="link small" onClick={() => mark(a, 'ad')}>Mark as ad</button>
                  )}
                </td>
                <td>{a.kept ? <span className="chip">kept</span> : <span className="chip warn">dropped: {humanize(a.removed_reason ?? 'filtered')}</span>}</td>
                <td>{a.ocr_confidence == null ? '-' : `${Math.round(a.ocr_confidence * 100)}%`}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
