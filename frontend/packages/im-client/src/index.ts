/**
 * OpenIM 客户端封装。平台前端只通过这里使用 IM：
 * - SDK 的授权（GPL-3.0，商用前需购买授权）和版本升级收敛在一处；
 * - 对外只暴露连接状态、收发文本、拉取历史这几个平台需要的能力。
 *
 * 纯 JS SDK 不在浏览器落盘，转接后旧坐席的电脑上不会残留聊天记录。
 */
import { CbEvents, getSDK } from '@openim/client-sdk'
import type { MessageItem, PicBaseInfo } from '@openim/client-sdk'

export type ConnectionState =
  | 'idle'
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'failed'
  | 'kicked'
  | 'expired'

/** 平台接口（如 /api/v1/visitor/init）返回的 IM 登录信息。 */
export interface ImLogin {
  userID: string
  token: string
  apiAddr: string
  wsAddr: string
  platformID: number
}

/** 图片或文件（URL 指向平台签发的文件链接）。 */
export interface Attachment {
  url: string
  name: string | null
  size: number | null
  width: number | null
  height: number | null
}

export interface ChatMessage {
  clientMsgID: string
  serverMsgID: string
  sendID: string
  senderNickname: string
  groupID: string
  seq: number
  sendTime: number
  contentType: number
  /** 文本类消息的正文；图片、文件等其他类型为 null。 */
  text: string | null
  /** 图片、文件消息的附件；其他类型为 null。 */
  attachment: Attachment | null
  ex: string
}

export interface ImageToSend {
  url: string
  type: string
  size: number
  width: number
  height: number
}

export interface FileToSend {
  url: string
  name: string
  size: number
  type: string
}

/** 平台发给员工的在线信令（系统用户以在线自定义消息发送，description 为 edp.signal）。 */
export interface ImSignal {
  sendID: string
  type: string
  data: Record<string, unknown>
}

export const SIGNAL_DESCRIPTION = 'edp.signal'
const CUSTOM = 110

/** 本封装用到的 SDK 能力，测试时可以替换。 */
export interface SdkLike {
  login(params: ImLogin): Promise<unknown>
  logout(): Promise<unknown>
  on(event: CbEvents, handler: (event: { data: unknown }) => void): void
  off(event: CbEvents, handler: (event: { data: unknown }) => void): void
  createTextMessage(text: string): Promise<{ data: MessageItem }>
  createImageMessageByURL(params: {
    sourcePicture: PicBaseInfo
    bigPicture: PicBaseInfo
    snapshotPicture: PicBaseInfo
    sourcePath: string
  }): Promise<{ data: MessageItem }>
  createFileMessageByURL(params: {
    filePath: string
    fileName: string
    uuid: string
    sourceUrl: string
    fileSize: number
    fileType?: string
  }): Promise<{ data: MessageItem }>
  sendMessage(params: {
    recvID: string
    groupID: string
    message: MessageItem
  }): Promise<{ data: MessageItem }>
  /** 发送已有 URL 的图片、文件消息：sendMessage 会尝试从本地路径上传文件，这里不上传。 */
  sendMessageNotOss(params: {
    recvID: string
    groupID: string
    message: MessageItem
  }): Promise<{ data: MessageItem }>
  getAdvancedHistoryMessageList(params: {
    conversationID: string
    count: number
    startClientMsgID: string
  }): Promise<{ data: { messageList: MessageItem[]; isEnd: boolean } }>
}

export interface ImClient {
  readonly state: ConnectionState
  connect(login: ImLogin): Promise<void>
  disconnect(): Promise<void>
  sendText(groupID: string, text: string): Promise<ChatMessage>
  sendImage(groupID: string, image: ImageToSend): Promise<ChatMessage>
  sendFile(groupID: string, file: FileToSend): Promise<ChatMessage>
  /** 会话中最近的消息（按时间升序）。 */
  history(conversationID: string, count: number): Promise<ChatMessage[]>
  onMessage(listener: (message: ChatMessage) => void): () => void
  onSignal(listener: (signal: ImSignal) => void): () => void
  onState(listener: (state: ConnectionState) => void): () => void
}

