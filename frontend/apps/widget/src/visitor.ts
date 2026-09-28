import { createVisitorApi, errorMessage, type Schemas } from '@edp/api-client'

export type VisitorSession = Schemas['VisitorInitResponse']

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

export async function initVisitor(channelKey: string): Promise<VisitorSession> {
  const { data, error } = await api.POST('/api/v1/visitor/init', {
    body: {
      channel_key: channelKey,
      visitor_token: readToken(tokenKey(channelKey)),
      page_url: document.referrer || null,
    },
  })
  if (!data) throw new Error(errorMessage(error, '客服暂时不可用，请稍后再试'))
  saveToken(tokenKey(channelKey), data.visitor_token)
  return data
}

/** 最近的消息（按时间倒序），用于补齐 IM 没有推送到的消息。 */
export async function fetchMessages(visitorToken: string): Promise<Schemas['VisitorMessageOut'][]> {
  const { data, error } = await api.GET('/api/v1/visitor/messages', {
    params: { header: { 'X-Visitor-Token': visitorToken }, query: { limit: 50 } },
  })
  if (!data) throw new Error(errorMessage(error))
  return data.items
}
