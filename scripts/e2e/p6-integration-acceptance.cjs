// P6 企业系统对接验收（设计文档 §25.8）与待办、订单报表（§24.10、§25.10）。
//
// 1. 管理员在"设置 → 企业系统对接"创建接口密钥（完整密钥只显示一次）和推送地址（签名密钥只显示
//    一次），测试推送：本脚本起一个本地接收服务，校验每条推送的签名。
// 2. 企业系统用接口密钥同步商品、创建订单（带自己的订单号）：推送 order.created；订单中心看到
//    来源"企业系统"、企业系统单号。
// 3. 企业系统回传发货（跳过确认，确认之后以企业系统为准）：推送 order.confirmed 和
//    order.status_changed；订单详情显示已发货，修改历史的操作人是"企业系统"；跟踪页看到物流。
// 4. 推送失败：接收方返回 500 时进入重试，运营后台"运维 → 企业系统推送"也能看到；测试推送失败后
//    在推送记录里重发，运营后台重发重试中的推送，接收方恢复后都能送达。
// 5. 企业系统创建待办（直接进入待办列表）；员工完成后推送 todo.done，带回企业系统的单号。
// 6. 报表：待办、订单页签。
// 7. 撤销接口密钥后立即不能调用。
//
// 前置：后端、实时消费进程、调度进程；控制台、运营后台、Widget（与其他验收脚本相同）。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p6-integration-acceptance.cjs
const { chromium } = require('playwright')
const { execFile } = require('child_process')
const crypto = require('crypto')
const fs = require('fs')
const http = require('http')
const path = require('path')
const { promisify } = require('util')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PLATFORM = env('PLATFORM_URL', 'http://localhost:5174')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p6-integration')
const RECEIVER_PORT = Number(env('RECEIVER_PORT', '18990'))
const BACKEND_DIR = path.resolve(__dirname, '../../backend')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `p6i-${RUN}`
const PASSWORD = 'demo-pass-2026'
const ERP_NO = `ERP-${RUN}`
const CRM_REF = `CRM-${RUN}`
const HOOK = `http://127.0.0.1:${RECEIVER_PORT}/hooks`

const summary = { tenant: TENANT, checks: [], consoleErrors: [] }
const check = (name, ok, detail) =>
  summary.checks.push(
    `${ok ? 'PASS' : 'FAIL'} ${name}${ok || detail === undefined ? '' : ` -> ${JSON.stringify(detail)}`}`,
  )