const TYPING = 113
const PICTURE = 102
const FILE = 105
const NOTIFICATION_BEGIN = 1000
/** 等待登录后首次同步的上限；超时后照常继续，只是历史消息可能要稍后才能拉到。 */
export const SYNC_TIMEOUT_MS = 5000

export function isChatContent(contentType: number): boolean {
  return contentType !== TYPING && contentType < NOTIFICATION_BEGIN
}

function attachmentOf(item: MessageItem): Attachment | null {
  const picture = item.pictureElem?.sourcePicture ?? item.pictureElem?.bigPicture
  if (item.contentType === PICTURE && picture?.url) {
    return {
      url: picture.url,
      name: null,
      size: picture.size || null,
      width: picture.width || null,
      height: picture.height || null,
    }
  }
  const file = item.fileElem
  if (item.contentType === FILE && file?.sourceUrl) {
    return {
      url: file.sourceUrl,
      name: file.fileName || null,
      size: file.fileSize || null,
      width: null,
      height: null,
    }
  }
  return null
}

export function toChatMessage(item: MessageItem): ChatMessage {
  const text = item.textElem?.content ?? item.atTextElem?.text ?? item.quoteElem?.text ?? null
  return {
    clientMsgID: item.clientMsgID,
    serverMsgID: item.serverMsgID,
    sendID: item.sendID,
    senderNickname: item.senderNickname ?? '',
    groupID: item.groupID,
    seq: item.seq,
    sendTime: item.sendTime,
    contentType: item.contentType,
    text,
    attachment: attachmentOf(item),
    ex: item.ex,
  }
}

/**
 * 规避 @openim/client-sdk 3.8.3 的缺陷：SDK 刷新"已读/最大 seq"缓存时直接赋值为服务端返回的
 * seqs，用户还没有任何会话时这个值是 undefined。之后一旦收到新会话的消息（例如刚被拉进服务群），
 * SDK 内部就会抛 TypeError，发送消息也随之失败。新访客、刚开通账号的坐席都会遇到。
 *
 * 这里把缓存字段改成访问器：赋值为 undefined 时存为空对象。升级 SDK 时先确认缺陷是否已修复；
 * SDK 内部结构变化时返回 false（测试会失败，提醒重新评估）。
 */
export function guardReadSeqCache(sdk: object): boolean {
  const cache = (sdk as { messageTrigger?: { cache?: Record<string, unknown> } }).messageTrigger
    ?.cache
  if (!cache || !('cachedHasReadAndMaxSeqs' in cache)) {
    console.warn('[im-client] OpenIM SDK internals changed; read-seq cache guard not applied')
    return false
  }
  let seqs = cache.cachedHasReadAndMaxSeqs ?? {}
  Object.defineProperty(cache, 'cachedHasReadAndMaxSeqs', {
    configurable: true,
    enumerable: true,
    get: () => seqs,
    set: (value: unknown) => {
      seqs = value ?? {}
    },
  })
  return true
}

/** 解析平台信令；不是平台信令时返回 null。 */
export function toSignal(item: MessageItem): ImSignal | null {
  const elem = item.customElem
  if (item.contentType !== CUSTOM || !elem || elem.description !== SIGNAL_DESCRIPTION) return null
  try {
    const data = JSON.parse(elem.data) as Record<string, unknown>
    if (typeof data !== 'object' || data === null || typeof data.type !== 'string') return null
    return { sendID: item.sendID, type: data.type, data }
  } catch {
    return null
  }
}

function defaultSdk(): SdkLike {
  const sdk = getSDK()
  guardReadSeqCache(sdk)
  return sdk as unknown as SdkLike
}

function waitForSync(sdk: SdkLike): { promise: Promise<void>; cancel: () => void } {
  const events = [CbEvents.OnSyncServerFinish, CbEvents.OnSyncServerFailed]
  let cancel = (): void => undefined
  const promise = new Promise<void>((resolve) => {
    const done = (): void => {
      clearTimeout(timer)
      events.forEach((event) => sdk.off(event, done))
      resolve()
    }
    const timer = setTimeout(done, SYNC_TIMEOUT_MS)
    events.forEach((event) => sdk.on(event, done))
    cancel = done
  })
  return { promise, cancel }
}

