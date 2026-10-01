// P13 验收：加工时的一键领料（设计文档 §25.17）。
//
// 1. 工人老王（手机）领取铝合金窗和纱窗的订单：领料单自动打开并填好——顶部是这次加工的商品，每种材料
//    写明怎么算的（几个商品都用到的分别列出），库存不够提示；改过的数量标出建议数量、可以恢复；提交领料单。
// 2. 仓管退回（玻璃要多领损耗）：卡片上显示退回原因，"修改领料单"打开那张单据（带退回原因和计算依据），
//    改好后重新提交。
// 3. 仓管确认后客户加了一樘：卡片上"订单改过，按配方还要领：……"，"补领材料"只填多出来的，写明已领多少。
// 4. 防盗门没有配方：领取后自动打开按以往 3 个订单估算的领料单（标"估算"，写明依据）。
// 5. 智能门锁既没有配方、也没有以往的领料：领取后不自动打开，开领料单时提示手动添加。
// 6. 管理员打开防盗门的配方：显示以往领料的每件用量，一键填入后保存；之后的订单按配方领料。
// 7. 电脑上的领料单截图。
//
// 前置：后端、控制台。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p13-assisted-requisition-acceptance.cjs
const { chromium } = require('playwright')
const fs = require('fs')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p13-assisted-requisition')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `p13-${RUN}`
const PASSWORD = 'demo-pass-2026'
const DESKTOP = { width: 1440, height: 900 }
const PHONE = { width: 390, height: 844 }
const RECEIVER = { name: '李女士', phone: '13800001111', address: '上海市浦东新区世纪大道100号' }

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

const product = (token, body) => json(`${API}/api/v1/products`, { method: 'POST', token, body })

async function setStock(token, id, quantity) {
  await json(`${API}/api/v1/products/${id}/stock`, {
    method: 'POST',
    token,
    body: { mode: 'set', quantity, note: '期初盘点' },
  })
}

async function setRecipe(token, id, items) {
  await json(`${API}/api/v1/products/${id}/materials`, {
    method: 'PUT',
    token,
    body: { items: Object.entries(items).map(([material_id, quantity]) => ({ material_id, quantity })) },
  })
}

async function confirmedOrder(token, customerId, lines) {
  const order = await json(`${API}/api/v1/orders`, {
    method: 'POST',
    token,
    body: {
      customer_id: customerId,
      items: lines.map(([product_id, quantity]) => ({ product_id, quantity })),
      receiver: RECEIVER,
    },
  })
  await json(`${API}/api/v1/orders/${order.id}/confirm`, {
    method: 'POST',
    token,
    body: { payment_method: 'cod', notify_customer: false },
  })
  return order
}

const claimApi = (token, orderId) =>
  json(`${API}/api/v1/production/orders/${orderId}/claim`, { method: 'POST', token })

async function requisitionApi(token, orderId, lines) {
  return json(`${API}/api/v1/warehouse/documents`, {
    method: 'POST',
    token,
    body: {
      kind: 'requisition',
      order_id: orderId,
      lines: Object.entries(lines).map(([product_id, quantity]) => ({ product_id, quantity })),
    },
  })
}

const confirmApi = (token, id) =>
  json(`${API}/api/v1/warehouse/documents/${id}/confirm`, { method: 'POST', token, body: {} })

