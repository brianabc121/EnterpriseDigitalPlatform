import type { Schemas } from '@edp/api-client'

/**
 * 项目合同管理（设计文档 §34）：状态、列表页签、正文的预览排版（与后端 contracts/document.py 同一套
 * 简单 Markdown）、填写项和分类树。
 */
export type Contract = Schemas['ContractOut']
export type ContractSummary = Schemas['ContractSummary']
export type ContractPage = Schemas['ContractPage']
export type ContractStatus = Contract['status']
export type ContractView = 'all' | 'draft' | 'final' | 'signed' | 'expiring' | 'expired' | 'void'
export type Category = Schemas['ContractCategoryOut']
export type Template = Schemas['ContractTemplateOut']
export type TemplateSummary = Schemas['ContractTemplateSummary']
export type TemplateField = Schemas['ContractTemplateField']
export type ContractSettings = Schemas['ContractSettings']
export type ContractParty = Schemas['ContractParty']
export type KnowledgeRef = Schemas['ContractKnowledgeRef']

export const STATUS_LABEL: Record<ContractStatus, string> = {
  draft: '草稿',
  final: '已定稿',
  signed: '已签署',
  void: '已作废',
}

export const STATUS_TAG: Record<ContractStatus, 'info' | 'primary' | 'success' | 'danger'> = {
  draft: 'info',
  final: 'primary',
  signed: 'success',
  void: 'danger',
}

export const VIEWS: [ContractView, string][] = [
  ['all', '全部'],
  ['draft', '草稿'],
  ['final', '已定稿'],
  ['signed', '已签署'],
  ['expiring', '快到期'],
  ['expired', '已到期'],
  ['void', '已作废'],
]

export const EXPIRY_LABEL: Record<'expiring' | 'expired', string> = {
  expiring: '快到期',
  expired: '已到期',
}

const PLACEHOLDER = /\{\{\s*([^{}\n]{1,40}?)\s*\}\}/g

/** 正文里的填写项名称（按出现的先后，不重复）。 */
export function placeholders(body: string): string[] {
  const names: string[] = []
  for (const match of body.matchAll(PLACEHOLDER)) {
    const name = (match[1] ?? '').trim()
    if (name && !names.includes(name)) names.push(name)
  }
  return names
}

/** 还没有值的填写项。 */
export function missing(body: string, values: Record<string, string>): string[] {
  return placeholders(body).filter((name) => !(values[name] ?? '').trim())
}

// 填写项在排版前换成占位标记（私用区字符），排版之后再换成值或"待填写"。
const MISSING_OPEN = '\uE000'
const MISSING_CLOSE = '\uE001'
const VALUE_OPEN = '\uE002'
const VALUE_CLOSE = '\uE003'
const TOKENS = /\*\*(.+?)\*\*|\uE000(.*?)\uE001|\uE002(.*?)\uE003/g
const FIELDS = /\uE000(.*?)\uE001|\uE002(.*?)\uE003/g

/** 一段文字：加粗、填好的填写项（value）、没填的填写项（missing，text 是名称）。 */
export interface Span {
  text: string
  bold?: boolean
  field?: 'value' | 'missing'
}

export interface Block {
  kind: 'title' | 'heading' | 'paragraph' | 'bullet' | 'table'
  /** 条款标题的级别：## 是 1，### 是 2。 */
  level?: number
  spans?: Span[]
  rows?: Span[][][]
  header?: boolean
}

function fieldSpans(text: string, bold: boolean): Span[] {
  const spans: Span[] = []
  let last = 0
  for (const match of text.matchAll(FIELDS)) {
    const at = match.index ?? 0
    if (at > last) spans.push({ text: text.slice(last, at), ...(bold ? { bold } : {}) })
    if (match[1] !== undefined) spans.push({ text: match[1], field: 'missing', ...(bold ? { bold } : {}) })
    else spans.push({ text: match[2] ?? '', field: 'value', ...(bold ? { bold } : {}) })
    last = at + match[0].length
  }
  if (last < text.length) spans.push({ text: text.slice(last), ...(bold ? { bold } : {}) })
  return spans
}

function spansOf(text: string): Span[] {
  const spans: Span[] = []
  let last = 0
  for (const match of text.matchAll(TOKENS)) {
    const at = match.index ?? 0
    if (at > last) spans.push({ text: text.slice(last, at) })
    if (match[1] !== undefined) spans.push(...fieldSpans(match[1], true))
    else if (match[2] !== undefined) spans.push({ text: match[2], field: 'missing' })
    else spans.push({ text: match[3] ?? '', field: 'value' })
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

const SEPARATOR = /^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/

/**
 * 正文排成块（预览、打印，与导出的 Word 同一套规则）：第一个一级标题是合同名称，## 和 ### 是
 * 条款标题，每行一个段落，"- " 开头的是列表项，"|" 开头的连续几行是表格，**加粗**。填写项换成值，
 * 多行的值（例如标的清单的表格）直接参与排版；没填的标出来。
 */
export function contractBlocks(body: string, values: Record<string, string>): Block[] {
  const filled = body.replace(PLACEHOLDER, (_, raw: string) => {
    const name = raw.trim()
    const value = values[name]
    if (!value) return `${MISSING_OPEN}${name}${MISSING_CLOSE}`
    return value.includes('\n') ? value : `${VALUE_OPEN}${value}${VALUE_CLOSE}`
  })
  const lines = filled.replace(/\r\n/g, '\n').split('\n')
  const blocks: Block[] = []
  let titled = false
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
        rows: rows.map((row) => [...row, ...Array<string>(width - row.length).fill('')].map(spansOf)),
      })
      continue
    }
    const heading = /^(#{1,3})\s+(.+?)\s*#*\s*$/.exec(line)
    const bullet = /^\s*[-*•]\s+(.*)$/.exec(line)
    if (heading) {
      const level = (heading[1] ?? '#').length
      if (level === 1 && !titled) {
        blocks.push({ kind: 'title', spans: spansOf(heading[2] ?? '') })
        titled = true
      } else {
        blocks.push({ kind: 'heading', level: Math.max(1, level - 1), spans: spansOf(heading[2] ?? '') })
      }
    } else if (bullet) {
      blocks.push({ kind: 'bullet', spans: spansOf(bullet[1] ?? '') })
    } else {
      blocks.push({ kind: 'paragraph', spans: spansOf(line.trim()) })
    }
    index += 1
  }
  return blocks
}

