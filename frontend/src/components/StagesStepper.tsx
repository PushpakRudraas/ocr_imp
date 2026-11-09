const LABELS: Record<string, string> = {
  ingest: 'Read PDFs', ocr: 'OCR + layout', clean: 'Clean + translate', cluster: 'Match stories', enrich: 'Enrich', graph: 'Relationship graph', export: 'Export',
};

export default function StagesStepper({ stages, current, status }: { stages: string[]; current: string | null; status: string }) {
  const idx = status === 'done' ? stages.length : current ? stages.indexOf(current) : -1;
  return (
    <ol className="stepper" aria-label="Pipeline progress">
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
