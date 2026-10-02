// P3 验收：知识库、AI 接待与自主转人工、坐席助手，在浏览器里走通。
//
// 1. 管理员在"知识库"新建问答并发布、批量导入两条问答、做检索测试；把默认路由策略改为"AI 优先"；
//    在"AI 接待"里启用 AI、设置名称和转人工关键词，用"试一试"和"评测"检验效果。
// 2. 访客咨询：AI（小智）依据知识库回答；访客说"我要转人工"，AI 生成交接摘要后转给在线的小艾。
// 3. 小艾在工作台看到转人工原因和摘要；用"AI 建议"和知识库检索回复访客。
// 4. 会话记录里有每一轮 AI 判定；报表显示 AI 接待数据；运营后台设置租户的 AI 回复额度。
//
// 前置：后端、实时消费进程、调度进程接到模拟大模型（或真实大模型）：
//   cd backend && uv run python -m tests.fake_llm --port 8900
//   EDP_LLM_BASE_URL=http://127.0.0.1:8900/v1 EDP_LLM_CHAT_MODEL=fake-chat EDP_LLM_EMBED_MODEL=fake-embed
// 其余与 m4-workbench-acceptance.cjs 相同（控制台、运营后台、Widget、OpenIM）。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p3-ai-acceptance.cjs
const { chromium } = require('playwright')
const fs = require('fs')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PLATFORM = env('PLATFORM_URL', 'http://localhost:5174')
const WIDGET = env('WIDGET_URL', 'http://localhost:5175')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p3')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `ai-${RUN}`
const PASSWORD = 'demo-pass-2026'

