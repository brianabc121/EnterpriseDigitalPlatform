import { describe, expect, it } from 'vitest'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'

import MessageBody from './MessageBody.vue'
import type { MessageView } from './types'

async function render(message: MessageView, slots: Record<string, () => unknown> = {}): Promise<string> {
  return renderToString(createSSRApp({ render: () => h(MessageBody, { message }, slots) }))
}

describe('MessageBody', () => {
  it('renders text with clickable links and escapes markup', async () => {
    const html = await render({
      contentType: 'text',
      text: '<b>x</b> 看 https://a.cn',
      attachment: null,
    })
    expect(html).toContain('&lt;b&gt;x&lt;/b&gt;')
    expect(html).toContain('href="https://a.cn"')
    expect(html).toContain('rel="noopener noreferrer nofollow"')
  })

  it('renders files with name and size', async () => {
    const html = await render({
      contentType: 'file',
      text: null,
      attachment: { url: '/f/1', name: '报价单.pdf', size: 2048 },
    })
    expect(html).toContain('data-testid="message-file"')
    expect(html).toContain('报价单.pdf')
    expect(html).toContain('2 KB')
  })

  it('offers a download for voice the browser cannot play, with the transcript', async () => {
    const html = await render({
      contentType: 'voice',
      text: null,
      attachment: { url: '/v/1', name: null, size: null, mime: 'audio/amr' },
      transcript: '我想退货',
    })
    expect(html).not.toContain('<audio')
    expect(html).toContain('下载语音')
    expect(html).toContain('data-testid="voice-transcript"')
  })

  it('lets the app replace the image and shows removed files', async () => {
    const image = await render(
      { contentType: 'image', text: null, attachment: { url: '/i/1', name: null, size: null } },
      { image: () => h('span', { class: 'custom' }, 'preview') },
    )
    expect(image).toContain('class="custom"')
    const removed = await render({
      contentType: 'file',
      text: null,
      attachment: null,
      removed: { reason: 'blocked', name: 'a.exe' },
    })
    expect(removed).toContain('文件含有病毒，已被拦截：a.exe')
  })
})