async function request(url, { method = 'GET', token, body } = {}) {
  const response = await fetch(url, {
    method,
    headers: {
      'content-type': 'application/json',
      ...(token ? { authorization: `Bearer ${token}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const text = await response.text()
  return { status: response.status, body: text ? JSON.parse(text) : null }
}

async function json(url, options) {
  const { status, body } = await request(url, options)
  if (status >= 400) throw new Error(`${options?.method ?? 'GET'} ${url} -> ${status} ${JSON.stringify(body)}`)
  return body
}

async function waitFor(fn, timeout = 20000) {
  const deadline = Date.now() + timeout
  for (;;) {
    const value = await fn().catch(() => null)
    if (value || Date.now() > deadline) return value
    await new Promise((r) => setTimeout(r, 1000))
  }
}

/**
 * 在后端目录执行命令行工具（平时由调度进程每 10 秒执行的推送任务）。必须异步执行：本脚本自己
 * 就是推送的接收方，同步等待子进程会卡住事件循环，推送等到超时被记成失败。
 */
async function cli(...args) {
  const { stdout } = await promisify(execFile)('uv', ['run', 'python', '-m', 'app.cli', ...args], {
    cwd: BACKEND_DIR,
    env: process.env,
    encoding: 'utf-8',
  })
  const lines = stdout.trim().split('\n')
  return JSON.parse(lines[lines.length - 1])
}

// ---- 模拟企业系统接收推送 ----

const receiver = { secret: '', fail: false, received: [] }

function verify(secret, header, body) {
  const parts = Object.fromEntries(
    String(header || '')
      .split(',')
      .map((p) => p.split('=', 2)),
  )
  const expected = crypto.createHmac('sha256', secret).update(`${parts.t}.${body}`).digest('hex')
  const fresh = Math.abs(Date.now() / 1000 - Number(parts.t)) < 300
  return (
    fresh &&
    typeof parts.v1 === 'string' &&
    parts.v1.length === expected.length &&
    crypto.timingSafeEqual(Buffer.from(parts.v1), Buffer.from(expected))
  )
}

function startReceiver() {
  const server = http.createServer((req, res) => {
    let body = ''
    req.on('data', (chunk) => (body += chunk))
    req.on('end', () => {
      const payload = JSON.parse(body || '{}')
      receiver.received.push({
        event: req.headers['x-edp-event'],
        delivery: req.headers['x-edp-delivery'],
        valid: verify(receiver.secret, req.headers['x-edp-signature'], body),
        payload,
        failed: receiver.fail,
      })
      res.writeHead(receiver.fail ? 500 : 200, { 'content-type': 'application/json' })
      res.end(receiver.fail ? '{"error":"busy"}' : '{"ok":true}')
    })
  })
  return new Promise((resolve) => server.listen(RECEIVER_PORT, '127.0.0.1', () => resolve(server)))
}

const got = (event, predicate = () => true) =>
  receiver.received.filter((r) => r.event === event && !r.failed && predicate(r.payload))

// ---- 浏览器 ----

function watchErrors(page, label) {
  page.on('console', (m) => {
    if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) {
      summary.consoleErrors.push(`${label}: ${m.text()}`)
    }
  })
  page.on('pageerror', (e) => summary.consoleErrors.push(`${label} pageerror: ${e.message}`))
}

const pages = []

async function newPage(browser, label, viewport = { width: 1440, height: 900 }) {
  const context = await browser.newContext({ viewport, locale: 'zh-CN' })
  const page = await context.newPage()
  watchErrors(page, label)
  pages.push([label, page])
  return page
}

async function shot(page, name) {
  await page.waitForTimeout(600)
  await page.screenshot({ path: `${SHOTS}/${name}.png` })
}

async function seen(locator, timeout = 20000) {
  return locator
    .first()
    .waitFor({ timeout })
    .then(() => true)
    .catch(() => false)
}

async function consoleLogin(browser, username) {
  const page = await newPage(browser, username)
  await page.goto(`${CONSOLE}/login`)
  await page.fill('input[placeholder="例如 demo"]', TENANT)
  await page.fill('input[autocomplete="username"]', username)
  await page.fill('input[autocomplete="current-password"]', PASSWORD)
  await page.click('button:has-text("登录")')
  await page.locator('[data-testid="main-menu"]').waitFor()
  return page
}

async function menu(page, title) {
  await page.locator('[data-testid="main-menu"] .el-menu-item', { hasText: title }).click()
}

const toast = (page, text) =>
  page.locator('.el-message--success', { hasText: text }).first().waitFor({ timeout: 15000 })

async function prepareTenant() {
  const platform = await json(`${API}/platform/v1/auth/login`, {
    method: 'POST',
    body: { username: PLATFORM_USER, password: PLATFORM_PASSWORD },
  })
  const tenant = await json(`${API}/platform/v1/tenants`, {
    method: 'POST',
    token: platform.access_token,
    body: {
      code: TENANT,
      name: `对接验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  return { tenantId: tenant.id }
}

// ---- 1. 接口密钥与推送地址 ----

async function settingsSection(browser, ctx) {
  const page = await consoleLogin(browser, 'admin')
  await menu(page, '设置')
  await page.click('#tab-integration')
  await page.locator('[data-testid="integration-tab"]').waitFor()

  await page.click('[data-testid="api-key-new"]')
  const keyDialog = page.locator('[data-testid="api-key-dialog"]')
  await keyDialog.locator('input[data-testid="api-key-name"]').fill('ERP 对接')
  for (const label of ['同步商品和价格', '查询订单（含收货信息）', '创建订单，回传状态、物流和收款', '创建待办']) {
    await keyDialog.locator('[data-testid="api-key-scopes"] .el-checkbox', { hasText: label }).click()
  }
  await shot(page, '1-api-key-dialog')
  await keyDialog.locator('[data-testid="api-key-save"]').click()
  const secretDialog = page.locator('[data-testid="secret-dialog"]')
  await secretDialog.locator('[data-testid="secret-value"]').waitFor()
  ctx.key = (await secretDialog.locator('[data-testid="secret-value"]').innerText()).trim()
  await shot(page, '1-api-key-created')
  await secretDialog.locator('[data-testid="secret-done"]').click()
  const keyRow = page.locator('[data-testid="api-keys"] .el-table__row', { hasText: 'ERP 对接' })
  const rowText = await keyRow.innerText()
  const keys = await json(`${API}/open/v1/orders`, { token: ctx.key })
  check(
    '创建接口密钥：完整密钥只显示一次，列表里只显示前缀；可以调用开放接口',
    ctx.key.startsWith('edp_') && !rowText.includes(ctx.key) && rowText.includes('_••••') && Array.isArray(keys.items),
    rowText,
  )

  await page.click('[data-testid="webhook-new"]')
  const hookDialog = page.locator('[data-testid="webhook-dialog"]')
  await hookDialog.locator('input[data-testid="webhook-name"]').fill('ERP')
  await hookDialog.locator('input[data-testid="webhook-url"]').fill(HOOK)
  await hookDialog.locator('[data-testid="webhook-events"] .el-checkbox', { hasText: '待办完成' }).click()
  await shot(page, '1-webhook-dialog')
  await hookDialog.locator('[data-testid="webhook-save"]').click()
  await secretDialog.locator('[data-testid="secret-value"]').waitFor()
  receiver.secret = (await secretDialog.locator('[data-testid="secret-value"]').innerText()).trim()
  await secretDialog.locator('[data-testid="secret-done"]').click()

  await page.locator('[data-testid="webhooks"] [data-testid="webhook-test"]').first().click()
  await toast(page, '推送成功')
  const ping = got('ping')[0]
  check(
    '添加推送地址（签名密钥只显示一次），测试推送送达且签名正确',
    receiver.secret.startsWith('whsec_') && ping?.valid === true && ping.payload.event === 'ping',
    ping,
  )
  await shot(page, '1-integration')
  return page
}

// ---- 2. 企业系统同步商品、创建订单 ----

async function orderSection(browser, ctx, admin) {
  await json(`${API}/open/v1/products/LOCK-X1`, {
    method: 'PUT',
    token: ctx.key,
    body: { name: '智能门锁 X1', spec: '黑色', retail_price: '1299', cost_price: '800' },
  })
  const created = await json(`${API}/open/v1/orders`, {
    method: 'POST',
    token: ctx.key,
    body: {
      customer: { name: '赵六', phone: '13900001111' },
      items: [{ code: 'LOCK-X1', quantity: 2 }],
      receiver: { name: '赵六', phone: '13900001111', address: '杭州市西湖区文三路 1 号' },
      external_no: ERP_NO,
    },
  })
  ctx.order = created.order
  const again = await request(`${API}/open/v1/orders`, {
    method: 'POST',
    token: ctx.key,
    body: {
      customer: { name: '赵六', phone: '13900001111' },
      items: [{ code: 'LOCK-X1', quantity: 2 }],
      external_no: ERP_NO,
    },
  })
  await cli('webhook-jobs')
  const pushed = await waitFor(async () => got('order.created')[0])
  check(
    '企业系统创建订单：进入平台审核；同一个企业系统单号重复创建返回原订单；推送 order.created（签名正确）',
    ctx.order.status === 'pending_review' &&
      again.status === 200 &&
      again.body.order.id === ctx.order.id &&
      pushed?.valid === true &&
      pushed.payload.data.order.external_no === ERP_NO,
    { status: ctx.order.status, again: again.status, pushed },
  )

  await menu(admin, '订单')
  const row = admin.locator('[data-testid="orders-table"] .el-table__row', { hasText: ctx.order.no })
  await row.waitFor({ timeout: 15000 })
  await row.locator('td').nth(2).click()
  const drawer = admin.locator('[data-testid="order-drawer"]')
  await drawer.locator('[data-testid="order-status"]').waitFor()
  const text = await drawer.innerText()
  check(
    '订单中心：来源"企业系统"，显示企业系统单号，创建人为企业系统',
    text.includes('企业系统') && text.includes(ERP_NO) && text.includes('待审核'),
    text.slice(0, 400),
  )
  await shot(admin, '2-order-from-erp')
  await admin.keyboard.press('Escape')
}

// ---- 3. 回传发货 ----

async function statusSection(browser, ctx, admin) {
  const shipped = await json(`${API}/open/v1/orders/${ERP_NO}/status`, {
    method: 'POST',
    token: ctx.key,
    body: {
      status: 'shipped',
      payment_method: 'cod',
      shipping_company: '中通快递',
      tracking_no: 'ZT998877',
      notify_customer: false,
    },
  })
  await cli('webhook-jobs')
  const statuses = await waitFor(async () => {
    const confirmed = got('order.confirmed')
    const changed = got('order.status_changed')
    return confirmed.length && changed.length ? { confirmed, changed } : null
  })
  check(
    '回传发货（跳过确认）：订单为已发货；推送 order.confirmed 与 order.status_changed',
    shipped.order.status === 'shipped' &&
      !!shipped.order.confirmed_at &&
      statuses?.changed[0].payload.data.order.tracking_no === 'ZT998877' &&
      statuses.changed.every((r) => r.valid),
    shipped.order.status,
  )

  await menu(admin, '订单')
  const row = admin.locator('[data-testid="orders-table"] .el-table__row', { hasText: ctx.order.no })
  await row.locator('td').nth(2).click()
  const drawer = admin.locator('[data-testid="order-drawer"]')
  const status = await seen(drawer.locator('[data-testid="order-status"]', { hasText: '已发货' }))
  const shipping = await drawer.locator('[data-testid="order-shipping"]').innerText()
  await drawer.locator('[data-testid="order-revisions-open"]').click()
  const revisions = admin.locator('[data-testid="history-drawer"]')
  await revisions.locator('[data-testid="history-version"]').first().waitFor()
  const heads = await revisions.locator('[data-testid="history-version"]').allInnerTexts()
  check(
    '订单详情：已发货和物流；修改历史的操作人是"企业系统"',
    status && shipping.includes('中通快递 ZT998877') && heads.every((h) => h.includes('企业系统')),
    { shipping, heads },
  )
  await shot(admin, '3-revisions')
  await revisions.locator('.el-drawer__close-btn').click()
  await admin.keyboard.press('Escape')

  const tracking = await newPage(browser, 'tracking', { width: 390, height: 844 })
  await tracking.goto(shipped.order.tracking_url)
  const shownShipping = await seen(
    tracking.locator('[data-testid="tracking-shipping"]', { hasText: '中通快递 ZT998877' }),
  )
  check('跟踪页看到企业系统回传的物流', shownShipping)
  await shot(tracking, '3-tracking')
}

// ---- 4. 推送失败、重试与重发 ----

async function failureSection(browser, ctx, admin) {
  receiver.fail = true
  await json(`${API}/open/v1/orders/${ERP_NO}/status`, {
    method: 'POST',
    token: ctx.key,
    body: {
      status: 'completed',
      payments: [{ amount: '2598.00', channel: 'cash', reference_no: `PAY-${RUN}` }],
      notify_customer: false,
    },
  })
  await cli('webhook-jobs')
  await menu(admin, '设置')
  await admin.click('#tab-integration')
  await admin.locator('[data-testid="integration-tab"]').waitFor()
  const retrying = await seen(
    admin.locator('[data-testid="deliveries"] [data-testid="delivery-state"]', { hasText: '重试中' }),
  )
  // 接收方还没恢复时测试推送：失败的测试推送不重试，可以手工重发。
  await admin.locator('[data-testid="webhooks"] [data-testid="webhook-test"]').first().click()
  const failedPing = await seen(admin.locator('.el-message--error', { hasText: '推送失败' }))
  await shot(admin, '4-retrying')

  // 运营后台"运维 → 企业系统推送"：接收方仍返回 500 时看到这个租户重试中的推送；恢复后在这里重发。
  const platform = await newPage(browser, 'platform')
  await platform.goto(`${PLATFORM}/login`)
  await platform.fill('input[autocomplete="username"]', PLATFORM_USER)
  await platform.fill('input[autocomplete="current-password"]', PLATFORM_PASSWORD)
  await platform.click('button:has-text("登录")')
  await platform.locator('[data-testid="platform-menu"]').waitFor()
  await platform.locator('[data-testid="menu-ops"]').click()
  await platform.locator('[data-testid="ops-view"]').waitFor()
  await platform.click('#tab-pushes')
  await platform.locator('[data-testid="push-status"] .el-radio-button', { hasText: '重试中' }).click()
  const pushRow = platform
    .locator('[data-testid="push-table"] .el-table__row', { hasText: TENANT })
    .filter({ hasText: 'order.status_changed' })
  const listed = await seen(pushRow)
  const pushText = listed ? await pushRow.first().innerText() : ''
  await shot(platform, '4-platform-pushes')
  receiver.fail = false
  const beforeCompleted = got('order.status_changed', (p) => p.data.order.status === 'completed').length
  if (listed) {
    await pushRow.first().locator('.el-checkbox').click()
    await platform.click('[data-testid="push-resend"]')
    await toast(platform, '已安排重发')
  }
  await cli('webhook-jobs')
  const completed = await waitFor(async () => {
    const hits = got('order.status_changed', (p) => p.data.order.status === 'completed')
    return hits.length > beforeCompleted ? hits.at(-1) : null
  })
  check(
    '运营后台"运维 → 企业系统推送"看到这个租户重试中的推送（次数、错误）；接收方恢复后重发送达',
    listed && pushText.includes('HTTP 500') && completed?.valid === true,
    { pushText, completed: !!completed },
  )

  const dead = admin
    .locator('[data-testid="deliveries"] .el-table__row', { hasText: '测试推送' })
    .filter({ has: admin.locator('[data-testid="delivery-state"]', { hasText: '失败' }) })
  await dead.first().waitFor({ timeout: 15000 })
  await dead.first().locator('[data-testid="delivery-view"]').click()
  const body = await admin.locator('[data-testid="delivery-body"]').innerText()
  await admin.keyboard.press('Escape')
  const before = got('ping').length
  await dead.first().locator('[data-testid="delivery-resend"]').click()
  await toast(admin, '已安排重发')
  await cli('webhook-jobs')
  const resent = await waitFor(async () => (got('ping').length > before ? got('ping').at(-1) : null))
  check(
    '推送失败：接收方返回 500 时进入重试；失败的测试推送可以查看内容并重发，恢复后送达',
    retrying && failedPing && body.includes('"event": "ping"') && resent?.valid === true,
    { retrying, failedPing, resent: !!resent },
  )
}

// ---- 5. 企业系统创建待办 ----

async function todoSection(ctx, admin) {
  const todo = await json(`${API}/open/v1/todos`, {
    method: 'POST',
    token: ctx.key,
    body: {
      type: 'callback',
      title: `回访安装效果 ${RUN}`,
      order_no: ERP_NO,
      external_ref: CRM_REF,
    },
  })
  await menu(admin, '待办')
  await admin.click('[data-testid="todo-view-all"]')
  const row = admin.locator('[data-testid="todos-table"] .el-table__row', { hasText: todo.title })
  await row.waitFor({ timeout: 15000 })
  await row.locator('td').nth(2).click()
  const box = admin.locator('[data-testid="todo-drawer"]')
  await box.locator('[data-testid="todo-status"]').waitFor()
  const text = await box.innerText()
  const listed = todo.status === 'open' && text.includes('企业系统')
  if (await box.locator('[data-testid="todo-start"]').count()) {
    await box.locator('[data-testid="todo-start"]').click()
    await seen(box.locator('[data-testid="todo-status"]', { hasText: '处理中' }))
  }
  await box.locator('[data-testid="todo-done"]').click()
  await admin.locator('textarea[data-testid="done-result"]').fill('已电话回访，客户满意')
  const notify = admin.locator('[data-testid="done-notify"] input')
  if (await notify.isChecked()) await admin.locator('[data-testid="done-notify"]').click()
  await admin.click('[data-testid="done-submit"]')
  await toast(admin, '已完成')
  await admin.keyboard.press('Escape')
  await cli('webhook-jobs')
  const done = await waitFor(async () => got('todo.done', (p) => p.data.todo.external_ref === CRM_REF)[0])
  check(
    '企业系统创建的待办直接进入待办列表（来源"企业系统"）；完成后推送 todo.done，带回企业系统的单号',
    listed && done?.valid === true && done.payload.data.todo.result === '已电话回访，客户满意',
    { status: todo.status, done: done?.payload?.data },
  )
  await shot(admin, '5-todo')
}

// ---- 6. 报表与运营后台 ----

async function reportSection(admin) {
  await menu(admin, '报表')
  await admin.locator('[data-testid="report-tabs"]').waitFor()
  await admin.click('#tab-orders')
  const orders = admin.locator('[data-testid="order-tile-orders"]')
  await orders.waitFor({ timeout: 15000 })
  const ordersText = await orders.innerText()
  await shot(admin, '6-order-report')
  await admin.click('#tab-todos')
  const created = admin.locator('[data-testid="todo-tile-created"]')
  await created.waitFor({ timeout: 15000 })
  const createdText = await created.innerText()
  await shot(admin, '6-todo-report')
  check(
    '报表：订单页签（订单数、金额）和待办页签（新建待办按来源）',
    ordersText.includes('1') && ordersText.includes('¥2,598.00') && createdText.includes('企业系统'),
    { ordersText, createdText },
  )
}

// ---- 7. 撤销密钥 ----

async function revokeSection(ctx, admin) {
  await menu(admin, '设置')
  await admin.click('#tab-integration')
  await admin.locator('[data-testid="api-keys"] [data-testid="api-key-revoke"]').first().click()
  const box = admin.locator('.el-message-box:visible')
  await box.waitFor()
  await box.locator('.el-message-box__btns button', { hasText: '撤销' }).click()
  await toast(admin, '已撤销')
  const after = await request(`${API}/open/v1/orders`, { token: ctx.key })
  check('撤销接口密钥后立即不能调用开放接口', after.status === 401, after)
  await shot(admin, '7-revoked')
}

async function run(browser) {
  const ctx = await prepareTenant()
  const admin = await settingsSection(browser, ctx)
  await orderSection(browser, ctx, admin)
  await statusSection(browser, ctx, admin)
  await failureSection(browser, ctx, admin)
  await todoSection(ctx, admin)
  await reportSection(admin)
  await revokeSection(ctx, admin)
  check('每条推送的签名都正确', receiver.received.every((r) => r.valid), receiver.received.length)
  check('没有前端脚本错误', summary.consoleErrors.length === 0, summary.consoleErrors)
}

;(async () => {
  if (!PLATFORM_PASSWORD) {
    console.error('请通过环境变量 PLATFORM_PASSWORD 提供平台运营账号的密码')
    process.exit(2)
  }
  fs.mkdirSync(SHOTS, { recursive: true })
  const server = await startReceiver()
  const browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_PATH || undefined,
    args: ['--no-sandbox'],
  })
  try {
    await run(browser)
  } catch (error) {
    for (const [label, page] of pages) {
      await page.screenshot({ path: `${SHOTS}/failed-${label}.png` }).catch(() => null)
    }
    throw error
  } finally {
    await browser.close()
    server.close()
    summary.received = receiver.received.map((r) => `${r.event}${r.failed ? '（返回 500）' : ''}${r.valid ? '' : '（签名不对）'}`)
    fs.writeFileSync(`${SHOTS}/summary.json`, JSON.stringify(summary, null, 2))
    console.log(JSON.stringify(summary, null, 2))
  }
  process.exit(summary.checks.some((c) => c.startsWith('FAIL')) ? 1 : 0)
})().catch((error) => {
  console.error(error)
  process.exit(1)
})
