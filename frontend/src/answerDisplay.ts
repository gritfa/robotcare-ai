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
