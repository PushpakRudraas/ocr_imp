import type { Run } from '../types';
import StagesStepper from './StagesStepper';

interface Props {
  run: Run;
  stages: string[];
  bus: 'kafka' | 'inline';
  starting: boolean;
  startError: string | null;
  onStart: () => void;
}

/** Second phase of a run: enrichment. Shows its own progress, and lets the user switch it on (or retry it). */
export default function EnrichmentPanel({ run, stages, bus, starting, startError, onStart }: Props) {
  const e = run.enrichment;
  const where = bus === 'kafka' ? 'on Kafka for the enrichment worker' : 'in this server';
  return (
    <section className="card" aria-label="Enrichment">
      <div className="run-head">
        <div>
          <h2>Enrichment</h2>
          <p className="muted small">Story matching across papers, entities, claims and sentiment. Reads the cleaned articles back from MongoDB and writes the enriched events there.</p>
        </div>
        <span className={`chip ${e.state === 'off' ? '' : e.state}`}>{e.state === 'off' ? 'not run' : e.state}</span>
      </div>

      {e.state !== 'off' && (
        <StagesStepper stages={['queued', ...stages]} current={e.stage} status={e.state === 'failed' ? 'failed' : e.state === 'done' ? 'done' : 'running'}
                       label="Enrichment progress" />
      )}
      {e.state === 'queued' && <p className="muted" role="status">Queued {where}. This page updates when it starts.</p>}
      {e.state === 'running' && <p className="muted" role="status">Enriching - this can take a while with the NLP models. This page updates automatically.</p>}
      {e.state === 'failed' && <p className="notice error" role="alert">Enrichment failed: {e.error}</p>}

      {(e.state === 'off' || e.state === 'failed') && (
        <div className="form-footer">
          <span className="muted small">{e.state === 'off' ? `Enrichment is switched off for this run. Starting it queues a job ${where}.` : 'The cleaned articles are safe; you can retry.'}</span>
          <button type="button" className="primary" disabled={starting} onClick={onStart}>
            {starting ? 'Starting...' : e.state === 'failed' ? 'Retry enrichment' : 'Run enrichment'}
          </button>
        </div>
      )}
      {startError && <p className="notice error" role="alert">{startError}</p>}
    </section>
  );
}
