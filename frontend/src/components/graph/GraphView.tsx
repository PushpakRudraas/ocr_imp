import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, getGraphNeighbors, getGraphSummary, getRunGraph } from '../../api';
import { humanize } from '../../lib/guess';
import {
  appendGraph, LAYOUTS, mergeGraphs, NODE_TYPES, neighboursOf, nodeDetails, onlyLinkedArticles, percent, PRESETS, scoreOf, searchMatches, TYPE_INFO,
  typeCounts, type GraphData, type LayoutName, type PresetId,
} from '../../lib/graph';
import type { GraphEdge, GraphNode, GraphNodeType, GraphScope, GraphSummary } from '../../types';
import GraphCanvas from './GraphCanvas';

const MAX_NODE_CHOICES = [100, 300, 500, 1000, 2000];
const OPTIONAL_TYPES = NODE_TYPES.filter((t) => t.type !== 'Article');

/** The "Graph" tab: the relationship graph of the run, read from Neo4j. Article nodes are always shown; every other node type, the
 *  similarity threshold, the size limit and the scope are chosen here. A table of the same data is available for accessibility. */
export default function GraphView({ runId, onOpenTab }: { runId: string; onOpenTab?: (tab: 'similar' | 'stories') => void }) {
  const [preset, setPreset] = useState<PresetId>('default');
  const [types, setTypes] = useState<Set<GraphNodeType>>(new Set(PRESETS.default.types));
  const [onlyLinked, setOnlyLinked] = useState(false);
  const [minScore, setMinScore] = useState(0);
  const [maxNodes, setMaxNodes] = useState(300);
  const [scope, setScope] = useState<GraphScope>('run');
  const [layout, setLayout] = useState<LayoutName>('cose');
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const [showTable, setShowTable] = useState(false);
  const [fit, setFit] = useState(0);

  const [summary, setSummary] = useState<GraphSummary | null>(null);
  const [base, setBase] = useState<GraphData & { truncated: boolean; total: number } | null>(null);
  const [extra, setExtra] = useState<GraphData>({ nodes: [], edges: [] });
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [expanding, setExpanding] = useState(false);

  const typesKey = [...types].sort().join(',');
  const fail = useCallback((e: unknown) => {
    setError(e instanceof ApiError ? (e.status === 404 ? 'The relationship graph is not enabled on this server.' : e.message) : 'Could not load the graph.');
  }, []);

  useEffect(() => {                                                       // legend counts
    let live = true;
    getGraphSummary(runId, scope).then((s) => live && setSummary(s)).catch((e) => live && fail(e));
    return () => { live = false; };
  }, [runId, scope, fail]);

  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => {                                                       // the graph itself (short delay so a slider drag is one request)
    let live = true;
    setLoading(true);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      getRunGraph(runId, { types: ['Article', ...(typesKey ? typesKey.split(',') : [])], minScore, maxNodes, scope })
        .then((g) => { if (live) { setBase({ nodes: g.nodes, edges: g.edges, truncated: g.truncated, total: g.total_nodes }); setExtra({ nodes: [], edges: [] }); setError(null); setSelected(null); } })
        .catch((e) => live && fail(e))
        .finally(() => live && setLoading(false));
    }, 200);
    return () => { live = false; clearTimeout(timer.current); };
  }, [runId, typesKey, minScore, maxNodes, scope, fail]);

  const graph: GraphData = useMemo(() => {
    const merged = base ? mergeGraphs(base, extra) : { nodes: [], edges: [] };
    return onlyLinked ? onlyLinkedArticles(merged) : merged;
  }, [base, extra, onlyLinked]);
  const matches = useMemo(() => searchMatches(graph.nodes, query), [graph.nodes, query]);
  const sel = graph.nodes.find((n) => n.id === selected) ?? null;
  const near = useMemo(() => (selected ? neighboursOf(selected, graph) : []), [selected, graph]);

  const applyPreset = (id: PresetId) => { setPreset(id); setTypes(new Set(PRESETS[id].types)); setOnlyLinked(PRESETS[id].onlyLinked); };
  const toggle = (t: GraphNodeType) => { setPreset('default'); setTypes((cur) => { const n = new Set(cur); if (n.has(t)) n.delete(t); else n.add(t); return n; }); };

  const expand = async (n: GraphNode) => {
    setExpanding(true);
    try {
      const r = await getGraphNeighbors(runId, n.id, ['Article', ...types], scope);
      setExtra((cur) => appendGraph(cur, r));
    } catch (e) { fail(e); } finally { setExpanding(false); }
  };

  if (error && !base) return <p className="notice warn" role="alert">{error}</p>;
  const counts = summary?.nodes_by_type ?? {};
  const shown = typeCounts(graph.nodes);
  const nSimilar = graph.edges.filter((e) => e.type === 'SIMILAR_TO').length, nPossible = graph.edges.filter((e) => e.type === 'POSSIBLE_MATCH').length;

  return (
    <div className="stack graph-view">
      <section className="card">
        <h2>Relationship graph</h2>
        <p className="muted small">
          How the newspapers, articles, stories and everything they mention are connected, read from the graph database. Green links join similar
          articles (thicker = higher score); dashed amber links are possible matches. The score is the matching engine&apos;s ranking score, not a probability.
        </p>
        <div role="group" aria-label="Views" className="filters">
          {(Object.keys(PRESETS) as PresetId[]).map((id) => (
            <button key={id} type="button" className={preset === id ? 'chip active' : 'chip'} aria-pressed={preset === id} title={PRESETS[id].help} onClick={() => applyPreset(id)}>{PRESETS[id].label}</button>
          ))}
        </div>
        <fieldset className="graph-types">
          <legend className="muted small">Show these node types (articles are always shown)</legend>
          {OPTIONAL_TYPES.map((t) => {
            const n = counts[t.type] ?? 0;
            return (
              <label key={t.type} className={`graph-type${n === 0 ? ' none' : ''}`} title={`${t.what}${n === 0 ? ' - none in this run' : ''}`}>
                <input type="checkbox" checked={types.has(t.type)} onChange={() => toggle(t.type)} />
                <span className="swatch" style={{ background: t.color }} aria-hidden="true" /> {t.label} <span className="muted small">{n}</span>
              </label>
            );
          })}
        </fieldset>
        <div className="filters">
          <label>Similarity at least <strong>{percent(minScore)}</strong>{' '}
            <input type="range" min={0} max={100} step={5} value={Math.round(minScore * 100)} aria-label="Minimum similarity" onChange={(e) => setMinScore(Number(e.target.value) / 100)} />
          </label>{' '}
          <label><input type="checkbox" checked={onlyLinked} onChange={(e) => setOnlyLinked(e.target.checked)} /> Only articles with similarity links</label>
        </div>
        <div className="filters">
          <label>Show up to{' '}
            <select value={maxNodes} onChange={(e) => setMaxNodes(Number(e.target.value))} aria-label="Maximum nodes">{MAX_NODE_CHOICES.map((n) => <option key={n} value={n}>{n} nodes</option>)}</select>
          </label>{' '}
          <label>Scope{' '}
            <select value={scope} onChange={(e) => setScope(e.target.value as GraphScope)} aria-label="Scope"><option value="run">This run</option><option value="all">All runs (most recent articles)</option></select>
          </label>{' '}
          <label>Layout{' '}
            <select value={layout} onChange={(e) => setLayout(e.target.value as LayoutName)} aria-label="Layout">{LAYOUTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
          </label>{' '}
          <input type="search" placeholder="Search nodes" aria-label="Search nodes" value={query} onChange={(e) => setQuery(e.target.value)} />
          {matches && <span className="muted small"> {matches.size} match{matches.size === 1 ? '' : 'es'}</span>}{' '}
          <button type="button" className="chip" onClick={() => setFit((n) => n + 1)}>Fit to screen</button>{' '}
          <button type="button" className={showTable ? 'chip active' : 'chip'} aria-pressed={showTable} onClick={() => setShowTable((v) => !v)}>Table view</button>
        </div>
      </section>

      {error && <p className="notice warn" role="alert">{error}</p>}
      <p className="small" role="status" aria-live="polite">
        {loading ? 'Loading the graph...' : base ? <>
          Showing <strong>{graph.nodes.length}</strong> nodes and <strong>{graph.edges.length}</strong> links
          {' '}({nSimilar} similar-to, {nPossible} possible match{nPossible === 1 ? '' : 'es'}).
          {base.truncated && <> <span className="chip warn">Showing the {maxNodes} best-connected of {base.total} nodes - narrow the node types or raise the limit.</span></>}
        </> : null}
      </p>

      {!loading && base && base.nodes.length === 0 && <p className="empty">There is no graph for this run yet.</p>}

      {showTable ? (
        <GraphTable nodes={graph.nodes} edges={graph.edges} onSelect={setSelected} />
      ) : (
        <div className="graph-layout">
          <GraphCanvas nodes={graph.nodes} edges={graph.edges} layout={layout} selectedId={selected} highlight={matches} fitSignal={fit} onSelect={setSelected} />
          <aside className="card graph-details" aria-label="Node details">
            {!sel ? (
              <>
                <h3>Legend</h3>
                <ul className="legend">
                  {NODE_TYPES.filter((t) => shown[t.type]).map((t) => (
                    <li key={t.type}><span className="swatch" style={{ background: t.color }} aria-hidden="true" /> {t.label} <span className="muted small">{shown[t.type]}</span></li>
                  ))}
                </ul>
                <p className="muted small">Click a node to see its details and what it is connected to. The Day node is the date entered when the newspaper was uploaded.</p>
              </>
            ) : (
              <NodeDetails node={sel} near={near} expanding={expanding} onExpand={() => void expand(sel)} onSelect={setSelected} onOpenTab={onOpenTab} onClose={() => setSelected(null)} />
            )}
          </aside>
        </div>
      )}
    </div>
  );
}

