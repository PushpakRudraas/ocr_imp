import type { GraphEdge, GraphNode, GraphNodeType } from '../types';

export interface TypeInfo { type: GraphNodeType; label: string; color: string; shape: string; size: number; what: string }

/** Every node type of the relationship graph: how it is drawn and what it means. */
export const NODE_TYPES: TypeInfo[] = [
  { type: 'Paper', label: 'Newspaper', color: '#4c78ff', shape: 'round-rectangle', size: 46, what: 'A newspaper' },
  { type: 'Edition', label: 'Edition', color: '#7a5cff', shape: 'round-rectangle', size: 30, what: 'An edition of a newspaper (for example Delhi)' },
  { type: 'Day', label: 'Day', color: '#00a3a3', shape: 'diamond', size: 30, what: 'The date entered when the newspaper was uploaded' },
  { type: 'Page', label: 'Page', color: '#8d99a6', shape: 'rectangle', size: 22, what: 'A page of one newspaper on one day' },
  { type: 'Section', label: 'Section', color: '#c2a000', shape: 'hexagon', size: 26, what: 'A section such as Business or Nation (only when it was read)' },
  { type: 'Author', label: 'Author', color: '#f08c00', shape: 'ellipse', size: 22, what: 'A byline that passed the quality check' },
  { type: 'Article', label: 'Article', color: '#2fb170', shape: 'ellipse', size: 24, what: 'One article' },
  { type: 'Story', label: 'Story', color: '#d9480f', shape: 'round-hexagon', size: 36, what: 'Articles about the same event, grouped by the matching engine' },
  { type: 'Entity', label: 'Entity', color: '#d6409f', shape: 'ellipse', size: 20, what: 'A person, organisation, party or place' },
  { type: 'Topic', label: 'Topic', color: '#a0783c', shape: 'round-tag', size: 28, what: 'The article\'s topic' },
  { type: 'Event', label: 'Event', color: '#c2255c', shape: 'triangle', size: 26, what: 'A specific event reported (shared by the papers that report it)' },
  { type: 'Claim', label: 'Claim', color: '#5c7080', shape: 'round-rectangle', size: 20, what: 'A check-worthy statement' },
  { type: 'Keyword', label: 'Keyword', color: '#12b886', shape: 'round-tag', size: 20, what: 'A key phrase' },
];
export const TYPE_INFO = Object.fromEntries(NODE_TYPES.map((t) => [t.type, t])) as Record<GraphNodeType, TypeInfo>;

export const SIMILARITY_RELS = ['SIMILAR_TO', 'POSSIBLE_MATCH'];
export type PresetId = 'default' | 'similarity' | 'structure' | 'content';
/** Node types each preset shows (Article is always shown). */
export const PRESETS: Record<PresetId, { label: string; types: GraphNodeType[]; onlyLinked: boolean; help: string }> = {
  default: { label: 'Overview', types: ['Paper', 'Story'], onlyLinked: false, help: 'Newspapers, articles and stories' },
  similarity: { label: 'Similarity', types: [], onlyLinked: true, help: 'Only the articles that are linked by similarity' },
  structure: { label: 'Structure', types: ['Paper', 'Edition', 'Day', 'Page', 'Section'], onlyLinked: false, help: 'Newspapers, editions, days, pages and sections' },
  content: { label: 'Content', types: ['Topic', 'Entity', 'Event', 'Keyword'], onlyLinked: false, help: 'Topics, entities, events and keywords' },
};

export interface GraphData { nodes: GraphNode[]; edges: GraphEdge[] }

/** The graph with extra nodes and edges (from "expand") merged in; nothing is listed twice. */
export function mergeGraphs(base: GraphData, extra: GraphData): GraphData {
  const nodes = new Map(base.nodes.map((n) => [n.id, n]));
  extra.nodes.forEach((n) => { if (!nodes.has(n.id)) nodes.set(n.id, n); });
  const edges = new Map(base.edges.map((e) => [e.id, e]));
  extra.edges.forEach((e) => { if (nodes.has(e.source) && nodes.has(e.target) && !edges.has(e.id)) edges.set(e.id, e); });
  return { nodes: [...nodes.values()], edges: [...edges.values()] };
}

