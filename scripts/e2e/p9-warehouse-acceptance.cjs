// P9 仓库验收：材料库存和成品库存、配方、仓管、领料单和入库单（设计文档 §25.13）。
//
// 1. 管理员打开"仓库"：还没指定仓管时由最早创建的工人老王担任。在"材料库存"里用 Excel 导入材料
//    （类别留空按材料，库存可以是小数），再新建一个材料（没有库存，标"不足"）。
// 2. 管理员在"成品库存"里给铝合金窗设置配方（每樘用多少型材、玻璃、密封条），再把仓管指定为仓管小陈。
// 3. 客服确认两张订单：铝合金窗（需要加工）和吸顶灯（现货）。工人老王在手机上只看到铝合金窗的订单，
//    卡片提示按配方算材料不够；领取后自动打开领料单：按配方预填（订单数量 × 用量），库存不够只提示，
//    照常提交，等仓管确认；领料单开了之后才能标记完成。
// 4. 仓管小陈：菜单上有待确认的单据数，打开"仓库"直接看待确认的领料单；按实际数量修改后确认，
//    材料库存扣减（不够的变成负数），库存记录里有"领料"和单号。
// 5. 老王完成订单：开入库单（按订单预填生产好的成品），等仓管确认；小陈退回（写明原因），老王修改后
//    重新提交，小陈确认入库：成品库存增加，订单进入"待发货"。
// 6. 客服：订单详情里有领料单和入库单；现货订单直接发货（不经过加工），发货时成品库存扣减。
//
// 前置：后端、控制台。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p9-warehouse-acceptance.cjs
const { chromium } = require('playwright')
const { execFileSync } = require('child_process')
const fs = require('fs')
const path = require('path')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p9-warehouse')
const BACKEND_DIR = path.resolve(__dirname, '../../backend')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `p9-${RUN}`
const PASSWORD = 'demo-pass-2026'
const PHONE_VIEWPORT = { width: 390, height: 844 }
const RECEIVER = { name: '李女士', phone: '13800001111', address: '上海市浦东新区世纪大道100号' }

const WINDOW = '铝合金窗'
const LAMP = '吸顶灯'
const FRAME = '铝合金型材'
const GLASS = '钢化玻璃'
const SEAL = '密封条'
const MATERIALS = [
  { 名称: FRAME, 代码: 'AL-6063', 单位: '米', 库存: '100.5', 库存预警: '20' },
  { 名称: GLASS, 代码: 'GL-5', 单位: '平方米', 库存: '2' },
]
const REJECT_REASON = '数量不对，请核对后重新提交'

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

source, target, rows = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
with open(source, 'rb') as f:
    table = parse_sheet(source, f.read())
header = table[0]
keys = [title.rstrip('*') for title in header]
body = table[1:3] + [[row.get(key, '') for key in keys] for row in rows]
with open(target, 'wb') as f:
    f.write(write_workbook([Sheet('商品', [Column(title) for title in header], rows=body)]))
print(json.dumps(header, ensure_ascii=False))
`

function fillTemplate(source, target, rows) {
  const output = execFileSync(
    'uv',
    ['run', 'python', '-c', XLSX_HELPER, source, target, JSON.stringify(rows)],
    { cwd: BACKEND_DIR, env: process.env, encoding: 'utf-8' },
  )
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
      name: `仓库验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  const admin = await login('admin')
  const staff = {}
  // 老王先创建：没有指定仓管时由他担任。
  for (const [username, name, role] of [
    ['mei', '客服小美', 'agent'],
    ['wang', '工人老王', 'worker'],
    ['cang', '仓管小陈', 'worker'],
  ]) {
    const created = await json(`${API}/api/v1/staff`, {
      method: 'POST',
      token: admin,
      body: { username, display_name: name, password: PASSWORD, role_codes: [role] },
    })
    staff[username] = created.id
  }
  const window = await json(`${API}/api/v1/products`, {
    method: 'POST',
    token: admin,
    body: { code: 'WIN-01', name: WINDOW, unit: '樘', retail_price: '1800' },
  })
  const lamp = await json(`${API}/api/v1/products`, {
    method: 'POST',
    token: admin,
    body: { code: 'LAMP-01', name: LAMP, unit: '盏', retail_price: '299', ready_made: true },
  })
  await json(`${API}/api/v1/products/${lamp.id}/stock`, {
    method: 'POST',
    token: admin,
    body: { mode: 'set', quantity: 3, note: '期初盘点' },
  })
  const mei = await login('mei')
  const customer = await json(`${API}/api/v1/customers`, {
    method: 'POST',
    token: mei,
    body: { display_name: '李女士' },
  })
  return { admin, mei, staff, customerId: customer.id, goods: { window: window.id, lamp: lamp.id } }
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
  const context = await browser.newContext({ viewport, locale: 'zh-CN', acceptDownloads: true })
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

