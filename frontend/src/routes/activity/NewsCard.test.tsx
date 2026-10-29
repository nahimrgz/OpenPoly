// @vitest-environment jsdom
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { NewsCard } from './NewsCard'
import type { NewsPipelineCard } from './newsTypes'

describe('NewsCard', () => {
  it('renders a news item whose sentiment is a text label', () => {
    // Regression: the backend sends tradingnews labels ("neutral"), and
    // calling toFixed on one threw and blanked the whole News tab.
    const card: NewsPipelineCard = {
      news: {
        id: 1,
        news_id: 'n1',
        content: 'Headline',
        urgency: 'medium',
        sentiment: 'neutral',
        published_at: 1_793_000_000,
        received_at: 1_793_000_001,
      },
      embedding: null,
      analyzer: null,
      entry: null,
      state: 'pending',
    }
    render(
      <MemoryRouter>
        <NewsCard card={card} />
      </MemoryRouter>,
    )
    expect(screen.getByText(/sentiment neutral/)).toBeTruthy()
  })
})
