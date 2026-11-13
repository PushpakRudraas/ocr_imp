import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '../api';
import type { Article, Story } from '../types';
import SimilarArticles from './SimilarArticles';

vi.mock('../api', async (orig) => ({ ...(await orig<typeof import('../api')>()), getArticle: vi.fn() }));
const m = vi.mocked(api);

const A = 'a'.repeat(64), B = 'b'.repeat(64), C = 'c'.repeat(64), D = 'd'.repeat(64);

const doc = (id: string, paper: string, headline: string, text: string, over: Partial<Article> = {}): Article => ({
  event_id: id, event: { type: 'Crime.Arrest', trigger: 'arrested', actor: null, institution: null, date: '2026-11-08', confidence: 'medium' },
  claims: [], keyphrases: ['Punjab Police'], summary: { one_line: '', multi_doc_summary: '' },
  entities: [{ text: 'Gurwinder Afridi', type: 'PERSON', mentions: 3 }, { text: 'Punjab Police', type: 'ORG', mentions: 2 }, { text: 'Ethiopia', type: 'GPE', mentions: 2 }],
  geo: { primary_location: 'Punjab' },
  newspaper: { paper, headline, translated_headline: null, text, translated_text: null, translation_status: null, page_no: 6, byline: 'Staff', language: 'en', original_text: text + ' (raw)' },
  ...over,
});

const TOI_TEXT = 'Punjab police secured the deportation of wanted gangster Gurwinder Afridi from Ethiopia on Sunday.\n\nHe faces 30 cases across the state.';
const HT_TEXT = 'The wanted gangster Gurwinder Afridi was deported from Ethiopia by Punjab police.\n\nHe faces 30 heinous cases.';
const comps = { semantic: 0.93, entity: 0.52, location: 0.8, date: 0.7, keyword: 0.31 };

const cov = (event_id: string, paper: string, headline: string, match: Story['coverage'][0]['match'] = null): Story['coverage'][0] =>
  ({ paper, paper_id: paper.toLowerCase(), page_no: 6, pages: [6], headline, translated_headline: null, byline: null, language: 'en', ocr_confidence: 0.98, event_id, sentiment: 'neutral', match });

const base = { suggested_story_key: null, event_type: 'Crime.Arrest', location: 'Punjab', topic: 'crime', impact: 0.5, summary: '', bias_label: '' };
const matchedStory: Story = { ...base, story_id: 'story-1', n_papers: 2,
  coverage: [cov(A, 'Hindustan Times', 'Gangster brought back from Ethiopia'), cov(B, 'The Times of India', 'Wanted Punjab gangster brought back', { score: 0.8, reasons: ['cos=0.80'], matched_with: 'Hindustan Times', matched_headline: 'x', components: comps })] };
const lowStory: Story = { ...base, story_id: 'story-2', n_papers: 2,
  coverage: [cov('e'.repeat(64), 'Hindustan Times', 'Hasina seeks passport'), cov('f'.repeat(64), 'The Times of India', 'Hasina extradition request', { score: 0.62, reasons: [], matched_with: 'Hindustan Times', matched_headline: 'y' })] };
const solo: Story = { ...base, story_id: 'story-3', n_papers: 1, coverage: [cov(C, 'Hindustan Times', 'Diwali spike in farm fires')],
  possible_matches: [{ event_id: D, paper: 'The Times of India', headline: 'Toll in fire rises', score: 0.58, reasons: ['cos=0.55'], components: comps, veto: null, for_event_id: C }] };

beforeEach(() => {
  vi.clearAllMocks();
  m.getArticle.mockImplementation(async (_run, id) => {
    if (id === A) return doc(A, 'Hindustan Times', 'Gangster brought back from Ethiopia', HT_TEXT);
    if (id === B) return doc(B, 'The Times of India', 'Wanted Punjab gangster brought back', TOI_TEXT, { dedup_clustering: { matches: [{ event_id: A, score: 0.8, reasons: ['cos=0.80', 'entities=3'], components: comps, veto: null }] } });
    if (id === C) return doc(C, 'Hindustan Times', 'Diwali spike in farm fires', 'Farm fires jumped.');
    if (id === D) return doc(D, 'The Times of India', 'Toll in fire rises', 'The toll rose to four.');
    return doc(id, 'X', 'Y', 'Z');
  });
});

