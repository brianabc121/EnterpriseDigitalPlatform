// P28 验收：商机（设计文档 §40，原意向客户 §35）——员工转入、看板拖拽换阶段、跟进、AI 写跟进话术、访客咨询后
// AI 转入、客户又来咨询、订单确认后自动赢单、只建议时确认 AI 的建议、"商机该跟进了"的提醒、输单和重新跟进、坐席的
// 查看范围，在浏览器里走通。
//
// 1. 准备：知识"智能门锁国庆活动"、商品、客户"王先生"（归管理员）；坐席小艾；平台的"意图判断"路由到
//    判断模型（接到模拟服务，结束时删除）。
// 2. 管理员在客户资料里把王先生转入商机（意向高、预计金额），客户列表标阶段"新线索"；"商机看板"：卡片在"新线索"
//    列，拖到"已沟通"、菜单"移到…"到"已报价"，拖到"赢单"弹出确认；列表里的数字和一行；商机详情里 AI 写跟进话术
//    （参考知识库）、记一次跟进、把下次跟进改成今天。
// 3. 访客咨询智能门锁（意向明确、嫌贵、下周再说），小艾接待后结束会话：AI 转入商机（来源 AI，想要什么、顾虑、
//    7 天后跟进，负责人小艾），依据的会话可以点开；访客又来说要下单：记"客户又来咨询了"、等级调高；这个客户的
//    订单确认后自动赢单，"本月赢单"加一。
// 4. 商机设置改成"只建议"：另一位访客咨询后进"待确认"，管理员确认转入。
// 5. AI 唤醒的每日巡检提醒"管理员 有 1 条商机该跟进了"，提醒的链接打开今天该跟进的看板。
// 6. 输单（选原因、写说明）和重新跟进（回到已沟通）。
// 7. 小艾有"商机"菜单，只看到自己负责的商机（客户不归她也能看到），没有商机设置。
// 8. 管理员首页的商机数字（进行中、本周要跟进、停滞、本月赢单金额）、报表的"销售"页签、导出 CSV。
// 9. 商机设置：加一个阶段后看板立刻多一列，再删掉；预计金额只给管理者看后小艾的列表没有金额列；赢单的
//    "再开一个商机"是新的一条；财务小芳只能看不能新建。
//
// 前置：与 p22-wake-acceptance.cjs 相同（后端、实时消费进程、调度进程接到模拟大模型 :8900），另需访客
// Widget（:5175）。运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<密码> node scripts/e2e/p28-opportunity-acceptance.cjs
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
const SHOTS = env('SHOTS', 'e2e-shots/p28')
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

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b)
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
      name: `商机验收 ${RUN}`,
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

/** el-input 的 textarea / input（data-testid 可能在 textarea 上，也可能在外层）。 */
const area = (scope, testid) =>
  scope.locator(`textarea[data-testid="${testid}"], [data-testid="${testid}"] textarea`).first()
const input = (scope, testid) =>
  scope.locator(`input[data-testid="${testid}"], [data-testid="${testid}"] input`).first()

async function closeDrawer(page) {
  await page.locator('.el-drawer__close-btn:visible').last().click()
  await page.mouse.move(400, 300)
  await settle(page)
}

/** 关闭指定的抽屉（商机详情里打开的会话记录叠在上面，关闭按钮在同一个位置）。 */
async function closeNamed(page, testid) {
  const drawer = page.locator(`.el-drawer[data-testid="${testid}"], .el-drawer:has([data-testid="${testid}"])`).first()
  await drawer.locator('.el-drawer__close-btn').click()
  await drawer.waitFor({ state: 'hidden' })
}

/** 打开商机页面：看板（默认）或列表，可以带快捷视图。 */
async function opportunities(page, view, mode = 'list') {
  const query = [mode === 'list' ? 'mode=list' : '', view ? `view=${view}` : ''].filter(Boolean).join('&')
  await page.goto(`${CONSOLE}/opportunities${query ? `?${query}` : ''}`)
  const root = mode === 'list' ? 'opp-table' : 'opp-board'
  await page.waitForSelector(`[data-testid="${root}"]`)
  await page.waitForFunction((id) => !document.querySelector(`[data-testid="${id}"] .el-loading-mask`), root)
  await page.waitForSelector('[data-testid="opp-tiles"]')
  // 鼠标从右上角（上一个抽屉的关闭按钮）挪开，不然顶栏的悬停菜单会盖住页面头部的按钮。
  await page.mouse.move(400, 300)
}

