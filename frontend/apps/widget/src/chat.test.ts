import type { Schemas } from '@edp/api-client'
import type { ChatMessage } from '@edp/im-client'
import { describe, expect, it } from 'vitest'

import { freshMessages, fromApi, fromIm, mergeMessages, senderLabel, senderRole } from './chat'

const ME = 'acme_c_0123456789abcdef0123456789abcdef'
const AGENT = 'acme_s_' + 'a'.repeat(32)

function im(
  clientMsgID: string,
  sendTime: number,
  overrides: Partial<ChatMessage> = {},
): ChatMessage {
  return {
    clientMsgID,
    serverMsgID: `s-${clientMsgID}`,
    sendID: ME,
    senderNickname: '',
    groupID: 'acme_r_1',
    seq: 0,
    sendTime,
    contentType: 101,
    text: clientMsgID,
    attachment: null,
    ex: '',
    ...overrides,
  }
}

function api(
  serverMsgID: string,
  sentAt: string,
  overrides: Partial<Schemas['VisitorMessageOut']> = {},
): Schemas['VisitorMessageOut'] {
  return {
    id: `p-${serverMsgID}`,
    server_msg_id: serverMsgID,
    sender_type: 'system',
    sender_name: null,
    content_type: 'text',
    text: serverMsgID,
    attachment: null,
    sent_at: sentAt,
    ...overrides,
  }
}

describe('mergeMessages', () => {
  it('dedupes by IM message id and sorts by send time', () => {
    const merged = mergeMessages(
      [fromIm(im('b', 2000), ME)],
      [fromIm(im('a', 1000), ME), fromIm(im('b', 2000), ME)],
    )
    expect(merged.map((m) => m.text)).toEqual(['a', 'b'])
  })

  it('fills in messages the IM push missed from the history API', () => {
    const live = [fromIm(im('hi', 1000), ME)]
    const history = [
      api('s-hi', '1970-01-01T00:00:01.000Z', { sender_type: 'customer', text: 'hi' }),
      api('notice', '1970-01-01T00:00:01.500Z', { text: '客服 Alice 为您服务。' }),
      api('reply', '1970-01-01T00:00:02.000Z', {
        sender_type: 'agent',
        sender_name: 'Alice',
        text: '您好',
      }),
    ].map(fromApi)

    const merged = mergeMessages(live, history)

    expect(merged.map((m) => [m.role, m.senderName, m.text])).toEqual([
      ['me', null, 'hi'],
      ['system', null, '客服 Alice 为您服务。'],
      ['agent', 'Alice', '您好'],
    ])
    // IM 随后又推送到同一条消息：仍然只有一条。
    const again = mergeMessages(merged, [
      fromIm(
        im('reply', 2000, { serverMsgID: 'reply', sendID: AGENT, senderNickname: 'Alice' }),
        ME,
      ),
    ])
    expect(again).toHaveLength(3)
  })
})

describe('senderRole', () => {
  it('reads the sender from the IM user id', () => {
    expect(senderRole(ME, ME)).toBe('me')
    expect(senderRole('acme_bot', ME)).toBe('bot')
    expect(senderRole('acme_sys', ME)).toBe('system')
    expect(senderRole(AGENT, ME)).toBe('agent')
    expect(senderRole('acme_c_' + 'b'.repeat(32), ME)).toBe('other')
  })
})

describe('senderLabel', () => {
  it('names agents and the bot, and falls back to fixed labels', () => {
    expect(senderLabel({ role: 'bot', senderName: '小智' })).toBe('小智')
    expect(senderLabel({ role: 'bot', senderName: null })).toBe('智能客服')
    expect(senderLabel({ role: 'agent', senderName: 'Alice' })).toBe('Alice')
    expect(senderLabel({ role: 'system', senderName: 'x' })).toBe('系统消息')
  })
})

describe('freshMessages', () => {
  it('counts only messages not seen before and not sent by the visitor', () => {
    const seen = [fromIm(im('hi', 1000), ME), fromIm(im('reply', 2000, { sendID: AGENT }), ME)]
    const incoming = [
      // 同一条消息从历史接口又来一次（按 IM 消息 ID 对上）
      fromApi(api('s-reply', '2026-10-02T08:00:02Z', { sender_type: 'agent' })),
      fromIm(im('mine', 3000), ME),
      fromIm(im('new', 4000, { sendID: AGENT }), ME),
    ]
    expect(freshMessages(seen, incoming).map((m) => m.text)).toEqual(['new'])
  })

  it('counts a message once when it arrives twice in one batch or without a server id', () => {
    const pushed = fromIm(im('x', 1000, { sendID: AGENT, serverMsgID: '' }), ME)
    const confirmed = fromIm(im('x', 1000, { sendID: AGENT }), ME)
    expect(freshMessages([], [pushed, confirmed])).toHaveLength(1)
    expect(freshMessages([pushed], [confirmed])).toHaveLength(0)
  })
})
