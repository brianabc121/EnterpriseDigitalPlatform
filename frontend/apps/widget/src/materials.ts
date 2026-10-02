/** 企业资料的分享页（设计文档 §36.4）用的小工具。 */

/**
 * 页面地址里的分享令牌（?share=…）：有这个参数时 Widget 只显示分享的资料。没有参数返回 null；格式不对
 * （链接被截断或改动过）返回空字符串，页面直接提示链接失效。
 */
export function shareToken(search: string): string | null {
  const token = new URLSearchParams(search).get('share')
  if (token === null) return null
  return /^[\w-]{16,64}$/.test(token) ? token : ''
}

/** 文件大小：1.5 GB、12.3 MB、48 KB。 */
export function sizeText(bytes: number): string {
  const trim = (value: number): string => (value >= 100 ? value.toFixed(0) : value.toFixed(1).replace(/\.0$/, ''))
  if (bytes >= 1024 ** 3) return `${trim(bytes / 1024 ** 3)} GB`
  if (bytes >= 1024 ** 2) return `${trim(bytes / 1024 ** 2)} MB`
  return `${Math.max(1, Math.ceil(bytes / 1024))} KB`
}

export type ShareView = 'video' | 'image' | 'pdf' | 'markdown' | 'text' | 'download'

/** 怎么给客户看：视频播放、图片、PDF、文字资料排版、纯文本；其他（Office 文档、压缩包）只能下载。 */
export function shareView(material: { kind: string; ext: string; view_url: string | null; text: string | null }): ShareView {
  if (material.text !== null) return 'markdown'
  if (!material.view_url) return 'download'
  if (material.kind === 'video') return 'video'
  if (material.kind === 'image') return 'image'
  if (material.ext === '.pdf') return 'pdf'
  if (material.ext === '.md' || material.ext === '.markdown') return 'markdown'
  if (['.txt', '.csv'].includes(material.ext)) return 'text'
  return 'download'
}

/** 到期时间：10月9日 14:05。 */
export function expiryText(value: string): string {
  const date = new Date(value)
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${date.getMonth() + 1}月${date.getDate()}日 ${pad(date.getHours())}:${pad(date.getMinutes())}`
}
