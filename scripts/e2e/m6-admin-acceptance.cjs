// P1 M6 验收：管理员在控制台完成配置，报表、用量和运营后台数据正确。
//
// 1. 管理员在"设置"里新建技能组（含组长）、两套路由策略（其中一套带工作时间）、把渠道绑定到
//    "分配到技能组"的策略，并把小艾的最多同时接待改成 1。
// 2. 两位访客先后咨询：按技能组和并发上限分别分给小艾、小博。小艾回复并结束，访客评价；
//    第三位访客留言（生成"留言"类待办），管理员在"待办"页处理。
// 3. 汇总用量后检查：会话记录（对话与过程）、用量页、报表、首页实时数据、运营后台的租户用量。
//
// 前置与 m4-workbench-acceptance.cjs 相同（后端、实时消费进程、调度进程、控制台、运营后台、Widget、
// OpenIM）。用量汇总默认执行 `cd backend && uv run python -m app.cli usage-rollup`（ROLLUP_CMD 可替换）。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/m6-admin-acceptance.cjs
const { chromium } = require('playwright')
const { execSync } = require('child_process')
const fs = require('fs')
const path = require('path')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PLATFORM = env('PLATFORM_URL', 'http://localhost:5174')
const WIDGET = env('WIDGET_URL', 'http://localhost:5175')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const ROLLUP_CMD = env('ROLLUP_CMD', 'cd backend && uv run python -m app.cli usage-rollup')
const SHOTS = env('SHOTS', 'e2e-shots/m6')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `ad-${RUN}`
const PASSWORD = 'demo-pass-2026'
const ROOT = path.resolve(__dirname, '../..')

