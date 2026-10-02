// P18 验收：云打印机（设计文档 §29）。厂商用仓库里的模拟厂商 tests/fake_printer.py（后端的
// EDP_PRINT_XPYUN_URL 指向它，验收通过它的控制接口看收到的小票、把打印机设成离线）。
//
// 1. 管理员在"设置 → 打印"添加芯烨云打印机（开发者账号、UserKEY、编号）：保存前厂商验证，状态"在线"；
//    "测试打印"后打印记录里有一条测试页，模拟厂商收到了带企业名和打印人的小票。
// 2. 工人老王（手机）领取订单：加工单自动打印——模拟厂商收到的小票有单号、加工人、打印人、"第 1 次打印"，
//    没有价格；提示里有"加工单已发往打印机"；领取后自动打开的领料单提交后，领料单也打印了（开单人老王）。
// 3. 卡片上"已打印 1 次"；点"打印加工单"后是第 2 次。
// 4. 仓管小陈打开待确认的领料单：有"云打印"和"已打印 1 次"，点一下后第 2 次，打印人是小陈。
// 5. 管理员的打印记录：按状态筛选，看某一次的内容（等宽预览有"第 2 次打印"）；把模拟打印机设成离线后
//    "检查状态"显示离线。
// 6. 客服没有"设置"菜单，打印机接口返回 403。
// 7. 没有前端脚本错误。
//
// 前置：后端（EDP_PRINT_XPYUN_URL 指向模拟厂商）、模拟厂商、控制台。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p18-print-acceptance.cjs
const { chromium } = require('playwright')
const fs = require('fs')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PRINTER = env('PRINTER_URL', 'http://127.0.0.1:8904')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p18-print')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `p18-${RUN}`
const PASSWORD = 'demo-pass-2026'
const DESKTOP = { width: 1440, height: 900 }
const PHONE = { width: 390, height: 844 }
const RECEIVER = { name: '李女士', phone: '13800001111', address: '上海市浦东新区世纪大道100号' }
// 和 backend/tests/fake_printer.py 一致。
const XPYUN_USER = 'dev@example.com'
const XPYUN_KEY = 'xpyun-dev-key'
const SN = `XPY${RUN.toUpperCase()}`

const summary = { tenant: TENANT, checks: [], consoleErrors: [] }
const check = (name, ok, detail) =>
  summary.checks.push(
    `${ok ? 'PASS' : 'FAIL'} ${name}${ok || detail === undefined ? '' : ` -> ${JSON.stringify(detail)}`}`,
  )
const pages = []

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

/** 模拟厂商收到的小票（这台打印机的）。 */
async function prints() {
  const all = await json(`${PRINTER}/_fake/prints`)
  return all.filter((p) => p.sn === SN)
}

/** 等到模拟厂商收到第 n 张小票（打印任务由后端在事务提交后马上发送）。 */
async function waitPrints(n) {
  for (let i = 0; i < 40; i += 1) {
    const items = await prints()
    if (items.length >= n) return items
    await new Promise((resolve) => setTimeout(resolve, 500))
  }
  return prints()
}

/** 厂商标记里的纯文字（去掉 <BR>、<CB> 等标签），用来检查内容。 */
const plain = (content) => content.replace(/<BR>/g, '\n').replace(/<[^>]+>/g, '')

