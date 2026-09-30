// P7 加工验收：工人领取订单加工，逐个商品标记完成或缺货，完成订单后进入"待发货"（设计文档 §25.11）。
//
// 1. 工人老王在手机上登录：只有"加工"菜单，进不了订单中心；"待领取"里有两张已确认的货到付款订单
//    （在线收款还没收清的不在里面），卡片上有客户称呼、期望时间、客户备注和内部备注，没有金额、
//    电话和地址。领取一张后它离开"待领取"，另一个工人不能再领。
// 2. 老王在"我的加工"里把门锁标记完成（可以撤销），给门铃登记缺货（缺多少、预计到货、说明），
//    订单带上"缺货"标记，"完成订单"不可点；再修改缺货说明。
// 3. 客服小美：站内信收到"缺货处理"待办，订单中心的"缺货"里有这张订单；详情的"加工"一列显示缺货
//    说明和加工人。小美登记到货：商品回到待加工，订单离开"缺货"，待办随之完成。
// 4. 老王点"完成订单"：还有没标记的商品时确认框提示一并标记完成；订单进入"已完成"。
// 5. 小美：站内信收到"待发货"待办，订单在"待发货"里（加工完成、加工人、完成时间），登记发货后离开
//    "待发货"，发货提醒随之完成；客户的跟踪页（手机）显示"已加工完成，等待发货"。
// 6. 主管在订单详情里把另一张订单指派给工人小李：小李从站内信打开，直接看到这张订单；放弃后回到
//    "待领取"。
//
// 前置：后端（含实时消费进程）、控制台、Widget。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p7-production-acceptance.cjs
const { chromium } = require('playwright')
const fs = require('fs')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const WIDGET = env('WIDGET_URL', 'http://localhost:5175')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p7-production')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `p7-${RUN}`
const PASSWORD = 'demo-pass-2026'
const PHONE_VIEWPORT = { width: 390, height: 844 }

const PHONE = '13800001111'
const ADDRESS = '上海市浦东新区世纪大道100号'
const LOCK = '智能门锁 X1'
const BELL = '可视门铃 D1'
const KIT = '安装配件包'
const CUSTOMER_NOTE = '门锁要黑色，周五前装好'
const INTERNAL_NOTE = '老客户，包装加固'
const SHORTAGE_NOTE = '门铃缺货，供应商周四到'
const SHORTAGE_NOTE_2 = '门铃缺货，供应商改为周五到'
const SHIP_COMPANY = '顺丰速运'
const SHIP_NO = 'SF7000000001'

const summary = { tenant: TENANT, checks: [], consoleErrors: [] }
const check = (name, ok, detail) =>
  summary.checks.push(
    `${ok ? 'PASS' : 'FAIL'} ${name}${ok || detail === undefined ? '' : ` -> ${JSON.stringify(detail)}`}`,
  )

