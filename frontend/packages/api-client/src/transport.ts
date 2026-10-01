/**
 * 接口传输加密（设计文档 §25.15）：包在 fetch 外面的一层。握手之后，发往 /api/ 和 /platform/ 的请求把
 * 查询参数、请求头（Authorization、Content-Type 等）和请求体加密，响应解密后交回调用方，页面代码不用改。
 *
 * - 握手：浏览器生成一次性的 ECDH P-256 密钥对，服务器返回它的一次性公钥和签名；签名用写死（或从
 *   /api/v1/transport/key 取得）的服务器公钥验证，双方的共享密钥经 HKDF-SHA256 派生请求、响应各一把
 *   AES-256-GCM 密钥（不可导出，只在内存里）。
 * - 会话过期（428）或时间不对时重新握手，重试一次。
 * - 服务器关闭了传输加密（off）或浏览器不支持 WebCrypto（不是 HTTPS）时照常发明文请求。
 *
 * 后端的实现在 backend/app/modules/transport，两边的格式必须一致（两边的测试用同一组测试向量）。
 */

const PROTOCOL = 'edp1'
export const SEALED_TYPE = 'application/x-edp-sealed'
export const SESSION_HEADER = 'X-EDP-Transport'
export const SEALED_HEADER = 'X-EDP-Sealed'
const NULL_BODY_STATUS = new Set([101, 204, 205, 304])
// 放进信封（加密）的请求头；其余的照原样发送。
const SEALED_REQUEST_HEADERS = new Set([
  'authorization',
  'content-type',
  'accept',
  'accept-language',
  'idempotency-key',
])
const encoder = new TextEncoder()
const decoder = new TextDecoder()

export interface TransportOptions {
  /** 后端地址；留空表示与页面同源。 */
  baseUrl?: string
  /** 服务器公钥（SubjectPublicKeyInfo DER 的 base64）；为空时从 /api/v1/transport/key 获取。 */
  publicKey?: string | null
  fetch?: (request: Request) => Promise<Response>
  /** 当前时间（毫秒），测试时替换。 */
  now?: () => number
}

export interface SessionKeys {
  request: CryptoKey
  response: CryptoKey
}

interface Session extends SessionKeys {
  id: string
  expiresAt: number
  /** 服务器时间减本机时间（秒）：本机时间不准时按服务器的时间发请求。 */
  offset: number
}

interface Plain {
  url: URL
  method: string
  headers: Headers
  body: Uint8Array | null
  init: RequestInit
}

class TransportOff extends Error {}

/**
 * 传输加密的服务器公钥：部署时的运行时配置（/config.js 里的 transportKey，容器按环境变量
 * EDP_TRANSPORT_PUBLIC_KEY 生成）优先，其次是构建时的 VITE_TRANSPORT_PUBLIC_KEY；都没有时返回
 * null，由后端提供（开发环境）。
 */
export function transportKey(buildTime?: string): string | null {
  const runtime = (globalThis as { EDP_CONFIG?: { transportKey?: string } }).EDP_CONFIG
  return runtime?.transportKey || buildTime || null
}

export function b64url(bytes: Uint8Array): string {
  let text = ''
  for (const byte of bytes) text += String.fromCharCode(byte)
  return btoa(text).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

export function unb64url(text: string): Uint8Array {
  const normal = text.replace(/-/g, '+').replace(/_/g, '/')
  return fromBase64(normal + '='.repeat((4 - (normal.length % 4)) % 4))
}

function fromBase64(text: string): Uint8Array {
  const raw = atob(text)
  const out = new Uint8Array(raw.length)
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i)
  return out
}

function concat(...parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0))
  let offset = 0
  for (const part of parts) {
    out.set(part, offset)
    offset += part.length
  }
  return out
}

// WebCrypto 的类型要求 ArrayBuffer 支撑的视图。
function buffer(bytes: Uint8Array): Uint8Array<ArrayBuffer> {
  return new Uint8Array(bytes)
}

export function requestAad(
  method: string,
  path: string,
  session: string,
  ts: number,
  nonce: string,
  part: 'meta' | 'body',
): string {
  return `${PROTOCOL}|req|${method.toUpperCase()}|${path}|${session}|${ts}|${nonce}|${part}`
}

export function responseAad(method: string, path: string, session: string, nonce: string): string {
  return `${PROTOCOL}|res|${method.toUpperCase()}|${path}|${session}|${nonce}`
}

/** 握手签名的内容：双方的一次性公钥（定长）、会话号和过期时间。 */
export function transcript(
  clientKey: Uint8Array,
  serverKey: Uint8Array,
  session: string,
  expiresAt: number,
): Uint8Array {
  return concat(
    encoder.encode(`${PROTOCOL}|handshake|`),
    clientKey,
    serverKey,
    encoder.encode(`${session}|${expiresAt}`),
  )
}

