// P24 验收：意向客户（设计文档 §35）——员工转入、跟进、AI 写跟进话术、访客咨询后 AI 转入、客户又来咨询、
// 订单确认后自动成交、只建议时确认 AI 的建议、"意向客户该跟进了"的提醒、放弃和重新跟进、坐席的查看范围，
// 在浏览器里走通。
//
// 1. 准备：知识"智能门锁国庆活动"、商品、客户"王先生"（归管理员）；坐席小艾；平台的"意图判断"路由到
//    判断模型（接到模拟服务，结束时删除）。
// 2. 管理员在客户资料里把王先生转入意向客户（意向高），客户列表标"意向"；"意向客户"页签的数字和列表；
//    意向详情里 AI 写跟进话术（参考知识库）、记一次跟进、把下次跟进改成今天。
// 3. 访客咨询智能门锁（意向明确、嫌贵、下周再说），小艾接待后结束会话：AI 转入意向客户（来源 AI，想要
//    什么、顾虑、7 天后跟进，跟进人小艾），依据的会话可以点开；访客又来说要下单：记"客户又来咨询了"、
//    等级调高；这个客户的订单确认后自动成交，"本月成交"加一。
// 4. 意向客户设置改成"只建议"：另一位访客咨询后进"待确认"，管理员确认转入。
// 5. AI 唤醒的每日巡检提醒"管理员 有 1 条商机该跟进了"，提醒的链接打开今天该跟进的列表。
// 6. 放弃（写原因）和重新跟进。
// 7. 小艾只看到自己跟进的意向客户（客户不归她也能看到），没有意向客户设置。
//
// 前置：与 p22-wake-acceptance.cjs 相同（后端、实时消费进程、调度进程接到模拟大模型 :8900），另需访客
// Widget（:5175）。运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<密码> node scripts/e2e/p24-prospect-acceptance.cjs
const { chromium } = require('playwright')
const { execFile } = require('child_process')
const { promisify } = require('util')
const fs = require('fs')
const path = require('path')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const WIDGET = env('WIDGET_URL', 'http://localhost:5175')
const FAKE_LLM = env('FAKE_LLM_URL', 'http://127.0.0.1:8900/v1')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p24')
const BACKEND_DIR = env('BACKEND_DIR', path.resolve(__dirname, '../../backend'))
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `prospect-${RUN}`
const PASSWORD = 'demo-pass-2026'
const ZONE = 'Asia/Shanghai'
const FIRST_ASK = '你好，智能门锁有货吗？多少钱？有点贵，我再考虑一下，下周再说'
const RETURN_ASK = '我要下单，地址是上海市浦东新区张江路 1 号'

const summary = { tenant: TENANT, checks: [], consoleErrors: [] }
const check = (name, ok, detail) =>
  summary.checks.push(
    `${ok ? 'PASS' : 'FAIL'} ${name}${ok || detail === undefined ? '' : ` -> ${JSON.stringify(detail)}`}`,
  )
const state = { contexts: [] }

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
  const data = await json(`${API}/api/v1/auth/login`, {
    method: 'POST',
    body: { tenant_code: TENANT, username, password: PASSWORD },
  })
  return data.access_token
}

const run = promisify(execFile)

