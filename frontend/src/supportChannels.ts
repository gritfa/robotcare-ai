/**
 * 官方售后入口的唯一事实源。
 *
 * 之前这个链接只硬编码在 SafetyBlockCard 里，而聊天页的「联系官方售后」快捷操作
 * 落到了指南页——那里没有任何联系方式，等于把最需要人接手的用户送进了死胡同。
 * 接入自建工单系统时只改这一处。
 */
export const OFFICIAL_SUPPORT_URL = 'https://www.haier.com/contact/'
export const OFFICIAL_SUPPORT_NAME = '海尔官方售后'

/** 打电话/提工单时对方一定会问型号，先摆给用户看，省得他再翻一遍。 */
export function supportHandoffHint(modelCode?: string | null): string {
  const model = modelCode ? `设备型号：${modelCode}` : '设备型号：可在机身铭牌查看'
  return `${model}\n本助手为第三方工具，不代表官方；转接后请以官方处理意见为准。`
}
