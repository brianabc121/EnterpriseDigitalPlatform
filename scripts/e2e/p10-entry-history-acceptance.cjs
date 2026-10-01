// P10 验收：开单界面和修改历史（设计文档 §25.14）。
//
// 1. 客服小美下单（电脑）：单据页从右侧打开，录入行输入名称回车加入、光标跳到数量，数量里回车回到
//    录入行；扫码（输入代码直接回车）加入现货吸顶灯，再扫一次数量变成 2；"批量选择"按分类勾选纱窗
//    （数量 4）和防盗门；合计 ¥7,278.00；Ctrl+S 提交。
// 2. 小美修改订单（铝合金窗 2 → 3，删掉纱窗，原因"客户要求"），打开修改历史：按日期分组的版本列表，
//    最新版本标出改动（改过的数量、删掉的行、合计），关掉"显示更改"不标，可以看最初的版本。
// 3. 主管老李在"仓库"开领料单：选择关联的加工中订单，按配方带入材料（2 樘 × 用量）；扫码加入
//    密封条（已有的行加数量），批量选择辅料里的螺丝；提交后等仓管小陈确认。
// 4. 仓管小陈确认：单据页顶部的处理进度（开单 → 仓管确认 → 已生效），打印单据（新窗口），修改历史
//    有"开单""确认"两个版本，状态和确认人标为改动。
// 5. 待办的修改历史：新建、修改、完成三个版本，不同操作人。
// 6. 管理员改成品的建议零售价、删除一个商品；成品的修改历史标出价格改动。
// 7. 操作日志的"修改历史"：全部订单、领料单、待办、成品和材料的版本，按操作（删除）、操作人筛选，
//    删除的商品也能打开历史；客服没有操作日志。
// 8. 手机：下单页全屏，明细变成卡片（每格带标签）、没有横向滚动；单据详情全屏。
//
// 前置：后端、控制台。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p10-entry-history-acceptance.cjs
const { chromium } = require('playwright')
const fs = require('fs')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p10-entry-history')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `p10-${RUN}`
const PASSWORD = 'demo-pass-2026'
const DESKTOP = { width: 1440, height: 900 }
const PHONE = { width: 390, height: 844 }
const RECEIVER = { name: '李女士', phone: '13800001111', address: '上海市浦东新区世纪大道100号' }

const summary = { tenant: TENANT, checks: [], consoleErrors: [] }
const check = (name, ok, detail) =>
  summary.checks.push(
    `${ok ? 'PASS' : 'FAIL'} ${name}${ok || detail === undefined ? '' : ` -> ${JSON.stringify(detail)}`}`,
  )

