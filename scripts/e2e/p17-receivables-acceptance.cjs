// P17 验收：应收账款与财务岗位（设计文档 §28）。
//
// 1. 管理员（默认的财务）：菜单里有"应收账款"，首页多一块"应收账款"（应收合计、已逾期、今天到期、
//    本月已收、最近登记的收款）。
// 2. 应收账款页：顶部数字（应收合计 ¥4,393.00 五笔、逾期 ¥3,195.00 三笔、今天到期 ¥199.00、本月已收
//    ¥1,599.00）和账龄分段；列表按到期日排序（暂欠逾期 100 天在前，尾款等加工完成的"未到期"在后）；
//    点"已逾期"只剩三笔，点账龄"逾期 31–60 天"只剩货到付款的那笔。
// 3. 按客户：李女士未收 ¥2,996.00、王先生 ¥1,397.00；王先生的对账单期末未收 ¥1,397.00、五行明细，可以
//    打印；"查看订单"回到按订单并按客户筛选。
// 4. 管理员在"员工 → 角色"新建"财务"角色：岗位选"财务"，一键填入默认权限（8 项），保存后岗位显示"财务"。
// 5. 财务小蔡：菜单只有首页、订单、应收账款、个人待办、客户、AI 助理；首页是应收的一块；能看到全部五笔
//    应收。跟进暂欠的订单（承诺付款日和备注，列表显示）；给货到付款的订单发起催收（列表显示催收待办）；
//    登记在线收款那笔的收款后剩四笔，本月已收变为 ¥1,798.00；导出 CSV。
// 6. 客服小美没有"应收账款"菜单，接口返回 403。
// 7. 手机：应收账款页是卡片，没有横向滚动。
//
// 前置：后端、控制台。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p17-receivables-acceptance.cjs
const { chromium } = require('playwright')
const { execFileSync } = require('child_process')
const fs = require('fs')
const path = require('path')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p17-receivables')
const BACKEND_DIR = path.resolve(__dirname, '../../backend')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `p17-${RUN}`
const PASSWORD = 'demo-pass-2026'
const DESKTOP = { width: 1440, height: 900 }
const PHONE = { width: 390, height: 844 }
const RECEIVER = { name: '王先生', phone: '13800001111', address: '上海市浦东新区世纪大道 100 号' }

const summary = { tenant: TENANT, checks: [], consoleErrors: [] }
const check = (name, ok, detail) =>
  summary.checks.push(
    `${ok ? 'PASS' : 'FAIL'} ${name}${ok || detail === undefined ? '' : ` -> ${JSON.stringify(detail)}`}`,
  )
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b)
const pages = []

function watchErrors(page, label) {
  page.on('pageerror', (error) => summary.consoleErrors.push(`${label} pageerror: ${error.message}`))
  page.on('console', (message) => {
    // 接口按权限返回 401/403 时浏览器也会打印"Failed to load resource"，不算脚本错误。
    if (message.type() === 'error' && !message.text().startsWith('Failed to load resource')) {
      summary.consoleErrors.push(`${label}: ${message.text()}`)
    }
  })
}

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
  const tokens = await json(`${API}/api/v1/auth/login`, {
    method: 'POST',
    body: { tenant_code: TENANT, username, password: PASSWORD },
  })
  return tokens.access_token
}

/** 在后端目录执行一段 Python：把订单的日期往前拨（验收需要逾期的应收，接口不允许填过去的日期）。 */
const BACKDATE = `
import asyncio, sys, uuid
from sqlalchemy import text
from app.core.config import get_settings
from app.db.session import Database

COLUMNS = {
    "credit_due_date": "UPDATE orders SET credit_due_date = credit_due_date - CAST(:days AS integer)"
    " WHERE id = CAST(:id AS uuid)",
    "confirmed_at": "UPDATE orders SET confirmed_at = confirmed_at - make_interval(days => :days)"
    " WHERE id = CAST(:id AS uuid)",
    "shipped_at": "UPDATE orders SET shipped_at = shipped_at - make_interval(days => :days)"
    " WHERE id = CAST(:id AS uuid)",
}

async def main(tenant_id, order_id, column, days):
    db = Database(get_settings())
    async with db.tenant_session(uuid.UUID(tenant_id)) as session:
        await session.execute(text(COLUMNS[column]), {"days": int(days), "id": order_id})
        await session.commit()
    await db.dispose()
    print("ok")

asyncio.run(main(*sys.argv[1:]))
`

