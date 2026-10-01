import { describe, expect, it } from 'vitest'

import {
  b64url,
  createTransport,
  deriveKeys,
  openSealed,
  requestAad,
  responseAad,
  seal,
  SEALED_TYPE,
  transcript,
  unb64url,
} from './transport'

const encoder = new TextEncoder()
const decoder = new TextDecoder()

function hex(bytes: ArrayBuffer | Uint8Array): string {
  return [...new Uint8Array(bytes)].map((b) => b.toString(16).padStart(2, '0')).join('')
}

async function aesKey(bytes: Uint8Array, usages: KeyUsage[]): Promise<CryptoKey> {
  return crypto.subtle.importKey('raw', new Uint8Array(bytes), 'AES-GCM', false, usages)
}

describe('传输加密：与后端相同的测试向量（backend/tests/test_transport.py）', () => {
  it('AES-GCM 密文和附加认证数据', async () => {
    const aad = requestAad('GET', '/api/v1/me', 'session-123', 1700000000, 'nonce-abcdefghijklmn', 'meta')
    expect(aad).toBe('edp1|req|GET|/api/v1/me|session-123|1700000000|nonce-abcdefghijklmn|meta')
    const key = await aesKey(Uint8Array.from({ length: 32 }, (_, i) => i), ['encrypt', 'decrypt'])
    const plain = encoder.encode('{"q":"a=1","h":{"authorization":"Bearer t"}}')
    const sealed = await seal(key, plain, aad, Uint8Array.from({ length: 12 }, (_, i) => i))
    expect(b64url(sealed)).toBe(
      'AAECAwQFBgcICQoLPCCnOf_Hoya8Y7up2ctCFqG38kCYFC0VQgaR7HIHIogjUsud3aRguACGApBtMz3RoEUA-kwsEoxWH59C',
    )
    expect(decoder.decode(await openSealed(key, sealed, aad))).toBe(decoder.decode(plain))
    await expect(openSealed(key, sealed, `${aad}x`)).rejects.toThrow()
  })

  it('HKDF 派生的请求和响应密钥、握手签名的内容', async () => {
    const keys = await deriveKeys(new Uint8Array(32).fill(7).buffer, 'session-123', true)
    expect(hex(await crypto.subtle.exportKey('raw', keys.request))).toBe(
      'b60ae179503083f959bce425948f4d51dd1a66cf2d122b165c9cfbcb44103071',
    )
    expect(hex(await crypto.subtle.exportKey('raw', keys.response))).toBe(
      '351dda0c995386a2536da38d7a687696c9dd6bc90a04d7cb18ba793f7d788150',
    )
    const point = new Uint8Array(65)
    point[0] = 4
    const digest = await crypto.subtle.digest('SHA-256', new Uint8Array(transcript(point, point, 'session-123', 1700000000)))
    expect(hex(digest)).toBe('9cc22b92948df5e5c477265b4435df2ad9fefa3548c7f9c9225902858a456492')
  })
})

