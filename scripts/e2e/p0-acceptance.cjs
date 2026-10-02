// P0 端到端验收：在真实浏览器里走一遍"开通租户 → 管理员/坐席/其他租户登录"。
//
// 前置：make dev-up、make migrate、创建平台账号，并启动后端、控制台、运营后台（见 README）。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p0-acceptance.cjs
//   需要全局安装的 Playwright 和 Chromium（见 README）；CHROMIUM_PATH 可指定浏览器可执行文件。
//   可选环境变量：PLATFORM_USER（默认 ops）、CONSOLE_URL、PLATFORM_URL、SHOTS（截图目录）。
// 每次运行使用新的企业代码，可以在同一个数据库上重复执行；有检查失败时以非 0 退出。
const { chromium } = require('playwright')
const fs = require('fs')

const env = (name, fallback) => process.env[name] || fallback
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PLATFORM = env('PLATFORM_URL', 'http://localhost:5174')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots')
const RUN = Date.now().toString(36).slice(-5)
const TENANT_A = `demo-${RUN}`
const TENANT_B = `globex-${RUN}`
const ADMIN_PASSWORD = 'admin-demo-2026'
const AGENT_PASSWORD = 'alice-demo-2026'

const summary = { tenants: [TENANT_A, TENANT_B], menus: {}, customers: {}, failedRequests: [], consoleErrors: [], checks: [] }

function check(name, ok, detail) {
  const suffix = ok || detail === undefined ? '' : ` -> ${JSON.stringify(detail)}`
  summary.checks.push(`${ok ? 'PASS' : 'FAIL'} ${name}${suffix}`)
}

async function newPage(browser, label) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 760 }, locale: 'zh-CN' })
  const page = await ctx.newPage()
  page.on('console', (m) => {
    if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) {
      summary.consoleErrors.push(`[${label}] ${m.text()}`)
    }
  })
  page.on('pageerror', (e) => summary.consoleErrors.push(`[${label}] pageerror: ${e.message}`))
  page.on('response', (r) => {
    if (r.status() >= 400) {
      summary.failedRequests.push(`[${label}] ${r.status()} ${r.request().method()} ${new URL(r.url()).pathname}`)
    }
  })
  return { ctx, page }
}

async function shot(page, name) {
  await page.waitForTimeout(300) // 等待 Element Plus 过渡动画结束
  await page.screenshot({ path: `${SHOTS}/${name}.png` })
}

async function consoleLogin(page, tenant, username, password) {
  await page.goto(`${CONSOLE}/login`)
  await page.fill('input[placeholder="例如 demo"]', tenant)
  await page.fill('input[autocomplete="username"]', username)
  await page.fill('input[autocomplete="current-password"]', password)
  await page.click('button:has-text("登录")')
  await page.waitForSelector('[data-testid="main-menu"]')
}

async function menuTitles(page) {
  const items = await page.locator('[data-testid="main-menu"] .el-menu-item').allInnerTexts()
  return items.map((t) => t.trim())
}

async function openCustomers(page) {
  await Promise.all([
    page.waitForResponse((r) => r.url().includes('/api/v1/customers') && r.request().method() === 'GET'),
    page.locator('[data-testid="main-menu"] .el-menu-item', { hasText: '客户' }).click(),
  ])
  await page
    .waitForSelector('[data-testid="customer-table"] .el-loading-mask', { state: 'hidden' })
    .catch(() => {})
}

async function customerNames(page) {
  const cells = page.locator('[data-testid="customer-table"] .el-table__body tr td:first-child')
  return (await cells.allInnerTexts()).map((t) => t.trim())
}

async function createCustomer(page, name, ownerName) {
  await page.click('button:has-text("新建客户")')
  const dialog = page.locator('.el-dialog:visible')
  await dialog.locator('.el-form-item', { hasText: '客户名称' }).locator('input').fill(name)
  if (ownerName) {
    await dialog.locator('.el-select').click()
    await page.locator('.el-select-dropdown__item:visible', { hasText: ownerName }).click()
  }
  await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/v1/customers') && r.request().method() === 'POST'),
    dialog.locator('button:has-text("保存")').click(),
  ])
  await page.waitForSelector(`[data-testid="customer-table"] >> text=${name}`)
}

