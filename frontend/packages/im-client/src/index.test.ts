import { CbEvents, getSDK } from '@openim/client-sdk'
import type { MessageItem, PicBaseInfo } from '@openim/client-sdk'
import { describe, expect, it } from 'vitest'

import {
  createImClient,
  guardReadSeqCache,
  SIGNAL_DESCRIPTION,
  toChatMessage,
  toSignal,
  type ConnectionState,
  type ImLogin,
  type ImSignal,
  type SdkLike,
} from './index'

const LOGIN: ImLogin = {
  userID: 'acme_c_1',
  token: 't',
  apiAddr: 'http://im',
  wsAddr: 'ws://im',
  platformID: 5,
}

function item(overrides: Partial<MessageItem>): MessageItem {
  return {
    clientMsgID: 'c1',
    serverMsgID: 's1',
    sendID: 'acme_bot',
    groupID: 'acme_r_1',
    seq: 1,
    sendTime: 1000,
    contentType: 101,
    ex: '',
    textElem: { content: '你好' },
    ...overrides,
  } as MessageItem
}

class FakeSdk implements SdkLike {
  handlers = new Map<CbEvents, Set<(event: { data: unknown }) => void>>()
  loginError: Error | null = null
  sent: { groupID: string; message: MessageItem }[] = []
  sentNotOss: { groupID: string; message: MessageItem }[] = []
  history: MessageItem[] = []

  on(event: CbEvents, handler: (event: { data: unknown }) => void): void {
    if (!this.handlers.has(event)) this.handlers.set(event, new Set())
    this.handlers.get(event)!.add(handler)
  }

  off(event: CbEvents, handler: (event: { data: unknown }) => void): void {
    this.handlers.get(event)?.delete(handler)
  }

  emit(event: CbEvents, data: unknown = null): void {
    this.handlers.get(event)?.forEach((handler) => handler({ data }))
  }

  syncDelayMs = 0

  async login(): Promise<unknown> {
    if (this.loginError) throw this.loginError
    // 真实 SDK 在登录成功后开始与服务端同步，完成时触发 OnSyncServerFinish。
    setTimeout(() => this.emit(CbEvents.OnSyncServerFinish), this.syncDelayMs)
    return {}
  }

  async logout(): Promise<unknown> {
    // 真实 SDK 登出时会触发一次连接失败事件。
    this.emit(CbEvents.OnConnectFailed)
    return {}
  }

  async createTextMessage(text: string): Promise<{ data: MessageItem }> {
    return { data: item({ clientMsgID: 'new', serverMsgID: '', textElem: { content: text } }) }
  }

  async createImageMessageByURL(params: {
    sourcePicture: PicBaseInfo
  }): Promise<{ data: MessageItem }> {
    return {
      data: item({
        clientMsgID: 'img',
        serverMsgID: '',
        contentType: 102,
        textElem: undefined,
        pictureElem: {
          sourcePath: '',
          sourcePicture: params.sourcePicture,
          bigPicture: params.sourcePicture,
          snapshotPicture: params.sourcePicture,
        },
      } as Partial<MessageItem>),
    }
  }

  async createFileMessageByURL(params: {
    fileName: string
    sourceUrl: string
    fileSize: number
    uuid: string
    filePath: string
  }): Promise<{ data: MessageItem }> {
    return {
      data: item({
        clientMsgID: 'file',
        serverMsgID: '',
        contentType: 105,
        textElem: undefined,
        fileElem: { ...params },
      } as Partial<MessageItem>),
    }
  }

  async sendMessage(params: {
    recvID: string
    groupID: string
    message: MessageItem
  }): Promise<{ data: MessageItem }> {
    this.sent.push(params)
    return { data: { ...params.message, serverMsgID: 'srv', groupID: params.groupID, seq: 7 } }
  }

  async sendMessageNotOss(params: {
    recvID: string
    groupID: string
    message: MessageItem
  }): Promise<{ data: MessageItem }> {
    this.sentNotOss.push(params)
    return { data: { ...params.message, serverMsgID: 'srv', groupID: params.groupID, seq: 8 } }
  }

  async getAdvancedHistoryMessageList(): Promise<{
    data: { messageList: MessageItem[]; isEnd: boolean }
  }> {
    return { data: { messageList: this.history, isEnd: true } }
  }
}

