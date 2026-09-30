// P8 库存验收：商品库存、Excel 盘点与入库、手动调整与库存记录、订单占用与发货出库、库存不足提示
// （设计文档 §25.12）。
//
// 1. 管理员下载商品模板（有"库存""库存预警"列），按"盘点"导入：预览标出每个商品的库存从多少变为多少，
//    确认后列表显示可用库存，没填库存的商品"不管理"。
// 2. 主管（能调整库存、不能维护商品库）用只有"代码"和"数量"两列的表格按"入库"导入；手动出库一台
//    损坏的门铃，门铃库存不足：列表标"不足"，"库存不足"筛选只剩它；库存记录里有导入盘点、导入入库和
//    出库，以及操作人。
// 3. 客服确认一张超出可用库存的订单：确认框提示库存不足（不拦截），确认后订单详情标出库存不足的商品。
// 4. 另一张订单排在后面、占不到库存：工人在手机上的加工页看到"库存不足"（不显示数量）。
// 5. 发货后出库：现有库存减少，库存记录里有"订单出库"和订单号。
//
// 前置：后端、控制台。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p8-inventory-acceptance.cjs
const { chromium } = require('playwright')
const { execFileSync } = require('child_process')
const fs = require('fs')
const path = require('path')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p8-inventory')
const BACKEND_DIR = path.resolve(__dirname, '../../backend')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `p8-${RUN}`
const PASSWORD = 'demo-pass-2026'
const PHONE_VIEWPORT = { width: 390, height: 844 }

const LOCK = '智能门锁 X1'
const BELL = '可视门铃 D1'
const SERVICE = '上门安装服务'
const PRODUCTS = [
  { 名称: LOCK, 代码: 'LOCK-X1', 建议零售价: '1299', 库存: '5', 库存预警: '2' },
  { 名称: BELL, 代码: 'BELL-D1', 建议零售价: '199', 库存: '1' },
  { 名称: SERVICE, 代码: 'SVC-01', 建议零售价: '100' },
]

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
  const tokens = await json(`${API}/api/v1/auth/login`, {
    method: 'POST',
    body: { tenant_code: TENANT, username, password: PASSWORD },
  })
  return tokens.access_token
}

// 读写 Excel：用后端自带的表格读写（只用标准库），与商品导入导出是同一套实现。
const XLSX_HELPER = `
import json, sys
from app.core.xlsx import Column, Sheet, write_workbook
from app.modules.kb.parsers import parse_sheet

mode = sys.argv[1]
if mode == 'fill':
    source, target, rows = sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
    with open(source, 'rb') as f:
        table = parse_sheet(source, f.read())
    header = table[0]
    keys = [title.rstrip('*') for title in header]
    body = table[1:2] + [[row.get(key, '') for key in keys] for row in rows]
else:
    target, header, body = sys.argv[2], json.loads(sys.argv[3]), json.loads(sys.argv[4])
with open(target, 'wb') as f:
    f.write(write_workbook([Sheet('商品', [Column(title) for title in header], rows=body)]))
print(json.dumps(header, ensure_ascii=False))
`