// 租户、员工（工人老王、仓管小陈、客服小美）、材料和有配方的铝合金窗、两张已确认的订单。
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
      name: `云打印验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  const admin = await login('admin')
  const staff = {}
  for (const [username, name, role] of [
    ['wang', '工人老王', 'worker'],
    ['cang', '仓管小陈', 'keeper'],
    ['mei', '客服小美', 'agent'],
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
  const frame = await product(admin, { code: 'AL-6063', name: '铝合金型材', unit: '米', kind: 'material' })
  const glass = await product(admin, { code: 'GL-5', name: '钢化玻璃', unit: '平方米', kind: 'material' })
  await setStock(admin, frame.id, 100)
  await setStock(admin, glass.id, 20)
  const window = await product(admin, {
    code: 'WIN-01',
    name: '铝合金窗',
    unit: '樘',
    spec: '1.2m×1.5m',
    retail_price: '1280',
    category: '门窗',
  })
  await setRecipe(admin, window.id, { [frame.id]: 6.5, [glass.id]: 1.8 })
  const customer = await json(`${API}/api/v1/customers`, {
    method: 'POST',
    token: admin,
    body: { display_name: '李女士' },
  })
  const orders = {
    windows: await confirmedOrder(admin, customer.id, [[window.id, 2]]),
    spare: await confirmedOrder(admin, customer.id, [[window.id, 1]]),
  }
  await json(`${PRINTER}/_fake/reset`, { method: 'POST', body: {} })
  return { admin, staff, orders, mei: await login('mei') }
}

function watchErrors(page, label) {
  page.on('console', (msg) => {
    if (msg.type() === 'error' && !msg.text().startsWith('Failed to load resource')) {
      summary.consoleErrors.push(`${label}: ${msg.text()}`)
    }
  })
  page.on('pageerror', (e) => summary.consoleErrors.push(`${label} pageerror: ${e.message}`))
}

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

// ---- 1. 管理员接入打印机、测试打印 ----

async function setupSection(admin) {
  await admin.goto(`${CONSOLE}/settings?tab=print`)
  await admin.locator('[data-testid="add-printer"]').click()
  const dialog = admin.locator('[data-testid="printer-dialog"]')
  await dialog.locator('input[data-testid="printer-name"]').fill('车间打印机')
  await dialog.locator('input[data-testid="printer-account"]').fill(XPYUN_USER)
  // 密钥错了保存不了：厂商返回签名失败。
  await dialog.locator('input[data-testid="printer-key"]').fill('wrong-key')
  await dialog.locator('input[data-testid="printer-sn"]').fill(SN)
  await dialog.locator('[data-testid="printer-save"]').click()
  const rejected = await admin.locator('.el-message--error').first().innerText()
  await dialog.locator('input[data-testid="printer-key"]').fill(XPYUN_KEY)
  await dialog.locator('[data-testid="printer-save"]').click()
  await dialog.waitFor({ state: 'hidden' })
  const row = admin.locator('[data-testid="printer-table"] .el-table__row', { hasText: '车间打印机' })
  await row.waitFor()
  const status = await row.locator('[data-testid="printer-status"]').innerText()
  const uses = await row.locator('[data-testid="printer-uses"]').innerText()
  await shot(admin, '1-printer-added')
  check(
    '添加芯烨云打印机：密钥错了厂商拒绝（签名失败）；填对后保存，状态"在线"，自动打印加工单和领料单',
    rejected.includes('REQUEST_SIGN_FAILED') && status.trim() === '在线' && uses.includes('加工单') && uses.includes('领料单'),
    { rejected, status, uses },
  )

  await row.locator(`[data-testid="test-printer-${SN}"]`).click()
  const [test] = await waitPrints(1)
  const jobRow = admin.locator('[data-testid="print-jobs"] .el-table__row').first()
  await jobRow.waitFor()
  const ticket = await jobRow.locator('[data-testid="print-job-ticket"]').innerText()
  const content = plain(test?.content ?? '')
  check(
    '测试打印：打印记录里有一条"测试页"，模拟厂商收到带企业名、打印机名和打印人的小票',
    ticket.includes('测试页') &&
      content.includes(`云打印验收 ${RUN}`) &&
      content.includes('打印机：车间打印机') &&
      content.includes('打印人：管理员'),
    { ticket, content },
  )
}

// ---- 2/3. 工人领取：自动打印加工单和领料单；手工重打 ----

async function workerSection(phone, ctx) {
  const { windows } = ctx.orders
  await phone.locator('[data-testid="production-view-pool"]').click()
  await card(phone, windows.no).waitFor()
  await card(phone, windows.no).locator('[data-testid="production-claim"]').click()
  const toast = await phone.locator('.el-message--success').first().innerText()
  const editor = phone.locator('[data-testid="document-editor"]:visible')
  await editor.locator('[data-testid="document-row"]').first().waitFor()
  const [, order] = await waitPrints(2)
  const text = plain(order?.content ?? '')
  check(
    '工人领取订单后自动打印加工单：单号、加工人、打印人、"第 1 次打印"，有商品和数量，没有价格；领取的提示里说加工单已发往打印机',
    text.includes('加工单') &&
      text.includes(windows.no) &&
      text.includes('加工人：工人老王') &&
      text.includes('打印人：工人老王') &&
      text.includes('第 1 次打印') &&
      text.includes('铝合金窗') &&
      !text.includes('1280') &&
      toast.includes('加工单已发往打印机'),
    { text, toast },
  )
  await editor.locator('[data-testid="document-submit"]').click()
  await editor.waitFor({ state: 'hidden' })
  const [, , requisition] = await waitPrints(3)
  const docText = plain(requisition?.content ?? '')
  const docs = await json(`${API}/api/v1/warehouse/documents?kind=requisition`, { token: ctx.admin })
  ctx.doc = docs.items.find((d) => d.order_no === windows.no)
  check(
    '领取后自动打开的领料单提交后，领料单也打印了：单号、关联订单、开单人老王、材料和数量、签字栏',
    docText.includes('领料单') &&
      docText.includes(ctx.doc?.no ?? '-') &&
      docText.includes(windows.no) &&
      docText.includes('开单人：工人老王') &&
      docText.includes('铝合金型材') &&
      docText.includes('13') &&
      docText.includes('领料人签字') &&
      docText.includes('第 1 次打印'),
    { docText, no: ctx.doc?.no },
  )

  await phone.locator('[data-testid="production-view-mine"]').click()
  const mine = card(phone, windows.no)
  await mine.locator('[data-testid="print-count-order"]').waitFor()
  const before = await mine.locator('[data-testid="print-count-order"]').innerText()
  await mine.locator('[data-testid="print-order"]').click()
  const [, , , again] = await waitPrints(4)
  await mine.locator('[data-testid="print-count-order"]', { hasText: '2' }).waitFor()
  const after = await mine.locator('[data-testid="print-count-order"]').innerText()
  await shot(phone, '2-worker-card-phone', true)
  check(
    '卡片上"已打印 1 次"；点"打印加工单"后模拟厂商收到第 2 次的加工单，卡片变成"已打印 2 次"',
    before.includes('已打印 1 次') && plain(again?.content ?? '').includes('第 2 次打印') && after.includes('已打印 2 次'),
    { before, after },
  )
}

// ---- 4. 仓管在单据里云打印 ----

async function keeperSection(browser, ctx) {
  const page = await consoleLogin(browser, 'cang')
  await page.goto(`${CONSOLE}/warehouse?doc=${ctx.doc.id}`)
  const drawer = page.locator('[data-testid="document-drawer"]')
  await drawer.locator('[data-testid="print-count-requisition"]').waitFor()
  const before = await drawer.locator('[data-testid="print-count-requisition"]').innerText()
  await drawer.locator('[data-testid="print-requisition"]').click()
  const [, , , , second] = await waitPrints(5)
  await drawer.locator('[data-testid="print-count-requisition"]', { hasText: '2' }).waitFor()
  await shot(page, '3-keeper-document')
  const text = plain(second?.content ?? '')
  check(
    '仓管打开领料单：有"云打印"和"已打印 1 次"；点一下后是第 2 次，打印人是仓管小陈',
    before.includes('已打印 1 次') && text.includes('领料单') && text.includes('第 2 次打印') && text.includes('打印人：仓管小陈'),
    { before, text },
  )
}

// ---- 5. 打印记录、打印机离线 ----

async function recordsSection(admin) {
  await admin.goto(`${CONSOLE}/settings?tab=print`)
  const rows = admin.locator('[data-testid="print-jobs"] .el-table__row')
  await rows.first().waitFor()
  const seqs = await rows.locator('[data-testid="print-job-seq"]').allInnerTexts()
  const tickets = await rows.locator('[data-testid="print-job-ticket"]').allInnerTexts()
  const statuses = await rows.locator('[data-testid="print-job-status"]').allInnerTexts()
  await rows.first().locator('[data-testid^="print-job-view-"]').click()
  const content = await admin.locator('[data-testid="print-job-content"] pre').innerText()
  await shot(admin, '4-print-records')
  await admin.keyboard.press('Escape')
  check(
    '打印记录：五条（测试页、加工单两次、领料单两次），每条有第几次和状态，最近一条的内容预览里有"第 2 次打印"',
    seqs.length === 5 &&
      tickets.filter((t) => t.includes('加工单')).length === 2 &&
      tickets.filter((t) => t.includes('领料单')).length === 2 &&
      statuses.every((s) => ['已发送', '已打印'].includes(s.trim())) &&
      content.includes('第 2 次打印'),
    { seqs, tickets, statuses },
  )

  await json(`${PRINTER}/_fake/printers/${SN}/status`, { method: 'POST', body: { status: 'offline' } })
  const row = admin.locator('[data-testid="printer-table"] .el-table__row', { hasText: '车间打印机' })
  await row.locator(`[data-testid="check-printer-${SN}"]`).click()
  await row.locator('[data-testid="printer-status"]', { hasText: '离线' }).waitFor()
  const status = await row.locator('[data-testid="printer-status"]').innerText()
  check('模拟打印机离线后"检查状态"显示离线', status.trim() === '离线', status)
}

// ---- 6. 客服没有打印设置 ----

async function agentSection(browser, ctx) {
  const page = await consoleLogin(browser, 'mei')
  const menus = await page.locator('[data-testid="main-menu"] .el-menu-item').allInnerTexts()
  const response = await fetch(`${API}/api/v1/print/printers`, {
    headers: { authorization: `Bearer ${ctx.mei}` },
  })
  check(
    '客服没有"设置"菜单，打印机接口返回 403',
    !menus.some((t) => t.trim() === '设置') && response.status === 403,
    { menus, status: response.status },
  )
}

async function run(browser) {
  const ctx = await prepareTenant()
  const admin = await consoleLogin(browser, 'admin')
  await setupSection(admin)
  const phone = await consoleLogin(browser, 'wang', PHONE)
  await phone.waitForURL(/\/production/)
  await workerSection(phone, ctx)
  await keeperSection(browser, ctx)
  await recordsSection(admin)
  await agentSection(browser, ctx)
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
