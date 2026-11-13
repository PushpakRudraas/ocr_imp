import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '../../api';
import type { GraphEdge, GraphNode, RunGraph } from '../../types';
import GraphView from './GraphView';

vi.mock('../../api', async (orig) => ({ ...(await orig<typeof import('../../api')>()), getGraphSummary: vi.fn(), getRunGraph: vi.fn(), getGraphNeighbors: vi.fn() }));
// the canvas is drawn by Cytoscape in a real browser; here a stand-in exposes what it would draw and lets a test click a node
vi.mock('./GraphCanvas', () => ({
  default: (p: { nodes: GraphNode[]; edges: GraphEdge[]; layout: string; highlight: Set<string> | null; onSelect: (id: string | null) => void }) => (
    <div data-testid="canvas" data-nodes={p.nodes.length} data-edges={p.edges.length} data-layout={p.layout} data-highlight={p.highlight ? [...p.highlight].join(',') : ''}>
      {p.nodes.map((n) => <button key={n.id} type="button" onClick={() => p.onSelect(n.id)}>{`node ${n.label}`}</button>)}
    </div>
  ),
}));
const m = vi.mocked(api);

const node = (id: string, type: GraphNode['type'], label: string, props: Record<string, unknown> = {}): GraphNode => ({ id, type, label, props });
const edge = (source: string, target: string, type: string, props: Record<string, unknown> = {}): GraphEdge => ({ id: `${source}|${type}|${target}`, source, target, type, props });

const A = node('Article:a', 'Article', 'Gangster brought back', { headline: 'Gangster brought back', ocr_confidence: 0.98, page_no: 6 });
const B = node('Article:b', 'Article', 'Wanted Punjab gangster', { page_no: 1 });
const C = node('Article:c', 'Article', 'Farm fires jump');
const HT = node('Paper:ht', 'Paper', 'Hindustan Times', { name: 'Hindustan Times' });
const S = node('Story:s', 'Story', 'punjab-gangster');
const graph = (over: Partial<RunGraph> = {}): RunGraph => ({
  run_id: 'r1', scope: 'run', max_nodes: 300, truncated: false, total_nodes: 5,
  nodes: [A, B, C, HT, S],
  edges: [edge(A.id, HT.id, 'PUBLISHED_IN'), edge(A.id, S.id, 'PART_OF'), edge(B.id, A.id, 'SIMILAR_TO', { score: 0.8 }), edge(C.id, A.id, 'POSSIBLE_MATCH', { score: 0.6 })],
  ...over,
});

beforeEach(() => {
  vi.clearAllMocks();
  m.getGraphSummary.mockResolvedValue({ run_id: 'r1', scope: 'run', nodes_by_type: { Article: 3, Paper: 1, Story: 1, Entity: 12, Keyword: 7, Author: 0 }, links_by_type: { SIMILAR_TO: 1 } });
  m.getRunGraph.mockResolvedValue(graph());
});

const canvas = () => screen.getByTestId('canvas');
const lastCall = () => m.getRunGraph.mock.calls[m.getRunGraph.mock.calls.length - 1][1];