async function menuTitles(page) {
  return page.locator('[data-testid="main-menu"] .el-menu-item').evaluateAll((els) =>
    els.map((el) => el.textContent.replace(/\d+/g, '').trim()),
  )
}

async function download(page, click, name) {
  const [file] = await Promise.all([page.waitForEvent('download', { timeout: 20000 }), click()])
  const target = path.resolve(SHOTS, name)
  await file.saveAs(target)
  return target
}

async function tab(page, name) {
  await page.locator(`[data-testid="warehouse-tab-${name}"]`).click()
}

const itemRow = (page, name) =>
  page.locator('[data-testid="warehouse-items"] .el-table__row', {
    has: page.locator(`[data-testid="warehouse-item"][data-name="${name}"]`),
  })

const docRow = (page, no) =>
  page.locator('[data-testid="warehouse-documents"] .el-table__row', {
    has: page.locator(`[data-testid="warehouse-document"][data-no="${no}"]`),
  })

// el-select（可搜索）：输入关键字后点选。
async function pick(page, select, text) {
  await select.click()
  await page.keyboard.type(text)
  const option = page.locator('.el-select-dropdown__item:visible', { hasText: text }).first()
  await option.waitFor()
  await option.click()
}

async function stockOf(token, id) {
  const p = await json(`${API}/api/v1/products/${id}`, { token })
  return { stock: p.stock, reserved: p.stock_reserved, available: p.stock_available, low: p.stock_low }
}

async function material(token, code) {
  const page = await json(`${API}/api/v1/products?kind=material&q=${code}`, { token })
  return page.items.find((p) => p.code === code)
}

async function confirmBox(page, button) {
  const box = page.locator('.el-message-box:visible')
  await box.waitFor()
  const message = await box.locator('.el-message-box__message').innerText()
  await box.locator('button', { hasText: button }).click()
  await box.waitFor({ state: 'hidden' }).catch(() => null)
  return message
}

// ---- 1. 管理员：仓管（最早创建的工人）、导入材料、新建材料 ----

