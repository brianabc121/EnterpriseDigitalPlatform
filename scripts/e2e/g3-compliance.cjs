// 权限、合规与审计的浏览器验收（G3）：
//
// 1. 员工：管理员编辑坐席的姓名和角色；停用后坐席无法登录，启用后恢复；重置密码后只能用新密码登录。
// 2. 角色：新建自定义角色"质检"，按分组勾选权限。
// 3. 客户敏感信息：新建带手机号、邮箱、公司的客户，列表只显示掩码；按完整手机号搜索命中、部分号码不命中；
//    客户资料里查看完整联系方式。
// 4. 导出：再次输入密码后导出 CSV，包含完整手机号，以等号开头的单元格加了单引号。
// 5. 合并：新建重复的客户后合并，只剩一个客户，标签合并。
// 6. 个人信息请求：生成个人信息副本（JSON），再删除客户；请求记录里有查询和删除两条。
// 7. 保留期：设置消息保留 180 天、文件 30 天。
// 8. 操作日志：按"客户"筛选看到查看联系方式、导出、合并、个人信息删除。
// 9. 修改自己的密码后用新密码登录。
// 10. 病毒扫描：访客发起咨询，坐席在工作台发送含 EICAR 测试串的文件；扫描后工作台显示"已被拦截"，下载返回 410。
// 11. 运营后台轮换租户数据密钥，客户的联系方式仍能正常查看。
//
// 前置：同 M4（后端、实时消费进程、调度进程、控制台、Widget、OpenIM、MinIO），另外运行模拟 clamd
// （cd backend && uv run python -m tests.fake_clamd --port 3310），后端和调度进程设置 EDP_CLAMAV_HOST=127.0.0.1。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/g3-compliance.cjs
const { chromium } = require('playwright')
const fs = require('fs')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PLATFORM = env('PLATFORM_URL', 'http://localhost:5174')
const WIDGET = env('WIDGET_URL', 'http://localhost:5175')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/g3')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `g3-${RUN}`
const ADMIN_PASSWORD = 'admin-demo-2026'
const NEW_ADMIN_PASSWORD = 'admin-new-2026'
const AGENT_PASSWORD = 'agent-demo-2026'
const RESET_PASSWORD = 'agent-reset-2026'
const PHONE = '13912345678'
const EICAR = String.raw`X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*`

const summary = { tenant: TENANT, checks: [], consoleErrors: [] }
const check = (name, ok, detail) =>
  summary.checks.push(
    `${ok ? 'PASS' : 'FAIL'} ${name}${ok || detail === undefined ? '' : ` -> ${JSON.stringify(detail)}`}`,
  )

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

async function json(url, options = {}) {
  const response = await request(url, options)
  const text = await response.text()
  if (!response.ok) throw new Error(`${options.method ?? 'GET'} ${url} -> ${response.status} ${text}`)
  return text ? JSON.parse(text) : null
}

async function staffLogin(username, password) {
  return request(`${API}/api/v1/auth/login`, {
    method: 'POST',
    body: { tenant_code: TENANT, username, password },
  })
}

async function waitFor(fn, timeout = 20000) {
  const deadline = Date.now() + timeout
  for (;;) {
    const value = await fn()
    if (value || Date.now() > deadline) return value
    await new Promise((r) => setTimeout(r, 1000))
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

async function newPage(browser, label, viewport = { width: 1440, height: 900 }) {
  const context = await browser.newContext({ viewport, locale: 'zh-CN', acceptDownloads: true })
  const page = await context.newPage()
  watchErrors(page, label)
  return page
}

const shot = (page, name) => page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true })

async function consoleLogin(page, username, password) {
  await page.goto(`${CONSOLE}/login`)
  await page.fill('input[placeholder="例如 demo"]', TENANT)
  await page.fill('input[autocomplete="username"]', username)
  await page.fill('input[autocomplete="current-password"]', password)
  await page.click('button:has-text("登录")')
  await page.locator('[data-testid="main-menu"]').waitFor()
}

async function menu(page, title) {
  await page.locator('[data-testid="main-menu"] .el-menu-item', { hasText: title }).click()
}

