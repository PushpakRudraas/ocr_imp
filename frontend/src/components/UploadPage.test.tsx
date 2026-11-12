import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '../api';
import { caps, pdfFile, run } from '../test/fixtures';
import type { UploadItem } from '../types';
import UploadPage, { validate } from './UploadPage';

vi.mock('../api', async (orig) => ({ ...(await orig<typeof import('../api')>()), createRun: vi.fn(), getConfig: vi.fn(), listRuns: vi.fn(), deleteRun: vi.fn() }));
const m = vi.mocked(api);

function setup() {
  render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route path="/" element={<UploadPage />} />
        <Route path="/runs/:runId" element={<p>run page</p>} />
      </Routes>
    </MemoryRouter>,
  );
  return userEvent.setup();
}
const add = (u: ReturnType<typeof userEvent.setup>, ...files: File[]) => u.upload(screen.getByTestId('file-input'), files);

beforeEach(() => { vi.clearAllMocks(); m.getConfig.mockResolvedValue(caps); m.listRuns.mockResolvedValue([]); });

describe('validate', () => {
  const it1 = (name: string, size = 10): UploadItem => ({ key: name, file: pdfFile(`${name}.pdf`, size), name, language: 'en', edition: 'main', reputation: 'high' });
  it('checks each rule', () => {
    expect(validate([], '2026-11-02', caps)).toMatch(/at least one/);
    expect(validate([it1('A')], '', caps)).toMatch(/publication date/);
    expect(validate([it1('A')], '2026-11-02', { ...caps, max_papers: 0 })).toMatch(/At most 0/);
    expect(validate([it1(' ')], '2026-11-02', caps)).toMatch(/newspaper name/);
    expect(validate([it1('A'), it1('a')], '2026-11-02', caps)).toMatch(/appears twice/);
    expect(validate([it1('A', 2 * 1024 * 1024)], '2026-11-02', { ...caps, max_upload_mb: 1 })).toMatch(/larger than 1 MB/);
    expect(validate([it1('A'), it1('B')], '2026-11-02', caps)).toBeNull();
    expect(validate([it1('A')], '2026-11-02', null)).toBeNull();
  });
});