async function materialsSection(browser) {
  const page = await consoleLogin(browser, 'admin')
  await menu(page, '仓库')
  const keeper = page.locator('[data-testid="warehouse-keeper"]')
  await keeper.waitFor()
  const keeperText = await keeper.innerText()
  check(
    '仓库页：还没指定仓管时由最早创建的工人老王担任',
    keeperText.includes('工人老王') && keeperText.includes('最早创建的工人'),
    keeperText,
  )

  await page.click('[data-testid="warehouse-import"]')
  const dialog = page.locator('[data-testid="product-import"]')
  await dialog.locator('[data-testid="product-template"]').waitFor()
  const title = await dialog.locator('.el-dialog__title').innerText()
  const hint = await dialog.locator('[data-testid="import-default-kind"]').innerText()
  const template = await download(
    page,
    () => dialog.locator('[data-testid="product-template"]').click(),
    'template.xlsx',
  )
  const filled = path.resolve(SHOTS, 'materials.xlsx')
  const header = fillTemplate(template, filled, MATERIALS)
  check(
    '导入材料：模板有"类别""单位""现货"列，"类别"留空的按材料导入',
    title === '导入材料' &&
      hint.includes('按材料导入') &&
      ['类别', '单位', '现货', '库存'].every((h) => header.includes(h)),
    { title, hint, header },
  )
  await dialog.locator('[data-testid="product-import-file"]').setInputFiles(filled)
  await dialog.locator('[data-testid="product-import-summary"]').waitFor()
  const changes = (await dialog.locator('[data-testid="product-import-stock"]').allInnerTexts()).map((t) =>
    t.trim(),
  )
  await shot(page, '1-materials-import-preview')
  check('预览：材料库存可以是小数（— → 100.5，— → 2）', changes.join('|') === '— → 100.5|— → 2', changes)
  await dialog.locator('[data-testid="product-import-confirm"]').click()
  await dialog.locator('.el-button', { hasText: '完成' }).click()
  await dialog.waitFor({ state: 'hidden' })

  // 新建材料：没有库存，标"不足"。
  await page.click('[data-testid="warehouse-new-material"]')
  const form = page.locator('[data-testid="product-dialog"]')
  await form.waitFor()
  const formTitle = await form.locator('.el-dialog__title').innerText()
  const priceFields = await form.locator('[data-testid="product-retail"]').count()
  await form.locator('input[data-testid="product-name"]').fill(SEAL)
  await form.locator('input[data-testid="product-code"]').fill('SEAL-01')
  await form.locator('input[data-testid="product-unit"]').fill('米')
  await form.locator('[data-testid="product-save"]').click()
  await form.waitFor({ state: 'hidden' })
  await itemRow(page, SEAL).waitFor()
  const frame = await itemRow(page, FRAME).locator('[data-testid="warehouse-item-stock"]').innerText()
  const sealLow = await itemRow(page, SEAL).locator('[data-testid="warehouse-item-low"]').count()
  await shot(page, '2-materials-list')
  check(
    '新建材料（没有售价等销售信息）；材料列表：型材现有 100.5，新材料库存 0 标"不足"',
    formTitle === '新建材料' && priceFields === 0 && frame.trim() === '100.5' && sealLow === 1,
    { formTitle, priceFields, frame, sealLow },
  )
  return page
}

// ---- 2. 管理员：成品的配方，指定仓管 ----

async function recipeSection(page, ctx) {
  await tab(page, 'goods')
  await itemRow(page, WINDOW).waitFor()
  const lampTag = await itemRow(page, LAMP).innerText()
  await itemRow(page, WINDOW).locator('[data-testid="warehouse-bom"]').click()
  const dialog = page.locator('[data-testid="bom-dialog"]')
  await dialog.waitFor()
  for (const [name, quantity] of [
    [FRAME, '2.5'],
    [GLASS, '1.2'],
    [SEAL, '3'],
  ]) {
    await pick(page, dialog.locator('[data-testid="bom-add"]'), name)
    const line = dialog.locator('.el-table__row', {
      has: page.locator(`[data-testid="bom-line"][data-name="${name}"]`),
    })
    await line.locator('[data-testid="bom-quantity"] input').fill(quantity)
    await line.locator('[data-testid="bom-quantity"] input').press('Tab')
  }
  await shot(page, '3-recipe-dialog')
  await dialog.locator('[data-testid="bom-save"]').click()
  await dialog.waitFor({ state: 'hidden' })
  const bom = await itemRow(page, WINDOW).locator('[data-testid="warehouse-bom"]').innerText()
  const saved = await json(`${API}/api/v1/products/${ctx.goods.window}/materials`, { token: ctx.admin })
  check(
    '成品库存：吸顶灯标"现货"；给铝合金窗设置配方（每樘型材 2.5 米、玻璃 1.2 平方米、密封条 3 米）',
    lampTag.includes('现货') &&
      bom.includes('3 种材料') &&
      saved.items.map((i) => `${i.name}:${i.quantity}`).join('|') === `${FRAME}:2.5|${GLASS}:1.2|${SEAL}:3`,
    { lampTag, bom, saved: saved.items.map((i) => [i.name, i.quantity]) },
  )

  // 指定仓管小陈。
  await page.click('[data-testid="warehouse-settings"]')
  const settings = page.locator('[data-testid="keeper-dialog"]')
  await settings.waitFor()
  await pick(page, settings.locator('[data-testid="keeper-select"]'), '仓管小陈')
  await settings.locator('[data-testid="keeper-save"]').click()
  await settings.waitFor({ state: 'hidden' })
  const keeper = await seen(page.locator('[data-testid="warehouse-keeper"]', { hasText: '仓管小陈' }))
  const cang = await json(`${API}/api/v1/me`, { token: await login('cang') })
  const wang = await json(`${API}/api/v1/me`, { token: await login('wang') })
  check(
    '管理员指定仓管小陈：他有确认单据的权限，老王不再是仓管',
    keeper && cang.permissions.includes('warehouse:confirm') && !wang.permissions.includes('warehouse:confirm'),
    { keeper, cang: cang.permissions, wang: wang.permissions },
  )
}

