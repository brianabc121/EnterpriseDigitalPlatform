import { createVisitorApi, errorMessage, type Schemas } from '@edp/api-client'

export type VisitorSession = Schemas['VisitorInitResponse']

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
  const api = createVisitorApi(import.meta.env.VITE_API_BASE ?? '')
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