describe('UploadPage', () => {
  it('lists dropped PDFs with guessed name, language and date, and ignores non-PDFs', async () => {
    const u = setup();
    await add(u, pdfFile('the_hindu_2026-11-02.pdf'), pdfFile('dainik_jagran_2026-11-02.pdf'));
    expect(screen.getByLabelText('Name for the_hindu_2026-11-02.pdf')).toHaveValue('The Hindu');
    expect(screen.getByLabelText('Language for dainik_jagran_2026-11-02.pdf')).toHaveValue('hi');
    expect(screen.getByLabelText('Publication date')).toHaveValue('2026-11-02');
    expect(screen.getByRole('button', { name: /process 2 newspapers/i })).toBeEnabled();
    await userEvent.upload(screen.getByTestId('file-input'), new File(['x'], 'notes.txt', { type: 'text/plain' }), { applyAccept: false });
    expect(screen.getByRole('alert')).toHaveTextContent('Skipped (not PDF): notes.txt');
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
  });

  it('ignores the same file added twice and lets you remove a row', async () => {
    const u = setup();
    await add(u, pdfFile('toi.pdf', 100));
    await add(u, pdfFile('toi.pdf', 100));
    expect(screen.getAllByLabelText(/^Name for/)).toHaveLength(1);
    await u.click(screen.getByRole('button', { name: 'Remove toi.pdf' }));
    expect(screen.queryByLabelText(/^Name for/)).toBeNull();
    expect(screen.getByRole('button', { name: /process/i })).toBeDisabled();
  });

  it('submits edited metadata in file order and navigates to the run', async () => {
    m.createRun.mockResolvedValue(run({ id: 'abcdefabcdef' }));
    const u = setup();
    await add(u, pdfFile('the_hindu.pdf'), pdfFile('toi.pdf'));
    const name = screen.getByLabelText('Name for toi.pdf');
    await u.clear(name); await u.type(name, 'TOI Delhi');
    await u.selectOptions(screen.getByLabelText('Reputation for toi.pdf'), 'medium');
    await u.click(screen.getByRole('button', { name: /process 2 newspapers/i }));
    await waitFor(() => expect(screen.getByText('run page')).toBeInTheDocument());
    const [date, items, enrich] = m.createRun.mock.calls[0];
    expect(enrich).toBe(false);                                           // enrichment is opt-in
    expect(date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(items.map((i) => [i.file.name, i.name, i.reputation])).toEqual([['the_hindu.pdf', 'The Hindu', 'high'], ['toi.pdf', 'TOI Delhi', 'medium']]);
  });

  it('has an enrichment toggle, off by default, that is sent with the upload', async () => {
    m.createRun.mockResolvedValue(run({ id: 'abcdefabcdef' }));
    const u = setup();
    await add(u, pdfFile('toi.pdf'));
    const toggle = screen.getByRole('switch', { name: /Run enrichment after extraction/ });
    expect(toggle).not.toBeChecked();
    expect(screen.getByText(/Off: the run stops once the cleaned articles are stored/)).toBeInTheDocument();
    await u.click(toggle);
    expect(toggle).toBeChecked();
    expect(screen.getByText(/queued on Kafka/)).toBeInTheDocument();
    await u.click(screen.getByRole('button', { name: /process/i }));
    await waitFor(() => expect(m.createRun).toHaveBeenCalled());
    expect(m.createRun.mock.calls[0][2]).toBe(true);
  });

  it('describes inline enrichment when no Kafka is configured', async () => {
    m.getConfig.mockResolvedValue({ ...caps, enrichment_bus: 'inline' });
    const u = setup();
    await add(u, pdfFile('toi.pdf'));
    await u.click(screen.getByRole('switch', { name: /Run enrichment after extraction/ }));
    expect(await screen.findByText(/starts in this server/)).toBeInTheDocument();
  });

  it('shows client-side validation and server errors without leaving the page', async () => {
    m.createRun.mockRejectedValue(new api.ApiError('toi.pdf is not a PDF', 400));
    const u = setup();
    await add(u, pdfFile('toi.pdf'), pdfFile('times_of_india.pdf'));
    await u.click(screen.getByRole('button', { name: /process/i }));
    expect(screen.getByRole('alert')).toHaveTextContent(/appears twice/);
    expect(m.createRun).not.toHaveBeenCalled();
    await u.clear(screen.getByLabelText('Name for times_of_india.pdf')); await u.type(screen.getByLabelText('Name for times_of_india.pdf'), 'Other');
    await u.click(screen.getByRole('button', { name: /process/i }));
    expect(await screen.findByText('toi.pdf is not a PDF')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /process/i })).toBeEnabled();     // can retry
  });

  it('warns about missing OCR and missing translation based on server capabilities', async () => {
    m.getConfig.mockResolvedValue({ ...caps, ocr: 'none', translator: 'none' });
    const u = setup();
    expect(await screen.findByText(/No OCR engine is installed/)).toBeInTheDocument();
    expect(screen.queryByText(/No translator is configured/)).toBeNull();
    await add(u, pdfFile('dainik_jagran.pdf'));
    expect(screen.getByText(/No translator is configured/)).toBeInTheDocument();
  });

  it('shows previous runs and still works when the config endpoint is down', async () => {
    m.getConfig.mockRejectedValue(new Error('down'));
    m.listRuns.mockResolvedValue([run()]);
    setup();
    const prev = await screen.findByRole('heading', { name: 'Previous runs' });
    expect(within(prev.closest('section')!).getByText(/The Hindu/)).toBeInTheDocument();
  });
});
