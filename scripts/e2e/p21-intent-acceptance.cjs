// P21 验收：AI 意图判断（设计文档 §32），在浏览器里走通。
//
// 1. 运营后台添加判断模型供应商（TypeSafe Jev，接到模拟服务），检查连通，把"意图判断"路由给它；
//    其他场景选不到判断模型。
// 2. 管理员在"AI 接待"里看到平台的判断模型，加一个自定义意图"定制尺寸"，设置"准备下单时"转人工；
//    "试一试"同时显示这个问题的意图判断。
// 3. 人工接待：访客问价格 → 坐席的意图卡片显示"有兴趣 · 询价比价 · 在意价格"；问定制 → 真实意图是
//    自定义的"定制尺寸"；说要买并给出地址 → "准备下单"、会话列表标签、坐席助手提醒；详情里有分布、
//    这次会话的变化和依据；"生成订单"打开右侧订单的 AI 预填。
// 4. AI 接待（默认策略改为 AI 优先）：AI 回复参考判断（会话记录里的判定带着意图判断）；换种说法要人工
//    时转人工；客户准备下单时转人工，坐席看到原因、标签和意图卡片。
// 5. 运营后台的大模型用量按"意图判断"统计。结束时删除判断模型供应商（路由一并去掉）。
//
// 前置：与 p3-ai-acceptance.cjs 相同（后端、实时消费进程接到模拟大模型 :8900，它也模拟判断模型的
// /v1/systemone）。运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<密码> node scripts/e2e/p21-intent-acceptance.cjs
const { chromium } = require('playwright')
const fs = require('fs')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PLATFORM = env('PLATFORM_URL', 'http://localhost:5174')
const WIDGET = env('WIDGET_URL', 'http://localhost:5175')
const FAKE_LLM = env('FAKE_LLM_URL', 'http://127.0.0.1:8900/v1')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p21')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `intent-${RUN}`
const PASSWORD = 'demo-pass-2026'
const PROVIDER = `Jev-${RUN}`
const ANSWER = '一般 2 到 3 天送达，偏远地区 5 到 7 天。'

const summary = { tenant: TENANT, checks: [], consoleErrors: [] }
const check = (name, ok, detail) =>
  summary.checks.push(
    `${ok ? 'PASS' : 'FAIL'} ${name}${ok || detail === undefined ? '' : ` -> ${JSON.stringify(detail)}`}`,
  )