describe('createImClient', () => {
  it('tracks connection state through login, reconnects and token expiry', async () => {
    const sdk = new FakeSdk()
    const client = createImClient(sdk)
    const states: ConnectionState[] = []
    client.onState((s) => states.push(s))

    await client.connect(LOGIN)
    sdk.emit(CbEvents.OnConnecting)
    sdk.emit(CbEvents.OnConnectSuccess)
    sdk.emit(CbEvents.OnUserTokenExpired)

    expect(states).toEqual(['connecting', 'connected', 'reconnecting', 'connected', 'expired'])
    expect(client.state).toBe('expired')
  })

  it('is only connected once the initial sync has finished', async () => {
    const sdk = new FakeSdk()
    sdk.syncDelayMs = 30
    const client = createImClient(sdk)
    let resolved = false

    const connecting = client.connect(LOGIN).then(() => (resolved = true))
    await new Promise((r) => setTimeout(r, 10))
    expect([client.state, resolved]).toEqual(['connecting', false])

    await connecting
    expect(client.state).toBe('connected')
  })

  it('reports a failed login and rethrows', async () => {
    const sdk = new FakeSdk()
    sdk.loginError = new Error('bad token')
    const client = createImClient(sdk)

    await expect(client.connect(LOGIN)).rejects.toThrow('bad token')
    expect(client.state).toBe('failed')
    expect(sdk.handlers.get(CbEvents.OnRecvNewMessages)?.size ?? 0).toBe(0)
  })

  it('delivers chat messages but drops typing and group notifications', async () => {
    const sdk = new FakeSdk()
    const client = createImClient(sdk)
    const received: (string | null)[] = []
    client.onMessage((m) => received.push(m.text))
    await client.connect(LOGIN)

    sdk.emit(CbEvents.OnRecvNewMessages, [
      item({ textElem: { content: '一' } }),
      item({ contentType: 113 }),
      item({ contentType: 1504, textElem: undefined }),
      item({ contentType: 106, textElem: undefined, atTextElem: { text: '@你' } as never }),
      item({ contentType: 102, textElem: undefined }),
    ])

    expect(received).toEqual(['一', '@你', null])
  })

  it('sends text to the group and returns the stored message', async () => {
    const sdk = new FakeSdk()
    const client = createImClient(sdk)
    await client.connect(LOGIN)

    const sent = await client.sendText('acme_r_1', '想咨询')

    expect(sdk.sent).toHaveLength(1)
    expect(sdk.sent[0]!.groupID).toBe('acme_r_1')
    expect(sent).toMatchObject({ serverMsgID: 'srv', seq: 7, text: '想咨询' })
  })

  it('returns history without notifications', async () => {
    const sdk = new FakeSdk()
    sdk.history = [item({ contentType: 1501, textElem: undefined }), item({ seq: 2 })]
    const client = createImClient(sdk)

    const history = await client.history('sg_acme_r_1', 20)

    expect(history.map((m) => m.seq)).toEqual([2])
  })

  it('goes idle on disconnect and ignores events the SDK emits while logging out', async () => {
    const sdk = new FakeSdk()
    const client = createImClient(sdk)
    const received: string[] = []
    client.onMessage((m) => received.push(m.clientMsgID))
    await client.connect(LOGIN)

    await client.disconnect()
    sdk.emit(CbEvents.OnRecvNewMessages, [item({})])

    expect(client.state).toBe('idle')
    expect(received).toEqual([])
  })
})

describe('attachments', () => {
  it('sends images and files by URL and reads them back', async () => {
    const sdk = new FakeSdk()
    const client = createImClient(sdk)
    await client.connect(LOGIN)

    const image = await client.sendImage('acme_r_1', {
      url: 'https://api/files/a.png?sig=1',
      type: 'image/png',
      size: 100,
      width: 40,
      height: 30,
    })
    const file = await client.sendFile('acme_r_1', {
      url: 'https://api/files/b.pdf?sig=2',
      name: 'b.pdf',
      size: 200,
      type: 'application/pdf',
    })

    expect(image.attachment).toEqual({
      url: 'https://api/files/a.png?sig=1',
      name: null,
      size: 100,
      width: 40,
      height: 30,
    })
    expect(file.attachment).toMatchObject({ url: 'https://api/files/b.pdf?sig=2', name: 'b.pdf' })
    expect(toChatMessage(item({})).attachment).toBeNull()
    // 文件已经在对象存储里：不能走 sendMessage，否则 SDK 会尝试从本地路径重新上传而失败。
    expect(sdk.sent).toEqual([])
    expect(sdk.sentNotOss.map((s) => s.message.contentType)).toEqual([102, 105])
  })
})

describe('signals', () => {
  it('delivers platform signals from online-only custom messages', async () => {
    const sdk = new FakeSdk()
    const client = createImClient(sdk)
    const signals: ImSignal[] = []
    client.onSignal((s) => signals.push(s))
    await client.connect(LOGIN)

    const signal = (data: string, description = SIGNAL_DESCRIPTION): MessageItem =>
      item({
        sendID: 'acme_sys',
        contentType: 110,
        textElem: undefined,
        customElem: { data, description, extension: '' },
      })
    sdk.emit(CbEvents.OnRecvOnlineOnlyMessages, [
      signal('{"type":"session.assigned","session_id":"s1"}'),
      signal('{"type":"x"}', 'other'),
      signal('not json'),
      signal('{"no_type":1}'),
    ])
    sdk.emit(CbEvents.OnRecvOnlineOnlyMessage, signal('{"type":"session.closed"}'))

    expect(signals.map((s) => [s.sendID, s.type])).toEqual([
      ['acme_sys', 'session.assigned'],
      ['acme_sys', 'session.closed'],
    ])
    expect(signals[0]!.data.session_id).toBe('s1')
    // 信令不会当作聊天消息送出。
    expect(toSignal(item({}))).toBeNull()
  })
})

describe('guardReadSeqCache', () => {
  it('stores an empty object when the SDK assigns undefined', () => {
    const cache: Record<string, unknown> = { cachedHasReadAndMaxSeqs: {} }
    expect(guardReadSeqCache({ messageTrigger: { cache } })).toBe(true)

    cache.cachedHasReadAndMaxSeqs = undefined
    expect(cache.cachedHasReadAndMaxSeqs).toEqual({})

    const seqs = { sg_acme_r_1: { maxSeq: 3 } }
    cache.cachedHasReadAndMaxSeqs = seqs
    expect(cache.cachedHasReadAndMaxSeqs).toBe(seqs)
  })

  it('still matches the internals of the installed SDK', () => {
    // 升级 SDK 后如果这里失败，先确认原缺陷是否已修复，再决定删除或调整 guardReadSeqCache。
    expect(guardReadSeqCache(getSDK())).toBe(true)
  })
})
