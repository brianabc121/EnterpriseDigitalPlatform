import { createVisitorApi, errorMessage, type Schemas } from '@edp/api-client'

export type VisitorSession = Schemas['VisitorInitResponse']
export type SessionState = Schemas['VisitorSessionState']
export type Identity = Schemas['VisitorIdentity']

const api = createVisitorApi(import.meta.env.VITE_API_BASE ?? '')

const tokenKey = (channelKey: string): string => `edp.visitor.${channelKey}`

// 嵌入第三方网页时，浏览器可能禁止访问存储；取不到时按新访客处理。
function readToken(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function saveToken(key: string, token: string): void {
  try {
    localStorage.setItem(key, token)
  } catch {
    // 保存失败只影响下次识别回访，不影响本次会话。
  }
}

/** 嵌入脚本放在 URL # 之后的实名身份（见 public/embed.js）。 */
export function identityFromHash(hash: string): Identity | null {
  const raw = new URLSearchParams(hash.replace(/^#/, '')).get('identity')
  if (!raw) return null
  try {
    const value = JSON.parse(raw) as Identity
    return typeof value.external_id === 'string' && typeof value.signature === 'string'
      ? value
      : null
  } catch {
    return null
  }
}

/**
 * Widget 所在网站的来源：被嵌入时取父页面（浏览器提供，页面脚本无法伪造），
 * 否则是 Widget 自己的地址（例如控制台里的访客测试页）。
 */
export function embedOrigin(): string {
  if (window.top === window) return location.origin
  const ancestors = (location as Location & { ancestorOrigins?: DOMStringList }).ancestorOrigins
  if (ancestors && ancestors.length > 0) return ancestors[0]!
  try {
    return document.referrer ? new URL(document.referrer).origin : ''
  } catch {
    return ''
  }
}

export async function initVisitor(channelKey: string): Promise<VisitorSession> {
  const query = new URLSearchParams(location.search)
  const { data, error } = await api.POST('/api/v1/visitor/init', {
    body: {
      channel_key: channelKey,
      visitor_token: readToken(tokenKey(channelKey)),
      identity: identityFromHash(location.hash),
      embed_origin: embedOrigin(),
      page_url: query.get('page_url') || document.referrer || null,
      referrer: query.get('referrer') || null,
    },
  })
  if (!data) throw new Error(errorMessage(error, '客服暂时不可用，请稍后再试'))
  saveToken(tokenKey(channelKey), data.visitor_token)
  return data
}

const auth = (token: string) => ({ header: { 'X-Visitor-Token': token } })

/** 最近的消息（按时间倒序），用于补齐 IM 没有推送到的消息。 */
export async function fetchMessages(token: string): Promise<Schemas['VisitorMessageOut'][]> {
  const { data, error } = await api.GET('/api/v1/visitor/messages', {
    params: { ...auth(token), query: { limit: 50 } },
  })
  if (!data) throw new Error(errorMessage(error))
  return data.items
}

export async function fetchState(token: string): Promise<SessionState> {
  const { data, error } = await api.GET('/api/v1/visitor/session', { params: auth(token) })
  if (!data) throw new Error(errorMessage(error))
  return data
}

export async function requestHuman(token: string): Promise<SessionState> {
  const { data, error } = await api.POST('/api/v1/visitor/handoff', { params: auth(token) })
  if (!data) throw new Error(errorMessage(error))
  return data
}

/** 取消排队：回到智能客服接待；智能客服不可用时结束会话。 */
export async function cancelQueue(token: string): Promise<SessionState> {
  const { data, error } = await api.POST('/api/v1/visitor/cancel-queue', { params: auth(token) })
  if (!data) throw new Error(errorMessage(error))
  return data
}

export async function rate(
  token: string,
  sessionId: string,
  score: number,
  comment: string,
): Promise<void> {
  const { error } = await api.POST('/api/v1/visitor/csat', {
    params: auth(token),
    body: { session_id: sessionId, score, comment: comment || null },
  })
  if (error) throw new Error(errorMessage(error))
}

/** 评价智能客服的一条回答：1 有用，-1 没用（可以改评价）。 */
export async function rateAnswer(token: string, serverMsgId: string, value: 1 | -1): Promise<void> {
  const { error } = await api.POST('/api/v1/visitor/ai-feedback', {
    params: auth(token),
    body: { server_msg_id: serverMsgId, value },
  })
  if (error) throw new Error(errorMessage(error))
}

export async function leaveMessage(token: string, content: string, contact: string): Promise<void> {
  const { error } = await api.POST('/api/v1/visitor/leave-message', {
    params: auth(token),
    body: { content, contact: contact || null },
  })
  if (error) throw new Error(errorMessage(error, '留言失败，请稍后再试'))
}

/** 申请上传凭证并把文件直接上传到对象存储；返回消息里引用的文件链接。 */
export async function upload(token: string, file: File): Promise<Schemas['UploadOut']> {
  const { data, error } = await api.POST('/api/v1/visitor/uploads', {
    params: auth(token),
    body: { filename: file.name, content_type: file.type, size: file.size },
  })
  if (!data) throw new Error(errorMessage(error, '上传失败'))
  const response = await fetch(data.upload_url, {
    method: 'PUT',
    headers: { 'content-type': file.type },
    body: file,
  })
  if (!response.ok) throw new Error('上传失败，请稍后再试')
  return data
}

/** 读取图片尺寸（发送图片消息需要）。 */
export function imageSize(file: File): Promise<{ width: number; height: number }> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file)
    const img = new Image()
    img.onload = () => {
      resolve({ width: img.naturalWidth, height: img.naturalHeight })
      URL.revokeObjectURL(url)
    }
    img.onerror = () => {
      resolve({ width: 0, height: 0 })
      URL.revokeObjectURL(url)
    }
    img.src = url
  })
}
