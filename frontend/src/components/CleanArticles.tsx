import { humanize } from '../lib/guess';
import type { CleanArticle } from '../types';

/** Phase-1 result: what OCR, cleaning and translation produced and stored, before any enrichment. */
export default function CleanArticles({ items }: { items: CleanArticle[] }) {
  if (items.length === 0) return <p className="empty">No articles were extracted from these PDFs.</p>;
  return (
    <table className="table" aria-label="Cleaned articles">
      <thead><tr><th>Paper</th><th>Page</th><th>Article</th><th>Language</th><th>Translation</th><th>OCR</th></tr></thead>
      <tbody>
        {items.map((a) => (
          <tr key={a.event_id}>
            <td>{a.paper}</td>
            <td>{a.page_no ?? '-'}</td>
            <td>
              <strong>{a.headline || <em className="muted">(no headline)</em>}</strong>
              {a.translated_headline && <div className="muted small">{a.translated_headline}</div>}
              {a.excerpt && <div className="muted small excerpt">{a.excerpt}</div>}
              {a.exact_duplicate_of && <span className="chip warn">duplicate</span>}
            </td>
            <td>{a.language ?? '-'}</td>
            <td>{a.translation_status ? <span className={`chip${/fail|partial/.test(a.translation_status) ? ' warn' : ''}`}>{humanize(a.translation_status)}</span> : '-'}</td>
            <td>{a.ocr_confidence == null ? '-' : `${Math.round(a.ocr_confidence * 100)}%`}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