export function createImClient(sdk: SdkLike = defaultSdk()): ImClient {
  let state: ConnectionState = 'idle'
  const messageListeners = new Set<(message: ChatMessage) => void>()
  const signalListeners = new Set<(signal: ImSignal) => void>()
  const stateListeners = new Set<(state: ConnectionState) => void>()

  const emitSignals = (items: MessageItem[]): void => {
    for (const item of items) {
      const signal = toSignal(item)
      if (!signal) continue
      for (const listener of signalListeners) listener(signal)
    }
  }

  const setState = (next: ConnectionState): void => {
    if (next === state) return
    state = next
    for (const listener of stateListeners) listener(next)
  }

  const handlers: [CbEvents, (event: { data: unknown }) => void][] = [
    [CbEvents.OnConnecting, () => setState(state === 'connected' ? 'reconnecting' : 'connecting')],
    [CbEvents.OnConnectSuccess, () => setState('connected')],
    [CbEvents.OnConnectFailed, () => setState('failed')],
    [CbEvents.OnKickedOffline, () => setState('kicked')],
    [CbEvents.OnUserTokenExpired, () => setState('expired')],
    [CbEvents.OnUserTokenInvalid, () => setState('expired')],
    [
      CbEvents.OnRecvNewMessages,
      ({ data }) => {
        for (const item of data as MessageItem[]) {
          if (!isChatContent(item.contentType)) continue
          const message = toChatMessage(item)
          for (const listener of messageListeners) listener(message)
        }
      },
    ],
    // 在线信令不落库、不占 seq，只在对方在线时通过这个事件送达。
    [CbEvents.OnRecvOnlineOnlyMessages, ({ data }) => emitSignals(data as MessageItem[])],
    [CbEvents.OnRecvOnlineOnlyMessage, ({ data }) => emitSignals([data as MessageItem])],
  ]
  const attach = (): void => handlers.forEach(([event, handler]) => sdk.on(event, handler))
  const detach = (): void => handlers.forEach(([event, handler]) => sdk.off(event, handler))

  return {
    get state() {
      return state
    },

    async connect(login) {
      attach()
      setState('connecting')
      // 登录后 SDK 还要与服务端做一次同步，完成前拉取历史会得到空列表，所以等同步结束才算连上。
      const synced = waitForSync(sdk)
      try {
        await sdk.login(login)
      } catch (error) {
        synced.cancel()
        detach()
        setState('failed')
        throw error
      }
      await synced.promise
      setState('connected')
    },

    async disconnect() {
      // 先解除监听：SDK 在登出时会触发一次连接失败事件。
      detach()
      setState('idle')
      await sdk.logout().catch(() => undefined)
    },

    async sendText(groupID, text) {
      const { data: message } = await sdk.createTextMessage(text)
      const { data: sent } = await sdk.sendMessage({ recvID: '', groupID, message })
      return toChatMessage(sent)
    },

    async sendImage(groupID, image) {
      const picture: PicBaseInfo = { uuid: crypto.randomUUID(), ...image }
      const { data: message } = await sdk.createImageMessageByURL({
        sourcePicture: picture,
        bigPicture: picture,
        snapshotPicture: picture,
        sourcePath: '',
      })
      const { data: sent } = await sdk.sendMessageNotOss({ recvID: '', groupID, message })
      return toChatMessage(sent)
    },

    async sendFile(groupID, file) {
      const { data: message } = await sdk.createFileMessageByURL({
        filePath: '',
        fileName: file.name,
        uuid: crypto.randomUUID(),
        sourceUrl: file.url,
        fileSize: file.size,
        fileType: file.type,
      })
      const { data: sent } = await sdk.sendMessageNotOss({ recvID: '', groupID, message })
      return toChatMessage(sent)
    },

    async history(conversationID, count) {
      const { data } = await sdk.getAdvancedHistoryMessageList({
        conversationID,
        count,
        startClientMsgID: '',
      })
      return data.messageList.filter((m) => isChatContent(m.contentType)).map(toChatMessage)
    },

    onMessage(listener) {
      messageListeners.add(listener)
      return () => messageListeners.delete(listener)
    },

    onSignal(listener) {
      signalListeners.add(listener)
      return () => signalListeners.delete(listener)
    },

    onState(listener) {
      stateListeners.add(listener)
      return () => stateListeners.delete(listener)
    },
  }
}
