// P20 验收：按员工设置页面和权限（设计文档 §31）。
//
// 1. 管理员新建员工：对话框里有"页面和权限"，默认"按角色"，预览坐席会看到的页面和登录后打开"首页"。
// 2. 选"自定义"：从坐席的页面和权限开始；去掉"知识库"和"查看知识库"，勾上"应收账款"自动勾上"查看应收
//    账款"（标"多给"），勾上"报表"再去掉"查看报表"后标"缺少权限，不会显示"；登录后打开"订单"；预览跟着变。
// 3. 保存后员工列表标"自定义"，悬停看到页面、登录后打开、多给和去掉的权限。
// 4. 这个员工登录后进入"订单"，菜单是勾选的页面（没有知识库、报表），打开知识库是"没有访问权限"；点"首页"
//    照常打开首页，刷新首页又进入"订单"；应收账款能打开。
// 5. 编辑这个员工：保留自定义的勾选；改回"按角色"后标签消失，员工重新登录进入首页，知识库回来了。
// 6. 选"租户管理员"角色时不能自定义。
// 7. 有员工管理权限的组长：自己没有的权限不能勾；勾"报表"页面提示权限不能给出；勾"客户"自动勾上
//    "查看客户"（多给），保存成功。
// 8. 手机上对话框不超出屏幕。
// 9. 分配权限的界面按业务模块整理（角色对话框）：每个模块的全选和计数、搜索、新建只看报表和日志的角色。
//
// 前置：后端、控制台。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p20-staff-access-acceptance.cjs
const { chromium } = require('playwright')
const fs = require('fs')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p20-staff-access')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `p20-${RUN}`
const PASSWORD = 'demo-pass-2026'
const DESKTOP = { width: 1440, height: 900 }
const PHONE = { width: 390, height: 844 }

const AGENT_PAGES = ['首页', '工作台', '会话记录', '待办', '订单', '合同', '资料', '个人待办', '客户', '知识库', 'AI 助理']
const CUSTOM_PAGES = ['首页', '工作台', '会话记录', '待办', '订单', '应收账款', '合同', '资料', '个人待办', '客户', 'AI 助理']

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

