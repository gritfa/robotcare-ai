import { describe, expect, it } from 'vitest'
import { refusalPresentation, renumberCitations } from './answerDisplay'

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

describe('renumberCitations', () => {
  const cite = (index: number) => ({ index, page_number: index * 3 })

  it('把跳号的引用压成连续编号，正文与角标同步', () => {
    const result = renumberCitations(
      '先检查水箱[1]，再清理拖布[3]，必要时更换滤网[5]。',
      [cite(1), cite(3), cite(5)],
    )

    expect(result.content).toBe('先检查水箱[1]，再清理拖布[2]，必要时更换滤网[3]。')
    expect(result.citations.map((c) => c.index)).toEqual([1, 2, 3])
    // 重排只改编号，页码等原始信息必须原样保留
    expect(result.citations.map((c) => c.page_number)).toEqual([3, 9, 15])
  })

  it('已经连续时不做任何改动', () => {
    const result = renumberCitations('步骤一[1]，步骤二[2]。', [cite(1), cite(2)])

    expect(result.content).toBe('步骤一[1]，步骤二[2]。')
    expect(result.citations.map((c) => c.index)).toEqual([1, 2])
  })

  it('同一编号在正文中出现多次时全部替换', () => {
    const result = renumberCitations('先看[3]，后面还要回到[3]确认。', [cite(3)])

    expect(result.content).toBe('先看[1]，后面还要回到[1]确认。')
  })

  it('不误伤正文里不属于引用的方括号数字', () => {
    const result = renumberCitations('型号[9]未被引用，只引用了[2]。', [cite(2)])

    expect(result.content).toBe('型号[9]未被引用，只引用了[1]。')
  })

  it('没有引用时原样返回', () => {
    const result = renumberCitations('资料不足，无法回答。', [])

    expect(result.content).toBe('资料不足，无法回答。')
    expect(result.citations).toEqual([])
  })

  it('不修改传入的原数组（组件里可能仍持有原引用）', () => {
    const original = [cite(2), cite(7)]
    renumberCitations('[2][7]', original)

    expect(original.map((c) => c.index)).toEqual([2, 7])
  })
})