// ---- 3. 工人领取订单、按配方开领料单 ----

async function orders(ctx) {
  const create = async (lines) => {
    const order = await json(`${API}/api/v1/orders`, {
      method: 'POST',
      token: ctx.mei,
      body: { customer_id: ctx.customerId, items: lines, receiver: RECEIVER },
    })
    await json(`${API}/api/v1/orders/${order.id}/confirm`, {
      method: 'POST',
      token: ctx.mei,
      body: { payment_method: 'cod', notify_customer: false },
    })
    return order
  }
  return {
    made: await create([{ product_id: ctx.goods.window, quantity: 2 }]),
    ready: await create([{ product_id: ctx.goods.lamp, quantity: 1 }]),
  }
}

const card = (page, no) => page.locator(`[data-testid="production-order"][data-order-no="${no}"]`)

async function requisitionSection(browser, ctx) {
  const { made, ready } = ctx.orders
  const page = await consoleLogin(browser, 'wang', PHONE_VIEWPORT)
  await page.waitForURL(/\/production/)
  await card(page, made.no).waitFor()
  const titles = await menuTitles(page)
  const listed = await page
    .locator('[data-testid="production-order"]')
    .evaluateAll((els) => els.map((el) => el.getAttribute('data-order-no')))
  const short = await card(page, made.no).locator('[data-testid="production-material-short"]').innerText()
  await shot(page, '4-worker-pool-phone', true)
  check(
    '工人老王（不再是仓管）只有"加工"；待领取里只有要加工的铝合金窗订单（现货的吸顶灯不用加工），卡片提示按配方算材料不够',
    titles.length === 1 &&
      listed.includes(made.no) &&
      !listed.includes(ready.no) &&
      short.includes(GLASS) &&
      short.includes(SEAL),
    { titles, listed, short },
  )

  await card(page, made.no).locator('[data-testid="production-claim"]').click()
  const editor = page.locator('[data-testid="document-editor"]:visible')
  await editor.locator('[data-testid="document-line"]').first().waitFor()
  const lines = await editor.locator('[data-testid="document-line"]').evaluateAll((els) =>
    els.map((el) => el.getAttribute('data-name')),
  )
  const quantities = await editor
    .locator('[data-testid="document-line-quantity"] input')
    .evaluateAll((els) => els.map((el) => el.value))
  const warning = await editor.locator('[data-testid="document-short"]').innerText()
  const direct = await editor.locator('[data-testid="document-direct"]').innerText()
  await shot(page, '5-requisition-phone', true)
  check(
    '领取后自动打开领料单：按配方预填（2 樘 × 用量：密封条 6、玻璃 2.4、型材 5），库存不够只提示，提交后等仓管小陈确认',
    lines.join('|') === `${SEAL}|${GLASS}|${FRAME}` &&
      quantities.join('|') === '6|2.4|5' &&
      warning.includes(GLASS) &&
      warning.includes(SEAL) &&
      direct.includes('仓管小陈'),
    { lines, quantities, warning, direct },
  )
  await editor.locator('[data-testid="document-submit"]').click()
  await editor.waitFor({ state: 'hidden' })
  await page.locator('[data-testid="production-view-mine"]').click()
  const mine = card(page, made.no)
  await mine.locator('[data-testid="production-document"]').first().waitFor()
  const chip = await mine.locator('[data-testid="production-document"]').first().innerText()
  const done = await mine.locator('[data-testid="production-item-done"]').first().isEnabled()
  check('提交后卡片上有"领料单 LL… 待确认"，可以开始加工（标记完成）', /领料单 LL\d{8}-\d{4} 待确认/.test(chip) && done, {
    chip,
    done,
  })
  ctx.requisitionNo = chip.split(' ')[1]
  return page
}

