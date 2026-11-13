import { useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ApiError, enrichRun, getClean, getConfig, getOcr, getPages, getReview, getRun, getStories } from '../api';
import type { CleanArticle, OcrArticle, PageInfo, ReviewItem, Run, Story } from '../types';
import CleanArticles from './CleanArticles';
import EnrichmentPanel from './EnrichmentPanel';
import MetricsBar from './MetricsBar';
import OcrResults from './OcrResults';
import PageView from './PageView';
import ReviewTable from './ReviewTable';
import StagesStepper from './StagesStepper';
import StoriesView from './StoriesView';

const DEFAULT_STAGES = ['ingest', 'ocr', 'clean'];       // the server sends the real list (with 'queued' and 'store' when Kafka is on)
const DEFAULT_ENRICH_STAGES = ['cluster', 'enrich', 'export'];
type Tab = 'articles' | 'ocr' | 'pages' | 'stories' | 'review';

export default function RunPage({ pollMs = 1500 }: { pollMs?: number }) {
  const { runId = '' } = useParams();
  const [run, setRun] = useState<Run | null>(null);
  const [stages, setStages] = useState(DEFAULT_STAGES);
  const [enrichStages, setEnrichStages] = useState(DEFAULT_ENRICH_STAGES);
  const [bus, setBus] = useState<'kafka' | 'inline'>('inline');
  const [clean, setClean] = useState<CleanArticle[] | null>(null);
  const [ocr, setOcr] = useState<OcrArticle[] | null>(null);
  const [pageInfo, setPageInfo] = useState<PageInfo[] | null>(null);
  const [ocrError, setOcrError] = useState<string | null>(null);
  const [stories, setStories] = useState<Story[] | null>(null);
  const [review, setReview] = useState<ReviewItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [pollKey, setPollKey] = useState(0);                      // bump to restart polling (e.g. after switching enrichment on)
  const loaded = useRef({ clean: false, ocr: false, results: false });

  useEffect(() => {
    getConfig().then((c) => { setStages(c.stages); setEnrichStages(c.enrich_stages ?? DEFAULT_ENRICH_STAGES); setBus(c.enrichment_bus ?? 'inline'); }).catch(() => undefined);
  }, []);

  useEffect(() => { loaded.current = { clean: false, ocr: false, results: false }; setClean(null); setOcr(null); setPageInfo(null); setOcrError(null); setStories(null); setReview([]); setTab(null); }, [runId]);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function tick() {
      try {
        const r = await getRun(runId);
        if (cancelled) return;
        setRun(r);
        if (r.status === 'done' && !loaded.current.clean) {          // phase 1 finished: the cleaned articles are readable now
          loaded.current.clean = true;
          getClean(runId).then((c) => { if (!cancelled) setClean(c); }).catch(() => { loaded.current.clean = false; });
        }
        if (r.status === 'done' && !loaded.current.ocr) {
          loaded.current.ocr = true;
          getPages(runId).then((p) => { if (!cancelled) setPageInfo(p); }).catch(() => undefined);
          getOcr(runId).then((o) => { if (!cancelled) setOcr(o); })
            .catch((e) => { if (!cancelled) setOcrError(e instanceof ApiError ? e.message : 'Could not load the OCR results.'); });
        }
        if (r.status === 'done' && r.enrichment.state === 'done' && !loaded.current.results) {
          loaded.current.results = true;
          const [s, rv] = await Promise.all([getStories(runId), getReview(runId)]);
          if (!cancelled) { setStories(s); setReview(rv); }
        }
        const extracting = r.status === 'queued' || r.status === 'running';
        const enriching = r.status === 'done' && (r.enrichment.state === 'queued' || r.enrichment.state === 'running');
        if (extracting || enriching) timer = setTimeout(tick, pollMs);
      } catch (e) {
        loaded.current.results = false;
        if (!cancelled) setError(e instanceof ApiError ? e.message : 'Could not load this run.');
      }
    }
    tick();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [runId, pollMs, pollKey]);

  async function startEnrichment() {
    setStarting(true);
    setStartError(null);
    try {
      setRun(await enrichRun(runId));
      setPollKey((k) => k + 1);
    } catch (e) {
      setStartError(e instanceof ApiError ? e.message : 'Could not start enrichment.');
    } finally {
      setStarting(false);
    }
  }

  if (error) return <p className="notice error" role="alert">{error} <Link to="/">Back to upload</Link></p>;
  if (!run) return <p className="empty">Loading run...</p>;

  const running = run.status === 'queued' || run.status === 'running';
  const extracted = run.status === 'done';
  const enriched = extracted && run.enrichment.state === 'done' && stories !== null;
  const active: Tab = tab ?? (enriched ? 'stories' : 'articles');
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
        <StagesStepper stages={stages} current={run.stage} status={run.status} label="Extraction progress" />
        {running && (
          <p className="muted" role="status">
            {run.status === 'queued'
              ? bus === 'kafka' ? 'Queued on Kafka - waiting for the extraction worker...' : 'Waiting for the pipeline...'
              : run.stage === 'store' ? 'Articles extracted - waiting for the data-sync service to store them in MongoDB.' : 'Processing - this page updates automatically.'}
          </p>
        )}
        {run.status === 'failed' && <p className="notice error" role="alert">Run failed: {run.error}</p>}
      </section>

      {extracted && (
        <EnrichmentPanel run={run} stages={enrichStages} bus={bus} starting={starting} startError={startError} onStart={startEnrichment} />
      )}

      {extracted && run.metrics && (
        <>
          <MetricsBar metrics={run.metrics} />
          {run.metrics.papers_skipped_no_ocr.length > 0 && (
            <p className="notice warn">Skipped (scanned PDF, no OCR engine on the server): {run.metrics.papers_skipped_no_ocr.join(', ')}</p>
          )}
          <div className="tabs" role="tablist">
            <button role="tab" aria-selected={active === 'articles'} onClick={() => setTab('articles')}>Articles{clean ? ` (${clean.length})` : ''}</button>
            <button role="tab" aria-selected={active === 'ocr'} onClick={() => setTab('ocr')}>All OCR results{ocr ? ` (${ocr.length})` : ''}</button>
            {pageInfo && <button role="tab" aria-selected={active === 'pages'} onClick={() => setTab('pages')}>Page view</button>}
            {enriched && stories && (
              <>
                <button role="tab" aria-selected={active === 'stories'} onClick={() => setTab('stories')}>Stories ({stories.length})</button>
                <button role="tab" aria-selected={active === 'review'} onClick={() => setTab('review')}>Needs review ({review.length})</button>
              </>
            )}
          </div>
          {active === 'articles' && (clean ? <CleanArticles items={clean} /> : <p className="empty">Loading articles...</p>)}
          {active === 'ocr' && (ocr ? <OcrResults runId={run.id} items={ocr} locked={run.enrichment.state !== 'off' && run.enrichment.state !== 'failed'} /> : ocrError ? <p className="notice warn">{ocrError}</p> : <p className="empty">Loading OCR results...</p>)}
          {active === 'pages' && pageInfo && ocr && <PageView runId={run.id} pages={pageInfo} items={ocr} />}
          {active === 'stories' && enriched && stories && <StoriesView runId={run.id} stories={stories} />}
          {active === 'review' && enriched && <ReviewTable items={review} />}
        </>
      )}
    </div>
  );
}
