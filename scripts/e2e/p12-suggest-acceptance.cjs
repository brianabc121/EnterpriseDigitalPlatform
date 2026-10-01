// P12 验收：开单时的商品联想（设计文档 §25.16）。
//
// 1. 客服小美下单（电脑）：录入行输入拼音首字母 lhjc 列出两个铝合金窗（标"拼音"）；规格的另一种
//    写法 1.2*1.5 找到 1.2m×1.5m 的那个（标"规格"）回车加入；win-0 按代码找到（标"代码"，代码里标出
//    输入的部分）；少字的"铝窗"列为相近（标题"没有完全匹配，相近的商品"）；俗称"窗纱"找到纱窗；
//    候选显示代码、分类、规格、建议零售价和可用库存（没有库存标红）；扫码 win01（没有横线）、
//    led36（型号）直接加入。
// 2. 订单里没有匹配商品库的明细：打开"对应到商品库"时按客户的说法直接列出相近的商品，选中后对应上。
// 3. 没有输入时点一下录入行：列出自己最近下单用过的商品；别人看不到。
// 4. 仓管小陈开领料单：拼音首字母找材料（库存和可用库存，不够标红）、俗称、扫码型号 M4（已有的行
//    加数量）、少字"钢玻璃"列为相近回车加入；批量选择的搜索也认拼音首字母；提交后再开单，点一下
//    录入行列出最近用过的材料。
// 5. 商品库和仓库列表的搜索认拼音首字母。
// 6. 手机：下单页的联想下拉不超出屏幕、没有横向滚动。
//
// 前置：后端、控制台。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p12-suggest-acceptance.cjs
const { chromium } = require('playwright')
const fs = require('fs')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p12-suggest')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `p12-${RUN}`
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

