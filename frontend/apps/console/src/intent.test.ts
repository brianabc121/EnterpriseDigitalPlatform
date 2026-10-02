import { describe, expect, it } from 'vitest'

import {
  decisionIntent,
  emotionLabel,
  intentLine,
  intentName,
  meter,
  percentText,
  stageChanges,
  stageTag,
  stageTone,
  type IntentJudgment,
  type SessionIntent,
} from './intent'

function judgment(overrides: Partial<IntentJudgment> = {}): IntentJudgment {
  return {
    stage: 4,
    stage_label: '准备下单',
    stage_probability: 0.82,
    purchase_probability: 0.865,
    has_purchase_intent: true,
    score: 3.6,
    distribution: [0.045, 0.045, 0.045, 0.045, 0.82],
    intent: 'delivery',
    intent_label: '库存发货',
    intent_probability: 0.8,
    intents: [{ code: 'delivery', label: '库存发货', probability: 0.8 }],
    concerns: [
      { code: 'delivery', label: '发货时效', probability: 0.4 },
      { code: 'price', label: '价格', probability: 0.4 },
    ],
    emotion: 0,
    emotion_label: '平静',
    human_probability: 0.03,
    route: null,
    source: 'judge',
    model: 'jev-1.13.0',
    ...overrides,
  }
}

describe('stages', () => {
  it('tags only clear purchase intent in the session list', () => {
    expect(stageTag(null)).toBeNull()
    expect(stageTag(2)).toBeNull()
    expect(stageTag(3)).toEqual({ label: '意向明确', type: 'warning' })
    expect(stageTag(4)).toEqual({ label: '准备下单', type: 'danger' })
    expect([0, 1, 3, 4].map(stageTone)).toEqual(['info', 'primary', 'warning', 'danger'])
  })

  it('lights the meter up to the stage', () => {
    expect(meter(0)).toEqual([false, false, false, false, false])
    expect(meter(2)).toEqual([true, true, true, false, false])
    expect(meter(4)).toEqual([true, true, true, true, true])
  })

  it('names intents, emotions and percentages', () => {
    expect(intentName('price')).toBe('询价比价')
    expect(intentName('x:定制尺寸')).toBe('定制尺寸')
    expect(intentName(null)).toBe('')
    expect([0.2, 1, 1.6].map(emotionLabel)).toEqual(['平静', '有些着急', '生气激动'])
    expect(percentText(0.865)).toBe('87%')
    expect(percentText(null)).toBe('')
  })
})

describe('summaries', () => {
  it('summarises a judgment in one line', () => {
    expect(intentLine(judgment())).toBe('准备下单 82% · 真实意图：库存发货 · 在意：发货时效、价格')
    expect(intentLine(judgment({ intent_label: null, concerns: [] }))).toBe('准备下单 82%')
  })

  it('merges repeated stages in the history', () => {
    const history: SessionIntent['history'] = [
      { at: '2026-10-02T10:01:00Z', stage: 2, stage_label: '有兴趣', purchase_probability: 0.1, intent_label: '询价比价' },
      { at: '2026-10-02T10:02:00Z', stage: 2, stage_label: '有兴趣', purchase_probability: 0.2, intent_label: '询价比价' },
      { at: '2026-10-02T10:03:00Z', stage: 4, stage_label: '准备下单', purchase_probability: 0.9, intent_label: null },
    ]
    expect(stageChanges(history, (iso) => iso.slice(11, 16))).toEqual([
      { at: '10:01', label: '有兴趣', stage: 2 },
      { at: '10:03', label: '准备下单', stage: 4 },
    ])
  })

  it('explains the judgment used by an AI decision', () => {
    expect(decisionIntent({})).toBeNull()
    expect(
      decisionIntent({
        intent: {
          stage: 3,
          purchase: 0.86,
          intent: 'x:定制尺寸',
          concerns: ['delivery'],
          emotion: 1,
          human: 0.03,
          source: 'judge',
        },
      }),
    ).toBe('下单意向：意向明确（86%） · 真实意图：定制尺寸 · 在意：发货时效 · 情绪：有些着急')
    expect(decisionIntent({ intent_human: 0.95 })).toBe('判断客户在要求人工（95%）')
  })
})
