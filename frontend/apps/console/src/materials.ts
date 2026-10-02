import type { Schemas } from '@edp/api-client'

/**
 * 企业资料（设计文档 §36）：类型、文件大小和已用空间、能否在线查看和加入知识库、上传前的检查，以及浏览器
 * 直传 OSS 的上传过程（不超过 64 MB 一次 PUT；更大的分片上传，同时传 3 片，失败的分片重试）。
 */
export type Material = Schemas['MaterialOut']
export type MaterialPage = Schemas['MaterialPage']
export type MaterialKind = Material['kind']
export type MaterialFolder = Schemas['MaterialFolderOut']
export type MaterialConfig = Schemas['MaterialConfig']
export type MaterialShare = Schemas['MaterialShareOut']
export type MaterialUpload = Schemas['MaterialUploadOut']
export type ScanStatus = NonNullable<Material['scan_status']>

export const KINDS: MaterialKind[] = ['video', 'document', 'image', 'text', 'other']

export const KIND_LABEL: Record<MaterialKind, string> = {
  video: '视频',
  document: '文档',
  image: '图片',
  text: '文字资料',
  other: '其他文件',
}

// 红色留给"已拦截"，类型标签不用红色。
export const KIND_TAG: Record<MaterialKind, 'primary' | 'success' | 'warning' | 'info'> = {
  video: 'primary',
  document: 'info',
  image: 'success',
  text: 'warning',
  other: 'info',
}

export const SCAN_LABEL: Record<ScanStatus, string> = {
  pending: '等待病毒扫描',
  clean: '病毒扫描通过',
  infected: '含有病毒，已拦截',
  skipped: '超过扫描上限，没有扫描',
  missing: '扫描时文件不存在',
}

export const SHARE_DAYS = [1, 3, 7, 30]

/** 文件大小：1.5 GB、12.3 MB、48 KB、512 B。 */
export function sizeText(bytes: number | null | undefined): string {
  const value = bytes ?? 0
  if (value >= 1024 ** 3) return `${trim(value / 1024 ** 3)} GB`
  if (value >= 1024 ** 2) return `${trim(value / 1024 ** 2)} MB`
  if (value >= 1024) return `${Math.ceil(value / 1024)} KB`
  return `${value} B`
}

function trim(value: number): string {
  return value >= 100 ? value.toFixed(0) : value.toFixed(1).replace(/\.0$/, '')
}

/** 已用空间："已用 1.2 GB，共 50 GB"；不限时只显示已用。 */
export function usageText(used: number, limit: number | null | undefined): string {
  if (limit === null || limit === undefined) return `已用 ${sizeText(used)}`
  return `已用 ${sizeText(used)}，共 ${sizeText(limit)}`
}

/** 已用的百分比（0–100）；不限时为空。 */
export function usagePercent(used: number, limit: number | null | undefined): number | null {
  if (limit === null || limit === undefined) return null
  if (limit <= 0) return 100
  return Math.min(100, Math.round((used / limit) * 1000) / 10)
}

/** 日期：2026/10/2。 */
export function dateText(value: string): string {
  return new Date(value).toLocaleDateString('zh-CN')
}

/** 卡片上的正文开头：去掉 Markdown 的标记（# 标题、**加粗**、列表符号、表格的竖线和分隔线）。 */
export function plainExcerpt(text: string | null | undefined, limit = 120): string {
  return (text ?? '')
    .split('\n')
    .map((line) =>
      line
        .replace(/^\s*#{1,6}\s+/, '')
        .replace(/^\s*>\s?/, '')
        .replace(/^\s*([-*•]|\d+[.、)])\s+/, '')
        .replace(/\*\*(.+?)\*\*/g, '$1')
        .replace(/^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/, '')
        .replace(/^\s*\||\|\s*$/g, '')
        .replace(/\s*\|\s*/g, '  ')
        .trim(),
    )
    .filter(Boolean)
    .join('\n')
    .slice(0, limit)
}

export type PreviewKind = 'video' | 'image' | 'pdf' | 'text' | 'markdown' | 'none'

/** 在线查看的方式：视频播放、图片、PDF、纯文本、文字资料（Markdown 排版）；Office 文档和压缩包只能下载。 */
export function previewKind(material: Pick<Material, 'kind' | 'ext'>): PreviewKind {
  if (material.kind === 'text') return 'markdown'
  if (material.kind === 'video') return 'video'
  if (material.kind === 'image') return 'image'
  if (material.ext === '.pdf') return 'pdf'
  if (['.txt', '.md', '.markdown', '.csv'].includes(material.ext)) return 'text'
  return 'none'
}

export const TEXT_PREVIEW_BYTES = 200 * 1024

