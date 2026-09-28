import type { Schemas } from '@edp/api-client'
import type { ChatMessage } from '@edp/im-client'
import { describe, expect, it } from 'vitest'

import {
  fromApi,
  fromIm,
  mergeMessages,
  objectIdOf,
  outgoingOf,
  pendingMessage,
  pmidOf,
  sendBody,
  senderTypeOf,
  type Outgoing,
} from './messages'

const STAFF = '0190a3b2c4d5e6f708192a3b4c5d6e7f'
const STAFF_ID = '0190a3b2-c4d5-e6f7-0819-2a3b4c5d6e7f'

function api(overrides: Partial<Schemas['MessageOut']>): Schemas['MessageOut'] {
  return {
    id: 'p1',
    session_id: 's1',
    direction: 'in',
    sender_type: 'customer',
    sender_id: null,
    sender_name: null,
    content_type: 'text',
    content: { text: '你好' },
    text_plain: '你好',
    channel_msg_id: 'srv1',
    client_msg_id: null,
    im_seq: 2,
    source: 'webhook',
    send_status: null,
    sent_at: '2026-09-28T10:00:00Z',
    ...overrides,
  }
}

function im(overrides: Partial<ChatMessage>): ChatMessage {
  return {
    clientMsgID: 'c1',
    serverMsgID: 'srv1',
    sendID: 'acme_c_' + 'a'.repeat(32),
    senderNickname: '',
    groupID: 'acme_r_1',
    seq: 2,
    sendTime: Date.parse('2026-09-28T10:00:00Z'),
    contentType: 101,
    text: '你好',
    attachment: null,
    ex: '',
    ...overrides,
  }
}

const text = (t: string): Outgoing => ({ type: 'text', text: t })

describe('ids', () => {
  it('reads the sender from the IM user id', () => {
    expect(senderTypeOf('acme_bot')).toBe('bot')
    expect(senderTypeOf('acme_sys')).toBe('system')
    expect(senderTypeOf(`acme_s_${STAFF}`)).toBe('agent')
    expect(senderTypeOf('acme_c_' + 'a'.repeat(32))).toBe('customer')
    expect(objectIdOf(`acmeXcn_s_${STAFF}`)).toBe(STAFF_ID)
    expect(objectIdOf('acme_bot')).toBeNull()
  })

  it('reads the platform message id from ex', () => {
    expect(pmidOf('{"pmid":"p9"}')).toBe('p9')
    expect(pmidOf('')).toBeNull()
    expect(pmidOf('oops')).toBeNull()
    expect(pmidOf('{"pmid":1}')).toBeNull()
  })
})

describe('mergeMessages', () => {
  it('keeps one copy of a message seen in history and live', () => {
    const merged = mergeMessages([fromApi(api({}))], [fromIm(im({}))])

    expect(merged).toHaveLength(1)
    expect(merged[0]).toMatchObject({ id: 'p1', serverMsgID: 'srv1', senderType: 'customer' })
  })

  it('merges an agent message whose IM echo arrives before the send response', () => {
    const me = { id: STAFF_ID, name: 'Alice' }
    let list = mergeMessages([], [pendingMessage('cm1', text('您好'), me)])
    expect(list[0]!.status).toBe('pending')

    // IM 推送先到：ex 里带 pmid，但还不知道它对应哪个占位消息。
    const echo = fromIm(
      im({
        serverMsgID: 'srv9',
        sendID: `acme_s_${STAFF}`,
        senderNickname: 'Alice',
        text: '您好',
        ex: '{"pmid":"p9"}',
      }),
    )
    list = mergeMessages(list, [echo])
    // 发送接口返回：client_msg_id 与占位消息相同，id 与 IM 推送的 pmid 相同。
    const response = fromApi(
      api({
        id: 'p9',
        sender_type: 'agent',
        sender_id: STAFF_ID,
        sender_name: 'Alice',
        direction: 'out',
        channel_msg_id: 'srv9',
        client_msg_id: 'cm1',
        source: 'api',
        send_status: 'sent',
        text_plain: '您好',
      }),
    )
    list = mergeMessages(list, [response])

    // 占位消息与 IM 推送在接口返回时汇合成一条。
    expect(list).toHaveLength(1)
    expect(list[0]).toMatchObject({
      id: 'p9',
      serverMsgID: 'srv9',
      clientMsgID: 'cm1',
      status: 'sent',
      senderName: 'Alice',
      key: 'srv9',
    })
  })

  it('merges pending, response and echo into one when the response comes first', () => {
    const me = { id: STAFF_ID, name: 'Alice' }
    let list = mergeMessages([], [pendingMessage('cm1', text('您好'), me)])
    list = mergeMessages(list, [
      fromApi(
        api({
          id: 'p9',
          sender_type: 'agent',
          channel_msg_id: 'srv9',
          client_msg_id: 'cm1',
          source: 'api',
          send_status: 'sent',
          text_plain: '您好',
        }),
      ),
    ])
    list = mergeMessages(list, [
      fromIm(
        im({ serverMsgID: 'srv9', sendID: `acme_s_${STAFF}`, text: '您好', ex: '{"pmid":"p9"}' }),
      ),
    ])

    expect(list).toHaveLength(1)
    expect(list[0]).toMatchObject({ id: 'p9', serverMsgID: 'srv9', status: 'sent', key: 'srv9' })
  })

  it('marks a failed send and replaces it on a successful retry', () => {
    const me = { id: STAFF_ID, name: 'Alice' }
    let list = mergeMessages([], [pendingMessage('cm1', text('在吗'), me)])
    list = mergeMessages(list, [{ ...list[0]!, status: 'failed' }])
    expect(list).toHaveLength(1)
    expect(list[0]!.status).toBe('failed')

    list = mergeMessages(list, [
      fromApi(
        api({
          id: 'p1',
          channel_msg_id: 'srv1',
          client_msg_id: 'cm1',
          send_status: 'sent',
          sender_type: 'agent',
        }),
      ),
    ])
    expect(list).toHaveLength(1)
    expect(list[0]!.status).toBe('sent')
  })

  it('orders by send time', () => {
    const list = mergeMessages(
      [fromApi(api({ id: 'b', channel_msg_id: 'sb', sent_at: '2026-09-28T10:00:02Z' }))],
      [fromApi(api({ id: 'a', channel_msg_id: 'sa', sent_at: '2026-09-28T10:00:01Z' }))],
    )
    expect(list.map((m) => m.id)).toEqual(['a', 'b'])
  })
})

