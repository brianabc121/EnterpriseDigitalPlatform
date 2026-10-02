import { describe, expect, it } from 'vitest'

import { brandInfo, canResend, printedText, USE_LABEL } from './printing'

describe('printing', () => {
  it('names the vendor fields per brand', () => {
    expect(brandInfo('xpyun').keyLabel).toBe('UserKEY')
    expect(brandInfo('feie').needsDeviceKey).toBe(true)
    expect(brandInfo('unknown').value).toBe('xpyun')
  })

  it('describes the printed count and the uses', () => {
    expect(printedText(0)).toBe('')
    expect(printedText(2)).toBe('已打印 2 次')
    expect(USE_LABEL.order).toContain('加工单')
  })

  it('allows resending only failed jobs', () => {
    expect(canResend({ status: 'dead' })).toBe(true)
    expect(canResend({ status: 'retrying' })).toBe(true)
    expect(canResend({ status: 'printed' })).toBe(false)
  })
})