async function confirmBox(page, text) {
  const box = page.locator('.el-message-box:visible')
  await box.waitFor()
  await box.locator('.el-message-box__btns button', { hasText: text }).click()
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
      name: `合规验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: ADMIN_PASSWORD },
    },
  })
  const admin = await (await staffLogin('admin', ADMIN_PASSWORD)).json()
  for (const [username, name] of [
    ['xiaowang', '小王'],
    ['xiaoai', '小爱'],
  ]) {
    await json(`${API}/api/v1/staff`, {
      method: 'POST',
      token: admin.access_token,
      body: { username, display_name: name, password: AGENT_PASSWORD, role_codes: ['agent'] },
    })
  }
  const channels = await json(`${API}/api/v1/channels`, { token: admin.access_token })
  return { adminToken: admin.access_token, channelKey: channels.items[0].public_key }
}

async function staffSection(page) {
  await menu(page, '员工')
  const row = page.locator('[data-testid="staff-table"] .el-table__row', { hasText: 'xiaowang' })
  await row.waitFor()
  await row.locator('button', { hasText: '编辑' }).click()
  const dialog = page.locator('.el-dialog:visible')
  await dialog.locator('.el-form-item', { hasText: '姓名' }).locator('input').fill('王小明')
  await dialog.locator('.el-checkbox', { hasText: '主管' }).click()
  await dialog.locator('button', { hasText: '保存' }).click()
  await row.locator('.el-tag', { hasText: '主管' }).waitFor()
  const rowText = await row.innerText()
  check('编辑员工的姓名和角色', rowText.includes('王小明') && rowText.includes('坐席'), rowText)

  await page.locator('[data-testid="toggle-xiaowang"]').click()
  await confirmBox(page, '停用')
  await row.locator('.el-tag', { hasText: '停用' }).waitFor()
  const blocked = await staffLogin('xiaowang', AGENT_PASSWORD)
  check('停用后员工无法登录', blocked.status === 403, blocked.status)
  await shot(page, '1-staff-disabled')
  await page.locator('[data-testid="toggle-xiaowang"]').click()
  await row.locator('.el-tag', { hasText: '启用' }).waitFor()
  check('重新启用后可以登录', (await staffLogin('xiaowang', AGENT_PASSWORD)).status === 200)

  await row.locator('button', { hasText: '重置密码' }).click()
  await page.locator('.el-dialog:visible input[type="password"]').fill(RESET_PASSWORD)
  await page.locator('.el-dialog:visible button', { hasText: '重置' }).click()
  await page.locator('.el-message--success', { hasText: '已重置密码' }).last().waitFor()
  const oldLogin = await staffLogin('xiaowang', AGENT_PASSWORD)
  const newLogin = await staffLogin('xiaowang', RESET_PASSWORD)
  check('重置密码后只能用新密码登录', oldLogin.status === 401 && newLogin.status === 200, {
    old: oldLogin.status,
    new: newLogin.status,
  })

  await page.locator('.el-tabs__item', { hasText: '角色' }).click()
  await page.locator('[data-testid="new-role"]').click()
  const roleDialog = page.locator('.el-dialog:visible')
  await roleDialog.locator('.el-form-item', { hasText: '代码' }).locator('input').fill('quality')
  await roleDialog.locator('.el-form-item', { hasText: '名称' }).locator('input').fill('质检')
  await roleDialog.locator('.el-checkbox', { hasText: '查看报表' }).click()
  await roleDialog.locator('.el-checkbox', { hasText: '查看全部会话' }).click()
  await roleDialog.locator('button', { hasText: '保存' }).click()
  const roleRow = page.locator('[data-testid="role-table"] .el-table__row', { hasText: '质检' })
  await roleRow.waitFor()
  const roleText = await roleRow.innerText()
  check(
    '新建自定义角色并显示权限名称',
    roleText.includes('查看报表') && roleText.includes('查看全部会话'),
    roleText,
  )
  await shot(page, '2-roles')
}

async function newCustomer(page, fields) {
  await page.locator('button', { hasText: '新建客户' }).click()
  const dialog = page.locator('.el-dialog:visible')
  for (const [label, value] of Object.entries(fields)) {
    await dialog.locator('.el-form-item', { hasText: label }).locator('input').fill(value)
  }
  await dialog.locator('button', { hasText: '保存' }).click()
  await dialog.waitFor({ state: 'hidden' })
}

async function search(page, q) {
  await page.fill('input[data-testid="customer-search"]', q)
  await page.press('input[data-testid="customer-search"]', 'Enter')
  await page.waitForTimeout(800)
  return page.locator('[data-testid="customer-table"] .el-table__row').allInnerTexts()
}

/** 名称完全一致的客户行（"张先生"不匹配"张先生（重复）"）。 */
function customerRow(page, name) {
  return page
    .locator('[data-testid="customer-table"] .el-table__row')
    .filter({ has: page.getByRole('button', { name, exact: true }) })
}

async function rowAction(page, name, action) {
  const row = customerRow(page, name)
  await row.locator('button', { hasText: '更多' }).click()
  await page.locator('.el-dropdown-menu__item:visible', { hasText: action }).click()
}

async function customerSection(page, adminToken) {
  await menu(page, '客户')
  await newCustomer(page, {
    客户名称: '张先生',
    手机号: '139 1234 5678',
    邮箱: 'Zhang@Example.com',
    公司: '=HYPERLINK("x")',
  })
  const row = customerRow(page, '张先生')
  await row.waitFor()
  const rowText = await row.innerText()
  check(
    '客户列表只显示手机号和邮箱的掩码',
    rowText.includes('139****5678') && rowText.includes('z***@example.com') && !rowText.includes(PHONE),
    rowText,
  )
  const byPhone = await search(page, PHONE)
  const byPartial = await search(page, '1391234')
  check(
    '按完整手机号可以搜到，部分号码搜不到',
    byPhone.length === 1 && byPhone[0].includes('张先生') && byPartial.length === 0,
    { byPhone, byPartial },
  )
  await search(page, '')

  await row.locator('button', { hasText: '张先生' }).click()
  const drawer = page.locator('.el-drawer:visible')
  await drawer.locator('[data-testid="reveal-contact"]').click()
  await drawer.locator('[data-testid="contact-phone"]', { hasText: PHONE }).waitFor()
  check('客户资料里查看完整联系方式', true)
  await shot(page, '3-customer-contact')
  await drawer.locator('.el-drawer__close-btn').click()
  await drawer.waitFor({ state: 'hidden' })

  // 导出
  await page.locator('[data-testid="export-customers"]').click()
  await page.fill('input[data-testid="export-password"]', ADMIN_PASSWORD)
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.locator('[data-testid="export-confirm"]').click(),
  ])
  const csv = fs.readFileSync(await download.path(), 'utf8')
  check(
    '输入密码后导出 CSV，包含完整手机号，公式单元格加了单引号',
    csv.includes(PHONE) && csv.includes(`"'=HYPERLINK(""x"")"`),
    csv.slice(0, 300),
  )

  // 合并
  await newCustomer(page, { 客户名称: '张先生（重复）', 邮箱: 'zhang2@example.com' })
  const [duplicate] = (await json(`${API}/api/v1/customers?q=${encodeURIComponent('重复')}`, {
    token: adminToken,
  })).items
  await json(`${API}/api/v1/customers/${duplicate.id}`, {
    method: 'PATCH',
    token: adminToken,
    body: { tags: ['老客户'] },
  })
  await page.reload()
  await rowAction(page, '张先生', '合并重复客户')
  const mergeDialog = page.locator('.el-dialog:visible')
  await mergeDialog.locator('.el-checkbox', { hasText: '张先生（重复）' }).click()
  await page.locator('[data-testid="merge-confirm"]').click()
  await mergeDialog.waitFor({ state: 'hidden' })
  const afterMerge = await json(`${API}/api/v1/customers`, { token: adminToken })
  check(
    '合并后只剩一个客户，标签合并',
    afterMerge.total === 1 && afterMerge.items[0].tags.includes('老客户'),
    afterMerge.items,
  )
  await shot(page, '4-customers-merged')

  // 个人信息查询与删除
  await rowAction(page, '张先生', '个人信息请求')
  await page.fill('input[data-testid="privacy-reason"]', '客户来电要求查询')
  const [copy] = await Promise.all([
    page.waitForEvent('download'),
    page.locator('[data-testid="privacy-access"]').click(),
  ])
  const document = JSON.parse(fs.readFileSync(await copy.path(), 'utf8'))
  check(
    '生成个人信息副本（含完整手机号）',
    document.customer.phone === PHONE && document.customer.display_name === '张先生',
    document.customer,
  )
  await rowAction(page, '张先生', '个人信息请求')
  const privacy = page.locator('.el-dialog:visible')
  await privacy.locator('.el-radio-button', { hasText: '删除' }).click()
  await page.fill('input[data-testid="privacy-reason"]', '客户要求删除')
  await page.fill('input[data-testid="privacy-confirm-name"]', '张先生')
  await page.locator('[data-testid="privacy-erase"]').click()
  await confirmBox(page, '确认删除')
  await privacy.waitFor({ state: 'hidden' })
  const afterErase = await json(`${API}/api/v1/customers`, { token: adminToken })
  await page.locator('button', { hasText: '个人信息请求' }).first().click()
  const requests = page.locator('[data-testid="privacy-requests"] .el-table__row')
  await requests.nth(1).waitFor()
  const requestTexts = await requests.allInnerTexts()
  check(
    '删除客户后记录里有查询和删除两条（名称已掩码）',
    afterErase.total === 0 &&
      requestTexts.length === 2 &&
      requestTexts[0].includes('删除') &&
      requestTexts[1].includes('查询') &&
      requestTexts.every((t) => t.includes('张**')),
    { total: afterErase.total, requestTexts },
  )
  await shot(page, '5-privacy-requests')
  await page.locator('.el-drawer:visible .el-drawer__close-btn').click()
}

async function settingsSection(page, adminToken) {
  await menu(page, '设置')
  await page.locator('[data-testid="settings-tabs"] .el-tabs__item', { hasText: '数据保留' }).click()
  await page.locator('[data-testid="retention-messages"] input').fill('180')
  await page.locator('[data-testid="retention-files"] input').fill('30')
  await page.locator('[data-testid="retention-save"]').click()
  await page.locator('.el-message-box:visible .el-message-box__btns .el-button--primary').click()
  await page.locator('.el-message--success', { hasText: '已保存保留期' }).last().waitFor()
  const policy = await json(`${API}/api/v1/tenant/retention`, { token: adminToken })
  check(
    '设置消息保留 180 天、文件 30 天',
    policy.messages_days === 180 && policy.files_days === 30,
    policy,
  )
  await shot(page, '6-retention')

  await menu(page, '操作日志')
  await page.locator('[data-testid="audit-group"]').click()
  await page.locator('.el-select-dropdown__item:visible', { hasText: '客户' }).click()
  await page.waitForTimeout(1000)
  const auditText = await page.locator('[data-testid="audit-table"]').innerText()
  const expected = ['查看客户联系方式', '导出客户', '合并客户', '个人信息查询', '个人信息删除']
  check(
    '操作日志按"客户"筛选看到敏感操作',
    expected.every((a) => auditText.includes(a)),
    expected.filter((a) => !auditText.includes(a)),
  )
  await shot(page, '7-audit')
}

async function passwordSection(page) {
  await page.locator('[data-testid="user-menu"]').hover()
  await page.locator('.el-dropdown-menu__item:visible', { hasText: '修改密码' }).click()
  const form = page.locator('[data-testid="password-form"]')
  await form.locator('.el-form-item', { hasText: '当前密码' }).locator('input').fill(ADMIN_PASSWORD)
  await form.locator('.el-form-item', { hasText: /^新密码/ }).locator('input').fill(NEW_ADMIN_PASSWORD)
  await form.locator('.el-form-item', { hasText: '确认新密码' }).locator('input').fill(NEW_ADMIN_PASSWORD)
  await page.locator('.el-dialog:visible button', { hasText: '保存' }).click()
  await page.locator('.el-message--success', { hasText: '密码已修改' }).last().waitFor()
  // 当前页面换用了新令牌，仍然可以继续操作。
  await menu(page, '客户')
  await page.locator('[data-testid="customer-table"]').waitFor()
  const oldLogin = await staffLogin('admin', ADMIN_PASSWORD)
  const newLogin = await staffLogin('admin', NEW_ADMIN_PASSWORD)
  check('修改自己的密码后只能用新密码登录', oldLogin.status === 401 && newLogin.status === 200, {
    old: oldLogin.status,
    new: newLogin.status,
  })
  return (await newLogin.json()).access_token
}

async function virusSection(browser, adminToken, channelKey) {
  const agent = await newPage(browser, 'workbench')
  await consoleLogin(agent, 'xiaoai', AGENT_PASSWORD)
  await menu(agent, '工作台')
  await agent.locator('[data-testid="im-state"]', { hasText: '已连接' }).waitFor({ timeout: 20000 })
  await agent.locator('[data-testid="agent-status"]', { hasText: '在线' }).waitFor({ timeout: 15000 })

  const visitor = await newPage(browser, 'widget', { width: 420, height: 720 })
  await visitor.goto(`${WIDGET}/?key=${encodeURIComponent(channelKey)}`)
  await visitor.locator('[data-testid="widget-state"]', { hasText: '在线' }).waitFor()
  await visitor.fill('[data-testid="message-input"]', '请把报价单发给我')
  await visitor.click('[data-testid="send-button"]')
  await agent.locator('[data-testid="session-item"]').first().waitFor({ timeout: 20000 })
  await agent.locator('[data-testid="session-item"]').first().click()
  await agent.locator('[data-testid="chat-message"]', { hasText: '请把报价单发给我' }).waitFor()

  await agent.setInputFiles('input[data-testid="attach-input"]', {
    name: '报价单.pdf',
    mimeType: 'application/pdf',
    buffer: Buffer.from(EICAR),
  })
  const link = visitor.locator('[data-testid="message-file"]', { hasText: '报价单.pdf' })
  await link.waitFor({ timeout: 20000 })
  const href = await link.getAttribute('href')

  // 调度进程每分钟扫描一次新附件。
  const rooms = await json(`${API}/api/v1/rooms`, { token: adminToken })
  const scanned = await waitFor(async () => {
    const page = await json(`${API}/api/v1/rooms/${rooms.items[0].id}/messages`, { token: adminToken })
    const file = page.items.find((m) => m.content_type === 'file')
    return file && file.content.scan ? file : null
  }, 150000)
  check(
    '含 EICAR 测试串的文件被扫描为感染并拦截',
    scanned && scanned.content.scan === 'infected' && scanned.content.blocked === true && !scanned.content.url,
    scanned && scanned.content,
  )
  const download = await fetch(href, { redirect: 'manual' })
  check('被拦截文件的下载链接返回 410', download.status === 410, download.status)

  await agent.reload()
  await agent.locator('[data-testid="session-item"]').first().click()
  const removed = agent.locator('[data-testid="message-removed"]', { hasText: '已被拦截' })
  const shown = await removed
    .waitFor({ timeout: 15000 })
    .then(() => true)
    .catch(() => false)
  check('工作台里显示"文件含有病毒，已被拦截"', shown)
  await shot(agent, '8-virus-blocked')
  await agent.context().close()
  await visitor.context().close()
}

async function keySection(browser, adminToken) {
  const customer = await json(`${API}/api/v1/customers`, {
    method: 'POST',
    token: adminToken,
    body: { display_name: '李女士', phone: '13700001111' },
  })
  const ops = await newPage(browser, 'platform')
  await ops.goto(`${PLATFORM}/login`)
  await ops.fill('input[autocomplete="username"]', PLATFORM_USER)
  await ops.fill('input[autocomplete="current-password"]', PLATFORM_PASSWORD)
  await ops.click('button:has-text("登录")')
  const row = ops.locator('[data-testid="tenant-table"] .el-table__row', { hasText: TENANT })
  await row.waitFor()
  await row.locator('[data-testid="tenant-detail-button"]').click()
  await ops.locator('.el-tabs__item', { hasText: '数据密钥' }).click()
  await ops.locator('[data-testid="tenant-keys"]').waitFor()
  await ops.locator('[data-testid="rotate-key"]').click()
  await confirmBox(ops, '轮换')
  await ops.locator('.el-message--success', { hasText: '已轮换到第 2 版' }).last().waitFor()
  const keysText = await ops.locator('[data-testid="tenant-keys"]').innerText()
  const sensitive = await json(`${API}/api/v1/customers/${customer.id}/sensitive`, {
    token: adminToken,
  })
  check(
    '运营轮换数据密钥后，旧密文全部换新，联系方式仍能查看',
    /旧版本密文\s*0/.test(keysText) && sensitive.phone === '13700001111',
    { keysText, sensitive },
  )
  await shot(ops, '9-tenant-keys')
  await ops.context().close()
}

async function run(browser) {
  const { adminToken, channelKey } = await prepareTenant()
  const page = await newPage(browser, 'console')
  await consoleLogin(page, 'admin', ADMIN_PASSWORD)
  const menus = await page.locator('[data-testid="main-menu"] .el-menu-item').allInnerTexts()
  check('管理员菜单里有"操作日志"', menus.map((m) => m.trim()).includes('操作日志'), menus)

  await staffSection(page)
  await customerSection(page, adminToken)
  await settingsSection(page, adminToken)
  const token = await passwordSection(page)
  await virusSection(browser, token, channelKey)
  await keySection(browser, token)
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
