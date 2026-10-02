import { describe, expect, it } from 'vitest'

import {
  batches,
  canJoinKnowledge,
  checkFile,
  fetchTextPreview,
  folderOptions,
  folderPath,
  folderTree,
  kindOf,
  plainExcerpt,
  previewKind,
  sizeText,
  uploadMaterial,
  usagePercent,
  usageText,
  type Material,
  type MaterialFolder,
  type MaterialUpload,
  type UploadSteps,
} from './materials'

const CONFIG = {
  extensions: {
    video: ['.mp4', '.mov'],
    document: ['.pdf', '.docx', '.xlsx', '.txt'],
    image: ['.png', '.jpg'],
    text: ['.md'],
    other: ['.zip'],
  },
  video_max_bytes: 2 * 1024 ** 3,
  file_max_bytes: 200 * 1024 ** 2,
}

describe('sizes and usage', () => {
  it('formats sizes and storage usage', () => {
    expect(sizeText(512)).toBe('512 B')
    expect(sizeText(48 * 1024 + 1)).toBe('49 KB')
    expect(sizeText(12.34 * 1024 ** 2)).toBe('12.3 MB')
    expect(sizeText(2 * 1024 ** 3)).toBe('2 GB')
    expect(sizeText(150 * 1024 ** 3)).toBe('150 GB')
    expect(usageText(1024 ** 3, 50 * 1024 ** 3)).toBe('已用 1 GB，共 50 GB')
    expect(usageText(1024, null)).toBe('已用 1 KB')
    expect(usagePercent(25, 100)).toBe(25)
    expect(usagePercent(1, 0)).toBe(100)
    expect(usagePercent(1, null)).toBeNull()
  })
})

describe('folders', () => {
  const folders: MaterialFolder[] = [
    { id: 'b', parent_id: null, name: '产品视频', sort: 2, materials: 1 },
    { id: 'a', parent_id: null, name: '公司介绍', sort: 1, materials: 2 },
    { id: 'c', parent_id: 'b', name: '安装教程', sort: 1, materials: 3 },
    { id: 'd', parent_id: 'c', name: '门锁', sort: 1, materials: 4 },
  ]

  it('builds the tree with counts that include sub-folders', () => {
    const tree = folderTree(folders)
    expect(tree.map((n) => [n.label, n.count, n.depth])).toEqual([
      ['公司介绍', 2, 1],
      ['产品视频', 8, 1],
    ])
    expect(tree[1]?.children[0]?.children[0]).toMatchObject({ label: '门锁', count: 4, depth: 3 })
  })

  it('gives cascader options without the folder being moved and paths', () => {
    const options = folderOptions(folderTree(folders), 'c')
    expect(options).toEqual([
      { value: 'a', label: '公司介绍' },
      { value: 'b', label: '产品视频' },
    ])
    expect(folderPath(folders, 'd')).toEqual(['b', 'c', 'd'])
    expect(folderPath(folders, null)).toEqual([])
  })
})

describe('kinds, preview and knowledge', () => {
  it('classifies files and checks them before upload', () => {
    expect(kindOf('演示.MP4', CONFIG.extensions)).toBe('video')
    expect(kindOf('报价单.pdf', CONFIG.extensions)).toBe('document')
    expect(kindOf('run.exe', CONFIG.extensions)).toBeNull()
    expect(checkFile({ name: '报价单.pdf', size: 10 }, CONFIG)).toBeNull()
    expect(checkFile({ name: 'run.exe', size: 10 }, CONFIG)).toContain('不支持')
    expect(checkFile({ name: '说明.md', size: 10 }, CONFIG)).toContain('不支持')
    expect(checkFile({ name: '空.pdf', size: 0 }, CONFIG)).toContain('空文件')
    expect(checkFile({ name: '大.pdf', size: 201 * 1024 ** 2 }, CONFIG)).toContain('200 MB')
    expect(checkFile({ name: '大.mp4', size: 201 * 1024 ** 2 }, CONFIG)).toBeNull()
  })

  it('decides how to preview and what can join the knowledge base', () => {
    expect(previewKind({ kind: 'text', ext: '.md' })).toBe('markdown')
    expect(previewKind({ kind: 'document', ext: '.pdf' })).toBe('pdf')
    expect(previewKind({ kind: 'document', ext: '.txt' })).toBe('text')
    expect(previewKind({ kind: 'document', ext: '.xlsx' })).toBe('none')
    expect(previewKind({ kind: 'video', ext: '.mov' })).toBe('video')
    expect(canJoinKnowledge({ kind: 'document', ext: '.docx', status: 'ready' })).toBe(true)
    expect(canJoinKnowledge({ kind: 'document', ext: '.xlsx', status: 'ready' })).toBe(false)
    expect(canJoinKnowledge({ kind: 'text', ext: '.md', status: 'blocked' })).toBe(false)
  })

  it('reads the start of a long text document with a range request', async () => {
    const seen: (RequestInit | undefined)[] = []
    const body = new TextEncoder().encode('门'.repeat(100_000))
    const fetcher = (async (_url: string, init?: RequestInit) => {
      seen.push(init)
      return new Response(body, { status: 206 })
    }) as typeof fetch
    const long = await fetchTextPreview('https://oss/a.txt', body.length, fetcher)
    expect(seen[0]).toEqual({ headers: { Range: 'bytes=0-204799' } })
    expect(long.partial).toBe(true)
    expect(long.text).toBe('门'.repeat(Math.floor(204800 / 3)))
    const short = await fetchTextPreview('https://oss/b.txt', 3, (async () => new Response('abc')) as typeof fetch)
    expect(short).toEqual({ text: 'abc', partial: false })
    await expect(
      fetchTextPreview('https://oss/c.txt', 3, (async () => new Response('', { status: 403 })) as typeof fetch),
    ).rejects.toThrow('403')
  })

  it('strips Markdown marks from the excerpt on cards', () => {
    const text = ['# 门锁安装说明', '', '安装前**先断电**。', '- 拆下旧锁', '1. 第一步', '| 型号 | 尺寸 |', '| --- | --- |', '| X1 | 24 mm |'].join('\n')
    expect(plainExcerpt(text)).toBe(['门锁安装说明', '安装前先断电。', '拆下旧锁', '第一步', '型号  尺寸', 'X1  24 mm'].join('\n'))
    expect(plainExcerpt(null)).toBe('')
    expect(plainExcerpt('一二三四五', 3)).toBe('一二三')
  })

  it('splits part numbers into batches', () => {
    expect(batches(5, 2)).toEqual([[1, 2], [3, 4], [5]])
    expect(batches(1, 20)).toEqual([[1]])
  })
})