async function rowOf(page, name) {
  const row = page.locator('[data-testid="opp-table"] .el-table__row', { hasText: name }).first()
  await row.waitFor()
  return row
}

async function openOpportunity(page, name) {
  await (await rowOf(page, name)).click()
  const drawer = page.locator('[data-testid="opp-drawer"]')
  await drawer.locator('[data-testid="opp-status"]').waitFor()
  return drawer
}

async function tile(page, name) {
  return Number((await text(page.locator(`[data-testid="opp-tile-${name}"] .value`))) || 0)
}

/** 看板上的卡片（卡片里的菜单、下次跟进也以 opp-card- 开头，按 class 排除）。 */
const CARD = '.card[data-testid^="opp-card-"]'
const cardsIn = (page, code) => page.locator(`[data-testid="opp-column-${code}"] ${CARD}`)

// ---- 2. 员工转入、看板、跟进、AI 写跟进话术 ----

async function staffConverts(page) {
  await page.goto(`${CONSOLE}/customers`)
  await page.waitForSelector('[data-testid="customer-table"]')
  await page.locator('[data-testid="customer-table"] .el-table__row', { hasText: '王先生' }).getByRole('button', { name: '王先生' }).click()
  const panel = page.locator('[data-testid="customer-opportunity"]')
  await panel.locator('[data-testid="customer-opportunity-create"]').click()
  const dialog = page.locator('[data-testid="opp-create"]')
  await dialog.waitFor()
  const fixed = await text(dialog.locator('[data-testid="opp-create-customer"]'))
  await dialog.locator('[data-testid="opp-level"] label', { hasText: '高' }).click()
  await area(dialog, 'opp-interest').fill('智能门锁 X1，小区 20 户统一换锁')
  await area(dialog, 'opp-concerns').fill('觉得价格偏高，想等活动')
  const amount = input(dialog, 'opp-amount')
  await amount.fill('25960')
  await amount.blur()
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/p28-01-convert.png` })
  await dialog.locator('[data-testid="opp-create-save"]').click()
  await success(page, '已转入商机')
  const status = await text(panel.locator('[data-testid="customer-opportunity-status"]'))
  const panelText = await text(panel)
  check(
    'the customer profile converts 王先生 into an opportunity in 新线索 with the amount',
    fixed === '王先生' && status === '新线索' && panelText.includes('¥25,960'),
    { fixed, status, panelText },
  )
  // 客户资料里打开商机详情。
  await panel.locator('[data-testid="customer-opportunity-open"]').click()
  const fromPanel = page.locator('[data-testid="opp-drawer"]')
  await fromPanel.locator('[data-testid="opp-status"]', { hasText: '跟进中' }).waitFor()
  const fromPanelText = await text(fromPanel)
  check(
    'the profile opens the opportunity details',
    fromPanelText.includes('员工转入') && fromPanelText.includes('¥25,960'),
    fromPanelText,
  )
  await closeNamed(page, 'opp-drawer')
  await closeDrawer(page)
  const tag = page.locator(`[data-testid="customer-opportunity-tag-${state.wangId}"]`)
  await tag.waitFor()
  check('the customer list tags 王先生 with the stage 新线索', (await text(tag)) === '新线索')
  await page.screenshot({ path: `${SHOTS}/p28-02-customer-tag.png` })

  // 客户页的"商机看板"按钮打开看板。
  await page.locator('[data-testid="customer-opportunities-link"]').click()
  await page.waitForURL(/\/opportunities/)
  await page.waitForSelector('[data-testid="opp-board"]')
  const card = cardsIn(page, 'new').filter({ hasText: '浦东物业' }).first()
  await card.waitFor()
  const cardText = await text(card)
  const newCount = await text(page.locator('[data-testid="opp-column-count-new"]'))
  check(
    'the board shows 王先生 in 新线索 with the amount, the owner and the follow-up in 3 days',
    cardText.includes('王先生') &&
      cardText.includes('¥25,960') &&
      cardText.includes('管理员') &&
      cardText.includes('3 天后') &&
      newCount === '1' &&
      (await tile(page, 'active')) === 1,
    { cardText, newCount },
  )
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/p28-03-board.png` })

  // 拖到"已沟通"换阶段；卡片菜单"移到…"到"已报价"；拖到"赢单"弹出确认。
  const cardId = (await card.getAttribute('data-testid')).replace('opp-card-', '')
  await card.dragTo(page.locator('[data-testid="opp-column-contacted"]'))
  await success(page, '已移到「已沟通」')
  const moved = page.locator(`[data-testid="opp-column-contacted"] [data-testid="opp-card-${cardId}"]`)
  await moved.waitFor()
  check('dragging the card to 已沟通 changes the stage', (await cardsIn(page, 'new').count()) === 0)
  await moved.locator(`[data-testid="opp-card-menu-${cardId}"]`).click()
  await page.locator('.el-dropdown-menu__item:visible', { hasText: '已报价' }).click()
  await success(page, '已移到「已报价」')
  const quoted = page.locator(`[data-testid="opp-column-quoted"] [data-testid="opp-card-${cardId}"]`)
  await quoted.waitFor()
  check('the card menu moves it to 已报价', await quoted.isVisible())
  await quoted.dragTo(page.locator('[data-testid="opp-column-won"]'))
  const wonDialog = page.locator('[data-testid="opp-won-dialog"]')
  await wonDialog.waitFor()
  check('dropping on 赢单 asks for confirmation instead of closing at once', await wonDialog.isVisible())
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/p28-04-won-dialog.png` })
  await wonDialog.locator('button', { hasText: '取消' }).click()
  await wonDialog.waitFor({ state: 'hidden' })

  // 列表：数字和一行。
  await opportunities(page)
  const row = await rowOf(page, '王先生')
  const rowText = await text(row)
  check(
    'the list shows 王先生 with level, stage, amount, owner and the follow-up in 3 days',
    (await tile(page, 'active')) === 1 &&
      rowText.includes('高') &&
      rowText.includes('已报价') &&
      rowText.includes('¥25,960') &&
      rowText.includes('管理员') &&
      rowText.includes('3 天后') &&
      rowText.includes(shanghaiDate(3)),
    rowText,
  )

  const drawer = await openOpportunity(page, '王先生')
  const stageInfo = await text(drawer.locator('[data-testid="opp-stage-info"]'))
  const current = await drawer.locator('[data-testid="opp-stage-quoted"]').getAttribute('class')
  check('the step bar marks 已报价 as the current stage', current.includes('current') && stageInfo.includes('已报价'), {
    current,
    stageInfo,
  })
  await drawer.locator('[data-testid="opp-write-message"]').click()
  const message = area(drawer, 'opp-message')
  await message.waitFor()
  const written = await message.inputValue()
  const refs = await text(drawer.locator('[data-testid="opp-message-refs"]'))
  check(
    'AI writes a follow-up message from the knowledge base',
    written.startsWith('王先生您好') && written.includes('国庆期间智能门锁 9 折') && refs.includes('智能门锁国庆活动'),
    { written, refs },
  )
  await drawer.locator('[data-testid="opp-copy-message"]').click()
  const copied = page.locator('.el-message', { hasText: '复制' }).last()
  await copied.waitFor()
  check('the message can be copied to send by hand', (await copied.getAttribute('class')).includes('success'))

  const form = drawer.locator('[data-testid="opp-follow-form"]')
  await form.locator('label', { hasText: '微信' }).click()
  await area(form, 'opp-follow-content').fill('微信发了活动介绍，客户说下周和业委会商量')
  await form.locator('[data-testid="opp-follow-save"]').click()
  await success(page, '已记一次跟进')
  const timeline = drawer.locator('[data-testid="opp-activities"]')
  await timeline.locator('text=业委会').waitFor()
  const entry = await text(timeline)
  check(
    'the timeline has the follow-up with its method and author and the stage changes',
    entry.includes('微信') && entry.includes('管理员') && entry.includes('已报价') && entry.includes('已沟通'),
    entry,
  )

  // 下次跟进改成今天（之后 AI 唤醒提醒"该跟进了"）。
  const next = drawer.locator('.el-form-item', { hasText: '下次跟进' }).first().locator('input').first()
  await next.fill(shanghaiDate(0))
  await next.press('Enter')
  await drawer.locator('[data-testid="opp-save"]').click()
  await success(page, '已保存')
  const due = await text(drawer.locator('[data-testid="opp-due"]'))
  check('moving the next follow-up to today shows 今天', due === '今天', due)
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/p28-05-drawer.png` })
  await closeDrawer(page)
}

