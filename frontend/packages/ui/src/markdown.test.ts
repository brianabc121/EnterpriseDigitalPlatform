import { describe, expect, it } from 'vitest'

import { markdownBlocks, markdownSpans } from './markdown'

describe('markdownBlocks', () => {
  it('lays out headings, paragraphs, lists, quotes, rules and tables', () => {
    const blocks = markdownBlocks(
      [
        '# 门锁安装说明',
        '',
        '安装前**先断电**。',
        '- 拆下旧锁',
        '- 装上新锁',
        '1. 第一步',
        '2. 第二步',
        '> 注意：不要用力敲打',
        '---',
        '| 型号 | 尺寸 |',
        '| --- | --- |',
        '| A1 | 24cm |',
        '#### 附录',
      ].join('\n'),
    )
    expect(blocks.map((b) => b.kind)).toEqual([
      'heading',
      'paragraph',
      'list',
      'list',
      'quote',
      'rule',
      'table',
      'heading',
    ])
    expect(blocks[1]).toEqual({
      kind: 'paragraph',
      spans: [{ text: '安装前' }, { text: '先断电', bold: true }, { text: '。' }],
    })
    expect(blocks[2]).toMatchObject({ kind: 'list', ordered: false, items: [[{ text: '拆下旧锁' }], [{ text: '装上新锁' }]] })
    expect(blocks[3]).toMatchObject({ kind: 'list', ordered: true })
    expect(blocks[6]).toMatchObject({ kind: 'table', header: true })
    expect(blocks[7]).toMatchObject({ kind: 'heading', level: 3 })
  })

  it('keeps plain text and empty input safe', () => {
    expect(markdownBlocks('')).toEqual([])
    expect(markdownBlocks('<script>alert(1)</script>')).toEqual([
      { kind: 'paragraph', spans: [{ text: '<script>alert(1)</script>' }] },
    ])
    expect(markdownSpans('')).toEqual([{ text: '' }])
  })
})