/** 按后端的规则实现的模拟服务器（只用于测试客户端的一侧）。 */
async function fakeServer() {
  const signing = await crypto.subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, false, [
    'sign',
    'verify',
  ])
  const spki = new Uint8Array(await crypto.subtle.exportKey('spki', signing.publicKey))
  const publicKey = btoa(String.fromCharCode(...spki))
  const sessions = new Map<string, { request: CryptoKey; response: CryptoKey }>()
  const seen: Request[] = []
  const state = { handshakes: 0, expireNext: false }

  async function handshake(request: Request): Promise<Response> {
    state.handshakes += 1
    const { client_key } = (await request.json()) as { client_key: string }
    const clientKey = unb64url(client_key)
    const pair = await crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveBits'])
    const serverKey = new Uint8Array(await crypto.subtle.exportKey('raw', pair.publicKey))
    const peer = await crypto.subtle.importKey('raw', new Uint8Array(clientKey), { name: 'ECDH', namedCurve: 'P-256' }, false, [])
    const shared = await crypto.subtle.deriveBits({ name: 'ECDH', public: peer }, pair.privateKey, 256)
    const session = `session-${state.handshakes}-abcdefghijk`
    const derived = await deriveKeys(shared, session, true)
    sessions.set(session, {
      request: await aesKey(new Uint8Array(await crypto.subtle.exportKey('raw', derived.request)), ['decrypt']),
      response: await aesKey(new Uint8Array(await crypto.subtle.exportKey('raw', derived.response)), ['encrypt']),
    })
    const expiresAt = Math.floor(Date.now() / 1000) + 3600
    const signature = await crypto.subtle.sign(
      { name: 'ECDSA', hash: 'SHA-256' },
      signing.privateKey,
      new Uint8Array(transcript(clientKey, serverKey, session, expiresAt)),
    )
    return Response.json({
      session,
      server_key: b64url(serverKey),
      expires_at: expiresAt,
      server_time: Math.floor(Date.now() / 1000),
      signature: b64url(new Uint8Array(signature)),
    })
  }

  async function sealed(request: Request): Promise<Response> {
    const url = new URL(request.url)
    const id = request.headers.get('X-EDP-Transport') ?? ''
    const keys = sessions.get(id)
    if (!keys || state.expireNext) {
      state.expireNext = false
      sessions.delete(id)
      return Response.json({ error: { code: 'transport_session', message: '过期' } }, { status: 428 })
    }
    const [ts, nonce, blob] = (request.headers.get('X-EDP-Sealed') ?? '').split('.')
    const meta = JSON.parse(
      decoder.decode(
        await openSealed(keys.request, unb64url(blob!), requestAad(request.method, url.pathname, id, Number(ts), nonce!, 'meta')),
      ),
    ) as { q: string; h: Record<string, string> }
    const raw = new Uint8Array(await request.arrayBuffer())
    const body = raw.length
      ? decoder.decode(await openSealed(keys.request, raw, requestAad(request.method, url.pathname, id, Number(ts), nonce!, 'body')))
      : null
    const echo = encoder.encode(JSON.stringify({ path: url.pathname, query: meta.q, headers: meta.h, body }))
    const head = encoder.encode(
      JSON.stringify({ h: { 'content-type': 'application/json', 'content-disposition': 'attachment; filename=a.csv' } }) + '\n',
    )
    const plain = new Uint8Array(head.length + echo.length)
    plain.set(head)
    plain.set(echo, head.length)
    const out = await seal(keys.response, plain, responseAad(request.method, url.pathname, id, nonce!))
    return new Response(new Uint8Array(out), { status: 201, headers: { 'content-type': SEALED_TYPE } })
  }

  const fetch = async (request: Request): Promise<Response> => {
    seen.push(request.clone())
    const path = new URL(request.url).pathname
    if (path === '/api/v1/transport/key') return Response.json({ public_key: publicKey, mode: 'optional' })
    if (path === '/api/v1/transport/handshake') return handshake(request)
    if (request.headers.has('X-EDP-Transport')) return sealed(request)
    return Response.json({ plain: true })
  }
  return { fetch, seen, state, publicKey }
}

describe('传输加密：客户端', () => {
  it('握手后请求头、查询参数和请求体都在密文里，响应解密后交回', async () => {
    const server = await fakeServer()
    const send = createTransport({ baseUrl: 'http://api.test', fetch: server.fetch })
    const response = await send(
      new Request('http://api.test/api/v1/customers?q=%E6%9D%8E&limit=20', {
        method: 'POST',
        headers: { Authorization: 'Bearer secret-token', 'Content-Type': 'application/json' },
        body: JSON.stringify({ display_name: '李女士' }),
      }),
    )
    expect(response.status).toBe(201)
    expect(response.headers.get('content-disposition')).toBe('attachment; filename=a.csv')
    expect(await response.json()).toEqual({
      path: '/api/v1/customers',
      query: 'q=%E6%9D%8E&limit=20',
      headers: { authorization: 'Bearer secret-token', 'content-type': 'application/json' },
      body: '{"display_name":"李女士"}',
    })
    const wire = server.seen.at(-1)!
    expect(wire.url).toBe('http://api.test/api/v1/customers')
    expect(wire.headers.has('authorization')).toBe(false)
    expect(wire.headers.get('content-type')).toBe(SEALED_TYPE)
    expect(new TextDecoder().decode(await wire.arrayBuffer())).not.toContain('李女士')
    expect(server.state.handshakes).toBe(1)
  })

  it('会话过期（428）时重新握手并重试一次；握手本身和其他地址不加密', async () => {
    const server = await fakeServer()
    const send = createTransport({ baseUrl: 'http://api.test', fetch: server.fetch, publicKey: server.publicKey })
    await send(new Request('http://api.test/api/v1/me'))
    server.state.expireNext = true
    const again = await send(new Request('http://api.test/api/v1/me', { headers: { Authorization: 'Bearer t' } }))
    expect(again.status).toBe(201)
    expect(server.state.handshakes).toBe(2)
    // 写死公钥时不再从后端取。
    expect(server.seen.some((r) => new URL(r.url).pathname === '/api/v1/transport/key')).toBe(false)
    const other = await send(new Request('http://files.test/a.png'))
    expect(await other.json()).toEqual({ plain: true })
  })

  it('服务器签名不对时拒绝（中间人换了服务器的公钥）', async () => {
    const server = await fakeServer()
    const impostor = await fakeServer()
    const send = createTransport({ baseUrl: 'http://api.test', fetch: server.fetch, publicKey: impostor.publicKey })
    await expect(send(new Request('http://api.test/api/v1/me'))).rejects.toThrow('服务器签名不对')
  })
})