function NodeDetails({ node, near, expanding, onExpand, onSelect, onOpenTab, onClose }: {
  node: GraphNode; near: ReturnType<typeof neighboursOf>; expanding: boolean; onExpand: () => void; onSelect: (id: string) => void;
  onOpenTab?: (tab: 'similar' | 'stories') => void; onClose: () => void;
}) {
  const info = TYPE_INFO[node.type];
  return (
    <>
      <p className="muted small"><span className="swatch" style={{ background: info.color }} aria-hidden="true" /> {info.label}</p>
      <h3 style={{ overflowWrap: 'anywhere' }}>{node.label}</h3>
      <dl className="sim-meta graph-props">
        {nodeDetails(node).map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}
      </dl>
      <p className="small">
        <button type="button" className="chip" onClick={onExpand} disabled={expanding}>{expanding ? 'Expanding...' : 'Show more neighbours'}</button>{' '}
        <button type="button" className="chip" onClick={onClose}>Close</button>
      </p>
      {node.type === 'Article' && onOpenTab && (
        <p className="small">
          <button type="button" className="link-btn" onClick={() => onOpenTab('similar')}>Open Similar articles</button>
          <button type="button" className="link-btn" onClick={() => onOpenTab('stories')}>Open Stories</button>
        </p>
      )}
      <h4>Connected to ({near.length})</h4>
      <ul className="graph-near">
        {near.slice(0, 40).map((n) => (
          <li key={n.edge.id}>
            <span className="muted small">{n.outgoing ? '' : 'from '}{humanize(n.edge.type.toLowerCase())}{scoreOf(n.edge) !== null ? ` ${percent(scoreOf(n.edge)!)}` : ''}{n.outgoing ? ' to' : ''}</span>{' '}
            <button type="button" className="link-btn" onClick={() => onSelect(n.node.id)}><span className="swatch" style={{ background: TYPE_INFO[n.node.type].color }} aria-hidden="true" /> {n.node.label}</button>
          </li>
        ))}
        {near.length > 40 && <li className="muted small">and {near.length - 40} more</li>}
      </ul>
    </>
  );
}