const state = { opsToken: null, providerId: null }

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
  state.opsToken = platform.access_token
  await json(`${API}/platform/v1/tenants`, {
    method: 'POST',
    token: platform.access_token,
    body: {
      code: TENANT,
      name: `意图验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  const admin = await login('admin')
  await json(`${API}/api/v1/staff`, {
    method: 'POST',
    token: admin,
    body: { username: 'alice', display_name: '小艾', password: PASSWORD, role_codes: ['agent'] },
  })
  await json(`${API}/api/v1/kb/items`, {
    method: 'POST',
    token: admin,
    body: {
      title: '订单发货后多久能到？',
      content: ANSWER,
      questions: ['快递几天能到'],
      publish: true,
    },
  })
  const channels = await json(`${API}/api/v1/channels`, { token: admin })
  return { admin, channel: channels.items[0] }
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
/** 截图前等弹窗、抽屉的动画结束。 */
const settle = (page) => page.waitForTimeout(600)
const text = (locator) => locator.innerText().then((t) => t.trim())

async function waitFor(fn, timeout = 15000) {
  const deadline = Date.now() + timeout
  for (;;) {
    const value = await fn().catch(() => null)
    if (value || Date.now() > deadline) return value
    await new Promise((r) => setTimeout(r, 500))
  }
}

async function selectOption(page, testid, label) {
  await page.locator(`[data-testid="${testid}"]`).click()
  const dropdown = page.locator('.el-select-dropdown:visible')
  await dropdown.locator('.el-select-dropdown__item', { hasText: label }).first().click()
}

async function platform(browser) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' })
  const page = await ctx.newPage()
  watchErrors(page, 'platform')
  await page.goto(`${PLATFORM}/login`)
  await page.fill('input[autocomplete="username"]', PLATFORM_USER)
  await page.fill('input[autocomplete="current-password"]', PLATFORM_PASSWORD)
  await page.click('button:has-text("登录")')
  await page.locator('[data-testid="menu-providers"]').waitFor({ timeout: 15000 })
  return { ctx, page }
}

async function run(browser) {
  const { admin: adminToken, channel } = await prepareTenant()

  // ---- 1. 运营后台：判断模型供应商与"意图判断"路由 ----
  const ops = await platform(browser)
  await ops.page.locator('[data-testid="menu-providers"]').click()
  await ops.page.locator('[data-testid="provider-create"]').click()
  await ops.page.locator('[data-testid="provider-protocol-typesafe"]').click()
  const formValues = async () => ({
    url: await ops.page.locator('input[data-testid="provider-url"]').inputValue(),
    model: await ops.page.locator('input[data-testid="provider-chat-model"]').inputValue(),
  })
  const prefilled = await formValues()
  // 切回 OpenAI 兼容时去掉没改过的默认值，再切回来又填上。
  await ops.page.locator('[data-testid="provider-protocol-openai"]').click()
  const switchedBack = await formValues()
  await ops.page.locator('[data-testid="provider-protocol-typesafe"]').click()
  await ops.page.locator('input[data-testid="provider-name"]').fill(PROVIDER)
  await ops.page.locator('input[data-testid="provider-url"]').fill(FAKE_LLM)
  await ops.page.locator('input[data-testid="provider-key"]').fill('sk-jev-e2e')
  await ops.page.locator('input[data-testid="provider-chat-model"]').fill('jev-1.13.0')
  await settle(ops.page)
  await ops.page.screenshot({ path: `${SHOTS}/1-provider-form.png` })
  await ops.page.locator('[data-testid="provider-save"]').click()
  const providerRow = ops.page.locator('[data-testid="provider-table"] .el-table__row', {
    hasText: PROVIDER,
  })
  await providerRow.waitFor()
  const rowText = await text(providerRow)
  state.providerId = (await json(`${API}/platform/v1/llm-providers`, { token: state.opsToken })).items.find(
    (p) => p.name === PROVIDER,
  )?.id
  check(
    '添加判断模型（Jev）：自动填上官方地址和模型名（切回 OpenAI 兼容时去掉），列表标出"判断模型"',
    prefilled.url === 'https://api.typesafe.ai/v1' &&
      prefilled.model === 'jev-latest' &&
      switchedBack.url === '' &&
      switchedBack.model === '' &&
      rowText.includes('判断模型') &&
      rowText.includes('判断：jev-1.13.0') &&
      !!state.providerId,
    { prefilled, switchedBack, rowText },
  )
  await providerRow.locator('[data-testid="provider-test"]').click()
  const testResult = providerRow.locator('[data-testid="provider-test-result"]')
  await testResult.filter({ hasText: '判断' }).waitFor()
  const testText = await text(testResult)
  check('检查连通：判断正常，返回模型版本', testText.includes('判断正常') && testText.includes('jev-fake-1'), testText)

  await ops.page.locator('[data-testid="route-reply"]').click()
  const replyOptions = await ops.page.locator('.el-select-dropdown:visible .el-select-dropdown__item').allInnerTexts()
  await ops.page.keyboard.press('Escape')
  await selectOption(ops.page, 'route-intent', PROVIDER)
  await ops.page.locator('button', { hasText: '保存路由' }).click()
  const routes = await waitFor(async () => {
    const current = await json(`${API}/platform/v1/settings/llm-routes`, { token: state.opsToken })
    return current.routes.intent === state.providerId ? current : null
  })
  check(
    '"意图判断"路由到判断模型；"AI 接待回复"选不到它',
    !!routes && routes.scenes.intent === '意图判断' && !replyOptions.some((o) => o.includes(PROVIDER)),
    { routes, replyOptions },
  )
  await settle(ops.page)
  await ops.page.screenshot({ path: `${SHOTS}/2-providers-routes.png`, fullPage: true })

  // ---- 2. 管理员：意图判断设置与"试一试" ----
  const admin = await consoleLogin(browser, 'admin')
  const page = admin.page
  await menu(page, 'AI 接待')
  await page.locator('[data-testid="ai-settings"]').waitFor()
  const sourceText = await waitFor(async () => {
    const value = await text(page.locator('[data-testid="ai-settings"]'))
    return value.includes(PROVIDER) ? value : null
  }, 10000)
  await page.locator('[data-testid="ai-settings"] .el-switch').first().click()
  await page.click('[data-testid="ai-custom-intent-add"]')
  await page.locator('input[data-testid="ai-custom-intent-name"]').fill('定制尺寸')
  await page.locator('input[data-testid="ai-custom-intent-description"]').fill('客户想按自己的尺寸定做')
  await selectOption(page, 'ai-intent-handoff', '准备下单时')
  await page.click('[data-testid="ai-save"]')
  const saved = await waitFor(async () => {
    const current = await json(`${API}/api/v1/ai/settings`, { token: adminToken })
    return current.enabled && current.custom_intents.length === 1 ? current : null
  })
  check(
    '意图判断设置：显示平台的判断模型；加自定义意图"定制尺寸"，准备下单时转人工',
    !!sourceText &&
      sourceText.includes(`当前用判断模型（${PROVIDER}（jev-1.13.0））`) &&
      saved?.intent_source === 'judge' &&
      saved.intent_enabled &&
      saved.intent_in_reply &&
      saved.intent_handoff_stage === 4 &&
      saved.custom_intents[0].name === '定制尺寸',
    { saved },
  )
  const intentSection = page.locator('[data-testid="ai-custom-intents"]')
  await intentSection.scrollIntoViewIfNeeded()
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/3-ai-settings-intent.png` })

  await tab(page, '试一试')
  await page.fill('input[data-testid="ai-test-input"]', '我要买两个，怎么下单')
  await page.click('[data-testid="ai-test-ask"]')
  const testCard = page.locator('[data-testid="ai-outcome"]', { hasText: '我要买两个' })
  await testCard.waitFor({ timeout: 20000 })
  const testCardText = await text(testCard)
  check(
    '试一试：显示意图判断（准备下单 · 购买下单），准备下单时转人工',
    testCardText.includes('意图判断：准备下单') &&
      testCardText.includes('真实意图：购买下单') &&
      testCardText.includes('转人工') &&
      testCardText.includes('客户有明确的购买意向'),
    testCardText,
  )
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/4-ai-test-intent.png` })

  // ---- 3. 人工接待：意图卡片、自定义意图、准备下单 ----
  const alice = await consoleLogin(browser, 'alice')
  await menu(alice.page, '工作台')
  await alice.page.locator('[data-testid="agent-status"]', { hasText: '在线' }).waitFor({ timeout: 15000 })
  const first = await openVisitor(browser, channel.public_key)
  await say(first, '你好，这款沙发多少钱？有灰色的吗')
  await alice.page.locator('[data-testid="session-item"]').first().waitFor({ timeout: 20000 })
  await alice.page.locator('[data-testid="session-item"]').first().click()
  const card = alice.page.locator('[data-testid="intent-card"]')
  await card.locator('[data-testid="intent-stage"]', { hasText: '有兴趣' }).waitFor({ timeout: 20000 })
  const interested = await text(card)
  check(
    '意图卡片：有兴趣 · 真实意图"询价比价" · 在意价格',
    interested.includes('有兴趣') && interested.includes('询价比价') && interested.includes('在意价格'),
    interested,
  )

  await say(first, '能做定制尺寸吗')
  await card.locator('[data-testid="intent-real"]', { hasText: '定制尺寸' }).waitFor({ timeout: 20000 })
  check('自定义意图：真实意图是"定制尺寸"', true)
  await alice.page.fill('textarea[data-testid="composer-input"]', '可以定制，告诉我需要的尺寸就行')
  await alice.page.click('[data-testid="send-button"]')
  await first.page.locator('[data-testid="message"].agent', { hasText: '可以定制' }).waitFor({ timeout: 15000 })

  await say(first, '我要两个，地址是上海市浦东新区张江路 1 号')
  await card.locator('[data-testid="intent-stage"]', { hasText: '准备下单' }).waitFor({ timeout: 20000 })
  const tag = alice.page.locator('[data-testid="session-item"] [data-testid="purchase-tag"]', {
    hasText: '准备下单',
  })
  await tag.first().waitFor({ timeout: 15000 })
  const alert = alice.page.locator('[data-testid="copilot-alert"]', { hasText: '客户准备下单' })
  await alert.waitFor({ timeout: 15000 })
  const ready = await text(card)
  check(
    '准备下单：卡片变红、会话列表标"准备下单"、坐席助手提醒一次',
    ready.includes('准备下单') &&
      ready.includes('购买下单') &&
      (await alert.count()) === 1 &&
      (await text(alert)).includes('订单'),
    { ready, alert: await text(alert) },
  )

  await alice.page.click('[data-testid="intent-toggle"]')
  const detail = alice.page.locator('[data-testid="intent-detail"]')
  await detail.waitFor()
  // 依据：判断的那条客户消息（消息有了平台 ID 后显示原文）。
  const detailText = await waitFor(async () => {
    const value = await text(detail)
    return value.includes('依据：客户说「我要两个') ? value : null
  }, 20000)
  const history = await text(alice.page.locator('[data-testid="intent-history"]'))
  check(
    '详情：5 级分布、这次会话的变化（有兴趣 → 准备下单）和依据',
    (await alice.page.locator('[data-testid="intent-distribution"]').count()) === 5 &&
      /有兴趣\s*→\s*\d{2}:\d{2} 准备下单/.test(history) &&
      !!detailText &&
      detailText.includes('判断模型（jev-fake-1）'),
    { history, detailText },
  )
  await settle(alice.page)
  await alice.page.screenshot({ path: `${SHOTS}/5-workbench-intent.png` })

  await alice.page.click('[data-testid="intent-order"]')
  const prefill = alice.page.locator('.el-dialog:visible', { hasText: 'AI 预填订单' })
  await prefill.waitFor({ timeout: 15000 })
  const picked = await prefill.locator('[data-testid="order-pick-message"].is-checked').count()
  check('生成订单：右侧切到订单并打开 AI 预填，默认选中客户最近的话', picked >= 1, { picked })
  await settle(alice.page)
  await alice.page.screenshot({ path: `${SHOTS}/6-order-prefill.png` })
  await alice.page.keyboard.press('Escape')

  // ---- 4. AI 接待参考意图判断 ----
  const policies = await json(`${API}/api/v1/routing-policies`, { token: adminToken })
  const policy = policies.items.find((p) => p.is_default)
  await json(`${API}/api/v1/routing-policies/${policy.id}`, {
    method: 'PATCH',
    token: adminToken,
    body: { mode: 'ai_first' },
  })

  const hurried = await openVisitor(browser, channel.public_key)
  await say(hurried, '快递几天能到？我比较着急')
  await hurried.page.locator('[data-testid="message"].bot', { hasText: '2 到 3 天' }).waitFor({ timeout: 20000 })
  check('AI 接待：参考意图判断后照常依据知识回答', true)

  const manager = await openVisitor(browser, channel.public_key)
  await say(manager, '能不能让你们负责人跟我说')
  const managerHandoff = await waitFor(async () => {
    const list = await json(`${API}/api/v1/sessions?status=open&limit=50`, { token: adminToken })
    return list.items.find((s) => s.handoff_reason === 'customer_request') ?? null
  }, 20000)
  check('换种说法要人工（"让你们负责人跟我说"）：判断模型认出来，转人工', !!managerHandoff, managerHandoff)

  const buyer = await openVisitor(browser, channel.public_key)
  await say(buyer, '我要两个，快递几天能到')
  const buyerItem = alice.page.locator('[data-testid="session-item"]', {
    has: alice.page.locator('[data-testid="purchase-tag"]', { hasText: '准备下单' }),
  })
  const handed = await waitFor(async () => {
    const list = await json(`${API}/api/v1/sessions?status=open&limit=50`, { token: adminToken })
    return list.items.find((s) => s.handoff_reason === 'purchase_intent') ?? null
  }, 20000)
  await waitFor(async () => ((await buyerItem.count()) >= 2 ? true : null), 20000)
  const buyerSession = alice.page.locator('[data-testid="session-item"]').filter({
    hasText: handed?.customer_display_name ?? '__none__',
  })
  await buyerSession.first().click()
  const banner = alice.page.locator('[data-testid="ai-summary"]')
  await banner.waitFor({ timeout: 15000 })
  const bannerText = await text(banner)
  await card.locator('[data-testid="intent-stage"]', { hasText: '准备下单' }).waitFor({ timeout: 15000 })
  check(
    '准备下单时 AI 转人工：坐席看到原因"客户有明确的购买意向"、列表标签和意图卡片',
    !!handed && handed.purchase_stage === 4 && bannerText.includes('客户有明确的购买意向'),
    { handed, bannerText },
  )
  await settle(alice.page)
  await alice.page.screenshot({ path: `${SHOTS}/7-ai-handoff-intent.png` })

  // ---- 5. 会话记录：AI 判定带着意图判断 ----
  await menu(page, '会话记录')
  const aiRow = page.locator('[data-testid="sessions-table"] .el-table__row', { hasText: 'AI 接待' })
  await aiRow.first().waitFor({ timeout: 15000 })
  await aiRow.first().click()
  const decisionIntent = page.locator('[data-testid="ai-decisions"] [data-testid="ai-intent"]')
  await decisionIntent.first().waitFor({ timeout: 15000 })
  const decisionText = await text(decisionIntent.first())
  check(
    '会话记录：AI 判定里写着当时的意图判断（意向明确 · 库存发货 · 在意发货时效 · 有些着急）',
    decisionText.includes('下单意向：意向明确') &&
      decisionText.includes('真实意图：库存发货') &&
      decisionText.includes('在意：发货时效') &&
      decisionText.includes('情绪：有些着急'),
    decisionText,
  )
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/8-session-decision-intent.png` })
  await page.keyboard.press('Escape')

  // ---- 6. 运营后台：用量按"意图判断"统计 ----
  const usage = await json(`${API}/platform/v1/llm-usage?days=1`, { token: state.opsToken })
  const scene = usage.by_scene.find((row) => row.label === '意图判断')
  check('运营后台的大模型用量里有"意图判断"', !!scene && scene.calls >= 6, scene)

  check('没有前端脚本错误', summary.consoleErrors.length === 0, summary.consoleErrors)
  for (const ctx of [ops.ctx, admin.ctx, alice.ctx, first.ctx, hurried.ctx, manager.ctx, buyer.ctx]) {
    await ctx.close()
  }
}

async function cleanup() {
  // 判断模型是平台级的配置：删除供应商，"意图判断"路由一并去掉，不影响后面的验收。
  if (state.providerId && state.opsToken) {
    await fetch(`${API}/platform/v1/llm-providers/${state.providerId}`, {
      method: 'DELETE',
      headers: { authorization: `Bearer ${state.opsToken}` },
    }).catch(() => undefined)
  }
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
    await cleanup()
    await browser.close()
    fs.writeFileSync(`${SHOTS}/summary.json`, JSON.stringify(summary, null, 2))
    console.log(JSON.stringify(summary, null, 2))
  }
  process.exit(summary.checks.some((c) => c.startsWith('FAIL')) ? 1 : 0)
})().catch((error) => {
  console.error(error)
  process.exit(1)
})
