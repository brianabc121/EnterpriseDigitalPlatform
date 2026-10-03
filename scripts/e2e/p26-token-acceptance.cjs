// P26 验收：企业 token 计费（设计文档 §37）——企业 AI 的每一次调用都记下 tokens 和费用；"Token 计费"页面按月展示
// 合计、上月同期、每天、按场景 / 模型 / 员工的构成，"查看近 7 天明细"列出每一次调用（筛选、关联的会话、导出 CSV）；
// 企业改用自己的接口密钥后费用记 0；只有管理员和财务看得到。
//
// 1. 准备：企业和管理员、坐席小艾、财务小芳（财务岗位的默认权限）；平台建一个有价格的供应商（输入每千 tokens
//    2 分、输出 6 分，接到模拟大模型），指定给这个企业。
// 2. 产生用量：管理员在"AI 设置"里试一试两次；小艾让 AI 起草一份合同。
// 3. 管理员打开"Token 计费"：tokens、费用、调用次数和接口一致，费用等于按价格算的结果；每天的柱状图；按场景、
//    按模型、按员工（管理员、小艾）的构成；和平台运营后台的用量报表对得上。
// 4. 近 7 天明细：每一次调用（时间、场景、模型、tokens、费用、员工），按场景、员工筛选，导出 CSV。
// 5. 企业在 AI 设置里填自己的接口密钥后再试一试：明细标"自带密钥"、费用 0，页面提示。
// 6. 小艾没有这个菜单（接口 403）；财务小芳看得到；上个月没有用量。
//
// 前置：与 p22-wake-acceptance.cjs 相同（后端接到模拟大模型 :8900）。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<密码> node scripts/e2e/p26-token-acceptance.cjs
const { chromium } = require('playwright')
const fs = require('fs')
const path = require('path')
const os = require('os')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p26')
const FAKE_LLM = env('FAKE_LLM_URL', 'http://127.0.0.1:8900/v1')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `token-${RUN}`
const PASSWORD = 'demo-pass-2026'
// 每千 tokens 的价格（分）。
const PRICE_INPUT = 2
const PRICE_OUTPUT = 6

const summary = { tenant: TENANT, checks: [], consoleErrors: [] }
const check = (name, ok, detail) =>
  summary.checks.push(
    `${ok ? 'PASS' : 'FAIL'} ${name}${ok || detail === undefined ? '' : ` -> ${JSON.stringify(detail)}`}`,
  )
const state = { contexts: [], pages: [] }

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

async function status(url, token) {
  return (await fetch(url, { headers: { authorization: `Bearer ${token}` } })).status
}

async function login(username) {
  const data = await json(`${API}/api/v1/auth/login`, {
    method: 'POST',
    body: { tenant_code: TENANT, username, password: PASSWORD },
  })
  return data.access_token
}

/** 元，两位小数（页面上的显示）。 */
const yuan = (cents) => `¥${(cents / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
const near = (a, b) => Math.abs(a - b) < 1e-6

// ---- 1. 准备 ----

async function prepare() {
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
      name: `计费验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  state.tenantId = tenant.id
  // 有价格的供应商，只指定给这个企业（不影响其他验收）。
  const provider = await json(`${API}/platform/v1/llm-providers`, {
    method: 'POST',
    token: state.ops,
    body: {
      name: `计费-${RUN}`,
      base_url: FAKE_LLM,
      api_key: 'sk-token-e2e',
      chat_model: 'fake-chat',
      prices: { input: PRICE_INPUT, output: PRICE_OUTPUT },
    },
  })
  state.providerId = provider.id
  state.providerName = provider.name
  await json(`${API}/platform/v1/tenants/${tenant.id}/llm`, {
    method: 'PUT',
    token: state.ops,
    body: { provider_id: provider.id },
  })

  state.admin = await login('admin')
  await json(`${API}/api/v1/staff`, {
    method: 'POST',
    token: state.admin,
    body: { username: 'alice', display_name: '小艾', password: PASSWORD, role_codes: ['agent'] },
  })
  // 财务：系统角色"财务"（员工导图分支新增），权限就是"财务"岗位的默认权限。
  const profiles = await json(`${API}/api/v1/roles/profile-permissions`, { token: state.admin })
  const finance = profiles.items.find((p) => p.profile === 'finance')
  await json(`${API}/api/v1/staff`, {
    method: 'POST',
    token: state.admin,
    body: { username: 'fay', display_name: '小芳', password: PASSWORD, role_codes: ['finance'] },
  })
  state.alice = await login('alice')
  state.fay = await login('fay')
  check('the finance position grants token:view by default', finance.permissions.includes('token:view'))
}

