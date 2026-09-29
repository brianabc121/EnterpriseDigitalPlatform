/**
 * 工作台的消息列表：历史走平台接口，增量走 IM，坐席自己的消息经平台接口发送。
 *
 * 同一条消息可能从三处到达：历史接口、IM 实时推送、发送接口的返回值。合并规则：
 * - 平台发出的消息，IM 消息的 ex 里带平台消息 ID（pmid），与接口返回的 id 相同；
 * - 其他消息按 IM 消息 ID（serverMsgID，即接口里的 channel_msg_id）关联；
 * - 正在发送的消息先用客户端 ID 占位，发送接口返回后替换。
 */
import type { Schemas } from '@edp/api-client'
import type { ChatMessage } from '@edp/im-client'

export type SenderType = 'customer' | 'agent' | 'bot' | 'system'
export type SendStatus = 'pending' | 'sent' | 'failed'

/** 图片或文件消息的附件（url 是平台签发的文件链接）。 */
export interface Attachment {
  url: string
  name: string | null
  size: number | null
  width: number | null
  height: number | null
  /** MIME 类型；IM 推送的文件消息里没有。 */
  mime: string | null
}

export interface WorkbenchMessage {
  /** 列表中的唯一键。 */
  key: string
  /** 平台消息 ID（接口返回的 id；IM 消息里来自 ex.pmid）。 */
  id: string | null
  /** IM 消息 ID。 */
  serverMsgID: string | null
  /** 坐席发送时生成的幂等键。 */
  clientMsgID: string | null
  senderType: SenderType
  senderID: string | null
  senderName: string | null
  text: string | null
  contentType: string
  attachment: Attachment | null
  sentAt: number
  status: SendStatus | null
}

const CONTENT_TYPES: Record<number, string> = {
  101: 'text',
  106: 'text',
  114: 'text',
  102: 'image',
  103: 'voice',
  104: 'video',
  105: 'file',
  110: 'custom',
}

const num = (v: unknown): number | null => (typeof v === 'number' ? v : null)
const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null)

/** 平台保存的图片、文件消息内容：{url, name, size, width, height, mime}（见后端 ingest.attachment_of）。 */
export function attachmentOf(
  contentType: string,
  content: Record<string, unknown>,
): Attachment | null {
  if (!['image', 'file'].includes(contentType) || typeof content.url !== 'string') return null
  return {
    url: content.url,
    name: str(content.name),
    size: num(content.size),
    width: num(content.width),
    height: num(content.height),
    mime: str(content.mime),
  }
}

export function fromApi(m: Schemas['MessageOut']): WorkbenchMessage {
  return {
    key: m.channel_msg_id ?? m.id,
    id: m.id,
    serverMsgID: m.channel_msg_id,
    clientMsgID: m.client_msg_id,
    senderType: m.sender_type as SenderType,
    senderID: m.sender_id,
    senderName: m.sender_name ?? null,
    text: m.text_plain,
    contentType: m.content_type,
    attachment: attachmentOf(m.content_type, m.content),
    sentAt: Date.parse(m.sent_at),
    status: (m.send_status as SendStatus | null) ?? null,
  }
}

/** 从 IM 用户 ID 推断发送者类型（ID 约定见实施计划 §7.2）。 */
export function senderTypeOf(sendID: string): SenderType {
  if (sendID.endsWith('_bot')) return 'bot'
  if (sendID.endsWith('_sys')) return 'system'
  if (/_s_[0-9a-f]{32}$/.test(sendID)) return 'agent'
  return 'customer'
}