function xlsx(...args) {
  const output = execFileSync('uv', ['run', 'python', '-c', XLSX_HELPER, ...args], {
    cwd: BACKEND_DIR,
    env: process.env,
    encoding: 'utf-8',
  })
  const lines = output.trim().split('\n')
  return JSON.parse(lines[lines.length - 1])
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
      name: `库存验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  const admin = await login('admin')
  for (const [username, name, role] of [
    ['mei', '客服小美', 'agent'],
    ['lead', '主管老周', 'supervisor'],
    ['wang', '工人老王', 'worker'],
  ]) {
    await json(`${API}/api/v1/staff`, {
      method: 'POST',
      token: admin,
      body: { username, display_name: name, password: PASSWORD, role_codes: [role] },
    })
  }
  const mei = await login('mei')
  const customer = await json(`${API}/api/v1/customers`, {
    method: 'POST',
    token: mei,
    body: { display_name: '李女士' },
  })
  return { admin, mei, customerId: customer.id }
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

async function shot(page, name, fullPage = false) {
  await page.waitForTimeout(600)
  await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage })
}

async function seen(locator, timeout = 20000) {
  return locator
    .first()
    .waitFor({ timeout })
    .then(() => true)
    .catch(() => false)
}

async function consoleLogin(browser, username, viewport) {
  const page = await newPage(browser, username, viewport)
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

async function download(page, click, name) {
  const [file] = await Promise.all([page.waitForEvent('download', { timeout: 20000 }), click()])
  const target = path.resolve(SHOTS, name)
  await file.saveAs(target)
  return target
}

const productRow = (page, code) =>
  page.locator('[data-testid="products-table"] .el-table__row', { hasText: code })

async function products(token, query = '') {
  return (await json(`${API}/api/v1/products?limit=50${query}`, { token })).items
}

async function byCode(token, code) {
  return (await products(token)).find((p) => p.code === code)
}

async function closeDialog(page) {
  await page.keyboard.press('Escape')
  await page.locator('.el-overlay:visible').last().waitFor({ state: 'hidden' }).catch(() => null)
}

// ---- 1. 管理员按"盘点"导入商品和库存 ----

async function stocktakeSection(browser, ctx) {
  const page = await consoleLogin(browser, 'admin')
  await menu(page, '商品')
  await page.click('[data-testid="products-import"]')
  const dialog = page.locator('[data-testid="product-import"]')
  await dialog.locator('[data-testid="product-template"]').waitFor()
  const template = await download(
    page,
    () => dialog.locator('[data-testid="product-template"]').click(),
    'template.xlsx',
  )
  const filled = path.resolve(SHOTS, 'stocktake.xlsx')
  const header = xlsx('fill', template, filled, JSON.stringify(PRODUCTS))
  const mode = await dialog
    .locator('[data-testid="import-stock-mode"] .is-active')
    .innerText()
    .catch(() => '')
  check(
    '商品模板有"库存""库存预警"列；上传前默认按"盘点"',
    header.includes('库存') && header.includes('库存预警') && mode.includes('盘点'),
    { header, mode },
  )
  await dialog.locator('[data-testid="product-import-file"]').setInputFiles(filled)
  await dialog.locator('[data-testid="product-import-summary"]').waitFor()
  const changes = await dialog.locator('[data-testid="product-import-stock"]').allInnerTexts()
  const rows = await dialog.locator('[data-testid="product-import-stock-rows"]').innerText()
  await shot(page, '1-stocktake-preview')
  check(
    '预览：盘点 2 个商品，库存从"—"（原来不管理）变为 5 和 1；没填库存的不修改',
    rows.includes('盘点 2') && changes.map((c) => c.trim()).join('|') === '— → 5|— → 1',
    { rows, changes },
  )
  await dialog.locator('[data-testid="product-import-confirm"]').click()
  await dialog.locator('.el-button', { hasText: '完成' }).click()
  await dialog.waitFor({ state: 'hidden' })
  await productRow(page, 'LOCK-X1').waitFor()
  const lock = await productRow(page, 'LOCK-X1').locator('[data-testid="product-stock"]').innerText()
  const service = await productRow(page, 'SVC-01').locator('[data-testid="product-stock"]').innerText()
  await shot(page, '2-products-after-stocktake')
  check(
    '列表显示可用库存（门锁可用 5），没填库存的服务"不管理"',
    lock.includes('可用 5') && service.includes('不管理'),
    { lock, service },
  )
  return page
}

// ---- 2. 主管按"入库"导入、手动出库、库存不足与库存记录 ----

async function supervisorSection(browser, ctx) {
  const page = await consoleLogin(browser, 'lead')
  await menu(page, '商品')
  const newButton = await page.locator('[data-testid="new-product"]').count()
  await page.click('[data-testid="products-import"]')
  const dialog = page.locator('[data-testid="product-import"]')
  await dialog.locator('[data-testid="import-stock-mode"]').waitFor()
  await dialog.locator('[data-testid="import-stock-mode"] label', { hasText: '入库' }).click()
  const receipt = path.resolve(SHOTS, 'receipt.xlsx')
  xlsx('write', receipt, JSON.stringify(['代码', '数量']), JSON.stringify([['LOCK-X1', '3']]))
  await dialog.locator('[data-testid="product-import-file"]').setInputFiles(receipt)
  await dialog.locator('[data-testid="product-import-summary"]').waitFor()
  const change = (await dialog.locator('[data-testid="product-import-stock"]').innerText()).trim()
  const rows = await dialog.locator('[data-testid="product-import-stock-rows"]').innerText()
  await shot(page, '3-receipt-preview')
  check(
    '主管（不能维护商品库）用"代码 + 数量"两列的表格按"入库"导入：门锁 5 → 8',
    newButton === 0 && rows.includes('入库 1') && change === '5 → 8',
    { newButton, rows, change },
  )
  await dialog.locator('[data-testid="product-import-confirm"]').click()
  await dialog.locator('.el-button', { hasText: '完成' }).click()
  await dialog.waitFor({ state: 'hidden' })

  // 手动出库一台损坏的门铃：门铃库存不足。
  await productRow(page, 'BELL-D1').locator('[data-testid="product-adjust-stock"]').click()
  const stock = page.locator('[data-testid="stock-dialog"]')
  await stock.waitFor()
  await stock.locator('[data-testid="stock-mode"] label', { hasText: '出库' }).click()
  await stock.locator('[data-testid="stock-quantity"] input').fill('1')
  await stock.locator('[data-testid="stock-quantity"] input').press('Tab')
  await stock.locator('input[data-testid="stock-note"]').fill('样机损坏')
  const after = await stock.locator('[data-testid="stock-after"]').innerText()
  await shot(page, '4-adjust-dialog')
  await stock.locator('[data-testid="stock-save"]').click()
  await stock.waitFor({ state: 'hidden' })
  const low = await seen(productRow(page, 'BELL-D1').locator('[data-testid="product-stock-low"]'))
  await page.click('[data-testid="products-low-stock"]')
  await page.waitForTimeout(800)
  const listed = await page.locator('[data-testid="products-table"] .el-table__row').allInnerTexts()
  await shot(page, '5-low-stock-filter')
  check(
    '手动出库（调整后现有库存 0）：门铃标"不足"，"库存不足"筛选只剩门铃',
    after.includes('调整后现有库存 0') && low && listed.length === 1 && listed[0].includes('BELL-D1'),
    { after, low, listed: listed.length },
  )

  // 库存记录：导入入库（主管）、导入盘点（管理员）。
  await page.locator('[data-testid="stock-filter"]').click()
  await page.locator('.el-select-dropdown__item:visible', { hasText: /^管理库存的$/ }).click()
  await productRow(page, 'LOCK-X1').waitFor()
  await productRow(page, 'LOCK-X1').locator('[data-testid="product-stock-history"]').click()
  const drawer = page.locator('[data-testid="stock-history"]')
  await drawer.locator('[data-testid="stock-movement-kind"]').first().waitFor()
  const kinds = await drawer.locator('[data-testid="stock-movement-kind"]').allInnerTexts()
  const deltas = await drawer.locator('[data-testid="stock-movement-delta"]').allInnerTexts()
  const text = await drawer.innerText()
  await shot(page, '6-stock-history')
  check(
    '库存记录：导入入库 +3（主管老周）、导入盘点 +5（管理员）',
    kinds.join('|') === '导入入库|导入盘点' &&
      deltas.join('|') === '+3|+5' &&
      text.includes('主管老周') &&
      text.includes('管理员'),
    { kinds, deltas },
  )
  await closeDialog(page)
  return page
}

// ---- 3. 客服确认超出库存的订单 ----

async function confirmSection(browser, ctx, products) {
  const order = await json(`${API}/api/v1/orders`, {
    method: 'POST',
    token: ctx.mei,
    body: {
      customer_id: ctx.customerId,
      items: [
        { product_id: products.lock, quantity: 6 },
        { product_id: products.bell, quantity: 1 },
      ],
      receiver: { name: '李女士', phone: '13800001111', address: '上海市浦东新区世纪大道100号' },
    },
  })
  const page = await consoleLogin(browser, 'mei')
  await menu(page, '订单')
  await page.click('[data-testid="order-view-pending_review"]')
  const row = page.locator('[data-testid="orders-table"] .el-table__row', { hasText: order.no })
  await row.waitFor()
  await row.click()
  const drawer = page.locator('[data-testid="order-drawer"]')
  await drawer.locator('[data-testid="order-confirm"]').click()
  const dialog = page.locator('[data-testid="confirm-order-dialog"]')
  await dialog.waitFor()
  const warning = await dialog
    .locator('[data-testid="confirm-stock-short"]')
    .innerText()
    .catch(() => '')
  await dialog.locator('[data-testid="confirm-method"] label', { hasText: '货到付款' }).click()
  await shot(page, '7-confirm-warning')
  await dialog.locator('[data-testid="confirm-submit"]').click()
  await dialog.waitFor({ state: 'hidden' })
  await drawer.locator('[data-testid="order-status"]', { hasText: '已确认' }).waitFor()
  const hints = (await drawer.locator('[data-testid="order-item-stock"]').allInnerTexts()).map((t) =>
    t.trim(),
  )
  await shot(page, '8-order-stock-hints')
  check(
    '确认框提示门铃库存不足（需要 1，可用 0），仍然可以确认；确认后门锁可用 2、门铃标出库存不足',
    warning.includes(`${BELL}：需要 1，可用 0`) &&
      hints.join('|') === '可用 2|库存不足（可用 -1）',
    { warning, hints },
  )
  return order
}

// ---- 4. 排在后面的订单：工人看到库存不足 ----

async function workerSection(browser, ctx, products, first) {
  const second = await json(`${API}/api/v1/orders`, {
    method: 'POST',
    token: ctx.mei,
    body: {
      customer_id: ctx.customerId,
      items: [{ product_id: products.lock, quantity: 3 }],
      receiver: { name: '李女士', phone: '13800001111', address: '上海市浦东新区世纪大道100号' },
    },
  })
  await json(`${API}/api/v1/orders/${second.id}/confirm`, {
    method: 'POST',
    token: ctx.mei,
    body: { payment_method: 'cod', notify_customer: false },
  })
  const page = await consoleLogin(browser, 'wang', PHONE_VIEWPORT)
  await page.waitForURL(/\/production/)
  const card = (no) => page.locator(`[data-testid="production-order"][data-order-no="${no}"]`)
  const item = (no, name) => card(no).locator(`[data-testid="production-item"][data-item-name="${name}"]`)
  await card(second.no).waitFor()
  const flag = (no, name) => item(no, name).locator('[data-testid="production-item-stock-short"]').count()
  const flags = {
    firstLock: await flag(first.no, LOCK),
    firstBell: await flag(first.no, BELL),
    secondLock: await flag(second.no, LOCK),
  }
  const text = await page.locator('[data-testid="production-list"]').innerText()
  await shot(page, '9-worker-stock-short-phone', true)
  check(
    '工人加工页：按确认先后占用库存，第一张的门铃（库存 0）和后一张的门锁（8 台里的第 7~9 台）标"库存不足"，不显示数量',
    flags.firstLock === 0 && flags.firstBell === 1 && flags.secondLock === 1 && !text.includes('可用'),
    flags,
  )
  return second
}

// ---- 5. 发货出库 ----

async function shipSection(page, ctx, products, first) {
  await json(`${API}/api/v1/orders/${first.id}/start`, { method: 'POST', token: ctx.mei })
  await json(`${API}/api/v1/orders/${first.id}/ship`, {
    method: 'POST',
    token: ctx.mei,
    body: { shipping_company: '顺丰速运', tracking_no: 'SF8000000001', notify_customer: false },
  })
  const lock = await byCode(ctx.admin, 'LOCK-X1')
  await page.reload()
  await page.locator('[data-testid="products-table"]').waitFor()
  await productRow(page, 'LOCK-X1').locator('[data-testid="product-stock-history"]').click()
  const drawer = page.locator('[data-testid="stock-history"]')
  await drawer.locator('[data-testid="stock-movement-kind"]').first().waitFor()
  const kind = await drawer.locator('[data-testid="stock-movement-kind"]').first().innerText()
  const orderNo = await drawer.locator('[data-testid="stock-movement-order"]').first().innerText()
  await shot(page, '10-ship-out-history')
  check(
    '发货后出库：门锁现有 8 → 2（另一张订单占用 3，可用 -1），库存记录里有"订单出库"和订单号',
    lock.stock === 2 &&
      lock.stock_reserved === 3 &&
      lock.stock_available === -1 &&
      kind === '订单出库' &&
      orderNo.includes(first.no),
    { stock: lock.stock, reserved: lock.stock_reserved, kind, orderNo },
  )
}

async function run(browser) {
  const ctx = await prepareTenant()
  await stocktakeSection(browser, ctx)
  const lead = await supervisorSection(browser, ctx)
  const ids = {
    lock: (await byCode(ctx.admin, 'LOCK-X1')).id,
    bell: (await byCode(ctx.admin, 'BELL-D1')).id,
  }
  const first = await confirmSection(browser, ctx, ids)
  await workerSection(browser, ctx, ids, first)
  await shipSection(lead, ctx, ids, first)
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
