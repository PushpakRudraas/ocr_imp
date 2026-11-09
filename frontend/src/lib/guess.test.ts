import { describe, expect, it } from 'vitest';
import { fmtBytes, guessPaper, humanize, isPdf } from './guess';

describe('guessPaper', () => {
  it('recognises well-known papers and their language', () => {
    expect(guessPaper('the_hindu_2026-11-02.pdf')).toEqual({ name: 'The Hindu', language: 'en', reputation: 'high', date: '2026-11-02' });
    expect(guessPaper('TOI.pdf').name).toBe('The Times of India');
    expect(guessPaper('times-of-india 2026.11.02.PDF')).toMatchObject({ name: 'The Times of India', date: '2026-11-02' });
    expect(guessPaper('indian_express.pdf').name).toBe('The Indian Express');
    expect(guessPaper('dainik_jagran_2026-11-02.pdf')).toMatchObject({ name: 'Dainik Jagran', language: 'hi' });
    expect(guessPaper('Amar Ujala.pdf').language).toBe('hi');
  });
  it('title-cases unknown names and defaults safely', () => {
    expect(guessPaper('daily_pioneer-2026-01-05.pdf')).toEqual({ name: 'Daily Pioneer', language: 'en', reputation: 'not_rated', date: '2026-01-05' });
    expect(guessPaper('2026-11-02.pdf').name).toBe('Untitled paper');
    expect(guessPaper('scan.pdf').date).toBeNull();
  });
});

describe('helpers', () => {
  it('formats sizes', () => {
    expect(fmtBytes(500)).toBe('500 B'); expect(fmtBytes(2048)).toBe('2 KB'); expect(fmtBytes(5 * 1024 * 1024)).toBe('5.0 MB');
  });
  it('detects pdfs by type or extension', () => {
    expect(isPdf(new File([], 'a.PDF'))).toBe(true);
    expect(isPdf(new File([], 'a', { type: 'application/pdf' }))).toBe(true);
    expect(isPdf(new File([], 'a.png', { type: 'image/png' }))).toBe(false);
  });
  it('humanizes identifiers', () => expect(humanize('translation_failed_no_backend')).toBe('translation failed no backend'));
});
