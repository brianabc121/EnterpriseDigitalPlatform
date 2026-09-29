// P4 验收：从会话沉淀知识的闭环在浏览器里走通。
//
// 1. 访客与坐席小艾进行四段对话（周末发货、增值税专票、发货时效的新说法、闲聊），小艾结束会话；
//    执行知识提炼（平时由调度进程每小时执行）。
// 2. 管理员在"知识库 → 审核台"看到新问题、知识缺口、答案冲突：编辑后通过新问题、补充缺口的答案、
//    对比差异后用新答案更新原问答、驳回闲聊；AI 立即能用新知识回答。
// 3. 版本历史里把原问答恢复到 v1；把它设为必读，小艾在工作台"动态"里确认已读，管理员看到确认情况。
// 4. 小艾在工作台评价知识；管理员查看运营数据和本周周报。
//
// 前置与 p3-ai-acceptance.cjs 相同（后端、实时消费进程、调度进程接到大模型，控制台、Widget、OpenIM）。
// 提炼默认执行 `cd backend && uv run python -m app.cli kb-extract --tenant <租户>`（EXTRACT_CMD 可替换），
// 命令的环境变量需要同样接到大模型。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p4-knowledge-acceptance.cjs
const { chromium } = require('playwright')
const { execSync } = require('child_process')
const fs = require('fs')
const path = require('path')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const WIDGET = env('WIDGET_URL', 'http://localhost:5175')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p4')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `kl-${RUN}`
const PASSWORD = 'demo-pass-2026'
const ROOT = path.resolve(__dirname, '../..')
const EXTRACT_CMD = env(
  'EXTRACT_CMD',
  `cd backend && uv run python -m app.cli kb-extract --tenant ${TENANT}`,
)