function GraphTable({ nodes, edges, onSelect }: { nodes: GraphNode[]; edges: GraphEdge[]; onSelect: (id: string) => void }) {
  const label = useMemo(() => new Map(nodes.map((n) => [n.id, n.label])), [nodes]);
  return (
    <div className="stack">
      <table className="table" aria-label="Graph nodes">
        <thead><tr><th>Type</th><th>Label</th><th>Details</th></tr></thead>
        <tbody>
          {nodes.map((n) => (
            <tr key={n.id}>
              <td>{TYPE_INFO[n.type].label}</td>
              <td><button type="button" className="link-btn" onClick={() => onSelect(n.id)}>{n.label}</button></td>
              <td className="muted small">{nodeDetails(n).slice(0, 4).map(([k, v]) => `${k}: ${v}`).join(' - ')}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <table className="table" aria-label="Graph links">
        <thead><tr><th>From</th><th>Link</th><th>To</th><th>Score</th></tr></thead>
        <tbody>
          {edges.map((e) => (
            <tr key={e.id}>
              <td>{label.get(e.source) ?? e.source}</td><td>{humanize(e.type.toLowerCase())}</td><td>{label.get(e.target) ?? e.target}</td>
              <td>{scoreOf(e) !== null ? percent(scoreOf(e)!) : ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