describe('attachments', () => {
  const FILE_URL = 'http://localhost:8000/api/v1/files/acme/2026/09/x/a.png?sig=abc'
  const image: Outgoing = {
    type: 'image',
    attachment: {
      url: FILE_URL,
      name: 'a.png',
      size: 2048,
      mime: 'image/png',
      width: 800,
      height: 600,
    },
  }

  it('reads image and file content from the platform', () => {
    const m = fromApi(
      api({
        content_type: 'file',
        text_plain: null,
        content: { url: FILE_URL, name: 'b.pdf', size: 99, width: null, height: null, mime: null },
      }),
    )
    expect(m.attachment).toEqual({
      url: FILE_URL,
      name: 'b.pdf',
      size: 99,
      width: null,
      height: null,
      mime: null,
    })
    expect(fromApi(api({})).attachment).toBeNull()
  })

  it('builds the send request and restores it for a retry', () => {
    const pending = pendingMessage('cm1', image, { id: STAFF_ID, name: 'Alice' })
    expect(pending).toMatchObject({ contentType: 'image', text: null, status: 'pending' })
    expect(sendBody('cm1', image)).toEqual({
      client_msg_id: 'cm1',
      type: 'image',
      attachment: {
        url: FILE_URL,
        name: 'a.png',
        size: 2048,
        content_type: 'image/png',
        width: 800,
        height: 600,
      },
    })
    expect(outgoingOf(pending)).toEqual(image)
    expect(sendBody('cm2', text('hi'))).toEqual({ client_msg_id: 'cm2', type: 'text', text: 'hi' })
    // IM 推送的文件消息没有 MIME 类型，不能据此重发。
    const echo = fromIm(
      im({
        contentType: 105,
        text: null,
        attachment: { url: FILE_URL, name: 'b.pdf', size: 9, width: null, height: null },
      }),
    )
    expect(outgoingOf(echo)).toBeNull()
  })

  it('keeps the MIME type when the IM echo merges into the sent message', () => {
    let list = mergeMessages([], [pendingMessage('cm1', image, { id: STAFF_ID, name: 'Alice' })])
    list = mergeMessages(list, [
      fromIm(
        im({
          serverMsgID: 'srv9',
          sendID: `acme_s_${STAFF}`,
          contentType: 102,
          text: null,
          attachment: { url: FILE_URL, name: null, size: 2048, width: 800, height: 600 },
          ex: '{"pmid":"p9"}',
        }),
      ),
    ])
    expect(list).toHaveLength(2)
    list = mergeMessages(list, [
      fromApi(
        api({
          id: 'p9',
          sender_type: 'agent',
          channel_msg_id: 'srv9',
          client_msg_id: 'cm1',
          source: 'api',
          send_status: 'sent',
          content_type: 'image',
          text_plain: null,
          content: {
            url: FILE_URL,
            name: 'a.png',
            size: 2048,
            width: 800,
            height: 600,
            mime: 'image/png',
          },
        }),
      ),
    ])
    expect(list).toHaveLength(1)
    expect(list[0]!.attachment).toMatchObject({ url: FILE_URL, mime: 'image/png' })
  })
})