const ANSWER = '一般 2 到 3 天送达，偏远地区 5 到 7 天。'
const CONVERSATIONS = [
  ['你们周末发货吗', '周末正常发货，周日下午 4 点前的订单当天发出。'],
  ['可以开增值税专用发票吗', '这个我帮您问一下，稍后回复您。'],
  ['订单发货后多久能到？', '现在一般 1 到 2 天送达。'],
  ['你们老板是谁', '这个不方便透露哦。'],
]

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
  const data = await json(`${API}/api/v1/auth/login`, {
    method: 'POST',
    body: { tenant_code: TENANT, username, password: PASSWORD },
  })
  return data.access_token
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
      name: `知识闭环验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  const admin = await login('admin')
  await json(`${API}/api/v1/staff`, {
    method: 'POST',
    token: admin,
    body: { username: 'alice', display_name: '小艾', password: PASSWORD, role_codes: ['agent'] },
  })
  const faq = await json(`${API}/api/v1/kb/items`, {
    method: 'POST',
    token: admin,
    body: { title: '订单发货后多久能到？', content: ANSWER, publish: true },
  })
  const channels = await json(`${API}/api/v1/channels`, { token: admin })
  return { admin, faq, channel: channels.items[0] }
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

async function openVisitor(browser, channelKey, label) {
  const ctx = await browser.newContext({ viewport: { width: 420, height: 720 }, locale: 'zh-CN' })
  const page = await ctx.newPage()
  watchErrors(page, label)
  await page.goto(`${WIDGET}/?key=${encodeURIComponent(channelKey)}`)
  await page
    .locator('[data-testid="widget-state"]', { hasText: '在线' })
    .waitFor({ timeout: 15000 })
  return { ctx, page }
}

const menu = (page, title) =>
  page.locator('[data-testid="main-menu"] .el-menu-item', { hasText: title }).click()
const tab = (page, title) =>
  page.locator('.el-tabs__item:visible', { hasText: title }).first().click()
const settle = (page) => page.waitForTimeout(600)
const tileValue = (page, testid) =>
  page
    .locator(`[data-testid="${testid}"] .value`)
    .innerText()
    .then((t) => t.trim())

async function waitFor(fn, timeout = 15000) {
  const deadline = Date.now() + timeout
  for (;;) {
    const value = await fn()
    if (value || Date.now() > deadline) return value
    await new Promise((r) => setTimeout(r, 500))
  }
}

/** 访客提问，小艾在工作台回复并结束会话。 */
async function converse(browser, alice, channelKey, question, answer, index) {
  const visitor = await openVisitor(browser, channelKey, `visitor-${index}`)
  await visitor.page.fill('[data-testid="message-input"]', question)
  await visitor.page.click('[data-testid="send-button"]')
  const item = alice.page.locator('[data-testid="session-item"]').first()
  await item.waitFor({ timeout: 20000 })
  await item.click()
  await alice.page
    .locator('[data-testid="chat-message"]', { hasText: question })
    .waitFor({ timeout: 15000 })
  await alice.page.fill('textarea[data-testid="composer-input"]', answer)
  await alice.page.click('[data-testid="send-button"]')
  await visitor.page
    .locator('[data-testid="message"].agent', { hasText: answer.slice(0, 6) })
    .waitFor({ timeout: 15000 })
  await alice.page.click('[data-testid="close-session"]')
  await alice.page.locator('.el-message-box button', { hasText: '结束' }).click()
  await alice.page.locator('footer.readonly', { hasText: '会话已结束' }).waitFor()
  await visitor.ctx.close()
}

/** 审核操作完成后抽屉关闭、列表刷新。 */
const reviewed = (page) =>
  page.locator('[data-testid="candidate-drawer"]').waitFor({ state: 'hidden', timeout: 15000 })

async function openCandidate(page, question) {
  await page
    .locator('[data-testid="candidates-table"] .el-table__row', { hasText: question })
    .locator('[data-testid="review-candidate"]')
    .click()
  const drawer = page.locator('[data-testid="candidate-drawer"]')
  await drawer.locator('textarea[data-testid="candidate-answer"]').waitFor()
  return drawer
}

async function run(browser) {
  const { admin: adminToken, faq, channel } = await prepareTenant()

  // 1. 四段人工对话，然后提炼
  const alice = await consoleLogin(browser, 'alice')
  await menu(alice.page, '工作台')
  await alice.page
    .locator('[data-testid="agent-status"]', { hasText: '在线' })
    .waitFor({ timeout: 15000 })
  for (const [i, [question, answer]] of CONVERSATIONS.entries()) {
    await converse(browser, alice, channel.public_key, question, answer, i + 1)
  }
  const output = execSync(EXTRACT_CMD, { cwd: ROOT, stdio: 'pipe', shell: '/bin/bash' }).toString()
  const report = JSON.parse(output.trim().split('\n').pop())
  check(
    '提炼 4 个会话：3 个问答、1 个缺口',
    report.sessions === 4 && report.pairs === 3 && report.gaps === 1,
    report,
  )

  // 2. 审核台
  const admin = await consoleLogin(browser, 'admin')
  const page = admin.page
  await menu(page, '知识库')
  await tab(page, '审核台')
  await page.locator('[data-testid="candidates-table"] .el-table__row').first().waitFor()
  const desk = await page.locator('[data-testid="candidates-table"]').innerText()
  const counts = await page.locator('[data-testid="candidate-kind-filter"]').innerText()
  check(
    '审核台列出新问题、知识缺口和答案冲突',
    desk.includes('你们周末发货吗') &&
      desk.includes('可以开增值税专用发票吗') &&
      desk.includes('现在一般 1 到 2 天送达') &&
      counts.includes('全部 4'),
    { desk: desk.slice(0, 300), counts },
  )
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/1-review-desk.png` })

  // 新问题：编辑答案后通过
  let drawer = await openCandidate(page, '你们周末发货吗')
  await drawer
    .locator('textarea[data-testid="candidate-answer"]')
    .fill('周末正常发货，周日 16 点前的订单当天发出。')
  const evidence = await drawer.locator('[data-testid="evidence"]').first().innerText()
  await page.click('[data-testid="approve-candidate"]')
  await reviewed(page)
  check(
    '证据对话显示客户与坐席的原话',
    evidence.includes('客户：你们周末发货吗') && evidence.includes('坐席：'),
    evidence,
  )

  // 冲突：查看差异后用新答案更新原问答
  drawer = await openCandidate(page, '现在一般 1 到 2 天送达')
  const diff = drawer.locator('[data-testid="candidate-diff"]')
  const removed = await diff.locator('.removed').allInnerTexts()
  const added = await diff.locator('.added').allInnerTexts()
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/2-conflict-diff.png` })
  await page.click('[data-testid="approve-candidate"]')
  await reviewed(page)
  const updated = await json(`${API}/api/v1/kb/items/${faq.id}`, { token: adminToken })
  check(
    '冲突：高亮新旧答案的差异，更新后原问答升为 v2',
    removed.join('').includes('2') &&
      added.join('').includes('1') &&
      updated.version === 2 &&
      updated.content === '现在一般 1 到 2 天送达。',
    { removed, added, version: updated.version },
  )

  // 缺口：补充答案后发布
  drawer = await openCandidate(page, '可以开增值税专用发票吗')
  await drawer
    .locator('textarea[data-testid="candidate-answer"]')
    .fill('可以开具增值税专用发票，请在下单时填写税号。')
  await page.click('[data-testid="approve-candidate"]')
  await reviewed(page)

  // 闲聊：驳回（填写理由）
  drawer = await openCandidate(page, '你们老板是谁')
  await page.click('[data-testid="reject-candidate"]')
  await page.locator('.el-message-box input').fill('与业务无关')
  await page.locator('.el-message-box button', { hasText: '驳回' }).click()
  await reviewed(page)
  const left = await json(`${API}/api/v1/kb/candidates`, { token: adminToken })
  const rejected = await json(`${API}/api/v1/kb/candidates?status=rejected`, { token: adminToken })
  check(
    '审核完成：待审核清空，驳回理由已记录',
    left.total === 0 && rejected.items[0]?.review_note === '与业务无关',
    { left: left.total, rejected: rejected.items.map((c) => c.review_note) },
  )

  // AI 立即能用新知识
  await json(`${API}/api/v1/ai/settings`, {
    method: 'PUT',
    token: adminToken,
    body: { enabled: true },
  })
  const answer = await json(`${API}/api/v1/ai/test`, {
    method: 'POST',
    token: adminToken,
    body: { question: '周末发货吗' },
  })
  check(
    '审核通过的知识 AI 立即可用',
    answer.action === 'reply' && answer.reply.includes('周日 16 点'),
    answer,
  )

  // 3. 版本历史：恢复到 v1；设为必读
  await tab(page, '知识条目')
  await page
    .locator('[data-testid="kb-table"] .el-table__row', { hasText: '订单发货后多久能到' })
    .click()
  await page.click('[data-testid="kb-open-versions"]')
  const versions = page.locator('[data-testid="kb-versions"]')
  await versions.locator('.el-timeline-item').nth(1).waitFor()
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/3-versions.png` })
  await versions.locator('[data-testid="restore-version"]').last().click()
  await page.locator('.el-message-box button', { hasText: '恢复' }).click()
  await page.locator('.el-message--success', { hasText: '已恢复为 v1' }).last().waitFor()
  const restored = await json(`${API}/api/v1/kb/items/${faq.id}`, { token: adminToken })
  check(
    '恢复历史版本：内容回到 v1，生成新版本 v3',
    restored.version === 3 && restored.content === ANSWER,
    {
      version: restored.version,
      content: restored.content,
    },
  )

  await page
    .locator('[data-testid="kb-table"] .el-table__row', { hasText: '订单发货后多久能到' })
    .click()
  await page.locator('.el-drawer .el-checkbox', { hasText: '必读' }).click()
  await page.click('[data-testid="kb-save-publish"]')
  await page.locator('.el-message--success', { hasText: '已保存并生效' }).last().waitFor()

  // 小艾在工作台"动态"里确认已读
  await alice.page.reload()
  await alice.page
    .locator('[data-testid="feed-tab"] .el-badge__content', { hasText: '1' })
    .waitFor({ timeout: 20000 })
  await alice.page.click('[data-testid="feed-tab"]')
  const mustRead = await alice.page.locator('[data-testid="must-read-item"]').innerText()
  await settle(alice.page)
  await alice.page.screenshot({ path: `${SHOTS}/4-workbench-feed.png` })
  await alice.page.click('[data-testid="confirm-read"]')
  await waitFor(
    async () => (await alice.page.locator('[data-testid="must-read-item"]').count()) === 0,
  )
  check(
    '必读知识出现在坐席的知识动态里，确认后消失',
    mustRead.includes('订单发货后多久能到'),
    mustRead,
  )

  await page
    .locator('[data-testid="kb-table"] .el-table__row', { hasText: '订单发货后多久能到' })
    .click()
  const stats = page.locator('[data-testid="kb-read-stats"]')
  await stats.waitFor()
  const statsText = await stats.innerText()
  check('管理员看到必读确认情况：1 / 2 人', statsText.includes('已确认 1 / 2'), statsText)
  await page.locator('.el-drawer__close-btn:visible').click()
  await page.locator('.el-overlay.is-drawer').first().waitFor({ state: 'hidden' })

  // 4. 小艾评价知识；运营数据与周报
  await alice.page.locator('.side-tabs .el-tabs__item', { hasText: '知识库' }).click()
  await alice.page.fill('input[data-testid="kb-search-input"]', '周末发货吗')
  await alice.page.click('[data-testid="kb-search-button"]')
  await alice.page.locator('[data-testid="kb-like"]').first().click()
  await alice.page.locator('.el-message--success', { hasText: '谢谢反馈' }).last().waitFor()

  await tab(page, '运营数据')
  await page.locator('[data-testid="kb-pass-rate"]').waitFor()
  const passRate = await tileValue(page, 'kb-pass-rate')
  const gaps = await page.locator('[data-testid="kb-gaps"]').innerText()
  check(
    '运营数据：候选通过率 75%，缺口已处理 1 个',
    passRate === '75%' && gaps.includes('已处理 1'),
    { passRate, gaps },
  )
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/5-metrics.png`, fullPage: true })

  await tab(page, '周报')
  await page.click('[data-testid="generate-digest"]')
  await page.locator('[data-testid="digest-new"] li', { hasText: '你们周末发货吗' }).waitFor()
  const digest = await page.locator('[data-testid="kb-digest"]').innerText()
  check(
    '周报：列出本周新增与更新的知识和审核情况',
    digest.includes('可以开增值税专用发票吗') &&
      digest.includes('订单发货后多久能到') &&
      digest.includes('本周处理'),
    digest.slice(0, 400),
  )
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/6-digest.png`, fullPage: true })

  const liked = await json(`${API}/api/v1/kb/items?q=${encodeURIComponent('周末发货')}`, {
    token: adminToken,
  })
  check(
    '坐席评价计入知识的"有用"数',
    liked.items[0]?.likes === 1,
    liked.items.map((i) => [i.title, i.likes]),
  )

  check('没有前端脚本错误', summary.consoleErrors.length === 0, summary.consoleErrors)
  for (const ctx of [admin.ctx, alice.ctx]) await ctx.close()
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
