import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ApiError, getConfig, getReview, getRun, getStories } from '../api';
import type { ReviewItem, Run, Story } from '../types';
import MetricsBar from './MetricsBar';
import ReviewTable from './ReviewTable';
import StagesStepper from './StagesStepper';
import StoriesView from './StoriesView';

const DEFAULT_STAGES = ['ingest', 'ocr', 'clean', 'cluster', 'enrich', 'export'];

export default function RunPage({ pollMs = 1500 }: { pollMs?: number }) {
  const { runId = '' } = useParams();
  const [run, setRun] = useState<Run | null>(null);
  const [stages, setStages] = useState(DEFAULT_STAGES);
  const [stories, setStories] = useState<Story[] | null>(null);
  const [review, setReview] = useState<ReviewItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<'stories' | 'review'>('stories');

  useEffect(() => { getConfig().then((c) => setStages(c.stages)).catch(() => undefined); }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function tick() {
      try {
        const r = await getRun(runId);
        if (cancelled) return;
        setRun(r);
        if (r.status === 'done') {
          const [s, rv] = await Promise.all([getStories(runId), getReview(runId)]);
          if (!cancelled) { setStories(s); setReview(rv); }
        } else if (r.status !== 'failed') {
          timer = setTimeout(tick, pollMs);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof ApiError ? e.message : 'Could not load this run.');
      }
    }
    tick();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [runId, pollMs]);

  if (error) return <p className="notice error" role="alert">{error} <Link to="/">Back to upload</Link></p>;
  if (!run) return <p className="empty">Loading run...</p>;

  const running = run.status === 'queued' || run.status === 'running';
  return (
    <div className="stack">
      <section className="card">
        <div className="run-head">
          <div>
            <h1>Run for {run.run_date}</h1>
            <p className="muted">{run.papers.map((p) => `${p.name} (${p.language})`).join(' - ')}</p>
          </div>
          <span className={`chip ${run.status}`}>{run.status}</span>
        </div>
        <StagesStepper stages={stages} current={run.stage} status={run.status} />
        {running && <p className="muted" role="status">{run.status === 'queued' ? 'Waiting for the pipeline...' : 'Processing - this page updates automatically.'}</p>}
        {run.status === 'failed' && <p className="notice error" role="alert">Run failed: {run.error}</p>}
      </section>

      {run.status === 'done' && run.metrics && stories && (
        <>
          <MetricsBar metrics={run.metrics} />
          {run.metrics.papers_skipped_no_ocr.length > 0 && (
            <p className="notice warn">Skipped (scanned PDF, no OCR engine on the server): {run.metrics.papers_skipped_no_ocr.join(', ')}</p>
          )}
          <div className="tabs" role="tablist">
            <button role="tab" aria-selected={tab === 'stories'} onClick={() => setTab('stories')}>Stories ({stories.length})</button>
            <button role="tab" aria-selected={tab === 'review'} onClick={() => setTab('review')}>Needs review ({review.length})</button>
          </div>
          {tab === 'stories' ? <StoriesView runId={run.id} stories={stories} /> : <ReviewTable items={review} />}
        </>
      )}
    </div>
  );
}
