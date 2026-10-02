import { describe, expect, it } from 'vitest'

import {
  aiUsage,
  amountText,
  categoryOptions,
  categoryPath,
  categoryTree,
  contractBlocks,
  missing,
  placeholders,
  titleOf,
  type Category,
} from './contracts'
import { contractHtml } from './print'

const BODY = [
  '# 定制加工合同',
  '甲方：{{客户名称}}  乙方：{{我方名称}}',
  '## 一、标的',
  '{{标的清单}}',
  '## 二、交付',
  '- 交货期限：{{交货期限}}',
  '**特别约定**：<script>alert(1)</script>',
].join('\n')

describe('placeholders', () => {
  it('lists fields in order and finds the missing ones', () => {
    expect(placeholders(BODY)).toEqual(['客户名称', '我方名称', '标的清单', '交货期限'])
    expect(missing(BODY, { 客户名称: '华东分公司', 我方名称: ' ' })).toEqual(['我方名称', '标的清单', '交货期限'])
    expect(titleOf(BODY)).toBe('定制加工合同')
  })
})

describe('contractBlocks', () => {
  const values = {
    客户名称: '华东分公司',
    我方名称: '某某服饰',
    标的清单: '| 名称 | 数量 |\n|---|---|\n| 工服 | 200 |',
  }

  it('lays out the title, clauses, lists and tables with the values filled in', () => {
    const blocks = contractBlocks(BODY, values)
    expect(blocks.map((b) => b.kind)).toEqual([
      'title',
      'paragraph',
      'heading',
      'table',
      'heading',
      'bullet',
      'paragraph',
    ])
    expect(blocks[0]?.spans).toEqual([{ text: '定制加工合同' }])
    expect(blocks[1]?.spans).toEqual([
      { text: '甲方：' },
      { text: '华东分公司', field: 'value' },
      { text: '  乙方：' },
      { text: '某某服饰', field: 'value' },
    ])
    expect(blocks[2]).toMatchObject({ kind: 'heading', level: 1 })
    expect(blocks[3]).toMatchObject({ header: true })
    expect(blocks[3]?.rows?.map((row) => row.map((cell) => cell[0]?.text))).toEqual([
      ['名称', '数量'],
      ['工服', '200'],
    ])
    expect(blocks[5]?.spans).toEqual([{ text: '交货期限：' }, { text: '交货期限', field: 'missing' }])
  })

  it('keeps markup as plain text and marks bold parts', () => {
    const last = contractBlocks(BODY, values).at(-1)
    expect(last?.spans).toEqual([
      { text: '特别约定', bold: true },
      { text: '：<script>alert(1)</script>' },
    ])
    expect(contractBlocks('**{{客户名称}}**', values)[0]?.spans).toEqual([
      { text: '华东分公司', field: 'value', bold: true },
    ])
  })
})

describe('contractHtml', () => {
  it('prints the same layout with markup escaped and blanks for missing fields', () => {
    const html = contractHtml(contractBlocks(BODY, { 客户名称: '华东<分公司>' }), 'HT-1 <定制>')
    expect(html).toContain('<title>HT-1 &lt;定制&gt;</title>')
    expect(html).toContain('<h1>定制加工合同</h1>')
    expect(html).toContain('甲方：华东&lt;分公司&gt;')
    expect(html).toContain('＿＿＿（我方名称）')
    expect(html).toContain('<p class="bullet">• 交货期限：＿＿＿（交货期限）</p>')
    expect(html).toContain('<strong>特别约定</strong>：&lt;script&gt;alert(1)&lt;/script&gt;')
    expect(html).not.toContain('<script>')
  })
})

describe('categories', () => {
  const items: Category[] = [
    { id: 'b', parent_id: null, name: '采购合同', sort: 2, templates: 0, contracts: 1 },
    { id: 'a', parent_id: null, name: '销售合同', sort: 1, templates: 1, contracts: 2 },
    { id: 'a1', parent_id: 'a', name: '定制加工', sort: 1, templates: 2, contracts: 3 },
    { id: 'a1x', parent_id: 'a1', name: '工服', sort: 1, templates: 0, contracts: 4 },
  ]

  it('builds a sorted tree with counts that include the sub-categories', () => {
    const tree = categoryTree(items)
    expect(tree.map((n) => n.label)).toEqual(['销售合同', '采购合同'])
    const sales = tree[0]!
    expect([sales.contracts, sales.templates, sales.depth]).toEqual([9, 3, 1])
    expect(sales.children[0]?.children[0]).toMatchObject({ label: '工服', depth: 3, contracts: 4 })
    expect(categoryOptions(tree)[0]?.children?.[0]?.children?.[0]).toEqual({ value: 'a1x', label: '工服' })
    expect(categoryPath(items, 'a1x')).toEqual(['a', 'a1', 'a1x'])
    expect(categoryPath(items, null)).toEqual([])
  })

  it('formats amounts and AI usage', () => {
    expect(amountText('12000')).toBe('¥12,000.00')
    expect(amountText(null)).toBe('—')
    const ai = { model: 'qwen-plus', prompt_tokens: 1200, completion_tokens: 834, cost: 1.5, knowledge: [], notes: [] }
    expect(aiUsage(ai)).toBe('qwen-plus · 2,034 Token · ¥0.0150')
    expect(aiUsage({ ...ai, model: null, cost: 0 })).toBe('AI · 2,034 Token')
  })
})
