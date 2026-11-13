import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { story } from '../test/fixtures';
import type { Story } from '../types';
import MatchReasons from './MatchReasons';

const withMatches: Story = {
  ...story,
  coverage: [
    { ...story.coverage[0], match: null },
    { ...story.coverage[1], match: {
      score: 0.82, reasons: ['cos=0.80'], matched_with: 'The Hindu', matched_headline: 'Punjab MLA resigns', cosine: 0.8, veto: null,
      components: { semantic: 0.93, entity: 0.7, location: null, date: 0.7, keyword: 0.2 } } },
  ],
  possible_matches: [{
    event_id: 'z'.repeat(16), paper: 'Hindustan Times', headline: 'Opposition demands session', score: 0.61, reasons: [],
    veto: 'different places (Punjab vs Haryana)', for_event_id: 'a'.repeat(16),
    components: { semantic: 0.7, entity: 0.5, location: 0, date: 0.7, keyword: 0.3 } }],
};

describe('MatchReasons', () => {
  it('explains a match with its signals, marks weak and unstated ones, and lists borderline pairs', () => {
    render(<MatchReasons story={withMatches} />);
    const panel = screen.getByLabelText('Why these articles were grouped');
    expect(within(panel).getByText(/82% match/)).toBeInTheDocument();
    expect(panel).toHaveTextContent('Dainik Jagran matched The Hindu');
    expect(within(panel).getAllByText(/Meaning 93%/)[0]).toBeInTheDocument();
    expect(within(panel).getByText(/Place not stated/)).toBeInTheDocument();           // an article with no place counts for nothing
    expect(within(panel).getByText(/Keywords 20%/).className).toContain('warn');       // a weak signal is highlighted
    expect(within(panel).getByText(/Possibly the same story - please check \(1\)/)).toBeInTheDocument();
    expect(panel).toHaveTextContent('Kept apart by a rule: different places (Punjab vs Haryana)');
    expect(within(panel).getByText(/61% - not merged/)).toBeInTheDocument();
  });

  it('shows nothing when a story has no match information (older runs)', () => {
    const { container } = render(<MatchReasons story={story} />);
    expect(container).toBeEmptyDOMElement();
  });
});
