import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, buildUploadForm, createRun, errorMessage, getRun, listRuns } from './api';
import { pdfFile } from './test/fixtures';
import type { UploadItem } from './types';

const item = (file: string, name: string, over: Partial<UploadItem> = {}): UploadItem =>
  ({ key: file, file: pdfFile(file), name, language: 'en', edition: 'Delhi', reputation: 'high', ...over });

afterEach(() => { vi.unstubAllGlobals(); });

describe('errorMessage', () => {
  it('handles string, validation-list and unknown bodies', () => {
    expect(errorMessage({ detail: 'x is not a PDF' }, 'fb')).toBe('x is not a PDF');
    expect(errorMessage({ detail: [{ msg: 'field required' }, { msg: 'bad date' }] }, 'fb')).toBe('field required; bad date');
    expect(errorMessage({}, 'fb')).toBe('fb');
    expect(errorMessage(null, 'fb')).toBe('fb');
  });
});

describe('buildUploadForm', () => {
  it('keeps files and metadata in the same order and trims fields', () => {
    const form = buildUploadForm('2026-11-02', [item('a.pdf', ' The Hindu '), item('b.pdf', 'TOI', { edition: '  ', language: 'hi' })]);
    expect(form.get('run_date')).toBe('2026-11-02');
    expect(JSON.parse(form.get('papers') as string)).toEqual([
      { name: 'The Hindu', language: 'en', edition: 'Delhi', reputation: 'high' },
      { name: 'TOI', language: 'hi', edition: 'main', reputation: 'high' }]);
    expect((form.getAll('files') as File[]).map((f) => f.name)).toEqual(['a.pdf', 'b.pdf']);
  });
  it('sends the enrichment toggle, off unless asked for', () => {
    expect(buildUploadForm('2026-11-02', [item('a.pdf', 'The Hindu')]).get('enrich')).toBe('false');
    expect(buildUploadForm('2026-11-02', [item('a.pdf', 'The Hindu')], true).get('enrich')).toBe('true');
  });
});

describe('fetch helpers', () => {
  it('returns parsed json', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => [{ id: 'r1' }] }));
    expect(await listRuns()).toEqual([{ id: 'r1' }]);
  });
  it('maps server errors and network failures to ApiError', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404, json: async () => ({ detail: 'unknown run' }) }));
    await expect(getRun('x')).rejects.toMatchObject({ message: 'unknown run', status: 404 });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 500, json: async () => { throw new Error('html'); } }));
    await expect(getRun('x')).rejects.toMatchObject({ message: 'Request failed (500)', status: 500 });
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('fail')));
    await expect(getRun('x')).rejects.toBeInstanceOf(ApiError);
    await expect(getRun('x')).rejects.toMatchObject({ status: 0 });
  });
});

class FakeXHR {
  static last: FakeXHR;
  upload: { onprogress?: (e: { lengthComputable: boolean; loaded: number; total: number }) => void } = {};
  onload?: () => void; onerror?: () => void;
  status = 202; responseText = '{"id":"abcdefabcdef","status":"queued"}';
  method = ''; url = ''; body: FormData | null = null;
  constructor() { FakeXHR.last = this; }
  open(m: string, u: string) { this.method = m; this.url = u; }
  send(b: FormData) { this.body = b; }
}

describe('createRun', () => {
  it('posts multipart, reports progress and resolves with the run', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXHR);
    const seen: number[] = [];
    const p = createRun('2026-11-02', [item('a.pdf', 'The Hindu')], true, (f) => seen.push(f));
    const x = FakeXHR.last;
    expect(x.method).toBe('POST'); expect(x.url).toBe('/api/runs');
    x.upload.onprogress?.({ lengthComputable: true, loaded: 50, total: 100 });
    x.upload.onprogress?.({ lengthComputable: false, loaded: 0, total: 0 });
    x.onload?.();
    expect(await p).toMatchObject({ id: 'abcdefabcdef' });
    expect(seen).toEqual([0.5]);
    expect(x.body?.get('run_date')).toBe('2026-11-02'); expect(x.body?.get('enrich')).toBe('true');
  });
  it('rejects with the server detail on 4xx and on network error', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXHR);
    const p = createRun('2026-11-02', [item('a.pdf', 'x')]);
    Object.assign(FakeXHR.last, { status: 400, responseText: '{"detail":"a.pdf is not a PDF"}' });
    FakeXHR.last.onload?.();
    await expect(p).rejects.toMatchObject({ message: 'a.pdf is not a PDF', status: 400 });
    const q = createRun('2026-11-02', [item('a.pdf', 'x')]);
    Object.assign(FakeXHR.last, { status: 502, responseText: '<html>bad gateway' });
    FakeXHR.last.onload?.();
    await expect(q).rejects.toMatchObject({ message: 'Upload failed (502)' });
    const r = createRun('2026-11-02', [item('a.pdf', 'x')]);
    FakeXHR.last.onerror?.();
    await expect(r).rejects.toMatchObject({ status: 0 });
  });
});
