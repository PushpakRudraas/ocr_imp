import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '../api';
import { article, caps, cleanArticle, enrichment, run, solo, story, storyGraph } from '../test/fixtures';
import RunPage from './RunPage';

vi.mock('../api', async (orig) => ({ ...(await orig<typeof import('../api')>()), getRun: vi.fn(), getClean: vi.fn(), enrichRun: vi.fn(), getConfig: vi.fn(), getStories: vi.fn(), getReview: vi.fn(), getArticle: vi.fn(), getStoryGraph: vi.fn() }));
const m = vi.mocked(api);

const setup = () => render(
  <MemoryRouter initialEntries={['/runs/abcdefabcdef']}>
    <Routes><Route path="/runs/:runId" element={<RunPage pollMs={5} />} /></Routes>
  </MemoryRouter>);

beforeEach(() => {
  vi.clearAllMocks();
  m.getConfig.mockResolvedValue(caps);
  m.getClean.mockResolvedValue([cleanArticle(), cleanArticle({ event_id: 'd'.repeat(16), paper: 'Dainik Jagran', headline: 'पंजाब विधायक', translated_headline: 'Punjab MLA (en)', language: 'hi', translation_status: 'translated' })]);
  m.getReview.mockResolvedValue([]);
  m.getStories.mockResolvedValue([story, solo]);
  m.getArticle.mockResolvedValue(article());
  m.getStoryGraph.mockRejectedValue(new api.ApiError('graph not enabled', 404));      // default: no graph backend
});

