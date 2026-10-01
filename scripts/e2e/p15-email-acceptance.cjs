// P15 验收：邮件渠道（设计文档 §10.8），对接模拟邮箱服务器 backend/tests/fake_mail.py。
//
// 1. 管理员在「设置 → 邮箱」添加 163 邮箱：输入地址自动选择"163 邮箱"并显示授权码的获取说明；
//    高级设置里把服务器换成模拟的；测试连接（收信、发信都正常）后保存，列表显示"正常"。
//    添加之前收件箱里的邮件不导入。
// 2. 客户王小明发来订购邮件（引用了以前的往来，带一个附件）：管理员点"立即收取"，邮件直接进入人工
//    排队、分配给在线的坐席 Amy（路由策略是 AI 优先，邮件也不经过 AI）；Amy 的会话列表显示"邮件"
//    标记、邮件主题和未读数 2（邮件和附件）。
// 3. Amy 打开会话：邮件显示主题、发件人和正文，引用的历史内容折叠（可以展开），附件跟在后面；
//    "查看原邮件"在沙箱里显示原邮件；未读清零，刷新页面后仍是已读（未读记在服务器上）。
// 4. Amy 回复：回复框显示"回复：订购 100 个保温杯（最近的一封）"、默认主题"Re: 订购 100 个保温杯"；
//    Ctrl+Enter 发送，客户收到"Re: 订购 100 个保温杯"，In-Reply-To 指向原邮件，带签名和引用。
// 5. 客户回信：Amy 没在看这个会话时显示未读 1，刷新页面后还在；打开后清零。客户面板显示邮件身份，
//    会话里可以直接为客户下单（订单页签）。
// 6. 管理员停用邮箱后，Amy 不能再回复（"邮箱已停用，不能回复"）。
//
// 前置：后端、实时消费进程、调度进程配置了 EDP_MAIL_ALLOW_PRIVATE_HOSTS=true（模拟邮箱在本机、
// 不加密，见 scripts/ci/e2e.env），模拟邮箱服务器已启动（IMAP :1143、SMTP :1025、控制接口 :8903）；
// 控制台、OpenIM 已启动。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p15-email-acceptance.cjs
const { chromium } = require('playwright')
const fs = require('fs')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const MAIL = env('MAIL_URL', 'http://127.0.0.1:8903')
const IMAP_PORT = env('MAIL_IMAP_PORT', '1143')
const SMTP_PORT = env('MAIL_SMTP_PORT', '1025')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p15-email')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `p15-${RUN}`
const PASSWORD = 'demo-pass-2026'
const MAILBOX = `kefu-${RUN}@163.com`
const SECRET = `AUTHCODE${RUN}`.toUpperCase()
const CUSTOMER = `wang-${RUN}@customer.test`
const SUBJECT = '订购 100 个保温杯'
const ORDER_ID = `<order-${RUN}@customer.test>`

const summary = { tenant: TENANT, checks: [], consoleErrors: [] }
const check = (name, ok, detail) =>
  summary.checks.push(
    `${ok ? 'PASS' : 'FAIL'} ${name}${ok || detail === undefined ? '' : ` -> ${JSON.stringify(detail)}`}`,
  )
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

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

const fakeMail = (path, body) =>
  json(`${MAIL}/_fake/${path}`, body === undefined ? {} : { method: 'POST', body })

/** 客户发一封邮件到企业邮箱（模拟邮箱的收件箱）。 */
const deliver = (mail) => fakeMail('deliver', { to: MAILBOX, from: CUSTOMER, from_name: '王小明', ...mail })

async function login(username) {
  const tokens = await json(`${API}/api/v1/auth/login`, {
    method: 'POST',
    body: { tenant_code: TENANT, username, password: PASSWORD },
  })
  return tokens.access_token
}