function material(id: string): Material {
  return { id, status: 'ready' } as Material
}

function fakeSteps(ticket: Omit<MaterialUpload, 'material'>, failures: Record<number, number> = {}) {
  const calls: string[] = []
  const puts: { url: string; size: number; headers: Record<string, string> }[] = []
  let completed: { part_number: number; etag: string }[] | null | undefined
  const steps: UploadSteps = {
    async start() {
      calls.push('start')
      return { ...ticket, material: material('m1') }
    },
    async parts(_, numbers) {
      calls.push(`parts ${numbers.join(',')}`)
      return numbers.map((n) => ({ part_number: n, url: `https://oss/part${n}` }))
    },
    async complete(_, parts) {
      calls.push('complete')
      completed = parts
      return material('m1')
    },
    async abort() {
      calls.push('abort')
    },
    async put(url, body, headers, onProgress) {
      const number = Number(url.replace(/\D/g, '')) || 0
      if ((failures[number] ?? 0) > 0) {
        failures[number] = (failures[number] ?? 0) - 1
        throw new Error('network')
      }
      onProgress(body.size)
      puts.push({ url, size: body.size, headers })
      return `"etag-${number}"`
    },
  }
  return { steps, calls, puts, completed: () => completed }
}

describe('uploadMaterial', () => {
  it('puts a small file once with the signed headers', async () => {
    const fake = fakeSteps({
      method: 'single',
      upload_url: 'https://oss/single',
      headers: { 'Content-Type': 'application/pdf' },
      part_size: null,
      part_count: null,
      expires_in: 900,
    })
    const progress: number[] = []
    await uploadMaterial(new File(['%PDF'], 'a.pdf'), fake.steps, (n) => progress.push(n))
    expect(fake.calls).toEqual(['start', 'complete'])
    expect(fake.puts).toEqual([
      { url: 'https://oss/single', size: 4, headers: { 'Content-Type': 'application/pdf' } },
    ])
    expect(fake.completed()).toBeNull()
    expect(progress.at(-1)).toBe(4)
  })

  it('uploads parts in parallel, retries a failed part and completes in order', async () => {
    const fake = fakeSteps(
      {
        method: 'multipart',
        upload_url: null,
        headers: {},
        part_size: 4,
        part_count: 3,
        expires_in: 900,
      },
      { 2: 1 },
    )
    const progress: number[] = []
    await uploadMaterial(new File(['0123456789'], 'v.mp4'), fake.steps, (n) => progress.push(n))
    expect(fake.calls).toEqual(['start', 'parts 1,2,3', 'complete'])
    expect(fake.puts.map((p) => p.size).sort()).toEqual([2, 4, 4])
    expect(fake.completed()).toEqual([
      { part_number: 1, etag: '"etag-1"' },
      { part_number: 2, etag: '"etag-2"' },
      { part_number: 3, etag: '"etag-3"' },
    ])
    expect(Math.max(...progress)).toBe(10)
  })

  it('cancels the registration when a part keeps failing', async () => {
    const fake = fakeSteps(
      {
        method: 'multipart',
        upload_url: null,
        headers: {},
        part_size: 4,
        part_count: 2,
        expires_in: 900,
      },
      { 1: 5 },
    )
    await expect(uploadMaterial(new File(['01234567'], 'v.mp4'), fake.steps, () => {})).rejects.toThrow(
      'network',
    )
    expect(fake.calls).toContain('abort')
    expect(fake.calls).not.toContain('complete')
  })
})
