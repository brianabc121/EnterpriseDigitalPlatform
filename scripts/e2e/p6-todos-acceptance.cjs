// P6 待办验收：AI 解析客户需求生成待办，人工确认后由员工处理（设计文档 §24）。
//
// 1. 管理员在"设置 → 待办"里把开票交给财务组（分给最空闲的组员），开启"访客查看服务进度"。
// 2. 访客向 AI 要专票：缺税号时 AI 追问，补全后登记并按类型的话术答复；待办进入待确认页，
//    确认人小财收到站内信，待办菜单有角标。
// 3. 小财从站内信打开待办，核对依据的对话，确认并交给小务。
// 4. 小务从站内信打开待办，开始处理，完成并通知客户：访客收到完成通知，在 Widget 的"服务进度"里
//    看到已完成；小财在"我分派的"里看到已完成。
// 5. 访客又要产品手册（AI 登记时会话还没有坐席，在公共待认领池）后转人工：小财在会话里看到待确认的
//    待办，直接确认后成为处理人；访客请他明天回电，小财在右栏"待办"里选中消息让 AI 预填，保存为
//    自己的待办。
// 6. 会话结束后 AI 解析出客户新提的换货，进入待确认页（已登记的事不重复生成）；缺少必填的诉求，
//    组长批量确认时提示缺少，打开后补全再确认。
// 7. 员工新建：小务粘贴客户的话让 AI 预填，补全字段后保存，直接进入"我的待办"（不经过待确认）。
//
// 前置：同 G5（后端、实时消费进程、调度进程接到模拟大模型；控制台、运营后台、Widget、OpenIM）。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p6-todos-acceptance.cjs
const { chromium } = require('playwright')
const { execFileSync } = require('child_process')
const fs = require('fs')
const path = require('path')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const WIDGET = env('WIDGET_URL', 'http://localhost:5175')
const FAKE_LLM = env('FAKE_LLM_URL', 'http://127.0.0.1:8900/v1')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p6')
const BACKEND_DIR = path.resolve(__dirname, '../../backend')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `p6-${RUN}`
const PASSWORD = 'demo-pass-2026'
const PROVIDER = `待办验收模型-${RUN}`

// 访客的话（AI 登记时的需求描述保持客户原话，会话后解析据此判断是不是已经登记过的事）。
const ASK_INVOICE = '你好，我要开一张专票，抬头是星河科技有限公司'
const TAX_NO = '91310000MA1FL8XQ3X'
const GIVE_TAX_NO = `税号是 ${TAX_NO}，发票发到 finance@xinghe.example`
const ASK_MANUAL = '另外把产品手册发我一份，谢谢'
const ASK_CALLBACK = '麻烦明天下午给我回个电话'
const ASK_EXCHANGE = '对了，上次买的门锁用了两天坏了，想换一个'
const INVOICE_TITLE = '开具增值税专用发票'
const INVOICE_RESULT = '电子专票已开具并发送到 finance@xinghe.example'

const summary = { tenant: TENANT, checks: [], consoleErrors: [] }
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
  if (!response.ok) throw new Error(`${method} ${url} -> ${response.status} ${text}`)
  return text ? JSON.parse(text) : null
}

async function waitFor(fn, timeout = 20000) {
  const deadline = Date.now() + timeout
  for (;;) {
    const value = await fn().catch(() => null)
    if (value || Date.now() > deadline) return value
    await new Promise((r) => setTimeout(r, 1000))
  }
}

async function login(username) {
  const tokens = await json(`${API}/api/v1/auth/login`, {
    method: 'POST',
    body: { tenant_code: TENANT, username, password: PASSWORD },
  })
  return tokens.access_token
}

/** 在后端目录执行命令行工具（平时由调度进程定时执行的待办任务）。 */
function cli(...args) {
  const output = execFileSync('uv', ['run', 'python', '-m', 'app.cli', ...args], {
    cwd: BACKEND_DIR,
    env: process.env,
    encoding: 'utf-8',
  })
  const lines = output.trim().split('\n')
  return JSON.parse(lines[lines.length - 1])
}

