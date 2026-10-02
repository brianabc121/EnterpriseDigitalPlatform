// P22 验收：AI 唤醒——数据巡检、知识库整理和增量更新索引（设计文档 §33），在浏览器里走通。
//
// 1. 准备：库存不足的门铃、低于成本价成交的订单、小艾手上两条到期的待办、和制度冲突的问答、一对重复的问答。
// 2. 管理员打开"AI 唤醒"：设置里把"逾期待办积压"改成 2 条；"立即巡检"后看到 3 个问题（库存不足、低于成本价、
//    逾期待办积压）和 AI 简报；唤醒记录写明检查了 18 项。
// 3. 增量更新索引：命令行按定时唤醒的方式再巡检一次——数据没有变化，18 项全部跳过；补了门铃的库存后再巡检，
//    只有读商品表的"库存不足"重新检查，问题自动消除；"增量更新索引"页签显示商品表最近的变化。
// 4. 小艾的首页显示"需要我处理的问题"，她忽略 7 天后首页不再显示；管理员按"已忽略"能看到原因。
// 5. 知识库：新建文档时勾选"规章制度"，列表显示"制度"标签；"制度对齐"立即整理后报告有冲突、建议新增和重复；
//    去审核台（来源"制度对齐"）按制度更新冲突的答案、合并重复的问答（另一条下线）。
//
// 前置：与 p3-ai-acceptance.cjs 相同（后端、实时消费进程、调度进程接到模拟大模型 :8900）。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<密码> node scripts/e2e/p22-wake-acceptance.cjs
const { chromium } = require('playwright')
const { execFile } = require('child_process')
const { promisify } = require('util')
const fs = require('fs')
const path = require('path')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p22')
const BACKEND_DIR = env('BACKEND_DIR', path.resolve(__dirname, '../../backend'))
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `wake-${RUN}`
const PASSWORD = 'demo-pass-2026'
const POLICY = [
  '# 售后服务制度',
  '## 退货',
  '退货期限：自签收之日起 15 天内可以申请无理由退货，退回运费由公司承担。',
  '## 换货',
  '换货期限：收到商品 30 天内出现质量问题可以换货。',
].join('\n')

const summary = { tenant: TENANT, checks: [], consoleErrors: [] }
const check = (name, ok, detail) =>
  summary.checks.push(
    `${ok ? 'PASS' : 'FAIL'} ${name}${ok || detail === undefined ? '' : ` -> ${JSON.stringify(detail)}`}`,
  )
const state = {}

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

