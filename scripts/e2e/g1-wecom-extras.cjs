// 企业微信补充能力的浏览器验收（对接模拟企业微信 backend/tests/fake_wecom.py）：
//
// 1. 群发：管理员在"群发"页按标签给客户创建群发任务，员工在企业微信里确认后，详情里回收到发送结果。
// 2. 客户群活码：生成"加入群聊"二维码，客户扫码进群后统计进群人数。
// 3. 侧边栏（模拟的企业微信 JS-SDK）：从客户单聊打开，修改客户标签（写回企业微信），用快捷话术区和
//    AI 建议发送到当前聊天，一键拉上接单员建群，群立即出现在客户档案里。
// 4. 手机版工作台：企业微信手机端打开工作台进入手机版；微信客户发来语音（转写成文字）和文字，
//    坐席在手机上回复；结束会话时客户收到满意度按钮，点选后评价记在会话上。
// 5. 离职继承：成员离职后，他的客户出现在"离职继承"页，分配给接手的员工，同时转移他的客户群。
//
// 前置：同 p2-wecom-acceptance.cjs；控制台以 VITE_WECOM_JSSDK_URLS=http://127.0.0.1:8901/jssdk/jwxwork.js
// 启动（侧边栏加载模拟的 JS-SDK）；后端配置了语音转文字（EDP_ASR_*，指向模拟大模型）。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/g1-wecom-extras.cjs
const { chromium } = require('playwright')
const fs = require('fs')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const WECOM = env('WECOM_URL', 'http://127.0.0.1:8901')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/g1')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `wx-${RUN}`
const PASSWORD = 'demo-pass-2026'
const GROUP = 'wrgroup1'
const DESKTOP_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 wxwork/4.1.20'
const PHONE_UA =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 wxwork/4.1.20 MicroMessenger/7.0.1'

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

async function waitFor(fn, timeout = 20000) {
  const deadline = Date.now() + timeout
  for (;;) {
    const value = await fn()
    if (value || Date.now() > deadline) return value
    await new Promise((r) => setTimeout(r, 500))
  }
}

async function login(username) {
  const data = await json(`${API}/api/v1/auth/login`, {
    method: 'POST',
    body: { tenant_code: TENANT, username, password: PASSWORD },
  })
  return data.access_token
}