describe('RunPage', () => {
  it('polls until done, advancing the stepper, then shows results', async () => {
    m.getRun.mockResolvedValueOnce(run({ status: 'queued', stage: null, metrics: null }))
      .mockResolvedValueOnce(run({ status: 'running', stage: 'ocr', metrics: null }))
      .mockResolvedValueOnce(run({ status: 'running', stage: 'store', metrics: null }))
      .mockResolvedValue(run());
    setup();
    expect(await screen.findByText(/Queued on Kafka - waiting for the extraction worker/)).toBeInTheDocument();
    expect(await screen.findByText(/Processing/)).toBeInTheDocument();
    expect(screen.getByText('OCR + layout').closest('li')).toHaveAttribute('aria-current', 'step');
    expect(await screen.findByText(/waiting for the data-sync service/)).toBeInTheDocument();
    expect(screen.getByText('Stored in MongoDB').closest('li')).toHaveAttribute('aria-current', 'step');
    expect(screen.getByRole('list', { name: 'Extraction progress' })).toHaveTextContent('Queued (Kafka)');
    expect(screen.getByRole('list', { name: 'Extraction progress' })).toHaveTextContent('Stored in MongoDB');
    expect(await screen.findByRole('tab', { name: /Stories \(2\)/ })).toBeInTheDocument();
    expect(screen.getByText('In 2+ papers').previousSibling).toHaveTextContent('1');
    const calls = m.getRun.mock.calls.length;
    await new Promise((r) => setTimeout(r, 30));
    expect(m.getRun.mock.calls.length).toBe(calls);                         // polling stopped
  });

  it('shows the failure reason and stops polling', async () => {
    m.getRun.mockResolvedValue(run({ status: 'failed', stage: 'ocr', metrics: null, error: 'RuntimeError: model exploded' }));
    setup();
    expect(await screen.findByText(/model exploded/)).toBeInTheDocument();
    expect(m.getStories).not.toHaveBeenCalled();
  });

  it('shows an error for an unknown run', async () => {
    m.getRun.mockRejectedValue(new api.ApiError('unknown run', 404));
    setup();
    expect(await screen.findByRole('alert')).toHaveTextContent('unknown run');
  });

  it('lists multi-paper stories first, hides single-paper ones until toggled, and searches', async () => {
    m.getRun.mockResolvedValue(run());
    const u = userEvent.setup();
    setup();
    const list = await screen.findByRole('list', { name: 'Stories' });
    expect(list.querySelectorAll('li')).toHaveLength(1);
    await u.click(screen.getByLabelText(/Only stories in 2\+ papers/));
    expect(list.querySelectorAll('li')).toHaveLength(2);
    await u.type(screen.getByLabelText('Search stories'), 'metro');
    expect(list.querySelectorAll('li')).toHaveLength(1);
    expect(screen.getAllByText('Metro line extension gets nod').length).toBeGreaterThan(0);
    await u.clear(screen.getByLabelText('Search stories')); await u.type(screen.getByLabelText('Search stories'), 'zzz');
    expect(screen.getByText('No stories match.')).toBeInTheDocument();
  });

  it('compares papers side by side and toggles original / English text', async () => {
    m.getRun.mockResolvedValue(run());
    m.getArticle.mockImplementation(async (_r, id) => id.startsWith('b')
      ? article({ paper: 'Dainik Jagran', text: 'मूल पाठ', translated_text: 'English text', translated_headline: 'x' })
      : article());
    const u = userEvent.setup();
    setup();
    expect(await screen.findByRole('heading', { name: 'punjab-assembly-resignation-2026-11' })).toBeInTheDocument();
    const hindu = screen.getByRole('article', { name: 'The Hindu' });
    const jag = screen.getByRole('article', { name: 'Dainik Jagran' });
    expect(hindu).toHaveTextContent('page 1 + 3'); expect(hindu).toHaveTextContent('OCR 98%'); expect(hindu).toHaveTextContent('By Staff Reporter');
    expect(await screen.findByText('English text')).toBeInTheDocument();
    expect(jag).toHaveTextContent('Punjab MLA resigns (en)');
    await u.click(screen.getByRole('button', { name: 'Show original text' }));
    expect(screen.getByText('मूल पाठ')).toBeInTheDocument();
    expect(jag).toHaveTextContent('पंजाब विधायक ने दिया इस्तीफा');
    expect(hindu).not.toHaveTextContent('Show original text');            // English paper has no toggle
    await waitFor(() => expect(m.getArticle).toHaveBeenCalledWith('abcdefabcdef', 'a'.repeat(16)));
  });

  it('shows claims with verification status and the enrichment JSON on demand', async () => {
    m.getRun.mockResolvedValue(run());
    const u = userEvent.setup();
    setup();
    const hindu = await screen.findByRole('article', { name: 'The Hindu' });
    await screen.findAllByText(/Claims \(1\)/);
    expect(hindu).toHaveTextContent('corroborated'); expect(hindu).toHaveTextContent('also in The Hindu');
    await u.click(screen.getAllByRole('button', { name: /Show enrichment JSON/ })[0]);
    expect(hindu.querySelector('pre')?.textContent).toContain('"Politics.Resignation"');
    expect(hindu.querySelector('pre')?.textContent).not.toContain('Original para one');   // raw text excluded from JSON dump
  });

  it('review tab lists flagged articles or an all-clear message', async () => {
    m.getRun.mockResolvedValue(run());
    m.getReview.mockResolvedValue([{ event_id: 'e', paper: 'Dainik Jagran', page_no: 3, headline: '', reasons: ['garbled_text', 'broken_continuation'], match_candidates: [], ocr_confidence: 0.62 }]);
    const u = userEvent.setup();
    setup();
    await u.click(await screen.findByRole('tab', { name: /Needs review \(1\)/ }));
    expect(screen.getByText('garbled text')).toBeInTheDocument(); expect(screen.getByText('62%')).toBeInTheDocument();
    expect(screen.getByText('(no headline)')).toBeInTheDocument();
  });

  it('warns when a scanned paper was skipped and shows the all-clear review state', async () => {
    m.getRun.mockResolvedValue(run({ metrics: { ...run().metrics!, papers_skipped_no_ocr: ['toi'] } }));
    const u = userEvent.setup();
    setup();
    expect(await screen.findByText(/Skipped \(scanned PDF.*toi/)).toBeInTheDocument();
    await u.click(screen.getByRole('tab', { name: /Needs review \(0\)/ }));
    expect(screen.getByText(/Nothing needs review/)).toBeInTheDocument();
  });

  it('shows graph relationships for the selected story when the graph backend is on', async () => {
    m.getRun.mockResolvedValue(run());
    m.getStoryGraph.mockResolvedValue(storyGraph);
    setup();
    const panel = await screen.findByRole('region', { name: 'Relationships' });
    expect(panel).toHaveTextContent('Kunwar Vijay Pratap Singh'); expect(panel).toHaveTextContent('resigned from');
    expect(panel).toHaveTextContent('reported by The Hindu, The Times of India (2026-11-02)'); expect(panel).toHaveTextContent('x4');
    expect(m.getStoryGraph).toHaveBeenCalledWith('abcdefabcdef', 's1');
  });

  it('hides relationships without a graph backend and reports other graph errors', async () => {
    m.getRun.mockResolvedValue(run());
    setup();
    await screen.findByRole('heading', { name: 'punjab-assembly-resignation-2026-11' });
    await waitFor(() => expect(m.getStoryGraph).toHaveBeenCalled());
    expect(screen.queryByRole('region', { name: 'Relationships' })).toBeNull();
    expect(screen.queryByText(/Relationship graph unavailable/)).toBeNull();
    m.getStoryGraph.mockRejectedValue(new api.ApiError('graph database error: ServiceUnavailable', 502));
    const u = userEvent.setup();
    await u.click(screen.getByLabelText(/Only stories in 2\+ papers/));
    await u.click(screen.getAllByRole('button', { name: /Metro line extension/ })[0]);
    expect(await screen.findByText(/Relationship graph unavailable: graph database error/)).toBeInTheDocument();
  });

  describe('two-phase flow', () => {
    const extractedOnly = (over = {}) => run({ enrich_after: false, enrichment: enrichment({ state: 'off', stage: null, started_at: null, finished_at: null, queued_at: null }),
      metrics: { ...run().metrics!, stories: undefined, multi_paper_stories: undefined }, ...over });

    it('stops after extraction when enrichment is off: shows the stored articles, no stories, and an enable button', async () => {
      m.getRun.mockResolvedValue(extractedOnly());
      setup();
      expect(await screen.findByRole('table', { name: 'Cleaned articles' })).toHaveTextContent('Punjab MLA (en)');
      expect(screen.getByRole('tab', { name: /Articles \(2\)/ })).toHaveAttribute('aria-selected', 'true');
      expect(screen.queryByRole('tab', { name: /Stories/ })).toBeNull();
      expect(screen.queryByText('In 2+ papers')).toBeNull();                       // story numbers do not exist yet
      expect(screen.getByRole('region', { name: 'Enrichment' })).toHaveTextContent('not run');
      expect(screen.getByRole('list', { name: 'Extraction progress' }).querySelectorAll('li.done')).toHaveLength(5);
      expect(screen.queryByRole('list', { name: 'Enrichment progress' })).toBeNull();
      expect(m.getStories).not.toHaveBeenCalled();
      expect(m.getRun.mock.calls.length).toBe(1);                                  // nothing left to poll
    });

    it('"Run enrichment" queues the job, follows it through the stages and reveals stories when it finishes', async () => {
      m.getRun.mockResolvedValueOnce(extractedOnly())
        .mockResolvedValueOnce(extractedOnly({ enrichment: enrichment({ state: 'queued', stage: 'queued' }) }))
        .mockResolvedValueOnce(extractedOnly({ enrichment: enrichment({ state: 'running', stage: 'enrich' }) }))
        .mockResolvedValue(run());
      m.enrichRun.mockResolvedValue(extractedOnly({ enrichment: enrichment({ state: 'queued', stage: 'queued' }) }));
      const u = userEvent.setup();
      setup();
      await u.click(await screen.findByRole('button', { name: 'Run enrichment' }));
      expect(m.enrichRun).toHaveBeenCalledWith('abcdefabcdef');
      expect(await screen.findByText(/Queued on Kafka for the enrichment worker/)).toBeInTheDocument();
      expect(await screen.findByText(/Enriching - this can take a while/)).toBeInTheDocument();
      const prog = within(screen.getByRole('list', { name: 'Enrichment progress' }));
      expect(prog.getByText('Enrich').closest('li')).toHaveAttribute('aria-current', 'step');
      expect(prog.getByText('Queued (Kafka)').closest('li')).toHaveClass('done');
      expect(await screen.findByRole('tab', { name: /Stories \(2\)/ })).toBeInTheDocument();
      expect(screen.getByRole('tab', { name: /Needs review \(0\)/ })).toBeInTheDocument();
      expect(await screen.findByRole('list', { name: 'Stories' })).toBeInTheDocument();           // switches to stories once ready
      expect(screen.getByText('In 2+ papers')).toBeInTheDocument();
    });

    it('keeps polling while enrichment is queued after extraction (started from the upload toggle)', async () => {
      m.getRun.mockResolvedValueOnce(run({ enrichment: enrichment({ state: 'queued', stage: 'queued' }), metrics: extractedOnly().metrics }))
        .mockResolvedValue(run());
      setup();
      expect(await screen.findByText(/Queued on Kafka/)).toBeInTheDocument();
      expect(await screen.findByRole('tab', { name: /Stories \(2\)/ })).toBeInTheDocument();
      expect(m.getClean).toHaveBeenCalledTimes(1);                                  // articles are not refetched on every poll
    });

    it('shows an enrichment failure, keeps the articles, and offers a retry', async () => {
      m.getRun.mockResolvedValue(extractedOnly({ enrichment: enrichment({ state: 'failed', stage: 'enrich', error: 'RuntimeError: model exploded' }) }));
      m.enrichRun.mockResolvedValue(extractedOnly({ enrichment: enrichment({ state: 'queued', stage: 'queued' }) }));
      const u = userEvent.setup();
      setup();
      expect(await screen.findByText(/Enrichment failed: RuntimeError: model exploded/)).toBeInTheDocument();
      expect(screen.getByRole('table', { name: 'Cleaned articles' })).toBeInTheDocument();
      await u.click(screen.getByRole('button', { name: 'Retry enrichment' }));
      expect(m.enrichRun).toHaveBeenCalled();
    });

    it('reports why enrichment could not be started', async () => {
      m.getRun.mockResolvedValue(extractedOnly());
      m.enrichRun.mockRejectedValue(new api.ApiError('could not queue enrichment on Kafka: broker down', 502));
      const u = userEvent.setup();
      setup();
      await u.click(await screen.findByRole('button', { name: 'Run enrichment' }));
      expect(await screen.findByText(/broker down/)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Run enrichment' })).toBeEnabled();
    });

    it('says inline when there is no Kafka', async () => {
      m.getConfig.mockResolvedValue({ ...caps, enrichment_bus: 'inline' });
      m.getRun.mockResolvedValue(extractedOnly());
      setup();
      expect(await screen.findByText(/queues a job in this server/)).toBeInTheDocument();
    });
  });
});