/** IM 用户 ID 末尾的平台对象 ID（员工 ID、客户身份 ID）。 */
export function objectIdOf(sendID: string): string | null {
  const match = /_[cs]_([0-9a-f]{32})$/.exec(sendID)
  if (!match) return null
  const h = match[1]!
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`
}

export function pmidOf(ex: string): string | null {
  if (!ex) return null
  try {
    const data = JSON.parse(ex) as { pmid?: unknown }
    return typeof data.pmid === 'string' ? data.pmid : null
  } catch {
    return null
  }
}

export function fromIm(m: ChatMessage): WorkbenchMessage {
  const senderType = senderTypeOf(m.sendID)
  return {
    key: m.serverMsgID || m.clientMsgID,
    id: pmidOf(m.ex),
    serverMsgID: m.serverMsgID || null,
    clientMsgID: null,
    senderType,
    senderID: objectIdOf(m.sendID),
    // 坐席显示姓名，智能客服显示租户设置的名称。
    senderName: senderType === 'agent' || senderType === 'bot' ? m.senderNickname || null : null,
    text: m.text,
    contentType: CONTENT_TYPES[m.contentType] ?? 'other',
    attachment: m.attachment ? { ...m.attachment, mime: null } : null,
    sentAt: m.sendTime,
    status: null,
  }
}

/** 回复的来源（用于统计 AI 建议的采纳率）。 */
export type ReplyOrigin = Schemas['SendMessageRequest']['origin']

/** 坐席要发送的内容：文本（及其来源），或已上传的图片、文件。 */
export type Outgoing =
  | { type: 'text'; text: string; origin?: ReplyOrigin }
  | {
      type: 'image' | 'file'
      attachment: Attachment & { name: string; size: number; mime: string }
    }

/** 发送接口的请求体。 */
export function sendBody(clientMsgID: string, out: Outgoing): Schemas['SendMessageRequest'] {
  if (out.type === 'text') {
    return {
      client_msg_id: clientMsgID,
      type: 'text',
      text: out.text,
      origin: out.origin ?? 'manual',
    }
  }
  const { url, name, size, mime, width, height } = out.attachment
  return {
    client_msg_id: clientMsgID,
    type: out.type,
    attachment: { url, name, size, content_type: mime, width, height },
    origin: 'manual',
  }
}

/** 从已有消息还原发送内容（重试用）；不能还原时返回 null。 */
export function outgoingOf(m: WorkbenchMessage): Outgoing | null {
  if (m.contentType === 'text' && m.text) return { type: 'text', text: m.text }
  const a = m.attachment
  if ((m.contentType === 'image' || m.contentType === 'file') && a?.name && a.size && a.mime) {
    return { type: m.contentType, attachment: { ...a, name: a.name, size: a.size, mime: a.mime } }
  }
  return null
}

/** 坐席刚提交、尚未得到接口响应的消息。 */
export function pendingMessage(
  clientMsgID: string,
  out: Outgoing,
  me: { id: string; name: string },
): WorkbenchMessage {
  return {
    key: `pending:${clientMsgID}`,
    id: null,
    serverMsgID: null,
    clientMsgID,
    senderType: 'agent',
    senderID: me.id,
    senderName: me.name,
    text: out.type === 'text' ? out.text : null,
    contentType: out.type,
    attachment: out.type === 'text' ? null : out.attachment,
    sentAt: Date.now(),
    status: 'pending',
  }
}

function sameMessage(a: WorkbenchMessage, b: WorkbenchMessage): boolean {
  return (
    (a.id !== null && a.id === b.id) ||
    (a.serverMsgID !== null && a.serverMsgID === b.serverMsgID) ||
    (a.clientMsgID !== null && a.clientMsgID === b.clientMsgID) ||
    a.key === b.key
  )
}

/** 合并两条表示同一消息的记录：平台字段（id、状态、姓名）与 IM 字段（serverMsgID）互补。 */
function combine(current: WorkbenchMessage, incoming: WorkbenchMessage): WorkbenchMessage {
  const serverMsgID = incoming.serverMsgID ?? current.serverMsgID
  // 平台发出的消息才有发送状态；IM 里已经有这条消息就是发送成功了。
  const tracked = current.status !== null || incoming.status !== null
  const status = !tracked ? null : serverMsgID ? 'sent' : (incoming.status ?? current.status)
  const id = incoming.id ?? current.id
  return {
    ...current,
    ...incoming,
    key: serverMsgID ?? id ?? current.key,
    id,
    serverMsgID,
    clientMsgID: incoming.clientMsgID ?? current.clientMsgID,
    senderID: incoming.senderID ?? current.senderID,
    senderName: incoming.senderName ?? current.senderName,
    // IM 推送的附件没有 MIME 类型，保留平台记录里的。
    attachment: incoming.attachment
      ? {
          ...incoming.attachment,
          mime: incoming.attachment.mime ?? current.attachment?.mime ?? null,
        }
      : current.attachment,
    status,
  }
}

/**
 * 合并新到达的消息。一条新记录可能同时对应多条已有记录（例如 IM 推送先于发送接口的返回到达：
 * 占位消息只有客户端 ID，推送只有 pmid，接口返回两者都有），这时把它们汇合成一条。
 */
export function mergeMessages(
  current: WorkbenchMessage[],
  incoming: WorkbenchMessage[],
): WorkbenchMessage[] {
  let result = [...current]
  for (const message of incoming) {
    const matches = result.filter((m) => sameMessage(m, message))
    if (matches.length === 0) {
      result.push(message)
      continue
    }
    const merged = [...matches.slice(1), message].reduce(combine, matches[0]!)
    result = result.filter((m) => !matches.includes(m))
    result.push(merged)
  }
  return result.sort((a, b) => a.sentAt - b.sentAt)
}
