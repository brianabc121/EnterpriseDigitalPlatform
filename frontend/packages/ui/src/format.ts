/** 消息展示用的格式化函数：文件大小、能否直接播放的音频、文本里的链接。 */

/** 文件大小：1 MB 以上显示 MB（一位小数），否则显示 KB（向上取整）。 */
export function formatSize(size: number | null | undefined): string {
  if (!size) return ''
  return size >= 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(1)} MB` : `${Math.ceil(size / 1024)} KB`
}

/** 浏览器能直接播放的音频（微信客服的 AMR、SILK 语音转成 MP3 后才能播放）。 */
export function playableAudio(mime: string | null | undefined): boolean {
  return !mime || !/amr|silk/i.test(mime)
}

export type TextSegment = { kind: 'text'; text: string } | { kind: 'link'; text: string; href: string }

// http(s) 链接；结尾的中英文标点不算在链接里。
const URL_PATTERN = /https?:\/\/[^\s<>"'，。！？；：、）】》]+/gi
const TRAILING = /[.,!?;:)\]}'"]+$/

/** 把文本拆成普通文字和链接（只识别 http、https，渲染时不使用 v-html）。 */
export function linkify(text: string): TextSegment[] {
  const segments: TextSegment[] = []
  let last = 0
  for (const match of text.matchAll(URL_PATTERN)) {
    const raw = match[0]
    const start = match.index ?? 0
    const trailing = raw.match(TRAILING)?.[0] ?? ''
    const href = trailing ? raw.slice(0, -trailing.length) : raw
    if (start > last) segments.push({ kind: 'text', text: text.slice(last, start) })
    segments.push({ kind: 'link', text: href, href })
    last = start + href.length
  }
  if (last < text.length) segments.push({ kind: 'text', text: text.slice(last) })
  return segments
}
