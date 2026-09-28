// P1 M2 + M4 验收：访客在 Widget 里发起咨询，会话分配给在线坐席；坐席在控制台工作台实时接待。
//
// 前置：make dev-up、make im-up、make migrate，创建平台账号；启动后端（make backend-dev）、
//       实时消费进程（make worker-dev）、调度进程（make scheduler-dev）、控制台（make console-dev）
//       和 Widget（make widget-dev）。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/m4-workbench-acceptance.cjs
const { chromium } = require('playwright')
const fs = require('fs')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const WIDGET = env('WIDGET_URL', 'http://localhost:5175')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/m4')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `wb-${RUN}`
const ADMIN_PASSWORD = 'admin-demo-2026'
const AGENT_PASSWORD = 'agent-demo-2026'
// 心跳每 30 秒刷新一次会话列表；要求在这之前出现，才能说明是分配信令触发的刷新。
const SIGNAL_DEADLINE_MS = 15000

const summary = { tenant: TENANT, checks: [], consoleErrors: [], storedMessages: [] }
const check = (name, ok, detail) =>
  summary.checks.push(`${ok ? 'PASS' : 'FAIL'} ${name}${ok || detail === undefined ? '' : ` -> ${JSON.stringify(detail)}`}`)

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
      name: `工作台验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: ADMIN_PASSWORD },
    },
  })
  const admin = await json(`${API}/api/v1/auth/login`, {
    method: 'POST',
    body: { tenant_code: TENANT, username: 'admin', password: ADMIN_PASSWORD },
  })
  await json(`${API}/api/v1/staff`, {
    method: 'POST',
    token: admin.access_token,
    body: {
      username: 'xiaoai',
      display_name: '小爱',
      password: AGENT_PASSWORD,
      role_codes: ['agent'],
    },
  })
  await json(`${API}/api/v1/quick-replies`, {
    method: 'POST',
    token: admin.access_token,
    body: {
      shared: true,
      category: '通用',
      title: '报价',
      content: '企业版每年 9800 元，含 10 个坐席。',
      sort: 0,
    },
  })
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

async function agentLogin(page) {
  await page.goto(`${CONSOLE}/login`)
  await page.fill('input[placeholder="例如 demo"]', TENANT)
  await page.fill('input[autocomplete="username"]', 'xiaoai')
  await page.fill('input[autocomplete="current-password"]', AGENT_PASSWORD)
  await page.click('button:has-text("登录")')
  await page.waitForSelector('[data-testid="main-menu"]')
  await page.locator('[data-testid="main-menu"] .el-menu-item', { hasText: '工作台' }).click()
  await page.locator('[data-testid="im-state"]', { hasText: '已连接' }).waitFor({ timeout: 20000 })
}

async function visitorSays(page, text) {
  await page.fill('[data-testid="message-input"]', text)
  await page.click('[data-testid="send-button"]')
  await page.locator('[data-testid="message"].me', { hasText: text }).waitFor()
}

async function agentSays(page, text) {
  // Element Plus 的输入框把 data-testid 放在原生 textarea / input 上。
  await page.fill('textarea[data-testid="composer-input"]', text)
  await page.click('[data-testid="send-button"]')
}

async function storedMessages(adminToken) {
  const rooms = await json(`${API}/api/v1/rooms`, { token: adminToken })
  const page = await json(`${API}/api/v1/rooms/${rooms.items[0].id}/messages`, {
    token: adminToken,
  })
  return page.items
    .reverse()
    .map((m) => `${m.sender_type}:${m.text_plain}:${m.source}${m.send_status ? `:${m.send_status}` : ''}`)
}

async function run(browser) {
  const { adminToken, channelKey } = await prepareTenant()

  // 1. 坐席登录控制台，进入工作台：自动上线并连接 IM
  const agentCtx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' })
  const agent = await agentCtx.newPage()
  watchErrors(agent, 'console')
  await agentLogin(agent)
  const status = (await agent.locator('[data-testid="agent-status"]').innerText()).trim()
  check('坐席进入工作台后自动上线并连接 IM', status === '在线', status)

  // 2. 访客打开 Widget 发起咨询
  const visitorCtx = await browser.newContext({ viewport: { width: 420, height: 720 }, locale: 'zh-CN' })
  const visitor = await visitorCtx.newPage()
  watchErrors(visitor, 'widget')
  await visitor.goto(`${WIDGET}/?key=${encodeURIComponent(channelKey)}`)
  await visitor.locator('[data-testid="widget-state"]', { hasText: '在线' }).waitFor()
  const askedAt = Date.now()
  await visitorSays(visitor, '你好，我想咨询企业版')

  // 3. 会话分配给坐席：分配信令让工作台立即刷新（早于心跳），并弹出提醒
  await agent.locator('[data-testid="session-item"]').first().waitFor({ timeout: SIGNAL_DEADLINE_MS })
  check('会话经分配信令实时出现在工作台', Date.now() - askedAt < SIGNAL_DEADLINE_MS)
  const notified = await agent
    .locator('.el-notification', { hasText: '新会话' })
    .waitFor({ timeout: 5000 })
    .then(() => true)
    .catch(() => false)
  check('坐席收到新会话提醒', notified)
  await visitor.locator('[data-testid="message"].system', { hasText: '客服 小爱 为您服务' }).waitFor()
  check('访客收到"客服 小爱 为您服务"', true)

  // 4. 坐席打开会话，看到历史消息（走平台接口）
  await agent.locator('[data-testid="session-item"]').first().click()
  await agent.locator('[data-testid="chat-message"]', { hasText: '你好，我想咨询企业版' }).waitFor()
  check('坐席看到访客的历史消息', true)

  // 5. 坐席回复，访客实时收到（显示坐席姓名）
  await agentSays(agent, '您好，我是小爱，请问有什么可以帮您？')
  await visitor
    .locator('[data-testid="message"].agent', { hasText: '您好，我是小爱，请问有什么可以帮您？' })
    .waitFor()
  const agentLabel = await visitor.locator('[data-testid="message"].agent .sender').first().innerText()
  check('访客实时收到坐席回复并显示坐席姓名', agentLabel === '小爱', agentLabel)

  // 6. 访客继续发消息，坐席实时收到（走 IM），不重复
  await visitorSays(visitor, '企业版多少钱？')
  await agent.locator('[data-testid="chat-message"]', { hasText: '企业版多少钱？' }).waitFor()
  await agent.waitForTimeout(1500)
  const dupCount = await agent.locator('[data-testid="chat-message"]', { hasText: '企业版多少钱？' }).count()
  const replyCount = await agent
    .locator('[data-testid="chat-message"]', { hasText: '您好，我是小爱，请问有什么可以帮您？' })
    .count()
  check('坐席实时收到访客消息，消息不重复', dupCount === 1 && replyCount === 1, { dupCount, replyCount })

  // 7. 快捷话术
  await agent.click('[data-testid="quick-replies-button"]')
  await agent.locator('[data-testid="quick-reply-item"]', { hasText: '报价' }).click()
  const draft = await agent.locator('textarea[data-testid="composer-input"]').inputValue()
  await agent.click('[data-testid="send-button"]')
  await visitor.locator('[data-testid="message"].agent', { hasText: '企业版每年 9800 元' }).waitFor()
  check('快捷话术插入并发送', draft === '企业版每年 9800 元，含 10 个坐席。', draft)
  await agent.screenshot({ path: `${SHOTS}/1-workbench-chat.png` })
  await visitor.screenshot({ path: `${SHOTS}/2-widget-chat.png` })

  // 8. 客户面板：修改名称、备注，添加标签
  await agent.fill('input[data-testid="customer-name"]', '王先生')
  await agent.fill('textarea[data-testid="customer-notes"]', '关注企业版报价')
  await agent.click('[data-testid="save-customer"]')
  await agent.fill('input[data-testid="customer-tag-input"]', '意向高')
  await agent.press('input[data-testid="customer-tag-input"]', 'Enter')
  await agent.locator('[data-testid="customer-panel"] .el-tag', { hasText: '意向高' }).waitFor()
  const customers = await json(`${API}/api/v1/customers`, { token: adminToken })
  const customer = customers.items[0]
  check(
    '客户面板保存名称与标签',
    customer.display_name === '王先生' && customer.tags.includes('意向高'),
    customer,
  )

  // 9. 坐席结束会话：访客收到结束提示，会话从接待列表移除
  await agent.click('[data-testid="close-session"]')
  await agent.locator('.el-message-box button', { hasText: '结束' }).click()
  await visitor.locator('[data-testid="message"].system', { hasText: '本次会话已结束' }).waitFor()
  await agent.locator('[data-testid="session-item"]').first().waitFor({ state: 'detached' })
  check('结束会话后访客收到提示、接待列表清空', true)
  await agent.screenshot({ path: `${SHOTS}/3-workbench-closed.png` })

  // 10. 入库检查：每条消息只有一行，坐席消息经 API 发出且已发送
  await agent.waitForTimeout(1500)
  summary.storedMessages = await storedMessages(adminToken)
  const expected = [
    'customer:你好，我想咨询企业版:webhook',
    'system:客服 小爱 为您服务。:webhook',
    'agent:您好，我是小爱，请问有什么可以帮您？:api:sent',
    'customer:企业版多少钱？:webhook',
    'agent:企业版每年 9800 元，含 10 个坐席。:api:sent',
    'system:本次会话已结束，感谢您的咨询。:webhook',
  ]
  check(
    '消息入库完整且不重复',
    JSON.stringify(summary.storedMessages) === JSON.stringify(expected),
    summary.storedMessages,
  )
  check('没有前端脚本错误', summary.consoleErrors.length === 0, summary.consoleErrors)
  await agentCtx.close()
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
