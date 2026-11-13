export type Reputation = 'high' | 'medium' | 'low' | 'not_rated';
export type RunStatus = 'queued' | 'running' | 'done' | 'failed';

export interface PaperMeta {
  id: string;
  name: string;
  language: string;
  edition: string;
  reputation: Reputation;
  filename: string;
  size: number;
}

/** Phase-1 numbers are always present once extraction is done; the story numbers only exist after enrichment. */
export interface Metrics {
  run_date: string;
  papers: string[];
  papers_skipped_no_ocr: string[];
  pages: number;
  pages_scanned: number;
  articles: number;
  mean_ocr_confidence: number | null;
  articles_flagged_review: number;
  translated: number;
  stories?: number;
  multi_paper_stories?: number;
  largest_story_papers?: number;
  borderline_matches?: number;
  stage_seconds: Record<string, number>;
}

/** The enrichment phase, tracked separately from the run's extraction `status`. */
export type EnrichState = 'off' | 'queued' | 'running' | 'done' | 'failed';

export interface Enrichment {
  state: EnrichState;
  stage: string | null;
  error: string | null;
  queued_at: number | null;
  started_at: number | null;
  finished_at: number | null;
}

/** `status`/`stage` describe extraction (PDF -> OCR -> clean + translate -> stored in MongoDB); `enrichment` the optional second phase. */
export interface Run {
  id: string;
  created_at: number;
  status: RunStatus;
  stage: string | null;
  run_date: string;
  papers: PaperMeta[];
  metrics: Metrics | null;
  error: string | null;
  finished_at: number | null;
  enrich_after: boolean;
  enrichment: Enrichment;
}

export interface Capabilities {
  ocr: string;
  translator: string;
  embedder: string;
  index: string;
  store: string;
  graph: string;
  max_upload_mb: number;
  max_papers: number;
  stages: string[];
  enrich_stages: string[];
  enrichment_bus: 'kafka' | 'inline';
}

/** One cleaned (and translated) article as stored after phase 1, before any enrichment. */
export interface CleanArticle {
  event_id: string;
  paper: string;
  paper_id: string;
  page_no: number | null;
  headline: string;
  translated_headline: string | null;
  byline: string | null;
  language: string | null;
  translation_status: string | null;
  ocr_confidence: number | null;
  status: string | null;
  chars: number;
  excerpt: string;
  exact_duplicate_of: string | null;
}

/** One raw OCR article in reading order, whether or not the clean filter kept it. */
export interface OcrArticle {
  article_id: string;
  paper: string;
  paper_id: string;
  page_no: number | null;
  headline: string;
  byline: string | null;
  language: string | null;
  ocr_confidence: number | null;
  status: string | null;
  quality_issues: string[];
  flags: string[];
  chars: number;
  text: string;
  bboxes: number[][];
  ad_label: 'news' | 'ad' | 'uncertain' | null;
  ad_reason: string | null;
  ad_score: number | null;
  marked?: 'ad' | 'news' | null;
  kept: boolean;
  removed_reason: string | null;
  duplicate_of: string | null;
}

export interface Coverage {
  paper: string;
  paper_id: string;
  page_no: number;
  pages: number[];
  headline: string;
  translated_headline: string | null;
  byline: string | null;
  language: string;
  ocr_confidence: number | null;
  event_id: string;
  sentiment: string;
}

export interface Story {
  story_id: string;
  suggested_story_key: string | null;
  n_papers: number;
  event_type: string;
  location: string | null;
  topic: string;
  impact: number;
  summary: string;
  bias_label: string;
  coverage: Coverage[];
}

export interface ReviewItem {
  event_id: string;
  paper: string;
  page_no: number;
  headline: string;
  reasons: string[];
  match_candidates: { event_id: string; score: number; reasons: string[] }[];
  ocr_confidence: number | null;
}

export interface Claim {
  claim: string;
  checkworthy: boolean;
  verification_status: string;
  next_step: string;
  corroborated_by?: string[];
}

export interface Article {
  event_id: string;
  event: { type: string; trigger: string; actor: string | null; institution: string | null; date: string; confidence: string };
  claims: Claim[];
  keyphrases: string[];
  summary: { one_line: string; multi_doc_summary: string };
  newspaper: {
    paper: string;
    text: string;
    translated_text: string | null;
    translated_headline: string | null;
    headline: string;
    translation_status: string | null;
  };
  [key: string]: unknown;
}

export interface UploadItem {
  key: string;
  file: File;
  name: string;
  language: string;
  edition: string;
  reputation: Reputation;
}

export interface GraphRelation {
  from_id: string;
  from_name: string;
  rel: string;
  to_id: string;
  to_name: string;
  papers: string[];
  event_ids: string[];
  date: string | null;
}

export interface GraphEntity {
  id: string;
  name: string;
  label: string;
  mentioned_in: number;
}

export interface StoryGraph {
  story_id: string;
  articles: { event_id: string; headline: string; paper_id: string; paper: string; page: number }[];
  entities: GraphEntity[];
  relations: GraphRelation[];
}

/** One region the YOLO ad model found, in PDF points. */
export interface PageRegion { id: number; label: 'ad' | 'article'; conf: number; bbox: [number, number, number, number] }
/** One layout block (DocLayout-YOLO: title, plain text, figure, ...), in PDF points. */
export interface PageBlock { id: number; cls: string; conf: number; bbox: [number, number, number, number] }
/** A page's size (PDF points), the ad candidates and layout blocks, for the page view. `ad_conf` = the threshold the pipeline used. */
export interface PageInfo {
  paper_id: string; page_no: number; width: number; height: number;
  yolo: boolean; layout?: boolean; ad_conf?: number; regions: PageRegion[]; blocks?: PageBlock[];
}
