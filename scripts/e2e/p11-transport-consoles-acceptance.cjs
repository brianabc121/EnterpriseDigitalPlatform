// P11 验收：接口传输加密和按岗位的控制台（设计文档 §25.15）。
//
// 1. 传输加密：管理员在浏览器里登录、打开各个页面，所有接口请求都带加密会话、请求体是密文，线路上
//    看不到访问令牌（Authorization）、查询参数和登录密码，响应也是密文；页面照常显示数据。
// 2. 管理员：全部菜单；首页是团队的实时接待、待处理（待审核的订单、待确认的待办、待确认的单据，
//    点开后打开对应的页面并筛选好）和常用功能。
// 3. 客服小美：菜单只有首页、工作台、会话记录、待办、订单、客户、知识库（没有"商品"）；首页是我的
//    接待、我的待办（已逾期、今天到期）、待审核的订单和快捷入口；"新建订单"直接打开下单页，"已逾期"
//    打开筛选好的待办。隐藏的"商品"仍然可以从链接打开（隐藏菜单不改变权限）。
// 4. 仓管小陈（"仓管"角色）：菜单只有首页和仓库；首页列出待确认的领料单，点开直接确认；库存不足的
//    材料点开后只看库存不足的；"开领料单"直接打开开单页。仓库页显示由"仓管"角色的员工担任。
// 5. 工人老王：只有"加工"，登录后直接打开加工，打开首页地址也回到加工。
// 6. 知识管理员小凯：菜单只有首页和知识库；首页是待审核的知识和 7 天内到期的知识，"去审核台"打开
//    审核台。
// 7. 客服兼仓管老陈：两个岗位的菜单合在一起，首页依次显示客服和仓管的内容。
// 8. 管理员在"设置 → 控制台"给客服勾选"商品"，小美重新打开后看到"商品"；恢复默认后又看不到。
// 9. 管理员新建自定义角色"拣货员"：不选岗位时按权限判断（仓管），选成"工人"后，有这个角色的员工
//    只看到"加工"。
// 10. 手机：仓管首页没有横向滚动。
//
// 前置：后端（传输加密为 optional 或 required）、控制台。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p11-transport-consoles-acceptance.cjs
const { chromium } = require('playwright')
const fs = require('fs')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p11-transport-consoles')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `p11-${RUN}`
const PASSWORD = 'demo-pass-2026'
const DESKTOP = { width: 1440, height: 900 }
const PHONE = { width: 390, height: 844 }
const TITLES = {
  dashboard: '首页',
  workbench: '工作台',
  sessions: '会话记录',
  todos: '待办',
  orders: '订单',
  receivables: '应收账款',
  contracts: '合同',
  materials: '资料',
  products: '商品',
  production: '加工',
  warehouse: '仓库',
  tasks: '个人待办',
  customers: '客户',
  knowledge: '知识库',
  ai: 'AI 接待',
  wake: 'AI 唤醒',
  assistant: 'AI 助理',
  staff: '员工',
  reports: '报表',
  profit: '盈利报表',
  tokens: 'Token 计费',
  broadcasts: '群发',
  wecom: '企业微信',
  audit: '操作日志',
  settings: '设置',
}

const summary = { tenant: TENANT, checks: [], consoleErrors: [] }
const check = (name, ok, detail) =>
  summary.checks.push(
    `${ok ? 'PASS' : 'FAIL'} ${name}${ok || detail === undefined ? '' : ` -> ${JSON.stringify(detail)}`}`,
  )
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b)
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
// 待办的"今天到期"按企业的时区（默认上海）划分日期。
const shanghaiDate = (date) => new Date(date.getTime() + 8 * 3600e3).toISOString().slice(0, 10)

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

