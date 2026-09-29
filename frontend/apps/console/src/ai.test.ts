import { describe, expect, it } from 'vitest'

import { activeSignals, EVAL_SAMPLE, parseEvalCases } from './ai'

describe('parseEvalCases', () => {
  it('reads questions with expected keywords or a handoff', () => {
    const { cases, errors } = parseEvalCases(
      ['快递几天能到 | 2 到 3 天，偏远', '我要投诉｜转人工', '  ', '# 注释', '营业时间'].join('\n'),
    )

    expect(errors).toEqual([])
    expect(cases).toEqual([
      { question: '快递几天能到', expect_handoff: false, expect_keywords: ['2 到 3 天', '偏远'] },
      { question: '我要投诉', expect_handoff: true, expect_keywords: [] },
      { question: '营业时间', expect_handoff: false, expect_keywords: [] },
    ])
  })

  it('reports lines it cannot use', () => {
    const { cases, errors } = parseEvalCases(`| 转人工\n问题 | ${'长'.repeat(33)}`)

    expect(cases).toEqual([])
    expect(errors).toEqual(['第 1 行：缺少问题', '第 2 行：关键词不能超过 32 个字'])
  })

  it('parses the sample', () => {
    expect(parseEvalCases(EVAL_SAMPLE).cases).toHaveLength(2)
  })
})

describe('activeSignals', () => {
  it('lists the signals that fired, in weight order', () => {
    expect(
      activeSignals({ repeated: true, low_relevance: true, negative: false, best_relevance: 0.2 }),
    ).toEqual(['知识相关度低', '重复提问'])
  })
})