/** 后端命令行（见 p22-wake-acceptance.cjs）。 */
async function cli(...args) {
  const { stdout } = await run('uv', ['run', 'python', '-m', 'app.cli', ...args], {
    cwd: BACKEND_DIR,
    env: process.env,
    encoding: 'utf-8',
    maxBuffer: 16 * 1024 * 1024,
  })
  const lines = stdout.trim().split('\n')
  return JSON.parse(lines[lines.length - 1])
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function until(label, probe, timeoutMs = 90_000) {
  const deadline = Date.now() + timeoutMs
  let last
  while (Date.now() < deadline) {
    last = await probe().catch(() => null)
    if (last) return last
    await sleep(1000)
  }
  throw new Error(`timed out waiting for ${label}`)
}

/** 企业时区（上海）的日期，YYYY-MM-DD。 */
function shanghaiDate(offsetDays = 0) {
  const day = new Date(Date.now() + offsetDays * 86_400_000)
  return new Intl.DateTimeFormat('en-CA', { timeZone: ZONE }).format(day)
}

async function prepare() {
  const platform = await json(`${API}/platform/v1/auth/login`, {
    method: 'POST',
    body: { username: PLATFORM_USER, password: PLATFORM_PASSWORD },
  })
  state.ops = platform.access_token
  await json(`${API}/platform/v1/tenants`, {
    method: 'POST',
    token: state.ops,
    body: {
      code: TENANT,
      name: `意向客户验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  // 意图判断是平台级的路由：接到模拟的判断模型，结束时删除供应商（路由一并去掉）。
  const provider = await json(`${API}/platform/v1/llm-providers`, {
    method: 'POST',
    token: state.ops,
    body: {
      name: `Jev-${RUN}`,
      protocol: 'typesafe',
      base_url: FAKE_LLM,
      api_key: 'sk-jev-e2e',
      chat_model: 'jev-1.13.0',
    },
  })
  state.providerId = provider.id
  const routes = await json(`${API}/platform/v1/settings/llm-routes`, { token: state.ops })
  await json(`${API}/platform/v1/settings/llm-routes`, {
    method: 'PUT',
    token: state.ops,
    body: { routes: { ...routes.routes, intent: provider.id } },
  })

  const admin = await login('admin')
  state.admin = admin
  state.adminId = (await json(`${API}/api/v1/me`, { token: admin })).id
  await json(`${API}/api/v1/staff`, {
    method: 'POST',
    token: admin,
    body: { username: 'alice', display_name: '小艾', password: PASSWORD, role_codes: ['agent'] },
  })
  await json(`${API}/api/v1/kb/items`, {
    method: 'POST',
    token: admin,
    body: {
      kind: 'faq',
      title: '智能门锁国庆活动',
      content: '国庆期间智能门锁 9 折。活动到 10 月 7 日。',
      publish: true,
    },
  })
  await until('the promotion to be searchable', async () => {
    const found = await json(`${API}/api/v1/kb/search?q=${encodeURIComponent('智能门锁')}`, { token: admin })
    return (found.items ?? found.hits ?? found).length > 0
  })
  const product = await json(`${API}/api/v1/products`, {
    method: 'POST',
    token: admin,
    body: { code: 'LOCK-X1', name: '智能门锁 X1', retail_price: '1299', cost_price: '800' },
  })
  state.productId = product.id
  const wang = await json(`${API}/api/v1/customers`, {
    method: 'POST',
    token: admin,
    body: { display_name: '王先生', company: '浦东物业', phone: '13800002222' },
  })
  state.wangId = wang.id
  const channels = await json(`${API}/api/v1/channels`, { token: admin })
  state.channelKey = channels.items[0].public_key
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
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    locale: 'zh-CN',
    timezoneId: ZONE,
    permissions: ['clipboard-read', 'clipboard-write'],
  })
  state.contexts.push(ctx)
  const page = await ctx.newPage()
  watchErrors(page, username)
  await page.goto(`${CONSOLE}/login`)
  await page.fill('input[placeholder="例如 demo"]', TENANT)
  await page.fill('input[autocomplete="username"]', username)
  await page.fill('input[autocomplete="current-password"]', PASSWORD)
  await page.click('button:has-text("登录")')
  await page.waitForSelector('[data-testid="main-menu"]')
  return page
}

async function openVisitor(browser) {
  const ctx = await browser.newContext({ viewport: { width: 420, height: 720 }, locale: 'zh-CN' })
  state.contexts.push(ctx)
  const page = await ctx.newPage()
  watchErrors(page, 'visitor')
  await page.goto(`${WIDGET}/?key=${encodeURIComponent(state.channelKey)}`)
  await page.locator('[data-testid="widget-state"]', { hasText: '在线' }).waitFor({ timeout: 15000 })
  return page
}

async function say(visitor, text) {
  await visitor.fill('[data-testid="message-input"]', text)
  await visitor.click('[data-testid="send-button"]')
}

async function success(page, text, timeout = 15_000) {
  await page.locator('.el-message--success', { hasText: text }).last().waitFor({ timeout })
}

/** 截图前等弹窗、抽屉的动画结束。 */
const settle = (page) => page.waitForTimeout(600)
const text = (locator) => locator.innerText().then((t) => t.trim())

/** el-input 的 textarea（data-testid 可能在 textarea 上，也可能在外层）。 */
const area = (scope, testid) =>
  scope.locator(`textarea[data-testid="${testid}"], [data-testid="${testid}"] textarea`).first()

async function closeDrawer(page) {
  await page.locator('.el-drawer__close-btn:visible').last().click()
  await settle(page)
}

/** 关闭指定的抽屉（意向详情里打开的会话记录叠在上面，关闭按钮在同一个位置）。 */
async function closeNamed(page, testid) {
  const drawer = page.locator(`.el-drawer[data-testid="${testid}"], .el-drawer:has([data-testid="${testid}"])`).first()
  await drawer.locator('.el-drawer__close-btn').click()
  await drawer.waitFor({ state: 'hidden' })
}

async function prospects(page, view) {
  await page.goto(`${CONSOLE}/customers?tab=prospects${view ? `&view=${view}` : ''}`)
  await page.waitForSelector('[data-testid="prospect-table"]')
  await page.waitForFunction(() => !document.querySelector('[data-testid="prospect-table"] .el-loading-mask'))
}

async function rowOf(page, name) {
  const row = page.locator('[data-testid="prospect-table"] .el-table__row', { hasText: name }).first()
  await row.waitFor()
  return row
}

async function openProspect(page, name) {
  await (await rowOf(page, name)).click()
  const drawer = page.locator('[data-testid="prospect-drawer"]')
  await drawer.locator('[data-testid="prospect-status"]').waitFor()
  return drawer
}

async function tile(page, name) {
  return Number((await text(page.locator(`[data-testid="prospect-tile-${name}"] .value`))) || 0)
}

// ---- 2. 员工转入、跟进、AI 写跟进话术 ----

async function staffConverts(page) {
  await page.goto(`${CONSOLE}/customers`)
  await page.waitForSelector('[data-testid="customer-table"]')
  await page.locator('[data-testid="customer-table"] .el-table__row', { hasText: '王先生' }).getByRole('button', { name: '王先生' }).click()
  const panel = page.locator('[data-testid="customer-prospect"]')
  await panel.locator('[data-testid="customer-prospect-create"]').click()
  const dialog = page.locator('[data-testid="prospect-create"]')
  await dialog.waitFor()
  const fixed = await text(dialog.locator('[data-testid="prospect-create-customer"]'))
  await dialog.locator('[data-testid="prospect-level"] label', { hasText: '高' }).click()
  await area(dialog, 'prospect-interest').fill('智能门锁 X1，小区 20 户统一换锁')
  await area(dialog, 'prospect-concerns').fill('觉得价格偏高，想等活动')
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/p24-01-convert.png` })
  await dialog.locator('[data-testid="prospect-create-save"]').click()
  await success(page, '已转入意向客户')
  const status = await text(panel.locator('[data-testid="customer-prospect-status"]'))
  check('the customer profile converts 王先生 into a prospect', fixed === '王先生' && status === '跟进中', {
    fixed,
    status,
  })
  // 客户资料里打开意向详情。
  await panel.locator('[data-testid="customer-prospect-open"]').click()
  const fromPanel = page.locator('[data-testid="prospect-drawer"]')
  await fromPanel.locator('[data-testid="prospect-status"]', { hasText: '跟进中' }).waitFor()
  check('the profile opens the prospect details', (await text(fromPanel)).includes('员工转入'))
  await closeNamed(page, 'prospect-drawer')
  await closeDrawer(page)
  const tag = page.locator(`[data-testid="customer-prospect-tag-${state.wangId}"]`)
  await tag.waitFor()
  check('the customer list tags 王先生 as 意向', (await text(tag)) === '意向')
  await page.screenshot({ path: `${SHOTS}/p24-02-customer-tag.png` })

  await page.locator('[data-testid="customer-tabs"] .el-tabs__item', { hasText: '意向客户' }).click()
  await page.waitForURL(/tab=prospects/)
  await page.waitForSelector('[data-testid="prospect-table"]')
  const row = await rowOf(page, '王先生')
  const rowText = await text(row)
  check(
    'the prospects tab lists 王先生 with level, wants, concerns and the follow-up in 3 days',
    (await tile(page, 'active')) === 1 &&
      rowText.includes('高') &&
      rowText.includes('小区 20 户') &&
      rowText.includes('价格偏高') &&
      rowText.includes('3 天后') &&
      rowText.includes(shanghaiDate(3)),
    rowText,
  )

  const drawer = await openProspect(page, '王先生')
  await drawer.locator('[data-testid="prospect-write-message"]').click()
  const message = area(drawer, 'prospect-message')
  await message.waitFor()
  const written = await message.inputValue()
  const refs = await text(drawer.locator('[data-testid="prospect-message-refs"]'))
  check(
    'AI writes a follow-up message from the knowledge base',
    written.startsWith('王先生您好') && written.includes('国庆期间智能门锁 9 折') && refs.includes('智能门锁国庆活动'),
    { written, refs },
  )
  await drawer.locator('[data-testid="prospect-copy-message"]').click()
  const copied = page.locator('.el-message', { hasText: '复制' }).last()
  await copied.waitFor()
  check('the message can be copied to send by hand', (await copied.getAttribute('class')).includes('success'))

  const form = drawer.locator('[data-testid="prospect-follow-form"]')
  await form.locator('label', { hasText: '微信' }).click()
  await area(form, 'prospect-follow-content').fill('微信发了活动介绍，客户说下周和业委会商量')
  await form.locator('[data-testid="prospect-follow-save"]').click()
  await success(page, '已记一次跟进')
  const timeline = drawer.locator('[data-testid="prospect-activities"]')
  await timeline.locator('text=业委会').waitFor()
  const entry = await text(timeline)
  check('a follow-up is recorded with its method and author', entry.includes('微信') && entry.includes('管理员'), entry)

  // 下次跟进改成今天（之后 AI 唤醒提醒"该跟进了"）。
  const next = drawer.locator('.el-form-item', { hasText: '下次跟进' }).locator('input').first()
  await next.fill(shanghaiDate(0))
  await next.press('Enter')
  await drawer.locator('[data-testid="prospect-save"]').click()
  await success(page, '已保存')
  const due = await text(drawer.locator('[data-testid="prospect-due"]'))
  check('moving the next follow-up to today shows 今天', due === '今天', due)
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/p24-03-drawer.png` })
  await closeDrawer(page)
}

// ---- 3. 访客咨询后 AI 转入、又来咨询、订单确认后成交 ----

async function serveAndClose(alice, visitor, ask, stage) {
  await say(visitor, ask)
  const item = alice.locator('[data-testid="session-item"]').first()
  await item.waitFor({ timeout: 20_000 })
  await item.click()
  const card = alice.locator('[data-testid="intent-card"]')
  await card.locator('[data-testid="intent-stage"]', { hasText: stage }).waitFor({ timeout: 30_000 })
  await alice.click('[data-testid="close-session"]')
  await alice.locator('.el-message-box button', { hasText: '结束' }).click()
  await visitor.locator('[data-testid="message"].system', { hasText: '本次会话已结束' }).waitFor({ timeout: 15_000 })
  await alice.locator('[data-testid="session-item"]').first().waitFor({ state: 'detached', timeout: 15_000 })
}

async function aiConverts(browser, admin, alice) {
  await alice.locator('[data-testid="main-menu"] .el-menu-item', { hasText: /^\s*工作台/ }).click()
  await alice.locator('[data-testid="agent-status"]', { hasText: '在线' }).waitFor({ timeout: 15_000 })
  const visitor = await openVisitor(browser)
  await serveAndClose(alice, visitor, FIRST_ASK, '意向明确')
  await cli('opportunities-scan', TENANT)
  const page = await until('the AI prospect', async () => {
    const list = await json(`${API}/api/v1/opportunities?view=active`, { token: state.admin })
    return list.items.find((p) => p.source === 'ai') ?? null
  })
  state.aiCustomerId = page.customer_id
  state.aiName = page.customer_name

  await prospects(admin)
  const row = await rowOf(admin, state.aiName)
  const rowText = await text(row)
  check(
    'AI converts the closed high-intent session: wants, concerns, 7 days, follower 小艾',
    rowText.includes('AI') &&
      rowText.includes('智能门锁有货吗') &&
      rowText.includes('价格、还要考虑') &&
      rowText.includes('7 天后') &&
      rowText.includes('小艾') &&
      rowText.includes('中'),
    rowText,
  )
  const drawer = await openProspect(admin, state.aiName)
  await drawer.locator('[data-testid="prospect-session"]').click()
  const session = admin.locator('[data-testid="session-drawer"]')
  await session.locator('text=智能门锁有货吗').first().waitFor()
  check('the source session opens from the prospect', true)
  await settle(admin)
  await admin.screenshot({ path: `${SHOTS}/p24-04-ai-session.png` })
  await closeNamed(admin, 'session-drawer')
  await closeNamed(admin, 'prospect-drawer')

  // 客户又来咨询了：说要下单——系统记一条跟进，等级调高。
  await serveAndClose(alice, visitor, RETURN_ASK, '准备下单')
  await cli('opportunities-scan', TENANT)
  await until('the return visit follow-up', async () => {
    const list = await json(`${API}/api/v1/opportunities?view=active&customer_id=${state.aiCustomerId}`, {
      token: state.admin,
    })
    const item = list.items[0]
    return item && item.level === 'high' ? item : null
  })
  await prospects(admin)
  const again = await openProspect(admin, state.aiName)
  const timeline = await text(again.locator('[data-testid="prospect-activities"]'))
  check(
    'a return visit is recorded by the system and the level goes up to 高',
    timeline.includes('客户又来咨询了（准备下单）') && timeline.includes('系统') && (await text(again)).includes('意向高'),
    timeline,
  )
  await settle(admin)
  await admin.screenshot({ path: `${SHOTS}/p24-05-return.png` })
  await closeDrawer(admin)

  // 这个客户的订单确认后自动成交。
  const order = await json(`${API}/api/v1/orders`, {
    method: 'POST',
    token: state.admin,
    body: {
      customer_id: state.aiCustomerId,
      items: [{ product_id: state.productId, quantity: 2 }],
      receiver: { name: '李先生', phone: '13900003333', address: '上海市浦东新区张江路 1 号' },
    },
  })
  await json(`${API}/api/v1/orders/${order.id}/confirm`, {
    method: 'POST',
    token: state.admin,
    body: { payment_method: 'cod', notify_customer: false },
  })
  await prospects(admin, 'won')
  const won = await openProspect(admin, state.aiName)
  const wonText = await text(won)
  check(
    'confirming the order marks the prospect won with the order',
    wonText.includes('已成交') && wonText.includes(order.no) && (await tile(admin, 'won')) === 1,
    wonText,
  )
  await settle(admin)
  await admin.screenshot({ path: `${SHOTS}/p24-06-won.png` })
  await closeDrawer(admin)
}

// ---- 4. 只建议：确认 AI 的建议 ----

async function suggestion(browser, admin, alice) {
  await prospects(admin)
  await admin.click('[data-testid="prospect-settings-open"]')
  const dialog = admin.locator('[data-testid="prospect-settings"]')
  await dialog.locator('[data-testid="prospect-ai-mode"] label', { hasText: '只建议' }).click()
  await dialog.locator('[data-testid="prospect-settings-save"]').click()
  await success(admin, '已保存意向客户设置')
  const saved = await json(`${API}/api/v1/opportunities/settings`, { token: state.admin })
  check('settings switch AI to suggest only', saved.settings.ai_mode === 'suggest', saved)

  const visitor = await openVisitor(browser)
  await serveAndClose(alice, visitor, '可视门铃有货吗？什么时候能发货', '意向明确')
  await cli('opportunities-scan', TENANT)
  await until('the AI suggestion', async () => {
    const list = await json(`${API}/api/v1/opportunities?view=suggested`, { token: state.admin })
    return list.items[0] ?? null
  })
  await prospects(admin, 'suggested')
  check('the suggestion waits in 待确认', (await tile(admin, 'suggested')) === 1)
  const row = admin.locator('[data-testid="prospect-table"] .el-table__row').first()
  await row.click()
  const drawer = admin.locator('[data-testid="prospect-drawer"]')
  await drawer.locator('[data-testid="prospect-suggestion"]').waitFor()
  await settle(admin)
  await admin.screenshot({ path: `${SHOTS}/p24-07-suggestion.png` })
  await drawer.locator('[data-testid="prospect-accept"]').click()
  await success(admin, '已转入意向客户')
  const status = await text(drawer.locator('[data-testid="prospect-status"]'))
  check('accepting the suggestion starts following up', status === '跟进中', status)
  await closeDrawer(admin)
}

// ---- 5. AI 唤醒：意向客户该跟进了 ----

async function wakeReminder(admin) {
  await until(
    'the scheduled wake-ups of the new tenant',
    async () => {
      const runs = await json(`${API}/api/v1/wake/runs`, { token: state.admin })
      const scheduled = runs.items.filter((r) => r.trigger === 'schedule')
      return scheduled.length > 0 && scheduled.every((r) => !['queued', 'running'].includes(r.status))
    },
    180_000,
  )
  await cli('wake-run', TENANT, '--kind', 'daily', '--force')
  const findings = await json(`${API}/api/v1/wake/findings?view=all&status=open&limit=50`, { token: state.admin })
  const finding = findings.items.find((f) => f.check_code === 'prospect_due')
  check(
    'AI wake-up reminds the follower: 管理员 有 1 条商机该跟进了',
    finding && finding.title === '管理员 有 1 条商机该跟进了' && finding.detail.startsWith('王先生'),
    findings.items.map((f) => f.title),
  )
  if (!finding) return
  await admin.goto(`${CONSOLE}${finding.link}`)
  await admin.waitForSelector('[data-testid="prospect-table"]')
  await admin.waitForFunction(() => !document.querySelector('[data-testid="prospect-table"] .el-loading-mask'))
  const rows = await admin.locator('[data-testid="prospect-table"] .el-table__row').allInnerTexts()
  const view = await text(admin.locator('[data-testid="prospect-views"] .is-active'))
  check(
    "the reminder link opens today's list of that follower",
    rows.length === 1 && rows[0].includes('王先生') && view.startsWith('今天该跟进'),
    { rows, view },
  )
  await admin.screenshot({ path: `${SHOTS}/p24-08-wake-link.png`, fullPage: true })
}

// ---- 6. 放弃和重新跟进 ----

async function loseAndReopen(admin) {
  await prospects(admin)
  const drawer = await openProspect(admin, '王先生')
  await drawer.locator('[data-testid="prospect-lost"]').click()
  const box = admin.locator('.el-message-box:visible')
  await box.waitFor()
  await box.locator('input').fill('业委会决定暂缓换锁')
  await box.locator('button', { hasText: '放弃' }).click()
  await success(admin, '已放弃跟进')
  const reason = await text(drawer.locator('[data-testid="prospect-lost-reason"]'))
  check('giving up keeps the reason', reason === '业委会决定暂缓换锁', reason)
  await drawer.locator('[data-testid="prospect-reopen"]').click()
  await success(admin, '已重新跟进')
  const status = await text(drawer.locator('[data-testid="prospect-status"]'))
  check('a lost prospect can be followed up again', status === '跟进中', status)
  await closeDrawer(admin)
}

// ---- 7. 坐席的查看范围 ----

async function agentScope(alice) {
  await prospects(alice, 'all')
  const rows = await alice.locator('[data-testid="prospect-table"] .el-table__row').allInnerTexts()
  const settings = await alice.locator('[data-testid="prospect-settings-open"]').count()
  check(
    "小艾 sees the prospects she follows (not 王先生) and has no settings",
    rows.length === 2 && rows.every((r) => !r.includes('王先生')) && settings === 0,
    { rows, settings },
  )
  await alice.screenshot({ path: `${SHOTS}/p24-09-agent.png`, fullPage: true })
}

async function cleanup() {
  if (state.providerId && state.ops) {
    await fetch(`${API}/platform/v1/llm-providers/${state.providerId}`, {
      method: 'DELETE',
      headers: { authorization: `Bearer ${state.ops}` },
    }).catch(() => undefined)
  }
}

;(async () => {
  if (!PLATFORM_PASSWORD) {
    console.error('请通过环境变量 PLATFORM_PASSWORD 提供平台运营账号的密码')
    process.exit(2)
  }
  fs.mkdirSync(SHOTS, { recursive: true })
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined })
  try {
    await prepare()
    const admin = await consoleLogin(browser, 'admin')
    const alice = await consoleLogin(browser, 'alice')
    await staffConverts(admin)
    await aiConverts(browser, admin, alice)
    await suggestion(browser, admin, alice)
    await wakeReminder(admin)
    await loseAndReopen(admin)
    await agentScope(alice)
  } catch (error) {
    summary.checks.push(`FAIL exception -> ${error.stack || error}`)
  } finally {
    await cleanup()
    for (const ctx of state.contexts) await ctx.close().catch(() => undefined)
    await browser.close()
  }
  check('no console errors', summary.consoleErrors.length === 0, summary.consoleErrors)
  fs.writeFileSync(`${SHOTS}/summary.json`, JSON.stringify(summary, null, 2))
  for (const line of summary.checks) console.log(line)
  const failed = summary.checks.filter((line) => line.startsWith('FAIL'))
  console.log(failed.length ? `${failed.length} FAILED` : `ALL ${summary.checks.length} PASSED`)
  process.exit(failed.length ? 1 : 0)
})()
