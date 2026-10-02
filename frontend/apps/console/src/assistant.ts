/** AI 公司助理（设计文档 §27.3）用到的名称和小工具，与后端 app/modules/assistant 一致。 */
import type { Schemas } from '@edp/api-client'

export type Bot = Schemas['BotOut']
export type Provider = Bot['provider']
export type ProviderSpec = Schemas['ProviderOut']
export type Identity = Schemas['IdentityOut']
export type Group = Schemas['GroupOut']
export type ChatMessage = Schemas['ChatMessageOut']
export type AssistantSettings = Schemas['AssistantSettings']

export const PROVIDER_NAME: Record<string, string> = {
  wecom: '企业微信',
  dingtalk: '钉钉',
  feishu: '飞书',
  telegram: 'Telegram',
  whatsapp: 'WhatsApp',
}

export const BOT_STATUS: Record<string, string> = {
  active: '启用',
  disabled: '已停用',
}

export const REPLY_MODE: Record<string, string> = {
  silent: '只记录不说话',
  mentioned: '被 @ 时回答',
}

export const TOOL_NAME: Record<string, string> = {
  list_my_tasks: '我的待办',
  create_task: '记一件事',
  complete_task: '完成事项',
  list_work_todos: '客户待办',
  lookup_orders: '查订单',
  lookup_customer: '查客户',
  search_knowledge: '知识库',
  search_products: '查商品',
  team_overview: '团队概览',
}

/** 机器人的状态说明：停用、最近发送失败、正常。 */
export function botHealth(bot: Pick<Bot, 'status' | 'failures' | 'last_error'>): {
  label: string
  type: 'success' | 'warning' | 'info' | 'danger'
} {
  if (bot.status === 'disabled') return { label: '已停用', type: 'info' }
  if (bot.failures > 0) return { label: `发送失败 ${bot.failures} 次`, type: 'danger' }
  return { label: '正常', type: 'success' }
}

/** 从表单里的键值对整理出要提交的配置：去掉空值。 */
export function compactFields(values: Record<string, string>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(values)
      .map(([key, value]) => [key, value.trim()])
      .filter(([, value]) => value),
  )
}

/** 群的回复方式：按群的设置，没有时按租户的默认值。 */
export function effectiveReplyMode(
  group: Pick<Group, 'reply_mode'>,
  settings: Pick<AssistantSettings, 'group_reply_mode'> | null,
): string {
  return group.reply_mode ?? settings?.group_reply_mode ?? 'silent'
}