// ---- 3. 访客咨询后 AI 转入、又来咨询、订单确认后赢单 ----

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
  const page = await until('the AI opportunity', async () => {
    const list = await json(`${API}/api/v1/opportunities?view=active`, { token: state.admin })
    return list.items.find((p) => p.source === 'ai') ?? null
  })
  state.aiCustomerId = page.customer_id
  state.aiName = page.customer_name

  await opportunities(admin)
  const row = await rowOf(admin, state.aiName)
  const rowText = await text(row)
  check(
    'AI converts the closed high-intent session: source AI, 新线索, 7 days, owner 小艾, level 中',
    rowText.includes('AI') &&
      rowText.includes('新线索') &&
      rowText.includes('7 天后') &&
      rowText.includes('小艾') &&
      rowText.includes('中'),
    rowText,
  )
  const drawer = await openOpportunity(admin, state.aiName)
  const interest = await area(drawer, 'opp-edit-interest').inputValue()
  const concerns = await area(drawer, 'opp-edit-concerns').inputValue()
  check(
    'the AI filled in what the customer wants and the concerns',
    interest.includes('智能门锁有货吗') && concerns.includes('价格、还要考虑'),
    { interest, concerns },
  )
  await drawer.locator('[data-testid="opp-session"]').click()
  const session = admin.locator('[data-testid="session-drawer"]')
  await session.locator('text=智能门锁有货吗').first().waitFor()
  check('the source session opens from the opportunity', true)
  await settle(admin)
  await admin.screenshot({ path: `${SHOTS}/p28-06-ai-session.png` })
  await closeNamed(admin, 'session-drawer')
  await closeNamed(admin, 'opp-drawer')

  // 客户又来咨询了：说要下单——系统记一条动态，等级调高。
  await serveAndClose(alice, visitor, RETURN_ASK, '准备下单')
  await cli('opportunities-scan', TENANT)
  await until('the return visit entry', async () => {
    const list = await json(`${API}/api/v1/opportunities?view=active&customer_id=${state.aiCustomerId}`, {
      token: state.admin,
    })
    const item = list.items[0]
    return item && item.level === 'high' ? item : null
  })
  await opportunities(admin)
  const again = await openOpportunity(admin, state.aiName)
  const timeline = await text(again.locator('[data-testid="opp-activities"]'))
  check(
    'a return visit is recorded by the system and the level goes up to 高',
    timeline.includes('客户又来咨询了（准备下单）') && timeline.includes('系统') && (await text(again)).includes('意向高'),
    timeline,
  )
  await settle(admin)
  await admin.screenshot({ path: `${SHOTS}/p28-07-return.png` })
  await closeDrawer(admin)

  // 这个客户的订单确认后自动赢单。
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
  await opportunities(admin, 'won')
  const won = await openOpportunity(admin, state.aiName)
  const wonText = await text(won)
  check(
    'confirming the order marks the opportunity won with the order',
    wonText.includes('已赢单') && wonText.includes(order.no) && (await tile(admin, 'won')) === 1,
    wonText,
  )
  await settle(admin)
  await admin.screenshot({ path: `${SHOTS}/p28-08-won.png` })
  await closeDrawer(admin)
}

