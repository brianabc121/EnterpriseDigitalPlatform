// P1 M3 验收：在第三方网站嵌入访客 Widget。
//
// 管理员在控制台设置窗口标题、欢迎语、隐私提示、允许嵌入的网站并启用实名访客；
// 一个"客户网站"（本脚本起的本地 HTTP 服务，模拟网站后端为登录用户签名）通过 embed.js 嵌入 Widget。
// 覆盖：实名访客与跨设备续接、图片与文件的上传和显示、收起时的未读角标、满意度评价、留言、
// 未授权网站被拒绝。
//
// 前置与 m4-workbench-acceptance.cjs 相同（后端、实时消费进程、调度进程、控制台、Widget、OpenIM），
// 另需 make dev-up 起的 MinIO。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/m3-widget-acceptance.cjs
const { chromium } = require('playwright')
const crypto = require('crypto')
const fs = require('fs')
const http = require('http')
const zlib = require('zlib')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const WIDGET = env('WIDGET_URL', 'http://localhost:5175')
const SITE_PORT = Number(env('SITE_PORT', '5176'))
const SITE = `http://localhost:${SITE_PORT}`
// 同一个服务换成 127.0.0.1 访问就是另一个来源（origin），用来验证未授权网站被拒绝。
const OTHER_SITE = `http://127.0.0.1:${SITE_PORT}`
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/m3')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `wg-${RUN}`
const PASSWORD = 'demo-pass-2026'
const USER = { external_id: `member-${RUN}`, name: '王先生' }

const summary = { tenant: TENANT, checks: [], consoleErrors: [], storedMessages: [] }
const check = (name, ok, detail) =>
  summary.checks.push(
    `${ok ? 'PASS' : 'FAIL'} ${name}${ok || detail === undefined ? '' : ` -> ${JSON.stringify(detail)}`}`,
  )

async function json(url, { method = 'GET', token, body } = {}) {
  const response = await fetch(url, {
    method,
    headers: {
      'content-type': 'application/json',
      ...(token ? { authorization: `Bearer ${token}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const text = await response.text()
  if (!response.ok) throw new Error(`${method} ${url} -> ${response.status} ${text}`)
  return text ? JSON.parse(text) : null
}

async function login(username) {
  return json(`${API}/api/v1/auth/login`, {
    method: 'POST',
    body: { tenant_code: TENANT, username, password: PASSWORD },
  })
}

async function prepareTenant() {
  const platform = await json(`${API}/platform/v1/auth/login`, {
    method: 'POST',
    body: { username: PLATFORM_USER, password: PLATFORM_PASSWORD },
  })
  await json(`${API}/platform/v1/tenants`, {
    method: 'POST',
    token: platform.access_token,
    body: {
      code: TENANT,
      name: `嵌入验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  const admin = await login('admin')
  await json(`${API}/api/v1/staff`, {
    method: 'POST',
    token: admin.access_token,
    body: { username: 'alice', display_name: '小艾', password: PASSWORD, role_codes: ['agent'] },
  })
  const channels = await json(`${API}/api/v1/channels`, { token: admin.access_token })
  return { adminToken: admin.access_token, channelKey: channels.items[0].public_key }
}

// ---- 测试素材：纯色 PNG 与一个小 PDF ----

const CRC_TABLE = Array.from({ length: 256 }, (_, n) => {
  let c = n
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
  return c >>> 0
})
function crc32(buf) {
  let c = 0xffffffff
  for (const b of buf) c = CRC_TABLE[(c ^ b) & 0xff] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}
function chunk(type, data) {
  const len = Buffer.alloc(4)
  len.writeUInt32BE(data.length)
  const body = Buffer.concat([Buffer.from(type), data])
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(body))
  return Buffer.concat([len, body, crc])
}
function png(width, height, [r, g, b]) {
  const header = Buffer.alloc(13)
  header.writeUInt32BE(width, 0)
  header.writeUInt32BE(height, 4)
  header.set([8, 2, 0, 0, 0], 8) // 8 位 RGB
  const row = Buffer.concat([Buffer.from([0]), Buffer.from(Array(width).fill([r, g, b]).flat())])
  const pixels = zlib.deflateSync(Buffer.concat(Array(height).fill(row)))
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', header),
    chunk('IDAT', pixels),
    chunk('IEND', Buffer.alloc(0)),
  ])
}
const PHOTO = png(160, 120, [22, 119, 255])
const QUOTE_PDF = Buffer.from('%PDF-1.4\n% EDP e2e 报价单\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n')

// ---- "客户网站"：页面由网站后端渲染，为当前登录用户签名 ----

function startSite(getSecret, channelKey) {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, SITE)
    let identity = ''
    if (url.pathname === '/shop.html') {
      const timestamp = Math.floor(Date.now() / 1000)
      const signature = crypto
        .createHmac('sha256', getSecret())
        .update(`${USER.external_id}:${USER.name}:${timestamp}`)
        .digest('hex')
      identity = `<script>window.EDPWidgetConfig = ${JSON.stringify({
        user: { ...USER, timestamp, signature },
      })}</script>`
    } else if (url.pathname !== '/guest.html') {
      res.writeHead(404).end()
      return
    }
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
    res.end(`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>Acme 商城</title>
<style>body{font-family:sans-serif;margin:0;background:#f5f7fa}header{background:#fff;padding:16px 32px;
box-shadow:0 1px 3px rgba(0,0,0,.08)}main{padding:32px}</style></head>
<body><header><strong>Acme 商城</strong> · 会员中心</header>
<main><h1>我的订单</h1><p>订单 A20260928 · 待发货</p></main>
${identity}
<script src="${WIDGET}/embed.js" data-key="${channelKey}" async></script>
</body></html>`)
  })
  return new Promise((resolve) => server.listen(SITE_PORT, () => resolve(server)))
}

