// P19 验收：盈利报表（设计文档 §30）。
//
// 1. 管理员的菜单里有"盈利报表"；本月的利润表：销售收入 ¥3,079.00（3 笔，不含草稿和取消的订单）、
//    销售成本 ¥1,780.00、毛利 ¥1,299.00（42.2%）；安装服务没有成本价，页面提示"毛利偏高"；上期是上月
//    1 日确认的门锁 ¥1,299.00。
// 2. 收支登记：上个月标了"每月固定"的房租提示一键登记到本月；登记工资（支出）和废料收入（收入）；
//    利润表里费用 ¥11,000.00、净利润 -¥9,501.00（红色），按类别展开；每月净利润的柱状图本月是亏损。
// 3. 毛利分析：按商品，门锁毛利 ¥900.00 在第一行、安装服务标"缺成本"；按订单，亏本的那笔（门铃 80 元
//    卖出，成本 90）在第一行，毛利 -¥10.00 标红。
// 4. 导出 Excel。
// 5. 给安装服务补上成本价后，提示消失，销售成本变为 ¥1,880.00。
// 6. 主管没有"盈利报表"菜单，接口 403；只给了 profit:view 的自定义角色能看、不能登记。
// 7. 手机上没有横向滚动。
//
// 前置：后端、控制台。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p19-profit-acceptance.cjs
const { chromium } = require('playwright')
const { execFileSync } = require('child_process')
const fs = require('fs')
const path = require('path')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p19-profit')
const BACKEND_DIR = path.resolve(__dirname, '../../backend')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `p19-${RUN}`
const PASSWORD = 'demo-pass-2026'
const DESKTOP = { width: 1440, height: 900 }
const PHONE = { width: 390, height: 844 }
const RECEIVER = { name: '王先生', phone: '13800001111', address: '上海市浦东新区世纪大道 100 号' }

const summary = { tenant: TENANT, checks: [], consoleErrors: [] }
const check = (name, ok, detail) =>
  summary.checks.push(
    `${ok ? 'PASS' : 'FAIL'} ${name}${ok || detail === undefined ? '' : ` -> ${JSON.stringify(detail)}`}`,
  )
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

/** 在后端目录执行一段 Python：把订单的确认时间改到上个月（接口不能填过去的确认时间）。 */
const CONFIRMED_AT = `
import asyncio, sys, uuid
from datetime import datetime
from sqlalchemy import text
from app.core.config import get_settings
from app.db.session import Database

async def main(tenant_id, order_id, at):
    db = Database(get_settings())
    async with db.tenant_session(uuid.UUID(tenant_id)) as session:
        await session.execute(
            text("UPDATE orders SET confirmed_at = :at WHERE id = CAST(:id AS uuid)"),
            {"at": datetime.fromisoformat(at), "id": order_id},
        )
        await session.commit()
    await db.dispose()
    print("ok")

asyncio.run(main(*sys.argv[1:]))
`

function setConfirmedAt(tenantId, orderId, at) {
  const output = execFileSync('uv', ['run', 'python', '-c', CONFIRMED_AT, tenantId, orderId, at], {
    cwd: BACKEND_DIR,
    env: process.env,
    encoding: 'utf-8',
  })
  if (!output.trim().endsWith('ok')) throw new Error(`setConfirmedAt failed: ${output}`)
}

const pad = (n) => String(n).padStart(2, '0')
/** 北京时间的今天、本月 1 日和上月 1 日（租户的默认时区）。 */
function calendar() {
  const now = new Date(Date.now() + 8 * 3600 * 1000)
  const y = now.getUTCFullYear()
  const m = now.getUTCMonth()
  const prev = new Date(Date.UTC(y, m - 1, 1))
  return {
    today: `${y}-${pad(m + 1)}-${pad(now.getUTCDate())}`,
    month: `${y}-${pad(m + 1)}`,
    previousFirst: `${prev.getUTCFullYear()}-${pad(prev.getUTCMonth() + 1)}-01`,
  }
}

