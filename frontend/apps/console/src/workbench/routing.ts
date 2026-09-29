/** 排队与路由的小工具（与后端 routing/priority.py 的档位一致）。 */

/** 每档 10 分：VIP 客户 20 起，投诉、情绪激动的客户 10 起；同一档内退回队列的往前排。 */
export const PRIORITY_TIER = 10

/** 排队列表上显示的优先级标记；普通客户不显示。 */
export function priorityTag(priority: number): string | null {
  const tier = Math.floor(priority / PRIORITY_TIER)
  if (tier >= 2) return 'VIP'
  if (tier === 1) return '优先'
  return null
}

/** 意图关键词：逗号（中英文）、顿号或空白分隔，去掉空的和重复的。 */
export function splitKeywords(text: string): string[] {
  return [
    ...new Set(
      text
        .split(/[,，、\s]+/)
        .map((k) => k.trim())
        .filter(Boolean),
    ),
  ]
}