export async function seal(
  key: CryptoKey,
  plaintext: Uint8Array,
  aad: string,
  iv: Uint8Array = crypto.getRandomValues(new Uint8Array(12)),
): Promise<Uint8Array> {
  const sealed = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv: buffer(iv), additionalData: encoder.encode(aad) },
    key,
    buffer(plaintext),
  )
  return concat(iv, new Uint8Array(sealed))
}

export async function openSealed(key: CryptoKey, blob: Uint8Array, aad: string): Promise<Uint8Array> {
  const plain = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv: buffer(blob.subarray(0, 12)), additionalData: encoder.encode(aad) },
    key,
    buffer(blob.subarray(12)),
  )
  return new Uint8Array(plain)
}

export async function deriveKeys(
  shared: ArrayBuffer,
  session: string,
  extractable = false,
): Promise<SessionKeys> {
  const base = await crypto.subtle.importKey('raw', shared, 'HKDF', false, ['deriveBits'])
  const okm = new Uint8Array(
    await crypto.subtle.deriveBits(
      {
        name: 'HKDF',
        hash: 'SHA-256',
        salt: encoder.encode(session),
        info: encoder.encode(`${PROTOCOL} transport keys`),
      },
      base,
      512,
    ),
  )
  const aes = (bytes: Uint8Array, usage: KeyUsage) =>
    crypto.subtle.importKey('raw', buffer(bytes), 'AES-GCM', extractable, [usage])
  return {
    request: await aes(okm.subarray(0, 32), 'encrypt'),
    response: await aes(okm.subarray(32), 'decrypt'),
  }
}

/** 需要加密的请求：后端地址下的 /api/ 和 /platform/，握手本身除外。 */
export function sealable(url: URL, basePath = ''): boolean {
  const path = basePath && url.pathname.startsWith(basePath) ? url.pathname.slice(basePath.length) : url.pathname
  return (path.startsWith('/api/') || path.startsWith('/platform/')) && !path.startsWith('/api/v1/transport/')
}

function isSealedHeader(name: string): boolean {
  return SEALED_REQUEST_HEADERS.has(name) || (name.startsWith('x-') && !name.startsWith('x-edp-'))
}

