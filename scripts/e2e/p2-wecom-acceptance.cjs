// P2 验收：企业微信接入在浏览器里走通（对接模拟企业微信服务 backend/tests/fake_wecom.py）。
//
// 1. 管理员在"企业微信"页扫码授权代开发应用（模拟授权页），授权后自动同步成员、客户、标签、客服账号；
//    把成员张三、李四绑定到坐席 Amy、Bob；设置微信客服欢迎语、新客户欢迎语（附带客服链接）。
// 2. 微信客户进入客服会话收到欢迎语；发来咨询后分配给 Amy（企业微信应用消息提醒张三），
//    工作台显示"剩余 N 条 / 截止 hh:mm"，Amy 的回复经企业微信送达客户。
// 3. 开启 AI 接待后，另一位微信客户的问题由 AI 回答，回复带"【AI】"标识。
// 4. 张三添加新客户：客户档案归 Amy，自动发送欢迎语；Amy 给客户打的企业标签写回企业微信。
// 5. 管理员把客户转给 Bob 并同步企业微信（在职继承），企业微信接替后归属记录显示"已接替"。
// 6. Amy 用企业微信扫码登录控制台；在侧边栏（开发模式）查看客户档案、生成 AI 建议并发送。
//
// 前置：后端、实时消费进程、调度进程接到模拟大模型和模拟企业微信（EDP_WECOM_* 指向
// http://127.0.0.1:8901，见 README），模拟企业微信以 --platform 指向后端运行；控制台、OpenIM 已启动。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p2-wecom-acceptance.cjs
const { chromium } = require('playwright')
const { execSync } = require('child_process')
const fs = require('fs')
const path = require('path')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const WECOM = env('WECOM_URL', 'http://127.0.0.1:8901')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p2')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `wc-${RUN}`
const PASSWORD = 'demo-pass-2026'
const ROOT = path.resolve(__dirname, '../..')
const TRANSFERS_CMD = env('TRANSFERS_CMD', 'cd backend && uv run python -m app.cli wecom-transfers')

const ANSWER = '一般 2 到 3 天送达，偏远地区 5 到 7 天。'
const KF_WELCOME = '您好，这里是官方客服，请问有什么可以帮您？'
const CONTACT_WELCOME = '您好，我是张三，很高兴为您服务！'

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

const fake = (name, body = {}) => json(`${WECOM}/_fake/${name}`, { method: 'POST', body })
const fakeState = () => fake('state')

async function login(username) {
  const data = await json(`${API}/api/v1/auth/login`, {
    method: 'POST',
    body: { tenant_code: TENANT, username, password: PASSWORD },
  })
  return data.access_token
}