// ---- 2. 产生用量 ----

async function useAi() {
  for (const question of ['快递几天能到', '能开发票吗']) {
    await json(`${API}/api/v1/ai/test`, { method: 'POST', token: state.admin, body: { question } })
  }
  await json(`${API}/api/v1/contracts/generate`, {
    method: 'POST',
    token: state.alice,
    body: { requirement: '给华东分公司定制 20 把智能门锁，30 天交货，预付 30%。' },
  })
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
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    locale: 'zh-CN',
    acceptDownloads: true,
  })
  state.contexts.push(ctx)
  const page = await ctx.newPage()
  state.pages.push(page)
  watchErrors(page, username)
  await page.goto(`${CONSOLE}/login`)
  await page.fill('input[placeholder="例如 demo"]', TENANT)
  await page.fill('input[autocomplete="username"]', username)
  await page.fill('input[autocomplete="current-password"]', PASSWORD)
  await page.click('button:has-text("登录")')
  await page.waitForSelector('[data-testid="main-menu"]')
  return page
}

async function openTokens(page) {
  await page.locator('[data-testid="main-menu"]').getByText('Token 计费', { exact: true }).click()
  await page.waitForURL(/\/tokens/)
  await page.locator('[data-testid="token-total"]').waitFor()
}

// 截图前等抽屉滑入、按钮的过渡动画结束（与 p24 相同）。
const settle = (page) => page.waitForTimeout(600)

async function tile(page, id) {
  return (await page.locator(`[data-testid="${id}"] .value`).innerText()).trim()
}

async function groupRows(page) {
  return page.locator('[data-testid="token-groups"] .el-table__body tr').allInnerTexts()
}

// ---- 3. 页面 ----

