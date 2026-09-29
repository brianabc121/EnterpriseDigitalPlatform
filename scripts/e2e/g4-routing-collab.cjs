// 会话与路由细节的浏览器验收（G4）：
//
// 1. 设置：技能组"一线"排队 1 分钟后溢出到"二线"；默认路由策略加上 VIP 标签"大客户"、开启"排队时 AI 回答"、
//    按意图分配（"售后"：退货、维修 → 售后组）。
// 2. 排队优先级：三位访客排队，打了"大客户"标签的排第一，说"投诉"的排第二；访客窗口显示前面还有几位；
//    主管在工作台"排队"里看到 VIP、优先标记；说"退货"的访客按意图排到售后组。
// 3. 访客取消排队（AI 未启用时结束会话）。
// 4. 溢出：一线没有人接待，1 分钟后溢出到二线，由小博接待；小艾上线后接待售后组的客户。
// 5. 旁听与协助：主管在"进行中"旁听小博的会话（客户看不到）；小博邀请小艾协助，客户看到提示，
//    小艾回复后退出协助。
// 6. 启用 AI 后小博把会话交还 AI，AI 继续回答；主管把这个 AI 会话转人工；坐席结束会话后在"已结束"里看到。
// 7. 排队期间 AI 继续回答；访客取消排队后回到 AI 接待。
// 8. 客户转移申请：小艾申请把客户转给自己，管理员在"转移申请"里批准，归属记录显示"申请审批"。
//
// 前置：同 P3（后端、实时消费进程、调度进程接到模拟大模型；控制台、Widget、OpenIM）。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/g4-routing-collab.cjs
const { chromium } = require('playwright')
const fs = require('fs')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const WIDGET = env('WIDGET_URL', 'http://localhost:5175')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/g4')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `g4-${RUN}`
const PASSWORD = 'demo-pass-2026'
const ANSWER = '一般 2 到 3 天送达，偏远地区 5 到 7 天。'

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
      name: `路由协作验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  const admin = await login('admin')
  const staff = {}
  for (const [username, name, role] of [
    ['alice', '小艾', 'agent'],
    ['bob', '小博', 'agent'],
    ['susan', '苏珊', 'supervisor'],
  ]) {
    const created = await json(`${API}/api/v1/staff`, {
      method: 'POST',
      token: admin,
      body: { username, display_name: name, password: PASSWORD, role_codes: [role] },
    })
    staff[username] = created.id
  }
  const groups = {}
  for (const [name, members] of [
    ['一线', [[staff.alice], [staff.susan, true]]],
    ['二线', [[staff.bob], [staff.susan, true]]],
    ['售后', [[staff.alice], [staff.susan, true]]],
  ]) {
    const group = await json(`${API}/api/v1/skill-groups`, {
      method: 'POST',
      token: admin,
      body: {
        name,
        members: members.map(([id, lead]) => ({ staff_id: id, is_lead: !!lead })),
      },
    })
    groups[name] = group.id
  }
  const policies = await json(`${API}/api/v1/routing-policies`, { token: admin })
  const policy = policies.items.find((p) => p.is_default)
  await json(`${API}/api/v1/routing-policies/${policy.id}`, {
    method: 'PATCH',
    token: admin,
    body: { default_skill_group_id: groups['一线'] },
  })
  // AI 在第 6 步才启用；先准备好知识，给向量化留出时间。
  await json(`${API}/api/v1/kb/items`, {
    method: 'POST',
    token: admin,
    body: {
      title: '订单发货后多久能到？',
      content: ANSWER,
      questions: ['快递几天能到'],
      publish: true,
    },
  })
  const channels = await json(`${API}/api/v1/channels`, { token: admin })
  return { admin, staff, groups, policyId: policy.id, channelKey: channels.items[0].public_key }
}

function watchErrors(page, label) {
  page.on('console', (m) => {
    if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) {
      summary.consoleErrors.push(`${label}: ${m.text()}`)
    }
  })
  page.on('pageerror', (e) => summary.consoleErrors.push(`${label} pageerror: ${e.message}`))
}

async function newPage(browser, label, viewport = { width: 1440, height: 900 }) {
  const context = await browser.newContext({ viewport, locale: 'zh-CN' })
  const page = await context.newPage()
  watchErrors(page, label)
  return page
}

async function shot(page, name) {
  // 等弹窗、抽屉的动画结束。
  await page.waitForTimeout(600)
  await page.screenshot({ path: `${SHOTS}/${name}.png` })
}

async function consoleLogin(page, username) {
  await page.goto(`${CONSOLE}/login`)
  await page.fill('input[placeholder="例如 demo"]', TENANT)
  await page.fill('input[autocomplete="username"]', username)
  await page.fill('input[autocomplete="current-password"]', PASSWORD)
  await page.click('button:has-text("登录")')
  await page.locator('[data-testid="main-menu"]').waitFor()
}

async function menu(page, title) {
  await page.locator('[data-testid="main-menu"] .el-menu-item', { hasText: title }).click()
}

async function confirmBox(page, text) {
  const box = page.locator('.el-message-box:visible')
  await box.waitFor()
  await box.locator('.el-message-box__btns button', { hasText: text }).click()
}

async function pickOption(page, text) {
  await page.locator('.el-select-dropdown__item:visible', { hasText: text }).first().click()
}

// ---- 工作台 ----

async function openWorkbench(browser, username) {
  const page = await newPage(browser, username)
  await consoleLogin(page, username)
  await menu(page, '工作台')
  await page.locator('[data-testid="agent-status"]', { hasText: '在线' }).waitFor({ timeout: 15000 })
  return page
}

async function setStatus(page, label) {
  await page.click('[data-testid="agent-status"]')
  await pickOption(page, label)
  await page.locator('[data-testid="agent-status"]', { hasText: label }).waitFor()
}

async function listTab(page, name) {
  await page.locator('[data-testid="session-tabs"] .el-tabs__item', { hasText: name }).click()
}

const sessionItem = (page, name) => page.locator('[data-testid="session-item"]', { hasText: name })

async function openSession(page, tab, name) {
  await listTab(page, tab)
  await sessionItem(page, name).waitFor({ timeout: 20000 })
  await sessionItem(page, name).click()
  await page.locator('[data-testid="chat-title"]', { hasText: name }).waitFor()
}

// ---- 访客 ----

async function customerIds(admin) {
  const page = await json(`${API}/api/v1/customers?limit=100`, { token: admin })
  return new Set(page.items.map((c) => c.id))
}

/** 打开访客窗口；按新出现的客户找到这位访客的客户记录。 */
async function openVisitor(browser, admin, channelKey, label) {
  const before = await customerIds(admin)
  const page = await newPage(browser, label, { width: 420, height: 720 })
  await page.goto(`${WIDGET}/?key=${encodeURIComponent(channelKey)}`)
  await page.locator('[data-testid="widget-state"]', { hasText: '在线' }).waitFor()
  const token = await page.evaluate((k) => localStorage.getItem(`edp.visitor.${k}`), channelKey)
  const customerId = await waitFor(async () => {
    const after = await customerIds(admin)
    return [...after].find((id) => !before.has(id))
  })
  const customer = await json(`${API}/api/v1/customers/${customerId}`, { token: admin })
  return { page, token, customerId, name: customer.display_name, label }
}

async function say(visitor, text) {
  await visitor.page.fill('[data-testid="message-input"]', text)
  await visitor.page.click('[data-testid="send-button"]')
  await visitor.page.locator('[data-testid="message"].me', { hasText: text }).waitFor()
}

async function visitorState(visitor) {
  return json(`${API}/api/v1/visitor/session`, { headers: { 'X-Visitor-Token': visitor.token } })
}

async function sessionOf(admin, visitor) {
  const state = await visitorState(visitor)
  return state.session_id
    ? json(`${API}/api/v1/sessions/${state.session_id}`, { token: admin })
    : null
}

async function waitSession(admin, visitor, predicate, timeout = 20000) {
  return waitFor(async () => {
    const chat = await sessionOf(admin, visitor)
    return chat && predicate(chat) ? chat : null
  }, timeout)
}

const banner = (visitor) => visitor.page.locator('[data-testid="service-banner"]')
const notice = (visitor, text) =>
  visitor.page.locator('[data-testid="message"].system', { hasText: text })

async function seen(locator, timeout = 20000) {
  return locator
    .first()
    .waitFor({ timeout })
    .then(() => true)
    .catch(() => false)
}

// ---- 1. 设置 ----

async function settingsSection(page, ctx) {
  await menu(page, '设置')
  const settingsTab = (label) =>
    page.locator('[data-testid="settings-tabs"] .el-tabs__item', { hasText: label }).click()

  await settingsTab('技能组')
  const firstLine = page.locator('[data-testid="groups-table"] .el-table__row', { hasText: '一线' })
  await firstLine.locator('button', { hasText: '编辑' }).click()
  await page.click('[data-testid="group-overflow-group"]')
  await pickOption(page, '二线')
  await page.fill('[data-testid="group-overflow-minutes"] input', '1')
  await page.locator('.el-dialog:visible button', { hasText: '保存' }).click()
  const overflow = page.locator('[data-testid="groups-table"] [data-testid="group-overflow"]')
  const overflowShown = await seen(overflow.filter({ hasText: '排队 1 分钟 → 二线' }))
  const groups = await json(`${API}/api/v1/skill-groups`, { token: ctx.admin })
  const saved = groups.items.find((g) => g.name === '一线')
  check(
    '技能组"一线"：排队 1 分钟后溢出到"二线"',
    overflowShown &&
      saved.overflow_group_id === ctx.groups['二线'] &&
      saved.overflow_after_seconds === 60,
    saved,
  )

  await settingsTab('路由策略')
  const row = page.locator('[data-testid="policies-table"] .el-table__row', { hasText: '默认' })
  await row.locator('button', { hasText: '编辑' }).click()
  const tags = page.locator('input[data-testid="policy-priority-tags"]')
  await tags.fill('大客户')
  await tags.press('Enter')
  await page.locator('[data-testid="policy-ai-while-queued"]').click()
  await page.click('[data-testid="add-intent-route"]')
  const route = page.locator('[data-testid="intent-route"]').first()
  await route.locator('input[data-testid="intent-name"]').fill('售后')
  await route.locator('input[data-testid="intent-keywords"]').fill('退货，维修')
  await route.locator('[data-testid="intent-group"]').click()
  await pickOption(page, '售后')
  await shot(page, '1-policy-form')
  await page.click('[data-testid="save-policy"]')
  const priorityShown = await seen(
    page
      .locator('[data-testid="policy-priority"]')
      .filter({ hasText: '大客户' })
      .filter({ hasText: '排队时 AI 继续回答' }),
  )
  const intentsShown = await seen(
    page.locator('[data-testid="policy-intents"]', { hasText: '售后 → 售后' }),
  )
  const policies = await json(`${API}/api/v1/routing-policies`, { token: ctx.admin })
  const policy = policies.items.find((p) => p.is_default)
  check(
    '路由策略：VIP 标签加上"大客户"、投诉优先、排队时 AI 回答、按意图分配"售后"',
    priorityShown &&
      intentsShown &&
      policy.priority_tags.join() === 'VIP,大客户' &&
      policy.urgent_first &&
      policy.ai_while_queued &&
      policy.intent_routes.length === 1 &&
      policy.intent_routes[0].skill_group_id === ctx.groups['售后'] &&
      policy.intent_routes[0].keywords.join() === '退货,维修',
    policy,
  )
  await shot(page, '2-routing-settings')
}

// ---- 2-3. 排队优先级、按意图分配、取消排队 ----

async function queueSection(browser, ctx, susan) {
  const { admin, channelKey } = ctx
  const normal = await openVisitor(browser, admin, channelKey, 'normal')
  await say(normal, '你好，想咨询一下')
  const angry = await openVisitor(browser, admin, channelKey, 'angry')
  await say(angry, '我要投诉你们的服务')
  const vip = await openVisitor(browser, admin, channelKey, 'vip')
  await json(`${API}/api/v1/customers/${vip.customerId}`, {
    method: 'PATCH',
    token: admin,
    body: { tags: ['大客户'] },
  })
  await say(vip, '你好')
  const queued = await waitFor(async () => {
    const page = await json(`${API}/api/v1/sessions?status=queued`, { token: admin })
    return page.items.length === 3 ? page.items : null
  })
  check(
    '排队顺序：大客户（VIP）、投诉的客户、普通客户',
    queued?.map((s) => `${s.customer_display_name}:${s.priority}`).join() ===
      [`${vip.name}:20`, `${angry.name}:10`, `${normal.name}:0`].join(),
    queued?.map((s) => `${s.customer_display_name}:${s.priority}`),
  )
  const banners = await Promise.all(
    [
      [vip, '请稍候'],
      [angry, '前面还有 1 位'],
      [normal, '前面还有 2 位'],
    ].map(([visitor, text]) => seen(banner(visitor).filter({ hasText: text }), 25000)),
  )
  check('访客窗口显示排队位置：VIP 第一位，投诉的前面 1 位，普通的前面 2 位', banners.every(Boolean), banners)

  const returning = await openVisitor(browser, admin, channelKey, 'after-sales')
  await say(returning, '我买的耳机想退货')
  const afterSales = await waitSession(admin, returning, (s) => s.status === 'queued')
  check(
    '说"退货"的访客按意图排到售后组',
    afterSales?.intent === '售后' && afterSales.skill_group_id === ctx.groups['售后'],
    afterSales && { intent: afterSales.intent, group: afterSales.skill_group_id },
  )

  await listTab(susan, '排队')
  await sessionItem(susan, returning.name).waitFor({ timeout: 20000 })
  const items = await susan.locator('[data-testid="session-item"]').allInnerTexts()
  const names = items.map((text) =>
    [vip, angry, normal, returning].find((v) => text.includes(v.name))?.label,
  )
  check(
    '主管的"排队"：VIP、优先标记和意图',
    names.join() === 'vip,angry,normal,after-sales' &&
      items[0].includes('VIP') &&
      items[1].includes('优先') &&
      !items[2].includes('优先') &&
      items[3].includes('售后'),
    items,
  )
  await shot(susan, '3-supervisor-queue')

  await normal.page.click('[data-testid="cancel-queue"]')
  const cancelled = await seen(notice(normal, '已取消排队。如需帮助，请随时留言。'))
  const closed = await waitSession(admin, normal, (s) => s.status === 'closed')
  check(
    '访客取消排队：AI 未启用时结束会话',
    cancelled && closed?.close_reason === 'visitor_cancel' && !(await banner(normal).isVisible()),
    closed && { status: closed.status, reason: closed.close_reason },
  )
  await normal.page.close()
  return { vip, angry, returning }
}

// ---- 4. 溢出 ----

async function overflowSection(browser, ctx, visitors) {
  const { admin } = ctx
  const bob = await openWorkbench(browser, 'bob')
  // 一线没有人接待（小艾不在线，苏珊小休）：1 分钟后溢出到二线，由小博接待。
  const both = await waitFor(async () => {
    const [vip, angry] = await Promise.all([
      sessionOf(admin, visitors.vip),
      sessionOf(admin, visitors.angry),
    ])
    return vip.assignee_id === ctx.staff.bob && angry.assignee_id === ctx.staff.bob
      ? [vip, angry]
      : null
  }, 100000)
  check(
    '排队 1 分钟后溢出到二线，由小博接待',
    !!both &&
      both.every((s) => s.skill_group_id === ctx.groups['二线'] && s.overflowed_at) &&
      both.every((s) => s.events.some((e) => e.type === 'overflowed')),
    both?.map((s) => ({ group: s.skill_group_id, at: s.overflowed_at })),
  )
  await sessionItem(bob, visitors.angry.name).waitFor({ timeout: 20000 })
  const served = await seen(banner(visitors.angry).filter({ hasText: '客服 小博 正在为您服务' }))
  check('小博的工作台出现溢出过来的会话，访客看到小博在服务', served)
  const alice = await openWorkbench(browser, 'alice')
  await sessionItem(alice, visitors.returning.name).waitFor({ timeout: 20000 })
  const afterSales = await sessionOf(admin, visitors.returning)
  check(
    '小艾上线后接待售后组的客户',
    afterSales.assignee_id === ctx.staff.alice && afterSales.status === 'human_serving',
    afterSales.status,
  )
  return { bob, alice }
}

// ---- 5. 旁听与协助 ----

async function collabSection(ctx, pages, visitors) {
  const { susan, bob, alice } = pages
  const angry = visitors.angry
  await openSession(susan, '进行中', angry.name)
  await susan.click('[data-testid="monitor-session"]')
  const watching = await seen(
    susan.locator('[data-testid="session-watchers"]', { hasText: '旁听：苏珊' }),
  )
  const readonly = await seen(
    susan.locator('[data-testid="chat-readonly"]', { hasText: '旁听中' }),
  )
  await listTab(susan, '接待中')
  const listed = await seen(sessionItem(susan, angry.name).filter({ hasText: '旁听' }))
  const quiet = (await angry.page.locator('[data-testid="message"]', { hasText: '苏珊' }).count()) === 0
  check('主管旁听：只能查看，会话带"旁听"标记出现在"接待中"；客户看不到', watching && readonly && listed && quiet, {
    watching,
    readonly,
    listed,
    quiet,
  })
  await shot(susan, '4-supervisor-monitor')

  await openSession(bob, '接待中', angry.name)
  await bob.click('[data-testid="invite-assist"]')
  await bob.click('[data-testid="assist-agent"]')
  await pickOption(bob, '小艾')
  await bob.click('[data-testid="assist-submit"]')
  const watchers = bob.locator('[data-testid="session-watchers"]')
  const both = await seen(watchers.filter({ hasText: '协助：小艾' }).filter({ hasText: '旁听：苏珊' }))
  const joined = await seen(notice(angry, '客服 小艾 加入了会话。'))
  check('小博邀请小艾协助：客户看到提示，接待坐席看到旁听和协助的同事', both && joined, {
    both,
    joined,
  })
  await shot(bob, '5-agent-assist')

  await openSession(alice, '接待中', angry.name)
  const assisting = await seen(sessionItem(alice, angry.name).filter({ hasText: '协助' }))
  await alice.fill('textarea[data-testid="composer-input"]', '我是小艾，和小博一起帮您处理')
  await alice.click('[data-testid="send-button"]')
  const reply = await seen(
    angry.page.locator('[data-testid="message"].agent', { hasText: '我是小艾' }),
  )
  check('协助的小艾可以直接回复客户', reply)
  await alice.click('[data-testid="leave-session"]')
  const left = await waitFor(async () => {
    const chat = await sessionOf(ctx.admin, angry)
    return chat.watchers.every((w) => w.role !== 'assist') ? chat : null
  })
  const gone = await sessionItem(alice, angry.name)
    .waitFor({ state: 'detached', timeout: 15000 })
    .then(() => true)
    .catch(() => false)
  check('小艾退出协助后，会话从她的"接待中"移除', assisting && !!left && gone, {
    assisting,
    watchers: left?.watchers,
    gone,
  })
}

// ---- 6. 交还 AI、主管转人工、已结束 ----

async function enableAi(ctx) {
  await json(`${API}/api/v1/ai/settings`, {
    method: 'PUT',
    token: ctx.admin,
    body: { enabled: true },
  })
  await json(`${API}/api/v1/routing-policies/${ctx.policyId}`, {
    method: 'PATCH',
    token: ctx.admin,
    body: { mode: 'ai_first' },
  })
}

async function aiSection(ctx, pages, visitors) {
  const { susan, bob } = pages
  const angry = visitors.angry
  await enableAi(ctx)
  await openSession(bob, '接待中', angry.name)
  await bob.click('[data-testid="return-to-ai"]')
  await confirmBox(bob, '交还')
  const back = await seen(notice(angry, '已为您转回智能客服'))
  const aiBanner = await seen(banner(angry).filter({ hasText: '智能客服为您服务' }))
  const chat = await waitSession(ctx.admin, angry, (s) => s.status === 'ai_serving')
  const released = chat?.watchers.length === 0
  const offList = await sessionItem(bob, angry.name)
    .first()
    .waitFor({ state: 'detached', timeout: 15000 })
    .then(() => true)
    .catch(() => false)
  check('小博交还 AI：客户看到提示，旁听的主管一并退出，会话离开小博的列表', back && aiBanner && released && offList, {
    back,
    aiBanner,
    watchers: chat?.watchers,
    offList,
  })
  await say(angry, '快递几天能到')
  const answered = await seen(
    angry.page.locator('[data-testid="message"].bot', { hasText: '2 到 3 天' }),
  )
  check('交还后 AI 继续回答', answered)
  await shot(angry.page, '6-visitor-back-to-ai')

  await openSession(susan, '进行中', angry.name)
  await susan.locator('[data-testid="chat-readonly"]', { hasText: '智能客服接待中' }).waitFor()
  await susan.click('[data-testid="handoff-session"]')
  const handed = await waitSession(ctx.admin, angry, (s) => s.status === 'human_serving')
  check(
    '主管把 AI 接待中的会话转人工，按原技能组分配给小博',
    handed?.handoff_reason === 'supervisor' && handed.assignee_id === ctx.staff.bob,
    handed && { reason: handed.handoff_reason, assignee: handed.assignee_display_name },
  )
  await openSession(bob, '接待中', angry.name)
  const reason = await seen(bob.locator('[data-testid="ai-summary"]', { hasText: '主管转人工' }))
  check('小博的工作台显示转人工原因"主管转人工"', reason)
  await bob.click('[data-testid="close-session"]')
  await confirmBox(bob, '结束')
  await bob.locator('[data-testid="chat-readonly"]', { hasText: '会话已结束' }).waitFor()
  await listTab(bob, '已结束')
  const closedListed = await seen(sessionItem(bob, angry.name).filter({ hasText: '已结束' }))
  check('结束的会话出现在"已结束"里', closedListed)
  await shot(bob, '7-agent-closed-tab')
}

// ---- 7. 排队期间 AI 继续回答、取消排队回到 AI ----

async function aiQueueSection(browser, ctx, pages) {
  await setStatus(pages.alice, '小休')
  await setStatus(pages.bob, '小休')
  const visitor = await openVisitor(browser, ctx.admin, ctx.channelKey, 'ai-queue')
  await say(visitor, '转人工')
  const waiting = await seen(notice(visitor, '排队期间您可以继续提问'))
  const queued = await waitSession(ctx.admin, visitor, (s) => s.status === 'queued')
  check('转人工排队时提示"排队期间可以继续提问"', waiting && !!queued)
  await say(visitor, '快递几天能到')
  const answered = await seen(
    visitor.page.locator('[data-testid="message"].bot', { hasText: '2 到 3 天' }),
  )
  const still = await sessionOf(ctx.admin, visitor)
  const cancelButton = await visitor.page.locator('[data-testid="cancel-queue"]').isVisible()
  check('排队期间 AI 继续回答，会话仍在排队', answered && still.status === 'queued' && cancelButton, {
    answered,
    status: still.status,
    cancelButton,
  })
  await shot(visitor.page, '8-visitor-ai-while-queued')
  await visitor.page.click('[data-testid="cancel-queue"]')
  const resumed = await seen(notice(visitor, '已取消排队，智能客服继续为您服务。'))
  const back = await waitSession(ctx.admin, visitor, (s) => s.status === 'ai_serving')
  const aiBanner = await seen(banner(visitor).filter({ hasText: '智能客服为您服务' }))
  check('访客取消排队后回到 AI 接待', resumed && !!back && aiBanner, { resumed, aiBanner })
}

// ---- 8. 客户转移申请 ----

async function transferRequestSection(browser, ctx, pages, visitors) {
  const { alice } = pages
  const customer = visitors.returning
  await menu(alice, '客户')
  const row = alice.locator('[data-testid="customer-table"] .el-table__row', {
    hasText: customer.name,
  })
  await row.waitFor()
  await row.locator(`[data-testid="customer-more-${customer.customerId}"]`).click()
  await alice.locator('.el-dropdown-menu__item:visible', { hasText: '申请转移' }).click()
  const dialog = alice.locator('[data-testid="transfer-request-dialog"]')
  await dialog.waitFor()
  await dialog.locator('textarea[data-testid="transfer-request-reason"]').fill('客户一直由我跟进售后')
  await dialog.locator('[data-testid="transfer-request-submit"]').click()
  const pending = await seen(
    alice.locator('[data-testid="transfer-requests-pending"]', { hasText: '1' }),
  )
  check('小艾提交客户转移申请（转给自己），"转移申请"显示待审批 1', pending)

  const admin = await newPage(browser, 'admin-customers')
  await consoleLogin(admin, 'admin')
  await menu(admin, '客户')
  await admin.locator('[data-testid="transfer-requests-pending"]', { hasText: '1' }).waitFor()
  await admin.click('[data-testid="transfer-requests-button"]')
  const requestRow = admin.locator('[data-testid="transfer-requests"] .el-table__row', {
    hasText: customer.name,
  })
  await requestRow.waitFor()
  const rowText = await requestRow.innerText()
  await shot(admin, '9-transfer-requests')
  await requestRow.locator('[data-testid="approve-transfer-request"]').click()
  const decision = admin.locator('[data-testid="transfer-decision-dialog"]')
  await decision.locator('input[data-testid="transfer-decision-note"]').fill('同意')
  await decision.locator('[data-testid="transfer-decision-submit"]').click()
  await decision.waitFor({ state: 'hidden' })
  const detail = await waitFor(async () => {
    const c = await json(`${API}/api/v1/customers/${customer.customerId}`, { token: ctx.admin })
    return c.owner_id === ctx.staff.alice ? c : null
  })
  const history = await json(`${API}/api/v1/customers/${customer.customerId}/owner-history`, {
    token: ctx.admin,
  })
  check(
    '管理员批准后客户归属小艾，归属记录原因为"申请审批"',
    rowText.includes('小艾') &&
      rowText.includes('客户一直由我跟进售后') &&
      !!detail &&
      history.items.some((h) => h.reason === 'request' && h.to_owner_name === '小艾'),
    { rowText, owner: detail?.owner_display_name, history: history.items },
  )
  await admin.keyboard.press('Escape')
  const ownerCell = await seen(
    admin
      .locator('[data-testid="customer-table"] .el-table__row', { hasText: customer.name })
      .filter({ hasText: '小艾' }),
  )
  await admin.locator('[data-testid="transfer-requests-pending"]').waitFor({ state: 'detached' })
  check('客户列表显示新的归属坐席，待审批数清零', ownerCell)
  const audit = await json(`${API}/api/v1/audit-logs?action=customer&limit=50`, {
    token: ctx.admin,
  })
  const actions = audit.items.map((a) => a.action)
  check(
    '操作日志记录申请和批准',
    actions.includes('customer.transfer_request') && actions.includes('customer.transfer_approve'),
    actions,
  )
}

async function run(browser) {
  const ctx = await prepareTenant()
  const admin = await newPage(browser, 'admin')
  await consoleLogin(admin, 'admin')
  await settingsSection(admin, ctx)

  // 主管先进入工作台再改成小休：她是三个技能组的组长，但不参与这次分配，只看排队、旁听、转人工。
  const susan = await openWorkbench(browser, 'susan')
  await setStatus(susan, '小休')
  const visitors = await queueSection(browser, ctx, susan)
  const { bob, alice } = await overflowSection(browser, ctx, visitors)
  const pages = { susan, bob, alice }
  await collabSection(ctx, pages, visitors)
  await aiSection(ctx, pages, visitors)
  await aiQueueSection(browser, ctx, pages)
  await transferRequestSection(browser, ctx, pages, visitors)
  check('没有前端脚本错误', summary.consoleErrors.length === 0, summary.consoleErrors)
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
