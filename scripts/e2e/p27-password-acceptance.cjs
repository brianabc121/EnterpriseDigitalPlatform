// P27 验收：重置密码（设计文档 §38）——企业后台为全部角色的员工重置密码，平台运营后台为企业拥有者（管理员账号）
// 重置密码；重置后的密码是临时密码，登录后先设置新密码；之前的登录立即失效。
//
// 1. 准备：企业（拥有者"张总"）和每个系统角色的员工——客服小艾、主管、财务、出纳、工厂工人、仓管、副管理员
//    （企业所有者），另有只能管理员工的"人事"和只能查看员工的"访客"（自定义角色）。
// 2. 张总在"员工"页面（员工导图的卡片）：每张员工卡片的"重置密码"、"停用"、删除都能用，自己的卡片上是"修改密码"、
//    不能删除；为小艾自动
//    生成新密码（只显示一次，可以复制），卡片上标"待改密码"；再为其他系统角色的员工逐个重置，用新密码都能登录、
//    都要先设置新密码，旧密码不能登录；工人已经打开的页面立即回到登录页；手动为人事设置密码、不要求修改。
// 3. 人事登录：权限高于自己的员工（管理员、客服）的"重置密码"、"停用"、删除都置灰并提示，访客的都能用；自己的
//    卡片不能删除。
// 4. 小艾用新密码登录后只能进入"设置新密码"页面（说明谁在什么时候重置的），其他页面和接口都不行；设置后进入控制台。
// 5. 运营后台的租户详情"管理员账号"：张总标"拥有者"、副管理员；填写原因后为张总重置密码，显示临时密码和登录信息。
// 6. 张总之前的登录立即失效；副管理员收到站内信，操作日志里记着"平台运维"和原因；张总用临时密码登录后看到
//    "平台运维人员……重置了你的密码，原因：……"，设置新密码后进入控制台；运营后台显示已经改过；平台审计里有记录。
//
// 前置：与 p0 相同（后端、控制台 :5173、运营后台 :5174）。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<密码> node scripts/e2e/p27-password-acceptance.cjs
const { chromium } = require('playwright')
const fs = require('fs')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PLATFORM = env('PLATFORM_URL', 'http://localhost:5174')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p27')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `pwd-${RUN}`
const PASSWORD = 'demo-pass-2026'
const HR_PASSWORD = 'hr-new-pass-2026'
const REASON = '企业负责人来电，核对营业执照后申请重置'
// 每个系统角色一个员工（企业所有者是副管理员）。
const MEMBERS = [
  ['alice', '小艾', ['agent']],
  ['sam', '主管老孙', ['supervisor']],
  ['fay', '财务小芳', ['finance']],
  ['qian', '出纳小钱', ['cashier']],
  ['wang', '工人老王', ['worker']],
  ['kay', '仓管小凯', ['keeper']],
  ['root2', '副管理员', ['tenant_admin']],
]

const summary = { tenant: TENANT, checks: [], consoleErrors: [] }
const check = (name, ok, detail) =>
  summary.checks.push(
    `${ok ? 'PASS' : 'FAIL'} ${name}${ok || detail === undefined ? '' : ` -> ${JSON.stringify(detail)}`}`,
  )
const state = { contexts: [], pages: [], temporary: {} }

