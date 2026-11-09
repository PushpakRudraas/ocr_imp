import type { Metrics } from '../types';

export default function MetricsBar({ metrics: m }: { metrics: Metrics }) {
  const tiles: [string, string | number][] = [
    ['Pages', m.pages],
    ['Articles', m.articles],
    ['Stories', m.stories],
    ['In 2+ papers', m.multi_paper_stories],
    ['OCR confidence', m.mean_ocr_confidence == null ? '-' : `${Math.round(m.mean_ocr_confidence * 100)}%`],
    ['Translated', m.translated],
  ];
  return (
    <section className="metrics" aria-label="Run summary">
      {tiles.map(([label, value]) => (
        <div key={label} className="tile"><span className="tile-value">{value}</span><span className="muted small">{label}</span></div>
      ))}
    </section>
  );
}