// ---- 4. 只建议：确认 AI 的建议 ----

async function suggestion(browser, admin, alice) {
  await opportunities(admin)
  await admin.click('[data-testid="opp-settings-open"]')
  const dialog = admin.locator('[data-testid="opp-settings"]')
  await dialog.waitFor()
  await settle(admin)
  await admin.screenshot({ path: `${SHOTS}/p28-09-settings.png` })
  await dialog.locator('.el-tabs__item', { hasText: 'AI 转入' }).click()
  await dialog.locator('[data-testid="opp-ai-mode"] label', { hasText: '只建议' }).click()
  await dialog.locator('[data-testid="opp-settings-save"]').click()
  await success(admin, '已保存商机设置')
  const saved = await json(`${API}/api/v1/opportunities/settings`, { token: state.admin })
  check('settings switch AI to suggest only', saved.settings.ai_mode === 'suggest', saved.settings)

  const visitor = await openVisitor(browser)
  await serveAndClose(alice, visitor, '可视门铃有货吗？什么时候能发货', '意向明确')
  await cli('opportunities-scan', TENANT)
  await until('the AI suggestion', async () => {
    const list = await json(`${API}/api/v1/opportunities?view=suggested`, { token: state.admin })
    return list.items[0] ?? null
  })
  await opportunities(admin, 'suggested')
  check('the suggestion waits in 待确认', (await tile(admin, 'suggested')) === 1)
  const row = admin.locator('[data-testid="opp-table"] .el-table__row').first()
  await row.click()
  const drawer = admin.locator('[data-testid="opp-drawer"]')
  await drawer.locator('[data-testid="opp-suggestion"]').waitFor()
  await settle(admin)
  await admin.screenshot({ path: `${SHOTS}/p28-10-suggestion.png` })
  await drawer.locator('[data-testid="opp-accept"]').click()
  await success(admin, '已转入商机')
  const status = await text(drawer.locator('[data-testid="opp-status"]'))
  check('accepting the suggestion starts following up', status === '跟进中', status)
  await closeDrawer(admin)
}

