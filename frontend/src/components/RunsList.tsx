import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { deleteRun, listRuns } from '../api';
import type { Run } from '../types';

export default function RunsList() {
  const [runs, setRuns] = useState<Run[] | null>(null);

  const load = () => listRuns().then(setRuns).catch(() => setRuns([]));
  useEffect(() => { load(); }, []);

  if (!runs || runs.length === 0) return null;
  return (
    <section className="card">
      <h2>Previous runs</h2>
      <ul className="runs">
        {runs.map((r) => (
          <li key={r.id}>
            <Link to={`/runs/${r.id}`}>
              <strong>{r.run_date}</strong> - {r.papers.map((p) => p.name).join(', ')}
            </Link>
            <span className={`chip ${r.status}`}>{r.status}</span>
            {r.metrics && <span className="muted small">{r.metrics.multi_paper_stories} cross-paper stories</span>}
            {r.status !== 'running' && r.status !== 'queued' && (
              <button type="button" className="link-btn" aria-label={`Delete run ${r.id}`}
                      onClick={() => deleteRun(r.id).then(load).catch(() => undefined)}>delete</button>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
