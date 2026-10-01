/**
 * 邮件渠道（设计文档 §10.8）：设置里的邮箱、工作台回复邮件用到的文字和小工具。
 */
import type { Schemas } from '@edp/api-client'

import type { WorkbenchMessage } from './workbench/messages'

export type MailAccount = Schemas['MailAccountOut']
export type MailProvider = Schemas['MailProviderOut']
export type MailSecurity = Schemas['MailServer']['security']
type TagType = 'success' | 'warning' | 'info' | 'danger' | 'primary'

export const SECURITY_LABEL: Record<MailSecurity, string> = {
  ssl: 'SSL',
  starttls: 'STARTTLS',
  none: '不加密（仅测试环境）',
}

/** 邮箱状态：正常收信时连续失败显示"连接失败"。 */
export function mailboxStatus(account: Pick<MailAccount, 'status' | 'failures'>): {
  label: string
  type: TagType
} {
  if (account.status === 'disabled') return { label: '已停用', type: 'info' }
  if (account.status === 'paused') return { label: '已暂停', type: 'danger' }
  if (account.failures > 0) return { label: '连接失败', type: 'warning' }
  return { label: '正常', type: 'success' }
}

/** 按地址的域名猜邮箱类型；猜不出时是"其他邮箱"（custom）。 */
export function guessProvider(providers: MailProvider[], address: string): string {
  const domain = address.split('@').pop()?.trim().toLowerCase() ?? ''
  if (!address.includes('@') || !domain) return 'custom'
  return providers.find((p) => p.domains.includes(domain))?.key ?? 'custom'
}

/** 忽略的发件人：一行一个（也可以用逗号、空格分开），去重、转小写。 */
export function parseIgnore(text: string): string[] {
  const items = text
    .split(/[\s,，;；]+/)
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean)
  return [...new Set(items)]
}

/** 不符合"完整地址或 @域名"格式的忽略规则。 */
export function invalidIgnore(items: string[]): string[] {
  return items.filter((item) => !/^[^@\s]*@[^@\s]+\.[^@\s]+$/.test(item))
}

const PREFIX = /^\s*((re|fw|fwd|aw|sv)\s*[:：]|(回复|答复|转发)\s*[:：])\s*/i

/** 去掉 "Re:"、"回复："、"Fwd:" 等前缀（与后端 mail/parse.base_subject 一致）。 */
export function baseSubject(subject: string): string {
  let previous: string | null = null
  let current = subject
  while (previous !== current) {
    previous = current
    current = current.replace(PREFIX, '')
  }
  return current.trim()
}

/** 回复的默认主题："Re: 原主题"；原邮件没有主题时"Re: 您的来信"。 */
export function replySubject(subject: string): string {
  const base = baseSubject(subject)
  return base ? `Re: ${base}` : 'Re: 您的来信'
}

/** 要回复的客户邮件：指定的那封，或者会话里最近的一封。 */
export function replyTarget(
  messages: WorkbenchMessage[],
  chosenId: string | null,
): WorkbenchMessage | null {
  const inbound = messages.filter((m) => m.email && m.senderType === 'customer')
  if (chosenId) return inbound.find((m) => m.id === chosenId) ?? null
  return inbound.at(-1) ?? null
}

/** 时间的简短显示：今天只显示时分，其他日子带月日。 */
export function shortTime(value: string | null | undefined): string {
  if (!value) return '—'
  const d = new Date(value)
  const hm = d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
  if (d.toDateString() === new Date().toDateString()) return hm
  return `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`
}