async function prepare() {
  await fakeMail('mailboxes', { address: MAILBOX, secret: SECRET, require_id: true })
  // 添加邮箱之前就在收件箱里的邮件：不导入。
  await deliver({ subject: '以前的邮件', text: '这封不应该导入' })
  const platform = await json(`${API}/platform/v1/auth/login`, {
    method: 'POST',
    body: { username: PLATFORM_USER, password: PLATFORM_PASSWORD },
  })
  await json(`${API}/platform/v1/tenants`, {
    method: 'POST',
    token: platform.access_token,
    body: {
      code: TENANT,
      name: `邮件验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  const admin = await login('admin')
  await json(`${API}/api/v1/staff`, {
    method: 'POST',
    token: admin,
    body: { username: 'amy', display_name: 'Amy', password: PASSWORD, role_codes: ['agent'] },
  })
  // AI 优先：邮件仍然直接交给客服。
  await json(`${API}/api/v1/ai/settings`, { method: 'PUT', token: admin, body: { enabled: true } })
  const policies = await json(`${API}/api/v1/routing-policies`, { token: admin })
  const policy = policies.items.find((p) => p.is_default)
  await json(`${API}/api/v1/routing-policies/${policy.id}`, {
    method: 'PATCH',
    token: admin,
    body: { mode: 'ai_first' },
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

async function consoleLogin(browser, username) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' })
  const page = await ctx.newPage()
  watchErrors(page, username)
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

async function waitFor(fn, timeout = 20000) {
  const deadline = Date.now() + timeout
  for (;;) {
    const value = await fn()
    if (value || Date.now() > deadline) return value
    await sleep(500)
  }
}

async function pickOption(page, selectLocator, text) {
  await selectLocator.click()
  // 刚打开的下拉框在最后；上一个下拉框可能还在收起的动画里。
  const option = page.locator('.el-select-dropdown__item:visible', { hasText: text }).last()
  await option.click()
  await option.waitFor({ state: 'hidden' }).catch(() => undefined)
}

/** 一行服务器设置：地址、端口、加密方式。 */
async function fillServer(page, label, port) {
  const item = page.locator('[data-testid="mailbox-dialog"] .el-form-item', { hasText: label })
  const inputs = item.locator('input')
  await inputs.nth(0).fill('127.0.0.1')
  await inputs.nth(1).fill(port)
  await inputs.nth(1).press('Tab')
  await pickOption(page, item.locator('.el-select'), '不加密')
}

async function openWorkbench(page) {
  await menu(page, '工作台')
  await page.locator('[data-testid="agent-status"]', { hasText: '在线' }).waitFor({ timeout: 20000 })
}

const emailSession = (page) =>
  page.locator('[data-testid="session-item"]', { has: page.locator('[data-testid="email-tag"]') })

async function unreadOf(page) {
  const badge = emailSession(page).locator('[data-testid="unread"] .el-badge__content')
  return (await badge.count()) ? (await badge.innerText()).trim() : ''
}

async function run(browser) {
  const adminToken = await prepare()
  const amy = await consoleLogin(browser, 'amy')
  await openWorkbench(amy.page)

  // 1. 添加邮箱
  const admin = await consoleLogin(browser, 'admin')
  const page = admin.page
  await menu(page, '设置')
  await tab(page, '邮箱')
  await page.click('[data-testid="add-mailbox"]')
  const dialog = page.locator('[data-testid="mailbox-dialog"]')
  await dialog.waitFor()
  await page.fill('input[data-testid="mailbox-address-input"]', MAILBOX)
  await page.locator('[data-testid="mailbox-help"]').waitFor()
  const providerText = await page.locator('[data-testid="mailbox-provider"]').innerText()
  const help = await page.locator('[data-testid="mailbox-help"]').innerText()
  const secretLabel = await dialog.locator('.el-form-item', { has: page.locator('[data-testid="mailbox-secret"]') }).locator('.el-form-item__label').innerText()
  check(
    '输入 163 邮箱地址后自动选择"163 邮箱"，密码一栏叫"授权码"，并显示开启 IMAP/SMTP 的说明',
    providerText.includes('163') && secretLabel.includes('授权码') && help.includes('IMAP'),
    { providerText, secretLabel, help },
  )
  await page.fill('input[data-testid="mailbox-secret"]', SECRET)
  await page.fill('input[data-testid="mailbox-display-name"]', 'Acme 客服')
  await page.fill('textarea[data-testid="mailbox-signature"]', 'Acme 客服部\n电话 400-000-0000')
  await page.click('[data-testid="mailbox-advanced"]')
  await fillServer(page, '收信 IMAP', IMAP_PORT)
  await fillServer(page, '发信 SMTP', SMTP_PORT)
  await page.screenshot({ path: `${SHOTS}/1-add-mailbox.png` })
  await page.click('[data-testid="mailbox-test"]')
  const result = page.locator('[data-testid="mailbox-test-result"]')
  await result.waitFor({ timeout: 20000 })
  const tested = await result.innerText()
  check('测试连接：收信、发信都正常', tested.includes('收信：正常') && tested.includes('发信：正常'), tested)
  await page.click('[data-testid="mailbox-save"]')
  await dialog.waitFor({ state: 'hidden', timeout: 20000 })
  const row = page.locator('[data-testid="mailbox-table"] .el-table__row', { hasText: MAILBOX })
  await row.waitFor()
  const rowText = await row.innerText()
  const [account] = (await json(`${API}/api/v1/mail/accounts`, { token: adminToken })).items
  check(
    '保存后邮箱列表显示"正常"，授权码不返回',
    rowText.includes('正常') && rowText.includes('163 邮箱') && !JSON.stringify(account).includes(SECRET),
    rowText,
  )
  await page.screenshot({ path: `${SHOTS}/2-mailboxes.png` })

  // 2. 客户发来订购邮件 → 立即收取 → Amy 的未读会话
  await deliver({
    subject: SUBJECT,
    text:
      '你好，\n我们公司想订 100 个保温杯，杯身印 logo，月底前要货，请报个价。\n\n' +
      '在 2026年9月30日 10:00，Acme 客服 写道：\n> 您好，请问有什么可以帮您？',
    attachments: [
      {
        name: '需求清单.xlsx',
        mime: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        data: Buffer.from('PK\u0003\u0004 order list').toString('base64'),
      },
    ],
    message_id: ORDER_ID,
  })
  await page.click(`[data-testid="fetch-mailbox-${MAILBOX}"]`)
  const fetched = await page
    .locator('.el-message', { hasText: '封新邮件' })
    .last()
    .innerText({ timeout: 20000 })
    .catch(() => '')
  const session = emailSession(amy.page)
  await session.waitFor({ timeout: 20000 })
  const badge = await waitFor(async () => ((await unreadOf(amy.page)) === '2' ? '2' : null), 15000)
  const itemText = await session.innerText()
  const sessions = await json(`${API}/api/v1/sessions?status=open`, { token: adminToken })
  const chat = sessions.items[0]
  const events = (await json(`${API}/api/v1/sessions/${chat.id}`, { token: adminToken })).events.map(
    (e) => e.type,
  )
  check(
    '立即收取：新邮件直接分配给 Amy（不经过 AI），列表显示"邮件"、主题和未读数 2；之前的邮件没有导入',
    fetched.includes('收到 1 封新邮件') &&
      badge === '2' &&
      itemText.includes(SUBJECT) &&
      sessions.total === 1 &&
      chat.assignee_display_name === 'Amy' &&
      !events.includes('ai_serving'),
    { fetched, badge, itemText, events, total: sessions.total },
  )
  await amy.page.screenshot({ path: `${SHOTS}/3-unread-email-session.png` })

  // 3. 打开会话：邮件、引用、附件、原邮件；未读清零并记在服务器上
  await session.click()
  const email = amy.page.locator('[data-testid="email-message"]').first()
  await email.waitFor({ timeout: 15000 })
  const emailText = await email.innerText()
  const quotedHidden = (await amy.page.locator('[data-testid="email-quoted"]').count()) === 0
  await amy.page.click('[data-testid="email-quoted-toggle"]')
  const quoted = await amy.page.locator('[data-testid="email-quoted"]').innerText()
  const file = await amy.page
    .locator('[data-testid="message-file"]', { hasText: '需求清单.xlsx' })
    .count()
  check(
    '邮件显示主题、发件人、正文，引用的历史内容折叠可展开，附件跟在后面',
    emailText.includes(SUBJECT) &&
      emailText.includes(`王小明 <${CUSTOMER}>`) &&
      emailText.includes('杯身印 logo') &&
      !emailText.includes('写道') &&
      quotedHidden &&
      quoted.includes('写道') &&
      file === 1,
    { emailText, quoted, file },
  )
  await amy.page.click('[data-testid="email-original-button"]')
  const frame = amy.page.frameLocator('[data-testid="email-original"]')
  const originalText = await frame.locator('body').innerText({ timeout: 15000 })
  const sandbox = await amy.page.locator('[data-testid="email-original"]').getAttribute('sandbox')
  check(
    '"查看原邮件"在沙箱里显示原邮件（不允许脚本）',
    originalText.includes('100 个保温杯') && sandbox !== null && !sandbox.includes('allow-scripts'),
    { originalText: originalText.slice(0, 120), sandbox },
  )
  await amy.page.screenshot({ path: `${SHOTS}/4-email-original.png` })
  await amy.page.keyboard.press('Escape')
  await amy.page.locator('[data-testid="email-original"]').waitFor({ state: 'detached' })
  const clearedNow = await unreadOf(amy.page)
  await amy.page.reload()
  await openWorkbench(amy.page)
  await emailSession(amy.page).waitFor()
  await sleep(1000)
  const clearedAfterReload = await unreadOf(amy.page)
  check('打开会话后未读清零，刷新页面后仍是已读', clearedNow === '' && clearedAfterReload === '', {
    clearedNow,
    clearedAfterReload,
  })

  // 4. 回复邮件
  await emailSession(amy.page).click()
  await amy.page.locator('[data-testid="email-message"]').first().waitFor()
  const target = await amy.page.locator('[data-testid="email-reply-target"]').innerText()
  const placeholder = await amy.page
    .locator('input[data-testid="email-subject-input"]')
    .getAttribute('placeholder')
  const reply = '王先生您好：\n100 个保温杯可以做，含印 logo 每个 35 元，月底前可以交货。'
  await amy.page.fill('textarea[data-testid="composer-input"]', reply)
  await amy.page.press('textarea[data-testid="composer-input"]', 'Control+Enter')
  const sent = await waitFor(async () =>
    (await fakeMail('sent')).find((m) => m.rcpt_to.includes(CUSTOMER)),
  )
  const original = (await json(`${API}/api/v1/rooms/${chat.room_id}/messages`, { token: adminToken })).items.find(
    (m) => m.content_type === 'email' && m.direction === 'in',
  )
  check(
    '回复框默认回复最近一封、主题"Re: 原主题"；Ctrl+Enter 发出的邮件带 In-Reply-To、签名和引用',
    target.includes(SUBJECT) &&
      placeholder.includes(`Re: ${SUBJECT}`) &&
      sent?.subject === `Re: ${SUBJECT}` &&
      sent?.mail_from === MAILBOX &&
      sent?.in_reply_to === original?.content.message_id &&
      sent?.text.includes('Acme 客服部') &&
      sent?.text.includes('写道：'),
    { target, placeholder, sent, original: original?.content.message_id },
  )
  await amy.page
    .locator('[data-testid="chat-message"].mine [data-testid="email-message"]')
    .first()
    .waitFor({ timeout: 15000 })
  await amy.page.screenshot({ path: `${SHOTS}/5-email-reply.png` })

  // 5. 客户回信：没在看时未读 1，刷新后还在；客户面板与订单
  await amy.page.reload()
  await openWorkbench(amy.page)
  await emailSession(amy.page).waitFor()
  await deliver({
    subject: `Re: ${SUBJECT}`,
    text: '好的，就按这个价格，请开发票。',
    in_reply_to: sent?.message_id,
    references: `${original?.content.message_id ?? ''} ${sent?.message_id ?? ''}`.trim(),
  })
  await json(`${API}/api/v1/mail/accounts/${account.id}/fetch`, { method: 'POST', token: adminToken })
  const followUp = await waitFor(async () => ((await unreadOf(amy.page)) === '1' ? '1' : null), 25000)
  await amy.page.reload()
  await openWorkbench(amy.page)
  await emailSession(amy.page).waitFor()
  await sleep(1000)
  const kept = await unreadOf(amy.page)
  await emailSession(amy.page).click()
  await amy.page.locator('[data-testid="email-message"]', { hasText: '请开发票' }).waitFor({ timeout: 15000 })
  await sleep(800)
  const opened = await unreadOf(amy.page)
  check('客户回信：没在看时未读 1，刷新页面后还在，打开后清零', followUp === '1' && kept === '1' && opened === '', {
    followUp,
    kept,
    opened,
  })
  const identity = await amy.page.locator('[data-testid="email-identity"]').innerText().catch(() => '')
  const ordersTab = await amy.page.locator('[data-testid="orders-tab"]').count()
  check('客户面板显示邮件身份（发件人名称），可以在会话里下单', identity.includes('王小明') && ordersTab === 1, {
    identity,
    ordersTab,
  })
  await amy.page.screenshot({ path: `${SHOTS}/6-follow-up.png` })

  // 6. 停用邮箱后不能回复
  await page.click(`[data-testid="toggle-mailbox-${MAILBOX}"]`)
  await page.locator('.el-message-box__btns button', { hasText: '停用' }).click()
  await row.locator('[data-testid="mailbox-status"]', { hasText: '已停用' }).waitFor({ timeout: 15000 })
  await amy.page.fill('textarea[data-testid="composer-input"]', '还能收到吗？')
  await amy.page.click('[data-testid="send-button"]')
  const failed = await amy.page
    .locator('[data-testid="send-failed"]')
    .last()
    .innerText({ timeout: 15000 })
    .catch(() => '')
  check('停用邮箱后不能回复：提示"邮箱已停用，不能回复"', failed.includes('邮箱已停用，不能回复'), failed)
  await page.screenshot({ path: `${SHOTS}/7-mailbox-disabled.png` })
  await amy.page.screenshot({ path: `${SHOTS}/8-reply-blocked.png` })

  check('没有前端脚本错误', summary.consoleErrors.length === 0, summary.consoleErrors)
  for (const ctx of [admin.ctx, amy.ctx]) await ctx.close()
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
