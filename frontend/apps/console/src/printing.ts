/**
 * 云打印机（设计文档 §29）：设置里的打印机、打印记录，以及加工页和单据详情里的打印按钮用到的文字。
 */
import type { Schemas } from '@edp/api-client'

export type Printer = Schemas['PrinterOut']
export type PrinterIn = Schemas['PrinterIn']
export type PrintJob = Schemas['PrintJobOut']
export type PrinterOption = Schemas['PrinterOption']
export type PrinterOptions = Schemas['PrinterOptions']
export type PrinterBrand = Printer['brand']
export type PrinterUse = Printer['uses'][number]
type TagType = 'success' | 'warning' | 'info' | 'danger' | 'primary'

export interface BrandInfo {
  value: PrinterBrand
  label: string
  accountLabel: string
  keyLabel: string
  snHint: string
  help: string
  needsDeviceKey: boolean
}

/** 支持的厂商和接入指引。 */
export const BRANDS: BrandInfo[] = [
  {
    value: 'xpyun',
    label: '芯烨云（XPrinter）',
    accountLabel: '开发者 ID',
    keyLabel: 'UserKEY',
    snHint: '机身底部标签上的编号，XPY 开头',
    help: '在芯烨云开放平台（admin.xpyun.net）注册开发者账号，"开发者信息"里有开发者 ID 和 UserKEY；打印机编号在机身底部的标签上。保存时会把打印机添加到这个开发者账号下并查一次状态。',
    needsDeviceKey: false,
  },
  {
    value: 'feie',
    label: '飞鹅云',
    accountLabel: '飞鹅云账号',
    keyLabel: 'UKEY',
    snHint: '机身标签上的编号',
    help: '在飞鹅云后台（admin.feieyun.com）注册账号，"个人中心"里有 UKEY；打印机编号和 KEY 都在机身标签上。保存时会把打印机添加到这个账号下并查一次状态。',
    needsDeviceKey: true,
  },
]

export function brandInfo(value: string): BrandInfo {
  return BRANDS.find((b) => b.value === value) ?? BRANDS[0]!
}

export const USE_LABEL: Record<PrinterUse, string> = {
  order: '工人领取订单后打印加工单',
  requisition: '开领料单后打印领料单',
}

export const USE_SHORT: Record<PrinterUse, string> = { order: '加工单', requisition: '领料单' }

export const PRINTER_STATUS_TAG: Record<Printer['status'], TagType> = {
  unknown: 'info',
  online: 'success',
  offline: 'danger',
  abnormal: 'warning',
  misconfigured: 'danger',
}

export const JOB_STATUS_TAG: Record<PrintJob['status'], TagType> = {
  queued: 'info',
  sent: 'primary',
  printed: 'success',
  retrying: 'warning',
  dead: 'danger',
}

/** 卡片和单据上的"已打印 N 次"。 */
export function printedText(count: number): string {
  return count > 0 ? `已打印 ${count} 次` : ''
}

/** 失败、放弃的任务可以重新发送。 */
export function canResend(job: Pick<PrintJob, 'status'>): boolean {
  return job.status === 'dead' || job.status === 'retrying'
}