function watchErrors(page, label) {
  page.on('console', (m) => {
    if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) {
      summary.consoleErrors.push(`${label}: ${m.text()}`)
    }
  })
  page.on('pageerror', (e) => summary.consoleErrors.push(`${label} pageerror: ${e.message}`))
}

async function consoleLogin(browser, username) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' })
  const page = await ctx.newPage()
  watchErrors(page, username)
  await page.goto(`${CONSOLE}/login`)
  await page.fill('input[placeholder="例如 demo"]', TENANT)
  await page.fill('input[autocomplete="username"]', username)
  await page.fill('input[autocomplete="current-password"]', PASSWORD)
  await page.click('button:has-text("登录")')
  await page.waitForSelector('[data-testid="main-menu"]')
  return { ctx, page }
}

const menu = (page, title) =>
  page.locator('[data-testid="main-menu"] .el-menu-item', { hasText: title }).click()

/** 打开嵌入了 Widget 的页面，点开客服按钮，返回 Widget 所在的 frame。 */
async function openShop(browser, url, label, { waitHistory = false } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, locale: 'zh-CN' })
  const page = await ctx.newPage()
  watchErrors(page, label)
  // 老访客：收起的窗口加载完历史消息后，按钮上不应该有角标（以前的消息不算新消息）。
  const history = waitHistory
    ? page.waitForResponse((r) => r.url().includes('/api/v1/visitor/messages'), { timeout: 20000 })
    : null
  await page.goto(url)
  const button = page.locator('[data-edp-widget-button]')
  await button.waitFor()
  const frameEl = page.locator('iframe[title="在线客服"]')
  const hiddenBefore = !(await frameEl.isVisible())
  let badgeBefore = null
  if (history) {
    await history
    await page.waitForTimeout(1500)
    badgeBefore = await button.locator('span').isVisible()
  }
  await button.click()
  await frameEl.waitFor({ state: 'visible' })
  return {
    ctx,
    page,
    button,
    frame: page.frameLocator('iframe[title="在线客服"]'),
    hiddenBefore,
    badgeBefore,
  }
}