/** Adds nodes and edges to a collection, listing nothing twice and dropping nothing: a link may reach a node that is only on screen. */
export function appendGraph(cur: GraphData, more: GraphData): GraphData {
  const nodes = new Map(cur.nodes.map((n) => [n.id, n]));
  more.nodes.forEach((n) => { if (!nodes.has(n.id)) nodes.set(n.id, n); });
  const edges = new Map(cur.edges.map((e) => [e.id, e]));
  more.edges.forEach((e) => { if (!edges.has(e.id)) edges.set(e.id, e); });
  return { nodes: [...nodes.values()], edges: [...edges.values()] };
}

export const isSimilarity = (e: GraphEdge) => SIMILARITY_RELS.includes(e.type);

/** Hide the articles that have no similarity link (used by the Similarity view). */
export function onlyLinkedArticles(g: GraphData): GraphData {
  const linked = new Set<string>();
  g.edges.filter(isSimilarity).forEach((e) => { linked.add(e.source); linked.add(e.target); });
  const nodes = g.nodes.filter((n) => n.type !== 'Article' || linked.has(n.id));
  const keep = new Set(nodes.map((n) => n.id));
  return { nodes, edges: g.edges.filter((e) => keep.has(e.source) && keep.has(e.target)) };
}

/** Ids of the nodes whose label or any property contains the text (case-insensitive); null when there is nothing to search for. */
export function searchMatches(nodes: GraphNode[], query: string): Set<string> | null {
  const q = query.trim().toLowerCase();
  if (!q) return null;
  return new Set(nodes.filter((n) => `${n.label} ${Object.values(n.props).filter((v) => v != null).join(' ')}`.toLowerCase().includes(q)).map((n) => n.id));
}

export interface Neighbour { node: GraphNode; edge: GraphEdge; outgoing: boolean }
export function neighboursOf(id: string, g: GraphData): Neighbour[] {
  const by = new Map(g.nodes.map((n) => [n.id, n]));
  const out: Neighbour[] = [];
  g.edges.forEach((e) => {
    if (e.source === id && by.has(e.target)) out.push({ node: by.get(e.target)!, edge: e, outgoing: true });
    else if (e.target === id && by.has(e.source)) out.push({ node: by.get(e.source)!, edge: e, outgoing: false });
  });
  return out.sort((a, b) => a.node.type.localeCompare(b.node.type) || a.node.label.localeCompare(b.node.label));
}

const KEY_LABELS: Record<string, string> = {
  headline: 'Headline', page_no: 'Page', date: 'Date', published: 'Newspaper date', ocr_confidence: 'OCR confidence', language: 'Language',
  sentiment: 'Sentiment', event_type: 'Event type', name: 'Name', kind: 'Kind', paper_id: 'Newspaper id', key: 'Story key', location: 'Place',
  topic: 'Topic', impact: 'Impact', n_papers: 'Newspapers', label: 'Entity type', type: 'Type', trigger: 'Trigger', actor: 'Actor',
  institution: 'Institution', text: 'Text', status: 'Status', checkworthy: 'Check-worthy', reputation: 'Reputation',
};

/** The label / value rows shown for a node (empty values are left out). */
export function nodeDetails(n: GraphNode): [string, string][] {
  return Object.entries(n.props).filter(([, v]) => v !== null && v !== undefined && v !== '').map(([k, v]) => {
    const val = typeof v === 'number' && k === 'ocr_confidence' ? `${Math.round(v * 100)}%` : typeof v === 'number' && !Number.isInteger(v) ? v.toFixed(2) : String(v);
    return [KEY_LABELS[k] ?? k, val];
  });
}

export const scoreOf = (e: GraphEdge): number | null => (typeof e.props.score === 'number' ? e.props.score : null);
export const percent = (v: number) => `${Math.round(v * 100)}%`;

export const typeCounts = (nodes: GraphNode[]) => {
  const c: Partial<Record<GraphNodeType, number>> = {};
  nodes.forEach((n) => { c[n.type] = (c[n.type] ?? 0) + 1; });
  return c;
};

export type LayoutName = 'cose' | 'concentric' | 'circle' | 'breadthfirst';
export const LAYOUTS: [LayoutName, string][] = [['cose', 'Force layout'], ['concentric', 'Concentric'], ['breadthfirst', 'Tree'], ['circle', 'Circle']];