/**
 * 在线查看纯文本文档（TXT、Markdown、CSV）：从 OSS 的查看地址取开头 200 KB（Range），超过的部分请下载
 * 查看。partial 表示只显示了开头。
 */
export async function fetchTextPreview(
  url: string,
  size: number,
  fetcher: typeof fetch = fetch,
): Promise<{ text: string; partial: boolean }> {
  const partial = size > TEXT_PREVIEW_BYTES
  const response = await fetcher(
    url,
    partial ? { headers: { Range: `bytes=0-${TEXT_PREVIEW_BYTES - 1}` } } : undefined,
  )
  if (!response.ok) throw new Error(`读取文件失败（${response.status}）`)
  const bytes = new Uint8Array(await response.arrayBuffer()).slice(0, TEXT_PREVIEW_BYTES)
  let text = new TextDecoder('utf-8').decode(bytes)
  // 截断处可能把一个汉字切成两半。
  if (partial) text = text.replace(/\uFFFD+$/, '')
  return { text, partial }
}

const KNOWLEDGE_EXT = ['.pdf', '.docx', '.md', '.markdown', '.txt']

/** 能加入知识库的：文字资料，以及 PDF、Word（.docx）、Markdown、TXT 文档。 */
export function canJoinKnowledge(material: Pick<Material, 'kind' | 'ext' | 'status'>): boolean {
  if (material.status !== 'ready') return false
  return material.kind === 'text' || (material.kind === 'document' && KNOWLEDGE_EXT.includes(material.ext))
}

export function extensionOf(filename: string): string {
  const dot = filename.lastIndexOf('.')
  return dot > 0 ? filename.slice(dot).toLowerCase() : ''
}

/** 按扩展名判断类型；不支持的返回 null。 */
export function kindOf(
  filename: string,
  extensions: Partial<Record<MaterialKind, string[]>>,
): MaterialKind | null {
  const ext = extensionOf(filename)
  for (const kind of KINDS) {
    if (extensions[kind]?.includes(ext)) return kind
  }
  return null
}

/** 上传前的检查：格式和单个文件上限。通过时返回 null，否则返回提示。 */
export function checkFile(
  file: { name: string; size: number },
  config: Pick<MaterialConfig, 'extensions' | 'video_max_bytes' | 'file_max_bytes'>,
): string | null {
  const kind = kindOf(file.name, config.extensions)
  if (!kind || kind === 'text') {
    return `不支持「${file.name}」这种文件：视频 mp4、mov、webm、m4v；文档 PDF、Word、Excel、PPT、TXT、Markdown；图片 jpg、png、gif、webp；其他 zip、rar、7z`
  }
  if (file.size <= 0) return `「${file.name}」是空文件`
  const limit = kind === 'video' ? config.video_max_bytes : config.file_max_bytes
  if (file.size > limit) return `「${file.name}」超过${KIND_LABEL[kind]}的上限 ${sizeText(limit)}`
  return null
}

/** 分片号分成几批申请上传地址（每批最多 size 个）。 */
export function batches(count: number, size: number): number[][] {
  const result: number[][] = []
  for (let start = 1; start <= count; start += size) {
    result.push(Array.from({ length: Math.min(size, count - start + 1) }, (_, i) => start + i))
  }
  return result
}

// ---- 文件夹 ----

export interface FolderNode {
  id: string
  label: string
  /** 这个文件夹和下级文件夹里的资料数。 */
  count: number
  depth: number
  children: FolderNode[]
}

/** 文件夹排成树（按顺序），数量包含下级文件夹。 */
export function folderTree(items: MaterialFolder[]): FolderNode[] {
  const byParent = new Map<string | null, MaterialFolder[]>()
  for (const item of items) {
    const list = byParent.get(item.parent_id ?? null) ?? []
    list.push(item)
    byParent.set(item.parent_id ?? null, list)
  }
  const build = (parent: string | null, depth: number): FolderNode[] =>
    (byParent.get(parent) ?? [])
      .slice()
      .sort((a, b) => a.sort - b.sort || a.name.localeCompare(b.name, 'zh-CN'))
      .map((item) => {
        const children = depth < 10 ? build(item.id, depth + 1) : []
        return {
          id: item.id,
          label: item.name,
          depth,
          children,
          count: item.materials + children.reduce((sum, c) => sum + c.count, 0),
        }
      })
  return build(null, 1)
}

/** 级联选择器用的选项（值是文件夹 id）。 */
export interface FolderOption {
  value: string
  label: string
  children?: FolderOption[]
}

export function folderOptions(nodes: FolderNode[], skip = ''): FolderOption[] {
  return nodes
    .filter((node) => node.id !== skip)
    .map((node) => {
      const children = folderOptions(node.children, skip)
      return { value: node.id, label: node.label, ...(children.length ? { children } : {}) }
    })
}