describe('GraphView', () => {
  it('loads the run graph with the default view and shows what is drawn, with counts per node type', async () => {
    render(<GraphView runId="r1" />);
    await waitFor(() => expect(canvas()).toHaveAttribute('data-nodes', '5'));
    expect(m.getRunGraph).toHaveBeenCalledWith('r1', { types: ['Article', 'Paper', 'Story'], minScore: 0, maxNodes: 300, scope: 'run' });
    expect(screen.getByRole('status')).toHaveTextContent('Showing 5 nodes and 4 links (1 similar-to, 1 possible match)');
    const entity = screen.getByLabelText(/Entity/);
    expect(entity).not.toBeChecked();
    expect(within(entity.closest('label')!).getByText('12')).toBeInTheDocument();                    // the count from the summary
    expect(screen.getByLabelText(/Author/).closest('label')).toHaveClass('none');                    // none in this run: greyed
    expect(screen.queryByLabelText(/^Article/)).toBeNull();                                           // articles cannot be switched off
  });

  it('switching a node type on asks the server for it', async () => {
    const u = userEvent.setup();
    render(<GraphView runId="r1" />);
    await waitFor(() => expect(canvas()).toHaveAttribute('data-nodes', '5'));
    await u.click(screen.getByLabelText(/Entity/));
    await waitFor(() => expect(lastCall().types).toEqual(['Article', 'Entity', 'Paper', 'Story']));
    expect(screen.getByLabelText(/Entity/)).toBeChecked();
  });

  it('the views change the node types, and the Similarity view hides articles with no similarity link', async () => {
    const u = userEvent.setup();
    render(<GraphView runId="r1" />);
    await waitFor(() => expect(canvas()).toHaveAttribute('data-nodes', '5'));
    await u.click(screen.getByRole('button', { name: 'Structure' }));
    await waitFor(() => expect(lastCall().types).toEqual(['Article', 'Day', 'Edition', 'Page', 'Paper', 'Section']));
    await u.click(screen.getByRole('button', { name: 'Similarity' }));
    await waitFor(() => expect(lastCall().types).toEqual(['Article']));
    expect(screen.getByLabelText(/Only articles with similarity links/)).toBeChecked();
    m.getRunGraph.mockResolvedValue(graph({ nodes: [A, B, C, node('Article:lonely', 'Article', 'Lonely')] }));
    await u.click(screen.getByRole('button', { name: 'Content' }));
    await u.click(screen.getByRole('button', { name: 'Similarity' }));
    await waitFor(() => expect(canvas()).toHaveAttribute('data-nodes', '3'));                         // the article without a similarity link is hidden
    expect(screen.queryByText('node Lonely')).toBeNull();
  });

  it('the similarity slider, the size limit and the scope go to the server', async () => {
    const u = userEvent.setup();
    render(<GraphView runId="r1" />);
    await waitFor(() => expect(canvas()).toHaveAttribute('data-nodes', '5'));
    fireEvent.change(screen.getByLabelText('Minimum similarity'), { target: { value: '80' } });
    await waitFor(() => expect(lastCall().minScore).toBe(0.8));
    await u.selectOptions(screen.getByLabelText('Maximum nodes'), '1000');
    await waitFor(() => expect(lastCall().maxNodes).toBe(1000));
    await u.selectOptions(screen.getByLabelText('Scope'), 'all');
    await waitFor(() => expect(lastCall().scope).toBe('all'));
    expect(m.getGraphSummary).toHaveBeenLastCalledWith('r1', 'all');
  });

  it('says so when the server cut the graph down to the cap', async () => {
    m.getRunGraph.mockResolvedValue(graph({ truncated: true, total_nodes: 2140 }));
    render(<GraphView runId="r1" />);
    expect(await screen.findByText(/Showing the 300 best-connected of 2140 nodes/)).toBeInTheDocument();
  });

  it('search highlights the matching nodes and counts them', async () => {
    const u = userEvent.setup();
    render(<GraphView runId="r1" />);
    await waitFor(() => expect(canvas()).toHaveAttribute('data-nodes', '5'));
    expect(canvas()).toHaveAttribute('data-highlight', '');
    await u.type(screen.getByLabelText('Search nodes'), 'gangster');
    expect(canvas()).toHaveAttribute('data-highlight', 'Article:a,Article:b,Story:s');               // two articles and the story whose label contains the word
    expect(screen.getByText(/3 matches/)).toBeInTheDocument();
  });

  it('clicking a node shows its details and neighbours, and "Show more neighbours" adds them to the graph', async () => {
    const u = userEvent.setup();
    const onOpenTab = vi.fn();
    m.getGraphNeighbors.mockResolvedValue({ nodes: [node('Entity:ethiopia', 'Entity', 'Ethiopia', { name: 'Ethiopia', label: 'Location' })],
      edges: [edge(A.id, 'Entity:ethiopia', 'MENTIONS')] });
    render(<GraphView runId="r1" onOpenTab={onOpenTab} />);
    await waitFor(() => expect(canvas()).toHaveAttribute('data-nodes', '5'));
    expect(screen.getByText('Legend')).toBeInTheDocument();
    await u.click(screen.getByRole('button', { name: 'node Gangster brought back' }));
    const panel = within(screen.getByLabelText('Node details'));
    expect(panel.getByRole('heading', { name: 'Gangster brought back' })).toBeInTheDocument();
    expect(panel.getByText('OCR confidence').nextSibling).toHaveTextContent('98%');
    expect(panel.getByText('Connected to (4)')).toBeInTheDocument();
    expect(panel.getByRole('button', { name: /Wanted Punjab gangster/ })).toBeInTheDocument();
    expect(panel.getByText(/80%/)).toBeInTheDocument();                                                // the similarity score on the link

    await u.click(panel.getByRole('button', { name: 'Show more neighbours' }));
    expect(m.getGraphNeighbors).toHaveBeenCalledWith('r1', 'Article:a', ['Article', 'Paper', 'Story'], 'run');
    await waitFor(() => expect(canvas()).toHaveAttribute('data-nodes', '6'));
    expect(canvas()).toHaveAttribute('data-edges', '5');

    await u.click(panel.getByRole('button', { name: 'Open Similar articles' }));
    expect(onOpenTab).toHaveBeenCalledWith('similar');
    await u.click(panel.getByRole('button', { name: 'Close' }));
    expect(screen.getByText('Legend')).toBeInTheDocument();
  });

  it('the table view lists the same nodes and links for readers who cannot use the canvas', async () => {
    const u = userEvent.setup();
    render(<GraphView runId="r1" />);
    await waitFor(() => expect(canvas()).toHaveAttribute('data-nodes', '5'));
    await u.click(screen.getByRole('button', { name: 'Table view' }));
    expect(screen.queryByTestId('canvas')).toBeNull();
    const nodes = within(screen.getByRole('table', { name: 'Graph nodes' })).getAllByRole('row');
    expect(nodes).toHaveLength(1 + 5);
    const links = within(screen.getByRole('table', { name: 'Graph links' }));
    expect(links.getAllByRole('row')).toHaveLength(1 + 4);
    expect(links.getByText('80%')).toBeInTheDocument();
  });

  it('shows a clear message when the graph is not enabled, and when the run has no graph yet', async () => {
    m.getRunGraph.mockRejectedValue(new api.ApiError('not enabled', 404));
    m.getGraphSummary.mockRejectedValue(new api.ApiError('not enabled', 404));
    const first = render(<GraphView runId="r1" />);
    expect(await screen.findByRole('alert')).toHaveTextContent('not enabled on this server');
    first.unmount();
    m.getRunGraph.mockResolvedValue(graph({ nodes: [], edges: [], total_nodes: 0 }));
    m.getGraphSummary.mockResolvedValue({ run_id: 'r1', scope: 'run', nodes_by_type: {}, links_by_type: {} });
    render(<GraphView runId="r2" />);
    expect(await screen.findByText('There is no graph for this run yet.')).toBeInTheDocument();
  });
});
