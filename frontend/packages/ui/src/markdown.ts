/**
 * 简单 Markdown 排成块（企业资料的文字资料：控制台预览和客户的分享页共用）。不生成 HTML，组件按块渲染，
 * 不使用 v-html：标题（#、##、###）、段落（每行一段，空行分隔）、列表（"- "、"* "、"1. "）、引用（"> "）、
 * 表格（"|" 开头的连续几行，第二行是分隔线时第一行是表头）、分隔线（---）、**加粗**和链接（只识别 http、https）。
 */
export interface MarkdownSpan {
  text: string
  bold?: boolean
}

export type MarkdownBlock =
  | { kind: 'heading'; level: 1 | 2 | 3; spans: MarkdownSpan[] }
  | { kind: 'paragraph'; spans: MarkdownSpan[] }
  | { kind: 'quote'; spans: MarkdownSpan[] }
  | { kind: 'list'; ordered: boolean; items: MarkdownSpan[][] }
  | { kind: 'table'; header: boolean; rows: MarkdownSpan[][][] }
  | { kind: 'rule' }

const BOLD = /\*\*(.+?)\*\*/g
const SEPARATOR = /^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/
const BULLET = /^\s*[-*•]\s+(.*)$/
const ORDERED = /^\s*\d+[.、)]\s+(.*)$/

export function markdownSpans(text: string): MarkdownSpan[] {
  const spans: MarkdownSpan[] = []
  let last = 0
  for (const match of text.matchAll(BOLD)) {
    const at = match.index ?? 0
    if (at > last) spans.push({ text: text.slice(last, at) })
    spans.push({ text: match[1] ?? '', bold: true })
    last = at + match[0].length
  }
  if (last < text.length) spans.push({ text: text.slice(last) })
  return spans.length ? spans : [{ text: '' }]
}

function cells(line: string): string[] {
  let inner = line.trim()
  if (inner.startsWith('|')) inner = inner.slice(1)
  if (inner.endsWith('|')) inner = inner.slice(0, -1)
  return inner.split('|').map((cell) => cell.trim())
}

export function markdownBlocks(source: string): MarkdownBlock[] {
  const lines = source.replace(/\r\n?/g, '\n').split('\n')
  const blocks: MarkdownBlock[] = []
  let index = 0
  while (index < lines.length) {
    const line = (lines[index] ?? '').trimEnd()
    if (!line.trim()) {
      index += 1
      continue
    }
    if (line.trimStart().startsWith('|')) {
      const rows: string[][] = []
      let header = false
      while (index < lines.length && (lines[index] ?? '').trimStart().startsWith('|')) {
        const current = (lines[index] ?? '').trim()
        if (SEPARATOR.test(current)) header = rows.length === 1
        else rows.push(cells(current))
        index += 1
      }
      const width = Math.max(...rows.map((r) => r.length))
      blocks.push({
        kind: 'table',
        header,
        rows: rows.map((row) =>
          [...row, ...Array<string>(width - row.length).fill('')].map(markdownSpans),
        ),
      })
      continue
    }
    if (BULLET.test(line) || ORDERED.test(line)) {
      const ordered = !BULLET.test(line)
      const pattern = ordered ? ORDERED : BULLET
      const items: MarkdownSpan[][] = []
      while (index < lines.length) {
        const match = pattern.exec((lines[index] ?? '').trimEnd())
        if (!match) break
        items.push(markdownSpans(match[1] ?? ''))
        index += 1
      }
      blocks.push({ kind: 'list', ordered, items })
      continue
    }
    const heading = /^(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line)
    if (heading) {
      const level = Math.min(3, (heading[1] ?? '#').length) as 1 | 2 | 3
      blocks.push({ kind: 'heading', level, spans: markdownSpans(heading[2] ?? '') })
    } else if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      blocks.push({ kind: 'rule' })
    } else if (/^\s*>\s?/.test(line)) {
      blocks.push({ kind: 'quote', spans: markdownSpans(line.replace(/^\s*>\s?/, '')) })
    } else {
      blocks.push({ kind: 'paragraph', spans: markdownSpans(line.trim()) })
    }
    index += 1
  }
  return blocks
}