/** 模拟大模型：安排接下来的工具调用。 */
async function plan(...calls) {
  return json(`${FAKE_LLM}/_control`, { method: 'POST', body: { mode: 'normal', tool_plan: calls } })
}

const invoiceCall = (fields) => [
  'create_todo',
  { type: 'invoice', title: INVOICE_TITLE, detail: ASK_INVOICE, fields },
]

async function prepareTenant() {
  const platform = await json(`${API}/platform/v1/auth/login`, {
    method: 'POST',
    body: { username: PLATFORM_USER, password: PLATFORM_PASSWORD },
  })
  const ops = platform.access_token
  const tenant = await json(`${API}/platform/v1/tenants`, {
    method: 'POST',
    token: ops,
    body: {
      code: TENANT,
      name: `待办验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  // 支持工具调用的模型，指定给验收租户。
  const provider = await json(`${API}/platform/v1/llm-providers`, {
    method: 'POST',
    token: ops,
    body: {
      name: PROVIDER,
      base_url: FAKE_LLM,
      api_key: 'sk-p6-acceptance',
      chat_model: 'fake-chat',
      capabilities: { tools: true },
    },
  })
  await json(`${API}/platform/v1/tenants/${tenant.id}/llm`, {
    method: 'PUT',
    token: ops,
    body: { provider_id: provider.id },
  })
  const admin = await login('admin')
  const staff = {}
  for (const [username, name, role] of [
    ['fin', '小财', 'agent'],
    ['fin2', '小务', 'agent'],
    ['lead', '组长', 'supervisor'],
  ]) {
    const created = await json(`${API}/api/v1/staff`, {
      method: 'POST',
      token: admin,
      body: { username, display_name: name, password: PASSWORD, role_codes: [role] },
    })
    staff[username] = created.id
  }
  const group = await json(`${API}/api/v1/skill-groups`, {
    method: 'POST',
    token: admin,
    body: {
      name: '财务组',
      members: [
        { staff_id: staff.fin },
        { staff_id: staff.fin2 },
        { staff_id: staff.lead, is_lead: true },
      ],
    },
  })
  const policies = await json(`${API}/api/v1/routing-policies`, { token: admin })
  const policy = policies.items.find((p) => p.is_default)
  await json(`${API}/api/v1/routing-policies/${policy.id}`, {
    method: 'PATCH',
    token: admin,
    body: { mode: 'ai_first', default_skill_group_id: group.id },
  })
  await json(`${API}/api/v1/ai/settings`, {
    method: 'PUT',
    token: admin,
    body: { enabled: true, tools_enabled: true },
  })
  const channels = await json(`${API}/api/v1/channels`, { token: admin })
  return { tenantId: tenant.id, admin, staff, groupId: group.id, channel: channels.items[0] }
}

function watchErrors(page, label) {
  page.on('console', (m) => {
    if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) {
      summary.consoleErrors.push(`${label}: ${m.text()}`)
    }
  })
  page.on('pageerror', (e) => summary.consoleErrors.push(`${label} pageerror: ${e.message}`))
}

// 打开的页面：失败时逐个截图，便于排查。
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
  await page.locator('[data-testid="main-menu"] .el-menu-item', { hasText: new RegExp('^\\s*' + title) }).click()
}

async function pickOption(page, text) {
  await page.locator('.el-select-dropdown__item:visible', { hasText: text }).first().click()
}

async function confirmBox(page, text) {
  const box = page.locator('.el-message-box:visible')
  await box.waitFor()
  await box.locator('.el-message-box__btns button', { hasText: text }).click()
}

async function openVisitor(browser, ctx) {
  const page = await newPage(browser, 'visitor', { width: 420, height: 760 })
  await page.goto(`${WIDGET}/?key=${encodeURIComponent(ctx.channel.public_key)}`)
  await page.locator('[data-testid="widget-state"]', { hasText: '在线' }).waitFor()
  const token = await page.evaluate(
    (k) => localStorage.getItem(`edp.visitor.${k}`),
    ctx.channel.public_key,
  )
  return { page, token }
}

async function say(visitor, text) {
  await visitor.page.fill('[data-testid="message-input"]', text)
  await visitor.page.click('[data-testid="send-button"]')
}

const botSaid = (visitor, text) =>
  seen(visitor.page.locator('[data-testid="message"].bot', { hasText: text }), 30000)

async function todos(token, query = '') {
  return (await json(`${API}/api/v1/todos?limit=100${query}`, { token })).items
}

/** 打开站内信里的一条提醒（跳转到待办并显示详情）。 */
async function openNotification(page, text) {
  await page.click('[data-testid="notification-bell"]')
  const item = page.locator('[data-testid="notification"]', { hasText: text })
  const found = await seen(item, 30000)
  if (found) await item.first().click()
  return found
}

const drawer = (page) => page.locator('[data-testid="todo-drawer"]')

async function closeDrawer(page) {
  await page.keyboard.press('Escape')
  await page.locator('.el-drawer:visible').waitFor({ state: 'hidden' }).catch(() => null)
}

// ---- 1. 待办设置 ----

async function adminSection(browser, ctx) {
  const page = await consoleLogin(browser, 'admin')
  await menu(page, '设置')
  await page.click('#tab-todos')
  const table = page.locator('[data-testid="todo-types-table"]')
  await table.locator('.el-table__row', { hasText: '开票' }).waitFor()
  const presets = await table.locator('.el-table__row').count()
  check('待办类型：预置了回电、留言、开票、投诉等 13 种（含订单的审核、催收、待发货、缺货处理）', presets === 13, presets)

  // 开票交给财务组，分给未完成待办最少的组员。
  await page.click('[data-testid="edit-type-invoice"]')
  const dialog = page.locator('[data-testid="todo-type-dialog"]')
  await dialog.locator('[data-testid="type-name"]').waitFor()
  await dialog.locator('[data-testid="type-group"]').click()
  await pickOption(page, '财务组')
  await dialog
    .locator('[data-testid="type-group-mode"] .el-radio', { hasText: '交给未完成待办最少的组员' })
    .click()
  await shot(page, '1-type-dialog')
  await dialog.locator('[data-testid="type-save"]').click()
  const rule = await seen(
    table.locator('.el-table__row', { hasText: '财务组（分给最空闲的组员）' }),
  )
  const types = await json(`${API}/api/v1/todo-types`, { token: ctx.admin })
  const invoice = types.items.find((t) => t.code === 'invoice')
  check(
    '开票类型的分派规则：财务组，分给最空闲的组员',
    rule &&
      invoice.assign_rule.skill_group_id === ctx.groupId &&
      invoice.assign_rule.group_mode === 'least_loaded',
    invoice.assign_rule,
  )

  // 开启访客在 Widget 查看服务进度。
  await page.click('[data-testid="visitor-progress-switch"]')
  await page.click('[data-testid="save-todo-settings"]')
  await page.locator('.el-message--success', { hasText: '待办设置已保存' }).waitFor()
  const settings = await json(`${API}/api/v1/admin/todo-settings`, { token: ctx.admin })
  check('待办设置：开启访客查看服务进度', settings.visitor_progress === true, settings)
  await shot(page, '1-todo-settings')
  return page
}

// ---- 2. AI 登记开票 ----

async function registerSection(browser, ctx) {
  const visitor = await openVisitor(browser, ctx)
  await plan(invoiceCall({ invoice_type: '增值税专用发票', invoice_title: '星河科技有限公司' }))
  await say(visitor, ASK_INVOICE)
  const asked = await botSaid(visitor, '税号')
  check(
    'AI 登记：缺少必填的税号时先向客户追问，不登记',
    asked && (await todos(ctx.admin, '&view=pending')).length === 0,
  )

  await plan(
    invoiceCall({
      invoice_type: '增值税专用发票',
      invoice_title: '星河科技有限公司',
      tax_no: TAX_NO,
      email: 'finance@xinghe.example',
    }),
  )
  await say(visitor, GIVE_TAX_NO)
  const promised = await botSaid(visitor, '已为您记录开票申请，客服确认后会尽快为您处理')
  await shot(visitor.page, '2-widget-registered')
  const pending = await waitFor(async () => {
    const items = await todos(ctx.admin, '&view=pending')
    return items.length === 1 ? items : null
  })
  const todo = pending?.[0]
  ctx.invoiceId = todo?.id
  check(
    'AI 登记后按类型的话术答复；待办进入待确认页，确认人是财务组里最空闲的小财',
    promised &&
      todo?.status === 'pending' &&
      todo.source === 'ai_chat' &&
      todo.type_code === 'invoice' &&
      todo.assignee_name === '小财',
    todo,
  )
  const listed = await todos(ctx.admin)
  check('待确认的不出现在待办列表里', !listed.some((t) => t.id === todo?.id), listed)
  return visitor
}

// ---- 3. 小财确认并交给小务 ----

async function confirmSection(browser, ctx) {
  const page = await consoleLogin(browser, 'fin')
  const badge = await seen(page.locator('[data-testid="todo-badge"]', { hasText: '1' }))
  check('待办菜单的角标提示 1 条待确认', badge)
  const opened = await openNotification(page, `待确认：开票「${INVOICE_TITLE}」`)
  const box = drawer(page)
  const shown =
    opened &&
    (await seen(box.locator('[data-testid="todo-status"]', { hasText: '待确认' }))) &&
    (await seen(box.locator('[data-testid="todo-handler"]', { hasText: '小财' })))
  const evidence = await seen(box.locator('[data-testid="todo-evidence"]', { hasText: '税号' }))
  const fields = await box.locator(`[data-testid="todo-field-tax_no"]`).innerText().catch(() => '')
  check(
    '站内信打开待确认的待办：显示字段、确认人和依据的对话',
    shown && evidence && fields.includes(TAX_NO) && page.url().includes('view=pending'),
    { url: page.url(), fields },
  )
  await shot(page, '3-pending-drawer')

  // 确认，并交给小务处理。
  await box.locator('[data-testid="todo-confirm"]').click()
  const dialog = page.locator('[data-testid="confirm-dialog"]')
  await dialog.locator('[data-testid="confirm-handler"]').waitFor()
  await dialog.locator('[data-testid="confirm-handler"] .el-radio', { hasText: '指定员工' }).click()
  await dialog.locator('[data-testid="confirm-assignee"]').click()
  await pickOption(page, '小务')
  await shot(page, '3-confirm-dialog')
  await dialog.locator('[data-testid="confirm-submit"]').click()
  await page.locator('.el-message--success', { hasText: '已确认' }).waitFor()
  const handed =
    (await seen(box.locator('[data-testid="todo-status"]', { hasText: '待处理' }))) &&
    (await seen(box.locator('[data-testid="todo-handler"]', { hasText: '小务' })))
  const detail = await json(`${API}/api/v1/todos/${ctx.invoiceId}`, { token: ctx.admin })
  check(
    '小财确认后交给小务：进入待办列表，截止时间从确认时开始计算',
    handed &&
      detail.status === 'open' &&
      detail.assignee_id === ctx.staff.fin2 &&
      !!detail.due_at &&
      detail.events.some((e) => e.type === 'confirmed'),
    { status: detail.status, assignee: detail.assignee_name, due: detail.due_at },
  )
  await closeDrawer(page)
  return page
}

// ---- 4. 小务处理并通知客户 ----

async function handleSection(browser, ctx, visitor, fin) {
  const page = await consoleLogin(browser, 'fin2')
  const opened = await openNotification(page, `新待办：开票「${INVOICE_TITLE}」`)
  const box = drawer(page)
  check(
    '小务收到站内信，打开后显示待处理的待办',
    opened && (await seen(box.locator('[data-testid="todo-status"]', { hasText: '待处理' }))),
  )
  await box.locator('[data-testid="todo-start"]').click()
  const started = await seen(box.locator('[data-testid="todo-status"]', { hasText: '处理中' }))
  await box.locator('[data-testid="todo-done"]').click()
  await page.locator('textarea[data-testid="done-result"]').fill(INVOICE_RESULT)
  const notify = await page.locator('[data-testid="done-notify"] input').isChecked()
  await shot(page, '4-done-dialog')
  await page.click('[data-testid="done-submit"]')
  await page.locator('.el-message--success', { hasText: '已完成，并通知客户' }).waitFor()
  const done =
    (await seen(box.locator('[data-testid="todo-status"]', { hasText: '已完成' }))) &&
    (await seen(box.locator('[data-testid="todo-result"]', { hasText: INVOICE_RESULT })))
  check('小务开始处理、填写结果完成（默认通知客户）', started && notify && done)
  await shot(page, '4-done-drawer')
  await closeDrawer(page)

  // 访客收到完成通知（类型的完成通知模板），在服务进度里看到已完成。
  const notice = await seen(
    visitor.page.locator('[data-testid="message"]', {
      hasText: `您好，您的发票已开具：${INVOICE_RESULT}`,
    }),
    30000,
  )
  check('访客收到完成通知（完成通知模板 + 处理结果）', notice)
  await visitor.page.click('[data-testid="progress-tab"]')
  const item = visitor.page.locator('[data-testid="progress-item"]', { hasText: INVOICE_TITLE })
  const progress = await seen(item.filter({ hasText: '已完成' }))
  check('Widget 的服务进度里看到开票已完成', progress, await item.allInnerTexts().catch(() => []))
  await shot(visitor.page, '4-widget-progress')
  // 顶部的"返回"回到对话。
  await visitor.page.click('[data-testid="leave-message-tab"]')
  await visitor.page.locator('[data-testid="message-input"]').waitFor()

  // 小财在"我分派的"里看到已完成。
  await menu(fin, '待办')
  await fin.click('[data-testid="todo-view-assigned"]')
  const assigned = await seen(
    fin
      .locator('[data-testid="todos-table"] .el-table__row', { hasText: INVOICE_TITLE })
      .filter({ has: fin.locator('[data-testid="todo-row-status"]', { hasText: '已完成' }) }),
  )
  check('小财在"我分派的"里跟踪到开票已完成', assigned)
  await shot(fin, '4-assigned-view')
  return page
}

// ---- 5. 工作台：会话里确认 AI 登记的待办，选中消息预填 ----

async function workbenchSection(ctx, visitor, fin) {
  await menu(fin, '工作台')
  await fin.locator('[data-testid="agent-status"]', { hasText: '在线' }).waitFor({ timeout: 15000 })

  // AI 登记资料寄送：会话还没有坐席、客户没有归属坐席，进入公共待认领池等待确认。
  await plan([
    'create_todo',
    {
      type: 'send_materials',
      title: '寄送产品手册',
      detail: ASK_MANUAL,
      fields: { material: '产品手册', email: 'finance@xinghe.example' },
    },
  ])
  await say(visitor, ASK_MANUAL)
  const promised = await botSaid(visitor, '已为您登记资料寄送')
  const manual = await waitFor(async () => {
    const items = await todos(ctx.admin, '&view=pending')
    return items.find((t) => t.type_code === 'send_materials') ?? null
  })
  check(
    'AI 登记资料寄送：会话还没有坐席，在公共待认领池等待确认',
    promised && !!manual && manual.assignee_id === null && manual.skill_group_id === null,
    manual,
  )

  // 转人工：小财接待，会话里显示 AI 生成、等待确认的待办，直接确认后成为处理人。
  await visitor.page.click('[data-testid="ask-human"]')
  const session = fin.locator('[data-testid="session-item"]').first()
  await session.waitFor({ timeout: 30000 })
  await session.click()
  const card = fin.locator('[data-testid="session-todos"]')
  const carded = await seen(card.filter({ hasText: '寄送产品手册' }))
  await shot(fin, '5-session-todos')
  await card.locator('[data-testid="session-todo-confirm"]').first().click()
  await fin.locator('.el-message--success', { hasText: '已确认' }).waitFor()
  const confirmed = await json(`${API}/api/v1/todos/${manual?.id}`, { token: ctx.admin })
  check(
    '会话里直接确认：按规则交给正在接待的小财',
    carded && confirmed.status === 'open' && confirmed.assignee_id === ctx.staff.fin,
    { status: confirmed.status, assignee: confirmed.assignee_name },
  )

  // 访客请小财明天回电：右栏"待办"里选中消息，AI 预填后保存为自己的待办。
  await say(visitor, ASK_CALLBACK)
  await fin
    .locator('[data-testid="chat-message"]', { hasText: ASK_CALLBACK })
    .first()
    .waitFor({ timeout: 20000 })
    .catch(() => null)
  await fin.click('[data-testid="todos-tab"]')
  const panel = fin.locator('[data-testid="customer-todos"]')
  const listed = await seen(panel.locator('[data-testid="customer-todo"]', { hasText: '寄送产品手册' }))
  await panel.locator('[data-testid="customer-todo-pick"]').click()
  const messages = fin.locator('[data-testid="pick-message"]')
  await messages.filter({ hasText: ASK_CALLBACK }).waitFor()
  await fin.click('[data-testid="pick-extract"]')
  // 选中的三条消息里有两件事（资料寄送、回电）：选择回电。
  const suggestion = fin.locator('.el-dialog:visible .el-button', { hasText: '回电 / 回访' })
  await suggestion.waitFor({ timeout: 20000 })
  await shot(fin, '5-pick-suggestions')
  await suggestion.click()
  const form = fin.locator('[data-testid="todo-form"]')
  await form.locator('input[data-testid="todo-title"]').waitFor()
  const title = await form.locator('input[data-testid="todo-title"]').inputValue()
  await form.locator('[data-testid="todo-assign-mode"] .el-radio', { hasText: '自己处理' }).click()
  await shot(fin, '5-prefilled-form')
  await form.locator('[data-testid="todo-save"]').click()
  await fin.locator('.el-message--success', { hasText: '已新建待办' }).waitFor()
  const callback = await waitFor(async () => {
    const items = await todos(ctx.admin, `&customer_id=${confirmed.customer_id}&view=all`)
    return items.find((t) => t.type_code === 'callback') ?? null
  })
  const shownInPanel = await seen(panel.locator('[data-testid="customer-todo"]', { hasText: title }))
  check(
    '选中消息让 AI 预填回电，保存后直接成为小财的待办（来源：工作台）',
    listed &&
      title.includes('回个电话') &&
      callback?.status === 'open' &&
      callback.source === 'copilot' &&
      callback.assignee_id === ctx.staff.fin &&
      shownInPanel,
    { title, callback },
  )
  await shot(fin, '5-customer-todos')
  return confirmed.customer_id
}

// ---- 6. 会话后解析，组长确认 ----

async function extractSection(browser, ctx, visitor, fin, customerId) {
  await say(visitor, ASK_EXCHANGE)
  await fin
    .locator('[data-testid="chat-message"]', { hasText: '门锁' })
    .first()
    .waitFor({ timeout: 20000 })
    .catch(() => null)
  await fin.click('[data-testid="close-session"]')
  await confirmBox(fin, '结束')

  // 调度进程每分钟解析一次；这里用命令行立即执行（两者都执行时不会重复解析）。
  const report = cli('todo-jobs')
  check(
    '命令行 todo-jobs：发送提醒、今日汇总并解析最近结束的会话',
    ['timers', 'digests', 'extracted'].every((key) => key in report),
    report,
  )
  const extracted = await waitFor(async () => {
    const items = await todos(ctx.admin, `&view=pending&customer_id=${customerId}`)
    return items.find((t) => t.source === 'ai_summary') ? items : null
  }, 60000)
  const summaryTodos = (extracted ?? []).filter((t) => t.source === 'ai_summary')
  check(
    '会话结束后 AI 解析出新提的换货进入待确认；已登记的开票、资料、回电不重复生成',
    summaryTodos.length === 1 && summaryTodos[0].type_code === 'after_sales',
    summaryTodos.map((t) => [t.type_code, t.title]),
  )

  // 组长：批量确认时提示缺少必填的诉求；打开后补全再确认，自己处理。
  const lead = await consoleLogin(browser, 'lead')
  await menu(lead, '待办')
  await lead.click('[data-testid="todo-view-pending"]')
  const row = lead.locator('[data-testid="todos-table"] .el-table__row', { hasText: '门锁' })
  await row.waitFor({ timeout: 15000 })
  await row.locator('.el-checkbox').first().click()
  await lead.click('[data-testid="batch-confirm"]')
  const warned = await seen(lead.locator('.el-message--warning', { hasText: '缺少诉求' }))
  check('批量确认：缺少必填信息的不能确认，提示缺少什么', warned)
  await shot(lead, '6-batch-warning')
  await row.locator('td').nth(2).click()
  const box = drawer(lead)
  await box.locator('[data-testid="todo-confirm"]').click()
  const dialog = lead.locator('[data-testid="confirm-dialog"]')
  await dialog.locator('[data-testid="field-request"]').click()
  await pickOption(lead, '换货')
  await dialog.locator('[data-testid="confirm-handler"] .el-radio', { hasText: '自己处理' }).click()
  await shot(lead, '6-confirm-fill')
  await dialog.locator('[data-testid="confirm-submit"]').click()
  await lead.locator('.el-message--success', { hasText: '已确认' }).waitFor()
  const exchange = await json(`${API}/api/v1/todos/${summaryTodos[0]?.id}`, { token: ctx.admin })
  check(
    '组长补全诉求后确认，自己处理',
    exchange.status === 'open' &&
      exchange.assignee_id === ctx.staff.lead &&
      exchange.fields.some((f) => f.key === 'request' && f.value === '换货'),
    { status: exchange.status, fields: exchange.fields },
  )
  await closeDrawer(lead)
}

// ---- 7. 员工新建 ----

async function createSection(ctx, fin2) {
  await menu(fin2, '待办')
  await fin2.click('[data-testid="new-todo"]')
  const form = fin2.locator('[data-testid="todo-form"]')
  await form.locator('textarea[data-testid="todo-paste"]').fill('客户王先生问 100 台智能门锁的批量价')
  await form.locator('[data-testid="todo-prefill"]').click()
  await fin2.locator('.el-message--success', { hasText: 'AI 已预填「报价」' }).waitFor({ timeout: 20000 })
  // 必填的"产品或型号"AI 没有识别出来：补上。
  await form.locator('[data-testid="todo-save"]').click()
  const warned = await seen(fin2.locator('.el-message--warning', { hasText: '产品或型号' }))
  await form.locator('input[data-testid="field-product"]').fill('智能门锁 X1')
  await form.locator('[data-testid="todo-assign-mode"] .el-radio', { hasText: '自己处理' }).click()
  await shot(fin2, '7-new-todo')
  await form.locator('[data-testid="todo-save"]').click()
  await fin2.locator('.el-message--success', { hasText: '已新建待办' }).waitFor()
  await fin2.click('[data-testid="todo-view-mine"]')
  const row = fin2
    .locator('[data-testid="todos-table"] .el-table__row', { hasText: '批量价' })
    .filter({ has: fin2.locator('[data-testid="todo-row-status"]', { hasText: '待处理' }) })
  const mine = await seen(row)
  const quote = (await todos(ctx.admin, '&view=all')).find((t) => t.type_code === 'quote')
  const pending = await todos(ctx.admin, '&view=pending')
  check(
    '员工新建（AI 预填、补全必填字段）直接进入我的待办，不经过待确认',
    warned &&
      mine &&
      quote?.status === 'open' &&
      quote.source === 'staff' &&
      quote.assignee_id === ctx.staff.fin2 &&
      !pending.some((t) => t.id === quote.id),
    quote,
  )
  await shot(fin2, '7-my-todos')
}

async function run(browser) {
  const ctx = await prepareTenant()
  try {
    await adminSection(browser, ctx)
    const visitor = await registerSection(browser, ctx)
    const fin = await confirmSection(browser, ctx)
    const fin2 = await handleSection(browser, ctx, visitor, fin)
    const customerId = await workbenchSection(ctx, visitor, fin)
    await extractSection(browser, ctx, visitor, fin, customerId)
    await createSection(ctx, fin2)
    check('没有前端脚本错误', summary.consoleErrors.length === 0, summary.consoleErrors)
  } finally {
    await plan().catch(() => null)
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
  } catch (error) {
    for (const [label, page] of pages) {
      await page.screenshot({ path: `${SHOTS}/failed-${label}.png` }).catch(() => null)
    }
    throw error
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