// ---- 4. 仓管确认领料 ----

async function confirmRequisitionSection(browser, ctx) {
  const page = await consoleLogin(browser, 'cang')
  const badge = await seen(page.locator('[data-testid="warehouse-badge"]', { hasText: '1' }))
  await menu(page, '仓库')
  const row = docRow(page, ctx.requisitionNo)
  await row.waitFor()
  const active = await page.locator('#tab-requisition.is-active').count()
  check('仓管小陈：菜单上有 1 张待确认的单据，打开"仓库"直接看待确认的领料单', badge && active === 1, {
    badge,
    active,
  })
  await row.locator('[data-testid="warehouse-document-open"]').click()
  const drawer = page.locator('[data-testid="document-drawer"]')
  await drawer.locator('[data-testid="document-detail-line"]').first().waitFor()
  const glassLine = drawer.locator('.line', {
    has: page.locator(`[data-testid="document-detail-line"][data-name="${GLASS}"]`),
  })
  await glassLine.locator('[data-testid="document-confirm-quantity"] input').fill('2.5')
  await glassLine.locator('[data-testid="document-confirm-quantity"] input').press('Tab')
  await shot(page, '6-keeper-confirm-requisition')
  await drawer.locator('[data-testid="document-confirm"]').click()
  const message = await confirmBox(page, '确认')
  await drawer.locator('[data-testid="document-status"]', { hasText: '已确认' }).waitFor()
  const stocks = await drawer.locator('[data-testid="document-detail-stock"]').allInnerTexts()
  const glass = await material(ctx.admin, 'GL-5')
  const frame = await material(ctx.admin, 'AL-6063')
  await shot(page, '7-requisition-confirmed')
  check(
    '按实际数量（玻璃 2.5）确认领料：确认框提示库存不够，材料库存扣减（玻璃 2 → -0.5，型材 100.5 → 95.5）',
    message.includes('已按实际数量修改 1 行') &&
      message.includes('库存不够') &&
      glass.stock === -0.5 &&
      frame.stock === 95.5 &&
      stocks.map((s) => s.trim()).includes('2 → -0.5'),
    { message, glass: glass.stock, frame: frame.stock, stocks },
  )
  await page.keyboard.press('Escape')
  await drawer.waitFor({ state: 'hidden' })

  // 库存记录：领料和单号。
  await tab(page, 'movements')
  await page.locator('[data-testid="warehouse-movement-kind"]').first().waitFor()
  const kinds = await page.locator('[data-testid="warehouse-movement-kind"]').allInnerTexts()
  const docs = await page.locator('[data-testid="warehouse-movement-document"]').allInnerTexts()
  check(
    '库存记录里有 3 条"领料"，关联领料单号',
    kinds.filter((k) => k === '领料').length === 3 && docs.filter((d) => d.includes(ctx.requisitionNo)).length === 3,
    { kinds, docs },
  )
  return page
}

// ---- 5. 完成加工开入库单；仓管退回，工人修改后重新提交，仓管确认入库 ----

