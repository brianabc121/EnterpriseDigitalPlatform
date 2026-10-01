import { describe, expect, it } from 'vitest'

import {
  actionTag,
  evidenceText,
  eventText,
  recordSummary,
  reviewDecisions,
  statusTag,
} from './formkb'

describe('表单知识（§25.18）', () => {
  it('学习记录里的表单动作', () => {
    expect(eventText({ form: 'order', event: 'created' })).toBe('下单')
    expect(eventText({ form: 'order', event: 'updated' })).toBe('改单')
    expect(eventText({ form: 'requisition', event: 'created' })).toBe('开单')
    expect(eventText({ form: 'requisition', event: 'updated' })).toBe('重新提交')
    expect(eventText({ form: 'receipt', event: 'confirmed' })).toBe('确认')
  })

  it('依据：叫法几次和比例、用量几个订单、搭配几张一起开、手工填写', () => {
    expect(evidenceText({ kind: 'alias', evidence: 3, share: 0.75, source: 'learned' })).toBe('3 次（占 75%）')
    expect(evidenceText({ kind: 'usage', evidence: 4, share: null, source: 'learned' })).toBe('4 个订单')
    expect(evidenceText({ kind: 'companion', evidence: 9, share: 0.75, source: 'learned' })).toBe('9 张一起开')
    expect(evidenceText({ kind: 'alias', evidence: 0, share: null, source: 'manual' })).toBe('手工填写')
  })

  it('待确认的处理方式', () => {
    expect(reviewDecisions('activate').map((d) => d.decision)).toEqual(['activate', 'keep'])
    expect(reviewDecisions('conflict').map((d) => d.label)).toEqual(['换成学到的', '保持不变'])
    expect(reviewDecisions('recipe').map((d) => d.label)).toEqual(['保持配方不变'])
    expect(reviewDecisions(null)).toEqual([])
  })

  it('判断结果一句话', () => {
    expect(recordSummary({ status: 'pending', result: [] })).toBe('正在判断…')
    expect(recordSummary({ status: 'done', result: [] })).toBe('没有需要更新的')
    expect(
      recordSummary({
        status: 'done',
        result: [{ entry_id: null, kind: 'alias', action: 'activated', text: '' }],
      }),
    ).toBe('1 处更新')
    expect(recordSummary({ status: 'failed', result: [] })).toBe('判断失败（已重试 3 次）')
  })

  it('标签颜色', () => {
    expect(statusTag('active')).toBe('success')
    expect(statusTag('disabled')).toBe('info')
    expect(actionTag('activated')).toBe('success')
    expect(actionTag('review')).toBe('warning')
    expect(actionTag('deactivated')).toBe('info')
    expect(actionTag('strengthened')).toBe('primary')
  })
})