async function call(url, { method = 'GET', token, body } = {}) {
  const response = await fetch(url, {
    method,
    headers: {
      'content-type': 'application/json',
      ...(token ? { authorization: `Bearer ${token}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const text = await response.text()
  return { status: response.status, body: text ? JSON.parse(text) : null, text }
}

async function json(url, options) {
  const { status, body, text } = await call(url, options)
  if (status >= 400) throw new Error(`${options?.method ?? 'GET'} ${url} -> ${status} ${text}`)
  return body
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

/** 本地日期"YYYY-MM-DD"（days 天后）。 */
function day(days) {
  const d = new Date(Date.now() + days * 24 * 3600 * 1000)
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

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
      name: `加工验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  const admin = await login('admin')
  const products = {}
  for (const [code, name, spec, price] of [
    ['LOCK-X1-B', LOCK, '黑色', '1299'],
    ['BELL-D1', BELL, '', '199'],
    ['KIT-01', KIT, '', '59'],
  ]) {
    const product = await json(`${API}/api/v1/products`, {
      method: 'POST',
      token: admin,
      body: { code, name, spec, retail_price: price, cost_price: '10' },
    })
    products[code] = product.id
  }
  const staff = {}
  for (const [username, name, role] of [
    ['mei', '客服小美', 'agent'],
    ['lead', '主管老周', 'supervisor'],
    ['wang', '工人老王', 'worker'],
    ['xiaoli', '工人小李', 'worker'],
  ]) {
    const created = await json(`${API}/api/v1/staff`, {
      method: 'POST',
      token: admin,
      body: { username, display_name: name, password: PASSWORD, role_codes: [role] },
    })
    staff[username] = created.id
  }
  // 主管带客服组：能看到小美的订单。
  await json(`${API}/api/v1/skill-groups`, {
    method: 'POST',
    token: admin,
    body: { name: '客服组', members: [{ staff_id: staff.mei }, { staff_id: staff.lead, is_lead: true }] },
  })

  // 客服小美为客户下单并审核确认（货到付款的可以直接开工；在线收款的要先收清）。
  const mei = await login('mei')
  const customer = await json(`${API}/api/v1/customers`, {
    method: 'POST',
    token: mei,
    body: { display_name: '李女士' },
  })
  const expected = new Date(Date.now() + 26 * 3600 * 1000)
  expected.setMinutes(0, 0, 0)
  async function order(lines, method, extra = {}) {
    const created = await json(`${API}/api/v1/orders`, {
      method: 'POST',
      token: mei,
      body: {
        customer_id: customer.id,
        items: lines.map(([code, quantity]) => ({ product_id: products[code], quantity })),
        receiver: { name: '李女士', phone: PHONE, address: ADDRESS },
        ...extra,
      },
    })
    await json(`${API}/api/v1/orders/${created.id}/confirm`, {
      method: 'POST',
      token: mei,
      body: { payment_method: method, notify_customer: false },
    })
    return created
  }
  const first = await order(
    [
      ['LOCK-X1-B', 2],
      ['BELL-D1', 1],
    ],
    'cod',
    { expected_at: expected.toISOString(), customer_note: CUSTOMER_NOTE, internal_note: INTERNAL_NOTE },
  )
  const second = await order([
    ['LOCK-X1-B', 1],
    ['KIT-01', 3],
  ], 'cod')
  const unpaid = await order([['BELL-D1', 2]], 'online')
  return { admin, mei, staff, orders: { first, second, unpaid } }
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

async function confirmBox(page, text) {
  const box = page.locator('.el-message-box:visible')
  await box.waitFor()
  const message = await box.locator('.el-message-box__message').innerText()
  await box.locator('.el-message-box__btns button', { hasText: text }).click()
  await box.waitFor({ state: 'hidden' }).catch(() => null)
  return message
}

/** 打开站内信里的一条提醒（跳转到对应的页面）。 */
async function openNotification(page, text) {
  await page.click('[data-testid="notification-bell"]')
  const item = page.locator('[data-testid="notification"]', { hasText: text })
  const found = await seen(item, 30000)
  if (found) await item.first().click()
  return found
}

const card = (page, no) => page.locator(`[data-testid="production-order"][data-order-no="${no}"]`)
const itemRow = (scope, name) => scope.locator(`[data-testid="production-item"][data-item-name="${name}"]`)

async function view(page, name) {
  await page.click(`[data-testid="production-view-${name}"]`)
  await page.waitForTimeout(300)
  await page.locator('[data-testid="production-list"] .el-loading-mask').waitFor({ state: 'hidden' }).catch(() => null)
}

async function production(token, query) {
  return json(`${API}/api/v1/production/orders?${query}`, { token })
}

async function orderDetail(token, id) {
  return json(`${API}/api/v1/orders/${id}`, { token })
}

async function openOrder(page, orderView, no) {
  await menu(page, '订单')
  await page.click(`[data-testid="order-view-${orderView}"]`)
  const row = page.locator('[data-testid="orders-table"] .el-table__row', { hasText: no })
  await row.waitFor()
  await row.click()
  const drawer = page.locator('[data-testid="order-drawer"]')
  await drawer.locator('[data-testid="order-status"]').waitFor()
  return drawer
}

async function closeDrawer(page) {
  await page.keyboard.press('Escape')
  await page.locator('.el-drawer:visible').waitFor({ state: 'hidden' }).catch(() => null)
}

// ---- 1. 工人在手机上领取订单 ----

async function claimSection(browser, ctx) {
  const { first, second, unpaid } = ctx.orders
  const page = await consoleLogin(browser, 'wang', PHONE_VIEWPORT)
  await page.waitForURL(/\/production/)
  const menus = await page.locator('[data-testid="main-menu"] .el-menu-item').count()
  const pool = card(page, first.no)
  await pool.waitFor()
  const active = await page.locator('#tab-pool.is-active').count()
  const listed = await page.locator('[data-testid="production-order"]').evaluateAll((els) =>
    els.map((el) => el.getAttribute('data-order-no')),
  )
  check(
    '工人登录后进入"加工"（手机上菜单收起，只有这一个），手上没有订单时先看"待领取"',
    menus === 1 && active === 1,
    { menus, active },
  )
  check(
    '待领取：两张货到付款的已确认订单；在线收款还没收清的不在里面',
    listed.includes(first.no) && listed.includes(second.no) && !listed.includes(unpaid.no),
    listed,
  )
  const text = await pool.innerText()
  check(
    '卡片上有客户称呼、客户备注和内部备注',
    text.includes('李女士') && text.includes(CUSTOMER_NOTE) && text.includes(INTERNAL_NOTE),
    text,
  )
  const all = await page.locator('[data-testid="production-list"]').innerText()
  check(
    '工人看不到金额、电话和收货地址',
    !all.includes('¥') && !all.includes('1299') && !all.includes(PHONE) && !all.includes('世纪大道'),
    all,
  )
  await shot(page, '1-worker-pool-phone', true)

  // 订单中心：工人没有权限（菜单里没有，直接打开地址是"无权限"，接口 403）。
  const orders = await call(`${API}/api/v1/orders`, { token: await login('wang') })
  await page.goto(`${CONSOLE}/orders`)
  const denied = await seen(page.locator('text=没有访问权限'))
  check('工人进不了订单中心（页面无权限、接口 403）', denied && orders.status === 403, orders.status)
  await page.goto(`${CONSOLE}/production`)
  await pool.waitFor()

  await pool.locator('[data-testid="production-claim"]').click()
  await pool.waitFor({ state: 'detached' })
  const li = await login('xiaoli')
  const taken = await call(`${API}/api/v1/production/orders/${first.id}/claim`, { method: 'POST', token: li })
  const mine = await seen(
    page.locator('[data-testid="production-view-mine"] .el-badge__content', { hasText: '1' }),
  )
  check(
    '领取后订单离开"待领取"、"我的加工"角标 1；另一个工人不能再领（409，提示已由老王领取）',
    taken.status === 409 && taken.text.includes('工人老王') && mine,
    { status: taken.status, text: taken.text },
  )
  return page
}

// ---- 2. 标记完成、登记缺货 ----

async function markSection(page, ctx) {
  const { first } = ctx.orders
  await view(page, 'mine')
  const order = card(page, first.no)
  await order.waitFor()
  const lock = itemRow(order, LOCK)
  const bell = itemRow(order, BELL)
  await lock.locator('[data-testid="production-item-done"]').click()
  await seen(lock.locator('[data-testid="production-item-status"]', { hasText: '已完成' }))
  await lock.locator('[data-testid="production-item-undo"]').click()
  const undone = await seen(lock.locator('[data-testid="production-item-status"]', { hasText: '待加工' }))
  await lock.locator('[data-testid="production-item-done"]').click()
  const done = await seen(lock.locator('[data-testid="production-item-status"]', { hasText: '已完成' }))
  const progress = await order.locator('[data-testid="production-progress"]').innerText()
  check('逐个商品标记完成（可以撤销），进度"已完成 1/2"', undone && done && progress.includes('已完成 1/2'), progress)

  // 门铃缺货：缺 1 个，预计三天后到货。
  await bell.locator('[data-testid="production-item-shortage-edit"]').click()
  const dialog = page.locator('[data-testid="shortage-dialog"]')
  await dialog.waitFor()
  await dialog.locator('[data-testid="shortage-quantity"] input').fill('1')
  await dialog.locator('[data-testid="shortage-quantity"] input').press('Tab')
  await dialog.locator('[data-testid="shortage-date"] input').fill(day(3))
  await dialog.locator('[data-testid="shortage-date"] input').press('Enter')
  await dialog.locator('input[data-testid="shortage-note"]').fill(SHORTAGE_NOTE)
  await shot(page, '2-worker-shortage-dialog-phone')
  await dialog.locator('[data-testid="shortage-submit"]').click()
  await dialog.waitFor({ state: 'hidden' })
  const short = await seen(bell.locator('[data-testid="production-item-shortage"]', { hasText: SHORTAGE_NOTE }))
  const shortText = await bell.locator('[data-testid="production-item-shortage"]').innerText()
  const tagged = await order.locator('[data-testid="production-order-shortage"]').count()
  const blocked = await order.locator('[data-testid="production-complete"]').isDisabled()
  check(
    '登记缺货：商品显示缺多少和预计到货，订单带"缺货"标记，"完成订单"不可点',
    short && shortText.includes('缺 1/1') && shortText.includes(`预计 ${day(3)} 到货`) && tagged === 1 && blocked,
    { shortText, tagged, blocked },
  )
  await shot(page, '3-worker-shortage-phone', true)

  // 修改缺货说明（客服的待办说明随之更新）。
  await bell.locator('[data-testid="production-item-shortage-edit"]').click()
  await dialog.waitFor()
  await dialog.locator('input[data-testid="shortage-note"]').fill(SHORTAGE_NOTE_2)
  await dialog.locator('[data-testid="shortage-submit"]').click()
  await dialog.waitFor({ state: 'hidden' })
  const edited = await seen(bell.locator('[data-testid="production-item-shortage"]', { hasText: SHORTAGE_NOTE_2 }))
  const todos = await json(`${API}/api/v1/todos?view=all&limit=50`, { token: ctx.mei })
  const todo = todos.items.find((t) => t.type_code === 'order_shortage' && t.title.includes(first.no))
  check(
    '修改缺货说明：客服的"缺货处理"待办说明随之更新（不重复生成）',
    edited &&
      todo?.detail?.includes(SHORTAGE_NOTE_2) &&
      todos.items.filter((t) => t.type_code === 'order_shortage').length === 1,
    todo,
  )
}

// ---- 3. 客服处理缺货 ----

async function shortageSection(browser, ctx) {
  const { first } = ctx.orders
  const page = await consoleLogin(browser, 'mei')
  const noticed = await openNotification(page, `缺货处理「订单 ${first.no} 缺货`)
  const todoOpen = noticed && (await seen(page.locator('[data-testid="todo-drawer"]', { hasText: BELL })))
  check('客服收到站内信"缺货处理"待办，点开是这条待办', todoOpen, noticed)
  await shot(page, '4-agent-shortage-todo')
  await page.keyboard.press('Escape')
  await page.locator('.el-drawer:visible').waitFor({ state: 'hidden' }).catch(() => null)

  const drawer = await openOrder(page, 'out_of_stock', first.no)
  const badge = await page
    .locator('[data-testid="order-view-out_of_stock"] .el-badge__content')
    .innerText()
    .catch(() => '')
  const row = page.locator('[data-testid="orders-table"] .el-table__row', { hasText: first.no })
  const rowTag = await row.locator('[data-testid="order-row-shortage"]').count()
  const worker = await drawer.locator('[data-testid="order-worker"]').innerText()
  const shortage = await drawer.locator('[data-testid="order-item-shortage"]').innerText()
  check(
    '订单中心"缺货"：角标 1，行上有缺货标记；详情里有加工人和缺货说明',
    badge.trim() === '1' && rowTag === 1 && worker.includes('工人老王') && shortage.includes(SHORTAGE_NOTE_2),
    { badge, rowTag, worker, shortage },
  )
  await shot(page, '5-agent-order-shortage')

  // 到货：商品回到待加工，订单离开"缺货"，待办随之完成。
  await drawer.locator('[data-testid="order-item-restock"]').click()
  await drawer.locator('[data-testid="order-item-shortage"]').waitFor({ state: 'detached' })
  await closeDrawer(page)
  const detail = await orderDetail(ctx.mei, first.id)
  const todos = await json(`${API}/api/v1/todos?view=all&limit=50`, { token: ctx.mei })
  const todo = todos.items.find((t) => t.type_code === 'order_shortage')
  const counts = await json(`${API}/api/v1/orders/counts`, { token: ctx.mei })
  check(
    '客服登记到货：门铃回到待加工，订单离开"缺货"，"缺货处理"待办完成',
    detail.items.find((i) => i.name === BELL)?.work_status === 'pending' &&
      !detail.shortage &&
      counts.out_of_stock === 0 &&
      todo?.status === 'done',
    { shortage: detail.shortage, out: counts.out_of_stock, todo: todo?.status },
  )
  return page
}

// ---- 4. 工人完成订单 ----

async function completeSection(page, ctx) {
  const { first } = ctx.orders
  await page.reload()
  await page.locator('[data-testid="production-views"]').waitFor()
  await view(page, 'mine')
  const order = card(page, first.no)
  await order.waitFor()
  const cleared = (await order.locator('[data-testid="production-order-shortage"]').count()) === 0
  await order.locator('[data-testid="production-complete"]').click()
  const message = await confirmBox(page, '一并完成')
  await order.waitFor({ state: 'detached' })
  check(
    '完成订单：还有没标记的商品时确认框提示一并标记完成',
    cleared && message.includes(`还有 1 个商品没有标记完成（${BELL}）`),
    message,
  )
  await view(page, 'done')
  const finished = card(page, first.no)
  const shown = await seen(finished.locator('[data-testid="production-order-processed"]'))
  const statuses = await finished.locator('[data-testid="production-item-status"]').allInnerTexts()
  check(
    '订单进入"已完成"，两个商品都是已完成',
    shown && statuses.length === 2 && statuses.every((s) => s.trim() === '已完成'),
    statuses,
  )
  await shot(page, '6-worker-done-phone', true)
}

// ---- 5. 客服发货 ----

async function shipSection(browser, page, ctx) {
  const { first } = ctx.orders
  await page.reload()
  await page.locator('[data-testid="main-menu"]').waitFor()
  const noticed = await openNotification(page, `待发货「订单 ${first.no} 已加工完成，请发货」`)
  const todoOpen =
    noticed && (await seen(page.locator('[data-testid="todo-drawer"]', { hasText: '已加工完成，请发货' })))
  check('客服收到站内信"待发货"待办，点开是这条待办', todoOpen, noticed)
  await page.keyboard.press('Escape')
  await page.locator('.el-drawer:visible').waitFor({ state: 'hidden' }).catch(() => null)

  const drawer = await openOrder(page, 'awaiting_shipment', first.no)
  const row = page.locator('[data-testid="orders-table"] .el-table__row', { hasText: first.no })
  const processed = await row.locator('[data-testid="order-row-processed"]').count()
  const at = await drawer.locator('[data-testid="order-processed-at"]').innerText()
  const works = await drawer.locator('[data-testid="order-item-work"]').allInnerTexts()
  check(
    '订单中心"待发货"：行上有"加工完成"，详情有完成时间和加工人，商品都已完成',
    processed === 1 && at.includes('工人老王') && works.every((w) => w.trim() === '已完成'),
    { processed, at, works },
  )
  await shot(page, '7-agent-awaiting-shipment')
  const trackingUrl = (await drawer.locator('[data-testid="order-tracking-url"]').innerText()).trim()

  await drawer.locator('[data-testid="order-ship"]').click()
  const dialog = page.locator('[data-testid="ship-dialog"]')
  await dialog.waitFor()
  await dialog.locator('input[data-testid="ship-company"]').fill(SHIP_COMPANY)
  await dialog.locator('input[data-testid="ship-no"]').fill(SHIP_NO)
  await dialog.locator('[data-testid="ship-submit"]').click()
  await dialog.waitFor({ state: 'hidden' })
  await drawer.locator('[data-testid="order-status"]', { hasText: '已发货' }).waitFor()
  await closeDrawer(page)
  const counts = await json(`${API}/api/v1/orders/counts`, { token: ctx.mei })
  const todos = await json(`${API}/api/v1/todos?view=all&limit=50`, { token: ctx.mei })
  const ship = todos.items.find((t) => t.type_code === 'order_ship')
  check(
    '登记发货：订单离开"待发货"，发货提醒完成',
    counts.awaiting_shipment === 0 && ship?.status === 'done',
    { awaiting: counts.awaiting_shipment, todo: ship?.status },
  )

  // 客户的跟踪页（手机）：加工完成、等待发货，之后已发货；不显示加工人。
  const visitor = await newPage(browser, 'tracking', PHONE_VIEWPORT)
  await visitor.goto(trackingUrl.startsWith('http') ? trackingUrl : `${WIDGET}/?track=${trackingUrl.split('?track=')[1]}`)
  const event = await seen(visitor.locator('[data-testid="tracking-event"]', { hasText: '已加工完成，等待发货' }))
  const body = await visitor.locator('body').innerText()
  check('客户跟踪页显示"已加工完成，等待发货"，不显示加工人', event && !body.includes('工人老王'), body.slice(0, 400))
  await shot(visitor, '8-tracking-phone', true)
}

// ---- 6. 主管指派加工人 ----

async function assignSection(browser, ctx) {
  const { second } = ctx.orders
  const lead = await consoleLogin(browser, 'lead')
  const menus = (await lead.locator('[data-testid="main-menu"] .el-menu-item').allInnerTexts()).map((m) => m.trim())
  const drawer = await openOrder(lead, 'processing', second.no)
  const waiting = await drawer.locator('[data-testid="order-worker"]').innerText()
  await drawer.locator('[data-testid="order-assign-worker"]').click()
  const dialog = lead.locator('[data-testid="worker-dialog"]')
  await dialog.waitFor()
  await dialog.locator('[data-testid="worker-select"]').click()
  await lead.locator('.el-select-dropdown__item:visible', { hasText: '工人小李' }).click()
  await dialog.locator('[data-testid="worker-submit"]').click()
  await dialog.waitFor({ state: 'hidden' })
  const assigned = await seen(drawer.locator('[data-testid="order-worker"]', { hasText: '工人小李' }))
  check(
    '主管没有"加工"菜单，在订单详情里把待领取的订单指派给工人小李',
    !menus.includes('加工') && waiting.includes('待领取') && assigned,
    { menus, waiting },
  )
  await shot(lead, '9-supervisor-assign')

  // 还没有加工完成时登记发货：提示加工人还没有完成（可以继续发货）。
  await drawer.locator('[data-testid="order-ship"]').click()
  const ship = lead.locator('[data-testid="ship-dialog"]')
  await ship.waitFor()
  const warned = await seen(
    ship.locator('[data-testid="ship-unprocessed"]', { hasText: '工人小李还没有完成加工' }),
    5000,
  )
  await ship.locator('.el-dialog__footer button', { hasText: '取消' }).click()
  await ship.waitFor({ state: 'hidden' })
  check('加工还没有完成时登记发货，提示加工人还没有完成', warned)

  // 小李正在看"待领取"时从站内信打开：切到"我的加工"，按订单号只显示这一张。
  const li = await consoleLogin(browser, 'xiaoli', PHONE_VIEWPORT)
  await li.locator('[data-testid="production-views"]').waitFor()
  await view(li, 'pool')
  const noticed = await openNotification(li, `订单 ${second.no} 交给你加工`)
  const searchBox = li.locator('input[data-testid="production-search"]')
  const filtered = await waitFor(async () => (await searchBox.inputValue()) === second.no, 10000)
  const order = card(li, second.no)
  const opened = noticed && filtered && (await seen(order))
  const active = await li.locator('#tab-mine.is-active').count()
  const cards = await li.locator('[data-testid="production-order"]').count()
  const url = li.url()
  check(
    '小李从站内信打开：切到"我的加工"，只显示这张订单，可以标记和完成',
    opened &&
      active === 1 &&
      cards === 1 &&
      !url.includes('order=') &&
      (await order.locator('[data-testid="production-complete"]').count()) === 1,
    { noticed, filtered, active, cards, url },
  )
  await shot(li, '10-worker-assigned-phone', true)

  await order.locator('[data-testid="production-release"]').click()
  await confirmBox(li, '放弃')
  await order.waitFor({ state: 'detached' })
  const pool = await production(await login('xiaoli'), 'view=pool')
  check('放弃后回到"待领取"', pool.items.some((o) => o.no === second.no && o.can_claim))
}

async function run(browser) {
  const ctx = await prepareTenant()
  const worker = await claimSection(browser, ctx)
  await markSection(worker, ctx)
  const agent = await shortageSection(browser, ctx)
  await completeSection(worker, ctx)
  await shipSection(browser, agent, ctx)
  await assignSection(browser, ctx)
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