describe('SimilarArticles', () => {
  it('lists the groups of similar articles, best score first, with event IDs and headlines, before anything is opened', () => {
    render(<SimilarArticles runId="r1" stories={[lowStory, matchedStory, solo]} />);
    expect(screen.getByText(/2 groups of articles that the matching engine found/)).toBeInTheDocument();
    const cards = screen.getAllByRole('listitem').filter((li) => li.classList.contains('sim-card'));
    expect(within(cards[0]).getByText('80%')).toBeInTheDocument();                          // 0.80 sorts before 0.62
    expect(within(cards[0]).getByText('Gangster brought back from Ethiopia')).toBeInTheDocument();
    expect(within(cards[0]).getByText(`${A.slice(0, 10)}...`)).toBeInTheDocument();
    expect(within(cards[1]).getByText('62%')).toBeInTheDocument();
    expect(m.getArticle).not.toHaveBeenCalled();                                            // nothing is fetched until a card is opened
  });

  it('opening a group shows both complete articles with their full event IDs, the score, the signals and the shared context', async () => {
    const u = userEvent.setup();
    render(<SimilarArticles runId="r1" stories={[matchedStory]} />);
    await u.click(screen.getByRole('button', { name: /2 similar articles in 2 newspapers/ }));
    const pair = await screen.findByRole('group', { name: /Similar articles/ });
    expect(m.getArticle).toHaveBeenCalledWith('r1', A);
    expect(m.getArticle).toHaveBeenCalledWith('r1', B);

    expect(within(pair).getByLabelText('Similarity 80%')).toBeInTheDocument();
    expect(within(pair).getByText(/merged into one story/)).toBeInTheDocument();
    expect(within(pair).getByText(/Meaning 93%/)).toBeInTheDocument();
    expect(within(pair).getByText(/cos=0.80 - entities=3/)).toBeInTheDocument();

    const left = within(within(pair).getByRole('article', { name: /Article A/ }));
    const right = within(within(pair).getByRole('article', { name: /Article B/ }));
    expect(left.getByText(A)).toBeInTheDocument();                                             // the full event ID
    expect(right.getByText(B)).toBeInTheDocument();
    expect(left.getByText('Gangster brought back from Ethiopia')).toBeInTheDocument();
    expect(left.getByLabelText('Article text')).toHaveTextContent('He faces 30 heinous cases.'); // the complete text, every paragraph
    expect(right.getByLabelText('Article text')).toHaveTextContent('He faces 30 cases across the state.');

    const ctx = within(within(pair).getByLabelText('Similar context'));
    expect(ctx.getByText('Gurwinder Afridi')).toBeInTheDocument();                              // shared names
    expect(ctx.getByText('Ethiopia')).toBeInTheDocument();
    expect(ctx.getByText('30')).toBeInTheDocument();                                            // a figure in both
    expect(ctx.getByText(/Same place:/)).toHaveTextContent('Punjab');
    expect(ctx.getByText('Closest passages')).toBeInTheDocument();
  });

  it('the original OCR text can be shown next to the cleaned text', async () => {
    const u = userEvent.setup();
    render(<SimilarArticles runId="r1" stories={[matchedStory]} />);
    await u.click(screen.getByRole('button', { name: /2 similar articles/ }));
    const left = within(await screen.findByRole('article', { name: /Article A/ }));
    await u.click(left.getByRole('button', { name: 'Original OCR text' }));
    expect(left.getByLabelText('Article text')).toHaveTextContent('(raw)');
  });

  it('a run processed before the match details were saved still shows the grouped articles, and says why there is no score', async () => {
    m.getArticle.mockImplementation(async (_r, id) => doc(id, id === A ? 'Hindustan Times' : 'The Times of India', `Head ${id.slice(0, 1)}`, 'Some text.'));
    const u = userEvent.setup();
    render(<SimilarArticles runId="r1" stories={[matchedStory]} />);
    await u.click(screen.getByRole('button', { name: /2 similar articles/ }));
    expect(await screen.findByText(/processed before the match details were saved/)).toBeInTheDocument();
    expect(screen.getByRole('article', { name: 'Hindustan Times' })).toHaveTextContent(A);
  });

  it('shows borderline pairs separately, marked as not merged, and opens them like a group', async () => {
    const u = userEvent.setup();
    render(<SimilarArticles runId="r1" stories={[matchedStory, solo]} />);
    const section = within(screen.getByRole('region', { name: 'Possible matches' }));
    expect(section.getByText(/Possible matches - please check \(1\)/)).toBeInTheDocument();
    expect(section.getByText('58%')).toBeInTheDocument();
    expect(section.getByText('not merged')).toBeInTheDocument();
    await u.click(section.getByRole('button', { name: /not merged/ }));
    const pair = await screen.findByRole('group', { name: /Similar articles/ });
    expect(within(pair).getByLabelText('Similarity 58%')).toBeInTheDocument();
    expect(within(pair).getByText(/possible match - not merged, needs review/)).toBeInTheDocument();
  });

  it('search and the similarity filter narrow the list; an empty run says so', async () => {
    const u = userEvent.setup();
    const { rerender } = render(<SimilarArticles runId="r1" stories={[matchedStory, lowStory]} />);
    await u.type(screen.getByLabelText('Search similar articles'), 'hasina');
    expect(screen.getByText('Matched articles (1)')).toBeInTheDocument();
    await u.clear(screen.getByLabelText('Search similar articles'));
    await u.selectOptions(screen.getByLabelText('Minimum similarity'), '75');
    expect(screen.getByText('Matched articles (1)')).toBeInTheDocument();
    expect(screen.queryByText('Hasina seeks passport')).toBeNull();
    await u.selectOptions(screen.getByLabelText('Minimum similarity'), '90');
    expect(screen.getByText('Nothing matches these filters.')).toBeInTheDocument();
    rerender(<SimilarArticles runId="r1" stories={[solo]} />);
    await waitFor(() => expect(screen.getByText('No similar articles were found across the uploaded newspapers.')).toBeInTheDocument());
  });

  it('shows the 10 highest possible matches and the rest on request', async () => {
    const many: Story = { ...solo, story_id: 'story-many', possible_matches: Array.from({ length: 12 }, (_, i) => ({
      event_id: String(i).padStart(64, '0'), paper: 'The Times of India', headline: `Candidate ${i}`, score: 0.7 - i * 0.01, reasons: [], components: comps, veto: null, for_event_id: C })) };
    const u = userEvent.setup();
    render(<SimilarArticles runId="r1" stories={[many]} />);
    const region = within(screen.getByRole('region', { name: 'Possible matches' }));
    expect(region.getAllByRole('button', { name: /not merged/ })).toHaveLength(10);
    expect(region.getByText('70%')).toBeInTheDocument();                       // highest first
    await u.click(region.getByRole('button', { name: 'Show all 12' }));
    expect(region.getAllByRole('button', { name: /not merged/ })).toHaveLength(12);
  });

  it('writes the shared names with spaces between them so they read and copy correctly', async () => {
    const u = userEvent.setup();
    render(<SimilarArticles runId="r1" stories={[matchedStory]} />);
    await u.click(screen.getByRole('button', { name: /2 similar articles/ }));
    const ctx = await screen.findByLabelText('Similar context');
    expect(ctx).toHaveTextContent('Shared people: Gurwinder Afridi');
    expect(ctx.textContent).toMatch(/Shared places: Ethiopia/);
    expect(ctx.textContent).not.toMatch(/Gurwinder AfridiPunjab|EthiopiaPunjab/);
  });

  it('shows the newspaper name and its date on every row of the list, and on the possible matches', () => {
    render(<SimilarArticles runId="r1" stories={[matchedStory, solo]} runDate="2026-11-14" />);
    const card = screen.getAllByRole('listitem').find((li) => li.classList.contains('sim-card'))!;
    expect(within(card).getByText('Hindustan Times')).toBeInTheDocument();
    expect(within(card).getByText('The Times of India')).toBeInTheDocument();
    expect(within(card).getAllByText('14 Nov 2026')).toHaveLength(2);                      // one date next to each newspaper
    expect(card.textContent).toMatch(/Hindustan Times\s+14 Nov 2026/);                    // written with a space, so it reads and copies correctly
    expect(card.textContent).toMatch(/The Times of India\s+14 Nov 2026/);
    const possible = within(screen.getByRole('region', { name: 'Possible matches' }));
    expect(possible.getByText('Hindustan Times')).toBeInTheDocument();
    expect(possible.getByText('The Times of India')).toBeInTheDocument();
    expect(possible.getAllByText('14 Nov 2026')).toHaveLength(2);                           // a date beside each of the two newspapers
  });

  it('names both newspapers with their dates above the comparison and in a labelled Newspaper / Date row on each article', async () => {
    const u = userEvent.setup();
    render(<SimilarArticles runId="r1" stories={[matchedStory]} runDate="2026-11-14" />);
    await u.click(screen.getByRole('button', { name: /2 similar articles/ }));
    const line = await screen.findByLabelText('Newspapers compared');
    expect(line).toHaveTextContent('Hindustan Times (14 Nov 2026) compared with The Times of India (14 Nov 2026)');
    for (const [role, paper] of [[/Article A/, 'Hindustan Times'], [/Article B/, 'The Times of India']] as const) {
      const box = within(screen.getByRole('article', { name: role }));
      expect(box.getByText('Newspaper').nextSibling).toHaveTextContent(paper);
      expect(box.getByText('Date').nextSibling).toHaveTextContent('14 Nov 2026');
      expect(box.getByText('Page').nextSibling).toHaveTextContent('6');
    }
  });

  it("prefers an article's own saved date, and says 'not recorded' when there is no date at all", async () => {
    m.getArticle.mockImplementation(async (_r, id) => id === A
      ? doc(A, 'Hindustan Times', 'Gangster brought back from Ethiopia', HT_TEXT, { ingestion_metadata: { published_at: '2026-11-13T00:00:00Z' } })
      : doc(B, 'The Times of India', 'Wanted Punjab gangster brought back', TOI_TEXT, { dedup_clustering: { matches: [{ event_id: A, score: 0.8, components: comps }] } }));
    const u = userEvent.setup();
    render(<SimilarArticles runId="r1" stories={[matchedStory]} />);                       // no run date given
    await u.click(screen.getByRole('button', { name: /2 similar articles/ }));
    const a = within(await screen.findByRole('article', { name: /Article A/ }));
    const b = within(screen.getByRole('article', { name: /Article B/ }));
    expect(a.getByText('Date').nextSibling).toHaveTextContent('13 Nov 2026');
    expect(b.getByText('Date').nextSibling).toHaveTextContent('not recorded');
    expect(screen.getByLabelText('Newspapers compared')).toHaveTextContent('Hindustan Times (13 Nov 2026) compared with The Times of India');
  });

  it('shows an error when an article cannot be loaded', async () => {
    m.getArticle.mockRejectedValue(new api.ApiError('unknown article', 404));
    const u = userEvent.setup();
    render(<SimilarArticles runId="r1" stories={[matchedStory]} />);
    await u.click(screen.getByRole('button', { name: /2 similar articles/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent('unknown article');
  });
});