// ---- 5. AI 唤醒：商机该跟进了 ----

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
    'AI wake-up reminds the owner: 管理员 有 1 条商机该跟进了',
    finding && finding.title === '管理员 有 1 条商机该跟进了' && finding.detail.startsWith('王先生'),
    findings.items.map((f) => f.title),
  )
  if (!finding) return
  await admin.goto(`${CONSOLE}${finding.link}`)
  await admin.waitForSelector('[data-testid="opp-board"]')
  await admin.waitForFunction(() => !document.querySelector('[data-testid="opp-board"] .el-loading-mask'))
  await admin.locator(CARD).first().waitFor()
  const cards = await admin.locator(CARD).allInnerTexts()
  const view = await text(admin.locator('[data-testid="opp-views"] .is-active'))
  check(
    "the reminder link opens today's board of that owner",
    cards.length === 1 && cards[0].includes('王先生') && view.startsWith('今天该跟进'),
    { cards, view },
  )
  await admin.screenshot({ path: `${SHOTS}/p28-11-wake-link.png`, fullPage: true })
}

// ---- 6. 输单和重新跟进 ----

async function loseAndReopen(admin) {
  await opportunities(admin)
  const drawer = await openOpportunity(admin, '王先生')
  await drawer.locator('[data-testid="opp-lost"]').click()
  const dialog = admin.locator('[data-testid="opp-lost-dialog"]')
  await dialog.waitFor()
  await dialog.locator('[data-testid="opp-lost-code"] label', { hasText: '其他' }).click()
  await area(dialog, 'opp-lost-text').fill('业委会决定暂缓换锁')
  await dialog.locator('[data-testid="opp-lost-confirm"]').click()
  await success(admin, '已输单')
  const reason = await text(drawer.locator('[data-testid="opp-lost-reason"]'))
  check('losing keeps the reason category and the note', reason === '其他：业委会决定暂缓换锁', reason)
  await drawer.locator('[data-testid="opp-reopen"]').click()
  await success(admin, '已重新跟进')
  const status = await text(drawer.locator('[data-testid="opp-status"]'))
  const stageInfo = await text(drawer.locator('[data-testid="opp-stage-info"]'))
  check(
    'a lost opportunity can be followed up again and goes back to 已沟通',
    status === '跟进中' && stageInfo.includes('已沟通'),
    { status, stageInfo },
  )
  await closeDrawer(admin)
}

// ---- 7. 坐席的查看范围 ----

async function agentScope(alice) {
  await opportunities(alice, 'all')
  const menu = await text(alice.locator('[data-testid="main-menu"]'))
  const rows = await alice.locator('[data-testid="opp-table"] .el-table__row').allInnerTexts()
  const settings = await alice.locator('[data-testid="opp-settings-open"]').count()
  check(
    '小艾 has the 商机 menu, sees the opportunities she owns (not 王先生) and has no settings',
    menu.includes('商机') && rows.length === 2 && rows.every((r) => !r.includes('王先生')) && settings === 0,
    { rows, settings },
  )
  await alice.screenshot({ path: `${SHOTS}/p28-12-agent.png`, fullPage: true })
}

