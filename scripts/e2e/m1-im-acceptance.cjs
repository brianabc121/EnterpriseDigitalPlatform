// P1 M1 验收：访客在浏览器里通过 Widget 与服务群收发消息，消息可靠进入平台消息库。
//
// 前置：make dev-up、make im-up、make migrate，创建平台账号；启动后端（make backend-dev）
//       和 Widget（pnpm --filter @edp/widget dev）。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/m1-im-acceptance.cjs
//
// 可选"回调丢失"阶段：同时提供以下三个命令时执行——停掉后端（回调无法送达）后访客再发一条消息，
// 重新启动后端并执行对账，检查这条消息被补录。
//   BACKEND_STOP_CMD、BACKEND_START_CMD、RECONCILE_CMD（例如 "cd backend && uv run python -m app.cli im-reconcile"）
const { chromium } = require('playwright')
const { execSync } = require('child_process')
const fs = require('fs')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const WIDGET = env('WIDGET_URL', 'http://localhost:5175')
const OPENIM_API = env('OPENIM_API_URL', 'http://localhost:10002')
const OPENIM_SECRET = env('OPENIM_SECRET', 'openim-dev-secret')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/m1')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `im-${RUN}`
const ADMIN_PASSWORD = 'admin-demo-2026'

const summary = { tenant: TENANT, checks: [], consoleErrors: [], storedMessages: [] }
const check = (name, ok) => summary.checks.push(`${ok ? 'PASS' : 'FAIL'} ${name}`)

async function json(url, { method = 'GET', token, headers = {}, body } = {}) {
  const response = await fetch(url, {
    method,
    headers: {
      'content-type': 'application/json',
      ...(token ? { authorization: `Bearer ${token}` } : {}),
      ...headers,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const text = await response.text()
  const data = text ? JSON.parse(text) : null
  if (!response.ok) throw new Error(`${method} ${url} -> ${response.status} ${text}`)
  return data
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
      name: `联调企业 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: ADMIN_PASSWORD },
    },
  })
  const admin = await json(`${API}/api/v1/auth/login`, {
    method: 'POST',
    body: { tenant_code: TENANT, username: 'admin', password: ADMIN_PASSWORD },
  })
  const channels = await json(`${API}/api/v1/channels`, { token: admin.access_token })
  return { adminToken: admin.access_token, channelKey: channels.items[0].public_key }
}

async function botReply(groupID, text) {
  const op = () => ({ operationID: `e2e-${Date.now()}-${Math.random()}` })
  const admin = await json(`${OPENIM_API}/auth/get_admin_token`, {
    method: 'POST',
    headers: op(),
    body: { secret: OPENIM_SECRET, userID: 'imAdmin' },
  })
  const sent = await json(`${OPENIM_API}/msg/send_msg`, {
    method: 'POST',
    headers: { ...op(), token: admin.data.token },
    body: {
      // 服务群 ID 形如 {IM 前缀}_r_{roomId}，机器人是 {IM 前缀}_bot（租户代码里的 "-" 写作 "X"）。
      sendID: `${groupID.split('_r_')[0]}_bot`,
      groupID,
      senderPlatformID: 5,
      content: { content: text },
      contentType: 101,
      sessionType: 3,
    },
  })
  if (sent.errCode !== 0) throw new Error(`bot send failed: ${JSON.stringify(sent)}`)
}

async function storedTexts(adminToken) {
  const rooms = await json(`${API}/api/v1/rooms`, { token: adminToken })
  if (rooms.items.length === 0) return []
  const page = await json(`${API}/api/v1/rooms/${rooms.items[0].id}/messages`, {
    token: adminToken,
  })
  return page.items.reverse().map((m) => `${m.sender_type}:${m.text_plain}:${m.source}`)
}

async function waitFor(fn, predicate, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs
  let value
  while (Date.now() < deadline) {
    value = await fn()
    if (predicate(value)) return value
    await new Promise((r) => setTimeout(r, 300))
  }
  return value
}

async function sendFromWidget(page, text) {
  await page.fill('[data-testid="message-input"]', text)
  await page.click('[data-testid="send-button"]')
  await page.locator('[data-testid="message"].me', { hasText: text }).waitFor()
}

async function run(browser) {
  const { adminToken, channelKey } = await prepareTenant()
  const ctx = await browser.newContext({ viewport: { width: 480, height: 720 }, locale: 'zh-CN' })
  const page = await ctx.newPage()
  page.on('console', (m) => {
    if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) {
      summary.consoleErrors.push(m.text())
    }
  })
  page.on('pageerror', (e) => summary.consoleErrors.push(`pageerror: ${e.message}`))

  // 1. 访客打开 Widget：初始化身份、登录 OpenIM
  await page.goto(`${WIDGET}/?key=${encodeURIComponent(channelKey)}`)
  await page.locator('[data-testid="widget-state"]', { hasText: '在线' }).waitFor()
  check('访客连接到 IM', true)

  // 2. 访客发消息；机器人（平台经 OpenIM REST）回复，访客实时收到
  await sendFromWidget(page, '你好，我想咨询价格')
  const tokenKey = await page.evaluate(() => {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (key && key.startsWith('edp.visitor.')) return key
    }
    return null
  })
  check('访客令牌已保存', Boolean(tokenKey))
  const rooms = await json(`${API}/api/v1/rooms`, { token: adminToken })
  await botReply(rooms.items[0].im_group_id, '您好，请问是哪款产品？')
  await page.locator('[data-testid="message"].bot', { hasText: '您好，请问是哪款产品？' }).waitFor()
  check('访客实时收到机器人回复', true)
  await page.screenshot({ path: `${SHOTS}/1-widget-chat.png` })

  const stored = await waitFor(
    () => storedTexts(adminToken),
    (texts) => texts.length >= 2,
  )
  check(
    '两条消息经回调入库',
    JSON.stringify(stored) ===
      JSON.stringify([
        'customer:你好，我想咨询价格:webhook',
        'bot:您好，请问是哪款产品？:webhook',
      ]),
  )

  // 3. 刷新页面：同一访客，历史消息仍在
  await page.reload()
  await page.locator('[data-testid="widget-state"]', { hasText: '在线' }).waitFor()
  await page.locator('[data-testid="message"]', { hasText: '您好，请问是哪款产品？' }).waitFor()
  const roomsAfterReload = await json(`${API}/api/v1/rooms`, { token: adminToken })
  check('刷新后仍是同一个访客和 Room', roomsAfterReload.total === 1)

  // 4. 可选：回调丢失后由对账补录
  const { BACKEND_STOP_CMD, BACKEND_START_CMD, RECONCILE_CMD } = process.env
  if (BACKEND_STOP_CMD && BACKEND_START_CMD && RECONCILE_CMD) {
    execSync(BACKEND_STOP_CMD, { stdio: 'inherit' })
    await sendFromWidget(page, '（后端停机期间）还在吗？')
    execSync(BACKEND_START_CMD, { stdio: 'inherit' })
    const before = await storedTexts(adminToken)
    check('停机期间的消息没有经回调入库', before.length === 2)
    const report = execSync(RECONCILE_CMD, { encoding: 'utf8' })
    summary.reconcile = report.trim().split('\n').pop()
    const after = await storedTexts(adminToken)
    check(
      '对账补录了回调丢失的消息',
      after.length === 3 && after[2] === 'customer:（后端停机期间）还在吗？:reconcile',
    )
  }
  await page.screenshot({ path: `${SHOTS}/2-widget-after-reload.png` })
  summary.storedMessages = await storedTexts(adminToken)
  check('没有前端脚本错误', summary.consoleErrors.length === 0)
  await ctx.close()
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