/** 后端命令行（异步执行，见 g5-ai-knowledge.cjs）。 */
async function cli(...args) {
  const { stdout } = await promisify(execFile)('uv', ['run', 'python', '-m', 'app.cli', ...args], {
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
    last = await probe()
    if (last) return last
    await sleep(1000)
  }
  throw new Error(`timed out waiting for ${label}`)
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
      name: `唤醒验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  const admin = await login('admin')
  state.admin = admin
  // 验收期间关闭定期整理：定时的知识库整理会被跳过，下面"立即整理"的报告才是第一次整理的结果。
  const settings = await json(`${API}/api/v1/wake/settings`, { token: admin })
  await json(`${API}/api/v1/wake/settings`, {
    method: 'PUT',
    token: admin,
    body: { ...settings.settings, kb_enabled: false },
  })
  const alice = await json(`${API}/api/v1/staff`, {
    method: 'POST',
    token: admin,
    body: { username: 'alice', display_name: '小艾', password: PASSWORD, role_codes: ['agent'] },
  })
  state.aliceId = alice.id

  // 商品：门锁（成本 800），门铃（预警值 5，现有 2：库存不足）。
  const lock = await json(`${API}/api/v1/products`, {
    method: 'POST',
    token: admin,
    body: { code: 'LOCK-X1', name: '智能门锁 X1', retail_price: '1299', cost_price: '800' },
  })
  const bell = await json(`${API}/api/v1/products`, {
    method: 'POST',
    token: admin,
    body: { code: 'BELL-D1', name: '可视门铃 D1', retail_price: '199', cost_price: '90', stock_alert: 5 },
  })
  await json(`${API}/api/v1/products/${bell.id}/stock`, {
    method: 'POST',
    token: admin,
    body: { mode: 'set', quantity: 2 },
  })
  state.bellId = bell.id

  // 低于成本价成交的订单：门锁按 600 卖出并确认。
  const customer = await json(`${API}/api/v1/customers`, {
    method: 'POST',
    token: admin,
    body: { display_name: '李女士' },
  })
  const order = await json(`${API}/api/v1/orders`, {
    method: 'POST',
    token: admin,
    body: {
      customer_id: customer.id,
      items: [{ product_id: lock.id, quantity: 1, unit_price: '600' }],
      receiver: { name: '李女士', phone: '13800001111', address: '上海市浦东新区世纪大道 100 号' },
    },
  })
  await json(`${API}/api/v1/orders/${order.id}/confirm`, {
    method: 'POST',
    token: admin,
    body: { payment_method: 'cod', notify_customer: false },
  })
  state.orderNo = order.no

  // 小艾手上两条马上到期的待办（几秒后逾期）。
  const types = await json(`${API}/api/v1/todo-types`, { token: admin })
  const callback = types.items.find((t) => t.code === 'callback') ?? types.items[0]
  const due = new Date(Date.now() + 3000).toISOString()
  for (const title of ['回电李女士确认安装时间', '回电王先生确认地址']) {
    await json(`${API}/api/v1/todos`, {
      method: 'POST',
      token: admin,
      body: { type_id: callback.id, title, assignee_id: alice.id, due_at: due },
    })
  }

  // 知识：和制度冲突的问答（7 天，制度是 15 天），一对重复的问答。
  const faq = (title, content, extra = {}) =>
    json(`${API}/api/v1/kb/items`, {
      method: 'POST',
      token: admin,
      body: { kind: 'faq', title, content, publish: true, ...extra },
    })
  state.returnFaq = await faq('退货期限是多久？', '签收后 7 天内可以退货，运费自理。')
  state.invoice = await faq('发票怎么开？', '下单时填写抬头和税号，发货后开具。')
  state.invoice2 = await faq('发票怎么开', '联系客服提供抬头和税号。', { questions: ['可以开专票吗'] })
  await sleep(3500)
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

async function tab(page, container, label) {
  await page.locator(`[data-testid="${container}"] .el-tabs__item:has-text("${label}")`).first().click()
}

async function findingsByCheck(page) {
  await page.waitForSelector('[data-testid="wake-findings"]')
  return page.$$eval('[data-testid="wake-finding"]', (rows) =>
    Object.fromEntries(
      rows.map((r) => [
        r.getAttribute('data-check'),
        r.querySelector('[data-testid="wake-finding-title"]')?.textContent.trim(),
      ]),
    ),
  )
}

async function wakeAsAdmin(browser) {
  const { ctx, page } = await consoleLogin(browser, 'admin')
  const menu = await page.locator('[data-testid="main-menu"]').innerText()
  check('admin sees the AI 唤醒 menu', menu.includes('AI 唤醒'), menu)
  await page.goto(`${CONSOLE}/wake`)
  await page.waitForSelector('[data-testid="wake-overview"]')

  // 设置：逾期待办积压改成 2 条，检查项旁边显示读的表。
  await tab(page, 'wake-tabs', '设置')
  await page.waitForSelector('[data-testid="wake-check-todo_overdue"]')
  const overdueCheck = await page.locator('[data-testid="wake-check-todo_overdue"]').innerText()
  check('settings list the tables each check reads', overdueCheck.includes('读：todos'), overdueCheck)
  const input = page.locator('[data-testid="wake-param-todo_overdue-count"] input')
  await input.fill('2')
  await input.press('Tab')
  await page.click('[data-testid="wake-settings-save"]')
  await page.waitForSelector('.el-message--success:has-text("已保存")')
  const saved = await json(`${API}/api/v1/wake/settings`, { token: state.admin })
  check(
    'only the changed number is saved',
    JSON.stringify(saved.settings.checks) === JSON.stringify({ todo_overdue: { enabled: true, params: { count: 2 } } }),
    saved.settings.checks,
  )
  await page.screenshot({ path: `${SHOTS}/p22-01-wake-settings.png`, fullPage: true })

  // 新企业开通后调度进程会登记这个时段的定时唤醒（全天服务时每小时检查一定到期）：等它们执行完，
  // 下面命令行的两次巡检之间就不会插进别的唤醒。
  await until(
    'the scheduled wake-ups of the new tenant',
    async () => {
      const runs = await json(`${API}/api/v1/wake/runs`, { token: state.admin })
      const scheduled = runs.items.filter((r) => r.trigger === 'schedule')
      return scheduled.length && runs.items.every((r) => !['queued', 'running'].includes(r.status))
    },
    180_000,
  )

  // 立即巡检：AI 醒来，全部检查项重新检查，发现 3 个问题并写简报。
  await tab(page, 'wake-tabs', '问题')
  await page.click('[data-testid="wake-run-daily"]')
  await page.waitForSelector('.el-message--success:has-text("AI 已经醒来")')
  const overview = await until('the manual inspection', async () => {
    const data = await json(`${API}/api/v1/wake/overview`, { token: state.admin })
    return !data.pending.length && data.brief ? data : null
  })
  check('brief written after the inspection', Boolean(overview.brief.summary), overview.brief)
  await page.waitForFunction(() => document.querySelectorAll('[data-testid="wake-finding"]').length >= 3, null, {
    timeout: 20_000,
  })
  await page.waitForSelector('[data-testid="wake-pending"]', { state: 'detached', timeout: 20_000 })
  const found = await findingsByCheck(page)
  check(
    'three problems found',
    found.stock_low === '成品「可视门铃 D1」库存不足' &&
      found.order_below_cost === `订单 ${state.orderNo} 低于成本价成交` &&
      found.todo_overdue === '小艾 有 2 条待办已逾期',
    found,
  )
  const brief = await page.locator('[data-testid="wake-brief-text"]').innerText()
  check('AI brief shown on the page', brief.includes('3 个问题待处理'), brief)
  const warning = await page.locator('[data-testid="wake-open-warning"]').innerText()
  check('open counts by severity', warning.replace(/\s+/g, '') === '3注意', warning)
  await page.screenshot({ path: `${SHOTS}/p22-02-wake-findings.png`, fullPage: true })

  // 唤醒记录：立即唤醒全部检查。
  await tab(page, 'wake-tabs', '唤醒记录')
  await page.waitForSelector('[data-testid="wake-run-summary"]')
  const firstRun = await page.locator('[data-testid="wake-run-summary"]').first().innerText()
  check('manual run checked all 18 checks', /^检查 18 项，.*待处理 3 个/.test(firstRun), firstRun)

  // 增量更新索引：按定时唤醒的方式再巡检一次——数据没有变化，18 项全部跳过。
  const idle = await cli('wake-run', TENANT, '--kind', 'daily')
  check('unchanged data: every check skipped', idle.stats.ran === 0 && idle.stats.skipped === 18, idle.stats)
  // 补了门铃的库存：只有读商品表的检查项重新检查，库存不足的问题自动消除。
  await json(`${API}/api/v1/products/${state.bellId}/stock`, {
    method: 'POST',
    token: state.admin,
    body: { mode: 'set', quantity: 20 },
  })
  const restocked = await cli('wake-run', TENANT, '--kind', 'daily')
  check(
    'only the stock check ran after the restock',
    restocked.stats.ran === 1 && restocked.stats.skipped === 17 && restocked.stats.resolved === 1,
    restocked.stats,
  )
  await page.reload()
  await tab(page, 'wake-tabs', '唤醒记录')
  await page.waitForSelector('[data-testid="wake-run-summary"]')
  const runs = await page.locator('[data-testid="wake-run-summary"]').allInnerTexts()
  check(
    'run log shows the skipped checks',
    runs[0].startsWith('检查 18 项（17 项数据没有变化，直接跳过），已消除 1 个，待处理 2 个') &&
      runs[1].startsWith('检查 18 项（18 项数据没有变化，直接跳过），待处理 3 个'),
    runs.slice(0, 3),
  )
  await page.screenshot({ path: `${SHOTS}/p22-03-wake-runs.png`, fullPage: true })
  await tab(page, 'wake-tabs', '增量更新索引')
  await page.waitForSelector('[data-testid="wake-data-index"] .el-table__row')
  const rows = await page.locator('[data-testid="wake-data-index"] .el-table__row').allInnerTexts()
  check(
    'change index lists the changed tables, newest first',
    rows.length >= 6 && rows.some((r) => r.includes('商品和材料')) && !rows.some((r) => /^[a-z_]+\s/.test(r)),
    rows.slice(0, 6),
  )
  await page.screenshot({ path: `${SHOTS}/p22-04-change-index.png`, fullPage: true })
  return { ctx, page }
}

async function aliceIgnores(browser) {
  const { ctx, page } = await consoleLogin(browser, 'alice')
  await page.goto(`${CONSOLE}/`)
  await page.waitForSelector('[data-testid="home-wake"]')
  const section = await page.locator('[data-testid="home-wake"]').innerText()
  check(
    "alice's home lists the problem assigned to her",
    section.includes('需要我处理的问题（1）') && section.includes('小艾 有 2 条待办已逾期'),
    section,
  )
  await page.screenshot({ path: `${SHOTS}/p22-05-home-mine.png`, fullPage: true })
  await page.click('[data-testid="home-wake"] [data-testid="wake-ignore"]')
  await page.locator('[data-testid="wake-ignore-7"]').click()
  await page.waitForSelector('.el-message-box')
  await page.fill('.el-message-box input', '本周排班已调整')
  await page.click('.el-message-box button:has-text("忽略")')
  await page.waitForSelector('.el-message--success:has-text("已忽略")')
  await page.waitForSelector('[data-testid="home-wake"]', { state: 'detached', timeout: 10_000 })
  check('ignored problem leaves the home page', true)
  await ctx.close()
}

async function adminSeesIgnored(page) {
  await page.goto(`${CONSOLE}/wake`)
  await page.waitForSelector('[data-testid="wake-findings"]')
  await page.locator('[data-testid="wake-status-filter"] label:has-text("已忽略")').click()
  await page.waitForFunction(
    () => document.querySelectorAll('[data-testid="wake-finding"][data-check="todo_overdue"]').length === 1,
  )
  const ignored = await page.locator('[data-testid="wake-finding"][data-check="todo_overdue"]').innerText()
  check('admin sees the ignore note', ignored.includes('本周排班已调整') && ignored.includes('忽略到'), ignored)
}

async function knowledge(page) {
  await page.goto(`${CONSOLE}/knowledge`)
  await page.waitForSelector('[data-testid="kb-table"]')
  // 新建文档时勾选"规章制度"。
  await page.click('[data-testid="kb-create"]')
  await page.locator('[data-testid="kb-create-doc"]').click()
  await page.locator('input[data-testid="kb-title"]').fill('售后服务制度')
  await page.locator('textarea[data-testid="kb-content"]').fill(POLICY)
  await page.locator('[data-testid="kb-policy"]').click()
  await page.click('[data-testid="kb-save-publish"]')
  await page.waitForSelector('.el-message--success')
  await page.locator('[data-testid="kb-policy-filter"]').click()
  await page.waitForFunction(() => document.querySelectorAll('[data-testid="kb-table"] .el-table__row').length === 1)
  const row = await page.locator('[data-testid="kb-table"] .el-table__row').first().innerText()
  check('policy listed with the 制度 tag', row.includes('售后服务制度') && row.includes('制度'), row)
  await page.screenshot({ path: `${SHOTS}/p22-06-policy.png`, fullPage: true })

  // 制度对齐：立即整理（和"制度变化后自动整理"排队的那一条合并，马上开始）。
  await tab(page, 'kb-tabs', '制度对齐')
  await page.waitForSelector('[data-testid="kb-alignment"]')
  await page.click('[data-testid="kb-alignment-run"]')
  const report = await until('the alignment', async () => {
    const data = await json(`${API}/api/v1/kb/alignment`, { token: state.admin })
    return data.finished_at && !data.running ? data : null
  })
  check(
    'alignment report: conflict, gap and duplicate',
    report.report.conflict === 1 && report.report.gap >= 1 && report.report.duplicate === 1,
    report.report,
  )
  await page.waitForFunction(
    () => !document.querySelector('[data-testid="kb-alignment-running"]'),
    null,
    { timeout: 20_000 },
  )
  const pending = Number(await page.locator('[data-testid="kb-alignment-pending"]').innerText())
  check('pending suggestions shown', pending >= 3, pending)
  const text = await page.locator('[data-testid="kb-alignment-summary"]').innerText()
  check('report summary on the page', text.includes('现行制度 1 份') && text.includes('冲突 1'), text)
  await page.screenshot({ path: `${SHOTS}/p22-07-alignment.png`, fullPage: true })

  // 去审核台：来源"制度对齐"。
  await page.click('[data-testid="kb-alignment-review"]')
  await page.waitForSelector('[data-testid="candidates-table"] .el-table__row')
  const rows = await page.locator('[data-testid="candidates-table"] .el-table__row').allInnerTexts()
  check('review desk filtered to 制度对齐', rows.length >= 3 && rows.every((r) => r.includes('制度对齐')), rows)
  await page.locator('[data-testid="candidates-table"] .el-table__row', { hasText: '退货期限是多久' }).first().click()
  await page.waitForSelector('[data-testid="evidence-policy"]')
  const evidence = await page.locator('[data-testid="candidate-drawer"]').innerText()
  check(
    'conflict shows the policy clause and the diff',
    evidence.includes('《售后服务制度》') && evidence.includes('15 天') && evidence.includes('知识里是 7 天'),
    evidence.slice(0, 400),
  )
  await page.waitForTimeout(500) // 抽屉滑入的动画结束后再截图
  await page.screenshot({ path: `${SHOTS}/p22-08-conflict.png`, fullPage: true })
  await page.click('[data-testid="approve-candidate"]')
  await page.waitForSelector('.el-message--success')
  await page.waitForSelector('[data-testid="candidate-drawer"]', { state: 'hidden' })
  await page.locator('[data-testid="candidates-table"] .el-table__row', { hasText: '重复' }).first().click()
  await page.waitForSelector('[data-testid="evidence-duplicate"]')
  await page.click('[data-testid="approve-candidate"]')
  await page.waitForSelector('.el-message--success:has-text("已合并")')
  const fixed = await json(`${API}/api/v1/kb/items/${state.returnFaq.id}`, { token: state.admin })
  check('conflicting answer updated from the policy', fixed.content === '签收后 15 天内可以退货，运费自理。', fixed.content)
  const [first, second] = await Promise.all(
    [state.invoice.id, state.invoice2.id].map((id) => json(`${API}/api/v1/kb/items/${id}`, { token: state.admin })),
  )
  check(
    'duplicate merged and archived',
    first.status === 'published' && second.status === 'archived' && first.questions.includes('可以开专票吗'),
    { first: first.status, second: second.status, questions: first.questions },
  )
}

;(async () => {
  fs.mkdirSync(SHOTS, { recursive: true })
  await prepareTenant()
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined })
  try {
    const { ctx, page } = await wakeAsAdmin(browser)
    await aliceIgnores(browser)
    await adminSeesIgnored(page)
    await knowledge(page)
    await ctx.close()
  } catch (error) {
    summary.checks.push(`FAIL exception -> ${error.stack || error}`)
  } finally {
    await browser.close()
  }
  check('no console errors', summary.consoleErrors.length === 0, summary.consoleErrors)
  fs.writeFileSync(`${SHOTS}/summary.json`, JSON.stringify(summary, null, 2))
  for (const line of summary.checks) console.log(line)
  const failed = summary.checks.filter((line) => line.startsWith('FAIL'))
  console.log(failed.length ? `${failed.length} FAILED` : `ALL ${summary.checks.length} PASSED`)
  process.exit(failed.length ? 1 : 0)
})()
