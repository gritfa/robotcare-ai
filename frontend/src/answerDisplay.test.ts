import { describe, expect, it } from 'vitest'
import { refusalPresentation } from './answerDisplay'

describe('refusalPresentation', () => {
  it('maps every refusal reason to Chinese copy without leaking codes', () => {
    const reasons = ['knowledge_gap', 'model_refused', 'citation_invalid', 'unsafe_answer', null] as const
    for (const reason of reasons) {
      const presentation = refusalPresentation(reason)
      expect(presentation.title.length).toBeGreaterThan(3)
      expect(presentation.description.length).toBeGreaterThan(10)
      expect(presentation.title).not.toMatch(/[a-z_]{4,}/)
    }
  })

  it('marks intercepted answers as warnings', () => {
    expect(refusalPresentation('unsafe_answer').type).toBe('warning')
    expect(refusalPresentation('citation_invalid').type).toBe('warning')
    expect(refusalPresentation('knowledge_gap').type).toBe('info')
  })
})
