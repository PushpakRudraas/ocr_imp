import { describe, expect, it } from 'vitest';
import type { Article, Story } from '../types';
import { hasContext, matchedPairs, pairKey, percent, possiblePairs, sharedEntities, sharedNumbers, sharedWords, similarContext, similarPassages } from './similar';

const doc = (id: string, over: Partial<Article> = {}, text = 'Plain text.'): Article => ({
  event_id: id, event: { type: 'Crime.Arrest', trigger: 'arrested', actor: null, institution: null, date: '2026-11-08', confidence: 'medium' },
  claims: [], keyphrases: [], summary: { one_line: '', multi_doc_summary: '' },
  newspaper: { paper: 'P', headline: 'H', translated_headline: null, text, translated_text: null, translation_status: null },
  ...over,
});

describe('pairs', () => {
  it('a pair key does not depend on the order, and scores show as whole percents', () => {
    expect(pairKey('a', 'b')).toBe(pairKey('b', 'a'));
    expect(percent(0.796)).toBe('80%');
  });

  it('matchedPairs reads the saved matches, keeps the best score per pair, ignores articles outside the story and sorts by score', () => {
    const docs = [
      doc('A'),
      doc('B', { dedup_clustering: { matches: [{ event_id: 'A', score: 0.8 }] } }),
      doc('C', { dedup_clustering: { matches: [{ event_id: 'A', score: 0.7 }, { event_id: 'B', score: 0.9 }, { event_id: 'Z', score: 0.99 }] } }),
    ];
    const pairs = matchedPairs(docs);
    expect(pairs.map((p) => [p.a, p.b, p.score])).toEqual([['B', 'C', 0.9], ['A', 'B', 0.8], ['A', 'C', 0.7]]);
    expect(pairs.every((p) => p.kind === 'matched')).toBe(true);
    expect(matchedPairs([doc('A'), doc('B')])).toEqual([]);                        // an older run: no saved matches
  });

  it('possiblePairs lists each borderline pair once, with the highest score', () => {
    const cand = (event_id: string, for_event_id: string, score: number) => ({ event_id, for_event_id, score, paper: 'HT', headline: 'x', reasons: ['r'], components: null, veto: null });
    const mk = (id: string, pm: ReturnType<typeof cand>[]): Story => ({ story_id: id, suggested_story_key: null, n_papers: 1, event_type: 'x', location: null, topic: 't', impact: 0, summary: '', bias_label: '', coverage: [], possible_matches: pm });
    const out = possiblePairs([mk('s1', [cand('B', 'A', 0.6)]), mk('s2', [cand('A', 'B', 0.7), cand('D', 'C', 0.55)])]);
    expect(out.map((p) => [pairKey(p.a, p.b), p.score, p.kind])).toEqual([['A|B', 0.7, 'possible'], ['C|D', 0.55, 'possible']]);
  });
});

describe('similar context (computed from the stored articles)', () => {
  it('finds names in both articles, including a fuller form of the same name, and counts the mentions', () => {
    const a = doc('A', { entities: [{ text: 'Vijay Pratap Singh', type: 'PERSON', mentions: 3 }, { text: 'Punjab Police', type: 'ORG', mentions: 2 }, { text: 'Ethiopia', type: 'GPE', mentions: 4 }] });
    const b = doc('B', { entities: [{ text: 'Kunwar Vijay Pratap Singh', type: 'PERSON', mentions: 1 }, { text: 'punjab police', type: 'ORG', mentions: 5 }, { text: 'Delhi', type: 'GPE', mentions: 2 }] });
    const shared = sharedEntities(a, b);
    expect(shared.map((e) => e.text)).toEqual(['Punjab Police', 'Kunwar Vijay Pratap Singh']);          // most mentioned first; the fuller name is shown
    expect(shared.find((e) => e.text === 'Kunwar Vijay Pratap Singh')).toMatchObject({ type: 'PERSON', mentionsA: 3, mentionsB: 1 });
    expect(shared.some((e) => e.text === 'Ethiopia' || e.text === 'Delhi')).toBe(false);
  });

  it('a single shared word is not a name match (only the same name or a fuller form of a multi-word name)', () => {
    const a = doc('A', { entities: [{ text: 'Singh', type: 'PERSON' }] });
    const b = doc('B', { entities: [{ text: 'Manmohan Singh', type: 'PERSON' }] });
    expect(sharedEntities(a, b)).toEqual([]);
  });

  it('shared figures ignore thousands separators and one-digit numbers', () => {
    expect(sharedNumbers('1,152 farm fires and 5 villages in 2026', 'There were 1152 fires; 5 villages; year 2026.')).toEqual(['1152', '2026']);
  });

  it('shared words skip common words and short words, most repeated first', () => {
    const w = sharedWords('The gangster was deported. The gangster was wanted in Punjab and Punjab.', 'Punjab police said the gangster was brought back; the gangster faces trial.');
    expect(w[0]).toBe('gangster');
    expect(w).toContain('punjab');
    expect(w.some((x) => ['the', 'was', 'said', 'and'].includes(x))).toBe(false);
  });

  it('shows the closest passages only when the wording really overlaps', () => {
    const a = 'Punjab police secured the deportation of the wanted gangster Gurwinder Afridi from Ethiopia on Sunday morning. The weather stayed dry across the state today.';
    const b = 'The wanted gangster Gurwinder Afridi was deported from Ethiopia by Punjab police and brought to Chandigarh. Markets closed lower on a quiet trading session.';
    const found = similarPassages(a, b);
    expect(found).toHaveLength(1);
    expect(found[0].shared).toEqual(expect.arrayContaining(['gangster', 'ethiopia', 'punjab']));
    expect(similarPassages('Rain fell over the hills this evening and the farmers were relieved.', 'The central bank held its policy rate steady on Friday afternoon.')).toEqual([]);
  });

  it('similarContext and hasContext combine event type, place, names and wording', () => {
    const a = doc('A', { geo: { primary_location: 'Punjab' }, keyphrases: ['Punjab Police', 'gangster'] }, 'Gangster Afridi was deported to Chandigarh on Sunday.');
    const b = doc('B', { geo: { primary_location: 'punjab' }, keyphrases: ['punjab police'] }, 'Afridi, a gangster, reached Chandigarh on Sunday.');
    const c = similarContext(a, b);
    expect(c.eventType).toEqual({ a: 'Crime.Arrest', b: 'Crime.Arrest', same: true });
    expect(c.place.same).toBe(true);
    expect(c.keyphrases).toEqual(['Punjab Police']);
    expect(hasContext(c)).toBe(true);
    const none = similarContext(doc('X', { event: { type: 'Sports.Win', trigger: '', actor: null, institution: null, date: '', confidence: '' } }, 'Alpha beta.'), doc('Y', { geo: { primary_location: 'Goa' } }, 'Gamma delta.'));
    expect(none.eventType?.same).toBe(false);
    expect(hasContext(none)).toBe(false);
  });
});
