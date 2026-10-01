import { describe, expect, it } from 'vitest'

import {
  baseSubject,
  guessProvider,
  invalidIgnore,
  mailboxStatus,
  parseIgnore,
  replySubject,
  replyTarget,
  type MailProvider,
} from './mail'
import type { WorkbenchMessage } from './workbench/messages'

const server = { host: '', port: 993, security: 'ssl' as const }
const provider = (key: string, domains: string[]): MailProvider => ({
  key,
  name: key,
  domains,
  imap: server,
  smtp: server,
  secret_label: '授权码',
  help: '',
})
const PROVIDERS = [
  provider('netease163', ['163.com']),
  provider('qq', ['qq.com', 'foxmail.com', 'vip.qq.com']),
  provider('gmail', ['gmail.com']),
  provider('custom', []),
]

function email(id: string, sender: 'customer' | 'agent', subject: string): WorkbenchMessage {
  return {
    key: id,
    id,
    serverMsgID: null,
    clientMsgID: null,
    senderType: sender,
    senderID: null,
    senderName: null,
    text: subject,
    contentType: 'email',
    attachment: null,
    sentAt: Number(id),
    status: null,
    email: {
      subject,
      from: null,
      to: [],
      cc: [],
      text: '',
      quoted: '',
      html: false,
      url: null,
      attachments: 0,
    },
  }
}

describe('邮件渠道（§10.8）', () => {
  it('按地址的域名选择邮箱类型，猜不出时是其他邮箱', () => {
    expect(guessProvider(PROVIDERS, 'li@163.com')).toBe('netease163')
    expect(guessProvider(PROVIDERS, 'Li@FoxMail.com')).toBe('qq')
    expect(guessProvider(PROVIDERS, 'li@gmail.com')).toBe('gmail')
    expect(guessProvider(PROVIDERS, 'li@company.cn')).toBe('custom')
    expect(guessProvider(PROVIDERS, 'li')).toBe('custom')
  })

  it('邮箱状态：正常、连接失败、已暂停、已停用', () => {
    expect(mailboxStatus({ status: 'active', failures: 0 }).label).toBe('正常')
    expect(mailboxStatus({ status: 'active', failures: 2 })).toEqual({
      label: '连接失败',
      type: 'warning',
    })
    expect(mailboxStatus({ status: 'paused', failures: 3 }).label).toBe('已暂停')
    expect(mailboxStatus({ status: 'disabled', failures: 0 }).label).toBe('已停用')
  })

  it('忽略的发件人：一行一个，去重并检查格式', () => {
    const items = parseIgnore('Boss@Partner.com\n@spam.cn， @spam.cn\n\nspam')
    expect(items).toEqual(['boss@partner.com', '@spam.cn', 'spam'])
    expect(invalidIgnore(items)).toEqual(['spam'])
  })

  it('回复的主题去掉已有的前缀', () => {
    expect(baseSubject('回复：Re: RE：订购 100 个')).toBe('订购 100 个')
    expect(replySubject('Fwd: 询价')).toBe('Re: 询价')
    expect(replySubject('  ')).toBe('Re: 您的来信')
  })

  it('默认回复最近一封客户邮件，也可以指定', () => {
    const messages = [
      email('1', 'customer', '第一封'),
      email('2', 'customer', '第二封'),
      email('3', 'agent', 'Re: 第二封'),
    ]
    expect(replyTarget(messages, null)?.id).toBe('2')
    expect(replyTarget(messages, '1')?.id).toBe('1')
    expect(replyTarget(messages, '3')).toBeNull()
    expect(replyTarget([], null)).toBeNull()
  })
})