/** 返回一个与 fetch(request) 用法相同的函数：需要加密的请求自动加密，响应自动解密。 */
export function createTransport(options: TransportOptions = {}): (request: Request) => Promise<Response> {
  const send = options.fetch ?? ((request: Request) => globalThis.fetch(request))
  const now = options.now ?? (() => Date.now())
  const baseUrl = (options.baseUrl ?? '').replace(/\/$/, '')
  const basePath = baseUrl ? new URL(baseUrl, 'http://localhost').pathname.replace(/\/$/, '') : ''
  let ready: Session | null = null
  let pending: Promise<Session> | null = null
  let off = false

  async function serverKey(): Promise<CryptoKey> {
    let spki = options.publicKey ?? ''
    if (!spki) {
      const response = await send(new Request(`${baseUrl}/api/v1/transport/key`))
      if (!response.ok) throw new Error(`获取服务器公钥失败（${response.status}）`)
      const body = (await response.json()) as { public_key: string; mode: string }
      if (body.mode === 'off') throw new TransportOff()
      spki = body.public_key
    }
    return crypto.subtle.importKey(
      'spki',
      buffer(fromBase64(spki)),
      { name: 'ECDSA', namedCurve: 'P-256' },
      false,
      ['verify'],
    )
  }

  async function handshake(): Promise<Session> {
    const verifyKey = await serverKey()
    const pair = await crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, false, [
      'deriveBits',
    ])
    const clientKey = new Uint8Array(await crypto.subtle.exportKey('raw', pair.publicKey))
    const response = await send(
      new Request(`${baseUrl}/api/v1/transport/handshake`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ client_key: b64url(clientKey) }),
      }),
    )
    if (response.status === 409) throw new TransportOff()
    if (!response.ok) throw new Error(`加密握手失败（${response.status}）`)
    const body = (await response.json()) as {
      session: string
      server_key: string
      expires_at: number
      server_time: number
      signature: string
    }
    const peerKey = unb64url(body.server_key)
    const valid = await crypto.subtle.verify(
      { name: 'ECDSA', hash: 'SHA-256' },
      verifyKey,
      buffer(unb64url(body.signature)),
      buffer(transcript(clientKey, peerKey, body.session, body.expires_at)),
    )
    if (!valid) throw new Error('服务器签名不对，连接可能被篡改')
    const peer = await crypto.subtle.importKey(
      'raw',
      buffer(peerKey),
      { name: 'ECDH', namedCurve: 'P-256' },
      false,
      [],
    )
    const shared = await crypto.subtle.deriveBits({ name: 'ECDH', public: peer }, pair.privateKey, 256)
    const keys = await deriveKeys(shared, body.session)
    const offset = body.server_time - Math.floor(now() / 1000)
    return { id: body.session, ...keys, expiresAt: body.expires_at, offset }
  }

  function session(): Promise<Session> {
    if (ready && ready.expiresAt - 60 > now() / 1000 + ready.offset) return Promise.resolve(ready)
    pending ??= handshake()
      .then((s) => {
        ready = s
        return s
      })
      .finally(() => {
        pending = null
      })
    return pending
  }

  function forget(s: Session): void {
    if (ready === s) ready = null
  }

  async function sealRequest(s: Session, plain: Plain): Promise<{ request: Request; nonce: string }> {
    const { url, method } = plain
    const ts = Math.floor(now() / 1000) + s.offset
    const nonce = b64url(crypto.getRandomValues(new Uint8Array(16)))
    const inner: Record<string, string> = {}
    const outer = new Headers()
    plain.headers.forEach((value, name) => {
      if (isSealedHeader(name)) inner[name] = value
      else outer.set(name, value)
    })
    if (!plain.body) delete inner['content-type']
    const meta = encoder.encode(JSON.stringify({ q: url.search.slice(1), h: inner }))
    const sealedMeta = await seal(s.request, meta, requestAad(method, url.pathname, s.id, ts, nonce, 'meta'))
    outer.set(SESSION_HEADER, s.id)
    outer.set(SEALED_HEADER, `${ts}.${nonce}.${b64url(sealedMeta)}`)
    let body: Uint8Array<ArrayBuffer> | null = null
    if (plain.body) {
      body = buffer(
        await seal(s.request, plain.body, requestAad(method, url.pathname, s.id, ts, nonce, 'body')),
      )
      outer.set('Content-Type', SEALED_TYPE)
    }
    const request = new Request(`${url.origin}${url.pathname}`, {
      ...plain.init,
      method,
      headers: outer,
      body,
    })
    return { request, nonce }
  }

  async function openResponse(s: Session, plain: Plain, nonce: string, response: Response): Promise<Response> {
    if (!(response.headers.get('content-type') ?? '').startsWith(SEALED_TYPE)) return response
    const blob = new Uint8Array(await response.arrayBuffer())
    const opened = await openSealed(
      s.response,
      blob,
      responseAad(plain.method, plain.url.pathname, s.id, nonce),
    )
    const cut = opened.indexOf(10)
    const meta = JSON.parse(decoder.decode(opened.subarray(0, cut))) as { h: Record<string, string> }
    const headers = new Headers(response.headers)
    headers.delete('content-type')
    headers.delete('content-length')
    for (const [name, value] of Object.entries(meta.h)) headers.set(name, value)
    const body = NULL_BODY_STATUS.has(response.status) ? null : buffer(opened.subarray(cut + 1))
    return new Response(body, { status: response.status, statusText: response.statusText, headers })
  }

  /** 会话过期或时间不对：重新握手后可以重试。 */
  async function retryable(response: Response): Promise<boolean> {
    if (response.status === 428) return true
    if (response.status !== 400) return false
    try {
      const body = (await response.clone().json()) as { error?: { code?: string } }
      return body.error?.code === 'transport_clock'
    } catch {
      return false
    }
  }

  return async (request: Request): Promise<Response> => {
    const url = new URL(request.url)
    if (off || !sealable(url, basePath) || !globalThis.crypto?.subtle) return send(request)
    const method = request.method.toUpperCase()
    const bytes = method === 'GET' || method === 'HEAD' ? null : new Uint8Array(await request.arrayBuffer())
    const plain: Plain = {
      url,
      method,
      headers: request.headers,
      body: bytes && bytes.length ? bytes : null,
      init: {
        credentials: request.credentials,
        signal: request.signal,
        cache: request.cache,
        redirect: request.redirect,
      },
    }
    let s: Session
    try {
      s = await session()
    } catch (error) {
      if (!(error instanceof TransportOff)) throw error
      off = true
      return send(
        new Request(request.url, { ...plain.init, method, headers: request.headers, body: plain.body ? buffer(plain.body) : null }),
      )
    }
    let sealed = await sealRequest(s, plain)
    let response = await send(sealed.request)
    if (await retryable(response)) {
      forget(s)
      s = await session()
      sealed = await sealRequest(s, plain)
      response = await send(sealed.request)
    }
    return openResponse(s, plain, sealed.nonce, response)
  }
}