// 租户和员工：客服小美、主管老李、仓管小陈（"仓管"角色）、工人老王、知识管理员小凯、客服兼仓管老陈；
// 库存不足的材料和一张主管开的待确认领料单；小美待审核的订单、已逾期和今天到期的待办；7 天内到期的
// 知识。
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
      name: `岗位验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  const admin = await login('admin')
  const staff = {}
  for (const [username, name, roles] of [
    ['mei', '客服小美', ['agent']],
    ['boss', '主管老李', ['supervisor']],
    ['cang', '仓管小陈', ['keeper']],
    ['wang', '工人老王', ['worker']],
    ['kate', '知识管理员小凯', ['knowledge_manager']],
    ['chen', '客服兼仓管老陈', ['agent', 'keeper']],
  ]) {
    const created = await json(`${API}/api/v1/staff`, {
      method: 'POST',
      token: admin,
      body: { username, display_name: name, password: PASSWORD, role_codes: roles },
    })
    staff[username] = created.id
  }
  const frame = await json(`${API}/api/v1/products`, {
    method: 'POST',
    token: admin,
    body: { code: 'AL-6063', name: '铝合金型材', unit: '米', kind: 'material', category: '型材', stock_alert: 10 },
  })
  await json(`${API}/api/v1/products/${frame.id}/stock`, {
    method: 'POST',
    token: admin,
    body: { mode: 'set', quantity: 6, note: '期初盘点' },
  })
  const window = await json(`${API}/api/v1/products`, {
    method: 'POST',
    token: admin,
    body: { code: 'WIN-01', name: '铝合金窗', unit: '樘', retail_price: '1800', category: '门窗' },
  })
  const boss = await login('boss')
  const requisition = await json(`${API}/api/v1/warehouse/documents`, {
    method: 'POST',
    token: boss,
    body: { kind: 'requisition', lines: [{ product_id: frame.id, quantity: 2 }], note: '车间补料' },
  })
  const mei = await login('mei')
  const customer = await json(`${API}/api/v1/customers`, {
    method: 'POST',
    token: mei,
    body: { display_name: '李女士' },
  })
  await json(`${API}/api/v1/orders`, {
    method: 'POST',
    token: mei,
    body: {
      customer_id: customer.id,
      items: [{ product_id: window.id, quantity: 1 }],
      receiver: { name: '李女士', phone: '13800001111', address: '上海市浦东新区世纪大道100号' },
    },
  })
  const [type] = (await json(`${API}/api/v1/todo-types`, { token: mei })).items
  const now = Date.now()
  const dues = [new Date(now + 2000), new Date(now + 5 * 60e3)]
  for (const [title, due] of [
    ['给李女士回电确认尺寸', dues[0]],
    ['发送安装视频', dues[1]],
  ]) {
    await json(`${API}/api/v1/todos`, {
      method: 'POST',
      token: mei,
      body: { type_id: type.id, title, due_at: due.toISOString(), assignee_id: staff.mei },
    })
  }
  await json(`${API}/api/v1/kb/items`, {
    method: 'POST',
    token: admin,
    body: {
      title: '国庆安装优惠',
      content: '10 月 1 日至 7 日下单免安装费。',
      publish: true,
      valid_to: new Date(now + 3 * 86400e3).toISOString(),
    },
  })
  await sleep(2500) // 第一条待办到期（已逾期）
  const today = shanghaiDate(new Date())
  return {
    admin,
    staff,
    requisition,
    todayCount: dues.filter((due) => shanghaiDate(due) === today).length,
  }
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

async function consoleLogin(browser, username, { wire, viewport = DESKTOP } = {}) {
  const context = await browser.newContext({ viewport, locale: 'zh-CN' })
  const page = await context.newPage()
  const label = `${username}${viewport === PHONE ? '-phone' : ''}`
  watchErrors(page, label)
  pages.push([label, page])
  if (wire) {
    page.on('request', (r) => {
      const url = new URL(r.url())
      if (!url.pathname.startsWith('/api/')) return
      wire.requests.push({
        path: url.pathname,
        search: url.search,
        headers: r.headers(),
        body: r.postData() ?? '',
      })
    })
    page.on('response', (r) => {
      const url = new URL(r.url())
      if (!url.pathname.startsWith('/api/') || url.pathname.startsWith('/api/v1/transport/')) return
      wire.responses.push({ path: url.pathname, status: r.status(), type: r.headers()['content-type'] ?? '' })
    })
  }
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

async function tile(page, testid) {
  const text = await page.locator(`[data-testid="${testid}"] .value`).innerText()
  return Number(text.trim())
}

async function shot(page, name) {
  await page.waitForTimeout(600)
  await page.screenshot({ path: `${SHOTS}/${name}.png` })
}

async function confirmBox(page, button) {
  const box = page.locator('.el-message-box:visible')
  await box.waitFor()
  const message = await box.locator('.el-message-box__message').innerText()
  await box.locator('button', { hasText: button }).click()
  await box.waitFor({ state: 'hidden' }).catch(() => null)
  return message
}

// 1-2. 管理员：接口传输加密、全部菜单和团队首页。
async function adminSection(browser) {
  const wire = { requests: [], responses: [] }
  const page = await consoleLogin(browser, 'admin', { wire })
  await page.locator('[data-testid="home-team"]').waitFor()
  await page.locator('[data-testid="rt-serving"]').waitFor()
  await page.locator('[data-testid="home-documents-pending"]').waitFor()
  await shot(page, '1-admin-home')
  const menu = await menus(page)
  const me = await json(`${API}/api/v1/me`, { token: await login('admin') })
  check(
    '管理员：显示全部菜单（套餐包含的）',
    same(menu, me.console.menus.map((name) => TITLES[name])) && menu.includes('设置') && menu.includes('商品'),
    { menu, console: me.console },
  )
  const pending = {
    orders: await tile(page, 'home-orders-review'),
    documents: await tile(page, 'home-documents-pending'),
  }
  check('管理员首页：待审核的订单 1 个、待确认的单据 1 张', pending.orders === 1 && pending.documents === 1, pending)
  await page.locator('[data-testid="home-documents-pending"]').click()
  await page.waitForURL(/\/warehouse/)
  await page.locator('[data-testid="warehouse-tabs"] .el-tabs__item.is-active', { hasText: '领料单' }).waitFor()
  check('点开"待确认的单据"打开仓库的领料单', true)
  const handshake = (r) => r.path === '/api/v1/transport/handshake'
  const firstLoad = wire.requests.filter(handshake).length

  // 打开几个带查询参数和请求体的页面（整页打开，重新握手），再看线路上的请求。
  await page.goto(`${CONSOLE}/customers`)
  await page.locator('.el-table__row', { hasText: '李女士' }).first().waitFor()
  await page.goto(`${CONSOLE}/orders?view=pending_review`)
  await page.locator('[data-testid="orders-table"] .el-table__row').first().waitFor()
  await page.waitForLoadState('networkidle')
  const api = wire.requests.filter((r) => !r.path.startsWith('/api/v1/transport/'))
  const plain = api.filter((r) => !r.headers['x-edp-transport'] || !r.headers['x-edp-sealed'])
  const leaked = api.filter(
    (r) => r.headers.authorization || r.search || r.body.includes(PASSWORD) || r.body.includes('tenant_code'),
  )
  const unsealed = wire.responses.filter((r) => r.status < 300 && r.status !== 204 && !r.type.includes('x-edp-sealed'))
  check(
    `传输加密：${api.length} 个接口请求都加密（请求头、查询参数、请求体），线路上没有令牌、查询参数和密码`,
    api.length > 10 && plain.length === 0 && leaked.length === 0,
    { plain: plain.map((r) => r.path), leaked: leaked.map((r) => r.path) },
  )
  check('传输加密：响应也是密文', unsealed.length === 0 && wire.responses.length > 10, unsealed.slice(0, 5))
  const handshakes = wire.requests.filter(handshake).length
  check(
    '传输加密：页面里握手一次后复用会话（登录、首页、打开仓库），整页打开时重新握手',
    firstLoad === 1 && handshakes === 3,
    { firstLoad, handshakes },
  )
  return page
}

// 3. 客服小美：简洁的菜单和客服首页。
async function agentSection(browser, ctx) {
  const page = await consoleLogin(browser, 'mei')
  await page.locator('[data-testid="home-agent"]').waitFor()
  await page.locator('[data-testid="home-todos-overdue"]').waitFor()
  await shot(page, '2-agent-home')
  const menu = await menus(page)
  check(
    '客服：菜单只有首页、工作台、会话记录、待办、订单、合同、资料、个人待办、客户、知识库、AI 助理（没有"商品"）',
    same(menu, ['首页', '工作台', '会话记录', '待办', '订单', '合同', '资料', '个人待办', '客户', '知识库', 'AI 助理']),
    menu,
  )
  const numbers = {
    overdue: await tile(page, 'home-todos-overdue'),
    today: await tile(page, 'home-todos-today'),
    orders: await tile(page, 'home-agent-orders-review'),
  }
  // 今天到期的还包括系统分派给小美的"审核订单"待办（按时效规则，可能在今天或明天到期）。
  const counts = await json(`${API}/api/v1/todos/counts`, { token: await login('mei') })
  check(
    '客服首页：我的待办已逾期 1 条、今天到期的、待审核的订单 1 个；有我的接待',
    numbers.overdue === 1 &&
      numbers.today === counts.due_today &&
      counts.due_today >= ctx.todayCount &&
      numbers.orders === 1 &&
      (await page.locator('[data-testid="rt-my-serving"]').count()) === 1,
    { numbers, counts, todayCount: ctx.todayCount },
  )
  await page.locator('[data-testid="home-todos-overdue"]').click()
  await page.waitForURL(/\/todos/)
  const row = page.locator('[data-testid="todos-table"] .el-table__row')
  await row.first().waitFor()
  await page.waitForLoadState('networkidle')
  const titles = await row.allInnerTexts()
  check(
    '点开"已逾期"打开我的待办、只看已逾期的',
    titles.length === 1 && titles[0].includes('给李女士回电确认尺寸'),
    titles,
  )
  await page.goto(`${CONSOLE}/`)
  await page.locator('[data-testid="home-new-order"]').click()
  await page.waitForURL(/\/orders/)
  await page.locator('[data-testid="order-form"]:visible').waitFor()
  await shot(page, '3-agent-new-order')
  check('客服首页的"新建订单"直接打开下单页', !page.url().includes('new='), page.url())
  await page.keyboard.press('Escape')
  await page.goto(`${CONSOLE}/products`)
  await page.locator('[data-testid="products-table"]').waitFor()
  check('隐藏的"商品"仍然可以从链接打开（隐藏菜单不改变权限）', page.url().endsWith('/products'))
  return page
}

// 4. 仓管小陈：仓库首页，点开待确认的领料单直接确认。
async function keeperSection(browser, ctx) {
  const page = await consoleLogin(browser, 'cang')
  await page.locator('[data-testid="home-keeper"]').waitFor()
  const rows = page.locator('[data-testid="home-pending-document"]')
  await rows.first().waitFor()
  await shot(page, '4-keeper-home')
  const menu = await menus(page)
  check('仓管：菜单只有首页、仓库、个人待办、AI 助理', same(menu, ['首页', '仓库', '个人待办', 'AI 助理']), menu)
  const listed = await rows.evaluateAll((els) => els.map((el) => el.getAttribute('data-no')))
  const low = await tile(page, 'home-low-materials')
  check(
    '仓管首页：列出主管开的待确认领料单，库存不足的材料 1 种',
    same(listed, [ctx.requisition.no]) && low === 1,
    { listed, low },
  )
  await page.locator(`[data-testid="home-open-document-${ctx.requisition.no}"]`).click()
  const drawer = page.locator('[data-testid="document-drawer"]')
  await drawer.locator('[data-testid="document-confirm"]').click()
  await confirmBox(page, '确认')
  await drawer.locator('[data-testid="document-status"]', { hasText: '已确认' }).waitFor()
  await page.keyboard.press('Escape')
  await drawer.waitFor({ state: 'hidden' })
  await page.locator('[data-testid="home-pending-empty"]').waitFor()
  await shot(page, '5-keeper-confirmed')
  check('在首页点开领料单直接确认，确认后列表清空', (await rows.count()) === 0)

  await page.locator('[data-testid="home-low-materials"]').click()
  await page.waitForURL(/\/warehouse/)
  await page.locator('[data-testid="warehouse-item"]').first().waitFor()
  const lowOnly = await page.locator('[data-testid="warehouse-low-only"] input').isChecked()
  const items = await page.locator('[data-testid="warehouse-item"]').evaluateAll((els) =>
    els.map((el) => el.getAttribute('data-name')),
  )
  const keeper = await page.locator('[data-testid="warehouse-keeper"]').innerText()
  check(
    '点开"库存不足的材料"只看库存不足的；仓库页显示由"仓管"角色的员工担任',
    lowOnly && same(items, ['铝合金型材']) && keeper.includes('仓管小陈') && keeper.includes('“仓管”角色'),
    { lowOnly, items, keeper },
  )
  await page.goto(`${CONSOLE}/`)
  await page.locator('[data-testid="home-new-requisition"]').click()
  await page.locator('[data-testid="document-editor"]:visible').waitFor()
  await shot(page, '6-keeper-new-requisition')
  check('仓管首页的"开领料单"直接打开开单页', page.url().endsWith('/warehouse'), page.url())
}

// 手机：仓管首页没有横向滚动，菜单收起成图标。
async function phoneSection(browser) {
  const page = await consoleLogin(browser, 'cang', { viewport: PHONE })
  await page.locator('[data-testid="home-keeper"]').waitFor()
  await shot(page, '12-keeper-phone')
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  check('手机：仓管首页没有横向滚动', overflow <= 0, { overflow })
}

// 5. 工人老王：只有"加工"。
async function workerSection(browser) {
  const page = await consoleLogin(browser, 'wang')
  await page.waitForURL(/\/production/)
  const menu = await menus(page)
  await page.goto(`${CONSOLE}/`)
  await page.waitForURL(/\/production/)
  await shot(page, '7-worker')
  check('工人：只有"加工"（另有个人待办、AI 助理），登录后和打开首页地址都直接打开加工', same(menu, ['加工', '个人待办', 'AI 助理']), menu)
}

// 6. 知识管理员小凯：待审核和快到期的知识。
async function knowledgeSection(browser) {
  const page = await consoleLogin(browser, 'kate')
  await page.locator('[data-testid="home-knowledge"]').waitFor()
  await page.locator('[data-testid="home-kb-expiring-item"]').first().waitFor()
  await shot(page, '8-knowledge-home')
  const menu = await menus(page)
  const expiring = await page.locator('[data-testid="home-kb-expiring-item"]').allInnerTexts()
  const review = await page.locator('[data-testid="home-kb-review"]').innerText()
  check(
    '知识管理员：菜单只有首页、资料、个人待办、知识库、AI 助理；首页有待审核的知识和 7 天内到期的知识',
    same(menu, ['首页', '资料', '个人待办', '知识库', 'AI 助理']) &&
      expiring.length === 1 &&
      expiring[0].includes('国庆安装优惠') &&
      review.includes('没有待审核的知识'),
    { menu, expiring, review },
  )
  await page.locator('[data-testid="home-kb-review-link"]').click()
  await page.waitForURL(/\/knowledge/)
  const active = page.locator('[data-testid="kb-tabs"] .el-tabs__item.is-active')
  await active.waitFor()
  check('"去审核台"打开知识库的审核台', (await active.innerText()).includes('审核台'), await active.innerText())
}

// 7. 客服兼仓管：菜单合在一起，首页依次显示两个岗位的内容。
async function combinedSection(browser) {
  const page = await consoleLogin(browser, 'chen')
  await page.locator('[data-testid="home-agent"]').waitFor()
  await page.locator('[data-testid="home-keeper"]').waitFor()
  await shot(page, '9-agent-keeper-home')
  const menu = await menus(page)
  const profiles = await page.locator('[data-testid="home-profiles"]').innerText()
  check(
    '客服兼仓管：两个岗位的菜单合在一起，首页依次显示客服和仓管的内容',
    same(menu, ['首页', '工作台', '会话记录', '待办', '订单', '合同', '资料', '仓库', '个人待办', '客户', '知识库', 'AI 助理']) &&
      profiles.includes('客服、仓管'),
    { menu, profiles },
  )
}

// 8. 管理员调整客服的菜单。
async function consoleSettingsSection(admin, browser) {
  await admin.goto(`${CONSOLE}/settings`)
  await admin.locator('[data-testid="settings-tabs"] .el-tabs__item', { hasText: '控制台' }).click()
  await admin.locator('[data-testid="console-matrix"]').waitFor()
  await admin.locator('[data-testid="console-agent-products"]').click()
  await admin.locator('[data-testid="console-reset-agent"]').waitFor()
  await shot(admin, '10-console-settings')
  await admin.locator('[data-testid="console-save"]').click()
  await admin.locator('.el-message--success').waitFor()
  const mei = await consoleLogin(browser, 'mei')
  const added = await menus(mei)
  check(
    '管理员给客服勾选"商品"后，客服重新打开控制台看到"商品"',
    same(added, ['首页', '工作台', '会话记录', '待办', '订单', '合同', '资料', '商品', '个人待办', '客户', '知识库', 'AI 助理']),
    added,
  )
  await admin.locator('[data-testid="console-reset-agent"]').click()
  await admin.locator('[data-testid="console-save"]').click()
  await admin.locator('.el-message--success').last().waitFor()
  await mei.reload()
  await mei.locator('[data-testid="main-menu"]').waitFor()
  const restored = await menus(mei)
  check('恢复默认后客服又看不到"商品"', !restored.includes('商品'), restored)
}

// 9. 自定义角色选择岗位。
async function customRoleSection(admin, browser, ctx) {
  await admin.goto(`${CONSOLE}/staff`)
  await admin.locator('.el-tabs__item', { hasText: '角色' }).click()
  await admin.locator('[data-testid="new-role"]').click()
  const dialog = admin.locator('[data-testid="role-dialog"]')
  await dialog.locator('input[placeholder="小写字母开头，如 quality"]').fill('picker')
  await dialog.locator('.el-form-item', { hasText: '名称' }).locator('input').fill('拣货员')
  for (const name of ['查看和调整库存', '领取订单加工']) {
    await dialog.locator('.el-checkbox', { hasText: name }).click()
  }
  await dialog.locator('button', { hasText: '保存' }).click()
  const auto = admin.locator('[data-testid="role-console-picker"]')
  await auto.waitFor()
  const autoRow = await admin.locator('[data-testid="role-table"] .el-table__row', { hasText: '拣货员' }).innerText()
  check('自定义角色不选岗位时按权限判断（有库存管理的是仓管）', autoRow.includes('仓管') && autoRow.includes('按权限'), autoRow)
  await admin.locator('[data-testid="role-table"] .el-table__row', { hasText: '拣货员' }).locator('button', { hasText: '编辑' }).click()
  await dialog.locator('[data-testid="role-console"]').click()
  await admin.locator('.el-select-dropdown__item:visible', { hasText: '工人' }).click()
  await dialog.locator('button', { hasText: '保存' }).click()
  await admin.locator('[data-testid="role-table"] .el-table__row', { hasText: '拣货员' }).filter({ hasNotText: '按权限' }).waitFor()
  await shot(admin, '11-custom-role')
  await json(`${API}/api/v1/staff`, {
    method: 'POST',
    token: ctx.admin,
    body: { username: 'zhou', display_name: '拣货员小周', password: PASSWORD, role_codes: ['picker'] },
  })
  const zhou = await consoleLogin(browser, 'zhou')
  await zhou.waitForURL(/\/production/)
  const menu = await menus(zhou)
  // 自定义角色只勾了库存和加工的权限，没有个人待办、AI 助理的权限，所以这两个菜单也不显示。
  check('把"拣货员"的岗位选成工人后，有这个角色的员工只看到"加工"', same(menu, ['加工']), menu)
}

async function run(browser) {
  const ctx = await prepareTenant()
  const admin = await adminSection(browser)
  await agentSection(browser, ctx)
  await keeperSection(browser, ctx)
  await workerSection(browser)
  await knowledgeSection(browser)
  await combinedSection(browser)
  await consoleSettingsSection(admin, browser)
  await customRoleSection(admin, browser, ctx)
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
