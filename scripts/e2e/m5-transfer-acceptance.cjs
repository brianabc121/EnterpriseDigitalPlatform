// P1 M5 验收：坐席 A 把会话转接给坐席 B，B 接受后看到完整历史，A 不再看到这个客户。
//
// 前置与 m4-workbench-acceptance.cjs 相同（后端、实时消费进程、调度进程、控制台、Widget、OpenIM）。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/m5-transfer-acceptance.cjs
const { chromium } = require('playwright')
const fs = require('fs')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const WIDGET = env('WIDGET_URL', 'http://localhost:5175')
const OPENIM_API = env('OPENIM_API_URL', 'http://localhost:10002')
const OPENIM_SECRET = env('OPENIM_SECRET', 'openim-dev-secret')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/m5')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `tr-${RUN}`
const PASSWORD = 'demo-pass-2026'

const summary = { tenant: TENANT, checks: [], consoleErrors: [], storedMessages: [] }
const check = (name, ok, detail) =>
  summary.checks.push(
    `${ok ? 'PASS' : 'FAIL'} ${name}${ok || detail === undefined ? '' : ` -> ${JSON.stringify(detail)}`}`,
  )

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
  if (!response.ok) {
    const error = new Error(`${method} ${url} -> ${response.status} ${text}`)
    error.status = response.status
    throw error
  }
  return data
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
      name: `转接验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  const admin = await login('admin')
  for (const [username, display_name] of [
    ['alice', '小艾'],
    ['bob', '小博'],
  ]) {
    await json(`${API}/api/v1/staff`, {
      method: 'POST',
      token: admin.access_token,
      body: { username, display_name, password: PASSWORD, role_codes: ['agent'] },
    })
  }
  const channels = await json(`${API}/api/v1/channels`, { token: admin.access_token })
  return { adminToken: admin.access_token, channelKey: channels.items[0].public_key }
}

function watchErrors(page, label) {
  page.on('console', (m) => {
    if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) {
      summary.consoleErrors.push(`${label}: ${m.text()}`)
    }
  })
  page.on('pageerror', (e) => summary.consoleErrors.push(`${label} pageerror: ${e.message}`))
}

async function openWorkbench(browser, username) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' })
  const page = await ctx.newPage()
  watchErrors(page, username)
  await page.goto(`${CONSOLE}/login`)
  await page.fill('input[placeholder="例如 demo"]', TENANT)
  await page.fill('input[autocomplete="username"]', username)
  await page.fill('input[autocomplete="current-password"]', PASSWORD)
  await page.click('button:has-text("登录")')
  await page.waitForSelector('[data-testid="main-menu"]')
  await page.locator('[data-testid="main-menu"] .el-menu-item', { hasText: '工作台' }).click()
  await page.locator('[data-testid="agent-status"]', { hasText: '在线' }).waitFor({ timeout: 15000 })
  return { ctx, page }
}

async function groupMembers(groupID, userIDs) {
  const op = () => ({ operationID: `e2e-${Date.now()}-${Math.random()}` })
  const admin = await json(`${OPENIM_API}/auth/get_admin_token`, {
    method: 'POST',
    headers: op(),
    body: { secret: OPENIM_SECRET, userID: 'imAdmin' },
  })
  const data = await json(`${OPENIM_API}/group/get_group_members_info`, {
    method: 'POST',
    headers: { ...op(), token: admin.data.token },
    body: { groupID, userIDs },
  })
  return (data.data.members || []).map((m) => m.userID)
}

async function run(browser) {
  const { adminToken, channelKey } = await prepareTenant()

  // 1. 小艾上线；访客发起咨询，分配给小艾
  const alice = await openWorkbench(browser, 'alice')
  const visitorCtx = await browser.newContext({ viewport: { width: 420, height: 720 }, locale: 'zh-CN' })
  const visitor = await visitorCtx.newPage()
  watchErrors(visitor, 'widget')
  await visitor.goto(`${WIDGET}/?key=${encodeURIComponent(channelKey)}`)
  await visitor.locator('[data-testid="widget-state"]', { hasText: '在线' }).waitFor()
  await visitor.fill('[data-testid="message-input"]', '你好，我想退订')
  await visitor.click('[data-testid="send-button"]')
  await alice.page.locator('[data-testid="session-item"]').first().waitFor({ timeout: 15000 })
  await alice.page.locator('[data-testid="session-item"]').first().click()
  await alice.page.fill('textarea[data-testid="composer-input"]', '这个问题我帮您转给同事小博')
  await alice.page.click('[data-testid="send-button"]')
  await visitor.locator('[data-testid="message"].agent', { hasText: '转给同事小博' }).waitFor()
  check('访客分配给小艾并收到回复', true)

  // 2. 小博上线；小艾发起转接
  const bob = await openWorkbench(browser, 'bob')
  await alice.page.click('[data-testid="transfer-session"]')
  await alice.page.click('[data-testid="transfer-agent"]')
  await alice.page.locator('.el-select-dropdown__item', { hasText: '小博' }).click()
  await alice.page.fill('[data-testid="transfer-dialog"] textarea', '客户要退订，麻烦跟进')
  await alice.page.click('[data-testid="transfer-submit"]')
  await alice.page.locator('[data-testid="transfer-pending"]').waitFor()
  check('小艾发起转接，等待对方接受', true)

  // 3. 小博收到转接请求并接受
  const prompt = bob.page.locator('[data-testid="incoming-transfer"]')
  await prompt.locator('text=客户要退订，麻烦跟进').waitFor({ timeout: 15000 })
  const promptText = await prompt.innerText()
  check('小博收到带备注的转接请求', promptText.includes('小艾'), promptText)
  await bob.page.screenshot({ path: `${SHOTS}/1-incoming-transfer.png` })
  await bob.page.click('[data-testid="accept-transfer"]')

  // 4. 访客收到转接提示；小博看到完整历史；小艾的列表中不再有这个会话
  await visitor
    .locator('[data-testid="message"].system', { hasText: '已为您转接至客服 小博' })
    .waitFor({ timeout: 15000 })
  check('访客收到"已为您转接至客服 小博"', true)
  await bob.page
    .locator('[data-testid="chat-message"]', { hasText: '你好，我想退订' })
    .waitFor({ timeout: 15000 })
  await bob.page.locator('[data-testid="chat-message"]', { hasText: '转给同事小博' }).waitFor()
  check('小博看到转接前的完整历史', true)
  await alice.page.locator('[data-testid="session-item"]').first().waitFor({ state: 'detached', timeout: 15000 })
  check('小艾的接待列表中不再有这个会话', true)

  // 5. 小博回复，访客实时收到
  await bob.page.fill('textarea[data-testid="composer-input"]', '您好，我是小博，马上为您办理')
  await bob.page.click('[data-testid="send-button"]')
  await visitor.locator('[data-testid="message"].agent', { hasText: '我是小博' }).waitFor()
  const label = await visitor
    .locator('[data-testid="message"].agent', { hasText: '我是小博' })
    .locator('.sender')
    .innerText()
  check('访客收到小博的回复并显示小博的名字', label === '小博', label)
  await bob.page.screenshot({ path: `${SHOTS}/2-bob-after-transfer.png` })
  await visitor.screenshot({ path: `${SHOTS}/3-widget-after-transfer.png` })

  // 6. 数据范围与 IM 成员：小艾看不到客户、已被移出服务群；小博在群里
  const aliceToken = (await login('alice')).access_token
  const bobToken = (await login('bob')).access_token
  const rooms = await json(`${API}/api/v1/rooms`, { token: adminToken })
  const room = rooms.items[0]
  const aliceSees = await json(`${API}/api/v1/customers/${room.customer_id}`, { token: aliceToken })
    .then(() => true)
    .catch((e) => (e.status === 404 ? false : Promise.reject(e)))
  const bobSees = await json(`${API}/api/v1/customers/${room.customer_id}`, { token: bobToken })
    .then(() => true)
    .catch(() => false)
  check('转接后小艾看不到这个客户，小博可以', !aliceSees && bobSees, { aliceSees, bobSees })
  const me = await json(`${API}/api/v1/me`, { token: aliceToken })
  const bobMe = await json(`${API}/api/v1/me`, { token: bobToken })
  const imUser = (staffId) => `${room.im_group_id.split('_r_')[0]}_s_${staffId.replaceAll('-', '')}`
  const members = await groupMembers(room.im_group_id, [imUser(me.id), imUser(bobMe.id)])
  check(
    '服务群成员：小博在群里，小艾已被移出',
    members.includes(imUser(bobMe.id)) && !members.includes(imUser(me.id)),
    members,
  )

  // 7. 入库：每条消息一行
  const page = await json(`${API}/api/v1/rooms/${room.id}/messages`, { token: adminToken })
  summary.storedMessages = page.items.reverse().map((m) => `${m.sender_type}:${m.text_plain}`)
  const expected = [
    'customer:你好，我想退订',
    'system:客服 小艾 为您服务。',
    'agent:这个问题我帮您转给同事小博',
    'system:已为您转接至客服 小博，请稍候。',
    'agent:您好，我是小博，马上为您办理',
  ]
  check(
    '消息入库完整且不重复',
    JSON.stringify(summary.storedMessages) === JSON.stringify(expected),
    summary.storedMessages,
  )
  check('没有前端脚本错误', summary.consoleErrors.length === 0, summary.consoleErrors)
  await alice.ctx.close()
  await bob.ctx.close()
  await visitorCtx.close()
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
