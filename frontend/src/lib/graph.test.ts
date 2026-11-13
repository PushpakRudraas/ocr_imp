import { describe, expect, it } from 'vitest';
import type { GraphEdge, GraphNode } from '../types';
import { appendGraph, mergeGraphs, neighboursOf, NODE_TYPES, nodeDetails, onlyLinkedArticles, PRESETS, scoreOf, searchMatches, typeCounts } from './graph';

const node = (id: string, type: GraphNode['type'], label = id, props: Record<string, unknown> = {}): GraphNode => ({ id, type, label, props });
const edge = (source: string, target: string, type: string, props: Record<string, unknown> = {}): GraphEdge => ({ id: `${source}|${type}|${target}`, source, target, type, props });

const A = node('Article:a', 'Article', 'Gangster brought back from Ethiopia'), B = node('Article:b', 'Article', 'Wanted Punjab gangster'), C = node('Article:c', 'Article', 'Diwali farm fires');
const P = node('Paper:hindustan_times', 'Paper', 'Hindustan Times', { name: 'Hindustan Times', reputation: 'high' });

describe('graph model', () => {
  it('has the 13 requested node types, each with its own colour, and presets that only use known types', () => {
    expect(NODE_TYPES.map((t) => t.type).sort()).toEqual(['Article', 'Author', 'Claim', 'Day', 'Edition', 'Entity', 'Event', 'Keyword', 'Page', 'Paper', 'Section', 'Story', 'Topic']);
    expect(new Set(NODE_TYPES.map((t) => t.color)).size).toBe(13);
    const known = new Set(NODE_TYPES.map((t) => t.type));
    Object.values(PRESETS).forEach((p) => p.types.forEach((t) => expect(known.has(t)).toBe(true)));
    expect(PRESETS.similarity.onlyLinked).toBe(true);
    expect(Object.values(PRESETS).every((p) => !p.types.includes('Article'))).toBe(true);       // articles are always shown
  });

  it('merging adds new nodes and edges, never lists one twice, and drops edges whose end is missing', () => {
    const base = { nodes: [A, P], edges: [edge(A.id, P.id, 'PUBLISHED_IN')] };
    const extra = { nodes: [A, B], edges: [edge(A.id, P.id, 'PUBLISHED_IN'), edge(B.id, P.id, 'PUBLISHED_IN'), edge(B.id, 'Paper:ghost', 'PUBLISHED_IN')] };
    const m = mergeGraphs(base, extra);
    expect(m.nodes.map((n) => n.id)).toEqual([A.id, P.id, B.id]);
    expect(m.edges.map((e) => e.id)).toEqual([`${A.id}|PUBLISHED_IN|${P.id}`, `${B.id}|PUBLISHED_IN|${P.id}`]);
  });

  it('appending keeps a link whose other end is only on screen (an expanded node), and lists nothing twice', () => {
    const more = { nodes: [B], edges: [edge(A.id, B.id, 'SIMILAR_TO', { score: 0.8 })] };
    const once = appendGraph({ nodes: [], edges: [] }, more);
    expect(once.edges).toHaveLength(1);                                                              // A is not in `more`, yet the link stays
    expect(appendGraph(once, more)).toEqual(once);
    expect(mergeGraphs({ nodes: [A, P], edges: [] }, once).edges).toHaveLength(1);                   // and shows once both ends are there
  });

  it('the similarity view keeps only the articles that have a similarity or possible-match link', () => {
    const g = { nodes: [A, B, C, P], edges: [edge(A.id, B.id, 'SIMILAR_TO', { score: 0.8 }), edge(C.id, P.id, 'PUBLISHED_IN')] };
    const out = onlyLinkedArticles(g);
    expect(out.nodes.map((n) => n.id)).toEqual([A.id, B.id, P.id]);                                  // C has no similarity link; other node types stay
    expect(out.edges.map((e) => e.type)).toEqual(['SIMILAR_TO']);
    const possible = onlyLinkedArticles({ nodes: [A, C], edges: [edge(A.id, C.id, 'POSSIBLE_MATCH', { score: 0.6 })] });
    expect(possible.nodes).toHaveLength(2);
  });

  it('search matches labels and property values, ignores case, and returns null for an empty query', () => {
    const nodes = [A, B, P];
    expect(searchMatches(nodes, '   ')).toBeNull();
    expect([...searchMatches(nodes, 'GANGSTER')!]).toEqual([A.id, B.id]);
    expect([...searchMatches(nodes, 'high')!]).toEqual([P.id]);                                      // a property value (reputation)
    expect(searchMatches(nodes, 'nothing like this')!.size).toBe(0);
  });

  it('lists the neighbours of a node in both directions, sorted, ignoring edges to nodes that are not shown', () => {
    const g = { nodes: [A, B, P], edges: [edge(A.id, P.id, 'PUBLISHED_IN'), edge(B.id, A.id, 'SIMILAR_TO', { score: 0.86 }), edge(A.id, 'Story:x', 'PART_OF')] };
    const near = neighboursOf(A.id, g);
    expect(near.map((n) => [n.node.id, n.outgoing])).toEqual([[B.id, false], [P.id, true]]);
    expect(scoreOf(near[0].edge)).toBe(0.86);
    expect(scoreOf(near[1].edge)).toBeNull();
  });

  it('shows node details with readable names, a percentage for OCR confidence, and no empty values', () => {
    const d = nodeDetails(node('Article:a', 'Article', 'x', { headline: 'H', ocr_confidence: 0.987, page_no: 2, sentiment: null, language: '', impact: 0.5333 }));
    expect(d).toEqual([['Headline', 'H'], ['OCR confidence', '99%'], ['Page', '2'], ['Impact', '0.53']]);
  });

  it('counts the nodes of each type', () => {
    expect(typeCounts([A, B, P])).toEqual({ Article: 2, Paper: 1 });
  });
});
