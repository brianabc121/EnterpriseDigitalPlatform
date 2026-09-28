import type { Schemas } from '@edp/api-client'
import type { ChatMessage } from '@edp/im-client'

export type SenderRole = 'me' | 'bot' | 'agent' | 'system' | 'other'

export const SENDER_LABEL: Record<SenderRole, string> = {
  me: '我',
  bot: '智能客服',
  agent: '客服',
  system: '系统消息',
  other: '',
}

/** Widget 展示的消息：来自 IM 推送、自己发送的返回值，或平台的历史接口。 */
export interface WidgetMessage {
  /** IM 消息 ID；自己刚发出、还没有 IM 消息 ID 时用客户端 ID。 */
  key: string
  serverMsgID: string
  clientMsgID: string
  role: SenderRole
  senderName: string | null
  text: string | null
  sendTime: number
}

/** 按平台的 IM ID 约定判断发送者（见实施计划 §7.2）。 */
export function senderRole(sendID: string, myUserID: string): SenderRole {
  if (sendID === myUserID) return 'me'
  if (sendID.endsWith('_bot')) return 'bot'
  if (sendID.endsWith('_sys')) return 'system'
  if (/_s_[0-9a-f]{32}$/.test(sendID)) return 'agent'
  return 'other'
}

export function fromIm(m: ChatMessage, myUserID: string): WidgetMessage {
  return {
    key: m.serverMsgID || m.clientMsgID,
    serverMsgID: m.serverMsgID,
    clientMsgID: m.clientMsgID,
    role: senderRole(m.sendID, myUserID),
    senderName: m.senderNickname || null,
    text: m.text,
    sendTime: m.sendTime,
  }
}

const API_ROLE: Record<string, SenderRole> = {
  customer: 'me',
  agent: 'agent',
  bot: 'bot',
  system: 'system',
}

export function fromApi(m: Schemas['VisitorMessageOut']): WidgetMessage {
  return {
    key: m.server_msg_id,
    serverMsgID: m.server_msg_id,
    clientMsgID: '',
    role: API_ROLE[m.sender_type] ?? 'other',
    senderName: m.sender_name,
    text: m.text,
    sendTime: Date.parse(m.sent_at),
  }
}

/** 合并消息：同一条消息可能同时来自历史接口、IM 推送和自己发送的返回值，按 IM 消息 ID 去重。 */
export function mergeMessages(
  current: WidgetMessage[],
  incoming: WidgetMessage[],
): WidgetMessage[] {
  const byKey = new Map(current.map((m) => [m.key, m]))
  for (const m of incoming) {
    const existing =
      byKey.get(m.key) ??
      (m.clientMsgID ? [...byKey.values()].find((x) => x.clientMsgID === m.clientMsgID) : undefined)
    if (existing) byKey.delete(existing.key)
    byKey.set(m.key, {
      ...existing,
      ...m,
      clientMsgID: m.clientMsgID || existing?.clientMsgID || '',
      senderName: m.senderName ?? existing?.senderName ?? null,
    })
  }
  return [...byKey.values()].sort((a, b) => a.sendTime - b.sendTime)
}