// 租户、员工（工人老王、仓管小陈）、材料（型材、玻璃、库存只有 12 米的密封条、门板、锁具）、成品
// （有配方的铝合金窗和纱窗；没有配方、但以往领过 3 次料的防盗门；什么都没有的智能门锁）、订单。
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
      name: `一键领料验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  const admin = await login('admin')
  const staff = {}
  for (const [username, name, role] of [
    ['wang', '工人老王', 'worker'],
    ['cang', '仓管小陈', 'keeper'],
  ]) {
    const created = await json(`${API}/api/v1/staff`, {
      method: 'POST',
      token: admin,
      body: { username, display_name: name, password: PASSWORD, role_codes: [role] },
    })
    staff[username] = created.id
  }
  await json(`${API}/api/v1/warehouse/settings`, {
    method: 'PUT',
    token: admin,
    body: { confirm_required: true, keeper_id: staff.cang },
  })
  const m = {}
  for (const [key, code, name, unit, stock] of [
    ['frame', 'AL-6063', '铝合金型材', '米', 100],
    ['glass', 'GL-5', '钢化玻璃', '平方米', 20],
    ['seal', 'SEAL-1', '密封条', '米', 12],
    ['panel', 'DB-1', '门板', '块', 50],
    ['lockset', 'LK-1', '锁具', '套', 50],
  ]) {
    m[key] = await product(admin, { code, name, unit, kind: 'material', category: '辅料' })
    await setStock(admin, m[key].id, stock)
  }
  const g = {}
  for (const [key, code, name, unit, spec] of [
    ['window', 'WIN-01', '铝合金窗', '樘', '1.2m×1.5m'],
    ['net', 'NET-01', '纱窗', '扇', ''],
    ['door', 'DOOR-01', '防盗门', '樘', ''],
    ['lock', 'LOCK-X1', '智能门锁 X1', '把', ''],
  ]) {
    g[key] = await product(admin, { code, name, unit, spec, retail_price: '100', category: '门窗' })
  }
  await setRecipe(admin, g.window.id, { [m.frame.id]: 6.5, [m.glass.id]: 1.8, [m.seal.id]: 8 })
  await setRecipe(admin, g.net.id, { [m.seal.id]: 2 })
  const customer = await json(`${API}/api/v1/customers`, {
    method: 'POST',
    token: admin,
    body: { display_name: '李女士' },
  })
  const wang = await login('wang')
  const cang = await login('cang')
  // 以往 3 张只做防盗门的订单（每樘：门板 2 块、锁具 1 套），领料单都已确认。
  for (let i = 0; i < 3; i += 1) {
    const past = await confirmedOrder(admin, customer.id, [[g.door.id, 2]])
    await claimApi(wang, past.id)
    const doc = await requisitionApi(wang, past.id, { [m.panel.id]: 4, [m.lockset.id]: 2 })
    await confirmApi(cang, doc.id)
  }
  const orders = {
    windows: await confirmedOrder(admin, customer.id, [
      [g.window.id, 2],
      [g.net.id, 1],
    ]),
    doors: await confirmedOrder(admin, customer.id, [[g.door.id, 3]]),
    locks: await confirmedOrder(admin, customer.id, [[g.lock.id, 1]]),
  }
  return { admin, wang, cang, staff, m, g, customerId: customer.id, orders }
}

function watchErrors(page, label) {
  page.on('console', (msg) => {
    if (msg.type() === 'error' && !msg.text().startsWith('Failed to load resource')) {
      summary.consoleErrors.push(`${label}: ${msg.text()}`)
    }
  })
  page.on('pageerror', (e) => summary.consoleErrors.push(`${label} pageerror: ${e.message}`))
}

const pages = []

async function consoleLogin(browser, username, viewport = DESKTOP) {
  const context = await browser.newContext({ viewport, locale: 'zh-CN' })
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
  return page
}

async function shot(page, name, fullPage = false) {
  await page.waitForTimeout(600)
  await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage })
}

const card = (page, no) => page.locator(`[data-testid="production-order"][data-order-no="${no}"]`)

/** 领料单里每种材料：名称、本次领用、建议、计算依据、已领、估算/手动添加、改过没有。 */
async function editorLines(editor) {
  return editor.locator('[data-testid="document-row"]').evaluateAll((rows) =>
    rows.map((row) => ({
      name: row.getAttribute('data-name'),
      quantity: row.querySelector('[data-testid="document-line-quantity"] input')?.value,
      basis: [...row.querySelectorAll('[data-testid="document-line-basis"]')].map((el) => el.textContent.trim()),
      taken: row.querySelector('[data-testid="document-line-taken"]')?.textContent.trim() ?? null,
      estimated: Boolean(row.querySelector('[data-testid="document-line-estimated"]')),
      edited: row.querySelector('[data-testid="document-line-quantity"]')?.getAttribute('data-edited') === 'true',
    })),
  )
}

async function making(editor) {
  return editor
    .locator('[data-testid="document-making-item"]')
    .evaluateAll((els) => els.map((el) => [...el.children].map((c) => c.textContent.trim()).join(' ')))
}

async function setQuantity(editor, name, value) {
  const input = editor.locator(`[data-testid="document-row"][data-name="${name}"] [data-testid="document-line-quantity"] input`)
  await input.fill(String(value))
  await input.press('Tab')
}

// ---- 1. 领取后自动打开填好的领料单 ----

async function recipeSection(page, ctx) {
  const { windows } = ctx.orders
  await page.locator('[data-testid="production-view-pool"]').click()
  await card(page, windows.no).waitFor()
  await card(page, windows.no).locator('[data-testid="production-claim"]').click()
  const editor = page.locator('[data-testid="document-editor"]:visible')
  await editor.locator('[data-testid="document-row"]').first().waitFor()
  const items = await making(editor)
  const lines = await editorLines(editor)
  const seal = lines.find((l) => l.name === '密封条')
  const glass = lines.find((l) => l.name === '钢化玻璃')
  const warning = await editor.locator('[data-testid="document-short"]').innerText()
  const submit = await editor.locator('[data-testid="document-submit"]').innerText()
  await shot(page, '1-filled-requisition-phone', true)
  check(
    '领取后自动打开填好的领料单：顶部是这次加工的商品（按配方），每种材料写明怎么算的（密封条：窗 8 米/樘 × 2 樘 + 纱窗 2 米/扇 × 1 扇 = 18 米），库存不够提示，主按钮"提交领料单"',
    items.join('|') === '铝合金窗 1.2m×1.5m × 2 樘 按配方|纱窗 × 1 扇 按配方' &&
      seal?.quantity === '18' &&
      seal.basis.join('|') === '铝合金窗 1.2m×1.5m 8 米/樘 × 2 樘|纱窗 2 米/扇 × 1 扇' &&
      glass?.basis.join('|') === '铝合金窗 1.2m×1.5m 1.8 平方米/樘 × 2 樘' &&
      warning.includes('密封条') &&
      submit.trim() === '提交领料单',
    { items, lines, warning, submit },
  )

  // 密封条只有 12 米：先领 12 米；改过的数量标出来，可以恢复。
  await setQuantity(editor, '密封条', 12)
  const edited = (await editorLines(editor)).find((l) => l.name === '密封条')
  const restore = editor.locator('[data-testid="document-row"][data-name="密封条"] [data-testid="document-line-restore"]')
  const restorable = await restore.isVisible()
  await restore.click()
  const restored = (await editorLines(editor)).find((l) => l.name === '密封条')
  await setQuantity(editor, '密封条', 12)
  check(
    '改过的数量标出来（建议 18），点"恢复"回到建议数量',
    edited?.edited && restorable && restored?.quantity === '18' && !restored.edited,
    { edited, restorable, restored },
  )
  await editor.locator('[data-testid="document-submit"]').click()
  await editor.waitFor({ state: 'hidden' })
  await page.locator('[data-testid="production-view-mine"]').click()
  const mine = card(page, windows.no)
  await mine.locator('[data-testid="production-document"]').first().waitFor()
  const todo = await mine.locator('[data-testid="production-requisition-todo"]').innerText()
  const button = mine.locator('[data-testid="production-open-requisition"]')
  const label = (await button.innerText()).trim()
  const primary = (await button.getAttribute('class')).includes('el-button--primary')
  check(
    '提交后卡片上有领料单；密封条少领了 6 米，卡片提示"按配方还要领：密封条 6 米"，"补领材料"是主按钮',
    todo.includes('密封条 6 米') && label === '补领材料' && primary,
    { todo, label, primary },
  )
  const docs = await json(`${API}/api/v1/warehouse/documents?kind=requisition`, { token: ctx.admin })
  ctx.windowsDoc = docs.items.find((d) => d.order_no === windows.no)
}

// ---- 2. 仓管退回，工人在卡片上修改 ----

async function rejectSection(page, ctx) {
  const { windows } = ctx.orders
  await json(`${API}/api/v1/warehouse/documents/${ctx.windowsDoc.id}/reject`, {
    method: 'POST',
    token: ctx.cang,
    body: { reason: '玻璃按 4 平方米领（含损耗）' },
  })
  await page.reload()
  await page.locator('[data-testid="production-view-mine"]').click()
  const mine = card(page, windows.no)
  const alert = mine.locator('[data-testid="production-requisition-rejected"]')
  await alert.waitFor()
  const reason = await alert.innerText()
  const button = mine.locator('[data-testid="production-open-requisition"]')
  const label = (await button.innerText()).trim()
  await button.click()
  const editor = page.locator('[data-testid="document-editor"]:visible')
  await editor.locator('[data-testid="document-rejected"]').waitFor()
  await editor.locator('[data-testid="document-line-basis"]').first().waitFor()
  const banner = await editor.locator('[data-testid="document-rejected"]').innerText()
  const lines = await editorLines(editor)
  await setQuantity(editor, '钢化玻璃', 4)
  const submit = (await editor.locator('[data-testid="document-submit"]').innerText()).trim()
  await shot(page, '2-fix-rejected-phone', true)
  await editor.locator('[data-testid="document-submit"]').click()
  await editor.waitFor({ state: 'hidden' })
  await mine.locator('[data-testid="production-requisition-rejected"]').waitFor({ state: 'hidden' })
  const doc = await json(`${API}/api/v1/warehouse/documents/${ctx.windowsDoc.id}`, { token: ctx.admin })
  check(
    '仓管退回：卡片上显示退回原因和"修改领料单"；打开的是那张单据（显示原因，保留计算依据），改好后"重新提交"',
    reason.includes('玻璃按 4 平方米领') &&
      label === '修改领料单' &&
      banner.includes('玻璃按 4 平方米领') &&
      lines.find((l) => l.name === '钢化玻璃')?.basis.length === 1 &&
      submit === '重新提交' &&
      doc.status === 'pending' &&
      doc.lines.find((l) => l.name === '钢化玻璃')?.quantity === 4,
    { reason, label, banner, lines, submit, status: doc.status },
  )
}

// ---- 3. 订单改了数量：补领 ----

async function topUpSection(page, ctx) {
  const { windows } = ctx.orders
  await confirmApi(ctx.cang, ctx.windowsDoc.id)
  // 补上之前少领的密封条，领齐后客户又加了一樘窗。
  const extra = await requisitionApi(ctx.wang, windows.id, { [ctx.m.seal.id]: 6 })
  await confirmApi(ctx.cang, extra.id)
  const detail = await json(`${API}/api/v1/orders/${windows.id}`, { token: ctx.admin })
  await json(`${API}/api/v1/orders/${windows.id}`, {
    method: 'PATCH',
    token: ctx.admin,
    body: {
      version: detail.version,
      reason: 'customer_request',
      items: [
        { product_id: ctx.g.window.id, quantity: 3 },
        { product_id: ctx.g.net.id, quantity: 1 },
      ],
    },
  })
  await page.reload()
  await page.locator('[data-testid="production-view-mine"]').click()
  const mine = card(page, windows.no)
  const todo = mine.locator('[data-testid="production-requisition-todo"]')
  await todo.waitFor()
  const text = await todo.innerText()
  await mine.locator('[data-testid="production-open-requisition"]').click()
  const editor = page.locator('[data-testid="document-editor"]:visible')
  await editor.locator('[data-testid="document-row"]').first().waitFor()
  const lines = await editorLines(editor)
  const frame = lines.find((l) => l.name === '铝合金型材')
  await shot(page, '3-top-up-phone', true)
  check(
    '客户加了一樘：卡片上列出按配方还要领的三种材料（玻璃之前多领了 0.4，只差 1.4）；"补领材料"只填还没领的（型材 6.5 米，写明已领 13 米）',
    ['铝合金型材 6.5 米', '钢化玻璃 1.4 平方米', '密封条 8 米'].every((t) => text.includes(t)) &&
      lines.length === 3 &&
      frame?.quantity === '6.5' &&
      frame.taken?.includes('已领 13 米'),
    { text, lines },
  )
  await editor.locator('[data-testid="document-submit"]').click()
  await editor.waitFor({ state: 'hidden' })
}

// ---- 4、5. 没有配方：按以往领料估算；什么都没有：手动添加 ----

async function estimateSection(page, ctx) {
  const { doors, locks } = ctx.orders
  await page.locator('[data-testid="production-view-pool"]').click()
  await card(page, doors.no).waitFor()
  const before = card(page, doors.no)
  await before.locator('[data-testid="production-claim"]').click()
  const editor = page.locator('[data-testid="document-editor"]:visible')
  await editor.locator('[data-testid="document-row"]').first().waitFor()
  const banner = await editor.locator('[data-testid="document-estimated"]').innerText()
  const items = await making(editor)
  const lines = await editorLines(editor)
  await shot(page, '4-estimated-phone', true)
  check(
    '防盗门没有配方：领取后自动打开按以往 3 个订单估算的领料单（门板 6 块、锁具 3 套，标"估算"，写明依据），顶部提示仔细核对',
    banner.includes('防盗门') &&
      items.join('|') === '防盗门 × 3 樘 按以往 3 个订单估算' &&
      lines.map((l) => `${l.name}:${l.quantity}:${l.estimated}`).sort().join('|') === '锁具:3:true|门板:6:true' &&
      lines.find((l) => l.name === '门板')?.basis[0] === '防盗门 2 块/樘 × 3 樘（按以往 3 个订单估算）',
    { banner, items, lines },
  )
  await editor.locator('[data-testid="document-submit"]').click()
  await editor.waitFor({ state: 'hidden' })

  await page.locator('[data-testid="production-view-pool"]').click()
  await card(page, locks.no).waitFor()
  await card(page, locks.no).locator('[data-testid="production-claim"]').click()
  await page.locator('[data-testid="production-view-mine"]').click()
  await card(page, locks.no).waitFor()
  const opened = await page.locator('[data-testid="document-editor"]:visible').count()
  const button = card(page, locks.no).locator('[data-testid="production-open-requisition"]')
  const primary = (await button.getAttribute('class')).includes('el-button--primary')
  await button.click()
  const empty = page.locator('[data-testid="document-editor"]:visible')
  const missing = await empty.locator('[data-testid="document-missing"]').innerText()
  const rows = await empty.locator('[data-testid="document-row"]').count()
  check(
    '智能门锁既没有配方也没有以往的领料：领取后不自动打开，"开领料单"不是主按钮，打开后提示在下面添加',
    opened === 0 && !primary && rows === 0 && missing.includes('智能门锁 X1没有配方，也没有以往的领料'),
    { opened, primary, rows, missing },
  )
  await empty.locator('[data-testid="doc-close"]').click()
}

// ---- 6. 按以往领料生成配方 ----

async function recipeFromHistorySection(browser, ctx) {
  const docs = await json(`${API}/api/v1/warehouse/documents?kind=requisition&status=pending`, { token: ctx.admin })
  const doorDoc = docs.items.find((d) => d.order_no === ctx.orders.doors.no)
  await confirmApi(ctx.cang, doorDoc.id)
  const page = await consoleLogin(browser, 'admin')
  await page.goto(`${CONSOLE}/products`)
  const row = page.locator('[data-testid="products-table"] .el-table__row', { hasText: '防盗门' })
  await row.waitFor()
  await row.locator('[data-testid="product-bom"]').click()
  const dialog = page.locator('[data-testid="bom-dialog"]')
  const box = dialog.locator('[data-testid="bom-history"]')
  await box.waitFor()
  const text = await box.innerText()
  await shot(page, '5-recipe-from-history')
  await dialog.locator('[data-testid="bom-fill-history"]').click()
  const filled = await dialog.locator('[data-testid="bom-line"]').evaluateAll((els) => els.map((el) => el.getAttribute('data-name')))
  await dialog.locator('[data-testid="bom-save"]').click()
  await dialog.waitFor({ state: 'hidden' })
  const saved = await json(`${API}/api/v1/products/${ctx.g.door.id}/materials`, { token: ctx.admin })
  const next = await confirmedOrder(ctx.admin, ctx.customerId, [[ctx.g.door.id, 1]])
  await claimApi(ctx.wang, next.id)
  const draft = await json(`${API}/api/v1/warehouse/drafts?kind=requisition&order_id=${next.id}`, { token: ctx.wang })
  check(
    '管理员打开防盗门的配方：显示以往 4 个订单的每樘用量，"按以往领料填入"后保存；之后的订单按配方领料',
    text.includes('按以往 4 个订单') &&
      text.includes('门板 2 块') &&
      text.includes('锁具 1 套') &&
      filled.sort().join('|') === '锁具|门板' &&
      saved.items.map((i) => `${i.name}:${i.quantity}`).sort().join('|') === '锁具:1|门板:2' &&
      draft.items[0]?.basis === 'recipe' &&
      draft.estimated.length === 0,
    { text, filled, saved: saved.items, draft: draft.items },
  )
}

// ---- 7. 电脑上的领料单 ----

async function desktopSection(browser, ctx) {
  const order = await confirmedOrder(ctx.admin, ctx.customerId, [[ctx.g.window.id, 1]])
  const page = await consoleLogin(browser, 'wang')
  await page.locator('[data-testid="production-view-pool"]').click()
  await card(page, order.no).waitFor()
  await card(page, order.no).locator('[data-testid="production-claim"]').click()
  const editor = page.locator('[data-testid="document-editor"]:visible')
  await editor.locator('[data-testid="document-line-basis"]').first().waitFor()
  await shot(page, '6-filled-requisition-desktop')
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  check('电脑上的领料单：计算依据在材料名称下面，页面没有横向滚动', overflow <= 0, { overflow })
}

async function run(browser) {
  const ctx = await prepareTenant()
  const phone = await consoleLogin(browser, 'wang', PHONE)
  await phone.waitForURL(/\/production/)
  await recipeSection(phone, ctx)
  await rejectSection(phone, ctx)
  await topUpSection(phone, ctx)
  await estimateSection(phone, ctx)
  await recipeFromHistorySection(browser, ctx)
  await desktopSection(browser, ctx)
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