// 租户、组长（有员工管理权限，自己的权限不多）和组长能分配的"助理"角色。
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
      name: `员工权限验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  const admin = await login('admin')
  const post = (url, body) => json(`${API}${url}`, { method: 'POST', token: admin, body })
  await post('/api/v1/roles', {
    code: 'team_lead',
    name: '组长',
    permissions: ['dashboard:view', 'staff:read', 'staff:manage', 'customer:read', 'task:use'],
  })
  await post('/api/v1/roles', { code: 'helper', name: '助理', permissions: ['dashboard:view', 'task:use'] })
  await post('/api/v1/staff', {
    username: 'lead',
    display_name: '组长老周',
    password: PASSWORD,
    role_codes: ['team_lead'],
  })
}

async function consoleLogin(browser, username, { viewport = DESKTOP } = {}) {
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
  await page.waitForLoadState('networkidle')
  return page
}

async function menus(page) {
  const items = await page.locator('[data-testid="main-menu"] .el-menu-item').allInnerTexts()
  return items.map((text) => text.replace(/\d+\+?/g, '').trim())
}

const menu = (page, title) =>
  page.locator('[data-testid="main-menu"] .el-menu-item', { hasText: new RegExp('^\\s*' + title) }).click()

async function shot(page, name) {
  await page.waitForTimeout(600)
  await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true })
}

/** 等条件成立（预览、列表重新取数是异步的）。 */
async function until(fn, timeout = 10_000) {
  const deadline = Date.now() + timeout
  for (;;) {
    const value = await fn()
    if (value || Date.now() > deadline) return value
    await new Promise((resolve) => setTimeout(resolve, 200))
  }
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b)
/** 预览里"会看到的页面"。 */
const preview = async (dialog) =>
  (await dialog.locator('[data-testid="access-preview-pages"] .page').allInnerTexts()).map((t) => t.trim())
const page_ = (dialog, name) => dialog.locator(`[data-testid="access-menu-${name}"]`)
const perm = (dialog, code) => dialog.locator(`[data-testid="perm-${code}"]`)
const module_ = (dialog, key) => dialog.locator(`[data-testid="perm-module-${key}"]`)
/** 展开全部模块（员工的自定义默认收起，只看每个模块的摘要）。 */
async function expandAll(dialog) {
  const button = dialog.locator('[data-testid="permission-expand"]')
  if ((await button.innerText()).includes('全部展开')) await button.click()
}
const checked = async (locator) => /is-checked/.test((await locator.getAttribute('class')) ?? '')
const disabled = async (locator) => /is-disabled/.test((await locator.getAttribute('class')) ?? '')

async function chooseRole(dialog, name, on) {
  const box = dialog.locator('.el-form-item', { hasText: '角色' }).first().locator('.el-checkbox', { hasText: name })
  if ((await checked(box)) !== on) await box.click()
}

async function fill(dialog, label, value) {
  await dialog.locator('.el-form-item', { hasText: label }).first().locator('input').fill(value)
}

// 1–3. 管理员新建自定义页面和权限的员工。
async function createSection(browser) {
  const admin = await consoleLogin(browser, 'admin')
  await menu(admin, '员工')
  await admin.waitForURL(/\/staff/)
  await admin.locator('button', { hasText: '新建员工' }).click()
  const dialog = admin.locator('[data-testid="staff-create"]')
  await dialog.locator('[data-testid="staff-access"]').waitFor()
  await fill(dialog, '用户名', 'xiao')
  await fill(dialog, '姓名', '客服小王')
  await fill(dialog, '初始密码', PASSWORD)
  const byRole = await until(async () => (await preview(dialog)).includes('知识库') && (await preview(dialog)))
  const home = await dialog.locator('[data-testid="access-preview-home"]').innerText()
  check(
    '新建员工默认"按角色"：预览坐席看到的页面，登录后打开"首页"',
    same(byRole, AGENT_PAGES) && home.trim() === '首页',
    { byRole, home },
  )
  await shot(admin, '1-create-by-role')

  await dialog.locator('[data-testid="access-mode-custom"]').click()
  await dialog.locator('[data-testid="permission-picker"]').waitFor()
  // 自定义默认收起：每个模块一行，写着勾上的页面和勾了几项。
  const collapsed = await module_(dialog, 'orders').innerText()
  const ordersBodyHidden = !(await perm(dialog, 'order:read').isVisible())
  check(
    '选"自定义"后按业务模块收起显示：订单与商品一行写着"页面：订单"和"4/10"',
    ordersBodyHidden && collapsed.includes('订单与商品') && collapsed.includes('页面：订单') && collapsed.includes('4/10'),
    collapsed,
  )
  await shot(admin, '2a-create-custom-modules')
  await expandAll(dialog)
  await page_(dialog, 'dashboard').waitFor()
  const startPages = []
  for (const name of ['dashboard', 'workbench', 'knowledge', 'receivables', 'reports', 'settings']) {
    startPages.push(await checked(page_(dialog, name)))
  }
  const diff0 = await dialog.locator('[data-testid="access-diff"]').innerText()
  check(
    '选"自定义"：从坐席的页面和权限开始（首页、工作台、知识库勾上，应收账款、报表、设置没有勾），和角色比没有差别',
    same(startPages, [true, true, true, false, false, false]) &&
      (await checked(perm(dialog, 'kb:read'))) &&
      diff0.includes('多给 0 项，去掉 0 项'),
    { startPages, diff0 },
  )

  // 去掉知识库页面和"查看知识库"权限；勾上应收账款（自动勾上查看应收账款）；勾上报表后去掉查看报表。
  await page_(dialog, 'knowledge').click()
  await perm(dialog, 'kb:read').click()
  await page_(dialog, 'receivables').click()
  const financeAdded = await until(() => checked(perm(dialog, 'finance:view')))
  const extraMark = await perm(dialog, 'finance:view').innerText()
  await page_(dialog, 'reports').click()
  await until(() => checked(perm(dialog, 'report:view')))
  await perm(dialog, 'report:view').click()
  const lack = await until(async () => (await module_(dialog, 'reports').innerText()).includes('缺少权限，不会显示'))
  check(
    '勾上"应收账款"自动勾上"查看应收账款"（标"多给"）；勾上"报表"再去掉"查看报表"，标"缺少权限，不会显示"',
    financeAdded && extraMark.includes('多给') && lack,
    { financeAdded, extraMark },
  )
  const kbMark = await perm(dialog, 'kb:read').innerText()
  const diff1 = await dialog.locator('[data-testid="access-diff"]').innerText()
  check(
    '去掉的"查看知识库"标"去掉"，和角色比：多给 1 项，去掉 1 项',
    kbMark.includes('去掉') && diff1.includes('多给 1 项，去掉 1 项'),
    { kbMark, diff1 },
  )

  const knowledgeHead = await module_(dialog, 'knowledge').locator('.head').innerText()
  check('模块标题上标出和角色的差别：知识库"去掉 1"', knowledgeHead.includes('去掉 1'), knowledgeHead)

  await dialog.locator('[data-testid="access-home"]').click()
  await admin.locator('.el-select-dropdown__item:visible', { hasText: /^\s*订单\s*$/ }).click()
  const custom = await until(async () => same(await preview(dialog), CUSTOM_PAGES) && (await preview(dialog)))
  check('预览跟着变：看不到知识库和报表，多了应收账款', same(custom, CUSTOM_PAGES), custom)
  await shot(admin, '2-create-custom')

  await dialog.locator('button', { hasText: '保存' }).click()
  await admin.locator('.el-message--success', { hasText: '已创建员工' }).waitFor()
  const tag = admin.locator('[data-testid="custom-access-xiao"]')
  await tag.waitFor()
  await tag.hover()
  const tooltip = admin.locator('.el-popper:visible', { hasText: '页面：' })
  await tooltip.waitFor()
  const lines = await tooltip.innerText()
  check(
    '员工列表标"自定义"，悬停看到页面、登录后打开、多给和去掉的权限',
    lines.includes('登录后打开：订单') &&
      lines.includes('多给：查看应收账款') &&
      lines.includes('去掉：') &&
      lines.includes('查看知识库') &&
      lines.includes('查看报表') === false,
    lines,
  )
  await shot(admin, '3-staff-list')
  return admin
}

// 4. 自定义的员工登录。
async function staffSection(browser) {
  const xiao = await consoleLogin(browser, 'xiao')
  const landed = new URL(xiao.url()).pathname
  const menu1 = await menus(xiao)
  check(
    '员工登录后进入"订单"，菜单是勾选的页面（没有知识库、报表）',
    landed === '/orders' && same(menu1, CUSTOM_PAGES),
    { landed, menu1 },
  )
  await shot(xiao, '4-staff-orders')
  await xiao.goto(`${CONSOLE}/knowledge`)
  const forbidden = await xiao
    .locator('.el-result__title', { hasText: '没有访问权限' })
    .waitFor({ timeout: 8000 })
    .then(() => true)
    .catch(() => false)
  check('去掉了"查看知识库"：打开知识库是"没有访问权限"', forbidden)
  await menu(xiao, '首页')
  await xiao.waitForTimeout(800)
  const home = new URL(xiao.url()).pathname
  await xiao.goto(`${CONSOLE}/`)
  await xiao.waitForURL(/\/orders/)
  const reopened = new URL(xiao.url()).pathname
  check('点"首页"照常打开首页；从地址栏打开控制台又进入"订单"', home === '/' && reopened === '/orders', {
    home,
    reopened,
  })
  await menu(xiao, '应收账款')
  await xiao.waitForURL(/\/receivables/)
  await xiao.waitForLoadState('networkidle')
  const receivables = !(await xiao.locator('.el-result__title', { hasText: '没有访问权限' }).isVisible())
  check('多给的"查看应收账款"：应收账款能打开', receivables)
  await xiao.context().close()
}

// 5–6. 编辑：保留自定义，改回按角色；租户管理员不能自定义。
async function editSection(browser, admin) {
  const row = admin.getByTestId('staff-node-xiao')
  await row.locator('button', { hasText: '编辑' }).click()
  const dialog = admin.locator('[data-testid="staff-edit"]')
  await dialog.locator('[data-testid="permission-picker"]').waitFor()
  await expandAll(dialog)
  await page_(dialog, 'receivables').waitFor()
  const kept =
    (await checked(page_(dialog, 'receivables'))) &&
    !(await checked(page_(dialog, 'knowledge'))) &&
    (await checked(perm(dialog, 'finance:view'))) &&
    !(await checked(perm(dialog, 'kb:read')))
  check('编辑员工：保留自定义的页面和权限', kept)
  await shot(admin, '5-edit-custom')
  await dialog.locator('[data-testid="access-mode-role"]').click()
  await dialog.locator('button', { hasText: '保存' }).click()
  await admin.locator('.el-message--success', { hasText: '已保存' }).waitFor()
  const gone = await until(async () => (await admin.locator('[data-testid="custom-access-xiao"]').count()) === 0)
  const xiao = await consoleLogin(browser, 'xiao')
  const landed = new URL(xiao.url()).pathname
  const menu2 = await menus(xiao)
  check(
    '改回"按角色"：标签消失，员工重新登录进入首页，菜单回到坐席的（有知识库）',
    gone && landed === '/' && same(menu2, AGENT_PAGES),
    { gone, landed, menu2 },
  )
  await xiao.context().close()

  await admin.locator('button', { hasText: '新建员工' }).click()
  const create = admin.locator('[data-testid="staff-create"]')
  await chooseRole(create, '租户管理员', true)
  const locked = await until(async () =>
    (await create.locator('[data-testid="staff-access"]').innerText()).includes('不能单独调整'),
  )
  const customDisabled = await create.locator('[data-testid="access-mode-custom"]').getAttribute('class')
  check('选了"租户管理员"不能自定义（提示不能单独调整）', locked && /is-disabled/.test(customDisabled ?? ''), customDisabled)
  await shot(admin, '6-create-admin-locked')
  await create.locator('button', { hasText: '取消' }).click()
}

// 7. 组长：只能给出自己有的权限。
async function leadSection(browser) {
  const lead = await consoleLogin(browser, 'lead')
  await menu(lead, '员工')
  await lead.waitForURL(/\/staff/)
  await lead.locator('button', { hasText: '新建员工' }).click()
  const dialog = lead.locator('[data-testid="staff-create"]')
  await dialog.locator('[data-testid="staff-access"]').waitFor()
  await fill(dialog, '用户名', 'zhuli')
  await fill(dialog, '姓名', '助理小李')
  await fill(dialog, '初始密码', PASSWORD)
  await chooseRole(dialog, '坐席', false)
  await chooseRole(dialog, '助理', true)
  await until(async () => (await preview(dialog)).includes('个人待办'))
  await dialog.locator('[data-testid="access-mode-custom"]').click()
  await dialog.locator('[data-testid="permission-picker"]').waitFor()
  await expandAll(dialog)
  await page_(dialog, 'reports').waitFor()
  const reportDisabled = await disabled(perm(dialog, 'report:view'))
  const customerEnabled = !(await disabled(perm(dialog, 'customer:read')))
  await page_(dialog, 'reports').click()
  const warning = await lead
    .locator('.el-message--warning', { hasText: '报表需要的权限你自己没有' })
    .waitFor({ timeout: 5000 })
    .then(() => true)
    .catch(() => false)
  await page_(dialog, 'customers').click()
  const customerAdded = await until(() => checked(perm(dialog, 'customer:read')))
  check(
    '组长：自己没有的"查看报表"不能勾；勾"报表"页面提示权限不能给出；勾"客户"自动勾上"查看客户"',
    reportDisabled && customerEnabled && warning && customerAdded,
    { reportDisabled, customerEnabled, warning, customerAdded },
  )
  await shot(lead, '7-lead-create')
  await dialog.locator('button', { hasText: '保存' }).click()
  await lead.locator('.el-message--success', { hasText: '已创建员工' }).waitFor()
  const leadToken = await login('lead')
  const created = (await json(`${API}/api/v1/staff`, { token: leadToken })).items.find((s) => s.username === 'zhuli')
  // 助理的页面是首页和个人待办，加上客户和报表（报表缺少权限，不会显示）。
  check(
    '组长保存成功：多给"查看客户"，页面是助理的加上客户和报表',
    same(created.access.extra_permissions, ['customer:read']) &&
      same(created.access.menus, ['dashboard', 'tasks', 'customers', 'reports']),
    created.access,
  )
  await lead.context().close()
}

// 8. 手机。
async function phoneSection(browser) {
  const admin = await consoleLogin(browser, 'admin', { viewport: PHONE })
  await admin.goto(`${CONSOLE}/staff`)
  await admin.locator('button', { hasText: '新建员工' }).click()
  const dialog = admin.locator('[data-testid="staff-create"]')
  await dialog.locator('[data-testid="staff-access"]').waitFor()
  await dialog.locator('[data-testid="access-mode-custom"]').click()
  await dialog.locator('[data-testid="permission-picker"]').waitFor()
  await expandAll(dialog)
  await page_(dialog, 'dashboard').waitFor()
  await admin.waitForTimeout(400)
  const layout = await admin.evaluate(() => {
    const box = document.querySelector('[data-testid="staff-create"]').getBoundingClientRect()
    return { left: Math.round(box.left), right: Math.round(box.right), inner: window.innerWidth, width: document.documentElement.scrollWidth }
  })
  check('手机：对话框不超出屏幕，没有横向滚动', layout.left >= 0 && layout.right <= layout.inner && layout.width <= layout.inner, layout)
  await shot(admin, '8-phone')
}

// 9. 角色对话框：按业务模块分组，模块全选、计数和搜索。
async function roleSection(browser) {
  const admin = await consoleLogin(browser, 'admin')
  await admin.goto(`${CONSOLE}/staff`)
  await admin.locator('.el-tabs__item', { hasText: '角色' }).click()
  await admin.locator('[data-testid="new-role"]').click()
  const dialog = admin.locator('[data-testid="role-dialog"]')
  await dialog.locator('[data-testid="permission-picker"]').waitFor()
  const titles = (await dialog.locator('[data-testid^="perm-module-"] .title').allInnerTexts()).map((t) => t.trim())
  const total = await dialog.locator('[data-testid="permission-picker"] .summary').innerText()
  check(
    '角色对话框按业务模块分组：通用、接待、客户、待办、订单与商品、财务、合同、资料、加工与仓库、知识库、报表与日志、员工与设置，共 61 项',
    same(titles, ['通用', '接待', '客户', '待办', '订单与商品', '财务', '合同', '资料', '加工与仓库', '知识库', '报表与日志', '员工与设置']) &&
      total.includes('已选 0 / 61 项'),
    { titles, total },
  )
  const hint = await perm(dialog, 'customer:read').innerText()
  check('长名称分成标题和说明：查看客户 / 自己的和正在接待的', hint.includes('查看客户') && hint.includes('自己的和正在接待的'), hint)
  await shot(admin, '9-role-modules')

  await dialog.locator('[data-testid="permission-search"]').fill('导出')
  const found = await until(async () => {
    const shown = (await dialog.locator('[data-testid^="perm-module-"] .title').allInnerTexts()).map((t) => t.trim())
    return shown.length === 5 && shown
  })
  const items = (await dialog.locator('.item .item-title').allInnerTexts()).map((t) => t.trim())
  check(
    '搜索"导出"：只剩客户、待办、订单与商品、财务、员工与设置里和导出有关的 8 项',
    same(found, ['客户', '待办', '订单与商品', '财务', '员工与设置']) && items.length === 8,
    { found, items },
  )
  await shot(admin, '9-role-search')
  await dialog.locator('[data-testid="permission-search"]').fill('')

  await dialog.locator('input[placeholder="小写字母开头，如 quality"]').fill('auditor')
  await dialog.locator('.el-form-item', { hasText: '名称' }).locator('input').fill('审计')
  await dialog.locator('[data-testid="perm-module-all-reports"]').click()
  const count = await module_(dialog, 'reports').locator('.count').innerText()
  const summary = await dialog.locator('[data-testid="permission-picker"] .summary').innerText()
  check('模块全选：报表与日志 2/2，已选 2 项', count.trim() === '2/2' && summary.includes('已选 2 / 61 项'), { count, summary })
  await dialog.locator('button', { hasText: '保存' }).click()
  const row = admin.locator('[data-testid="role-table"] .el-table__row', { hasText: '审计' })
  await row.waitFor()
  const rowText = await row.innerText()
  check('保存后的角色有"查看报表"和"查看操作日志"', rowText.includes('查看报表') && rowText.includes('查看操作日志'), rowText)
  await admin.context().close()
}

async function run(browser) {
  await prepareTenant()
  const admin = await createSection(browser)
  await staffSection(browser)
  await editSection(browser, admin)
  await leadSection(browser)
  await roleSection(browser)
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
