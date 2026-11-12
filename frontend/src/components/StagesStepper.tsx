const LABELS: Record<string, string> = {
  ingest: 'Read PDFs', ocr: 'OCR + layout', clean: 'Clean + translate', store: 'Stored in MongoDB',
  queued: 'Queued (Kafka)', cluster: 'Match stories', enrich: 'Enrich', graph: 'Relationship graph', export: 'Export',
};

interface Props {
  stages: string[];
  current: string | null;
  status: string;
  /** Names the list for assistive tech; two steppers (extraction / enrichment) can be on one page. */
  label?: string;
}

export default function StagesStepper({ stages, current, status, label = 'Pipeline progress' }: Props) {
  const idx = status === 'done' ? stages.length : current ? stages.indexOf(current) : -1;
  return (
    <ol className="stepper" aria-label={label}>
      {stages.map((s, i) => {
        const state = status === 'failed' && i === idx ? 'failed' : i < idx ? 'done' : i === idx ? 'active' : 'todo';
        return (
          <li key={s} className={state} aria-current={state === 'active' ? 'step' : undefined}>
            <span className="dot">{state === 'done' ? '✓' : i + 1}</span>
            <span>{LABELS[s] ?? s}</span>
          </li>
        );
      })}
    </ol>
  );
}
