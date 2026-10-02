import { describe, expect, it } from 'vitest'

import { shareToken, shareView, sizeText } from './materials'

describe('material share page', () => {
  it('reads the share token from the page address', () => {
    expect(shareToken('?share=abcdefghijklmnop_-12')).toBe('abcdefghijklmnop_-12')
    expect(shareToken('?share=short')).toBe('')
    expect(shareToken('?key=acme.x')).toBeNull()
  })

  it('decides how to show the material', () => {
    const base = { view_url: 'https://oss/x', text: null }
    expect(shareView({ ...base, kind: 'video', ext: '.mp4' })).toBe('video')
    expect(shareView({ ...base, kind: 'image', ext: '.png' })).toBe('image')
    expect(shareView({ ...base, kind: 'document', ext: '.pdf' })).toBe('pdf')
    expect(shareView({ ...base, kind: 'document', ext: '.md' })).toBe('markdown')
    expect(shareView({ ...base, kind: 'document', ext: '.txt' })).toBe('text')
    expect(shareView({ kind: 'document', ext: '.docx', view_url: null, text: null })).toBe('download')
    expect(shareView({ kind: 'text', ext: '.md', view_url: 'https://oss/t', text: '# 标题' })).toBe('markdown')
  })

  it('formats sizes', () => {
    expect(sizeText(300)).toBe('1 KB')
    expect(sizeText(12.34 * 1024 ** 2)).toBe('12.3 MB')
    expect(sizeText(2 * 1024 ** 3)).toBe('2 GB')
  })
})