async function monthPage(page) {
  await openTokens(page)
  const api = await json(`${API}/api/v1/tokens/summary`, { token: state.admin })
  state.summary = api
  const totals = api.totals
  check(
    'usage is recorded: 2 tries and 1 contract draft, all billed',
    totals.calls >= 3 && totals.failed === 0 && totals.tokens > 0 && totals.cost > 0,
    totals,
  )
  const calls = await json(`${API}/api/v1/tokens/calls?limit=100`, { token: state.admin })
  state.calls = calls
  // 对话走指定的有价格的供应商；知识检索的向量化走平台的向量模型（验收环境没有设价格，记 0）。
  const priced = calls.items.filter((c) => c.provider === state.providerName)
  const others = calls.items.filter((c) => c.provider !== state.providerName)
  const expected = priced.reduce(
    (sum, c) => sum + (c.prompt_tokens * PRICE_INPUT + c.completion_tokens * PRICE_OUTPUT) / 1000,
    0,
  )
  check(
    'the fee is the tokens times the provider prices (input 2, output 6 cents per 1,000); unpriced embeddings cost nothing',
    priced.length === 3 &&
      near(calls.cost, expected) &&
      near(totals.cost, expected) &&
      others.every((c) => c.cost === 0),
    { calls: calls.cost, totals: totals.cost, expected, others: others.map((c) => [c.scene, c.cost]) },
  )
  const shown = { cost: await tile(page, 'token-cost'), calls: await tile(page, 'token-calls') }
  check(
    'the page shows the same fee and calls as the API',
    shown.cost === yuan(totals.cost) && shown.calls === totals.calls.toLocaleString('zh-CN'),
    { shown, totals },
  )
  check('the daily chart is drawn', (await page.locator('[data-testid="token-trend"] svg').count()) === 1)

  const scenes = await groupRows(page)
  check(
    'by scene: the try-it calls and the contract draft',
    scenes.some((r) => r.includes('AI 设置里的试一试')) && scenes.some((r) => r.includes('合同起草')),
    scenes,
  )
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/p26-01-month.png` })
  await page.locator('[data-testid="token-group"] .el-radio-button', { hasText: '按员工' }).click()
  await page.waitForFunction(() =>
    [...document.querySelectorAll('[data-testid="token-groups"] .el-table__body tr')].some((r) =>
      r.textContent.includes('小艾'),
    ),
  )
  const staff = await groupRows(page)
  check(
    'by staff: the admin and 小艾 who triggered the calls',
    staff.some((r) => r.includes('管理员')) && staff.some((r) => r.includes('小艾')),
    staff,
  )
  await page.locator('[data-testid="token-group"] .el-radio-button', { hasText: '按模型' }).click()
  await page.waitForFunction(() =>
    [...document.querySelectorAll('[data-testid="token-groups"] .el-table__body tr')].some((r) =>
      r.textContent.includes('fake-chat'),
    ),
  )
  const models = await groupRows(page)
  check('by model: the priced provider', models.some((r) => r.includes('fake-chat') && r.includes(state.providerName)), models)
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/p26-02-by-staff-model.png` })

  // 平台运营后台的用量报表（按租户）对得上。
  const usage = await json(`${API}/platform/v1/llm-usage?days=1`, { token: state.ops })
  const row = usage.by_tenant.find((t) => t.label.includes(`（${TENANT}）`))
  check(
    "the platform's usage report shows the same tokens for this enterprise",
    row && row.tokens === totals.tokens && row.calls === totals.calls,
    { row, totals },
  )
}

// ---- 4. 明细 ----

