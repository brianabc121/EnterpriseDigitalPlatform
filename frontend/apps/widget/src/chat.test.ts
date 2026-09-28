import type { ChatMessage } from '@edp/im-client'
import { describe, expect, it } from 'vitest'

import { mergeMessages, senderRole } from './chat'

const ME = 'acme_c_0123456789abcdef0123456789abcdef'

function msg(clientMsgID: string, sendTime: number, text = clientMsgID): ChatMessage {
  return {
    clientMsgID,
    serverMsgID: `s-${clientMsgID}`,
    sendID: ME,
    groupID: 'acme_r_1',
    seq: 0,
    sendTime,
    contentType: 101,
    text,
    ex: '',
  }
}

describe('senderRole', () => {
  it('recognises the platform IM ID conventions', () => {
    expect(senderRole(ME, ME)).toBe('me')
    expect(senderRole('acme_bot', ME)).toBe('bot')
    expect(senderRole('acme_sys', ME)).toBe('system')
    expect(senderRole('acme_s_0123456789abcdef0123456789abcdef', ME)).toBe('agent')
    expect(senderRole('someone-else', ME)).toBe('other')
  })
})

describe('mergeMessages', () => {
  it('deduplicates by clientMsgID and keeps time order', () => {
    const history = [msg('a', 1), msg('b', 2)]
    const merged = mergeMessages(history, [msg('c', 3), msg('b', 2, 'b-updated'), msg('z', 0)])

    expect(merged.map((m) => m.clientMsgID)).toEqual(['z', 'a', 'b', 'c'])
    expect(merged.find((m) => m.clientMsgID === 'b')?.text).toBe('b-updated')
  })
})
