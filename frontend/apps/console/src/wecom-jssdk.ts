/**
 * 企业微信 JS-SDK 的最小封装（侧边栏使用）：注入企业和应用权限，取当前聊天，发送消息。
 *
 * 需要在企业微信客户端里运行；页面地址（不含 # 之后的部分）必须与签名用的地址一致。
 */
import type { Schemas } from '@edp/api-client'

export interface WecomChat {
  kind: 'contact' | 'group'
  /** 单聊为客户的 external_userid，客户群为 chat_id。 */
  id: string
}

type Result = Record<string, unknown>

interface Wx {
  config(options: Record<string, unknown>): void
  ready(callback: () => void): void
  error(callback: (res: Result) => void): void
  agentConfig(options: Record<string, unknown>): void
  invoke(name: string, args: Record<string, unknown>, callback: (res: Result) => void): void
}

const DEFAULT_SCRIPTS = [
  'https://res.wx.qq.com/open/js/jweixin-1.2.0.js',
  'https://open.work.weixin.qq.com/wwopen/js/jwxwork-1.0.0.js',
]
// 联调、验收时可以换成模拟的 JS-SDK（逗号分隔，见 backend/tests/fake_wecom.py）。
const SCRIPTS: string[] = import.meta.env.VITE_WECOM_JSSDK_URLS
  ? String(import.meta.env.VITE_WECOM_JSSDK_URLS).split(',').filter(Boolean)
  : DEFAULT_SCRIPTS
const JS_API = [
  'getContext',
  'getCurExternalContact',
  'getCurExternalChat',
  'sendChatMessage',
  'openEnterpriseChat',
]

function loadScript(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    if (document.querySelector(`script[src="${src}"]`)) {
      resolve()
      return
    }
    const script = document.createElement('script')
    script.src = src
    script.onload = () => resolve()
    script.onerror = () => reject(new Error(`加载企业微信 JS-SDK 失败：${src}`))
    document.head.appendChild(script)
  })
}

function ok(res: Result): boolean {
  const message = String(res.err_msg ?? res.errMsg ?? '')
  return message.endsWith(':ok')
}

export async function loadJssdk(config: Schemas['JssdkConfig']) {
  for (const src of SCRIPTS) await loadScript(src)
  const wx = (window as unknown as { wx?: Wx }).wx
  if (!wx) throw new Error('企业微信 JS-SDK 没有加载成功')

  await new Promise<void>((resolve, reject) => {
    wx.config({
      beta: true,
      debug: false,
      appId: config.corp_id,
      timestamp: config.config.timestamp,
      nonceStr: config.config.nonce_str,
      signature: config.config.signature,
      jsApiList: JS_API,
    })
    wx.ready(() => resolve())
    wx.error((res) => reject(new Error(`企业微信权限校验失败：${JSON.stringify(res)}`)))
  })
  await new Promise<void>((resolve, reject) => {
    wx.agentConfig({
      corpid: config.corp_id,
      agentid: String(config.agent_id ?? ''),
      timestamp: config.agent_config.timestamp,
      nonceStr: config.agent_config.nonce_str,
      signature: config.agent_config.signature,
      jsApiList: JS_API,
      success: () => resolve(),
      fail: (res: Result) => reject(new Error(`应用权限校验失败：${JSON.stringify(res)}`)),
    })
  })

  function invoke(name: string, args: Record<string, unknown> = {}): Promise<Result> {
    return new Promise((resolve, reject) => {
      wx!.invoke(name, args, (res) => {
        if (ok(res)) resolve(res)
        else reject(new Error(`${name} 失败：${String(res.err_msg ?? res.errMsg ?? '')}`))
      })
    })
  }

  return {
    /** 当前是客户单聊还是客户群（从聊天工具栏打开时才有）。 */
    async currentChat(): Promise<WecomChat | null> {
      const context = await invoke('getContext')
      if (context.entry === 'single_chat_tools') {
        const res = await invoke('getCurExternalContact')
        return { kind: 'contact', id: String(res.userId) }
      }
      if (context.entry === 'group_chat_tools') {
        const res = await invoke('getCurExternalChat')
        return { kind: 'group', id: String(res.chatId) }
      }
      return null
    },
    /** 发送文字到当前聊天（员工确认后发出）。 */
    async sendText(text: string): Promise<void> {
      await invoke('sendChatMessage', { msgtype: 'text', enterChat: true, text: { content: text } })
    },
    /**
     * 一键建群（设计 §10.5）：拉上企业成员（如接单员）和客户，员工在企业微信里确认后建群。
     * 含外部联系人时最多 40 人。返回新群的 chat_id。
     */
    async createGroup(userIds: string[], externalUserIds: string[], name: string): Promise<string> {
      const res = await invoke('openEnterpriseChat', {
        userIds: userIds.join(';'),
        externalUserIds: externalUserIds.join(';'),
        groupName: name,
        chatId: '',
      })
      return String(res.chatId ?? '')
    },
  }
}