async function receiptSection(worker, keeper, ctx) {
  const { made } = ctx.orders
  await worker.reload()
  await worker.locator('[data-testid="production-views"]').waitFor()
  await worker.locator('[data-testid="production-view-mine"]').click()
  const mine = card(worker, made.no)
  await mine.waitFor()
  await mine.locator('[data-testid="production-complete"]').click()
  const editor = worker.locator('[data-testid="document-editor"]:visible')
  await editor.locator('[data-testid="document-line"]').first().waitFor()
  const title = await editor.locator('.sheet-title h3').innerText()
  const lines = await editor.locator('[data-testid="document-line"]').evaluateAll((els) =>
    els.map((el) => el.getAttribute('data-name')),
  )
  const quantity = await editor.locator('[data-testid="document-line-quantity"] input').first().inputValue()
  const intro = await editor.locator('.el-alert__title').first().innerText()
  await shot(worker, '8-receipt-phone', true)
  check(
    '完成订单：开入库单（按订单预填铝合金窗 2），还没标记的商品一并标记完成',
    title.includes('开入库单') &&
      lines.join('|') === WINDOW &&
      quantity === '2' &&
      intro.includes('一并标记为已完成'),
    { title, lines, quantity, intro },
  )
  await editor.locator('[data-testid="document-submit"]').click()
  await editor.waitFor({ state: 'hidden' })
  const pending = mine.locator('[data-testid="production-receipt"]')
  await pending.waitFor()
  const pendingText = await pending.innerText()
  check('入库单等仓管确认：订单还在"我的加工"里', pendingText.includes('等仓管确认'), pendingText)
  const receiptNo = pendingText.match(/RK\d{8}-\d{4}/)?.[0]

  // 仓管退回。
  await keeper.locator('[data-testid="warehouse-tab-receipt"]').click()
  await docRow(keeper, receiptNo).waitFor()
  await docRow(keeper, receiptNo).locator('[data-testid="warehouse-document-open"]').click()
  const drawer = keeper.locator('[data-testid="document-drawer"]')
  await drawer.locator('[data-testid="document-reject"]').waitFor()
  await drawer.locator('[data-testid="document-reject"]').click()
  const prompt = keeper.locator('.el-message-box:visible')
  await prompt.locator('input').fill(REJECT_REASON)
  await prompt.locator('button', { hasText: '确定' }).click()
  await drawer.locator('[data-testid="document-status"]', { hasText: '已退回' }).waitFor()
  await keeper.keyboard.press('Escape')
  await drawer.waitFor({ state: 'hidden' })

  // 工人：看到退回原因，修改后重新提交。
  await worker.reload()
  await worker.locator('[data-testid="production-views"]').waitFor()
  await worker.locator('[data-testid="production-view-mine"]').click()
  await card(worker, made.no).waitFor()
  const rejected = card(worker, made.no).locator('[data-testid="production-receipt"]', { hasText: '被仓管退回' })
  await rejected.waitFor()
  const rejectedText = await rejected.innerText()
  await shot(worker, '9-receipt-rejected-phone', true)
  await card(worker, made.no).locator('[data-testid="production-receipt-open"]').click()
  const workerDrawer = worker.locator('[data-testid="document-drawer"]')
  await workerDrawer.locator('[data-testid="document-edit"]').click()
  const edit = worker.locator('[data-testid="document-editor"]:visible')
  await edit.locator('[data-testid="document-line"]').first().waitFor()
  await edit.locator('input[data-testid="document-note"]').fill('已核对：2 樘')
  await edit.locator('[data-testid="document-submit"]').click()
  await edit.waitFor({ state: 'hidden' })
  await workerDrawer.locator('[data-testid="document-status"]', { hasText: '待确认' }).waitFor()
  check(
    '工人看到退回原因，修改后重新提交（回到待确认）',
    rejectedText.includes(REJECT_REASON),
    rejectedText,
  )
  await worker.keyboard.press('Escape')

  // 仓管确认入库：成品库存增加，订单进入"待发货"。
  await keeper.reload()
  await keeper.locator('[data-testid="warehouse-tabs"]').waitFor()
  await keeper.locator('[data-testid="warehouse-tab-receipt"]').click()
  await docRow(keeper, receiptNo).locator('[data-testid="warehouse-document-open"]').click()
  await drawer.locator('[data-testid="document-confirm"]').click()
  const message = await confirmBox(keeper, '确认')
  await drawer.locator('[data-testid="document-status"]', { hasText: '已确认' }).waitFor()
  await keeper.keyboard.press('Escape')
  await drawer.waitFor({ state: 'hidden' })
  await keeper.locator('[data-testid="warehouse-tab-goods"]').click()
  await itemRow(keeper, WINDOW).waitFor()
  await shot(keeper, '10-goods-after-receipt')
  const window = await stockOf(ctx.admin, ctx.goods.window)
  const center = await json(`${API}/api/v1/orders?view=awaiting_shipment`, { token: ctx.mei })
  check(
    '仓管确认入库：铝合金窗现有 2（被这张订单占用），订单进入"待发货"',
    message.includes(made.no) &&
      window.stock === 2 &&
      window.reserved === 2 &&
      window.available === 0 &&
      center.items.some((o) => o.no === made.no),
    { message, window, awaiting: center.items.map((o) => o.no) },
  )
  ctx.receiptNo = receiptNo
}