async function details(page) {
  await page.click('[data-testid="token-details-open"]')
  const drawer = page.locator('[data-testid="token-details"]')
  await drawer.waitFor()
  await drawer.locator('[data-testid="token-calls-table"] .el-table__body tr').first().waitFor()
  const rows = await drawer.locator('[data-testid="token-calls-table"] .el-table__body tr').count()
  check('the 7-day details list every call', rows === state.calls.total, { rows, total: state.calls.total })
  const text = await drawer.locator('[data-testid="token-calls-summary"]').innerText()
  check('the details sum up calls, tokens and fee', text.includes(`${state.calls.total} 次调用`) && text.includes(yuan(state.calls.cost)), text)
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/p26-03-details.png` })

  // 按场景筛选：合同起草，员工是小艾。
  await drawer.locator('[data-testid="token-calls-scene"]').click()
  await page.locator('.el-select-dropdown__item:visible', { hasText: '合同起草' }).first().click()
  await page.waitForFunction(() => {
    const rows = [...document.querySelectorAll('[data-testid="token-calls-table"] .el-table__body tr')]
    return rows.length > 0 && rows.every((r) => r.textContent.includes('合同起草'))
  })
  const contract = await drawer.locator('[data-testid="token-calls-table"] .el-table__body tr').allInnerTexts()
  check('filtering by scene shows 小艾 drafting the contract', contract.every((r) => r.includes('小艾')), contract)
  await drawer.locator('[data-testid="token-calls-scene"] .el-select__wrapper').hover()
  await drawer.locator('[data-testid="token-calls-scene"] .el-select__clear').click()

  // 导出 CSV。
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    drawer.locator('[data-testid="token-calls-export"]').click(),
  ])
  const file = path.join(os.tmpdir(), `p26-${RUN}.csv`)
  await download.saveAs(file)
  const csv = fs.readFileSync(file, 'utf-8')
  fs.rmSync(file, { force: true })
  const lines = csv.trim().split(/\r?\n/)
  check(
    'the CSV export has a header and one line per call',
    csv.startsWith('﻿时间,场景') && lines.length === 1 + state.calls.total && csv.includes('合同起草'),
    { lines: lines.length },
  )
  await page.locator('.el-drawer__close-btn:visible').last().click()
}

// ---- 5. 自带密钥 ----

async function ownKey(page) {
  await json(`${API}/api/v1/ai/llm`, {
    method: 'PUT',
    token: state.admin,
    body: { base_url: FAKE_LLM, api_key: 'sk-own-key', chat_model: 'own-chat', enabled: true },
  })
  await json(`${API}/api/v1/ai/test`, { method: 'POST', token: state.admin, body: { question: '退货怎么办' } })
  const calls = await json(`${API}/api/v1/tokens/calls?limit=5`, { token: state.admin })
  const own = calls.items.find((c) => c.own_key)
  check('a call with the own key is recorded with tokens and no fee', own && own.tokens > 0 && own.cost === 0, own)
  await page.reload()
  await page.locator('[data-testid="token-own-key"]').waitFor()
  // 关抽屉时鼠标停在右上角，会悬停打开顶栏的账号菜单，挡住按钮：先移开。
  await page.mouse.move(700, 600)
  await page.click('[data-testid="token-details-open"]')
  const drawer = page.locator('[data-testid="token-details"]')
  await drawer.locator('[data-testid="token-calls-table"] .el-table__body tr').first().waitFor()
  const first = await drawer.locator('[data-testid="token-calls-table"] .el-table__body tr').first().innerText()
  check('the details mark it 自带密钥', first.includes('自带密钥'), first)
  await settle(page)
  await page.screenshot({ path: `${SHOTS}/p26-04-own-key.png` })
  await page.locator('.el-drawer__close-btn:visible').last().click()
}

// ---- 6. 权限和以前的月份 ----

async function access(browser) {
  const alice = await consoleLogin(browser, 'alice')
  const menu = await alice.locator('[data-testid="main-menu"]').innerText()
  check(
    '小艾 has no Token 计费 menu and the API refuses her',
    !menu.includes('Token 计费') && (await status(`${API}/api/v1/tokens/summary`, state.alice)) === 403,
    menu,
  )
  const fay = await consoleLogin(browser, 'fay')
  await openTokens(fay)
  const current = await json(`${API}/api/v1/tokens/summary`, { token: state.fay })
  check(
    '财务小芳 sees the page with the same totals',
    (await tile(fay, 'token-calls')) === current.totals.calls.toLocaleString('zh-CN') &&
      (await tile(fay, 'token-cost')) === yuan(current.totals.cost),
    current.totals,
  )
  await fay.locator('[data-testid="token-month"]').click()
  const previous = fay.locator('.el-select-dropdown__item:visible').nth(1)
  await previous.click()
  await fay.locator('[data-testid="token-groups"] .el-table__empty-text').waitFor()
  check('the previous month has no usage', (await tile(fay, 'token-calls')) === '0')
  await settle(fay)
  await fay.screenshot({ path: `${SHOTS}/p26-05-finance-previous-month.png` })
}

async function cleanup() {
  if (state.providerId && state.ops) {
    await fetch(`${API}/platform/v1/llm-providers/${state.providerId}`, {
      method: 'DELETE',
      headers: { authorization: `Bearer ${state.ops}` },
    }).catch(() => undefined)
  }
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
    await useAi()
    const admin = await consoleLogin(browser, 'admin')
    await monthPage(admin)
    await details(admin)
    await ownKey(admin)
    await access(browser)
  } catch (error) {
    summary.checks.push(`FAIL exception -> ${error.stack || error}`)
    for (const [i, page] of state.pages.entries()) {
      await page.screenshot({ path: `${SHOTS}/failure-${i + 1}.png` }).catch(() => undefined)
    }
  } finally {
    await cleanup()
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
