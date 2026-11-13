import cytoscape, { type Core, type ElementDefinition } from 'cytoscape';
import { useEffect, useRef } from 'react';
import { isSimilarity, NODE_TYPES, type LayoutName } from '../../lib/graph';
import type { GraphEdge, GraphNode } from '../../types';

interface Props {
  nodes: GraphNode[];
  edges: GraphEdge[];
  layout: LayoutName;
  selectedId: string | null;
  highlight: Set<string> | null;           // ids matching the search; null = no search
  fitSignal: number;                       // bump to re-fit the view
  onSelect: (id: string | null) => void;
}

const cssVar = (name: string, fallback: string) => {
  try { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback; } catch { return fallback; }
};

function elements(nodes: GraphNode[], edges: GraphEdge[]): ElementDefinition[] {
  const score = (e: GraphEdge) => (typeof e.props.score === 'number' ? e.props.score : 0);
  return [
    ...nodes.map((n) => ({ group: 'nodes' as const, data: { id: n.id, label: n.label, type: n.type } })),
    ...edges.map((e) => ({
      group: 'edges' as const,
      data: { id: e.id, source: e.source, target: e.target, type: e.type, score: score(e), sim: isSimilarity(e) ? 1 : 0, possible: e.type === 'POSSIBLE_MATCH' ? 1 : 0 },
    })),
  ];
}

function style(): cytoscape.StylesheetStyle[] {
  const text = cssVar('--text', '#e8eaee'), bg = cssVar('--bg', '#0f1216'), muted = cssVar('--muted', '#98a1b0'), accent = cssVar('--accent', '#6c93ff');
  return [
    { selector: 'node', style: { label: 'data(label)', color: text, 'font-size': 10, 'text-wrap': 'ellipsis', 'text-max-width': '110px', 'text-valign': 'bottom',
        'text-margin-y': 3, 'text-background-color': bg, 'text-background-opacity': 0.7, 'text-background-padding': '1px', 'min-zoomed-font-size': 8,
        'border-width': 1, 'border-color': bg, width: 24, height: 24 } },
    ...NODE_TYPES.map((t) => ({ selector: `node[type = "${t.type}"]`, style: { 'background-color': t.color, shape: t.shape as cytoscape.Css.Node['shape'], width: t.size, height: t.size } })),
    { selector: 'edge', style: { width: 1, 'line-color': muted, opacity: 0.45, 'curve-style': 'bezier', 'target-arrow-shape': 'none' } },
    { selector: 'edge[sim = 1]', style: { width: 'mapData(score, 0.5, 1, 3, 9)', 'line-color': '#2fb170', opacity: 0.9, 'z-index': 5 } },
    { selector: 'edge[possible = 1]', style: { 'line-style': 'dashed', 'line-color': '#f0a020' } },
    { selector: '.dim', style: { opacity: 0.12 } },
    { selector: '.match', style: { 'border-width': 3, 'border-color': '#ffd43b', opacity: 1 } },
    { selector: 'node:selected', style: { 'border-width': 4, 'border-color': accent } },
    { selector: 'edge:selected', style: { 'line-color': accent, opacity: 1 } },
  ];
}

/** The drawn graph (Cytoscape.js on a canvas). All data comes from props; this component only draws, lays out and reports clicks. */
export default function GraphCanvas({ nodes, edges, layout, selectedId, highlight, fitSignal, onSelect }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const cy = useRef<Core | null>(null);
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;

  useEffect(() => {                                   // create once
    const c = cytoscape({ container: box.current, style: style(), wheelSensitivity: 0.25, minZoom: 0.1, maxZoom: 3 });
    c.on('tap', 'node', (ev) => onSelectRef.current(ev.target.id()));
    c.on('tap', (ev) => { if (ev.target === c) onSelectRef.current(null); });
    cy.current = c;
    (box.current as unknown as { __cy?: Core }).__cy = c;           // lets automated browser tests inspect the drawn graph
    const ro = typeof ResizeObserver !== 'undefined' && box.current ? new ResizeObserver(() => c.resize()) : null;
    if (ro && box.current) ro.observe(box.current);
    return () => { ro?.disconnect(); c.destroy(); cy.current = null; };
  }, []);

  useEffect(() => {                                   // data or layout changed
    const c = cy.current;
    if (!c) return;
    c.batch(() => { c.elements().remove(); c.add(elements(nodes, edges)); });
    if (nodes.length > 0) {
      c.layout({ name: layout, animate: false, fit: true, padding: 30, ...(layout === 'cose' ? { nodeRepulsion: () => 9000, idealEdgeLength: () => 70, numIter: 600 } : {}),
        ...(layout === 'breadthfirst' ? { directed: false, spacingFactor: 0.9 } : {}) } as cytoscape.LayoutOptions).run();
    }
  }, [nodes, edges, layout]);

  useEffect(() => {                                   // search highlight
    const c = cy.current;
    if (!c) return;
    c.batch(() => {
      c.elements().removeClass('dim match');
      if (highlight) {
        c.nodes().forEach((n) => { n.addClass(highlight.has(n.id()) ? 'match' : 'dim'); });
        c.edges().addClass('dim');
      }
    });
  }, [highlight, nodes, edges]);

  useEffect(() => {                                   // selection
    const c = cy.current;
    if (!c) return;
    c.elements().unselect();
    if (selectedId) c.getElementById(selectedId).select();
  }, [selectedId, nodes]);

  useEffect(() => { cy.current?.fit(undefined, 30); }, [fitSignal]);

  return (
    <div ref={box} className="graph-canvas" role="img" aria-label={`Relationship graph with ${nodes.length} nodes and ${edges.length} links. A table of the same data is available.`}
         data-nodes={nodes.length} data-edges={edges.length} />
  );
}
