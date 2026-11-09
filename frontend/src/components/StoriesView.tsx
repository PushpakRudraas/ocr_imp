import { useMemo, useState } from 'react';
import { humanize } from '../lib/guess';
import type { Story } from '../types';
import StoryDetail from './StoryDetail';

export default function StoriesView({ runId, stories }: { runId: string; stories: Story[] }) {
  const [multiOnly, setMultiOnly] = useState(true);
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<string | null>(null);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return stories.filter((s) => (!multiOnly || s.n_papers >= 2) &&
      (!q || s.coverage.some((c) => `${c.headline} ${c.translated_headline ?? ''}`.toLowerCase().includes(q)) || (s.location ?? '').toLowerCase().includes(q)));
  }, [stories, multiOnly, query]);

  const current = shown.find((s) => s.story_id === selected) ?? shown[0];
  return (
    <div className="stories">
      <aside className="story-list">
        <input type="search" placeholder="Search headlines or places" aria-label="Search stories" value={query} onChange={(e) => setQuery(e.target.value)} />
        <label className="inline small"><input type="checkbox" checked={multiOnly} onChange={(e) => setMultiOnly(e.target.checked)} /> Only stories in 2+ papers</label>
        {shown.length === 0 && <p className="empty small">No stories match.</p>}
        <ul aria-label="Stories">
          {shown.map((s) => (
            <li key={s.story_id}>
              <button type="button" className={s === current ? 'story-item active' : 'story-item'} onClick={() => setSelected(s.story_id)}>
                <span className="story-title">{s.coverage[0].translated_headline ?? s.coverage[0].headline}</span>
                <span className="muted small">{s.n_papers} paper{s.n_papers === 1 ? '' : 's'} - {humanize(s.event_type.split('.').pop() ?? '')}{s.location ? ` - ${s.location}` : ''}</span>
              </button>
            </li>
          ))}
        </ul>
      </aside>
      <div className="story-detail">{current ? <StoryDetail runId={runId} story={current} /> : <p className="empty">Select a story.</p>}</div>
    </div>
  );
}