/** 开通租户、授权企业微信（经模拟授权页）、绑定成员、同步客户和客户群。 */
async function prepare() {
  await fake('reset')
  await fake('suite_ticket')
  await fake('add_contact', { external_userid: 'wmA', name: '客户甲', tags: ['tag-vip'] })
  await fake('add_contact', { external_userid: 'wmB', name: '客户乙', userid: 'lisi' })
  const platform = await json(`${API}/platform/v1/auth/login`, {
    method: 'POST',
    body: { username: PLATFORM_USER, password: PLATFORM_PASSWORD },
  })
  await json(`${API}/platform/v1/tenants`, {
    method: 'POST',
    token: platform.access_token,
    body: {
      code: TENANT,
      name: `企业微信补充验收 ${RUN}`,
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
  const install = await json(`${API}/api/v1/admin/integrations/wecom/install`, {
    method: 'POST',
    token: admin,
  })
  const confirm = install.url.replace('/3rdapp/install?', '/3rdapp/install/confirm?')
  const back = await fetch(confirm, { redirect: 'manual' })
  await fetch(back.headers.get('location'), { redirect: 'manual' })
  await waitFor(async () => {
    const status = await json(`${API}/api/v1/admin/integrations/wecom`, { token: admin })
    return status.counts?.members === 3 && status.kf_accounts.length === 1
  })
  const staff = (await json(`${API}/api/v1/staff`, { token: admin })).items
  const id = (name) => staff.find((s) => s.display_name === name).id
  for (const [userid, name] of [
    ['zhangsan', 'Amy'],
    ['lisi', 'Bob'],
  ]) {
    await json(`${API}/api/v1/admin/integrations/wecom/members/${userid}`, {
      method: 'PUT',
      token: admin,
      body: { staff_id: id(name) },
    })
  }
  await fake('add_group', { chat_id: GROUP, name: '甲总的服务群', owner: 'zhangsan', externals: ['wmA'] })
  await json(`${API}/api/v1/admin/integrations/wecom/sync`, {
    method: 'POST',
    token: admin,
    body: { targets: ['contacts', 'groups'] },
  })
  await waitFor(async () => {
    const customers = await json(`${API}/api/v1/customers`, { token: admin })
    const a = customers.items.find((c) => c.display_name === '客户甲')
    return a?.owner_display_name === 'Amy'
  })
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

async function consoleLogin(browser, username, options = {}) {
  const ctx = await browser.newContext({
    viewport: options.viewport ?? { width: 1440, height: 900 },
    userAgent: options.userAgent,
    locale: 'zh-CN',
    isMobile: options.isMobile ?? false,
    hasTouch: options.isMobile ?? false,
  })
  const page = await ctx.newPage()
  watchErrors(page, username)
  await page.goto(`${CONSOLE}/login`)
  await page.fill('input[placeholder="例如 demo"]', TENANT)
  await page.fill('input[autocomplete="username"]', username)
  await page.fill('input[autocomplete="current-password"]', PASSWORD)
  await page.click('button:has-text("登录")')
  await page.waitForURL((url) => !url.pathname.startsWith('/login'))
  return { ctx, page }
}

const menu = (page, title) =>
  page.locator('[data-testid="main-menu"] .el-menu-item', { hasText: title }).click()
const tab = (page, title) =>
  page.locator('.el-tabs__item:visible', { hasText: title }).first().click()
const settle = (page) => page.waitForTimeout(600)

async function pickOption(page, selectLocator, text) {
  await selectLocator.click()
  await page.locator('.el-select-dropdown__item:visible', { hasText: text }).first().click()
}

async function run(browser) {
  const adminToken = await prepare()
  const admin = await consoleLogin(browser, 'admin')
  const page = admin.page

  // 1. 群发
  await menu(page, '群发')
  await page.click('[data-testid="broadcast-new"]')
  await page.fill('input[data-testid="broadcast-title"]', '国庆活动')
  await page.fill('textarea[data-testid="broadcast-content"]', '国庆期间全场九折，欢迎选购！')
  await pickOption(page, page.locator('[data-testid="broadcast-tags"]'), 'VIP')
  await page.keyboard.press('Escape')
  await page.click('[data-testid="broadcast-save"]')
  await page.locator('[data-testid="broadcast-table"] .el-table__row').first().waitFor()
  const template = await waitFor(async () => (await fakeState()).templates[0])
  await fake('confirm_broadcast')
  await page.click('[data-testid="broadcast-detail"]')
  await page.click('[data-testid="broadcast-refresh"]')
  const stats = page.locator('[data-testid="broadcast-stats"]')
  const refreshed = await waitFor(async () => (await stats.innerText()).includes('已发送 1'), 10000)
  check(
    '群发：按标签给客户创建群发任务，由归属坐席的企业微信成员确认后发出，回收到发送结果',
    template?.sender === 'zhangsan' && Object.keys(template.results).join() === 'wmA' && refreshed,
    { template, stats: await stats.innerText() },
  )
  await page.screenshot({ path: `${SHOTS}/1-broadcast.png` })
  await page.keyboard.press('Escape')

  // 2. 客户群活码
  await menu(page, '企业微信')
  await tab(page, '客户群活码')
  await page.click('[data-testid="join-way-new"]')
  await page.fill('input[data-testid="join-way-name"]', '国庆活动群')
  await pickOption(page, page.locator('[data-testid="join-way-groups"]'), '甲总的服务群')
  await page.keyboard.press('Escape')
  await page.fill('input[data-testid="join-way-base"]', 'VIP 客户群')
  await page.click('[data-testid="join-way-save"]')
  await page.locator('[data-testid="join-way-table"] .el-table__row').first().waitFor()
  const way = (await fakeState()).join_ways[0]
  await fake('join_by_qr', { config_id: way.config_id, external_userid: 'wmScan' })
  const joined = await waitFor(async () => {
    await page.reload()
    await tab(page, '客户群活码')
    const cell = page.locator('[data-testid="join-way-joined"]').first()
    await cell.waitFor()
    return (await cell.innerText()) === '1'
  })
  check(
    '客户群活码：生成"加入群聊"二维码（群满自动建群），客户扫码进群后统计进群人数',
    way?.auto_create_room === 1 && way.room_base_name === 'VIP 客户群' && joined,
    way,
  )
  await page.screenshot({ path: `${SHOTS}/2-join-ways.png` })

  // 3. 侧边栏（模拟 JS-SDK）
  await fake('jssdk/set_context', { entry: 'single_chat_tools', external_userid: 'wmA', userid: 'zhangsan' })
  const side = await consoleLogin(browser, 'amy', {
    viewport: { width: 420, height: 900 },
    userAgent: DESKTOP_UA,
  })
  await side.page.goto(`${CONSOLE}/wecom/sidebar`)
  const card = side.page.locator('[data-testid="sidebar-customer"]')
  await card.waitFor({ timeout: 15000 })
  await side.page.click('[data-testid="sidebar-edit-tags"]')
  await pickOption(side.page, side.page.locator('[data-testid="sidebar-tag-select"]'), '高意向')
  await side.page.keyboard.press('Escape')
  await side.page.click('[data-testid="sidebar-save-tags"]')
  const marked = await waitFor(async () =>
    (await fakeState()).marked.find((m) => m.external_userid === 'wmA' && m.add_tag?.includes('tag-hot')),
  )
  const library = await side.page.locator('[data-testid="sidebar-library"]').innerText()
  await side.page.fill('textarea[data-testid="sidebar-question"]', '能开发票吗')
  await side.page.click('[data-testid="sidebar-suggest"]')
  await side.page.locator('[data-testid="sidebar-suggestion"]').first().waitFor({ timeout: 15000 })
  await side.page.locator('[data-testid="sidebar-suggestion"]').first().click()
  await side.page.click('[data-testid="sidebar-send"]')
  const sentViaSdk = await waitFor(async () => (await fakeState()).jssdk_sent[0])
  check(
    '侧边栏（企业微信 JS-SDK）：改客户标签写回企业微信，快捷话术与知识检索可用，AI 建议经 sendChatMessage 发出',
    !!marked && library.includes('快捷话术') && !!sentViaSdk?.text?.content,
    { marked, sentViaSdk },
  )
  await side.page.click('[data-testid="sidebar-create-group"]')
  await pickOption(side.page, side.page.locator('[data-testid="sidebar-group-members"]'), '李四')
  await side.page.keyboard.press('Escape')
  await side.page.click('[data-testid="sidebar-group-create"]')
  const created = await waitFor(async () => {
    const groups = (await fakeState()).group_chats
    return groups.find((g) => g.chat_id !== GROUP && g.owner === 'zhangsan')
  })
  const cardText = await waitFor(async () => {
    const text = await card.innerText()
    return text.includes('甲总的服务群') && text.includes('客户甲的服务群') ? text : null
  }, 15000)
  check('侧边栏一键建群（openEnterpriseChat）：拉上接单员李四，新群出现在客户档案里', !!created && !!cardText, {
    created,
    card: await card.innerText(),
  })
  await side.page.screenshot({ path: `${SHOTS}/3-sidebar.png`, fullPage: true })

  // 4. 手机版工作台
  const phone = await consoleLogin(browser, 'amy', {
    viewport: { width: 390, height: 844 },
    userAgent: PHONE_UA,
    isMobile: true,
  })
  await phone.page.goto(`${CONSOLE}/workbench`)
  await phone.page.waitForURL(/\/m(\?|$)/)
  await phone.page.locator('[data-testid="mobile-status"]', { hasText: '在线' }).waitFor({ timeout: 15000 })
  await fake('customer_voice', { external_userid: 'wmcust0001', nickname: '王小明', text: '我想查一下订单到哪了' })
  await fake('customer_says', { external_userid: 'wmcust0001', nickname: '王小明', text: '单号 2026' })
  await phone.page.locator('[data-testid="mobile-session"]').first().waitFor({ timeout: 20000 })
  await phone.page.locator('[data-testid="mobile-session"]').first().click()
  const transcript = phone.page.locator('[data-testid="voice-transcript"]')
  await transcript.waitFor({ timeout: 15000 })
  const audio = await phone.page.locator('[data-testid="message-voice"] audio').count()
  await phone.page.fill('textarea[data-testid="composer-input"]', '已经发货了，预计明天送达')
  await phone.page.click('[data-testid="send-button"]')
  const replied = await waitFor(async () =>
    (await fakeState()).sent.find((m) => m.touser === 'wmcust0001' && m.text?.content === '已经发货了，预计明天送达'),
  )
  check(
    '手机版工作台：企业微信手机端进入手机版，语音转写成文字（MP3 可播放），坐席在手机上回复送达客户',
    (await transcript.innerText()).includes('我想查一下订单到哪了') && audio === 1 && !!replied,
    { transcript: await transcript.innerText(), audio, replied: !!replied },
  )
  await phone.page.screenshot({ path: `${SHOTS}/4-mobile-chat.png` })
  const sessionId = new URL(phone.page.url()).searchParams.get('session')
  await phone.page.click('[data-testid="close-session"]')
  await phone.page.locator('.el-message-box__btns button', { hasText: '结束' }).click()
  const csatMenu = await waitFor(async () =>
    (await fakeState()).sent.find((m) => m.touser === 'wmcust0001' && m.msgtype === 'msgmenu'),
  )
  await fake('menu_click', { menu_id: 'edp_csat_5', external_userid: 'wmcust0001' })
  const rated = await waitFor(async () => {
    const detail = await json(`${API}/api/v1/sessions/${sessionId}`, { token: adminToken })
    return detail.csat === 5 ? detail : null
  })
  check(
    '微信客服满意度：结束会话时发送评价按钮（菜单消息），客户点选后评价记在会话上',
    csatMenu?.msgmenu?.list?.length === 5 && !!rated,
    { csatMenu, csat: rated?.csat },
  )
  await phone.page.screenshot({ path: `${SHOTS}/5-mobile-closed.png` })

  // 5. 离职继承
  await fake('member_leaves', { userid: 'zhangsan' })
  await waitFor(async () => {
    const members = await json(`${API}/api/v1/admin/integrations/wecom/members`, { token: adminToken })
    return members.items.find((m) => m.userid === 'zhangsan')?.status === 'left'
  })
  await tab(page, '离职继承')
  const member = page.locator('[data-testid="resigned-member"]').first()
  await member.waitFor({ timeout: 15000 })
  const listed = await member.innerText()
  await pickOption(page, member.locator('[data-testid="resigned-target"]'), 'Bob')
  await member.locator('[data-testid="resigned-assign"]').click()
  const toast = await page
    .locator('.el-message', { hasText: '离职继承' })
    .last()
    .innerText()
    .catch(() => '')
  const state = await fakeState()
  await page.locator('[data-testid="resigned-empty"]').waitFor({ timeout: 10000 })
  const records = await page.locator('[data-testid="group-transfers"]').innerText()
  check(
    '离职继承：离职成员的客户分配给 Bob（企业微信离职继承），他的客户群同时转给李四',
    listed.includes('客户甲') &&
      toast.includes('离职继承 1 位') &&
      state.resigned_transfers.some((t) => t.external_userid === 'wmA' && t.takeover === 'lisi') &&
      state.group_transfers.some((t) => t.chat_id === GROUP && t.to === 'lisi' && t.resigned) &&
      records.includes('甲总的服务群'),
    { listed, toast, records },
  )
  await page.screenshot({ path: `${SHOTS}/6-resigned.png` })

  check('没有前端脚本错误', summary.consoleErrors.length === 0, summary.consoleErrors)
  for (const ctx of [admin.ctx, side.ctx, phone.ctx]) await ctx.close()
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