const summary = { tenant: TENANT, checks: [], consoleErrors: [] }
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
  const data = await json(`${API}/api/v1/auth/login`, {
    method: 'POST',
    body: { tenant_code: TENANT, username, password: PASSWORD },
  })
  return data.access_token
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
      name: `管理验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  const admin = await login('admin')
  const ids = {}
  for (const [username, display_name] of [
    ['alice', '小艾'],
    ['bob', '小博'],
  ]) {
    const staff = await json(`${API}/api/v1/staff`, {
      method: 'POST',
      token: admin,
      body: { username, display_name, password: PASSWORD, role_codes: ['agent'] },
    })
    ids[username] = staff.id
  }
  const channels = await json(`${API}/api/v1/channels`, { token: admin })
  return { admin, ids, channel: channels.items[0] }
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
const tab = (page, title) => page.locator('.el-tabs__item', { hasText: title }).click()
const option = (page, text) =>
  page.locator('.el-select-dropdown__item:visible', { hasText: text }).first().click()
/** 等到表格里出现这一行（保存后列表会重新加载）。 */
const rowIn = (page, table, text) =>
  page.locator(`[data-testid="${table}"] .el-table__row`, { hasText: text }).first().waitFor()

async function openVisitor(browser, channelKey, label) {
  const ctx = await browser.newContext({ viewport: { width: 420, height: 720 }, locale: 'zh-CN' })
  const page = await ctx.newPage()
  watchErrors(page, label)
  await page.goto(`${WIDGET}/?key=${encodeURIComponent(channelKey)}`)
  await page.locator('[data-testid="widget-state"]', { hasText: '在线' }).waitFor({ timeout: 15000 })
  return { ctx, page }
}

async function say(visitor, text) {
  await visitor.page.fill('[data-testid="message-input"]', text)
  await visitor.page.click('[data-testid="send-button"]')
}

/** 截图前等弹窗、抽屉的动画结束。 */
const settle = (page) => page.waitForTimeout(600)

const tileValue = (page, testid) =>
  page.locator(`[data-testid="${testid}"] .value`).innerText().then((t) => t.trim())

async function waitFor(fn, timeout = 15000) {
  const deadline = Date.now() + timeout
  for (;;) {
    const value = await fn()
    if (value || Date.now() > deadline) return value
    await new Promise((r) => setTimeout(r, 500))
  }
}

async function run(browser) {
  const { admin: adminToken, ids, channel } = await prepareTenant()
  const admin = await consoleLogin(browser, 'admin')
  const page = admin.page

  // 1. 技能组：售前（小艾、小博，小博为组长）
  await menu(page, '设置')
  await tab(page, '技能组')
  await page.click('[data-testid="new-group"]')
  await page.fill('input[data-testid="group-name"]', '售前')
  await page.locator('.el-dialog [data-testid="group-members"]').click()
  await option(page, '小艾')
  await option(page, '小博')
  await page.keyboard.press('Escape')
  await page.locator('.el-dialog .el-checkbox', { hasText: '小博' }).click()
  await page.click('[data-testid="save-group"]')
  await rowIn(page, 'groups-table', '售前')
  const groupRow = await page.locator('[data-testid="groups-table"] .el-table__row').first().innerText()
  check('新建技能组：成员与组长', groupRow.includes('售前') && groupRow.includes('小博（组长）'), groupRow)

  // 2. 路由策略：一套带工作时间（周一至周六），一套分配到售前组、全天服务
  await tab(page, '路由策略')
  await page.click('[data-testid="new-policy"]')
  await page.fill('input[data-testid="policy-name"]', '工作日')
  await page.locator('[data-testid="business-hours"] .el-radio', { hasText: '按工作时间' }).click()
  await page.locator('[data-testid="weekday-6"]').click()
  await page.click('[data-testid="save-policy"]')
  await rowIn(page, 'policies-table', '工作日')
  await page.click('[data-testid="new-policy"]')
  await page.fill('input[data-testid="policy-name"]', '售前全天')
  await page.locator('.el-dialog .el-select', { hasText: '不限' }).click()
  await option(page, '售前')
  await page.click('[data-testid="save-policy"]')
  await rowIn(page, 'policies-table', '售前全天')
  const policies = await page.locator('[data-testid="policies-table"]').innerText()
  check(
    '新建路由策略：工作时间与技能组',
    policies.includes('周一至周六 09:00-18:00') && policies.includes('技能组：售前'),
    policies,
  )
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/1-routing-policies.png` })

  // 3. 渠道改用"售前全天"策略
  await tab(page, '接入渠道')
  await page.click('[data-testid="edit-channel-官网"]')
  await page.locator('[data-testid="channel-editor"] .el-select', { hasText: '租户默认策略' }).click()
  await option(page, '售前全天')
  await page.click('[data-testid="save-channel"]')
  const policyList = await json(`${API}/api/v1/routing-policies`, { token: adminToken })
  const salesPolicy = policyList.items.find((p) => p.name === '售前全天')
  const boundChannel = await waitFor(async () => {
    const [current] = (await json(`${API}/api/v1/channels`, { token: adminToken })).items
    return current.routing_policy_id === salesPolicy?.id ? current : null
  })
  await page.keyboard.press('Escape')
  check('渠道绑定到"售前全天"策略', Boolean(boundChannel), boundChannel)

  // 4. 小艾最多同时接待 1 个会话
  await tab(page, '坐席')
  const concurrency = page.locator(
    'input[data-testid="concurrency-alice"], [data-testid="concurrency-alice"] input',
  )
  await concurrency.fill('1')
  await concurrency.press('Enter')
  await page.locator('.el-message--success', { hasText: '最多同时接待 1' }).waitFor()
  const board = await json(`${API}/api/v1/agents`, { token: adminToken })
  const aliceBoard = board.items.find((a) => a.username === 'alice')
  check('坐席并发上限改为 1', aliceBoard?.max_concurrency === 1, aliceBoard)

  // 5. 坐席上线；两位访客咨询：小艾满 1 个后分给小博
  const agents = {}
  for (const username of ['alice', 'bob']) {
    const session = await consoleLogin(browser, username)
    await menu(session.page, '工作台')
    await session.page
      .locator('[data-testid="agent-status"]', { hasText: '在线' })
      .waitFor({ timeout: 15000 })
    agents[username] = session
  }
  const first = await openVisitor(browser, channel.public_key, 'visitor-1')
  await say(first, '咨询一：想了解报价')
  await agents.alice.page.locator('[data-testid="session-item"]').first().waitFor({ timeout: 15000 })
  const second = await openVisitor(browser, channel.public_key, 'visitor-2')
  await say(second, '咨询二：怎么开发票')
  await agents.bob.page.locator('[data-testid="session-item"]').first().waitFor({ timeout: 15000 })
  const open = await json(`${API}/api/v1/sessions?status=open`, { token: adminToken })
  const groupId = (await json(`${API}/api/v1/skill-groups`, { token: adminToken })).items[0].id
  const assignees = open.items.map((s) => s.assignee_display_name).sort()
  check(
    '按技能组与并发上限分配：小艾、小博各一个',
    JSON.stringify(assignees) === JSON.stringify(['小博', '小艾'].sort()) &&
      open.items.every((s) => s.skill_group_id === groupId),
    open.items.map((s) => [s.assignee_display_name, s.skill_group_id]),
  )

  // 6. 小艾回复并结束，访客评 5 分；第三位访客留言
  const alicePage = agents.alice.page
  await alicePage.locator('[data-testid="session-item"]').first().click()
  await alicePage.fill('textarea[data-testid="composer-input"]', '您好，报价单稍后发您')
  await alicePage.click('[data-testid="send-button"]')
  await first.page.locator('[data-testid="message"].agent', { hasText: '报价单' }).waitFor()
  await alicePage.click('[data-testid="close-session"]')
  await alicePage.locator('.el-message-box button', { hasText: '结束' }).click()
  await first.page.locator('[data-testid="csat"]').waitFor({ timeout: 20000 })
  await first.page.click('[data-testid="csat-5"]')
  await first.page.click('[data-testid="csat-submit"]')
  await first.page.locator('[data-testid="csat-thanks"]').waitFor()
  const third = await openVisitor(browser, channel.public_key, 'visitor-3')
  await third.page.click('[data-testid="leave-message-tab"]')
  await third.page.locator('[data-testid="leave-message"] textarea').fill('周末能送货吗？')
  await third.page.locator('[data-testid="leave-message"] input').fill('13900000000')
  await third.page.locator('[data-testid="leave-message"] button[type="submit"]').click()
  await third.page.locator('[data-testid="leave-thanks"]').waitFor()

  // 7. 留言：管理员在待办里处理
  await menu(page, '待办')
  const todoRow = page.locator('[data-testid="todos-table"] .el-table__row', {
    hasText: '周末能送货吗',
  })
  await todoRow.waitFor({ timeout: 15000 })
  await todoRow.locator('[data-testid="complete-todo"]').click()
  await page.locator('.el-message--success', { hasText: '已处理' }).waitFor()
  await page.locator('[data-testid="todo-status-filter"] .el-radio-button', { hasText: '已完成' }).click()
  await todoRow.waitFor()
  check('留言在"待办"页处理完成', true)

  // 8. 会话记录：已结束的会话，查看对话与过程
  await menu(page, '会话记录')
  await page.locator('[data-testid="session-status-filter"] .el-radio-button', { hasText: '已结束' }).click()
  await page.locator('[data-testid="sessions-table"] .el-table__row', { hasText: '小艾' }).first().click()
  const transcript = page.locator('[data-testid="session-transcript"]')
  await transcript.locator('text=报价单稍后发您').waitFor({ timeout: 15000 })
  const drawer = await page.locator('[data-testid="session-drawer"]').innerText()
  check(
    '会话记录：对话、满意度和分配过程',
    drawer.includes('咨询一') && drawer.includes('分配坐席') && drawer.includes('会话结束'),
    drawer.slice(0, 300),
  )
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/2-session-record.png` })
  await page.keyboard.press('Escape')

  // 9. 汇总用量，检查用量页与报表
  execSync(ROLLUP_CMD, { cwd: ROOT, stdio: 'pipe', shell: '/bin/bash' })
  await menu(page, '设置')
  await tab(page, '用量')
  await page.locator('[data-testid="usage-messages_in"]').waitFor()
  const usage = {
    messagesIn: await tileValue(page, 'usage-messages_in'),
    agentMessages: await tileValue(page, 'usage-agent_messages'),
    seats: await tileValue(page, 'usage-seats'),
  }
  check(
    '用量按日汇总：客户消息 2、坐席消息 1、员工账号 3',
    usage.messagesIn === '2' && usage.agentMessages === '1' && usage.seats === '3',
    usage,
  )
  await page.screenshot({ path: `${SHOTS}/3-usage.png` })

  await menu(page, '报表')
  await page.locator('[data-testid="tile-sessions"]').waitFor()
  const report = {
    sessions: await tileValue(page, 'tile-sessions'),
    csat: await tileValue(page, 'tile-csat'),
    agents: await page.locator('[data-testid="report-agents"]').innerText(),
  }
  check(
    '报表：会话 2、满意度 5.0、两位坐席',
    report.sessions === '2' &&
      report.csat === '5.0 分' &&
      report.agents.includes('小艾') &&
      report.agents.includes('小博'),
    report,
  )
  await page.screenshot({ path: `${SHOTS}/4-reports.png`, fullPage: true })

  await menu(page, '首页')
  await page.locator('[data-testid="rt-serving"]').waitFor()
  const live = {
    serving: await tileValue(page, 'rt-serving'),
    today: await tileValue(page, 'rt-today'),
    queued: await tileValue(page, 'rt-queued'),
  }
  check('首页实时数据：接待中 1、今日会话 2、排队 0', live.serving === '1' && live.today === '2' && live.queued === '0', live)
  await page.screenshot({ path: `${SHOTS}/5-dashboard.png` })

  // 10. 运营后台：租户列表的用量列与用量明细
  const ops = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' })
  const opsPage = await ops.newPage()
  watchErrors(opsPage, 'platform')
  await opsPage.goto(`${PLATFORM}/login`)
  await opsPage.fill('input[autocomplete="username"]', PLATFORM_USER)
  await opsPage.fill('input[autocomplete="current-password"]', PLATFORM_PASSWORD)
  await opsPage.click('button:has-text("登录")')
  const row = opsPage.locator('[data-testid="tenant-table"] .el-table__row', { hasText: TENANT })
  await row.waitFor({ timeout: 15000 })
  const headers = await opsPage.locator('[data-testid="tenant-table"] thead th').allInnerTexts()
  const cells = await row.locator('td').allInnerTexts()
  const cell = (title) => cells[headers.findIndex((h) => h.trim() === title)]?.trim()
  check(
    '运营后台显示租户用量：员工账号 3、近 30 天客户消息 2',
    cell('员工账号') === '3' && cell('近 30 天客户消息') === '2',
    { headers, cells },
  )
  await row.locator('[data-testid="tenant-usage-button"]').click()
  await opsPage.locator('[data-testid="tenant-usage"] .el-table__row').first().waitFor()
  await settle(opsPage)
  await opsPage.screenshot({ path: `${SHOTS}/6-platform-usage.png` })

  check('没有前端脚本错误', summary.consoleErrors.length === 0, summary.consoleErrors)
  for (const ctx of [admin.ctx, agents.alice.ctx, agents.bob.ctx, first.ctx, second.ctx, third.ctx, ops]) {
    await ctx.close()
  }
  return ids
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