function backdate(tenantId, orderId, column, days) {
  const output = execFileSync('uv', ['run', 'python', '-c', BACKDATE, tenantId, orderId, column, String(days)], {
    cwd: BACKEND_DIR,
    env: process.env,
    encoding: 'utf-8',
  })
  if (!output.trim().endsWith('ok')) throw new Error(`backdate failed: ${output}`)
}

// 租户、客服小美、两个商品、两位客户和七个订单（和 backend/tests/test_finance.py 一样）。
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
      name: `应收验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  const admin = await login('admin')
  const post = (url, body) => json(`${API}${url}`, { method: 'POST', token: admin, body })
  await post('/api/v1/staff', {
    username: 'mei',
    display_name: '客服小美',
    password: PASSWORD,
    role_codes: ['agent'],
  })
  const lock = await post('/api/v1/products', { code: 'LOCK-X1', name: '智能门锁 X1', retail_price: '1299' })
  const bell = await post('/api/v1/products', { code: 'BELL-D1', name: '可视门铃 D1', retail_price: '199' })
  const li = await post('/api/v1/customers', { display_name: '李女士' })
  const wang = await post('/api/v1/customers', { display_name: '王先生', company: '王记五金' })

  const order = async (customer, product, quantity, confirm) => {
    const created = await post('/api/v1/orders', {
      customer_id: customer.id,
      items: [{ product_id: product.id, quantity }],
      receiver: RECEIVER,
    })
    const result = await post(`/api/v1/orders/${created.id}/confirm`, { notify_customer: false, ...confirm })
    return result.order
  }
  const ship = async (id) => {
    await post(`/api/v1/orders/${id}/start`, {})
    await post(`/api/v1/orders/${id}/ship`, { shipping_company: '顺丰', tracking_no: 'SF1', notify_customer: false })
  }
  const pay = (id, amount) => post(`/api/v1/orders/${id}/payments`, { amount, channel: 'bank' })
  const today = (await json(`${API}/api/v1/finance/receivables/summary`, { token: admin })).today

  const credit = await order(li, lock, 2, { payment_method: 'credit', credit_due_date: today })
  backdate(tenant.id, credit.id, 'credit_due_date', 100)
  const online = await order(li, bell, 1, { payment_method: 'online' })
  backdate(tenant.id, online.id, 'confirmed_at', 3)
  const codToday = await order(li, bell, 1, { payment_method: 'cod' })
  await ship(codToday.id)
  const deposit = await order(wang, lock, 1, { payment_method: 'deposit', deposit_amount: '300' })
  await pay(deposit.id, '300')
  await post(`/api/v1/orders/${deposit.id}/start`, {})
  const cod = await order(wang, bell, 2, { payment_method: 'cod' })
  await ship(cod.id)
  backdate(tenant.id, cod.id, 'shipped_at', 40)
  const paid = await order(wang, lock, 1, { payment_method: 'online' })
  await pay(paid.id, '1299')
  await post('/api/v1/orders', {
    customer_id: li.id,
    items: [{ product_id: bell.id, quantity: 1 }],
    receiver: RECEIVER,
    submit: false,
  })
  return { admin, today, credit, online, codToday, deposit, cod }
}

async function consoleLogin(browser, username, { viewport = DESKTOP } = {}) {
  const context = await browser.newContext({ viewport, locale: 'zh-CN', acceptDownloads: true })
  const page = await context.newPage()
  const label = `${username}${viewport === PHONE ? '-phone' : ''}`
  watchErrors(page, label)
  pages.push([label, page])
  await page.goto(`${CONSOLE}/login`)
  await page.fill('input[placeholder="例如 demo"]', TENANT)
  await page.fill('input[autocomplete="username"]', username)
  await page.fill('input[autocomplete="current-password"]', PASSWORD)
  await page.click('button:has-text("登录")')
  await page.locator('[data-testid="main-menu"]').waitFor()
  await page.waitForLoadState('networkidle')
  return page
}

async function menus(page) {
  const items = await page.locator('[data-testid="main-menu"] .el-menu-item').allInnerTexts()
  return items.map((text) => text.replace(/\d+\+?/g, '').trim())
}

async function shot(page, name) {
  await page.waitForTimeout(600)
  await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true })
}

const tileText = (page, testid) => page.locator(`[data-testid="${testid}"] .value`).innerText()
const rows = (page, testid) => page.locator(`[data-testid="${testid}"] .el-table__row`)

/** 等列表重新取数后行数变成 n（点筛选后请求是异步的）。 */
async function waitRows(page, testid, n, timeout = 10_000) {
  const deadline = Date.now() + timeout
  for (;;) {
    const count = await rows(page, testid).count()
    if (count === n || Date.now() > deadline) return count
    await page.waitForTimeout(150)
  }
}

async function openReceivables(page, query = '') {
  await page.goto(`${CONSOLE}/receivables${query}`)
  await page.locator('[data-testid="receivable-summary"]').waitFor()
  await page.locator('[data-testid="receivables-table"] .el-table__row').first().waitFor()
  await page.waitForLoadState('networkidle')
}

// 1. 管理员：菜单和首页。
async function adminHome(browser) {
  const admin = await consoleLogin(browser, 'admin')
  const menu = await menus(admin)
  check('管理员的菜单里有"应收账款"（管理员就是默认的财务）', menu.includes('应收账款'), menu)
  await admin.locator('[data-testid="home-finance-receivables"]').waitFor()
  const open = await tileText(admin, 'home-finance-open')
  const overdue = await tileText(admin, 'home-finance-overdue')
  const received = await tileText(admin, 'home-finance-received')
  check(
    '管理员首页的"应收账款"：应收合计 ¥4,393.00、已逾期 3 笔、本月已收 ¥1,599.00',
    open === '¥4,393.00' && overdue === '3' && received === '¥1,599.00',
    { open, overdue, received },
  )
  const recent = await admin.locator('[data-testid="home-finance-payments"] .el-table__row').count()
  check('首页列出最近登记的收款（2 笔）', recent === 2, recent)
  await shot(admin, '1-admin-home')
  return admin
}

// 2-3. 应收账款页：数字、账龄、列表、按客户、对账单。
async function receivablesPage(admin, ctx) {
  await admin.locator('[data-testid="home-finance-overdue"]').click()
  await admin.waitForURL(/\/receivables\?view=overdue/)
  await admin.locator('[data-testid="receivable-summary"]').waitFor()
  await rows(admin, 'receivables-table').first().waitFor()
  const overdueRows = await rows(admin, 'receivables-table').count()
  check('首页点"已逾期"打开应收账款并只看逾期的（3 笔）', overdueRows === 3, overdueRows)

  await openReceivables(admin)
  const tiles = {
    open: await tileText(admin, 'receivable-open'),
    overdue: await tileText(admin, 'receivable-overdue'),
    dueToday: await tileText(admin, 'receivable-due-today'),
    received: await tileText(admin, 'receivable-received'),
  }
  check(
    '顶部数字：应收合计 ¥4,393.00、已逾期 ¥3,195.00、今天到期 ¥199.00、本月已收 ¥1,599.00',
    same(tiles, { open: '¥4,393.00', overdue: '¥3,195.00', dueToday: '¥199.00', received: '¥1,599.00' }),
    tiles,
  )
  const aging = await admin.locator('[data-testid="receivable-aging"] .segment').allInnerTexts()
  check(
    '账龄分段：未到期 2 笔、逾期 1–30 天 1 笔、31–60 天 1 笔、90 天以上 1 笔',
    aging.length === 5 && aging[0].includes('· 2') && aging[1].includes('· 1') && aging[2].includes('· 1') && aging[4].includes('· 1'),
    aging,
  )
  const order = await rows(admin, 'receivables-table').allInnerTexts()
  const nos = order.map((text) => text.split('\n')[0].trim())
  check(
    '按到期日排序：暂欠逾期 100 天在前，货到付款逾期 40 天、在线收款逾期 3 天、今天到期、尾款等加工完成（未到期）',
    same(nos, [ctx.credit.no, ctx.cod.no, ctx.online.no, ctx.codToday.no, ctx.deposit.no]),
    nos,
  )
  const due = await admin.locator(`[data-testid="receivable-due-${ctx.credit.no}"]`).innerText()
  const notDue = await admin.locator(`[data-testid="receivable-due-${ctx.deposit.no}"]`).innerText()
  check('到期列：暂欠"逾期 100 天"，收了定金等加工的尾款"未到期"', due === '逾期 100 天' && notDue === '未到期', { due, notDue })
  await shot(admin, '2-receivables')

  await admin.locator('[data-testid="aging-d31_60"]').click()
  await waitRows(admin, 'receivables-table', 1)
  const bucketRows = await rows(admin, 'receivables-table').allInnerTexts()
  check('点账龄"逾期 31–60 天"只剩货到付款发货 40 天没收的那笔', bucketRows.length === 1 && bucketRows[0].includes(ctx.cod.no), bucketRows)
  await admin.locator('[data-testid="aging-d31_60"]').click()

  await admin.locator('[data-testid="receivable-tabs"] .el-tabs__item', { hasText: '按客户' }).click()
  await rows(admin, 'receivable-customers').first().waitFor()
  const li = await admin.locator('[data-testid="customer-outstanding-李女士"]').innerText()
  const wang = await admin.locator('[data-testid="customer-outstanding-王先生"]').innerText()
  const customerRows = await rows(admin, 'receivable-customers').allInnerTexts()
  check(
    '按客户：李女士未收 ¥2,996.00（最长逾期 100 天），王先生（王记五金）¥1,397.00',
    li === '¥2,996.00' && wang === '¥1,397.00' && customerRows[0].includes('最长逾期 100 天') && customerRows[1].includes('王记五金'),
    { li, wang, customerRows },
  )
  await admin.locator('[data-testid="customer-statement-王先生"]').click()
  const drawer = admin.locator('[data-testid="statement-drawer"]')
  await drawer.locator('[data-testid="statement-closing"]').waitFor()
  const closing = await drawer.locator('[data-testid="statement-closing"]').innerText()
  const lines = await drawer.locator('[data-testid="statement-lines"] .el-table__row').count()
  const openOrders = await drawer.locator('[data-testid="statement-open"] .el-table__row').count()
  check('王先生的对账单：期末未收 ¥1,397.00，五行明细（三笔订单、两笔收款），两笔未收清', closing === '¥1,397.00' && lines === 5 && openOrders === 2, { closing, lines, openOrders })
  await shot(admin, '3-statement')
  const [popup] = await Promise.all([
    admin.waitForEvent('popup'),
    drawer.locator('[data-testid="statement-print"]').click(),
  ])
  await popup.waitForLoadState('domcontentloaded').catch(() => null)
  const printTitle = await popup.title().catch(() => '')
  const printBody = await popup.locator('body').innerText().catch(() => '')
  check('打印：新窗口里的对账单（客户、期间、明细、期末未收、制表人）', printTitle.includes('对账单') && printBody.includes('王先生') && printBody.includes('¥1,397.00') && printBody.includes('制表人'), { printTitle })
  await popup.close().catch(() => null)
  await admin.keyboard.press('Escape')
  await drawer.waitFor({ state: 'hidden' }).catch(() => null)
  await admin.locator('[data-testid="customer-orders-李女士"]').click()
  await admin.locator('[data-testid="receivable-customer-filter"]').waitFor()
  const filtered = await waitRows(admin, 'receivables-table', 3)
  check('"查看订单"回到按订单并按客户筛选（李女士 3 笔）', filtered === 3, filtered)
}

// 4. 管理员新建"财务"角色：岗位"财务"、一键填入默认权限。
async function financeRole(admin, ctx) {
  await admin.goto(`${CONSOLE}/staff`)
  await admin.locator('.el-tabs__item', { hasText: '角色' }).click()
  await admin.locator('[data-testid="new-role"]').click()
  const dialog = admin.locator('[data-testid="role-dialog"]')
  await dialog.locator('input[placeholder="小写字母开头，如 quality"]').fill('finance')
  await dialog.locator('.el-form-item', { hasText: '名称' }).locator('input').fill('财务')
  await dialog.locator('[data-testid="role-console"]').click()
  await admin.locator('.el-select-dropdown__item:visible', { hasText: '财务' }).click()
  await dialog.locator('[data-testid="role-fill-defaults"]').click()
  const checked = await dialog.locator('.el-checkbox.is-checked').allInnerTexts()
  check(
    '选了岗位"财务"后一键填入默认权限：查看应收账款、跟进催收导出、查看订单、登记收款等 8 项',
    checked.length === 8 && checked.some((t) => t.includes('应收账款')) && checked.some((t) => t.includes('登记收款')),
    checked,
  )
  await shot(admin, '4-finance-role')
  await dialog.locator('button', { hasText: '保存' }).click()
  const console_ = admin.locator('[data-testid="role-console-finance"]')
  await console_.waitFor()
  const label = await console_.innerText()
  check('保存后角色列表显示岗位"财务"', label === '财务', label)
  await json(`${API}/api/v1/staff`, {
    method: 'POST',
    token: ctx.admin,
    body: { username: 'cai', display_name: '财务小蔡', password: PASSWORD, role_codes: ['finance'] },
  })
}

// 5. 财务小蔡：菜单、首页、跟进、催收、登记收款、导出。
async function financeSection(browser, ctx) {
  const cai = await consoleLogin(browser, 'cai')
  const menu = await menus(cai)
  check('财务的菜单只有首页、订单、应收账款、个人待办、客户、AI 助理', same(menu, ['首页', '订单', '应收账款', '个人待办', '客户', 'AI 助理']), menu)
  await cai.locator('[data-testid="home-finance-receivables"]').waitFor()
  const team = await cai.locator('[data-testid="rt-my-queued"]').count()
  check('财务的首页是应收的一块，没有接待的内容', team === 0, team)
  await shot(cai, '5-finance-home')

  await openReceivables(cai)
  const all = await rows(cai, 'receivables-table').count()
  check('财务能看到全公司的应收（5 笔，不受客户归属限制）', all === 5, all)

  await cai.locator(`[data-testid="receivable-followup-btn-${ctx.credit.no}"]`).click()
  const followup = cai.locator('[data-testid="followup-dialog"]')
  await followup.waitFor()
  const promise = new Date(Date.parse(ctx.today) + 5 * 86_400e3).toISOString().slice(0, 10)
  await followup.locator('[data-testid="followup-date"] input').fill(promise)
  await followup.locator('[data-testid="followup-date"] input').press('Enter')
  await followup.locator('[data-testid="followup-note"] textarea').fill('电话联系，客户说下周付')
  await followup.locator('[data-testid="followup-submit"]').click()
  await cai.locator(`[data-testid="receivable-followup-${ctx.credit.no}"]`).waitFor()
  const followed = await cai.locator(`[data-testid="receivable-followup-${ctx.credit.no}"]`).innerText()
  const promiseCell = await rows(cai, 'receivables-table').filter({ hasText: ctx.credit.no }).innerText()
  check('跟进：承诺付款日和备注记下来，列表显示', followed.includes('客户说下周付') && promiseCell.includes(`承诺 ${promise}`), { followed, promise })

  await cai.locator(`[data-testid="receivable-collect-${ctx.cod.no}"]`).click()
  const collect = cai.locator('[data-testid="collect-dialog"]')
  await collect.waitFor()
  await collect.locator('[data-testid="collect-submit"]').click()
  await rows(cai, 'receivables-table').filter({ hasText: ctx.cod.no }).filter({ hasText: '催收待办' }).waitFor()
  const collected = await cai.locator(`[data-testid="receivable-collect-${ctx.cod.no}"]`).count()
  check('催收：生成催收待办后列表显示待办号，不能再次发起', collected === 0, collected)
  await shot(cai, '6-followup-collect')

  await cai.locator(`[data-testid="receivable-pay-${ctx.online.no}"]`).click()
  const payment = cai.locator('[data-testid="payment-dialog"]')
  await payment.waitFor()
  const amount = await payment.locator('input[data-testid="payment-amount"]').inputValue()
  await payment.locator('[data-testid="payment-submit"]').click()
  await payment.waitFor({ state: 'hidden' })
  await cai.locator('[data-testid="receivable-received"] .value', { hasText: '¥1,798.00' }).waitFor()
  const left = await waitRows(cai, 'receivables-table', 4)
  check('登记收款：金额默认是未收的 199.00，收清后剩 4 笔，本月已收 ¥1,798.00', amount === '199.00' && left === 4, { amount, left })

  const [download] = await Promise.all([
    cai.waitForEvent('download'),
    cai.locator('[data-testid="receivables-export"]').click(),
  ])
  const filename = download.suggestedFilename()
  const csv = fs.readFileSync(await download.path(), 'utf-8')
  check('导出当前筛选的应收明细（CSV，带表头和 4 行）', filename.startsWith('receivables-') && csv.includes('订单号') && csv.trim().split('\n').length === 5, { filename, lines: csv.trim().split('\n').length })
}

// 6. 客服：没有应收账款。
async function agentSection(browser) {
  const mei = await consoleLogin(browser, 'mei')
  const menu = await menus(mei)
  const token = await login('mei')
  const response = await fetch(`${API}/api/v1/finance/receivables/summary`, { headers: { authorization: `Bearer ${token}` } })
  check('客服没有"应收账款"菜单，接口返回 403', !menu.includes('应收账款') && response.status === 403, { menu, status: response.status })
}

// 7. 手机。
async function phoneSection(browser) {
  const cai = await consoleLogin(browser, 'cai', { viewport: PHONE })
  await cai.goto(`${CONSOLE}/receivables`)
  await cai.locator('[data-testid="receivable-summary"]').waitFor()
  await cai.locator('[data-testid="receivable-cards"] .card').first().waitFor()
  await cai.waitForLoadState('networkidle')
  const cards = await cai.locator('[data-testid="receivable-cards"] .card').count()
  const scroll = await cai.evaluate(() => ({
    width: document.documentElement.scrollWidth,
    inner: window.innerWidth,
    table: getComputedStyle(document.querySelector('.table-wrap')).display,
  }))
  check('手机：应收账款变成卡片（4 张），表格隐藏，没有横向滚动', cards === 4 && scroll.table === 'none' && scroll.width <= scroll.inner, { cards, scroll })
  await shot(cai, '7-phone')
}

async function run(browser) {
  const ctx = await prepareTenant()
  const admin = await adminHome(browser)
  await receivablesPage(admin, ctx)
  await financeRole(admin, ctx)
  await financeSection(browser, ctx)
  await agentSection(browser)
  await phoneSection(browser)
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