async function json(url, { method = 'GET', token, body, raw = false } = {}) {
  const response = await fetch(url, {
    method,
    headers: {
      'content-type': 'application/json',
      ...(token ? { authorization: `Bearer ${token}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (raw) return response
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

async function product(token, body) {
  return json(`${API}/api/v1/products`, { method: 'POST', token, body })
}

async function setStock(token, id, quantity) {
  await json(`${API}/api/v1/products/${id}/stock`, {
    method: 'POST',
    token,
    body: { mode: 'set', quantity, note: '期初盘点' },
  })
}

// 租户、员工（客服、工人、仓管、主管）、成品（分类：门窗、灯具）、材料（分类：型材、玻璃、辅料）、
// 铝合金窗的配方、客户；一张已确认、工人老王已领取加工的订单（铝合金窗 2 樘）。
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
      name: `开单验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  const admin = await login('admin')
  const staff = {}
  for (const [username, name, role] of [
    ['mei', '客服小美', 'agent'],
    ['wang', '工人老王', 'worker'],
    ['cang', '仓管小陈', 'worker'],
    ['boss', '主管老李', 'supervisor'],
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
  const goods = {}
  for (const [key, code, name, unit, price, spec, category, ready] of [
    ['window', 'WIN-01', '铝合金窗', '樘', '1800', '1.2m×1.5m', '门窗', false],
    ['door', 'DOOR-01', '防盗门', '樘', '2600', '甲级', '门窗', false],
    ['net', 'NET-01', '纱窗', '扇', '120', '隐形', '门窗', false],
    ['lamp', 'LAMP-01', '吸顶灯', '盏', '299', 'LED 36W', '灯具', true],
    ['sample', 'TEST-01', '样品测试窗', '樘', '99', '', '样品', false],
  ]) {
    goods[key] = await product(admin, { code, name, unit, retail_price: price, spec, category, ready_made: ready })
  }
  await setStock(admin, goods.lamp.id, 3)
  const materials = {}
  for (const [key, code, name, unit, stock, category] of [
    ['frame', 'AL-6063', '铝合金型材', '米', 100.5, '型材'],
    ['glass', 'GL-5', '钢化玻璃', '平方米', 20, '玻璃'],
    ['seal', 'SEAL-1', '密封条', '米', 50, '辅料'],
    ['screw', 'SCR-4', '不锈钢螺丝', '个', 500, '辅料'],
  ]) {
    materials[key] = await product(admin, { code, name, unit, kind: 'material', category })
    await setStock(admin, materials[key].id, stock)
  }
  await json(`${API}/api/v1/products/${goods.window.id}/materials`, {
    method: 'PUT',
    token: admin,
    body: {
      items: [
        { material_id: materials.frame.id, quantity: 6.5 },
        { material_id: materials.glass.id, quantity: 1.8 },
        { material_id: materials.seal.id, quantity: 8 },
      ],
    },
  })
  const mei = await login('mei')
  const customer = await json(`${API}/api/v1/customers`, {
    method: 'POST',
    token: mei,
    body: { display_name: '李女士' },
  })
  const made = await json(`${API}/api/v1/orders`, {
    method: 'POST',
    token: mei,
    body: { customer_id: customer.id, items: [{ product_id: goods.window.id, quantity: 2 }], receiver: RECEIVER },
  })
  await json(`${API}/api/v1/orders/${made.id}/confirm`, {
    method: 'POST',
    token: mei,
    body: { payment_method: 'cod', notify_customer: false },
  })
  await json(`${API}/api/v1/production/orders/${made.id}/claim`, { method: 'POST', token: await login('wang') })
  return { admin, mei, staff, goods, materials, customerId: customer.id, made }
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

async function shot(page, name) {
  await page.waitForTimeout(600)
  await page.screenshot({ path: `${SHOTS}/${name}.png` })
}

async function seen(locator, timeout = 15000) {
  return locator
    .first()
    .waitFor({ timeout })
    .then(() => true)
    .catch(() => false)
}

// el-select（可搜索）：输入关键字后点选。
async function pick(page, select, text) {
  await select.click()
  await page.keyboard.type(text)
  const option = page.locator('.el-select-dropdown__item:visible', { hasText: text }).first()
  await option.waitFor()
  await option.click()
}

// 普通下拉：点开后点选（页面还在加载时偶尔没有展开，没展开就再点一次）。
async function choose(page, select, text) {
  const option = page.locator('.el-select-dropdown__item:visible', { hasText: text }).first()
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await select.click()
    if (await option.waitFor({ timeout: 5000 }).then(() => true, () => false)) break
  }
  await option.click()
}

async function confirmBox(page, button) {
  const box = page.locator('.el-message-box:visible')
  await box.waitFor()
  await box.locator('button', { hasText: button }).click()
  await box.waitFor({ state: 'hidden' }).catch(() => null)
}

// 录入行：输入名称，等候选出来后回车加入（加入后光标跳到这一行的数量）。
async function enter(page, scope, testid, text) {
  const input = scope.locator(`input[data-testid="${testid}-input"]`)
  await input.fill(text)
  await scope.locator(`[data-testid="${testid}-option"][data-name="${text}"]`).first().waitFor()
  await input.press('Enter')
}

// 扫码：扫码枪输入代码后马上回车（不等候选）。
async function scan(scope, testid, code) {
  const input = scope.locator(`input[data-testid="${testid}-input"]`)
  await input.fill(code)
  await input.press('Enter')
}

// 当前光标所在的输入框（或它所在的组件）的 data-testid。
async function focused(page) {
  return page.evaluate(() => {
    const el = document.activeElement
    return el?.getAttribute('data-testid') ?? el?.closest('[data-testid]')?.getAttribute('data-testid') ?? null
  })
}

async function values(scope, selector) {
  return scope.locator(selector).evaluateAll((els) => els.map((el) => el.value))
}

async function names(scope, selector) {
  return scope.locator(selector).evaluateAll((els) => els.map((el) => el.getAttribute('data-name')))
}

// 修改历史抽屉：版本列表（操作）、选中版本的字段和明细行的改动。
function historyDrawer(page) {
  return page.locator('[data-testid="history-drawer"]:visible')
}

async function versionActions(history) {
  await history.locator('[data-testid="history-version"]').first().waitFor()
  return history
    .locator('[data-testid="history-version"]')
    .evaluateAll((els) => els.map((el) => el.querySelector('.el-tag')?.textContent?.trim() ?? ''))
}

async function field(history, label) {
  const cell = history.locator(`[data-testid="history-field"][data-label="${label}"]`).first()
  if (!(await cell.count())) return null
  return {
    changed: (await cell.getAttribute('data-changed')) === 'true',
    old: (await cell.locator('del').count()) ? (await cell.locator('del').innerText()).trim() : null,
    value: (await cell.locator('.new').innerText()).trim(),
  }
}

async function rowStates(history) {
  return history.locator('[data-testid="history-row"]').evaluateAll((els) =>
    els.map((el) => `${el.querySelector('td')?.textContent?.trim() ?? ''}:${el.getAttribute('data-state')}`),
  )
}

async function closeHistory(page) {
  const history = historyDrawer(page)
  await history.locator('.el-drawer__close-btn').click()
  await history.waitFor({ state: 'hidden' })
}

// ---- 1. 客服下单：录入行、扫码、批量选择、合计、Ctrl+S ----

async function orderEntrySection(browser, ctx) {
  const page = await consoleLogin(browser, 'mei')
  await page.goto(`${CONSOLE}/orders`)
  await page.locator('[data-testid="new-order"]').click()
  const form = page.locator('[data-testid="order-form"]:visible')
  await form.waitFor()
  const box = await form.locator('.sheet').boundingBox()
  const no = await form.locator('[data-testid="doc-no"]').innerText()
  const width = Math.round(box?.width ?? 0)
  check('新建订单：单据页从右侧打开（宽 1200px），单号保存后生成', width === 1200 && no.includes('保存后生成'), {
    width,
    no,
  })
  await pick(page, form.locator('[data-testid="order-customer"]'), '李女士')

  // 录入行：输入名称回车加入，光标跳到数量；输入数量回车回到录入行（客服不能改价，跳过单价）。
  await enter(page, form, 'order-add-product', '铝合金窗')
  const afterAdd = await focused(page)
  await page.keyboard.type('2')
  await page.keyboard.press('Enter')
  const afterQty = await focused(page)
  const quantity = await form.locator('[data-testid="order-qty-0"] input').inputValue()
  check(
    '录入行：输入名称回车加入，光标跳到数量（直接输入就是替换：2），数量里回车回到录入行',
    afterAdd === 'order-qty-0' && quantity === '2' && afterQty === 'order-add-product-input',
    { afterAdd, quantity, afterQty },
  )

  // 扫码：输入代码直接回车；再扫一次同一个商品，数量加 1（不加新行）。
  await scan(form, 'order-add-product', 'LAMP-01')
  await form.locator('[data-testid="order-line-1"]').waitFor()
  await scan(form, 'order-add-product', 'LAMP-01')
  await page.waitForTimeout(500)
  const lamp = form.locator('[data-testid="order-line-1"]')
  const lampName = await lamp.locator('.item-name').innerText()
  const available = await lamp.locator('td[data-label="可用"]').innerText()
  const lampQty = await form.locator('[data-testid="order-qty-1"] input').inputValue()
  const rowCount = await form.locator('tbody tr[data-testid^="order-line-"]').count()
  check(
    '扫码（代码 LAMP-01 回车）加入现货吸顶灯，显示可用库存 3；再扫一次数量变成 2，不加新行',
    lampName === '吸顶灯' && available.trim() === '3' && lampQty === '2' && rowCount === 2,
    { lampName, available, lampQty, rowCount },
  )

  // 批量选择：按分类（门窗）列出，勾选防盗门、纱窗填数量 4，一次加入。
  await form.locator('[data-testid="order-add-product-batch"]').click()
  const picker = page.locator('[data-testid="item-picker"]')
  await picker.locator('[data-testid="item-picker-check"]').first().waitFor()
  const categories = await picker.locator('[data-testid="item-picker-category"]').allInnerTexts()
  await picker.locator('[data-testid="item-picker-category"]', { hasText: '门窗' }).click()
  await page.waitForTimeout(500)
  const listed = await names(picker, '[data-testid="item-picker-check"]')
  const net = picker.locator('[data-testid="item-picker-quantity"][data-name="纱窗"] input')
  await net.fill('4')
  await net.press('Tab')
  await picker.locator('[data-testid="item-picker-check"][data-name="防盗门"]').click()
  const count = await picker.locator('[data-testid="item-picker-count"]').innerText()
  await shot(page, '1-order-batch-picker')
  check(
    '批量选择：左边是分类（门窗、灯具……），选"门窗"只列出门窗；填了数量的纱窗自动勾选，已选 2 项',
    ['门窗', '灯具', '样品'].every((c) => categories.map((t) => t.trim()).includes(c)) &&
      [...listed].sort().join('|') === ['铝合金窗', '防盗门', '纱窗'].sort().join('|') &&
      count.includes('已选 2 项'),
    { categories, listed, count },
  )
  await picker.locator('[data-testid="item-picker-confirm"]').click()
  await picker.waitFor({ state: 'hidden' })
  await form.locator('[data-testid="order-line-3"]').waitFor()
  const quantities = await values(form, '[data-testid^="order-qty-"] input')
  const total = await form.locator('[data-testid="order-total"]').innerText()
  const lines = await form.locator('[data-testid="order-line-count"]').innerText()
  for (const [field, value] of Object.entries(RECEIVER)) {
    await form.locator(`input[data-testid="receiver-${field}"]`).fill(value)
  }
  await shot(page, '2-order-entry')
  check(
    '明细 4 项（铝合金窗 2、吸顶灯 2、纱窗 4、防盗门 1），应收合计 ¥7,278.00',
    quantities.join('|') === '2|2|4|1' && total === '¥7,278.00' && lines.includes('共 4 项'),
    { quantities, total, lines },
  )

  // Ctrl+S 提交：保存后打开订单详情。
  await page.keyboard.press('Control+S')
  await form.waitFor({ state: 'hidden' })
  const drawer = page.locator('[data-testid="order-drawer"]')
  await drawer.locator('[data-testid="order-edit"]').waitFor()
  const title = await drawer.locator('.el-drawer__title').first().innerText()
  const orderNo = title.replace('订单', '').trim()
  const saved = (await json(`${API}/api/v1/orders?q=${orderNo}`, { token: ctx.mei })).items[0]
  check('Ctrl+S 提交订单，保存后打开订单详情', saved?.no === orderNo && saved?.total === '7278.00', {
    orderNo,
    saved: saved && [saved.no, saved.status, saved.total],
  })
  ctx.orderNo = orderNo
  return page
}

// ---- 2. 修改订单，查看修改历史 ----

async function orderHistorySection(page) {
  const drawer = page.locator('[data-testid="order-drawer"]')
  await drawer.locator('[data-testid="order-edit"]').click()
  const form = page.locator('[data-testid="order-form"]:visible')
  await form.locator('[data-testid="order-line-3"]').waitFor()
  const steps = await form.locator('[data-testid="doc-steps"] li').evaluateAll((els) =>
    els.map((el) => `${el.querySelector('.step-label')?.textContent?.trim()}:${el.className}`),
  )
  await form.locator('[data-testid="order-qty-0"] input').fill('3')
  await form.locator('[data-testid="order-qty-0"] input').press('Tab')
  await form.locator('[data-testid="order-line-remove-2"]').click()
  await form.locator('[data-testid="order-reason"] .el-radio', { hasText: '客户要求' }).click()
  await form.locator('[data-testid="order-save"]').click()
  await form.waitFor({ state: 'hidden' })
  check(
    '修改订单：单据页顶部显示处理进度（草稿 → 待审核 → 已确认……，当前一步高亮）',
    steps.length === 6 && steps.some((s) => s.endsWith(':current')),
    steps,
  )

  await drawer.locator('[data-testid="order-revisions-open"]').click()
  const history = historyDrawer(page)
  const actions = await versionActions(history)
  const days = await history.locator('.day').allInnerTexts()
  const rows = await rowStates(history)
  const total = await field(history, '合计')
  await shot(page, '3-order-history')
  check(
    '订单的修改历史：版本按日期分组，最新是小美的"修改"，最早是"新建"；标出改动：铝合金窗数量 2 → 3、删掉的纱窗、合计 ¥7,278.00 → ¥8,598.00',
    actions[0] === '修改' &&
      actions[actions.length - 1] === '新建' &&
      days.length >= 1 &&
      rows.includes('铝合金窗:changed') &&
      rows.includes('纱窗:removed') &&
      total?.changed &&
      total.old === '¥7,278.00' &&
      total.value === '¥8,598.00',
    { actions, days, rows, total },
  )

  // 关掉"显示更改"：不标改动；选最早的版本：4 行明细。
  await history.locator('[data-testid="history-highlight"]').click()
  const plain = await rowStates(history)
  const plainTotal = await field(history, '合计')
  await history.locator('[data-testid="history-version"]').last().click()
  await page.waitForTimeout(300)
  const first = await rowStates(history)
  check(
    '关掉"显示更改"不标改动；选最早的版本看到当时的 4 行明细',
    plain.every((r) => r.endsWith(':same')) && !plainTotal?.changed && first.length === 4,
    { plain, plainTotal, first },
  )
  await closeHistory(page)
  await page.keyboard.press('Escape')
}

// ---- 3. 主管在仓库开领料单：关联订单、按配方带入、扫码、批量选择 ----

async function requisitionSection(browser, ctx) {
  const page = await consoleLogin(browser, 'boss')
  await page.goto(`${CONSOLE}/warehouse`)
  await page.locator('[data-testid="warehouse-tab-requisition"]').click()
  await page.locator('[data-testid="warehouse-new-document"]').click()
  const editor = page.locator('[data-testid="document-editor"]')
  await editor.waitFor()
  await pick(page, editor.locator('[data-testid="document-order-select"]'), ctx.made.no)
  await editor.locator('[data-testid="document-row"]').nth(2).waitFor()
  const prefilled = Object.fromEntries(
    (await names(editor, '[data-testid="document-row"]')).map((name, i, all) => [name, i]),
  )
  const before = await values(editor, '[data-testid="document-line-quantity"] input')
  const planned = Object.fromEntries(Object.entries(prefilled).map(([name, i]) => [name, before[i]]))
  check(
    '开领料单：选择关联的加工中订单（工人老王领取的），按配方带入材料（2 樘 × 用量：型材 13、玻璃 3.6、密封条 16）',
    planned['铝合金型材'] === '13' && planned['钢化玻璃'] === '3.6' && planned['密封条'] === '16',
    planned,
  )

  // 扫码加入已有的密封条：加数量，不加新行。
  await scan(editor, 'document-add', 'SEAL-1')
  await page.waitForTimeout(500)
  // 批量选择：辅料里的不锈钢螺丝 20 个。
  await editor.locator('[data-testid="document-add-batch"]').click()
  const picker = page.locator('[data-testid="item-picker"]')
  await picker.locator('[data-testid="item-picker-check"]').first().waitFor()
  const categories = (await picker.locator('[data-testid="item-picker-category"]').allInnerTexts()).map((c) => c.trim())
  await picker.locator('[data-testid="item-picker-category"]', { hasText: '辅料' }).click()
  await page.waitForTimeout(500)
  const listed = await names(picker, '[data-testid="item-picker-check"]')
  const screw = picker.locator('[data-testid="item-picker-quantity"][data-name="不锈钢螺丝"] input')
  await screw.fill('20')
  await screw.press('Tab')
  await picker.locator('[data-testid="item-picker-confirm"]').click()
  await picker.waitFor({ state: 'hidden' })
  await editor.locator('[data-testid="document-row"]').nth(3).waitFor()
  const rows = await names(editor, '[data-testid="document-row"]')
  const quantities = await values(editor, '[data-testid="document-line-quantity"] input')
  const lines = Object.fromEntries(rows.map((name, i) => [name, quantities[i]]))
  const count = await editor.locator('[data-testid="document-count"]').innerText()
  const direct = await editor.locator('[data-testid="document-direct"]').innerText()
  await shot(page, '4-requisition-entry')
  check(
    '扫码 SEAL-1：密封条 16 → 17（不加新行）；批量选择按材料分类（型材、玻璃、辅料），加入螺丝 20 个；共 4 项，提交后由仓管小陈确认',
    lines['密封条'] === '17' &&
      lines['不锈钢螺丝'] === '20' &&
      rows.length === 4 &&
      ['型材', '玻璃', '辅料'].every((c) => categories.includes(c)) &&
      [...listed].sort().join('|') === ['密封条', '不锈钢螺丝'].sort().join('|') &&
      count.includes('共 4 项') &&
      direct.includes('仓管小陈'),
    { lines, categories, listed, count, direct },
  )
  await editor.locator('[data-testid="document-submit"]').click()
  await editor.waitFor({ state: 'hidden' })
  const docs = await json(`${API}/api/v1/warehouse/documents?kind=requisition`, { token: ctx.admin })
  const doc = docs.items.find((d) => d.order_no === ctx.made.no && d.status === 'pending')
  check('领料单提交成功，关联订单，等仓管确认', Boolean(doc), docs.items.map((d) => [d.no, d.order_no, d.status]))
  ctx.requisition = doc
}

// ---- 4. 仓管确认、打印、单据的修改历史 ----

async function keeperSection(browser, ctx) {
  const page = await consoleLogin(browser, 'cang')
  await page.goto(`${CONSOLE}/warehouse?doc=${ctx.requisition.id}`)
  const drawer = page.locator('[data-testid="document-drawer"]')
  await drawer.locator('[data-testid="document-confirm"]').waitFor()
  const stepsOf = () =>
    drawer.locator('[data-testid="doc-steps"] li').evaluateAll((els) =>
      els.map((el) => `${el.querySelector('.step-label')?.textContent?.trim()}:${el.className}`),
    )
  const pending = await stepsOf()
  await shot(page, '5-keeper-pending')
  await drawer.locator('[data-testid="document-confirm"]').click()
  await confirmBox(page, '确认')
  await drawer.locator('[data-testid="document-status"]', { hasText: '已确认' }).waitFor()
  const confirmed = await stepsOf()
  check(
    '单据页顶部的处理进度：开单（完成）→ 仓管确认（当前）→ 已生效；确认后三步都完成',
    pending.join('|') === '开单:done|仓管确认:current|已生效:todo' &&
      confirmed.join('|') === '开单:done|仓管确认:done|已生效:done',
    { pending, confirmed },
  )

  // 打印：新窗口里的打印页，只有单据本身。
  const [popup] = await Promise.all([
    page.waitForEvent('popup'),
    drawer.locator('[data-testid="document-print"]').click(),
  ])
  await popup.waitForLoadState()
  const printTitle = await popup.title()
  const printed = await popup.locator('body').innerText()
  await popup.screenshot({ path: `${SHOTS}/6-print.png` })
  await popup.close()
  check(
    '打印：新窗口里的领料单（单号、关联订单、明细、确认人、签字栏）',
    printTitle === `领料单 ${ctx.requisition.no}` &&
      printed.includes(ctx.made.no) &&
      printed.includes('不锈钢螺丝') &&
      printed.includes('仓管小陈') &&
      printed.includes('仓管签字'),
    { printTitle, printed: printed.slice(0, 300) },
  )

  await drawer.locator('[data-testid="document-history"]').click()
  const history = historyDrawer(page)
  const actions = await versionActions(history)
  const status = await field(history, '状态')
  const keeper = await field(history, '确认人')
  await shot(page, '7-requisition-history')
  check(
    '领料单的修改历史：主管老李"开单"、仓管小陈"确认"两个版本；确认的版本标出状态 待确认 → 已确认、确认人',
    actions.join('|') === '确认|开单' &&
      status?.changed &&
      status.old === '待确认' &&
      status.value === '已确认' &&
      keeper?.value === '仓管小陈',
    { actions, status, keeper },
  )
  await closeHistory(page)
  return page
}

// ---- 5. 待办的修改历史 ----

async function todoSection(ctx, admin) {
  const kinds = (await json(`${API}/api/v1/todo-types`, { token: ctx.admin })).items
  const callback = kinds.find((t) => t.code === 'callback')
  const todo = await json(`${API}/api/v1/todos`, {
    method: 'POST',
    token: ctx.admin,
    body: {
      type_id: callback.id,
      title: '回电确认安装时间',
      customer_id: ctx.customerId,
      assignee_id: ctx.staff.mei,
    },
  })
  await json(`${API}/api/v1/todos/${todo.id}`, {
    method: 'PATCH',
    token: ctx.admin,
    body: { title: '回电确认安装日期' },
  })
  await json(`${API}/api/v1/todos/${todo.id}/done`, {
    method: 'POST',
    token: ctx.mei,
    body: { result: '约好周六上午' },
  })
  await admin.goto(`${CONSOLE}/todos?id=${todo.id}`)
  const drawer = admin.locator('[data-testid="todo-drawer"]')
  await drawer.locator('[data-testid="todo-history"]').click()
  const history = historyDrawer(admin)
  const actions = await versionActions(history)
  const actors = await history.locator('[data-testid="history-version"] .actor').allInnerTexts()
  const result = await field(history, '处理结果')
  await shot(admin, '8-todo-history')
  check(
    '待办的修改历史：新建、修改（管理员）、完成（客服小美）三个版本，完成的版本标出处理结果',
    actions.join('|') === '完成|修改|新建' &&
      actors.join('|') === '客服小美|管理员|管理员' &&
      result?.changed &&
      result.value === '约好周六上午',
    { actions, actors, result },
  )
  await closeHistory(admin)
  await admin.keyboard.press('Escape')
}

// ---- 6. 成品：改价、删除，成品的修改历史 ----

const productRow = (page, name) =>
  page.locator('[data-testid="products-table"] .el-table__row', { hasText: name }).first()

async function productSection(page) {
  await page.goto(`${CONSOLE}/products`)
  await productRow(page, '防盗门').waitFor()
  await productRow(page, '防盗门').locator('button', { hasText: '修改' }).click()
  const dialog = page.locator('[data-testid="product-dialog"]')
  await dialog.waitFor()
  await dialog.locator('input[data-testid="product-retail"]').fill('2800')
  await dialog.locator('[data-testid="product-save"]').click()
  await dialog.waitFor({ state: 'hidden' })
  await productRow(page, '样品测试窗').locator('button', { hasText: '删除' }).click()
  await confirmBox(page, '删除')
  await page.locator('.el-message--success', { hasText: '已删除' }).first().waitFor()

  await productRow(page, '防盗门').locator('[data-testid="product-versions"]').click()
  const history = historyDrawer(page)
  const actions = await versionActions(history)
  const price = await field(history, '建议零售价')
  await shot(page, '9-product-history')
  check(
    '成品的修改历史：新建、修改两个版本，修改的版本标出建议零售价 ¥2,600.00 → ¥2,800.00',
    actions.join('|') === '修改|新建' &&
      price?.changed &&
      price.old === '¥2,600.00' &&
      price.value === '¥2,800.00',
    { actions, price },
  )
  await closeHistory(page)
}

// ---- 7. 操作日志的修改历史（管理员）；客服没有 ----

async function feedSection(page, ctx) {
  await page.goto(`${CONSOLE}/audit`)
  const feed = page.locator('[data-testid="history-feed"]')
  await feed.locator('.el-table__row').first().waitFor()
  const types = new Set(await feed.locator('.el-table__row td:nth-child(3)').allInnerTexts())
  check(
    '操作日志默认打开"修改历史"：订单、领料单、待办、成品和材料的版本都在一个列表里',
    ['订单', '领料单', '待办', '成品', '材料'].every((t) => types.has(t)),
    [...types],
  )

  // 按操作筛选"删除"：删除的商品也能打开历史。
  await choose(page, page.locator('[data-testid="history-action"]'), '删除')
  await page.waitForTimeout(600)
  const deletedRows = await feed.locator('[data-testid="history-open"]').evaluateAll((els) =>
    els.map((el) => el.getAttribute('data-label')),
  )
  const tags = await feed.locator('[data-testid="history-action-tag"]').allInnerTexts()
  await feed.locator('[data-testid="history-open"][data-label="样品测试窗（TEST-01）"]').click()
  const history = historyDrawer(page)
  const actions = await versionActions(history)
  const deleted = await seen(history.locator('[data-testid="history-deleted"]'), 5000)
  await shot(page, '10-feed-deleted')
  check(
    '按操作筛选"删除"：列出删除的样品测试窗（带代码），打开后标"已删除"，最后一个版本是"删除"',
    deletedRows.includes('样品测试窗（TEST-01）') && tags.every((t) => t === '删除') && deleted && actions[0] === '删除',
    { deletedRows, tags, deleted, actions },
  )
  await closeHistory(page)

  // 按操作人筛选：仓管小陈只有确认领料单。
  await choose(page, page.locator('[data-testid="history-action"]'), '全部操作')
  await pick(page, page.locator('[data-testid="history-actor"]'), '仓管小陈')
  await page.waitForTimeout(600)
  const rows = await feed.locator('.el-table__row').evaluateAll((els) =>
    els.map((el) => [...el.querySelectorAll('td')].map((td) => td.textContent.trim()).slice(1, 5).join(' ')),
  )
  await shot(page, '11-feed-actor')
  check(
    '按操作人筛选"仓管小陈"：只有他确认的领料单',
    rows.length === 1 && rows[0].includes('仓管小陈') && rows[0].includes(ctx.requisition.no) && rows[0].includes('确认'),
    rows,
  )

  const agent = await json(`${API}/api/v1/history`, { token: ctx.mei, raw: true })
  const own = await json(`${API}/api/v1/orders?q=${ctx.orderNo}`, { token: ctx.mei })
  const ownHistory = await json(`${API}/api/v1/history/order/${own.items[0].id}`, { token: ctx.mei, raw: true })
  check(
    '客服不能看全部修改历史（403），但能看自己订单的修改历史',
    agent.status === 403 && ownHistory.status === 200,
    { feed: agent.status, own: ownHistory.status },
  )
}

// ---- 8. 手机：下单页全屏、明细变成卡片；单据详情全屏 ----

async function phoneSection(browser, ctx) {
  const page = await consoleLogin(browser, 'mei', PHONE)
  await page.goto(`${CONSOLE}/orders`)
  await page.locator('[data-testid="new-order"]').click()
  const form = page.locator('[data-testid="order-form"]:visible')
  await form.waitFor()
  await pick(page, form.locator('[data-testid="order-customer"]'), '李女士')
  await enter(page, form, 'order-add-product', '吸顶灯')
  await form.locator('[data-testid="order-line-0"]').waitFor()
  await page.waitForTimeout(400)
  const layout = await form.evaluate((root) => {
    const sheet = root.querySelector('.sheet')
    const body = root.querySelector('.sheet-body')
    const head = root.querySelector('[data-testid="order-lines"] thead')
    const cell = root.querySelector('[data-testid="order-line-0"] td[data-label="数量"]')
    const entry = root.querySelector('input[data-testid="order-add-product-input"]')
    return {
      width: Math.round(sheet.getBoundingClientRect().width),
      overflow: body.scrollWidth - body.clientWidth,
      head: getComputedStyle(head).display,
      label: getComputedStyle(cell, '::before').content,
      entry: Math.round(entry.getBoundingClientRect().width),
    }
  })
  for (const [field, value] of Object.entries(RECEIVER)) {
    await form.locator(`input[data-testid="receiver-${field}"]`).fill(value)
  }
  await shot(page, '12-order-phone')
  check(
    '手机下单：单据页全屏（390px），明细变成卡片（没有表头，每格带"数量"等标签），录入行可用，没有横向滚动',
    layout.width === PHONE.width &&
      layout.overflow <= 1 &&
      layout.head === 'none' &&
      layout.label.includes('数量') &&
      layout.entry > 200,
    layout,
  )
  await form.locator('[data-testid="order-save-draft"]').click()
  await form.waitFor({ state: 'hidden' })

  const keeper = await consoleLogin(browser, 'cang', PHONE)
  await keeper.goto(`${CONSOLE}/warehouse?doc=${ctx.requisition.id}`)
  const drawer = keeper.locator('[data-testid="document-drawer"]')
  await drawer.locator('[data-testid="document-detail-line"]').first().waitFor()
  const detail = await drawer.evaluate((root) => {
    const sheet = root.querySelector('.sheet')
    const body = root.querySelector('.sheet-body')
    return {
      width: Math.round(sheet.getBoundingClientRect().width),
      overflow: body.scrollWidth - body.clientWidth,
      lines: root.querySelectorAll('[data-testid="document-detail-line"]').length,
    }
  })
  await shot(keeper, '13-document-phone')
  check('手机上的单据详情全屏，4 行明细，没有横向滚动', detail.width === PHONE.width && detail.overflow <= 1 && detail.lines === 4, detail)
}

async function run(browser) {
  const ctx = await prepareTenant()
  const agent = await orderEntrySection(browser, ctx)
  await orderHistorySection(agent)
  await requisitionSection(browser, ctx)
  await keeperSection(browser, ctx)
  const admin = await consoleLogin(browser, 'admin')
  await todoSection(ctx, admin)
  await productSection(admin)
  await feedSection(admin, ctx)
  const menu = await agent.locator('[data-testid="main-menu"] .el-menu-item').allInnerTexts()
  check('客服的菜单里没有"操作日志"', !menu.some((m) => m.includes('操作日志')), menu)
  await phoneSection(browser, ctx)
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