// ---- 6. 客服：订单详情的单据，现货订单直接发货 ----

async function shipSection(browser, ctx) {
  const { made, ready } = ctx.orders
  const page = await consoleLogin(browser, 'mei')
  await menu(page, '订单')
  await page.click('[data-testid="order-view-awaiting_shipment"]')
  const row = page.locator('[data-testid="orders-table"] .el-table__row', { hasText: made.no })
  await row.waitFor()
  await row.click()
  const drawer = page.locator('[data-testid="order-drawer"]')
  await drawer.locator('[data-testid="order-documents"]').waitFor()
  const docs = await drawer.locator('[data-testid="order-document"]').allInnerTexts()
  await shot(page, '11-order-documents')
  check(
    '订单详情里有领料单和入库单（都已确认）',
    docs.length === 2 &&
      docs[0].includes(`领料单 ${ctx.requisitionNo} 已确认`) &&
      docs[1].includes(`入库单 ${ctx.receiptNo} 已确认`),
    docs,
  )
  await page.keyboard.press('Escape')
  await drawer.waitFor({ state: 'hidden' })

  // 现货订单：开始处理后就在"待发货"里，不经过加工。
  await json(`${API}/api/v1/orders/${ready.id}/start`, { method: 'POST', token: ctx.mei })
  await page.reload()
  await page.click('[data-testid="order-view-awaiting_shipment"]')
  const readyRow = page.locator('[data-testid="orders-table"] .el-table__row', { hasText: ready.no })
  await readyRow.waitFor()
  await readyRow.click()
  await drawer.locator('[data-testid="order-item-ready-made"]').waitFor()
  await drawer.locator('[data-testid="order-ship"]').click()
  const ship = page.locator('[data-testid="ship-dialog"]')
  await ship.waitFor()
  const warned = await ship.locator('[data-testid="ship-unprocessed"]').count()
  await ship.locator('input[data-testid="ship-company"]').fill('顺丰速运')
  await ship.locator('input[data-testid="ship-no"]').fill('SF9000000001')
  await shot(page, '12-ship-ready-made')
  await ship.locator('[data-testid="ship-submit"]').click()
  await ship.waitFor({ state: 'hidden' })
  await drawer.locator('[data-testid="order-status"]', { hasText: '已发货' }).waitFor()
  await json(`${API}/api/v1/orders/${made.id}/ship`, {
    method: 'POST',
    token: ctx.mei,
    body: { shipping_company: '顺丰速运', tracking_no: 'SF9000000002', notify_customer: false },
  })
  const lamp = await stockOf(ctx.admin, ctx.goods.lamp)
  const window = await stockOf(ctx.admin, ctx.goods.window)
  check(
    '现货订单直接发货（没有"还没加工完成"的提示）；发货时成品库存扣减（吸顶灯 3 → 2，铝合金窗 2 → 0）',
    warned === 0 && lamp.stock === 2 && window.stock === 0,
    { warned, lamp, window },
  )
}

async function run(browser) {
  const ctx = await prepareTenant()
  const admin = await materialsSection(browser)
  await recipeSection(admin, ctx)
  ctx.orders = await orders(ctx)
  const worker = await requisitionSection(browser, ctx)
  const keeper = await confirmRequisitionSection(browser, ctx)
  await receiptSection(worker, keeper, ctx)
  await shipSection(browser, ctx)
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