// 租户、主管、只能看报表的员工、三个商品、两位客户、订单和上个月的房租。
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
      name: `盈利验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  const admin = await login('admin')
  const post = (url, body) => json(`${API}${url}`, { method: 'POST', token: admin, body })
  await post('/api/v1/staff', {
    username: 'boss',
    display_name: '主管老周',
    password: PASSWORD,
    role_codes: ['supervisor'],
  })
  await post('/api/v1/roles', { code: 'profit_view', name: '看利润', permissions: ['profit:view'] })
  await post('/api/v1/staff', {
    username: 'gao',
    display_name: '股东老高',
    password: PASSWORD,
    role_codes: ['profit_view'],
  })
  const lock = await post('/api/v1/products', {
    code: 'LOCK-X1',
    name: '智能门锁 X1',
    retail_price: '1299',
    cost_price: '800',
  })
  const bell = await post('/api/v1/products', {
    code: 'BELL-D1',
    name: '可视门铃 D1',
    retail_price: '199',
    cost_price: '90',
  })
  const install = await post('/api/v1/products', { code: 'SVC-1', name: '安装服务', retail_price: '300' })
  const li = await post('/api/v1/customers', { display_name: '李女士' })
  const wang = await post('/api/v1/customers', { display_name: '王先生', company: '王记五金' })

  const order = async (customer, items, extra = {}) => {
    const created = await post('/api/v1/orders', { customer_id: customer.id, items, receiver: RECEIVER, ...extra })
    const result = await post(`/api/v1/orders/${created.id}/confirm`, {
      notify_customer: false,
      payment_method: 'online',
    })
    return result.order
  }
  // 本月：两把门锁优惠 98（合计 2500）；门铃 + 安装服务（没有成本价）；门铃按 80 元卖出（亏 10 元）。
  const a = await order(li, [{ product_id: lock.id, quantity: 2 }], { discount: '98' })
  const b = await order(wang, [
    { product_id: bell.id, quantity: 1 },
    { product_id: install.id, quantity: 1 },
  ])
  const c = await order(li, [{ product_id: bell.id, quantity: 1, unit_price: '80' }])
  // 草稿不算；确认后又取消的不算。
  await post('/api/v1/orders', {
    customer_id: li.id,
    items: [{ product_id: lock.id, quantity: 5 }],
    receiver: RECEIVER,
    submit: false,
  })
  const cancelled = await order(wang, [{ product_id: lock.id, quantity: 3 }])
  await post(`/api/v1/orders/${cancelled.id}/cancel`, { reason: '客户不要了', notify_customer: false })
  // 上月 1 日中午确认的门锁：算上期。
  const days = calendar()
  const previous = await order(wang, [{ product_id: lock.id, quantity: 1 }])
  setConfirmedAt(tenant.id, previous.id, `${days.previousFirst}T12:00:00+08:00`)
  await post(`/api/v1/orders/${a.id}/payments`, { amount: '1000', channel: 'bank' })
  // 上个月 1 日的房租，标了"每月固定"。
  await post('/api/v1/profit/entries', {
    kind: 'expense',
    category: '房租物业',
    amount: '3000',
    occurred_on: days.previousFirst,
    recurring: true,
    note: '厂房',
  })
  return { tenant, admin, install, a, b, c, days }
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

const tile = (page, key) => page.locator(`[data-testid="profit-tile-${key}"]`)
const tileValue = (page, key) => tile(page, key).locator('.value').innerText()
const rows = (page, testid) => page.locator(`[data-testid="${testid}"] .el-table__body-wrapper .el-table__row`)

/** 等条件成立（列表、数字重新取数是异步的）。 */
async function until(fn, timeout = 10_000) {
  const deadline = Date.now() + timeout
  for (;;) {
    const value = await fn()
    if (value || Date.now() > deadline) return value
    await new Promise((resolve) => setTimeout(resolve, 200))
  }
}

async function statementRow(page, label) {
  const row = rows(page, 'profit-statement').filter({ hasText: new RegExp(`^\\s*${label}`) }).first()
  return (await row.locator('td').allInnerTexts()).map((t) => t.trim())
}

// 1. 管理员：菜单、本月的利润表、成本缺失。
async function statementSection(browser, ctx) {
  const admin = await consoleLogin(browser, 'admin')
  const menu = await menus(admin)
  check('管理员的菜单里有"盈利报表"，排在"报表"之后', menu.indexOf('盈利报表') === menu.indexOf('报表') + 1, menu)
  await admin.locator('[data-testid="main-menu"] .el-menu-item', { hasText: /^\s*盈利报表/ }).click()
  await admin.waitForURL(/\/profit/)
  await tile(admin, 'revenue').waitFor()
  await admin.waitForLoadState('networkidle')
  const values = {
    revenue: await tileValue(admin, 'revenue'),
    cost: await tileValue(admin, 'cost'),
    gross: await tileValue(admin, 'gross'),
  }
  const revenueHint = await tile(admin, 'revenue').locator('.hint').innerText()
  const grossHint = await tile(admin, 'gross').locator('.hint').innerText()
  check(
    '本月：销售收入 ¥3,079.00（3 笔）、销售成本 ¥1,780.00、毛利 ¥1,299.00（毛利率 42.2%）',
    values.revenue === '¥3,079.00' &&
      values.cost === '¥1,780.00' &&
      values.gross === '¥1,299.00' &&
      revenueHint.includes('订单 3 笔') &&
      grossHint.includes('42.2%'),
    { values, revenueHint, grossHint },
  )
  const gap = await admin.locator('[data-testid="profit-cost-gap"]').innerText()
  check('安装服务没有成本价：提示 1 个商品行、涉及收入 ¥300.00、毛利偏高', gap.includes('1 个商品行') && gap.includes('¥300.00') && gap.includes('安装服务'), gap)
  const revenueRow = await statementRow(admin, '一、销售收入')
  check('利润表：本期 ¥3,079.00、上期（上月 1 日确认的门锁）¥1,299.00', revenueRow[1] === '¥3,079.00' && revenueRow[2] === '¥1,299.00', revenueRow)
  await shot(admin, '1-statement')
  return admin
}

// 2. 收支登记：一键登记每月固定、登记支出和收入，利润表随之变化。
async function entriesSection(admin) {
  await admin.locator('#tab-entries').click()
  await admin.locator('[data-testid="entries-recurring"]').waitFor()
  const banner = await admin.locator('[data-testid="entries-recurring"]').innerText()
  check('上个月的房租（每月固定）提示一键登记到本月', banner.includes('1 笔每月固定') && banner.includes('房租物业'), banner)
  await admin.locator('[data-testid="entries-copy-recurring"]').click()
  await admin.locator('[data-testid="entries-recurring"]').waitFor({ state: 'detached' })
  const afterCopy = await until(async () => (await rows(admin, 'entries-table').count()) === 1)
  const rent = afterCopy ? await rows(admin, 'entries-table').first().innerText() : ''
  check('一键登记后本月有一笔房租 ¥3,000.00（每月固定）', rent.includes('房租物业') && rent.includes('¥3,000.00') && rent.includes('每月固定'), rent)

  const register = async (button, category, amount) => {
    await admin.locator(`[data-testid="${button}"]`).click()
    const dialog = admin.locator('[data-testid="entry-dialog"]')
    await dialog.locator('input[data-testid="entry-category"]').waitFor()
    await dialog.locator('input[data-testid="entry-category"]').fill(category)
    await admin.keyboard.press('Tab')
    await dialog.locator('[data-testid="entry-amount"] input').fill(amount)
    await admin.keyboard.press('Tab')
    await dialog.locator('[data-testid="entry-save"]').click()
    await dialog.waitFor({ state: 'hidden' })
  }
  await register('entry-add-expense', '工资社保', '8000')
  await register('entry-add-income', '废料收入', '200')
  const listed = await until(async () => (await rows(admin, 'entries-table').count()) === 3)
  const totals = await admin.locator('[data-testid="entries-totals"]').innerText()
  check('登记工资 ¥8,000.00 和废料收入 ¥200.00：支出合计 ¥11,000.00、收入合计 ¥200.00', listed && totals.includes('¥11,000.00') && totals.includes('¥200.00'), totals)
  await shot(admin, '2-entries')

  await admin.locator('#tab-statement').click()
  const net = await until(async () => {
    const value = await tileValue(admin, 'net')
    return value === '-¥9,501.00' ? value : null
  })
  const danger = await tile(admin, 'net').locator('.value.danger').count()
  const expenses = await tileValue(admin, 'expenses')
  check('利润表：费用 ¥11,000.00、净利润 -¥9,501.00（红色）', net === '-¥9,501.00' && danger === 1 && expenses === '¥11,000.00', { net, danger, expenses })
  const salary = await statementRow(admin, '工资社保')
  const scrap = await statementRow(admin, '废料收入')
  check('费用和其他收入按类别展开（工资社保 ¥8,000.00、废料收入 ¥200.00）', salary[1] === '¥8,000.00' && scrap[1] === '¥200.00', { salary, scrap })
  const bars = await admin.locator('[data-testid="profit-chart"] path.column').count()
  const lastLoss = await admin.locator('[data-testid="profit-chart"] path.column').last().getAttribute('class')
  check('每月净利润：12 个月的柱子，本月是亏损（红色）', bars === 12 && /loss/.test(lastLoss || ''), { bars, lastLoss })
  await shot(admin, '3-statement-with-entries')
}

// 3. 毛利分析：按商品、按订单。
async function breakdownSection(admin, ctx) {
  await admin.locator('#tab-breakdown').click()
  await rows(admin, 'breakdown-table').first().waitFor()
  await admin.waitForLoadState('networkidle')
  const first = await rows(admin, 'breakdown-table').first().innerText()
  const install = await rows(admin, 'breakdown-table').filter({ hasText: '安装服务' }).first().innerText()
  check('按商品：门锁毛利 ¥900.00 排第一，安装服务标"缺成本"', first.includes('智能门锁 X1') && first.includes('¥900.00') && install.includes('缺成本'), { first, install })
  await shot(admin, '4-breakdown-products')

  await admin.locator('[data-testid="breakdown-dim-order"]').click()
  const loss = await until(async () => {
    const text = await rows(admin, 'breakdown-table').first().innerText()
    return text.includes(ctx.c.no) ? text : null
  })
  const red = await rows(admin, 'breakdown-table').first().evaluate((row) => row.classList.contains('loss-row'))
  check('按订单（毛利率从低到高）：亏本的那笔在第一行，毛利 -¥10.00 标红', Boolean(loss) && loss.includes('-¥10.00') && red, { loss, red })
  await rows(admin, 'breakdown-table').first().locator('[data-testid="breakdown-order"]').click()
  const drawer = admin.locator('.el-drawer').filter({ hasText: ctx.c.no }).first()
  await drawer.waitFor()
  check('点订单号打开订单详情', await drawer.isVisible())
  await shot(admin, '5-breakdown-orders')
  await admin.keyboard.press('Escape')
}

// 4. 导出 Excel。
async function exportSection(admin) {
  const [download] = await Promise.all([
    admin.waitForEvent('download'),
    admin.locator('[data-testid="profit-export"]').click(),
  ])
  const file = path.join(SHOTS, 'profit.xlsx')
  await download.saveAs(file)
  const head = fs.readFileSync(file).subarray(0, 2).toString()
  const name = download.suggestedFilename()
  check('导出 Excel（profit-起止日期.xlsx）', /^profit-\d{8}-\d{8}\.xlsx$/.test(name) && head === 'PK', { name, head })
}

// 5. 补上成本价后提示消失、成本重算。
async function fillCost(admin, ctx) {
  await json(`${API}/api/v1/products/${ctx.install.id}`, {
    method: 'PUT',
    token: ctx.admin,
    body: { code: 'SVC-1', name: '安装服务', retail_price: '300', cost_price: '100' },
  })
  await admin.goto(`${CONSOLE}/profit`)
  await tile(admin, 'cost').waitFor()
  await admin.waitForLoadState('networkidle')
  const cost = await until(async () => {
    const value = await tileValue(admin, 'cost')
    return value === '¥1,880.00' ? value : null
  })
  const gap = await admin.locator('[data-testid="profit-cost-gap"]').count()
  check('给安装服务补上成本价：提示消失，销售成本 ¥1,880.00', cost === '¥1,880.00' && gap === 0, { cost, gap })
}

// 6. 主管看不到；只给了查看权限的角色能看、不能登记。
async function permissionSection(browser) {
  const boss = await consoleLogin(browser, 'boss')
  const bossMenu = await menus(boss)
  const token = await login('boss')
  const response = await fetch(`${API}/api/v1/profit/summary`, { headers: { authorization: `Bearer ${token}` } })
  check('主管没有"盈利报表"菜单，接口 403', !bossMenu.includes('盈利报表') && response.status === 403, { bossMenu, status: response.status })

  const gao = await consoleLogin(browser, 'gao')
  const gaoMenu = await menus(gao)
  await gao.goto(`${CONSOLE}/profit?tab=entries`)
  await gao.locator('[data-testid="entries-table"]').waitFor()
  await gao.waitForLoadState('networkidle')
  const buttons = await gao.locator('[data-testid="entry-add-expense"]').count()
  const recordRows = await rows(gao, 'entries-table').count()
  check('只能看报表的角色：菜单只有"盈利报表"，能看到 3 笔收支，没有登记按钮', gaoMenu.length === 1 && gaoMenu[0] === '盈利报表' && recordRows === 3 && buttons === 0, { gaoMenu, recordRows, buttons })
}

// 7. 手机。
async function phoneSection(browser) {
  const admin = await consoleLogin(browser, 'admin', { viewport: PHONE })
  await admin.goto(`${CONSOLE}/profit`)
  await tile(admin, 'net').waitFor()
  await admin.waitForLoadState('networkidle')
  const layout = await admin.evaluate(() => {
    const picker = document.querySelector('[data-testid="profit-filters"] .el-date-editor').getBoundingClientRect()
    return {
      width: document.documentElement.scrollWidth,
      inner: window.innerWidth,
      columns: getComputedStyle(document.querySelector('[data-testid="profit-tiles"]')).gridTemplateColumns.split(' ').length,
      pickerRight: Math.round(picker.right),
    }
  })
  check('手机：数字两列，月份选择框不超出屏幕，没有横向滚动', layout.columns === 2 && layout.width <= layout.inner && layout.pickerRight <= layout.inner, layout)
  await shot(admin, '6-phone')
}

async function run(browser) {
  const ctx = await prepareTenant()
  const admin = await statementSection(browser, ctx)
  await entriesSection(admin)
  await breakdownSection(admin, ctx)
  await exportSection(admin)
  await fillCost(admin, ctx)
  await permissionSection(browser)
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