/** 文件夹的路径（从第一级到它自己的 id），给级联选择器用。 */
export function folderPath(items: MaterialFolder[], id: string | null | undefined): string[] {
  const byId = new Map(items.map((item) => [item.id, item]))
  const path: string[] = []
  let current = id ? byId.get(id) : undefined
  while (current && !path.includes(current.id)) {
    path.unshift(current.id)
    current = current.parent_id ? byId.get(current.parent_id) : undefined
  }
  return path
}

// ---- 上传 ----

export interface UploadSteps {
  /** 登记上传，返回上传地址或分片信息。 */
  start(file: File): Promise<MaterialUpload>
  /** 这几个分片的上传地址。 */
  parts(materialId: string, numbers: number[]): Promise<{ part_number: number; url: string }[]>
  complete(materialId: string, parts: { part_number: number; etag: string }[] | null): Promise<Material>
  abort(materialId: string): Promise<void>
  /** 把内容 PUT 到签名地址，返回 ETag。onProgress 收到这次已经传了多少字节。 */
  put(
    url: string,
    body: Blob,
    headers: Record<string, string>,
    onProgress: (loaded: number) => void,
    signal?: AbortSignal,
  ): Promise<string>
}

export const PARALLEL_PARTS = 3
export const PART_RETRIES = 3
const URL_BATCH = 20

export class UploadAborted extends Error {
  constructor() {
    super('已取消上传')
  }
}

/**
 * 上传一个文件：登记 → PUT 到 OSS（或分片上传）→ 完成。onProgress 收到已传的字节数。失败或取消时
 * 取消平台上的登记（删除已经传上去的部分），再抛出错误。
 */
export async function uploadMaterial(
  file: File,
  steps: UploadSteps,
  onProgress: (loaded: number) => void,
  signal?: AbortSignal,
): Promise<Material> {
  const ticket = await steps.start(file)
  const id = ticket.material.id
  try {
    if (signal?.aborted) throw new UploadAborted()
    if (ticket.method === 'single') {
      await steps.put(ticket.upload_url ?? '', file, ticket.headers, onProgress, signal)
      return await steps.complete(id, null)
    }
    const partSize = ticket.part_size ?? file.size
    const count = ticket.part_count ?? 1
    const done = new Map<number, string>()
    const inFlight = new Map<number, number>()
    const report = (): void => {
      let loaded = 0
      for (const number of done.keys()) loaded += Math.min(partSize, file.size - (number - 1) * partSize)
      for (const value of inFlight.values()) loaded += value
      onProgress(loaded)
    }
    for (const batch of batches(count, URL_BATCH)) {
      const urls = await steps.parts(id, batch)
      const queue = [...urls]
      const worker = async (): Promise<void> => {
        for (let next = queue.shift(); next; next = queue.shift()) {
          const part = next
          const start = (part.part_number - 1) * partSize
          const body = file.slice(start, Math.min(start + partSize, file.size))
          let attempt = 0
          for (;;) {
            if (signal?.aborted) throw new UploadAborted()
            try {
              const etag = await steps.put(
                part.url,
                body,
                {},
                (loaded) => {
                  inFlight.set(part.part_number, loaded)
                  report()
                },
                signal,
              )
              inFlight.delete(part.part_number)
              done.set(part.part_number, etag)
              report()
              break
            } catch (error) {
              inFlight.delete(part.part_number)
              attempt += 1
              if (signal?.aborted || attempt >= PART_RETRIES) throw error
            }
          }
        }
      }
      await Promise.all(Array.from({ length: Math.min(PARALLEL_PARTS, queue.length) }, worker))
    }
    const parts = [...done.entries()]
      .sort(([a], [b]) => a - b)
      .map(([part_number, etag]) => ({ part_number, etag }))
    return await steps.complete(id, parts)
  } catch (error) {
    await steps.abort(id).catch(() => undefined)
    if (signal?.aborted) throw new UploadAborted()
    throw error
  }
}

/** 浏览器里 PUT 到 OSS 的签名地址（XMLHttpRequest 才有上传进度）。 */
export function xhrPut(
  url: string,
  body: Blob,
  headers: Record<string, string>,
  onProgress: (loaded: number) => void,
  signal?: AbortSignal,
): Promise<string> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('PUT', url)
    for (const [name, value] of Object.entries(headers)) xhr.setRequestHeader(name, value)
    xhr.upload.onprogress = (event) => onProgress(event.loaded)
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve(xhr.getResponseHeader('ETag') ?? '')
      else reject(new Error(`上传到存储失败（${xhr.status}）`))
    }
    xhr.onerror = () => reject(new Error('上传到存储失败：网络错误'))
    xhr.onabort = () => reject(new UploadAborted())
    signal?.addEventListener('abort', () => xhr.abort())
    xhr.send(body)
  })
}