async function request(url, { method = 'GET', token, body } = {}) {
  return fetch(url, {
    method,
    headers: {
      'content-type': 'application/json',
      ...(token ? { authorization: `Bearer ${token}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

async function json(url, options) {
  const response = await request(url, options)
  const text = await response.text()
  if (!response.ok) throw new Error(`${options?.method ?? 'GET'} ${url} -> ${response.status} ${text}`)
  return text ? JSON.parse(text) : null
}

// 登录按 IP 限流（每分钟 30 次，员工和运营人员的登录一起算）：这个脚本登录得多，自己控制在每分钟 18 次以内，
// 给前后的验收脚本留出余量。
const LOGINS_PER_MINUTE = 18
const loginTimes = []

async function pace() {
  for (;;) {
    const now = Date.now()
    while (loginTimes.length && now - loginTimes[0] > 61000) loginTimes.shift()
    if (loginTimes.length < LOGINS_PER_MINUTE) break
    await new Promise((resolve) => setTimeout(resolve, 61000 - (now - loginTimes[0])))
  }
  loginTimes.push(Date.now())
}

async function loginStatus(username, password) {
  await pace()
  const response = await request(`${API}/api/v1/auth/login`, {
    method: 'POST',
    body: { tenant_code: TENANT, username, password },
  })
  return { status: response.status, token: response.ok ? (await response.json()).access_token : null }
}

async function login(username, password = PASSWORD) {
  const { status, token } = await loginStatus(username, password)
  if (status !== 200) throw new Error(`login ${username} -> ${status}`)
  return token
}

// ---- 1. 准备 ----

async function prepare() {
  await pace()
  const platform = await json(`${API}/platform/v1/auth/login`, {
    method: 'POST',
    body: { username: PLATFORM_USER, password: PLATFORM_PASSWORD },
  })
  state.ops = platform.access_token
  const tenant = await json(`${API}/platform/v1/tenants`, {
    method: 'POST',
    token: state.ops,
    body: {
      code: TENANT,
      name: `重置密码验收 ${RUN}`,
      admin: { username: 'admin', display_name: '张总', password: PASSWORD },
    },
  })
  state.tenantId = tenant.id
  state.admin = await login('admin')
  for (const [code, name, permissions] of [
    ['hr', '人事', ['staff:read', 'staff:manage']],
    ['visitor_role', '访客', ['staff:read']],
  ]) {
    await json(`${API}/api/v1/roles`, {
      method: 'POST',
      token: state.admin,
      body: { code, name, permissions },
    })
  }
  for (const [username, displayName, roles] of [
    ...MEMBERS,
    ['hrm', '人事小何', ['hr']],
    ['vic', '访客小薇', ['visitor_role']],
  ]) {
    await json(`${API}/api/v1/staff`, {
      method: 'POST',
      token: state.admin,
      body: { username, display_name: displayName, password: PASSWORD, role_codes: roles },
    })
  }
}

// ---- 浏览器 ----

function watchErrors(page, label) {
  page.on('console', (m) => {
    if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) {
      summary.consoleErrors.push(`${label}: ${m.text()}`)
    }
  })
  page.on('pageerror', (e) => summary.consoleErrors.push(`${label} pageerror: ${e.message}`))
}

async function newPage(browser, label) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' })
  state.contexts.push(ctx)
  const page = await ctx.newPage()
  state.pages.push(page)
  watchErrors(page, label)
  return page
}

/** 控制台登录；要先设置新密码时停在"设置新密码"页面。 */
async function consoleLogin(browser, username, password = PASSWORD, { setup = false } = {}) {
  const page = await newPage(browser, username)
  await pace()
  await page.goto(`${CONSOLE}/login`)
  await page.fill('input[placeholder="例如 demo"]', TENANT)
  await page.fill('input[autocomplete="username"]', username)
  await page.fill('input[autocomplete="current-password"]', password)
  await page.click('button:has-text("登录")')
  await page.waitForSelector(setup ? '[data-testid="password-setup"]' : '[data-testid="main-menu"]')
  return page
}

// 截图前等对话框、提示的过渡动画结束（与 p24 相同）。
const settle = (page) => page.waitForTimeout(600)

// 截图前等前一步的消息提示消失。
async function quiet(page) {
  await page.locator('.el-message').first().waitFor({ state: 'detached', timeout: 8000 }).catch(() => undefined)
}

// 员工页面是可以上下左右滚动的员工导图（10 位员工时比屏幕宽）：截图前页面和菜单滚回顶部，导图以企业卡片为中心，
// 企业、两位管理员和中间几位员工的卡片完整地出现在画面里。
async function frameDiagram(page) {
  await page.evaluate(() => {
    const viewport = document.querySelector('.tree-viewport')
    for (const element of document.querySelectorAll('*')) {
      if (element !== viewport && element.scrollTop > 0) element.scrollTop = 0
    }
    const company = document.querySelector('[data-testid="staff-node-company"]')
    if (!viewport || !company) return
    const v = viewport.getBoundingClientRect()
    const c = company.getBoundingClientRect()
    viewport.scrollTo({ top: 0, left: viewport.scrollLeft + c.left + c.width / 2 - (v.left + v.width / 2) })
  })
}

// 员工页面的截图用 1600×1000 的窗口：导图（高约 730 像素）在 75% 高度的滚动区域里放得下。
const STAFF_VIEWPORT = { width: 1600, height: 1000 }

async function menu(page, title) {
  await page.locator('[data-testid="main-menu"]').getByText(title, { exact: true }).click()
}

/** 员工卡片上的管理按钮：重置密码、停用/启用、删除卡片。 */
function manageButtons(page, username) {
  return {
    reset: page.locator(`[data-testid="reset-${username}"]`),
    toggle: page.locator(`[data-testid="toggle-${username}"]`),
    remove: page.locator(`[data-testid="staff-node-${username}"] [data-testid^="delete-card-"]`),
  }
}

/** 这些按钮各自是否置灰。 */
async function disabledButtons(page, username) {
  const states = {}
  for (const [name, button] of Object.entries(manageButtons(page, username))) states[name] = await button.isDisabled()
  return states
}

/** 在"员工"页面为一个员工重置密码，返回自动生成的密码（手动设置时为空）。 */
async function resetInUi(page, username, { manual, mustChange = true } = {}) {
  await page.locator(`[data-testid="reset-${username}"]`).click()
  const dialog = page.locator('[data-testid="staff-reset"]')
  await dialog.waitFor()
  if (manual) {
    await dialog.locator('.el-radio', { hasText: '手动设置' }).click()
    await dialog.locator('[data-testid="reset-password-input"]').fill(manual)
  }
  if (!mustChange) await dialog.locator('[data-testid="reset-must-change"]').click()
  await dialog.locator('[data-testid="reset-submit"]').click()
  await dialog.locator('[data-testid="reset-result"]').waitFor()
  const password = manual
    ? null
    : (await dialog.locator('[data-testid="reset-result-password"]').innerText()).trim()
  return { dialog, password }
}

// ---- 2. 企业为全部角色的员工重置密码 ----

async function staffPage(browser) {
  // 工人先打开控制台：重置后这个页面在下一次请求时回到登录页。
  const worker = await consoleLogin(browser, 'wang')

  const page = await consoleLogin(browser, 'admin')
  state.adminPage = page
  await page.setViewportSize(STAFF_VIEWPORT)
  await menu(page, '员工')
  await page.locator('[data-testid="staff-node-admin"]').waitFor()
  const others = [...MEMBERS.map(([u]) => u), 'hrm', 'vic']
  const states = {}
  for (const username of others) states[username] = await disabledButtons(page, username)
  check(
    'every staff member of every role has enabled 重置密码, 停用 and delete; your own card has 修改密码 and cannot be deleted',
    Object.values(states).every((s) => !s.reset && !s.toggle && !s.remove) &&
      (await page.locator('[data-testid="password-admin"]').isVisible()) &&
      (await page.locator('[data-testid="reset-admin"]').count()) === 0 &&
      (await manageButtons(page, 'admin').remove.isDisabled()),
    states,
  )

  // 小艾：自动生成，下次登录必须修改。
  const { dialog, password } = await resetInUi(page, 'alice')
  state.temporary.alice = password
  check(
    'the generated password is shown once, 12 readable characters, with a copy button',
    /^[A-HJ-NP-Za-km-z2-9]{12}$/.test(password) &&
      (await dialog.locator('[data-testid="reset-copy"]').isVisible()),
    password,
  )
  await frameDiagram(page)
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/p27-01-reset-generated.png` })
  await dialog.locator('[data-testid="reset-done"]').click()
  await page.locator('[data-testid="must-change-alice"]').waitFor()
  check('the list marks alice 待改密码', await page.locator('[data-testid="must-change-alice"]').isVisible())

  // 其他角色：逐个重置。
  for (const [username] of MEMBERS.slice(1)) {
    const result = await resetInUi(page, username)
    state.temporary[username] = result.password
    await result.dialog.locator('[data-testid="reset-done"]').click()
    await page.locator(`[data-testid="must-change-${username}"]`).waitFor()
  }
  const outcomes = {}
  for (const [username] of MEMBERS) {
    const fresh = await loginStatus(username, state.temporary[username])
    const me = fresh.token ? await json(`${API}/api/v1/me`, { token: fresh.token }) : null
    const blocked = fresh.token ? await request(`${API}/api/v1/tasks/counts`, { token: fresh.token }) : null
    state.tokens = { ...state.tokens, [username]: fresh.token }
    outcomes[username] = { fresh: fresh.status, mustChange: me?.must_change_password, blocked: blocked?.status }
  }
  check(
    'all seven system roles (客服、主管、财务、出纳、工厂工人、仓管、企业所有者) log in with the new password and must set their own first',
    Object.values(outcomes).every((o) => o.fresh === 200 && o.mustChange === true && o.blocked === 403),
    outcomes,
  )
  check('the old password no longer works', (await loginStatus('alice', PASSWORD)).status === 401)

  // 工人已经打开的页面：之前的访问令牌立即失效（不用等过期），打开别的页面时回到登录页。
  await worker.locator('[data-testid="main-menu"]').getByText('个人待办', { exact: true }).click()
  await worker.waitForURL(/\/login/, { timeout: 15000 })
  check('the worker\'s open console goes back to the login page at once', worker.url().includes('/login'), worker.url())

  // 人事：手动设置，不要求修改。
  const manual = await resetInUi(page, 'hrm', { manual: HR_PASSWORD, mustChange: false })
  check(
    'a manual password shows no generated password',
    (await manual.dialog.locator('[data-testid="reset-result-password"]').count()) === 0,
  )
  await manual.dialog.locator('[data-testid="reset-done"]').click()
  await frameDiagram(page)
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/p27-02-staff-list.png` })

  // 自己的密码不能在这里重置（接口也拒绝）。
  const own = await request(`${API}/api/v1/staff/${(await json(`${API}/api/v1/me`, { token: state.admin })).id}/password`, {
    method: 'POST',
    token: state.admin,
    body: {},
  })
  check('the API refuses to reset your own password', own.status === 422, own.status)

  // 副管理员先设置自己的新密码（后面要看平台重置的提醒）。
  await json(`${API}/api/v1/me/password`, {
    method: 'POST',
    token: state.tokens.root2,
    body: { current_password: state.temporary.root2, new_password: PASSWORD + '-r2' },
  })
}

// ---- 3. 人事只能重置权限不高于自己的员工 ----

async function hrPage(browser) {
  // 手动设置、不要求修改的密码：直接进入控制台。
  const page = await consoleLogin(browser, 'hrm', HR_PASSWORD)
  check('人事 logs in with the manual password without a forced change', page.url().includes('/password') === false)
  await page.setViewportSize(STAFF_VIEWPORT)
  await menu(page, '员工')
  await page.locator('[data-testid="staff-node-hrm"]').waitFor()
  const states = {}
  for (const username of ['admin', 'root2', 'alice', 'vic']) states[username] = await disabledButtons(page, username)
  const all = (s) => s.reset && s.toggle && s.remove
  const none = (s) => !s.reset && !s.toggle && !s.remove
  check(
    '人事 cannot reset, disable or delete staff with more permissions (管理员、客服) but can for 访客',
    all(states.admin) && all(states.root2) && all(states.alice) && none(states.vic),
    states,
  )
  check("人事's own card cannot be deleted", await manageButtons(page, 'hrm').remove.isDisabled())

  // 置灰的按钮外面那一层显示提示。直接把鼠标移过去（hover() 可能为了让按钮完整出现而横向滚动导图）。
  await frameDiagram(page)
  const tooltip = async (button, text) => {
    const box = await button.locator('..').boundingBox()
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
    return page
      .locator('.el-popper:visible', { hasText: text })
      .waitFor({ timeout: 5000 })
      .then(() => true, () => false)
  }
  const owner = manageButtons(page, 'admin')
  const tips = {
    remove: await tooltip(owner.remove, '权限高于你，请让管理员删除'),
    reset: await tooltip(owner.reset, '权限高于你，请让管理员重置'),
    toggle: await tooltip(owner.toggle, '权限高于你，请让管理员停用'),
  }
  check('tooltips explain why (重置、停用、删除)', Object.values(tips).every(Boolean), tips)
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/p27-03-hr-view.png` })
}

// ---- 4. 小艾登录后先设置新密码 ----

async function forcedChange(browser) {
  const page = await consoleLogin(browser, 'alice', state.temporary.alice, { setup: true })
  const notice = await page.locator('[data-testid="password-setup-notice"]').innerText()
  check(
    'alice lands on 设置新密码, which says who reset the password',
    page.url().includes('/password') && notice.includes('管理员 张总 于') && notice.includes('重置了你的密码'),
    notice,
  )
  await page.goto(`${CONSOLE}/customers`)
  await page.locator('[data-testid="password-setup"]').waitFor()
  check(
    'other pages lead back to 设置新密码 and the menu stays hidden',
    page.url().includes('/password') && (await page.locator('[data-testid="main-menu"]').count()) === 0,
    page.url(),
  )
  const blocked = await request(`${API}/api/v1/customers`, { token: state.tokens.alice })
  const body = await blocked.json()
  check(
    'the API refuses other requests with password_change_required',
    blocked.status === 403 && body.error.code === 'password_change_required',
    { status: blocked.status, body },
  )
  await page.locator('[data-testid="password-setup-current"]').fill(state.temporary.alice)
  await page.locator('[data-testid="password-setup-new"]').fill(state.temporary.alice)
  await page.locator('[data-testid="password-setup-confirm"]').fill(state.temporary.alice)
  await page.locator('[data-testid="password-setup-save"]').click()
  await page.locator('.el-message--warning', { hasText: '新密码不能与当前密码相同' }).last().waitFor()
  await page.locator('[data-testid="password-setup-new"]').fill('alice-own-2026')
  await page.locator('[data-testid="password-setup-confirm"]').fill('alice-own-2026')
  await quiet(page)
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/p27-04-password-setup.png` })
  await page.locator('[data-testid="password-setup-save"]').click()
  await page.locator('[data-testid="main-menu"]').waitFor()
  await menu(page, '客户')
  await page.locator('[data-testid="customer-table"]').waitFor()
  const own = await loginStatus('alice', 'alice-own-2026')
  const earlier = await request(`${API}/api/v1/me`, { token: state.tokens.alice })
  check(
    'after setting her own password alice uses the console; the login with the temporary one is gone',
    own.status === 200 && earlier.status === 401,
    { own: own.status, earlier: earlier.status },
  )
}

// ---- 5. 运营后台为企业拥有者重置密码 ----

async function platformReset(browser) {
  const ops = await newPage(browser, 'ops')
  await pace()
  await ops.goto(`${PLATFORM}/login`)
  await ops.fill('input[autocomplete="username"]', PLATFORM_USER)
  await ops.fill('input[autocomplete="current-password"]', PLATFORM_PASSWORD)
  await ops.click('button:has-text("登录")')
  const row = ops.locator('[data-testid="tenant-table"] .el-table__row', { hasText: TENANT })
  await row.waitFor()
  await row.locator('[data-testid="tenant-detail-button"]').click()
  await ops.locator('.el-tabs__item', { hasText: '管理员账号' }).click()
  const table = ops.locator('[data-testid="tenant-admins-table"]')
  await table.locator('.el-table__row').first().waitFor()
  const rows = await table.locator('.el-table__row').allInnerTexts()
  check(
    'the 管理员账号 tab lists the owner first (拥有者) and the other admin',
    rows.length === 2 && rows[0].includes('admin') && rows[0].includes('拥有者') && rows[1].includes('root2'),
    rows,
  )
  await settle(ops)
  await ops.screenshot({ path: `${SHOTS}/p27-05-platform-admins.png` })

  await ops.locator('[data-testid="admin-reset-admin"]').click()
  const dialog = ops.locator('[data-testid="admin-reset-dialog"]')
  await dialog.waitFor()
  await dialog.locator('[data-testid="admin-reset-submit"]').click()
  await ops.locator('.el-message--warning', { hasText: '请填写重置的原因' }).last().waitFor()
  await dialog.locator('[data-testid="admin-reset-reason"]').fill(REASON)
  await dialog.locator('[data-testid="admin-reset-submit"]').click()
  await dialog.locator('[data-testid="admin-reset-result"]').waitFor()
  const resultText = await dialog.innerText()
  state.ownerTemporary = (await dialog.locator('[data-testid="admin-reset-password"]').innerText()).trim()
  check(
    'the reset shows the temporary password once with the tenant code and username',
    /^[A-HJ-NP-Za-km-z2-9]{12}$/.test(state.ownerTemporary) &&
      resultText.includes(TENANT) &&
      resultText.includes('admin'),
    resultText,
  )
  await quiet(ops)
  await settle(ops)
  await ops.screenshot({ path: `${SHOTS}/p27-06-platform-reset.png` })
  await dialog.locator('[data-testid="admin-reset-done"]').click()
  await table.locator('.el-table__row', { hasText: '待改密码' }).waitFor()
  check('the owner row now shows 待改密码', (await table.locator('.el-table__row').first().innerText()).includes('待改密码'))
}

// ---- 6. 留痕、提醒和拥有者设置新密码 ----

async function afterPlatformReset(browser) {
  check(
    'the owner\'s earlier login stops working at once',
    (await request(`${API}/api/v1/me`, { token: state.admin })).status === 401,
  )

  const root2 = await consoleLogin(browser, 'root2', PASSWORD + '-r2')
  await root2.locator('[data-testid="notification-bell"]').click()
  const notice = root2.locator('[data-testid="notification"]', { hasText: '重置了管理员' })
  await notice.waitFor()
  const noticeText = await notice.innerText()
  check(
    'the other admin is told the platform reset the owner\'s password, with the reason',
    noticeText.includes('平台运维人员重置了管理员 张总（admin）的密码') && noticeText.includes(REASON),
    noticeText,
  )
  await settle(root2)
  await root2.screenshot({ path: `${SHOTS}/p27-07-admin-notice.png` })
  await root2.keyboard.press('Escape')
  await root2.mouse.click(700, 600)
  await menu(root2, '操作日志')
  await root2.locator('.el-tabs__item', { hasText: '系统日志' }).click()
  const auditRow = root2.locator('[data-testid="audit-table"] .el-table__row', { hasText: '平台运维' }).first()
  await auditRow.waitFor()
  const auditText = await auditRow.innerText()
  check(
    'the tenant audit log shows 重置员工密码 by 平台运维 with the reason',
    auditText.includes('重置员工密码') && auditText.includes(REASON),
    auditText,
  )
  await settle(root2)
  await root2.screenshot({ path: `${SHOTS}/p27-08-audit.png` })

  const owner = await consoleLogin(browser, 'admin', state.ownerTemporary, { setup: true })
  const setupNotice = await owner.locator('[data-testid="password-setup-notice"]').innerText()
  check(
    'the owner sees that the platform reset the password and why',
    setupNotice.includes('平台运维人员于') && setupNotice.includes(`原因：${REASON}`),
    setupNotice,
  )
  await owner.locator('[data-testid="password-setup-current"]').fill(state.ownerTemporary)
  await owner.locator('[data-testid="password-setup-new"]').fill('owner-own-2026')
  await owner.locator('[data-testid="password-setup-confirm"]').fill('owner-own-2026')
  await settle(owner)
  await owner.screenshot({ path: `${SHOTS}/p27-09-owner-setup.png` })
  await owner.locator('[data-testid="password-setup-save"]').click()
  await owner.locator('[data-testid="main-menu"]').waitFor()
  const ownerMe = await owner.evaluate(() => document.title)
  check('the owner enters the console after setting a new password', !owner.url().includes('/password'), ownerMe)

  const admins = await json(`${API}/platform/v1/tenants/${state.tenantId}/admins`, { token: state.ops })
  const ownerRow = admins.items.find((a) => a.owner)
  check(
    'the platform list shows the owner has set a new password',
    ownerRow.must_change_password === false && ownerRow.password_changed_at !== null,
    ownerRow,
  )
  const logs = await json(
    `${API}/platform/v1/audit-logs?tenant_id=${state.tenantId}&action=staff.reset_password&actor_type=platform`,
    { token: state.ops },
  )
  check(
    'the platform audit log records the reset with the operator',
    logs.items.length === 1 && logs.items[0].actor_name && logs.items[0].detail.reason === REASON,
    logs.items,
  )
}

;(async () => {
  if (!PLATFORM_PASSWORD) {
    console.error('请通过环境变量 PLATFORM_PASSWORD 提供平台运营账号的密码')
    process.exit(2)
  }
  fs.mkdirSync(SHOTS, { recursive: true })
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined })
  try {
    await prepare()
    await staffPage(browser)
    await hrPage(browser)
    await forcedChange(browser)
    await platformReset(browser)
    await afterPlatformReset(browser)
  } catch (error) {
    summary.checks.push(`FAIL exception -> ${error.stack || error}`)
    for (const [i, page] of state.pages.entries()) {
      await page.screenshot({ path: `${SHOTS}/failure-${i + 1}.png` }).catch(() => undefined)
    }
  } finally {
    for (const ctx of state.contexts) await ctx.close().catch(() => undefined)
    await browser.close()
  }
  check('no console errors', summary.consoleErrors.length === 0, summary.consoleErrors)
  fs.writeFileSync(`${SHOTS}/summary.json`, JSON.stringify(summary, null, 2))
  for (const line of summary.checks) console.log(line)
  const failed = summary.checks.filter((line) => line.startsWith('FAIL'))
  console.log(failed.length ? `${failed.length} FAILED` : `ALL ${summary.checks.length} PASSED`)
  process.exit(failed.length ? 1 : 0)
})()
