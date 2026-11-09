import { useEffect, useState } from 'react';
import { ApiError, getStoryGraph } from '../api';
import { humanize } from '../lib/guess';
import type { StoryGraph } from '../types';

/** Entities and relations from Neo4j for one story. Renders nothing when the server has no graph backend (404). */
export default function Relationships({ runId, storyId }: { runId: string; storyId: string }) {
  const [graph, setGraph] = useState<StoryGraph | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setGraph(null);
    setError(null);
    getStoryGraph(runId, storyId)
      .then((g) => live && setGraph(g))
      .catch((e) => live && e instanceof ApiError && e.status !== 404 && setError(e.message));
    return () => { live = false; };
  }, [runId, storyId]);

  if (error) return <p className="notice error">Relationship graph unavailable: {error}</p>;
  if (!graph || (graph.relations.length === 0 && graph.entities.length === 0)) return null;
  return (
    <section className="card relationships" aria-label="Relationships">
      <h3>Relationships <span className="muted small">from the graph database</span></h3>
      {graph.relations.length > 0 ? (
        <ul className="relations">
          {graph.relations.map((r) => (
            <li key={`${r.from_id}-${r.rel}-${r.to_id}`}>
              <strong>{r.from_name}</strong> <span className="chip rel">{humanize(r.rel.toLowerCase())}</span> <strong>{r.to_name}</strong>
              <span className="muted small"> - reported by {r.papers.join(', ')}{r.date ? ` (${r.date})` : ''}</span>
            </li>
          ))}
        </ul>
      ) : <p className="muted small">No relations were extracted for this story.</p>}
      <p className="small">
        <span className="muted">Mentioned entities: </span>
        {graph.entities.map((e) => (
          <span key={e.id} className="chip" title={e.label}>{e.name} <span className="muted">x{e.mentioned_in}</span></span>
        ))}
      </p>
    </section>
  );
}
