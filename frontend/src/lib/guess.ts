import type { Reputation } from '../types';

export interface Guess {
  name: string;
  language: string;
  reputation: Reputation;
  date: string | null;
}

const KNOWN: { match: RegExp; name: string; language: string; reputation: Reputation }[] = [
  { match: /^the[ _-]?hindu$|^hindu$/, name: 'The Hindu', language: 'en', reputation: 'high' },
  { match: /^toi$|times[ _-]?of[ _-]?india/, name: 'The Times of India', language: 'en', reputation: 'high' },
  { match: /indian[ _-]?express|^ie$/, name: 'The Indian Express', language: 'en', reputation: 'high' },
  { match: /hindustan[ _-]?times|^ht$/, name: 'Hindustan Times', language: 'en', reputation: 'high' },
  { match: /dainik[ _-]?jagran|^jagran$/, name: 'Dainik Jagran', language: 'hi', reputation: 'medium' },
  { match: /amar[ _-]?ujala/, name: 'Amar Ujala', language: 'hi', reputation: 'medium' },
  { match: /navbharat/, name: 'Navbharat Times', language: 'hi', reputation: 'medium' },
  { match: /dainik[ _-]?bhaskar|^bhaskar$/, name: 'Dainik Bhaskar', language: 'hi', reputation: 'medium' },
];

const DATE = /(\d{4})[-_.](\d{2})[-_.](\d{2})/;

function titleCase(s: string): string {
  return s.replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim().replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Suggest a paper name, language and date from a filename like "the_hindu_2026-11-02.pdf". */
export function guessPaper(filename: string): Guess {
  const base = filename.replace(/\.pdf$/i, '');
  const m = DATE.exec(base);
  const date = m ? `${m[1]}-${m[2]}-${m[3]}` : null;
  const stem = base.replace(DATE, '').replace(/[_\-. ]+$/g, '').replace(/^[_\-. ]+/g, '');
  const key = stem.toLowerCase().replace(/[_.]+/g, ' ').trim();
  const known = KNOWN.find((k) => k.match.test(key) || k.match.test(key.replace(/\s+/g, '_')));
  if (known) return { name: known.name, language: known.language, reputation: known.reputation, date };
  return { name: titleCase(stem) || 'Untitled paper', language: 'en', reputation: 'not_rated', date };
}

export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function isPdf(f: File): boolean {
  return f.type === 'application/pdf' || /\.pdf$/i.test(f.name);
}

export function humanize(s: string): string {
  return s.replace(/[._]/g, ' ').replace(/\s+/g, ' ').trim();
}
