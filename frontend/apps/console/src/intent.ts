import type { Schemas } from '@edp/api-client'

/**
 * AI 意图判断（设计文档 §32）：下单意向 5 级、真实意图、在意什么、情绪和要不要人工。工作台的意图卡片、
 * 会话列表的标签、AI 判定记录和"试一试"共用这些名称和小工具（与后端 ai/intent.py 一致）。
 */
export type IntentJudgment = Schemas['IntentJudgmentOut']
export type SessionIntent = Schemas['SessionIntentOut']

export const STAGES = ['没有购买意向', '随便了解', '有兴趣', '意向明确', '准备下单'] as const

/** 阶段的颜色：没有意向灰色，了解和有兴趣蓝色，意向明确橙色，准备下单红色。 */
const STAGE_TONE = ['info', 'primary', 'primary', 'warning', 'danger'] as const
export type StageTone = (typeof STAGE_TONE)[number]

/** 内置的真实意图（租户自定义的编码是 "x:名称"）。 */
export const INTENT_LABEL: Record<string, string> = {
  product: '了解商品',
  price: '询价比价',
  purchase: '购买下单',
  delivery: '库存发货',
  order_status: '查订单',
  order_change: '改订单',
  after_sales: '售后',
  complaint: '投诉',
  invoice: '发票手续',
  cooperation: '合作代理',
  human: '要人工',
  other: '闲聊其他',
}

export const CONCERN_LABEL: Record<string, string> = {
  price: '价格',
  quality: '质量效果',
  delivery: '发货时效',
  service: '售后保障',
  trust: '是否可靠',
}

const EMOTIONS = ['平静', '有些着急', '生气激动'] as const

export function stageLabel(stage: number | null | undefined): string {
  return stage === null || stage === undefined ? '' : (STAGES[stage] ?? '')
}

export function stageTone(stage: number | null | undefined): StageTone {
  return stage === null || stage === undefined ? 'info' : (STAGE_TONE[stage] ?? 'info')
}

/** 会话列表的标签：只标出意向明确和准备下单的会话。 */
export function stageTag(
  stage: number | null | undefined,
): { label: string; type: 'warning' | 'danger' } | null {
  if (stage === null || stage === undefined || stage < 3) return null
  return { label: stageLabel(stage), type: stage >= 4 ? 'danger' : 'warning' }
}

export function intentName(code: string | null | undefined): string {
  if (!code) return ''
  if (code.startsWith('x:')) return code.slice(2)
  return INTENT_LABEL[code] ?? code
}

export function emotionLabel(value: number | null | undefined): string {
  if (value === null || value === undefined) return ''
  return EMOTIONS[value < 0.5 ? 0 : value < 1.5 ? 1 : 2]
}

export function percentText(value: number | null | undefined): string {
  return value === null || value === undefined ? '' : `${Math.round(value * 100)}%`
}

/** 下单意向的五格进度条：前 stage + 1 格点亮（没有意向时一格也不亮）。 */
export function meter(stage: number): boolean[] {
  return STAGES.map((_, index) => stage > 0 && index <= stage)
}

/** 一行概括，如"准备下单 86% · 真实意图：库存发货 · 在意：发货时效、价格"。 */
export function intentLine(judgment: IntentJudgment): string {
  const parts = [`${judgment.stage_label} ${percentText(judgment.stage_probability)}`]
  if (judgment.intent_label) parts.push(`真实意图：${judgment.intent_label}`)
  if (judgment.concerns.length) {
    parts.push(`在意：${judgment.concerns.map((c) => c.label).join('、')}`)
  }
  return parts.join(' · ')
}

/** 这次会话的变化，如"10:01 有兴趣 → 10:03 意向明确"；阶段没变的相邻几次合并成一次。 */
export function stageChanges(
  history: SessionIntent['history'],
  format: (iso: string) => string,
): { at: string; label: string; stage: number }[] {
  const changes: { at: string; label: string; stage: number }[] = []
  for (const point of history) {
    if (changes.length && changes[changes.length - 1]!.stage === point.stage) continue
    changes.push({ at: format(point.at), label: point.stage_label, stage: point.stage })
  }
  return changes
}

/** AI 判定留痕里用到的意图判断（signals.intent）和由它触发的转人工，写成一行说明。 */
export function decisionIntent(signals: Record<string, unknown>): string | null {
  const parts: string[] = []
  const judged = signals.intent as Record<string, unknown> | undefined
  if (judged && typeof judged.stage === 'number') {
    let line = `下单意向：${stageLabel(judged.stage)}`
    if (typeof judged.purchase === 'number') line += `（${percentText(judged.purchase)}）`
    parts.push(line)
    if (typeof judged.intent === 'string') parts.push(`真实意图：${intentName(judged.intent)}`)
    const concerns = Array.isArray(judged.concerns) ? (judged.concerns as string[]) : []
    if (concerns.length) {
      parts.push(`在意：${concerns.map((c) => CONCERN_LABEL[c] ?? c).join('、')}`)
    }
    if (typeof judged.emotion === 'number' && judged.emotion >= 0.5) {
      parts.push(`情绪：${emotionLabel(judged.emotion)}`)
    }
  }
  if (typeof signals.intent_human === 'number') {
    parts.push(`判断客户在要求人工（${percentText(signals.intent_human)}）`)
  }
  return parts.length ? parts.join(' · ') : null
}