async function run(browser) {
  // 1. 运营后台：开通两个租户
  {
    const { ctx, page } = await newPage(browser, 'platform')
    await page.goto(`${PLATFORM}/login`)
    await page.fill('input[autocomplete="username"]', PLATFORM_USER)
    await page.fill('input[autocomplete="current-password"]', PLATFORM_PASSWORD)
    await page.click('button:has-text("登录")')
    await page.waitForSelector('button:has-text("开通租户")')
    for (const [code, name] of [
      [TENANT_A, `演示科技 ${RUN}`],
      [TENANT_B, `环球贸易 ${RUN}`],
    ]) {
      await page.click('button:has-text("开通租户")')
      const dialog = page.locator('.el-dialog:visible')
      await dialog.locator('.el-form-item', { hasText: '企业代码' }).locator('input').fill(code)
      await dialog.locator('.el-form-item', { hasText: '企业名称' }).locator('input').fill(name)
      await dialog.locator('.el-form-item', { hasText: '初始密码' }).locator('input').fill(ADMIN_PASSWORD)
      await dialog.locator('button:has-text("开通")').click()
      await page.waitForSelector(`[data-testid="tenant-table"] >> text=${code}`)
      await page.locator('.el-dialog').first().waitFor({ state: 'hidden' })
    }
    await shot(page, '1-platform-tenants')
    // 刷新页面后用刷新令牌 Cookie 恢复登录，不用重新登录
    await page.reload()
    const stillIn = await page
      .waitForSelector('button:has-text("开通租户")', { timeout: 15000 })
      .then(() => true)
      .catch(() => false)
    check('运营后台刷新页面后仍然登录', stillIn)
    await ctx.close()
  }

  // 2. 租户 A 管理员：创建坐席 Alice 和两个客户
  {
    const { ctx, page } = await newPage(browser, 'tenant-a-admin')
    await consoleLogin(page, TENANT_A, 'admin', ADMIN_PASSWORD)
    summary.menus['租户 A 管理员'] = await menuTitles(page)
    await shot(page, '2-admin-dashboard')

    await page.locator('[data-testid="main-menu"] .el-menu-item', { hasText: '员工' }).click()
    await page.waitForSelector('[data-testid="staff-table"] >> text=admin')
    await page.click('button:has-text("新建员工")')
    const dialog = page.locator('.el-dialog:visible')
    await dialog.locator('.el-form-item', { hasText: '用户名' }).locator('input').fill('alice')
    await dialog.locator('.el-form-item', { hasText: '姓名' }).locator('input').fill('Alice')
    await dialog.locator('.el-form-item', { hasText: '初始密码' }).locator('input').fill(AGENT_PASSWORD)
    await dialog.locator('button:has-text("保存")').click()
    await page.waitForSelector('[data-testid="staff-table"] >> text=alice')
    await shot(page, '3-admin-staff')

    await openCustomers(page)
    await createCustomer(page, '客户甲-归属Alice', 'Alice')
    await createCustomer(page, '客户乙-归属管理员', null)
    summary.customers['租户 A 管理员'] = await customerNames(page)
    await shot(page, '4-admin-customers')
    await ctx.close()
  }

  // 3. 租户 A 坐席 Alice
  {
    const { ctx, page } = await newPage(browser, 'tenant-a-alice')
    await consoleLogin(page, TENANT_A, 'alice', AGENT_PASSWORD)
    summary.menus['租户 A 坐席'] = await menuTitles(page)
    await shot(page, '5-agent-dashboard')
    await openCustomers(page)
    summary.customers['租户 A 坐席'] = await customerNames(page)
    await shot(page, '6-agent-customers')

    await page.goto(`${CONSOLE}/staff`)
    await page.waitForSelector('text=没有访问权限')
    check('坐席直接访问 /staff 被拦截到无权限页', page.url().endsWith('/forbidden'))
    await shot(page, '7-agent-forbidden')
    await ctx.close()
  }

  // 4. 租户 B 管理员
  {
    const { ctx, page } = await newPage(browser, 'tenant-b-admin')
    await consoleLogin(page, TENANT_B, 'admin', ADMIN_PASSWORD)
    await openCustomers(page)
    summary.customers['租户 B 管理员'] = await customerNames(page)
    await shot(page, '8-other-tenant-customers')
    await ctx.close()
  }

  const menus = summary.menus
  const customers = summary.customers
  // 与后端 app/core/consoles.py 一致（§25.15）：管理员看全部菜单；客服只看接待相关的菜单。
  const ALL_MENUS = ['首页', '工作台', '会话记录', '待办', '订单', '应收账款', '合同', '资料', '商品', '加工', '仓库', '个人待办', '客户', '知识库', 'AI 接待', 'AI 唤醒', 'AI 助理', '员工', '报表', '盈利报表', '群发', '企业微信', '操作日志', '设置']
  const AGENT_MENUS = ['首页', '工作台', '会话记录', '待办', '订单', '合同', '资料', '个人待办', '客户', '知识库', 'AI 助理']
  check(
    `管理员看到全部 ${ALL_MENUS.length} 个菜单`,
    JSON.stringify(menus['租户 A 管理员']) === JSON.stringify(ALL_MENUS),
    menus['租户 A 管理员'],
  )
  check(
    `坐席只看到 ${AGENT_MENUS.join('/')}`,
    JSON.stringify(menus['租户 A 坐席']) === JSON.stringify(AGENT_MENUS),
    menus['租户 A 坐席'],
  )
  check('管理员看到本租户全部 2 个客户', customers['租户 A 管理员'].length === 2)
  check(
    '坐席只看到归属自己的客户',
    JSON.stringify(customers['租户 A 坐席']) === JSON.stringify(['客户甲-归属Alice']),
  )
  check('租户 B 看不到租户 A 的客户', customers['租户 B 管理员'].length === 0)
  check('没有前端脚本错误', summary.consoleErrors.length === 0)
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
