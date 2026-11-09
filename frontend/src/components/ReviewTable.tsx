import { humanize } from '../lib/guess';
import type { ReviewItem } from '../types';

export default function ReviewTable({ items }: { items: ReviewItem[] }) {
  if (items.length === 0) return <p className="empty">Nothing needs review. Every article passed the quality checks.</p>;
  return (
    <table className="table">
      <thead><tr><th>Paper</th><th>Page</th><th>Headline</th><th>Why</th><th>OCR</th></tr></thead>
      <tbody>
        {items.map((i) => (
          <tr key={i.event_id}>
            <td>{i.paper}</td>
            <td>{i.page_no}</td>
            <td>{i.headline || <em className="muted">(no headline)</em>}</td>
            <td>{i.reasons.map((r) => <span key={r} className="chip warn">{humanize(r)}</span>)}</td>
            <td>{i.ocr_confidence == null ? '-' : `${Math.round(i.ocr_confidence * 100)}%`}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
