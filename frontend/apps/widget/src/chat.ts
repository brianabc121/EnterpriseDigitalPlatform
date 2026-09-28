import type { ChatMessage } from '@edp/im-client'

export type SenderRole = 'me' | 'bot' | 'agent' | 'system' | 'other'

export const SENDER_LABEL: Record<SenderRole, string> = {
  me: '我',
  bot: '智能客服',
  agent: '客服',
  system: '系统消息',
  other: '',
}

/** 按平台的 IM ID 约定判断发送者（见实施计划 §7.2）。 */
export function senderRole(sendID: string, myUserID: string): SenderRole {
  if (sendID === myUserID) return 'me'
  if (sendID.endsWith('_bot')) return 'bot'
  if (sendID.endsWith('_sys')) return 'system'
  if (/_s_[0-9a-f]{32}$/.test(sendID)) return 'agent'
  return 'other'
}

/** 合并消息：历史、实时推送和自己发出的消息可能重叠，按 clientMsgID 去重后按时间排序。 */
export function mergeMessages(current: ChatMessage[], incoming: ChatMessage[]): ChatMessage[] {
  const byId = new Map(current.map((m) => [m.clientMsgID, m]))
  for (const m of incoming) byId.set(m.clientMsgID, m)
  return [...byId.values()].sort((a, b) => a.sendTime - b.sendTime || a.seq - b.seq)
}