// ---- 8. 首页数字、报表"销售"页签、导出 ----

async function homeReportExport(admin) {
  await admin.goto(`${CONSOLE}/`)
  const section = admin.locator('[data-testid="home-team-opportunities"]')
  await section.waitFor()
  const active = await text(section.locator('[data-testid="home-opps-active"] .value'))
  const won = await text(section.locator('[data-testid="home-opps-won"]'))
  check(
    'the home page shows the opportunity numbers: 2 in progress, 1 won this month with the amount',
    active === '2' && won.includes('1') && won.includes('¥2,598'),
    { active, won },
  )
  await section.locator('[data-testid="home-opps-active"]').click()
  await admin.waitForURL(/\/opportunities/)
  await admin.waitForSelector('[data-testid="opp-board"]')
  check('the home tile opens the opportunities board', true)
  await admin.screenshot({ path: `${SHOTS}/p28-13-home.png` })

  await admin.goto(`${CONSOLE}/reports`)
  await admin.locator('[data-testid="report-tabs"] .el-tabs__item', { hasText: '销售' }).click()
  const report = admin.locator('[data-testid="sales-report"]')
  await report.locator('[data-testid="sales-tile-created"]').waitFor()
  const created = await text(report.locator('[data-testid="sales-tile-created"] .value'))
  const funnelNew = await text(report.locator('[data-testid="sales-funnel-new"]'))
  const sources = await report.locator('[data-testid="sales-by-source"] .el-table__row').allInnerTexts()
  const owners = await report.locator('[data-testid="sales-by-owner"] .el-table__row').allInnerTexts()
  check(
    'the sales report counts 3 new opportunities, the funnel, 2 from AI and 1 from staff, and the owners',
    created === '3' &&
      funnelNew.includes('3') &&
      sources.some((r) => r.includes('AI') && r.includes('2')) &&
      sources.some((r) => r.includes('员工') && r.includes('1')) &&
      owners.length === 2,
    { created, funnelNew, sources, owners },
  )
  await settle(admin)
  await admin.screenshot({ path: `${SHOTS}/p28-14-sales-report.png`, fullPage: true })

  await opportunities(admin, 'all')
  const [download] = await Promise.all([admin.waitForEvent('download'), admin.click('[data-testid="opp-export"]')])
  const csv = fs.readFileSync(await download.path(), 'utf-8')
  const lines = csv.split('\n').filter(Boolean)
  check(
    'exporting downloads a CSV with the header and the three opportunities',
    download.suggestedFilename().startsWith('opportunities-') &&
      lines.length === 4 &&
      lines[0].includes('客户') &&
      csv.includes('王先生') &&
      csv.includes('已沟通'),
    { name: download.suggestedFilename(), lines: lines.length, head: lines[0] },
  )
}

// ---- 9. 阶段设置、金额可见范围、再开一个商机、财务只读 ----