async function prepare() {
  // 模拟企业微信只有一个企业：先让平台解除上一次验收的绑定，再准备这次的数据。
  await fake('reset')
  await fake('suite_ticket')
  await fake('add_contact', { external_userid: 'wmA', name: '客户甲', tags: ['tag-vip'] })
  await fake('add_contact', { external_userid: 'wmcust0001', name: '王小明', remark: '王总' })
  const platform = await json(`${API}/platform/v1/auth/login`, {
    method: 'POST',
    body: { username: PLATFORM_USER, password: PLATFORM_PASSWORD },
  })
  await json(`${API}/platform/v1/tenants`, {
    method: 'POST',
    token: platform.access_token,
    body: {
      code: TENANT,
      name: `企业微信验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  const admin = await login('admin')
  for (const [username, name] of [
    ['amy', 'Amy'],
    ['bob', 'Bob'],
  ]) {
    await json(`${API}/api/v1/staff`, {
      method: 'POST',
      token: admin,
      body: { username, display_name: name, password: PASSWORD, role_codes: ['agent'] },
    })
  }
  return admin
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
  const ctx = await browser.newContext({ viewport, locale: 'zh-CN' })
  const page = await ctx.newPage()
  watchErrors(page, label)
  return { ctx, page }
}

async function consoleLogin(browser, username) {
  const { ctx, page } = await newPage(browser, username)
  await page.goto(`${CONSOLE}/login`)
  await page.fill('input[placeholder="例如 demo"]', TENANT)
  await page.fill('input[autocomplete="username"]', username)
  await page.fill('input[autocomplete="current-password"]', PASSWORD)
  await page.click('button:has-text("登录")')
  await page.waitForSelector('[data-testid="main-menu"]')
  return { ctx, page }
}

const menu = (page, title) =>
  page.locator('[data-testid="main-menu"] .el-menu-item', { hasText: title }).click()
const tab = (page, title) =>
  page.locator('.el-tabs__item:visible', { hasText: title }).first().click()
const settle = (page) => page.waitForTimeout(600)

async function waitFor(fn, timeout = 20000) {
  const deadline = Date.now() + timeout
  for (;;) {
    const value = await fn()
    if (value || Date.now() > deadline) return value
    await new Promise((r) => setTimeout(r, 500))
  }
}

async function pickOption(page, selectLocator, text) {
  await selectLocator.click()
  await page.locator('.el-select-dropdown__item:visible', { hasText: text }).first().click()
}

async function run(browser) {
  const adminToken = await prepare()

  // 1. 扫码授权
  const admin = await consoleLogin(browser, 'admin')
  const page = admin.page
  await menu(page, '企业微信')
  await page.locator('[data-testid="wecom-unbound"]').waitFor()
  await page.click('[data-testid="wecom-install"]')
  await page.waitForURL(/3rdapp\/install/)
  await page.screenshot({ path: `${SHOTS}/1-install-page.png` })
  await page.click('#authorize')
  await page.waitForURL(/integrations\/wecom/)
  await page.locator('[data-testid="wecom-corp"]').waitFor()
  const synced = await waitFor(async () => {
    const status = await json(`${API}/api/v1/admin/integrations/wecom`, { token: adminToken })
    return status.counts?.members === 3 && status.kf_accounts.length === 1 && status.counts.contacts === 2
      ? status
      : null
  })
  await page.reload()
  await page.locator('[data-testid="wecom-corp"]').waitFor()
  await settle(page)
  const corpText = await page.locator('[data-testid="wecom-corp"]').innerText()
  check(
    '管理员扫码授权企业微信，自动同步成员、客户、标签和客服账号',
    !!synced && corpText.includes('示例科技有限公司') && corpText.includes('3'),
    { corpText: corpText.slice(0, 200), counts: synced?.counts },
  )
  await page.screenshot({ path: `${SHOTS}/2-wecom-bound.png` })

  // 成员绑定
  await tab(page, '成员绑定')
  await page.locator('[data-testid="wecom-members"] .el-table__row').first().waitFor()
  await pickOption(page, page.locator('[data-testid="bind-zhangsan"]'), 'Amy')
  await settle(page)
  await pickOption(page, page.locator('[data-testid="bind-lisi"]'), 'Bob')
  await settle(page)
  const members = await json(`${API}/api/v1/admin/integrations/wecom/members`, { token: adminToken })
  const bound = Object.fromEntries(members.items.map((m) => [m.userid, m.staff_name]))
  check('成员绑定：张三 → Amy、李四 → Bob', bound.zhangsan === 'Amy' && bound.lisi === 'Bob', bound)
  await page.screenshot({ path: `${SHOTS}/3-members.png` })
  // 绑定后重新同步客户：添加人张三绑定的 Amy 成为归属坐席。
  await page.click('[data-testid="wecom-sync"]')

  // 微信客服欢迎语、新客户欢迎语
  await tab(page, '微信客服')
  await page.click('[data-testid="kf-welcome"]')
  await page.fill('textarea[data-testid="kf-welcome-input"]', KF_WELCOME)
  await page.click('[data-testid="kf-welcome-save"]')
  await page.locator('.el-dialog:visible').waitFor({ state: 'hidden' })
  await tab(page, '设置')
  const form = page.locator('[data-testid="wecom-settings"]')
  await form.locator('.el-form-item', { hasText: '新客户欢迎语' }).locator('.el-switch').click()
  await page.fill('textarea[data-testid="welcome-text"]', CONTACT_WELCOME)
  await pickOption(page, form.locator('.el-form-item', { hasText: '附带客服入口' }).locator('.el-select'), '官方客服')
  await page.click('[data-testid="wecom-settings-save"]')
  await settle(page)
  const status = await json(`${API}/api/v1/admin/integrations/wecom`, { token: adminToken })
  check(
    '设置微信客服欢迎语和新客户欢迎语（附带客服链接）',
    status.settings.welcome_enabled &&
      status.settings.welcome_kf_id === status.kf_accounts[0].open_kfid,
    status.settings,
  )

  // 2. 微信客户进入会话 → 欢迎语；发来咨询 → Amy 接待
  const amy = await consoleLogin(browser, 'amy')
  await menu(amy.page, '工作台')
  await amy.page.locator('[data-testid="agent-status"]', { hasText: '在线' }).waitFor({ timeout: 15000 })
  await fake('customer_enters', { external_userid: 'wmcust0001', nickname: '王小明' })
  const welcomed = await waitFor(async () =>
    (await fakeState()).event_replies.find((r) => r.text?.content === KF_WELCOME),
  )
  check('客户进入微信客服会话，收到客服账号的欢迎语', !!welcomed, (await fakeState()).event_replies)

  await fake('customer_says', { text: '你好，想问下发货时间', external_userid: 'wmcust0001', nickname: '王小明' })
  await amy.page.locator('[data-testid="session-item"]').first().waitFor({ timeout: 20000 })
  await amy.page.locator('[data-testid="session-item"]').first().click()
  await amy.page
    .locator('[data-testid="chat-message"]', { hasText: '你好，想问下发货时间' })
    .waitFor({ timeout: 15000 })
  const indicator = amy.page.locator('[data-testid="reply-window"]')
  await indicator.waitFor()
  const before = await indicator.innerText()
  const title = await amy.page.locator('[data-testid="chat-title"]').innerText()
  const cards = (await fakeState()).app_messages
  check(
    '微信客户的咨询分配给 Amy（应用消息提醒张三），工作台显示回复额度',
    title.includes('王总') &&
      /剩余 4 条/.test(before) &&
      cards.some((c) => c.touser === 'zhangsan' && c.textcard.title === '新会话分配'),
    { title, before, cards: cards.map((c) => [c.touser, c.textcard.title]) },
  )

  await amy.page.fill('textarea[data-testid="composer-input"]', '一般 2 到 3 天送达，您放心')
  await amy.page.click('[data-testid="send-button"]')
  const delivered = await waitFor(async () =>
    (await fakeState()).sent.find(
      (m) => m.touser === 'wmcust0001' && m.text?.content === '一般 2 到 3 天送达，您放心',
    ),
  )
  await waitFor(async () => /剩余 3 条/.test(await indicator.innerText()), 10000)
  const after = await indicator.innerText()
  check('坐席回复经企业微信送达客户，回复额度减少', !!delivered && /剩余 3 条/.test(after), {
    after,
    delivered: !!delivered,
  })
  await amy.page.screenshot({ path: `${SHOTS}/4-workbench-kf.png` })

  // 3. AI 接待另一位微信客户
  await json(`${API}/api/v1/ai/settings`, { method: 'PUT', token: adminToken, body: { enabled: true } })
  const policies = await json(`${API}/api/v1/routing-policies`, { token: adminToken })
  const policy = policies.items.find((p) => p.is_default)
  await json(`${API}/api/v1/routing-policies/${policy.id}`, {
    method: 'PATCH',
    token: adminToken,
    body: { mode: 'ai_first' },
  })
  await json(`${API}/api/v1/kb/items`, {
    method: 'POST',
    token: adminToken,
    body: { title: '订单发货后多久能到？', content: ANSWER, questions: ['快递几天能到'], publish: true },
  })
  await fake('customer_says', { text: '快递几天能到', external_userid: 'wmcust0002', nickname: '李小红' })
  // AI 的回答是菜单消息：正文带"【AI】"标识，后面是「转人工」按钮。
  const textOf = (m) => m?.text?.content ?? m?.msgmenu?.head_content
  const aiReply = await waitFor(async () =>
    (await fakeState()).sent.find((m) => m.touser === 'wmcust0002' && textOf(m)?.startsWith('【AI】')),
  )
  check(
    'AI 接待微信客户，回复带"【AI】"标识和「转人工」按钮',
    textOf(aiReply) === `【AI】${ANSWER}` && aiReply?.msgmenu?.list?.[0]?.click?.id === 'edp_handoff',
    aiReply,
  )
  await json(`${API}/api/v1/routing-policies/${policy.id}`, {
    method: 'PATCH',
    token: adminToken,
    body: { mode: 'human_first' },
  })

  // 4. 张三添加新客户；Amy 打标签写回企业微信
  await fake('staff_adds_contact', { external_userid: 'wmC', name: '客户丙', userid: 'zhangsan' })
  const listed = await waitFor(async () => {
    await amy.page.goto(`${CONSOLE}/customers`)
    const table = amy.page.locator('[data-testid="customer-table"]')
    await table.locator('.el-table__row').first().waitFor()
    return (await table.innerText()).includes('客户丙')
  })
  const welcomes = (await fakeState()).welcomes
  check(
    '张三添加新客户：客户归 Amy，自动发送欢迎语（附带客服链接）',
    listed &&
      welcomes.some(
        (w) => w.text.content === CONTACT_WELCOME && w.attachments?.[0]?.link?.url.includes('kfid'),
      ),
    { listed, welcomes },
  )
  await amy.page.screenshot({ path: `${SHOTS}/5-customers.png` })

  await menu(amy.page, '工作台')
  await amy.page.locator('[data-testid="session-item"]').first().click()
  await amy.page.locator('[data-testid="customer-wecom"]').waitFor({ timeout: 15000 })
  const wecomBlock = await amy.page.locator('[data-testid="customer-wecom"]').innerText()
  await amy.page.fill('input[data-testid="customer-tag-input"]', '高意向')
  await amy.page.press('input[data-testid="customer-tag-input"]', 'Enter')
  const marked = await waitFor(async () =>
    (await fakeState()).marked.find(
      (m) => m.external_userid === 'wmcust0001' && m.add_tag?.includes('tag-hot'),
    ),
  )
  check('客户面板显示企业微信添加人；打的企业标签写回企业微信', !!marked && wecomBlock.includes('Amy'), {
    wecomBlock,
    marked,
  })
  await amy.page.screenshot({ path: `${SHOTS}/6-customer-panel.png` })

  // 5. 在职继承：客户甲转给 Bob
  await menu(page, '客户')
  await page.locator('[data-testid="customer-table"] .el-table__row', { hasText: '客户甲' }).waitFor()
  await page
    .locator('[data-testid="customer-table"] .el-table__row', { hasText: '客户甲' })
    .locator('.el-checkbox')
    .click()
  await page.click('[data-testid="transfer-owner"]')
  await pickOption(page, page.locator('.el-dialog:visible [data-testid="new-owner"]'), 'Bob')
  await page.locator('.el-dialog:visible .el-checkbox', { hasText: '在职继承' }).click()
  await page.locator('.el-dialog:visible button', { hasText: '转移' }).click()
  const toast = await page
    .locator('.el-message', { hasText: '在职继承' })
    .last()
    .innerText()
    .catch(() => '')
  const waiting = (await fakeState()).transfers.find((t) => t.external_userid === 'wmA')
  await fake('complete_transfers')
  execSync(TRANSFERS_CMD, { cwd: ROOT, stdio: 'pipe', shell: '/bin/bash' })
  await page.reload()
  await page
    .locator('[data-testid="customer-table"] .el-table__row', { hasText: '客户甲' })
    .locator('button', { hasText: '归属记录' })
    .click()
  const drawer = page.locator('.el-drawer:visible')
  await drawer.locator('.el-timeline-item').first().waitFor()
  await settle(page)
  const history = await drawer.innerText()
  check(
    '客户转给 Bob 并同步企业微信：提交在职继承，接替后归属记录显示"已接替"',
    toast.includes('已提交在职继承 1 位') && waiting?.status === 2 && history.includes('企业微信已接替'),
    { toast, waiting, history: history.slice(0, 300) },
  )
  await page.screenshot({ path: `${SHOTS}/7-owner-history.png` })

  // 6. 企业微信扫码登录
  const sso = await newPage(browser, 'sso')
  await sso.page.goto(`${CONSOLE}/login`)
  await sso.page.fill('input[placeholder="例如 demo"]', TENANT)
  await sso.page.click('[data-testid="wecom-login"]')
  await sso.page.waitForURL(/wwlogin\/sso\/login/)
  await sso.page.screenshot({ path: `${SHOTS}/8-sso-page.png` })
  await sso.page.click('a[data-userid="zhangsan"]')
  await sso.page.waitForSelector('[data-testid="main-menu"]', { timeout: 15000 })
  const who = await sso.page.locator('.el-header, header').first().innerText()
  check('Amy 用企业微信扫码登录控制台', who.includes('Amy'), who.slice(0, 120))

  // 侧边栏（开发模式：不在企业微信里）
  await sso.page.setViewportSize({ width: 400, height: 900 })
  await sso.page.goto(`${CONSOLE}/wecom/sidebar?external_userid=wmcust0001`)
  await sso.page.locator('[data-testid="sidebar-customer"]').waitFor({ timeout: 15000 })
  const card = await sso.page.locator('[data-testid="sidebar-customer"]').innerText()
  await sso.page.fill('textarea[data-testid="sidebar-question"]', '快递几天能到')
  await sso.page.click('[data-testid="sidebar-suggest"]')
  await sso.page.locator('[data-testid="sidebar-suggestion"]').first().waitFor({ timeout: 15000 })
  await sso.page.locator('[data-testid="sidebar-suggestion"]').first().click()
  await sso.page.click('[data-testid="sidebar-send"]')
  const sent = await sso.page
    .locator('.el-message', { hasText: '已记录' })
    .last()
    .innerText()
    .catch(() => '')
  const editor = await sso.page.locator('textarea[data-testid="sidebar-editor"]').inputValue()
  check(
    '侧边栏显示客户档案和标签，AI 建议一键发送并记录',
    card.includes('王总') && card.includes('高意向') && sent.includes('已记录') && editor === '',
    { card, sent },
  )
  await sso.page.screenshot({ path: `${SHOTS}/9-sidebar.png`, fullPage: true })

  check('没有前端脚本错误', summary.consoleErrors.length === 0, summary.consoleErrors)
  for (const ctx of [admin.ctx, amy.ctx, sso.ctx]) await ctx.close()
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
