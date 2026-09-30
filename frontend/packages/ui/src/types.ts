/** 共享组件展示的一条消息（各应用把自己的消息结构转换成这个形状）。 */
export interface MessageAttachment {
  url: string
  name: string | null
  size: number | null
  mime?: string | null
}

export interface MessageView {
  /** text、image、file、voice、video，其他类型显示为 [类型]。 */
  contentType: string
  text: string | null
  attachment: MessageAttachment | null
  /** 语音转写的文字。 */
  transcript?: string | null
  /** 附件因病毒拦截（blocked）或超过保留期（expired）被删除。 */
  removed?: { reason: string; name?: string | null } | null
}
