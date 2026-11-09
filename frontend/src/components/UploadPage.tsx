import { useEffect, useRef, useState } from 'react';
import type { DragEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { ApiError, createRun, getConfig } from '../api';
import { fmtBytes, guessPaper, isPdf } from '../lib/guess';
import type { Capabilities, Reputation, UploadItem } from '../types';
import RunsList from './RunsList';

export const LANGUAGES: [string, string][] = [
  ['en', 'English'], ['hi', 'Hindi'], ['mr', 'Marathi'], ['bn', 'Bengali'], ['ta', 'Tamil'], ['te', 'Telugu'],
  ['kn', 'Kannada'], ['ml', 'Malayalam'], ['gu', 'Gujarati'], ['pa', 'Punjabi'], ['ur', 'Urdu'],
];
const REPUTATIONS: Reputation[] = ['high', 'medium', 'low', 'not_rated'];
const today = () => new Date().toISOString().slice(0, 10);

/** Returns an error message, or null if the form can be submitted. */
export function validate(items: UploadItem[], runDate: string, caps: Capabilities | null): string | null {
  if (items.length === 0) return 'Add at least one newspaper PDF.';
  if (!/^\d{4}-\d{2}-\d{2}$/.test(runDate)) return 'Choose the publication date of these editions.';
  if (caps && items.length > caps.max_papers) return `At most ${caps.max_papers} newspapers per run.`;
  const blank = items.find((i) => !i.name.trim());
  if (blank) return `Give "${blank.file.name}" a newspaper name.`;
  const names = new Set<string>();
  for (const i of items) {
    const k = i.name.trim().toLowerCase();
    if (names.has(k)) return `"${i.name.trim()}" appears twice - one edition per newspaper.`;
    names.add(k);
  }
  const big = caps && items.find((i) => i.file.size > caps.max_upload_mb * 1024 * 1024);
  if (big) return `"${big.file.name}" is larger than ${caps!.max_upload_mb} MB.`;
  return null;
}

export default function UploadPage() {
  const navigate = useNavigate();
  const [items, setItems] = useState<UploadItem[]>([]);
  const [runDate, setRunDate] = useState(today());
  const [dateTouched, setDateTouched] = useState(false);
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [rejected, setRejected] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    getConfig().then(setCaps).catch(() => setCaps(null));
  }, []);

  function addFiles(list: FileList | File[]) {
    const files = Array.from(list);
    setRejected(files.filter((f) => !isPdf(f)).map((f) => f.name));
    const fresh: UploadItem[] = [];
    let guessedDate: string | null = null;
    for (const file of files.filter(isPdf)) {
      const dup = [...items, ...fresh].some((i) => i.file.name === file.name && i.file.size === file.size);
      if (dup) continue;
      const g = guessPaper(file.name);
      guessedDate = guessedDate ?? g.date;
      fresh.push({ key: `${file.name}:${file.size}:${Math.random().toString(36).slice(2, 7)}`, file, name: g.name, language: g.language, edition: 'main', reputation: g.reputation });
    }
    if (fresh.length) setItems((prev) => [...prev, ...fresh]);
    if (guessedDate && !dateTouched) setRunDate(guessedDate);
    setError(null);
  }

  function update(key: string, patch: Partial<UploadItem>) {
    setItems((prev) => prev.map((i) => (i.key === key ? { ...i, ...patch } : i)));
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    setDragging(false);
    addFiles(e.dataTransfer.files);
  }

  async function submit() {
    const problem = validate(items, runDate, caps);
    if (problem) return setError(problem);
    setError(null);
    setProgress(0);
    try {
      const run = await createRun(runDate, items, setProgress);
      navigate(`/runs/${run.id}`);
    } catch (e) {
      setProgress(null);
      setError(e instanceof ApiError ? e.message : 'Upload failed.');
    }
  }

  const uploading = progress !== null;
  const nonEnglish = items.some((i) => i.language !== 'en');
  return (
    <div className="stack">
      <section className="card">
        <h1>Upload today's newspapers</h1>
        <p className="muted">
          Add the e-paper PDF of each newspaper. The pipeline reads every page, splits it into articles, translates, finds the
          same story across papers and enriches it.
        </p>

        <div
          className={`dropzone${dragging ? ' dragging' : ''}`}
          role="button"
          tabIndex={0}
          aria-label="Add newspaper PDFs"
          onClick={() => input.current?.click()}
          onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && input.current?.click()}
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
        >
          <strong>Drop PDFs here</strong> or click to browse
          <span className="muted small">One edition per newspaper{caps ? ` - up to ${caps.max_papers} papers, ${caps.max_upload_mb} MB each` : ''}</span>
          <input ref={input} data-testid="file-input" type="file" accept="application/pdf,.pdf" multiple hidden
                 onChange={(e) => { if (e.target.files) addFiles(e.target.files); e.target.value = ''; }} />
        </div>

        {rejected.length > 0 && <p className="notice warn" role="alert">Skipped (not PDF): {rejected.join(', ')}</p>}

        {items.length > 0 && (
          <ul className="papers" aria-label="Newspapers to upload">
            {items.map((it) => (
              <li key={it.key} className="paper-row">
                <div className="file-info">
                  <span className="file-name" title={it.file.name}>{it.file.name}</span>
                  <span className="muted small">{fmtBytes(it.file.size)}</span>
                </div>
                <label>Newspaper
                  <input value={it.name} aria-label={`Name for ${it.file.name}`} disabled={uploading} onChange={(e) => update(it.key, { name: e.target.value })} />
                </label>
                <label>Language
                  <select value={it.language} aria-label={`Language for ${it.file.name}`} disabled={uploading} onChange={(e) => update(it.key, { language: e.target.value })}>
                    {LANGUAGES.map(([c, n]) => <option key={c} value={c}>{n}</option>)}
                  </select>
                </label>
                <label>Edition
                  <input value={it.edition} aria-label={`Edition for ${it.file.name}`} disabled={uploading} onChange={(e) => update(it.key, { edition: e.target.value })} />
                </label>
                <label>Reputation
                  <select value={it.reputation} aria-label={`Reputation for ${it.file.name}`} disabled={uploading} onChange={(e) => update(it.key, { reputation: e.target.value as Reputation })}>
                    {REPUTATIONS.map((r) => <option key={r} value={r}>{r.replace('_', ' ')}</option>)}
                  </select>
                </label>
                <button type="button" className="icon-btn" aria-label={`Remove ${it.file.name}`} disabled={uploading}
                        onClick={() => setItems((p) => p.filter((i) => i.key !== it.key))}>x</button>
              </li>
            ))}
          </ul>
        )}

        {caps?.ocr === 'none' && (
          <p className="notice warn">No OCR engine is installed on the server: scanned (image-only) PDFs will be skipped. Digital e-paper PDFs work.</p>
        )}
        {nonEnglish && caps && caps.translator === 'none' && caps.embedder === 'hashing' && (
          <p className="notice warn">No translator is configured: non-English papers will be read but not matched to English coverage of the same story.</p>
        )}

        <div className="form-footer">
          <label className="inline">Publication date
            <input type="date" value={runDate} disabled={uploading} onChange={(e) => { setRunDate(e.target.value); setDateTouched(true); }} />
          </label>
          <button type="button" className="primary" disabled={uploading || items.length === 0} onClick={submit}>
            {uploading ? `Uploading ${Math.round((progress ?? 0) * 100)}%` : `Process ${items.length || ''} newspaper${items.length === 1 ? '' : 's'}`.replace('  ', ' ')}
          </button>
        </div>
        {uploading && <div className="bar" role="progressbar" aria-valuenow={Math.round((progress ?? 0) * 100)} aria-valuemin={0} aria-valuemax={100}><span style={{ width: `${(progress ?? 0) * 100}%` }} /></div>}
        {error && <p className="notice error" role="alert">{error}</p>}
      </section>

      <RunsList />
    </div>
  );
}