// 租户、员工（客服小美和小燕、仓管小陈）、成品（两个规格的铝合金窗、防盗门、纱窗（俗称窗纱）、
// 吸顶灯（型号 LED-36）、没有库存的智能门锁）、材料（型材、玻璃、没有库存的密封条、俗称螺钉的
// 不锈钢螺丝（型号 M4））、客户。
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
      name: `联想验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  const admin = await login('admin')
  const staff = {}
  for (const [username, name, role] of [
    ['mei', '客服小美', 'agent'],
    ['yan', '客服小燕', 'agent'],
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
  const goods = {}
  for (const [key, body] of [
    ['window', { code: 'WIN-01', name: '铝合金窗', unit: '樘', retail_price: '1800', spec: '1.2m×1.5m', category: '门窗' }],
    ['wide', { code: 'WIN-03', name: '铝合金窗', unit: '樘', retail_price: '2200', spec: '1.5m×1.8m', category: '门窗' }],
    ['door', { code: 'DOOR-01', name: '防盗门', unit: '樘', retail_price: '2600', spec: '甲级', category: '门窗' }],
    ['net', { code: 'NET-01', name: '纱窗', unit: '扇', retail_price: '120', spec: '隐形', category: '门窗', aliases: ['窗纱'] }],
    ['lamp', { code: 'LAMP-01', name: '吸顶灯', unit: '盏', retail_price: '299', model: 'LED-36', spec: '36W', category: '灯具', ready_made: true }],
    ['lock', { code: 'LOCK-X1', name: '智能门锁 X1', unit: '把', retail_price: '1299', category: '五金', ready_made: true }],
  ]) {
    goods[key] = await product(admin, body)
  }
  await setStock(admin, goods.lamp.id, 3)
  await setStock(admin, goods.lock.id, 0)
  const materials = {}
  for (const [key, body, stock] of [
    ['frame', { code: 'AL-6063', name: '铝合金型材', unit: '米', category: '型材' }, 100.5],
    ['glass', { code: 'GL-5', name: '钢化玻璃', unit: '平方米', category: '玻璃' }, 20],
    ['seal', { code: 'SEAL-1', name: '密封条', unit: '米', category: '辅料' }, 0],
    ['screw', { code: 'SCR-4', name: '不锈钢螺丝', unit: '个', model: 'M4', category: '辅料', aliases: ['螺钉'] }, 500],
  ]) {
    materials[key] = await product(admin, { ...body, kind: 'material' })
    if (stock) await setStock(admin, materials[key].id, stock)
  }
  const mei = await login('mei')
  const customer = await json(`${API}/api/v1/customers`, {
    method: 'POST',
    token: mei,
    body: { display_name: '李女士' },
  })
  return { admin, mei, staff, goods, materials, customerId: customer.id }
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

// el-select（可搜索）：输入关键字后点选。
async function pick(page, select, text) {
  await select.click()
  await page.keyboard.type(text)
  const option = page.locator('.el-select-dropdown__item:visible', { hasText: text }).first()
  await option.waitFor()
  await option.click()
}

/**
 * 录入行的下拉：输入后等这次输入的候选出来（下拉的 data-query 是这次的输入），返回候选（名称、
 * 代码、匹配方式、标签、库存是否标红、标出的部分）和标题。
 */
async function suggest(scope, testid, text) {
  const input = scope.locator(`input[data-testid="${testid}-input"]`)
  await input.fill(text)
  await scope.page().waitForFunction(
    ([sel, query]) => document.querySelector(sel)?.getAttribute('data-query') === query,
    [`[data-testid="${testid}-options"]`, text],
  )
  return dropdown(scope, testid)
}

async function dropdown(scope, testid) {
  const options = await scope.locator(`[data-testid="${testid}-option"]`).evaluateAll((els) =>
    els.map((el) => ({
      name: el.getAttribute('data-name'),
      code: el.getAttribute('data-code'),
      match: el.getAttribute('data-match'),
      tag: el.querySelector('.tag')?.textContent?.trim() ?? null,
      sub: el.querySelector('.sub > span')?.textContent?.replace(/\s+/g, ' ').trim() ?? '',
      stock: el.querySelector('.stock')?.textContent?.replace(/\s+/g, ' ').trim() ?? '',
      short: el.querySelector('.stock')?.classList.contains('short') ?? false,
      price: el.querySelector('.line .right')?.textContent?.trim() ?? '',
      marks: [...el.querySelectorAll('mark')].map((m) => m.textContent),
    })),
  )
  const title = scope.locator(`[data-testid="${testid}-title"]`)
  return { options, title: (await title.count()) ? (await title.innerText()).trim() : null }
}

// 扫码：扫码枪输入代码后马上回车（不等候选）。
async function scan(scope, testid, code) {
  const input = scope.locator(`input[data-testid="${testid}-input"]`)
  await input.fill(code)
  await input.press('Enter')
}

async function values(scope, selector) {
  return scope.locator(selector).evaluateAll((els) => els.map((el) => el.value))
}

async function names(scope, selector) {
  return scope.locator(selector).evaluateAll((els) => els.map((el) => el.getAttribute('data-name')))
}

const brief = (d) => d.options.map((o) => `${o.name}/${o.code}/${o.tag ?? ''}`)

// ---- 1、2. 客服下单：拼音、规格、代码、相近、俗称、扫码；没有匹配的明细按客户的说法联想 ----

async function orderSection(browser, ctx) {
  const page = await consoleLogin(browser, 'mei')
  await page.goto(`${CONSOLE}/orders`)
  await page.locator('[data-testid="new-order"]').click()
  const form = page.locator('[data-testid="order-form"]:visible')
  await form.waitFor()
  await pick(page, form.locator('[data-testid="order-customer"]'), '李女士')
  const entry = 'order-add-product'

  const pinyin = await suggest(form, entry, 'lhjc')
  await shot(page, '1-pinyin')
  check(
    '拼音首字母 lhjc：下拉列出两个铝合金窗（WIN-01、WIN-03），标"拼音"；显示代码、分类、规格、建议零售价',
    brief(pinyin).join('|') === '铝合金窗/WIN-01/拼音|铝合金窗/WIN-03/拼音' &&
      pinyin.options[0].sub === 'WIN-01 · 门窗 · 1.2m×1.5m' &&
      pinyin.options[0].price === '¥1,800.00',
    pinyin,
  )

  const spec = await suggest(form, entry, '1.2*1.5')
  await form.locator(`input[data-testid="${entry}-input"]`).press('Enter')
  await form.locator('[data-testid="order-line-0"]').waitFor()
  const first = await form.locator('[data-testid="order-line-0"] .item-sub').innerText()
  check(
    '规格的另一种写法 1.2*1.5：1.2m×1.5m 的铝合金窗排在最前面（标"规格"），回车加入',
    spec.options[0].tag === '规格' && spec.options[1]?.code === 'WIN-03' && first.includes('WIN-01'),
    { options: brief(spec), first },
  )

  const code = await suggest(form, entry, 'win-0')
  check(
    '代码 win-0（不分大小写）：WIN-01、WIN-03 标"代码"，代码里标出输入的部分',
    brief(code).join('|') === '铝合金窗/WIN-01/代码|铝合金窗/WIN-03/代码' &&
      code.options.every((o) => o.marks.includes('WIN-0')),
    code,
  )

  const similar = await suggest(form, entry, '铝窗')
  await shot(page, '2-similar')
  check(
    '少字"铝窗"：标题"没有完全匹配，相近的商品"，铝合金窗排在最前面、标"相近"',
    similar.title === '没有完全匹配，相近的商品' &&
      similar.options[0].name === '铝合金窗' &&
      similar.options.every((o) => o.tag === '相近'),
    similar,
  )

  const alias = await suggest(form, entry, '窗纱')
  check('俗称"窗纱"：纱窗排在最前面，标"俗称"', alias.options[0].tag === '俗称' && alias.title === null, alias)

  const lock = await suggest(form, entry, 'znms')
  check(
    '拼音首字母 znms 找到智能门锁 X1：可用库存 0 标红，显示建议零售价',
    lock.options[0].stock === '可用 0 把' && lock.options[0].short && lock.options[0].price === '¥1,299.00',
    lock,
  )
  await form.locator(`input[data-testid="${entry}-input"]`).press('Escape')
  await page.waitForTimeout(300)
  const sheetOpen = await form.isVisible()
  const listGone = (await form.locator(`[data-testid="${entry}-options"]`).count()) === 0
  check('Esc 只收起联想的下拉，单据还开着', sheetOpen && listGone, { sheetOpen, listGone })

  // 扫码：代码没有横线、型号，也直接加入；已经有的累加数量。
  await scan(form, entry, 'win01')
  await page.waitForFunction(
    () => document.querySelector('[data-testid="order-qty-0"] input')?.value === '2',
  )
  await scan(form, entry, 'led36')
  await form.locator('[data-testid="order-line-1"]').waitFor()
  const quantities = await values(form, '[data-testid^="order-qty-"] input')
  const lineNames = await form.locator('[data-testid^="order-line-"] .item-name').allInnerTexts()
  check(
    '扫码 win01（代码 WIN-01 没有横线）累加数量为 2；扫码型号 led36 加入吸顶灯',
    quantities.join('|') === '2|1' && lineNames.join('|') === '铝合金窗|吸顶灯',
    { quantities, lineNames },
  )

  // 客户的说法没有匹配商品库：打开"对应到商品库"直接列出相近的商品。
  await form.locator('[data-testid="order-add-text"]').click()
  const text = form.locator('input[data-testid="order-line-text-2"]')
  await text.fill('白色防盗门 甲级 一樘')
  await form.locator('[data-testid="order-line-map-2"]').click()
  const mapped = page.locator('.el-select-dropdown__item:visible', { hasText: '防盗门' }).first()
  await mapped.waitFor()
  const header = (await page.locator('[data-testid="order-line-map-2-title"]:visible').innerText()).trim()
  const offered = (await page.locator('.el-select-dropdown__item:visible').allInnerTexts()).map((t) =>
    t.replace(/\s+/g, ' ').trim(),
  )
  await shot(page, '3-map-unmatched')
  await mapped.click()
  await page.waitForTimeout(300)
  const mappedName = await form.locator('[data-testid="order-line-2"] .item-name').innerText()
  check(
    '没有匹配的明细"白色防盗门 甲级 一樘"：打开"对应到商品库"不用输入就列出相近的防盗门（标题"没有完全匹配"），选中后对应上',
    header === '没有完全匹配，相近的商品' &&
      ['防盗门', '相近', 'DOOR-01'].every((t) => offered[0]?.includes(t)) &&
      mappedName === '防盗门',
    { header, offered, mappedName },
  )

  for (const [field, value] of Object.entries(RECEIVER)) {
    await form.locator(`input[data-testid="receiver-${field}"]`).fill(value)
  }
  await shot(page, '4-order-lines')
  await page.keyboard.press('Control+S')
  await form.waitFor({ state: 'hidden' })
  const drawer = page.locator('[data-testid="order-drawer"]')
  await drawer.locator('[data-testid="order-edit"]').waitFor()
  const orders = await json(`${API}/api/v1/orders`, { token: ctx.mei })
  const saved = orders.items[0]
  const detail = await json(`${API}/api/v1/orders/${saved.id}`, { token: ctx.mei })
  const items = detail.items.map((i) => `${i.code}×${i.quantity}`)
  check('提交订单：铝合金窗 WIN-01 ×2、吸顶灯 ×1、防盗门 ×1', items.join('|') === 'WIN-01×2|LAMP-01×1|DOOR-01×1', items)
  await page.keyboard.press('Escape')
  return page
}

// ---- 3. 没有输入时：自己最近下单用过的 ----

async function recentOrderSection(page, ctx) {
  await page.goto(`${CONSOLE}/orders`)
  await page.locator('[data-testid="new-order"]').click()
  const form = page.locator('[data-testid="order-form"]:visible')
  await form.waitFor()
  const entry = 'order-add-product'
  await form.locator(`input[data-testid="${entry}-input"]`).click()
  await form.locator(`[data-testid="${entry}-option"]`).first().waitFor()
  const recent = await dropdown(form, entry)
  await shot(page, '5-recent')
  const others = await json(`${API}/api/v1/products/suggest?q=`, { token: await login('yan') })
  check(
    '没有输入时点一下录入行：标题"最近用过的"，列出刚下过单的铝合金窗、吸顶灯、防盗门；客服小燕没有最近用过的',
    recent.title === '最近用过的' &&
      [...recent.options.map((o) => o.code)].sort().join('|') === 'DOOR-01|LAMP-01|WIN-01' &&
      recent.options.every((o) => o.tag === null) &&
      others.recent === true &&
      others.items.length === 0,
    { recent: brief(recent), title: recent.title, others: others.items.length },
  )
  // ↓ 选择第二个、回车加入。
  await form.locator(`input[data-testid="${entry}-input"]`).press('ArrowDown')
  const second = recent.options[1].name
  await form.locator(`input[data-testid="${entry}-input"]`).press('Enter')
  await form.locator('[data-testid="order-line-0"]').waitFor()
  const added = await form.locator('[data-testid="order-line-0"] .item-name').innerText()
  check('最近用过的里按 ↓ 选第二个、回车加入', added === second, { added, second })
  await form.locator('[data-testid="doc-close"]').click()
  await form.waitFor({ state: 'hidden' })
}

// ---- 4. 仓管开领料单：拼音、俗称、型号扫码、相近、批量选择、最近用过的 ----

async function requisitionSection(browser, ctx) {
  const page = await consoleLogin(browser, 'cang')
  await page.goto(`${CONSOLE}/warehouse`)
  await page.locator('[data-testid="warehouse-tab-requisition"]').click()
  await page.locator('[data-testid="warehouse-new-document"]').click()
  const editor = page.locator('[data-testid="document-editor"]')
  await editor.waitFor()
  const entry = 'document-add'

  const seal = await suggest(editor, entry, 'mft')
  await shot(page, '6-requisition-pinyin')
  check(
    '领料单：拼音首字母 mft 找到密封条（标"拼音"），显示现有和可用库存，没有库存标红，没有价格',
    seal.options[0].name === '密封条' &&
      seal.options[0].tag === '拼音' &&
      seal.options[0].stock === '现有 0 · 可用 0 米' &&
      seal.options[0].short &&
      seal.options[0].price === '',
    seal,
  )

  const screw = await suggest(editor, entry, '螺钉')
  await editor.locator(`input[data-testid="${entry}-input"]`).press('Enter')
  await editor.locator('[data-testid="document-row"]').first().waitFor()
  await scan(editor, entry, 'M4')
  await page.waitForFunction(
    () => document.querySelector('[data-testid="document-line-quantity"] input')?.value === '2',
  )
  check(
    '俗称"螺钉"找到不锈钢螺丝（标"俗称"）回车加入；扫码型号 M4 累加数量为 2',
    screw.options[0].tag === '俗称' && screw.options[0].stock === '现有 500 · 可用 500 个',
    screw,
  )

  const frame = await suggest(editor, entry, 'lhjxc')
  await editor.locator(`input[data-testid="${entry}-input"]`).press('Enter')
  const glass = await suggest(editor, entry, '钢玻璃')
  await editor.locator(`input[data-testid="${entry}-input"]`).press('Enter')
  await editor.locator('[data-testid="document-row"]').nth(2).waitFor()
  const rows = await names(editor, '[data-testid="document-row"]')
  check(
    '拼音首字母 lhjxc 找到铝合金型材；少字"钢玻璃"列为相近（标题"没有完全匹配"），回车加入钢化玻璃',
    frame.options[0].name === '铝合金型材' &&
      glass.title === '没有完全匹配，相近的商品' &&
      glass.options[0].tag === '相近' &&
      rows.join('|') === '不锈钢螺丝|铝合金型材|钢化玻璃',
    { frame: brief(frame), glass, rows },
  )

  // 批量选择的搜索也认拼音首字母。
  await editor.locator(`[data-testid="${entry}-batch"]`).click()
  const picker = page.locator('[data-testid="item-picker"]')
  await picker.locator('[data-testid="item-picker-check"]').first().waitFor()
  await picker.locator('input[data-testid="item-picker-search"]').fill('bxgls')
  await page.waitForFunction(
    () => document.querySelectorAll('[data-testid="item-picker"] [data-testid="item-picker-check"]').length === 1,
  )
  const listed = await names(picker, '[data-testid="item-picker-check"]')
  check('批量选择的搜索：拼音首字母 bxgls 找到不锈钢螺丝', listed.join('|') === '不锈钢螺丝', listed)
  await picker.locator('button', { hasText: '取消' }).click()
  await picker.waitFor({ state: 'hidden' })

  await editor.locator('[data-testid="document-submit"]').click()
  await editor.waitFor({ state: 'hidden' })
  await page.locator('[data-testid="warehouse-new-document"]').click()
  await editor.waitFor()
  await editor.locator(`input[data-testid="${entry}-input"]`).click()
  await editor.locator(`[data-testid="${entry}-option"]`).first().waitFor()
  const recent = await dropdown(editor, entry)
  await shot(page, '7-requisition-recent')
  check(
    '提交后再开领料单，点一下录入行：列出最近用过的三种材料',
    recent.title === '最近用过的' &&
      [...recent.options.map((o) => o.code)].sort().join('|') === 'AL-6063|GL-5|SCR-4',
    { title: recent.title, options: brief(recent) },
  )
  await editor.locator(`input[data-testid="${entry}-input"]`).press('Escape')
  await editor.locator('button', { hasText: '取消' }).last().click()
  return page
}

// ---- 5. 商品库和仓库列表的搜索 ----

async function listSection(browser, warehousePage) {
  const page = await consoleLogin(browser, 'admin')
  await page.goto(`${CONSOLE}/products`)
  const table = page.locator('[data-testid="products-table"]')
  await table.locator('.el-table__row').first().waitFor()
  await page.locator('input[data-testid="product-search"]').fill('fdm')
  await page.locator('input[data-testid="product-search"]').press('Enter')
  await page.waitForFunction(
    () => document.querySelectorAll('[data-testid="products-table"] .el-table__row').length === 1,
  )
  const products = await table.locator('.el-table__row .name').allInnerTexts()

  await warehousePage.goto(`${CONSOLE}/warehouse`)
  await warehousePage.locator('[data-testid="warehouse-tab-material"]').click()
  await warehousePage.locator('[data-testid="warehouse-item"]').first().waitFor()
  await warehousePage.locator('input[data-testid="warehouse-item-search"]').fill('ghbl')
  await warehousePage.locator('input[data-testid="warehouse-item-search"]').press('Enter')
  await warehousePage.waitForFunction(
    () => document.querySelectorAll('[data-testid="warehouse-item"]').length === 1,
  )
  const items = await names(warehousePage, '[data-testid="warehouse-item"]')
  check(
    '商品库搜索 fdm 找到防盗门；仓库材料搜索 ghbl 找到钢化玻璃',
    products.length === 1 && products[0].includes('防盗门') && items.join('|') === '钢化玻璃',
    { products, items },
  )
}

// ---- 6. 手机：下拉不超出屏幕 ----

async function phoneSection(browser) {
  const page = await consoleLogin(browser, 'mei', PHONE)
  await page.goto(`${CONSOLE}/orders`)
  await page.locator('[data-testid="new-order"]').click()
  const form = page.locator('[data-testid="order-form"]:visible')
  await form.waitFor()
  const entry = 'order-add-product'
  await suggest(form, entry, 'lhjc')
  const list = form.locator(`[data-testid="${entry}-options"]`)
  await list.scrollIntoViewIfNeeded()
  const box = await list.boundingBox()
  const overflow = await page.evaluate(() => ({
    page: document.documentElement.scrollWidth - window.innerWidth,
    sheet: (() => {
      const el = document.querySelector('[data-testid="order-form"] .sheet-body') ?? document.querySelector('[data-testid="order-form"] .sheet')
      return el ? el.scrollWidth - el.clientWidth : 0
    })(),
  }))
  await shot(page, '8-phone')
  check(
    '手机：下单页的联想下拉在屏幕内（宽度不超过 390），页面没有横向滚动',
    box && box.x >= 0 && box.x + box.width <= PHONE.width && overflow.page <= 0,
    { box, overflow },
  )
}

async function run(browser) {
  const ctx = await prepareTenant()
  const orderPage = await orderSection(browser, ctx)
  await recentOrderSection(orderPage, ctx)
  const warehousePage = await requisitionSection(browser, ctx)
  await listSection(browser, warehousePage)
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
