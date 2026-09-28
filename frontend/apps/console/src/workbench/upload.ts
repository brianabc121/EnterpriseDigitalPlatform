import { errorMessage } from '@edp/api-client'

import { api } from '../api'
import type { Attachment } from './messages'

/** 与后端 files/service.py 一致的上传限制。 */
export const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp']
export const MAX_IMAGE_BYTES = 10 * 1024 * 1024
export const MAX_FILE_BYTES = 20 * 1024 * 1024

export interface Uploaded {
  kind: 'image' | 'file'
  attachment: Attachment & { name: string; size: number; mime: string }
}

/** 读取图片尺寸（图片消息需要）；读取失败时为 0。 */
export function imageSize(file: File): Promise<{ width: number; height: number }> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file)
    const img = new Image()
    const done = (width: number, height: number) => {
      URL.revokeObjectURL(url)
      resolve({ width, height })
    }
    img.onload = () => done(img.naturalWidth, img.naturalHeight)
    img.onerror = () => done(0, 0)
    img.src = url
  })
}

/** 申请上传凭证，把文件直接 PUT 到对象存储；返回发送消息时引用的附件。 */
export async function uploadFile(file: File): Promise<Uploaded> {
  const mime = file.type || 'application/octet-stream'
  const { data, error } = await api.POST('/api/v1/uploads', {
    body: { filename: file.name, content_type: mime, size: file.size },
  })
  if (!data) throw new Error(errorMessage(error, '上传失败'))
  const response = await fetch(data.upload_url, {
    method: 'PUT',
    headers: { 'content-type': mime },
    body: file,
  })
  if (!response.ok) throw new Error('上传失败，请稍后再试')
  const kind = data.kind === 'image' ? 'image' : 'file'
  const { width, height } = kind === 'image' ? await imageSize(file) : { width: 0, height: 0 }
  return {
    kind,
    attachment: {
      url: data.file_url,
      name: file.name,
      size: file.size,
      mime,
      width: width || null,
      height: height || null,
    },
  }
}
