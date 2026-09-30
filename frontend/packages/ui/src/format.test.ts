import { describe, expect, it } from 'vitest'

import { formatSize, linkify, playableAudio } from './format'

describe('formatSize', () => {
  it.each([
    [null, ''],
    [0, ''],
    [1, '1 KB'],
    [2048, '2 KB'],
    [1024 * 1024, '1.0 MB'],
    [5.5 * 1024 * 1024, '5.5 MB'],
  ])('%s -> %s', (size, text) => {
    expect(formatSize(size)).toBe(text)
  })
})

describe('playableAudio', () => {
  it('rejects formats browsers cannot play', () => {
    expect(playableAudio('audio/mpeg')).toBe(true)
    expect(playableAudio(null)).toBe(true)
    expect(playableAudio('audio/amr')).toBe(false)
    expect(playableAudio('audio/SILK')).toBe(false)
  })
})

describe('linkify', () => {
  it('finds http and https links and keeps the rest as text', () => {
    expect(linkify('详见 https://help.example.com/a?b=1，谢谢')).toEqual([
      { kind: 'text', text: '详见 ' },
      { kind: 'link', text: 'https://help.example.com/a?b=1', href: 'https://help.example.com/a?b=1' },
      { kind: 'text', text: '，谢谢' },
    ])
  })

  it('leaves trailing punctuation out of links', () => {
    expect(linkify('see http://a.cn/x. and (https://b.cn/y)')).toEqual([
      { kind: 'text', text: 'see ' },
      { kind: 'link', text: 'http://a.cn/x', href: 'http://a.cn/x' },
      { kind: 'text', text: '. and (' },
      { kind: 'link', text: 'https://b.cn/y', href: 'https://b.cn/y' },
      { kind: 'text', text: ')' },
    ])
  })

  it('does not treat other schemes as links', () => {
    expect(linkify('javascript:alert(1) ftp://x')).toEqual([
      { kind: 'text', text: 'javascript:alert(1) ftp://x' },
    ])
  })
})