/** 正文的第一个一级标题。 */
export function titleOf(body: string): string {
  const match = /^#\s+(.+?)\s*#*\s*$/m.exec(body)
  return match ? (match[1] ?? '').trim() : ''
}

// ---- 分类树 ----

export interface CategoryNode {
  id: string
  label: string
  /** 这个分类和下级分类里的合同数、模板数。 */
  contracts: number
  templates: number
  depth: number
  children: CategoryNode[]
}

/** 分类排成树（按顺序），数量包含下级分类。 */
export function categoryTree(items: Category[]): CategoryNode[] {
  const byParent = new Map<string | null, Category[]>()
  for (const item of items) {
    const key = item.parent_id ?? null
    const list = byParent.get(key) ?? []
    list.push(item)
    byParent.set(key, list)
  }
  const build = (parent: string | null, depth: number): CategoryNode[] =>
    (byParent.get(parent) ?? [])
      .slice()
      .sort((a, b) => a.sort - b.sort)
      .map((item) => {
        const children = depth < 10 ? build(item.id, depth + 1) : []
        return {
          id: item.id,
          label: item.name,
          depth,
          children,
          contracts: item.contracts + children.reduce((sum, c) => sum + c.contracts, 0),
          templates: item.templates + children.reduce((sum, c) => sum + c.templates, 0),
        }
      })
  return build(null, 1)
}

/** 级联选择器用的选项（值是分类 id）。 */
export interface CategoryOption {
  value: string
  label: string
  children?: CategoryOption[]
}

export function categoryOptions(nodes: CategoryNode[]): CategoryOption[] {
  return nodes.map((node) => ({
    value: node.id,
    label: node.label,
    ...(node.children.length ? { children: categoryOptions(node.children) } : {}),
  }))
}

/** 分类的路径（从第一级到它自己的 id），给级联选择器用。 */
export function categoryPath(items: Category[], id: string | null | undefined): string[] {
  const byId = new Map(items.map((item) => [item.id, item]))
  const path: string[] = []
  let current = id ? byId.get(id) : undefined
  while (current && !path.includes(current.id)) {
    path.unshift(current.id)
    current = current.parent_id ? byId.get(current.parent_id) : undefined
  }
  return path
}

/** 金额：¥12,000.00；没有时为"—"。 */
export function amountText(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  const number = Number(value)
  if (!Number.isFinite(number)) return '—'
  return `¥${number.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

/** AI 起草用的模型和用量：模型 · Token · 费用（元；接口里是分）。 */
export function aiUsage(ai: Schemas['ContractAiInfo']): string {
  const tokens = ai.prompt_tokens + ai.completion_tokens
  const parts = [ai.model ?? 'AI', `${tokens.toLocaleString('zh-CN')} Token`]
  if (ai.cost > 0) parts.push(`¥${(ai.cost / 100).toFixed(4)}`)
  return parts.join(' · ')
}

/** 新建模板时的正文骨架：常见的条款结构，内置填写项由系统填写，其他的用合同时再填。 */
export const STARTER_BODY = [
  '# 合同名称',
  '',
  '合同编号：{{合同编号}}',
  '甲方（买方）：{{客户名称}}',
  '乙方（卖方）：{{我方名称}}',
  '',
  '## 一、合同标的',
  '{{标的清单}}',
  '',
  '## 二、价款与支付',
  '合同金额：人民币 {{合同金额}} 元（{{合同金额大写}}）。',
  '付款方式：{{付款方式}}',
  '',
  '## 三、交付与验收',
  '- 交货期限：{{交货期限}}',
  '- 交货地点：{{交货地点}}',
  '- 甲方收货后 {{验收天数}} 日内验收，逾期未提出异议的视为验收合格。',
  '',
  '## 四、售后与质保',
  '{{质保条款}}',
  '',
  '## 五、违约责任',
  '{{违约责任}}',
  '',
  '## 六、争议解决',
  '履行本合同发生争议的，双方协商解决；协商不成的，向乙方所在地人民法院起诉。',
  '',
  '## 七、其他',
  '本合同一式两份，双方各执一份，自双方签字盖章之日起生效。',
  '',
  '甲方（盖章）：{{客户名称}}　　乙方（盖章）：{{我方名称}}',
  '签订日期：{{签订日期}}',
].join('\n')

/** 模板的预览值：填写项的默认值。 */
export function defaultValues(fields: TemplateField[]): Record<string, string> {
  return Object.fromEntries(fields.filter((f) => f.default.trim()).map((f) => [f.name, f.default]))
}
