import type { Article, Capabilities, CleanArticle, Enrichment, Run, Story, StoryGraph } from '../types';

export const pdfFile = (name: string, size = 2048) => new File([new Uint8Array(size)], name, { type: 'application/pdf' });

export const caps: Capabilities = { ocr: 'rapidocr', translator: 'aws', embedder: 'hashing', index: 'memory', store: 'sqlite', graph: 'none', max_upload_mb: 250, max_papers: 10,
  stages: ['queued', 'ingest', 'ocr', 'clean', 'store'], enrich_stages: ['cluster', 'enrich', 'export', 'store'], enrichment_bus: 'kafka' };

export const story: Story = {
  story_id: 's1', suggested_story_key: 'punjab-assembly-resignation-2026-11', n_papers: 2, event_type: 'Politics.Resignation',
  location: 'Punjab', topic: 'politics', impact: 0.61, bias_label: 'consistent framing across papers',
  summary: 'Reported by 2 papers: Singh resigned from the Punjab Assembly.',
  coverage: [
    { paper: 'The Hindu', paper_id: 'the_hindu', page_no: 1, pages: [1, 3], headline: 'Punjab MLA resigns', translated_headline: null, byline: 'Staff Reporter',
      language: 'en', ocr_confidence: 0.98, event_id: 'a'.repeat(16), sentiment: 'neutral' },
    { paper: 'Dainik Jagran', paper_id: 'dainik_jagran', page_no: 1, pages: [1], headline: 'पंजाब विधायक ने दिया इस्तीफा', translated_headline: 'Punjab MLA resigns (en)',
      byline: null, language: 'hi', ocr_confidence: 0.91, event_id: 'b'.repeat(16), sentiment: 'negative' },
  ],
};
export const solo: Story = { ...story, story_id: 's2', suggested_story_key: 'metro-2026-11', n_papers: 1, event_type: 'Government.Approval', location: 'Delhi',
  coverage: [{ ...story.coverage[0], headline: 'Metro line extension gets nod', event_id: 'c'.repeat(16) }] };

export const article = (over: Partial<Article['newspaper']> = {}): Article => ({
  event_id: 'x', event: { type: 'Politics.Resignation', trigger: 'resigns', actor: 'Singh', institution: 'Punjab Assembly', date: '2026-11-02', confidence: 'high' },
  claims: [{ claim: 'Singh resigned on Monday.', checkworthy: true, verification_status: 'corroborated', next_step: '', corroborated_by: ['The Hindu'] }],
  keyphrases: [], summary: { one_line: 'x', multi_doc_summary: 'y' },
  newspaper: { paper: 'p', headline: 'h', translated_headline: null, text: 'Original para one.\n\nOriginal para two.', translated_text: null, translation_status: null, ...over },
});

export const enrichment = (over: Partial<Enrichment> = {}): Enrichment =>
  ({ state: 'done', stage: 'done', error: null, queued_at: 1, started_at: 1, finished_at: 2, ...over });

export const cleanArticle = (over: Partial<CleanArticle> = {}): CleanArticle => ({
  event_id: 'c'.repeat(16), paper: 'The Hindu', paper_id: 'the_hindu', page_no: 1, headline: 'Punjab MLA resigns', translated_headline: null,
  byline: 'Staff Reporter', language: 'en', translation_status: 'skipped_already_target', ocr_confidence: 0.98, status: 'ok', chars: 900,
  excerpt: 'Kunwar Vijay Pratap Singh resigned from the Punjab Assembly on Monday.', exact_duplicate_of: null, ...over });

export const run = (over: Partial<Run> = {}): Run => ({
  id: 'abcdefabcdef', created_at: 1, status: 'done', stage: 'done', run_date: '2026-11-02',
  papers: [{ id: 'the_hindu', name: 'The Hindu', language: 'en', edition: 'main', reputation: 'high', filename: 'a.pdf', size: 1 }],
  metrics: { run_date: '2026-11-02', papers: ['the_hindu'], papers_skipped_no_ocr: [], pages: 3, pages_scanned: 0, articles: 5, mean_ocr_confidence: 0.97,
    articles_flagged_review: 1, translated: 1, stories: 2, multi_paper_stories: 1, largest_story_papers: 2, borderline_matches: 0, stage_seconds: {} },
  error: null, finished_at: 2, enrich_after: true, enrichment: enrichment(), ...over,
});

export const storyGraph: StoryGraph = {
  story_id: 's1',
  articles: [{ event_id: 'a'.repeat(16), headline: 'Punjab MLA resigns', paper_id: 'the_hindu', paper: 'The Hindu', page: 1 }],
  entities: [{ id: 'kunwar_vijay_pratap_singh', name: 'Kunwar Vijay Pratap Singh', label: 'Person', mentioned_in: 4 },
             { id: 'punjab_assembly', name: 'Punjab Assembly', label: 'Organization', mentioned_in: 3 }],
  relations: [{ from_id: 'kunwar_vijay_pratap_singh', from_name: 'Kunwar Vijay Pratap Singh', rel: 'RESIGNED_FROM', to_id: 'punjab_assembly',
                to_name: 'Punjab Assembly', papers: ['The Hindu', 'The Times of India'], event_ids: ['x'], date: '2026-11-02' }],
};