const ANSWER = '一般 2 到 3 天送达，偏远地区 5 到 7 天。'
const PAYMENT = '支持微信、支付宝和银行卡。'
const INVOICE = '下单时填写发票抬头，发货后 3 天内开具电子发票。'
const IMPORT_CSV = [
  '标准问,答案,相似问,分类',
  `支持哪些付款方式？,${PAYMENT},怎么付款|可以用什么支付,支付`,
  `发票怎么开？,${INVOICE},能开发票吗|电子发票,发票`,
].join('\n')

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
  const tenant = await json(`${API}/platform/v1/tenants`, {
    method: 'POST',
    token: platform.access_token,
    body: {
      code: TENANT,
      name: `AI 验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  const admin = await login('admin')
  await json(`${API}/api/v1/staff`, {
    method: 'POST',
    token: admin,
    body: { username: 'alice', display_name: '小艾', password: PASSWORD, role_codes: ['agent'] },
  })
  const channels = await json(`${API}/api/v1/channels`, { token: admin })
  return { admin, tenant, channel: channels.items[0] }
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

async function openVisitor(browser, channelKey) {
  const ctx = await browser.newContext({ viewport: { width: 420, height: 720 }, locale: 'zh-CN' })
  const page = await ctx.newPage()
  watchErrors(page, 'visitor')
  await page.goto(`${WIDGET}/?key=${encodeURIComponent(channelKey)}`)
  await page
    .locator('[data-testid="widget-state"]', { hasText: '在线' })
    .waitFor({ timeout: 15000 })
  return { ctx, page }
}

async function say(visitor, text) {
  await visitor.page.fill('[data-testid="message-input"]', text)
  await visitor.page.click('[data-testid="send-button"]')
}

// 按开头匹配菜单名："报表"不能点到"盈利报表"。
const menu = (page, title) =>
  page.locator('[data-testid="main-menu"] .el-menu-item', { hasText: new RegExp('^\\s*' + title) }).click()
const tab = (page, title) => page.locator('.el-tabs__item:visible', { hasText: title }).click()
const rowIn = (page, table, text) =>
  page.locator(`[data-testid="${table}"] .el-table__row`, { hasText: text }).first().waitFor()
/** 截图前等弹窗、抽屉的动画结束。 */
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

/** 逐个输入标签：回车后输入框清空才算加上，再输入下一个（偶尔回车没生效时再按一次）。 */
async function addTags(page, testid, values) {
  const input = page.locator(`input[data-testid="${testid}"]`)
  for (const value of values) {
    await input.fill(value)
    for (let attempt = 0; attempt < 3; attempt += 1) {
      await input.press('Enter')
      if (await waitFor(async () => (await input.inputValue()) === '', 2000)) break
    }
  }
}

async function run(browser) {
  const { admin: adminToken, tenant, channel } = await prepareTenant()
  const admin = await consoleLogin(browser, 'admin')
  const page = admin.page

  // 1. 知识库：新建问答并发布
  await menu(page, '知识库')
  await page.click('[data-testid="kb-create"]')
  await page.locator('.el-dropdown-menu__item:visible', { hasText: '问答' }).click()
  await page.fill('input[data-testid="kb-title"]', '订单发货后多久能到？')
  await addTags(page, 'kb-questions', ['快递几天能到', '多久能收到货'])
  await page.fill('textarea[data-testid="kb-content"]', ANSWER)
  await page.click('[data-testid="kb-save-publish"]')
  await rowIn(page, 'kb-table', '订单发货后多久能到')
  const created = await page
    .locator('[data-testid="kb-table"] .el-table__row', { hasText: '订单发货后多久能到' })
    .innerText()
  check('新建问答并发布', created.includes('已发布') && created.includes('+2 个问法'), created)

  // 2. 批量导入两条问答并发布
  await page.click('[data-testid="kb-open-import"]')
  await page.fill('textarea[data-testid="kb-import-text"]', IMPORT_CSV)
  await page.locator('.el-dialog:visible .el-checkbox', { hasText: '导入后立即发布' }).click()
  await page.click('[data-testid="kb-import-submit"]')
  const imported = await page.locator('[data-testid="kb-import-result"]').innerText()
  await page.locator('.el-dialog:visible button', { hasText: '关闭' }).click()
  await rowIn(page, 'kb-table', '发票怎么开')
  const rows = await page.locator('[data-testid="kb-table"] .el-table__row').count()
  check('CSV 批量导入 2 条', imported.includes('导入 2 条') && rows === 3, { imported, rows })
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/1-knowledge.png` })

  // 3. 检索测试：换一种问法也能找到
  await page.click('[data-testid="kb-open-search"]')
  await page.fill('input[data-testid="kb-search-input"]', '快递大概几天到')
  await page.click('[data-testid="kb-search-button"]')
  const firstHit = page.locator('[data-testid="kb-hit"]').first()
  await firstHit.waitFor()
  const hitText = await firstHit.innerText()
  check('检索测试：换个问法找到对应问答', hitText.includes('订单发货后多久能到'), hitText)
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/2-kb-search.png` })
  await page.keyboard.press('Escape')

  // 4. 默认路由策略改为 AI 优先
  await menu(page, '设置')
  await tab(page, '路由策略')
  const defaultRow = page.locator('[data-testid="policies-table"] .el-table__row', {
    hasText: '默认',
  })
  await defaultRow.waitFor()
  await defaultRow.locator('button', { hasText: '编辑' }).click()
  await page.locator('[data-testid="policy-form"] .el-radio', { hasText: 'AI 优先' }).click()
  await page.click('[data-testid="save-policy"]')
  await page
    .locator('[data-testid="policies-table"] .el-table__row', { hasText: 'AI 优先' })
    .waitFor()
  check('默认路由策略改为 AI 优先', true)

  // 5. AI 接待设置
  await menu(page, 'AI 接待')
  await page.locator('[data-testid="ai-settings"]').waitFor()
  await page.locator('[data-testid="ai-settings"] .el-switch').first().click()
  await page.fill('input[data-testid="ai-bot-name"]', '小智')
  await addTags(page, 'ai-handoff-keywords', ['找经理'])
  await page.click('[data-testid="ai-save"]')
  // 不能只等"已保存"提示：上一步保存路由策略的提示可能还没消失。
  const aiSettings = await waitFor(async () => {
    const current = await json(`${API}/api/v1/ai/settings`, { token: adminToken })
    return current.enabled ? current : null
  })
  check(
    '启用 AI 接待，名称"小智"，转人工关键词"找经理"',
    aiSettings?.bot_name === '小智' &&
      aiSettings.handoff_keywords.includes('找经理') &&
      aiSettings.llm_configured,
    aiSettings,
  )
  await page.screenshot({ path: `${SHOTS}/3-ai-settings.png`, fullPage: true })

  // 6. 试一试：能回答的问题直接回复，说关键词的转人工
  await tab(page, '试一试')
  for (const question of ['快递几天能到', '我要找经理']) {
    await page.fill('input[data-testid="ai-test-input"]', question)
    await page.click('[data-testid="ai-test-ask"]')
    await page.locator('[data-testid="ai-outcome"]', { hasText: question }).waitFor()
  }
  const [managerCard, answerCard] = await page.locator('[data-testid="ai-outcome"]').allInnerTexts()
  check(
    '试一试：依据知识回答；说"找经理"转人工',
    answerCard.includes('回复') &&
      answerCard.includes('2 到 3 天') &&
      answerCard.includes('订单发货后多久能到') &&
      managerCard.includes('转人工') &&
      managerCard.includes('客户要求人工'),
    { answerCard, managerCard },
  )
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/4-ai-test.png` })

  // 7. 评测：默认样例两题，回答与转人工都正确
  await tab(page, '评测')
  await page.click('[data-testid="ai-eval-run"]')
  await page.locator('[data-testid="ai-eval-results"] .el-table__row').first().waitFor()
  const evalRows = await page.locator('[data-testid="ai-eval-results"]').innerText()
  await page.keyboard.press('Escape')
  const runs = await page.locator('[data-testid="ai-eval-runs"] .el-table__row').first().innerText()
  check(
    '评测：回答正确率与转人工正确率 100%',
    runs.includes('100%') && !evalRows.includes('错误'),
    { runs, evalRows },
  )
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/5-ai-eval.png` })

  // 8. 小艾上线；访客咨询，AI 回答
  const alice = await consoleLogin(browser, 'alice')
  await menu(alice.page, '工作台')
  await alice.page
    .locator('[data-testid="agent-status"]', { hasText: '在线' })
    .waitFor({ timeout: 15000 })
  const visitor = await openVisitor(browser, channel.public_key)
  await say(visitor, '请问快递几天能到')
  const botReply = visitor.page.locator('[data-testid="message"].bot', { hasText: '2 到 3 天' })
  await botReply.waitFor({ timeout: 20000 })
  const botText = await botReply.innerText()
  const banner = await visitor.page.locator('[data-testid="service-banner"]').innerText()
  const aiSessions = await json(`${API}/api/v1/sessions?status=ai_serving`, { token: adminToken })
  check(
    'AI 依据知识库回答访客（显示"小智"和 AI 标识），坐席不被打扰',
    botText.includes('小智') &&
      botText.includes('AI') &&
      banner.includes('智能客服为您服务') &&
      aiSessions.total === 1 &&
      (await alice.page.locator('[data-testid="session-item"]').count()) === 0,
    { botText, banner, total: aiSessions.total },
  )
  await visitor.page.screenshot({ path: `${SHOTS}/6-visitor-ai.png` })

  // 9. 访客要求人工：AI 写交接摘要后转给小艾
  await say(visitor, '我要转人工')
  await alice.page.locator('[data-testid="session-item"]').first().waitFor({ timeout: 20000 })
  await alice.page.locator('[data-testid="session-item"]').first().click()
  const summaryBox = alice.page.locator('[data-testid="ai-summary"]')
  await summaryBox.waitFor()
  const handoffText = await summaryBox.innerText()
  check(
    '转人工：坐席看到原因和 AI 交接摘要',
    handoffText.includes('客户要求人工') && handoffText.includes('客户咨询'),
    handoffText,
  )
  await visitor.page
    .locator('[data-testid="service-banner"]', { hasText: '小艾' })
    .waitFor({ timeout: 20000 })

  // 10. 坐席助手：访客再问，小艾用"AI 建议"回复
  await say(visitor, '支持哪些付款方式')
  await alice.page
    .locator('[data-testid="chat-message"]', { hasText: '支持哪些付款方式' })
    .waitFor({ timeout: 15000 })
  await alice.page.click('[data-testid="suggest-button"]')
  const suggestion = alice.page.locator('[data-testid="suggestion"]').first()
  await suggestion.waitFor()
  const suggestionText = await suggestion.innerText()
  await settle(alice.page)
  await alice.page.screenshot({ path: `${SHOTS}/7-workbench-copilot.png` })
  await suggestion.click()
  const draft = await alice.page.locator('textarea[data-testid="composer-input"]').inputValue()
  await alice.page.click('[data-testid="send-button"]')
  await visitor.page
    .locator('[data-testid="message"].agent', { hasText: '支付宝' })
    .waitFor({ timeout: 15000 })
  check(
    'AI 建议：给出知识库答案，坐席一键使用并发送',
    suggestionText.includes(PAYMENT) && draft === suggestionText.trim(),
    { suggestionText, draft },
  )

  // 11. 知识库检索：插入回复框后发送
  await alice.page.locator('.side-tabs .el-tabs__item', { hasText: '知识库' }).click()
  await alice.page.fill('input[data-testid="kb-search-input"]', '怎么开发票')
  await alice.page.click('[data-testid="kb-search-button"]')
  await alice.page.locator('[data-testid="kb-insert"]').first().click()
  const inserted = await alice.page.locator('textarea[data-testid="composer-input"]').inputValue()
  await alice.page.click('[data-testid="send-button"]')
  await visitor.page
    .locator('[data-testid="message"].agent', { hasText: '电子发票' })
    .waitFor({ timeout: 15000 })
  check('工作台知识检索：插入回复框并发送', inserted === INVOICE, inserted)
  await settle(alice.page)
  await alice.page.screenshot({ path: `${SHOTS}/8-workbench-kb.png` })
  await visitor.page.screenshot({ path: `${SHOTS}/9-visitor-after.png` })

  // 12. 会话记录：AI 判定过程
  await menu(page, '会话记录')
  await page.locator('[data-testid="sessions-table"] .el-table__row').first().click()
  const decisions = page.locator('[data-testid="ai-decisions"]')
  await decisions.waitFor({ timeout: 15000 })
  const decisionText = await decisions.innerText()
  const drawerSummary = await page.locator('[data-testid="drawer-ai-summary"]').innerText()
  check(
    '会话记录：每轮 AI 判定（回复与转人工）和交接摘要',
    decisionText.includes('回复') &&
      decisionText.includes('转人工') &&
      decisionText.includes('订单发货后多久能到') &&
      drawerSummary.includes('客户咨询'),
    { decisionText: decisionText.slice(0, 300), drawerSummary },
  )
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/10-session-decisions.png`, fullPage: true })
  await page.keyboard.press('Escape')

  // 13. 报表：AI 接待 1 个会话、转人工 1 个
  await menu(page, '报表')
  await page.locator('[data-testid="tile-ai-sessions"]').waitFor()
  const aiTile = await tileValue(page, 'tile-ai-sessions')
  const aiHint = await page.locator('[data-testid="tile-ai-sessions"]').innerText()
  check(
    '报表：AI 接待 1 个会话、转人工 1 个',
    aiTile === '1' && aiHint.includes('转人工 1'),
    aiHint,
  )
  await page.screenshot({ path: `${SHOTS}/11-reports.png`, fullPage: true })

  // 14. 运营后台：设置租户的 AI 回复额度，控制台显示本月用量
  const ops = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' })
  const opsPage = await ops.newPage()
  watchErrors(opsPage, 'platform')
  await opsPage.goto(`${PLATFORM}/login`)
  await opsPage.fill('input[autocomplete="username"]', PLATFORM_USER)
  await opsPage.fill('input[autocomplete="current-password"]', PLATFORM_PASSWORD)
  await opsPage.click('button:has-text("登录")')
  const row = opsPage.locator('[data-testid="tenant-table"] .el-table__row', { hasText: TENANT })
  await row.waitFor({ timeout: 15000 })
  await row.locator('[data-testid="tenant-quota-button"]').click()
  await opsPage.locator('.el-dialog:visible .el-checkbox', { hasText: '不限' }).click()
  const quotaInput = opsPage.locator(
    'input[data-testid="quota-value"], [data-testid="quota-value"] input',
  )
  await quotaInput.fill('1000')
  // 回车提交表单即保存（之前会触发浏览器原生提交、刷新页面）。
  await quotaInput.press('Enter')
  await opsPage.locator('.el-message--success', { hasText: '已保存' }).waitFor()
  await row.locator('td', { hasText: '1,000 条/月' }).waitFor()
  await settle(opsPage)
  await opsPage.screenshot({ path: `${SHOTS}/12-platform-quota.png` })
  await menu(page, 'AI 接待')
  const quotaText = await page.locator('[data-testid="ai-quota"]').innerText()
  check('运营后台设置 AI 额度 1000，控制台显示本月已用 1 条', /1\s*\/\s*1,000/.test(quotaText), {
    quotaText,
    tenant: tenant.id,
  })

  check('没有前端脚本错误', summary.consoleErrors.length === 0, summary.consoleErrors)
  for (const ctx of [admin.ctx, alice.ctx, visitor.ctx, ops]) await ctx.close()
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