async function settingsAndScope(browser, admin, alice) {
  // 加一个阶段，看板立刻多一列；再删掉。
  await opportunities(admin, undefined, 'board')
  await admin.click('[data-testid="opp-settings-open"]')
  const dialog = admin.locator('[data-testid="opp-settings"]')
  await dialog.waitFor()
  await dialog.locator('[data-testid="opp-stage-new"]').fill('样品试用')
  await dialog.locator('[data-testid="opp-stage-add"]').click()
  await success(admin, '已添加阶段')
  // 阶段名在输入框里（不是文本），按接口返回的代码找行和列。
  const stagesNow = await json(`${API}/api/v1/opportunities/stages`, { token: state.admin })
  const trial = stagesNow.find((s) => s.name === '样品试用')
  await dialog.locator(`[data-testid="opp-stage-row-${trial.code}"]`).waitFor()
  await dialog.locator('button', { hasText: '关闭' }).click()
  const column = admin.locator(`[data-testid="opp-column-${trial.code}"]`)
  await column.waitFor()
  const heads = await admin.locator('[data-testid="opp-board"] .head .title').allInnerTexts()
  check('adding a stage in the settings adds a board column at once', same(heads, ['新线索', '已沟通', '已报价', '谈判中', '样品试用', '赢单', '输单']), heads)
  await admin.click('[data-testid="opp-settings-open"]')
  await dialog.waitFor()
  await dialog.locator(`[data-testid="opp-stage-delete-${trial.code}"]`).click()
  const confirm = admin.locator('[data-testid="opp-stage-delete-dialog"]')
  await confirm.waitFor()
  await confirm.locator('[data-testid="opp-stage-delete-confirm"]').click()
  await success(admin, '已删除阶段')
  await dialog.locator('button', { hasText: '关闭' }).click()
  await column.waitFor({ state: 'detached' })
  check('deleting the empty stage removes its column', (await admin.locator('[data-testid="opp-board"] .column').count()) === 6)

  // 预计金额只给能分配商机的员工看：小艾的列表没有金额列。
  const list = await json(`${API}/api/v1/opportunities?view=active`, { token: state.admin })
  const alicesOwn = list.items.find((o) => o.owner_name === '小艾')
  await json(`${API}/api/v1/opportunities/${alicesOwn.id}`, { method: 'PATCH', token: state.admin, body: { amount: '3000' } })
  const settings = await json(`${API}/api/v1/opportunities/settings`, { token: state.admin })
  await json(`${API}/api/v1/opportunities/settings`, {
    method: 'PUT',
    token: state.admin,
    body: { ...settings.settings, amount_visibility: 'managers' },
  })
  await opportunities(alice, 'all')
  const headers = await alice.locator('[data-testid="opp-table"] th').allInnerTexts()
  const aliceRow = await text(await rowOf(alice, alicesOwn.customer_name))
  check(
    'with amounts for managers only, 小艾 sees no amount column and no amount',
    !headers.some((h) => h.includes('预计金额')) && !aliceRow.includes('¥3,000'),
    { headers, aliceRow },
  )
  await opportunities(admin, 'all')
  const adminRow = await text(await rowOf(admin, alicesOwn.customer_name))
  check('the admin still sees the amount', adminRow.includes('¥3,000'), adminRow)
  await json(`${API}/api/v1/opportunities/settings`, {
    method: 'PUT',
    token: state.admin,
    body: { ...settings.settings, amount_visibility: 'all' },
  })

  // 赢单的"再开一个商机"是新的一条。
  await opportunities(admin, 'won')
  const won = await openOpportunity(admin, state.aiName)
  await won.locator('[data-testid="opp-reopen"]').click()
  await success(admin, '已再开一个商机')
  const status = await text(won.locator('[data-testid="opp-status"]'))
  const stageInfo = await text(won.locator('[data-testid="opp-stage-info"]'))
  await closeDrawer(admin)
  const all = await json(`${API}/api/v1/opportunities?view=all&customer_id=${state.aiCustomerId}`, { token: state.admin })
  check(
    'reopening a won opportunity starts a new one in 新线索 and keeps the won record',
    status === '跟进中' && stageInfo.includes('新线索') && all.items.length === 2 && all.items.some((o) => o.status === 'won'),
    { status, stageInfo, count: all.items.length },
  )

  // 财务只能看不能改。
  await json(`${API}/api/v1/staff`, {
    method: 'POST',
    token: state.admin,
    body: { username: 'fang', display_name: '小芳', password: PASSWORD, role_codes: ['finance'] },
  })
  const fang = await consoleLogin(browser, 'fang')
  await opportunities(fang, 'all')
  const rows = await fang.locator('[data-testid="opp-table"] .el-table__row').count()
  const create = await fang.locator('[data-testid="opp-create-open"]').count()
  const drawer = await openOpportunity(fang, '王先生')
  const saveButtons = await drawer.locator('[data-testid="opp-save"]').count()
  const wonButtons = await drawer.locator('[data-testid="opp-won"]').count()
  check(
    '财务 sees every opportunity but cannot create, edit or close',
    rows === 4 && create === 0 && saveButtons === 0 && wonButtons === 0,
    { rows, create, saveButtons, wonButtons },
  )
  await fang.screenshot({ path: `${SHOTS}/p28-15-finance.png` })
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
    await homeReportExport(admin)
    await settingsAndScope(browser, admin, alice)
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
