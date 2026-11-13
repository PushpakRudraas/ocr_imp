import type { Article, MatchEdge, Story } from '../types';

/** A pair of articles the matching engine scored. `matched` = the engine merged them into one story; `possible` = borderline, kept apart for review. */
export interface Pair {
  key: string;
  a: string;                    // event_id of the first article
  b: string;                    // event_id of the second article
  score: number;                // the engine's score, 0..1 (not a calibrated probability)
  edge: MatchEdge;
  kind: 'matched' | 'possible';
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
/** "2026-11-14" or "2026-11-14T00:00:00Z" -> "14 Nov 2026"; other text is shown as it is; nothing -> null. */
export function formatDate(value?: string | null): string | null {
  if (!value) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  const month = m ? MONTHS[Number(m[2]) - 1] : undefined;
  return m && month ? `${Number(m[3])} ${month} ${m[1]}` : value;
}

/** The newspaper's date: the article's own saved date, else the date entered for the run. (The date is the one entered at upload;
 *  the system does not read a printed date from the PDF.) */
export function articleDate(doc: Article, runDate?: string | null): string | null {
  return formatDate(doc.ingestion_metadata?.published_at) ?? formatDate(runDate);
}

export const pairKey = (a: string, b: string) => [a, b].sort().join('|');
export const percent = (score: number) => `${Math.round(score * 100)}%`;

/** Pairs the engine merged, read from the saved records of one story's articles (each record lists the earlier articles it matched). */
export function matchedPairs(docs: Article[]): Pair[] {
  const ids = new Set(docs.map((d) => d.event_id));
  const best = new Map<string, Pair>();
  for (const d of docs) {
    for (const m of d.dedup_clustering?.matches ?? []) {
      if (!ids.has(m.event_id) || m.event_id === d.event_id) continue;
      const key = pairKey(d.event_id, m.event_id);
      const cur = best.get(key);
      if (!cur || m.score > cur.score) best.set(key, { key, a: m.event_id, b: d.event_id, score: m.score, edge: m, kind: 'matched' });
    }
  }
  return [...best.values()].sort((x, y) => y.score - x.score);
}

/** Borderline pairs listed on the stories (needs a person to decide); one entry per pair, highest score kept. */
export function possiblePairs(stories: Story[]): Pair[] {
  const best = new Map<string, Pair>();
  for (const s of stories) {
    for (const p of s.possible_matches ?? []) {
      const key = pairKey(p.for_event_id, p.event_id);
      const cur = best.get(key);
      if (!cur || p.score > cur.score) {
        best.set(key, { key, a: p.for_event_id, b: p.event_id, score: p.score,
          edge: { event_id: p.event_id, score: p.score, reasons: p.reasons, components: p.components, veto: p.veto }, kind: 'possible' });
      }
    }
  }
  return [...best.values()].sort((x, y) => y.score - x.score);
}

// ----------------------------------------------------------------------------------------- similar context (from the stored articles)
const STOP = new Set(`about above after again against also among because been before being between both cannot could does doing down during each from
further have having here hers herself himself into itself just more most much myself once only other ours ourselves over same should some such than
that their theirs them themselves then there these they this those through under until very were what when where which while whom will with would your
yours yourself said says say told according new one two three also still many first last year years today yesterday monday tuesday wednesday thursday
friday saturday sunday`.split(/\s+/));

const norm = (s: string) => s.toLowerCase().replace(/[^\p{L}\p{N}\s]/gu, ' ').replace(/\s+/g, ' ').trim();
const tokens = (s: string) => norm(s).split(' ').filter(Boolean);
const contentWords = (s: string) => tokens(s).filter((w) => w.length >= 4 && !STOP.has(w) && !/^\d+$/.test(w));

export interface SharedEntity { text: string; type: string; mentionsA: number; mentionsB: number }

/** Names that appear in both articles: the same name, or a fuller form of it ("Vijay Pratap Singh" / "Kunwar Vijay Pratap Singh"). */
export function sharedEntities(a: Article, b: Article): SharedEntity[] {
  const out: SharedEntity[] = [];
  const used = new Set<number>();
  for (const ea of a.entities ?? []) {
    const ta = tokens(ea.text);
    if (ta.length === 0) continue;
    const j = (b.entities ?? []).findIndex((eb, i) => {
      if (used.has(i)) return false;
      const tb = tokens(eb.text);
      if (tb.length === 0) return false;
      if (ta.join(' ') === tb.join(' ')) return true;
      const [short, long] = ta.length <= tb.length ? [ta, tb] : [tb, ta];
      return short.length >= 2 && short.every((w) => long.includes(w));
    });
    if (j >= 0) {
      used.add(j);
      const eb = (b.entities ?? [])[j];
      out.push({ text: ea.text.length >= eb.text.length ? ea.text : eb.text, type: ea.type, mentionsA: ea.mentions ?? 1, mentionsB: eb.mentions ?? 1 });
    }
  }
  return out.sort((x, y) => (y.mentionsA + y.mentionsB) - (x.mentionsA + x.mentionsB));
}

export function sharedKeyphrases(a: Article, b: Article): string[] {
  const nb = new Map((b.keyphrases ?? []).map((k) => [norm(k), k]));
  return (a.keyphrases ?? []).filter((k) => nb.has(norm(k)));
}

const NUM = /\d[\d,]*(?:\.\d+)?/g;
/** Figures (counts, amounts, years, dates) that appear in both texts. */
export function sharedNumbers(textA: string, textB: string, limit = 10): string[] {
  const grab = (t: string) => new Set((t.match(NUM) ?? []).map((n) => n.replace(/,/g, '').replace(/\.$/, '')).filter((n) => n.length >= 2));
  const nb = grab(textB);
  return [...grab(textA)].filter((n) => nb.has(n)).sort((x, y) => y.length - x.length).slice(0, limit);
}

/** Content words used in both texts, most repeated first. */
export function sharedWords(textA: string, textB: string, limit = 14): string[] {
  const count = (t: string) => { const m = new Map<string, number>(); contentWords(t).forEach((w) => m.set(w, (m.get(w) ?? 0) + 1)); return m; };
  const ca = count(textA), cb = count(textB);
  return [...ca.keys()].filter((w) => cb.has(w))
    .sort((x, y) => (Math.min(cb.get(y)!, ca.get(y)!) - Math.min(cb.get(x)!, ca.get(x)!)) || (y.length - x.length) || x.localeCompare(y))
    .slice(0, limit);
}

export interface Passage { a: string; b: string; shared: string[]; overlap: number }

const sentences = (t: string) => t.split(/(?<=[.!?])\s+|\n+/).map((s) => s.trim()).filter((s) => s.split(/\s+/).length >= 6);

/** The passages of the two articles that use the most of the same words (only shown when the overlap is real). */
export function similarPassages(textA: string, textB: string, limit = 3): Passage[] {
  const sb = sentences(textB).map((s) => ({ s, w: new Set(contentWords(s)) }));
  const found: Passage[] = [];
  const taken = new Set<number>();
  for (const sa of sentences(textA)) {
    const wa = new Set(contentWords(sa));
    if (wa.size < 3) continue;
    let best = { i: -1, shared: [] as string[], overlap: 0 };
    sb.forEach((cand, i) => {
      if (taken.has(i)) return;
      const shared = [...wa].filter((w) => cand.w.has(w));
      const overlap = shared.length / new Set([...wa, ...cand.w]).size;
      if (shared.length >= 3 && overlap > best.overlap) best = { i, shared, overlap };
    });
    if (best.i >= 0 && best.overlap >= 0.2) { taken.add(best.i); found.push({ a: sa, b: sb[best.i].s, shared: best.shared, overlap: best.overlap }); }
  }
  return found.sort((x, y) => y.overlap - x.overlap).slice(0, limit);
}

export interface Context {
  eventType: { a: string; b: string; same: boolean } | null;
  place: { a: string | null; b: string | null; same: boolean };
  entities: SharedEntity[];
  keyphrases: string[];
  numbers: string[];
  words: string[];
  passages: Passage[];
}

const textOf = (d: Article) => d.newspaper.text || d.newspaper.translated_text || '';

/** Everything the two articles have in common, worked out from their stored records. */
export function similarContext(a: Article, b: Article): Context {
  const ta = textOf(a), tb = textOf(b);
  return {
    eventType: a.event?.type && b.event?.type ? { a: a.event.type, b: b.event.type, same: a.event.type === b.event.type } : null,
    place: { a: a.geo?.primary_location ?? null, b: b.geo?.primary_location ?? null,
      same: !!a.geo?.primary_location && norm(a.geo.primary_location) === norm(b.geo?.primary_location ?? '') },
    entities: sharedEntities(a, b), keyphrases: sharedKeyphrases(a, b),
    numbers: sharedNumbers(ta, tb), words: sharedWords(ta, tb), passages: similarPassages(ta, tb),
  };
}

export const hasContext = (c: Context) =>
  c.entities.length > 0 || c.keyphrases.length > 0 || c.numbers.length > 0 || c.words.length > 0 || c.passages.length > 0 || c.place.same || !!c.eventType?.same;