async function imageLoaded(locator) {
  await locator.waitFor({ timeout: 15000 })
  // 图片地址是平台文件链接：302 到对象存储的临时地址，能显示说明上传和下载都通了。
  for (let i = 0; i < 30; i++) {
    const width = await locator.evaluate((img) => (img.complete ? img.naturalWidth : 0))
    if (width > 0) return width
    await new Promise((r) => setTimeout(r, 300))
  }
  return 0
}

async function run(browser) {
  const { adminToken, channelKey } = await prepareTenant()
  let secret = ''
  const site = await startSite(() => secret, channelKey)

  try {
    // 1. 管理员在控制台设置渠道：窗口文案、允许嵌入的网站、实名访客密钥
    const admin = await consoleLogin(browser, 'admin')
    await menu(admin.page, '设置')
    await admin.page.click('[data-testid="edit-channel-官网"]')
    const editor = admin.page.locator('[data-testid="channel-editor"]')
    await editor.waitFor()
    await admin.page.fill('input[data-testid="widget-title-input"]', 'Acme 客服')
    await admin.page.fill('textarea[data-testid="welcome-input"]', '您好，欢迎咨询 Acme 商城！')
    await admin.page.fill('textarea[data-testid="privacy-input"]', '对话内容仅用于为您提供服务。')
    const origins = editor.locator('input[data-testid="origins-input"]')
    await origins.fill(SITE)
    await origins.press('Enter')
    await admin.page.click('[data-testid="save-channel"]')
    await admin.page.locator('.el-message--success', { hasText: '已保存' }).waitFor()
    await admin.page.click('[data-testid="rotate-secret"]')
    await admin.page.locator('.el-message--success', { hasText: '签名密钥' }).waitFor()
    secret = (await admin.page.locator('[data-testid="identity-secret"]').innerText()).trim()
    const snippet = await admin.page.locator('[data-testid="embed-snippet"]').innerText()
    await admin.page.screenshot({ path: `${SHOTS}/1-channel-settings.png` })
    const [channel] = (await json(`${API}/api/v1/channels`, { token: adminToken })).items
    check(
      '控制台保存了窗口设置、允许的网站和签名密钥',
      channel.widget.title === 'Acme 客服' &&
        JSON.stringify(channel.widget.allowed_origins) === JSON.stringify([SITE]) &&
        /^[0-9a-f]{64}$/.test(secret) &&
        channel.identity_secret === secret &&
        snippet.includes(`data-key="${channelKey}"`),
      { widget: channel.widget, secretLength: secret.length },
    )

    // 2. 小艾上线
    const alice = await consoleLogin(browser, 'alice')
    await menu(alice.page, '工作台')
    await alice.page.locator('[data-testid="agent-status"]', { hasText: '在线' }).waitFor({ timeout: 15000 })

    // 3. 已登录的会员在客户网站打开 Widget
    const shop = await openShop(browser, `${SITE}/shop.html`, 'shop')
    const w = shop.frame
    await w.locator('[data-testid="widget-state"]', { hasText: '在线' }).waitFor({ timeout: 15000 })
    const title = await w.locator('[data-testid="widget-title"]').innerText()
    const welcome = await w.locator('[data-testid="welcome"]').innerText()
    const privacy = await w.locator('[data-testid="privacy-notice"]').isVisible()
    check(
      '嵌入按钮默认收起；打开后显示标题、欢迎语和隐私提示',
      shop.hiddenBefore && title === 'Acme 客服' && welcome.includes('欢迎咨询 Acme 商城') && privacy,
      { hiddenBefore: shop.hiddenBefore, title, welcome, privacy },
    )
    await shop.page.screenshot({ path: `${SHOTS}/2-embedded-widget.png` })

    await w.locator('[data-testid="message-input"]').fill('我是老会员，想问下订单什么时候发货')
    await w.locator('[data-testid="send-button"]').click()
    const item = alice.page.locator('[data-testid="session-item"]').first()
    await item.waitFor({ timeout: 15000 })
    await item.click()
    const chatTitle = await alice.page.locator('[data-testid="chat-title"]').innerText()
    check('实名访客：坐席看到网站上的用户名', chatTitle === USER.name, chatTitle)
    check(
      '发出第一条消息后隐私提示消失',
      !(await w.locator('[data-testid="privacy-notice"]').isVisible()),
    )

    await alice.page.fill('textarea[data-testid="composer-input"]', '王先生您好，我帮您查一下')
    await alice.page.click('[data-testid="send-button"]')
    await w.locator('[data-testid="message"].agent', { hasText: '帮您查一下' }).waitFor()
    await w.locator('[data-testid="service-banner"]', { hasText: '小艾' }).waitFor({ timeout: 15000 })
    check('访客看到"客服 小艾 正在为您服务"', true)

    // 4. 访客发图片，坐席发文件
    await w.locator('[data-testid="file-input"]').setInputFiles({
      name: '订单截图.png',
      mimeType: 'image/png',
      buffer: PHOTO,
    })
    const visitorImage = await imageLoaded(w.locator('[data-testid="message-image"]').first())
    const agentImage = await imageLoaded(
      alice.page.locator('[data-testid="message-image"] img').first(),
    )
    check('访客上传图片，双方都能看到', visitorImage === 160 && agentImage === 160, {
      visitorImage,
      agentImage,
    })

    await alice.page.setInputFiles('input[data-testid="attach-input"]', {
      name: '报价单.pdf',
      mimeType: 'application/pdf',
      buffer: QUOTE_PDF,
    })
    const file = w.locator('[data-testid="message-file"]', { hasText: '报价单.pdf' })
    await file.waitFor({ timeout: 15000 })
    const href = await file.getAttribute('href')
    const downloaded = Buffer.from(await (await fetch(href)).arrayBuffer())
    check('坐席发送文件，访客收到并可下载', downloaded.equals(QUOTE_PDF), {
      href,
      size: downloaded.length,
    })
    await alice.page.screenshot({ path: `${SHOTS}/3-workbench-attachments.png` })
    await shop.page.screenshot({ path: `${SHOTS}/4-widget-attachments.png` })

    // 5. 收起 Widget 后收到消息：客服按钮上显示未读数；再打开时清零
    await shop.button.click()
    await shop.page.locator('iframe[title="在线客服"]').waitFor({ state: 'hidden' })
    await alice.page.fill('textarea[data-testid="composer-input"]', '订单今天下午发货')
    await alice.page.click('[data-testid="send-button"]')
    const badge = shop.button.locator('span')
    const counted = await badge
      .filter({ hasText: /^1$/ })
      .waitFor({ timeout: 15000 })
      .then(() => true)
      .catch(() => false)
    await shop.page.screenshot({ path: `${SHOTS}/5-unread-badge.png` })
    const badgeText = await badge.textContent()
    await shop.button.click()
    await badge.waitFor({ state: 'hidden' })
    check('收起时收到消息显示未读角标，打开后清零', counted, { badge: badgeText })

    // 6. 坐席结束会话，访客评价
    await alice.page.click('[data-testid="close-session"]')
    await alice.page.locator('.el-message-box button', { hasText: '结束' }).click()
    await w.locator('[data-testid="csat"]').waitFor({ timeout: 20000 })
    await w.locator('[data-testid="csat-5"]').click()
    await w.locator('[data-testid="csat"] input').fill('回复很及时')
    await w.locator('[data-testid="csat-submit"]').click()
    await w.locator('[data-testid="csat-thanks"]').waitFor()
    await shop.page.screenshot({ path: `${SHOTS}/6-csat.png` })
    const sessions = await json(`${API}/api/v1/sessions?status=closed`, { token: adminToken })
    const rated = sessions.items[0]
    check('会话结束后访客评价，评分入库', rated?.csat === 5 && rated.csat_comment === '回复很及时', rated)

    // 7. 留言
    await w.locator('[data-testid="leave-message-tab"]').click()
    const form = w.locator('[data-testid="leave-message"]')
    await form.locator('textarea').fill('请把发票寄到公司地址')
    await form.locator('input').fill('13800000000')
    await form.locator('button[type="submit"]').click()
    await w.locator('[data-testid="leave-thanks"]').waitFor()
    const todos = await json(`${API}/api/v1/todos`, { token: adminToken })
    const todo = todos.items.find((t) => t.source === 'visitor')
    const contact = todo?.fields.find((f) => f.key === 'contact')?.value
    check(
      '访客留言生成"留言"类待办',
      todo?.type_code === 'leave_message' &&
        todo.detail === '请把发票寄到公司地址' &&
        contact === '13800000000',
      todos.items,
    )
    await shop.ctx.close()

    // 8. 同一会员换一台设备（没有本地访客令牌）：还是同一个客户，看得到之前的对话
    const phone = await openShop(browser, `${SITE}/shop.html`, 'second-device', {
      waitHistory: true,
    })
    await phone.frame
      .locator('[data-testid="message"]', { hasText: '想问下订单什么时候发货' })
      .waitFor({ timeout: 15000 })
    const customers = await json(`${API}/api/v1/customers`, { token: adminToken })
    check('实名访客换设备后续接同一客户和对话历史', customers.total === 1, customers.items)
    check('换设备打开网页：以前的消息不算未读，按钮上没有角标', phone.badgeBefore === false, {
      badgeBefore: phone.badgeBefore,
    })
    await phone.ctx.close()

    // 9. 未授权的网站嵌入：拒绝
    const other = await openShop(browser, `${OTHER_SITE}/guest.html`, 'other-site')
    const alert = other.frame.locator('[role="alert"]')
    await alert.waitFor({ timeout: 15000 })
    const alertText = await alert.innerText()
    check('未授权网站嵌入被拒绝', alertText.includes('未被授权'), alertText)
    await other.page.screenshot({ path: `${SHOTS}/7-origin-denied.png` })
    await other.ctx.close()

    // 10. 入库：每条消息一行，图片和文件都有记录
    const rooms = await json(`${API}/api/v1/rooms`, { token: adminToken })
    const page = await json(`${API}/api/v1/rooms/${rooms.items[0].id}/messages`, {
      token: adminToken,
    })
    summary.storedMessages = page.items
      .reverse()
      .map((m) => `${m.sender_type}:${m.content_type}:${m.text_plain ?? m.content.name ?? ''}`)
    const expected = [
      'customer:text:我是老会员，想问下订单什么时候发货',
      'system:text:客服 小艾 为您服务。',
      'agent:text:王先生您好，我帮您查一下',
      'customer:image:',
      'agent:file:报价单.pdf',
      'agent:text:订单今天下午发货',
    ]
    const stored = summary.storedMessages
    check(
      '消息入库完整且不重复（含图片、文件）',
      JSON.stringify(stored.slice(0, expected.length)) === JSON.stringify(expected) &&
        stored.slice(expected.length).every((m) => m.startsWith('system:text:')),
      stored,
    )
    check('没有前端脚本错误', summary.consoleErrors.length === 0, summary.consoleErrors)
    await admin.ctx.close()
    await alice.ctx.close()
  } finally {
    site.close()
  }
}

;(async () => {
  if (!PLATFORM_PASSWORD) {
    console.error('请通过环境变量 PLATFORM_PASSWORD 提供平台运营账号的密码')
    process.exit(2)
  }
  fs.mkdirSync(SHOTS, { recursive: true })
  const browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_PATH || undefined,
    args: ['--no-sandbox'],
  })
  try {
    await run(browser)
  } finally {
    await browser.close()
    fs.writeFileSync(`${SHOTS}/summary.json`, JSON.stringify(summary, null, 2))
    console.log(JSON.stringify(summary, null, 2))
  }
  process.exit(summary.checks.some((c) => c.startsWith('FAIL')) ? 1 : 0)
})().catch((error) => {
  console.error(error)
  process.exit(1)
})
