import type { KnowledgeAnswer } from './types'

/** 拒答原因 → 会计/用户可读文案。所有拒答都不展示模型原始输出。 */
export function refusalPresentation(reason: KnowledgeAnswer['refusal_reason']): {
  title: string
  description: string
  type: 'info' | 'warning'
} {
  switch (reason) {
    case 'knowledge_gap':
      return {
        title: '资料不足，未生成回答',
        description: '当前型号的资料中没有足够依据回答这个问题。系统不会编造答案，请调整问题描述或使用安全诊断流程。',
        type: 'info',
      }
    case 'model_refused':
      return {
        title: '资料无法支撑回答',
        description: '检索到的片段不足以可靠回答，已按拒答处理。可补充错误码或具体现象后重试。',
        type: 'info',
      }
    case 'citation_invalid':
      return {
        title: '回答未通过引用校验',
        description: '生成结果没有逐条标注可靠来源，已被系统拦截。请重试一次或改用资料检索。',
        type: 'warning',
      }
    case 'unsafe_answer':
      return {
        title: '回答涉及不安全操作，已拦截',
        description: '生成内容涉及拆机或其他高风险操作，系统已拦截并留痕。请联系官方售后处理。',
        type: 'warning',
      }
    default:
      return {
        title: '未生成回答',
        description: '系统未能生成可靠回答，请稍后重试。',
        type: 'info',
      }
  }
}

/** 引用编号在正文与角标里必须一一对应，且从 1 起连续。
 *
 * 后端的 `index` 是**检索片段序号**：模型只引用了第 1、2、3、5 条片段时，
 * 角标就显示 [1][2][3][5]，用户看到的是"第 4 条哪去了"。
 *
 * 重排放在展示层而不是后端：`citations_json` 已经落库，是数据契约（改了会让
 * 历史消息的正文标记与角标错位）。展示层重排则新旧消息都能修好。
 *
 * **正文和角标必须成对改**：只重排角标而不动正文里的 `[5]`，角标 [4] 和正文
 * [5] 就彻底对不上，比跳号更糟。
 */
export function renumberCitations<T extends { index: number }>(
  content: string,
  citations: readonly T[],
): { content: string; citations: T[] } {
  if (!citations.length) return { content, citations: [] }

  const ordered = [...citations].sort((a, b) => a.index - b.index)
  const remap = new Map<number, number>()
  ordered.forEach((citation, position) => remap.set(citation.index, position + 1))

  // 一次性替换：逐个 replace 会连环覆盖（[2]→[1] 之后再处理 [1] 就找错了目标）
  const rewritten = content.replace(/\[(\d{1,2})\]/g, (whole, digits) => {
    const next = remap.get(Number(digits))
    return next === undefined ? whole : `[${next}]`
  })

  return {
    content: rewritten,
    citations: ordered.map((citation, position) => ({ ...citation, index: position + 1 })),
  }
}
