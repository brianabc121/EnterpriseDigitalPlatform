/**
 * 表单填写知识库（设计文档 §25.18）：知识库页面"表单知识"页签的文字和标签。
 */
import type { Schemas } from '@edp/api-client'

export type FormKbEntry = Schemas['FormKbEntryOut']
export type FormKbDetail = Schemas['FormKbEntryDetail']
export type FormKbRecord = Schemas['FormKbSubmissionOut']
export type FormKbKind = FormKbEntry['kind']
export type FormKbStatus = FormKbEntry['status']
export type FormKbForm = NonNullable<FormKbEntry['form']>
export type FormKbAction = Schemas['FormKbResult']['action']
export type FormKbReview = NonNullable<FormKbEntry['review']>
type TagType = 'success' | 'warning' | 'info' | 'danger' | 'primary'

export const KIND_LABEL: Record<FormKbKind, string> = {
  alias: '叫法',
  usage: '用量',
  companion: '搭配',
}

export const KIND_HINT: Record<FormKbKind, string> = {
  alias: '输入的文字（员工、客户的说法）对应的商品',
  usage: '成品每件用多少材料（一键领料时预填）',
  companion: '开这个商品时常一起开的商品',
}

export const STATUS_LABEL: Record<FormKbStatus, string> = {
  active: '生效',
  observing: '观察中',
  disabled: '已停用',
}

export const FORM_LABEL: Record<FormKbForm, string> = {
  order: '订单',
  requisition: '领料单',
  receipt: '入库单',
}

export const SOURCE_LABEL: Record<FormKbEntry['source'], string> = {
  learned: '学到的',
  manual: '手工添加',
}

export const BASIS_LABEL: Record<NonNullable<FormKbEntry['basis']>, string> = {
  estimate: '没有配方，按以往领料估算',
  extra: '配方里没有，常补领',
  deviation: '和配方不一致',
  recipe: '和配方一致',
}

export const REVIEW_LABEL: Record<FormKbReview, string> = {
  activate: '达到生效条件，确认后用上',
  conflict: '学到的和固定的不一致',
  recipe: '实际用量和配方不一致',
}

export const ACTION_LABEL: Record<FormKbAction, string> = {
  created: '学到',
  strengthened: '加强',
  activated: '生效',
  updated: '更新',
  deactivated: '回到观察中',
  review: '待确认',
  hit: '用到',
  added: '手工添加',
  edited: '修改',
  enabled: '启用',
  disabled: '停用',
  locked: '固定',
  unlocked: '取消固定',
  confirmed: '确认生效',
  adopted: '换成学到的',
  kept: '保持不变',
  recipe: '写进配方',
}

export function statusTag(status: FormKbStatus): TagType {
  if (status === 'active') return 'success'
  if (status === 'disabled') return 'info'
  return 'primary'
}

export function actionTag(action: FormKbAction): TagType {
  if (['activated', 'added', 'confirmed', 'adopted', 'recipe', 'enabled', 'hit'].includes(action)) {
    return 'success'
  }
  if (action === 'review') return 'warning'
  if (['deactivated', 'disabled', 'kept'].includes(action)) return 'info'
  return 'primary'
}

/** 学习记录里的表单动作：订单是下单、改单；领料单、入库单是开单、重新提交、确认。 */
export function eventText(record: Pick<FormKbRecord, 'form' | 'event'>): string {
  if (record.form === 'order') return record.event === 'updated' ? '改单' : '下单'
  if (record.event === 'confirmed') return '确认'
  return record.event === 'updated' ? '重新提交' : '开单'
}

/** 依据：叫法是几次（选这个商品的比例）；用量是几个订单；搭配是几张单据一起开。 */
export function evidenceText(entry: Pick<FormKbEntry, 'kind' | 'evidence' | 'share' | 'source'>): string {
  if (entry.source === 'manual' && !entry.evidence) return '手工填写'
  if (entry.kind === 'usage') return `${entry.evidence} 个订单`
  if (entry.kind === 'companion') return `${entry.evidence} 张一起开`
  const share = entry.share === null ? '' : `（占 ${Math.round(entry.share * 100)}%）`
  return `${entry.evidence} 次${share}`
}

/** 待确认的知识可以怎么处理（更新配方另有按钮）。 */
export function reviewDecisions(
  review: FormKbReview | null,
): { decision: 'activate' | 'adopt' | 'keep'; label: string; primary: boolean }[] {
  if (review === 'activate') {
    return [
      { decision: 'activate', label: '确认生效', primary: true },
      { decision: 'keep', label: '不生效', primary: false },
    ]
  }
  if (review === 'conflict') {
    return [
      { decision: 'adopt', label: '换成学到的', primary: true },
      { decision: 'keep', label: '保持不变', primary: false },
    ]
  }
  if (review === 'recipe') return [{ decision: 'keep', label: '保持配方不变', primary: false }]
  return []
}

/** 学习记录的判断结果一句话：还没判断、判断失败、没有需要更新的，或者几处更新。 */
export function recordSummary(record: Pick<FormKbRecord, 'status' | 'result'>): string {
  if (record.status === 'pending') return '正在判断…'
  if (record.status === 'failed') return '判断失败（已重试 3 次）'
  if (!record.result.length) return '没有需要更新的'
  return `${record.result.length} 处更新`
}
