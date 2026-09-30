/** 前端应用共用的组件与格式化函数（设计文档 §17.1 packages/ui）：消息内容、链接识别、文件大小。 */
export { formatSize, linkify, playableAudio, type TextSegment } from './format'
export { default as MessageBody } from './MessageBody.vue'
export { default as TextWithLinks } from './TextWithLinks.vue'
export type { MessageAttachment, MessageView } from './types'
