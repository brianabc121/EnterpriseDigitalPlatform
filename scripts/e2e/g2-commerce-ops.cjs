// 套餐计费、租户生命周期与运营后台的浏览器验收（G2）：
//
// 1. 企业在控制台自助注册，开始 14 天试用，顶部显示试用提醒，"套餐与账单"显示额度用量。
// 2. 运营账号设置二次验证（验证器应用扫码），之后登录需要验证码。
// 3. 运营新建套餐，把租户转为正式订阅并单独调整坐席额度；租户新增员工超出额度时被拒绝。
// 4. 生成账单并标记已付款，租户在控制台看到账单。
// 5. 添加模型供应商并检查连通；全局敏感词让 AI 转人工。
// 6. 系统健康显示各组件状态。
// 7. 租户授权平台运维访问，运营查看会话，租户看到访问记录。
// 8. 租户导出数据并下载；申请注销后运营立即删除数据，留下删除记录；审计日志可查。
// 9. 关闭自助注册后注册页提示暂未开放。
//
// 前置：make dev-up、make im-up；后端、实时消费进程和调度进程已启动（导出由调度进程生成），
// 控制台与运营后台已启动；模拟大模型运行在 8900 端口（EDP_LLM_* 指向它）。
// 自助注册每个 IP 每小时限 5 次：一小时内重复运行时先删掉 Redis 里的 rl:signup-ip:* 键。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/g2-commerce-ops.cjs
const { chromium } = require('playwright')
const crypto = require('crypto')
const fs = require('fs')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PLATFORM = env('PLATFORM_URL', 'http://localhost:5174')
const FAKE_LLM = env('FAKE_LLM_URL', 'http://127.0.0.1:8900/v1')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/g2')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `g2-${RUN}`
const PLAN = `g2pro-${RUN}`
const PASSWORD = 'demo-pass-2026'
const WORD = `违禁词${RUN}`

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
  if (!response.ok) {
    const error = new Error(`${method} ${url} -> ${response.status} ${text}`)
    error.status = response.status
    error.body = text ? JSON.parse(text) : null
    throw error
  }
  return text ? JSON.parse(text) : null
}

async function waitFor(fn, timeout = 20000) {
  const deadline = Date.now() + timeout
  for (;;) {
    const value = await fn()
    if (value || Date.now() > deadline) return value
    await new Promise((r) => setTimeout(r, 1000))
  }
}

/** TOTP（RFC 6238，SHA-1、6 位、30 秒），offset 为相对当前时间步的偏移。 */
function totp(secret, offset = 0) {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
  let bits = 0
  let value = 0
  const bytes = []
  for (const c of secret.replace(/=+$/, '').toUpperCase()) {
    value = (value << 5) | alphabet.indexOf(c)
    bits += 5
    if (bits >= 8) {
      bytes.push((value >>> (bits - 8)) & 255)
      bits -= 8
    }
  }
  const counter = Buffer.alloc(8)
  counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30000) + offset))
  const digest = crypto.createHmac('sha1', Buffer.from(bytes)).update(counter).digest()
  const start = digest[digest.length - 1] & 0x0f
  return String((digest.readUInt32BE(start) & 0x7fffffff) % 1e6).padStart(6, '0')
}

function watchErrors(page, label) {
  page.on('console', (m) => {
    if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) {
      summary.consoleErrors.push(`${label}: ${m.text()}`)
    }
  })
  page.on('pageerror', (e) => summary.consoleErrors.push(`${label} pageerror: ${e.message}`))
}

async function newPage(browser, label) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page = await context.newPage()
  watchErrors(page, label)
  return page
}

const shot = (page, name) => page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true })

async function settingsTab(page, label) {
  await page.goto(`${CONSOLE}/settings`)
  await page.locator('[data-testid="settings-tabs"] .el-tabs__item', { hasText: label }).click()
}

async function platformMenu(page, name) {
  await page.locator(`[data-testid="menu-${name}"]`).click()
}

async function messageBoxInput(page, value) {
  const box = page.locator('.el-message-box:visible')
  await box.locator('input').fill(value)
  await box.locator('.el-message-box__btns .el-button--primary').click()
}

const state = { secret: null, providerId: null, words: null, opsToken: null }

async function run(browser) {
  // ---- 1. 自助注册 ----
  const page = await newPage(browser, 'console')
  await page.goto(`${CONSOLE}/login`)
  await page.locator('[data-testid="signup-link"]').click()
  await page.locator('input[data-testid="signup-company"]').fill(`G2 验收公司 ${RUN}`)
  await page.locator('input[data-testid="signup-code"]').fill(TENANT)
  await page.locator('input[data-testid="signup-name"]').fill('张三')
  await page.locator('input[data-testid="signup-password"]').fill(PASSWORD)
  await page.locator('input[data-testid="signup-contact"]').fill('13800000000')
  await page.locator('[data-testid="signup-agree"]').click()
  await shot(page, '1-signup')
  await page.locator('[data-testid="signup-submit"]').click()
  await page.locator('[data-testid="main-menu"]').waitFor()
  const banner = page.locator('[data-testid="billing-banner"]')
  await banner.waitFor()
  const bannerText = await banner.innerText()
  check('自助注册后进入控制台，顶部提示试用期剩余 14 天', bannerText.includes('试用期剩余 14 天'), {
    bannerText,
  })
  await settingsTab(page, '套餐与账单')
  await page.locator('[data-testid="billing-plan"]').waitFor()
  const planText = await page.locator('[data-testid="billing-plan"]').innerText()
  const limitsText = await page.locator('[data-testid="billing-limits"]').innerText()
  check('套餐页显示试用版与坐席 1/5 个', planText.includes('试用版') && /1\s*\/\s*5 个/.test(limitsText), {
    planText,
    limitsText,
  })
  await shot(page, '2-console-trial')

  // ---- 2. 运营账号二次验证 ----
  // 先用密码取一个 API 令牌（启用二次验证后已签发的令牌仍然有效），供后面的接口检查和清理使用。
  state.opsToken = (
    await json(`${API}/platform/v1/auth/login`, {
      method: 'POST',
      body: { username: PLATFORM_USER, password: PLATFORM_PASSWORD },
    })
  ).access_token
  const ops = await newPage(browser, 'platform')
  await ops.goto(`${PLATFORM}/login`)
  await ops.fill('input[autocomplete="username"]', PLATFORM_USER)
  await ops.fill('input[autocomplete="current-password"]', PLATFORM_PASSWORD)
  await ops.click('button:has-text("登录")')
  await ops.locator('[data-testid="tenant-table"]').waitFor()
  await platformMenu(ops, 'security')
  await ops.locator('[data-testid="mfa-setup"]').click()
  state.secret = (await ops.locator('[data-testid="mfa-secret"]').innerText()).trim()
  await ops.locator('input[data-testid="mfa-code"]').fill(totp(state.secret))
  await ops.locator('[data-testid="mfa-enable"]').click()
  await ops.locator('[data-testid="mfa-status"]', { hasText: '已启用' }).waitFor()
  await shot(ops, '3-platform-mfa')
  await ops.click('button:has-text("退出")')
  await ops.fill('input[autocomplete="username"]', PLATFORM_USER)
  await ops.fill('input[autocomplete="current-password"]', PLATFORM_PASSWORD)
  await ops.click('button:has-text("登录")')
  const otpInput = ops.locator('input[data-testid="login-otp"]')
  await otpInput.waitFor()
  await otpInput.fill(totp(state.secret, 1))
  await ops.click('button:has-text("验证并登录")')
  await ops.locator('[data-testid="tenant-table"]').waitFor()
  check('运营账号启用二次验证后，登录需要输入验证码', true)

  // ---- 3. 套餐、订阅与额度 ----
  await platformMenu(ops, 'plans')
  await ops.locator('[data-testid="plan-create"]').click()
  const planDialog = ops.locator('.el-dialog:visible')
  await planDialog.locator('.el-form-item', { hasText: '代码' }).locator('input').fill(PLAN)
  await ops.locator('input[data-testid="plan-name"]').fill(`G2 专业版 ${RUN}`)
  await planDialog.locator('.el-form-item', { hasText: '月费（元）' }).locator('input').fill('3000')
  await planDialog.locator('.el-form-item', { hasText: '坐席账号' }).locator('.el-checkbox').click()
  await ops.locator('[data-testid="plan-limit-seats"] input').fill('3')
  await ops.locator('[data-testid="plan-save"]').click()
  await ops.locator('[data-testid="plan-table"] .el-table__row', { hasText: PLAN }).waitFor()
  await shot(ops, '4-plans')
  check('运营新建套餐', true)

  await platformMenu(ops, 'tenants')
  const row = ops.locator('[data-testid="tenant-table"] .el-table__row', { hasText: TENANT })
  await row.waitFor()
  const rowText = await row.innerText()
  check('租户列表显示试用版与试用状态', rowText.includes('试用版') && rowText.includes('试用中'), {
    rowText,
  })
  await row.locator('[data-testid="tenant-detail-button"]').click()
  await ops.locator('[data-testid="current-plan"]').waitFor()
  await ops.locator('[data-testid="subscribe-button"]').click()
  await ops.locator('.el-dialog:visible .el-select').click()
  await ops.locator('.el-select-dropdown__item:visible', { hasText: `G2 专业版 ${RUN}` }).click()
  await ops.locator('.el-dialog:visible .el-form-item', { hasText: '月数' }).locator('input').fill('1')
  await ops.locator('[data-testid="subscribe-save"]').click()
  await ops.locator('[data-testid="current-plan"]', { hasText: `G2 专业版 ${RUN}` }).waitFor()
  await ops.locator('[data-testid="overrides-button"]').click()
  const overrides = ops.locator('.el-dialog:visible')
  await overrides.locator('.el-form-item', { hasText: '坐席账号' }).locator('.el-radio-button', { hasText: '自定义' }).click()
  await ops.locator('[data-testid="override-seats"] input').fill('2')
  await ops.locator('[data-testid="overrides-save"]').click()
  await ops.locator('[data-testid="tenant-limits"] .el-table__row', { hasText: '单独设置' }).waitFor()
  await ops.locator('.el-dialog:visible').waitFor({ state: 'hidden' })
  const historyText = await ops.locator('[data-testid="subscription-history"]').innerText()
  check('开始正式订阅后，试用订阅变为已取消', historyText.includes('已取消') && historyText.includes('正常'), {
    historyText,
  })
  await shot(ops, '5-tenant-billing')

  const admin = (
    await json(`${API}/api/v1/auth/login`, {
      method: 'POST',
      body: { tenant_code: TENANT, username: 'admin', password: PASSWORD },
    })
  ).access_token
  const staff = (username) =>
    json(`${API}/api/v1/staff`, {
      method: 'POST',
      token: admin,
      body: { username, display_name: username, password: PASSWORD, role_codes: ['agent'] },
    })
  await staff('amy')
  let refused = null
  try {
    await staff('bob')
  } catch (error) {
    refused = error
  }
  check(
    '单独设置坐席额度 2 个后，第三个员工被拒绝（plan_limit）',
    refused?.status === 409 && refused.body?.error?.code === 'plan_limit',
    { message: refused?.message },
  )
  await settingsTab(page, '套餐与账单')
  await page.locator('[data-testid="billing-plan"]', { hasText: `G2 专业版 ${RUN}` }).waitFor()
  const seatsText = await page.locator('[data-testid="billing-limits"]').innerText()
  check('控制台显示坐席 2/2 个，已达上限', /2\s*\/\s*2 个/.test(seatsText), { seatsText })
  await shot(page, '6-seat-limit')

  // ---- 4. 账单 ----
  const month = new Date().toISOString().slice(0, 7)
  await platformMenu(ops, 'invoices')
  await ops.locator('[data-testid="invoice-generate"]').click()
  await messageBoxInput(ops, month)
  const invoiceRow = ops.locator('[data-testid="invoice-table"] .el-table__row', { hasText: TENANT })
  await invoiceRow.waitFor()
  await invoiceRow.locator('[data-testid="invoice-paid"]').click()
  await invoiceRow.locator('.el-tag', { hasText: '已付款' }).waitFor()
  await shot(ops, '7-invoices')
  await settingsTab(page, '套餐与账单')
  const invoiceText = await page.locator('[data-testid="billing-invoices"]').innerText()
  check('生成本月账单并标记已付款，租户看到账单', invoiceText.includes(`INV-${month.replace('-', '')}-${TENANT}`) && invoiceText.includes('已付款'), {
    invoiceText,
  })

  // ---- 5. 模型供应商与全局敏感词 ----
  await platformMenu(ops, 'providers')
  await ops.locator('[data-testid="provider-create"]').click()
  await ops.locator('input[data-testid="provider-name"]').fill(`g2-${RUN}`)
  await ops.locator('input[data-testid="provider-url"]').fill(FAKE_LLM)
  await ops.locator('input[data-testid="provider-key"]').fill('dev-key')
  await ops.locator('input[data-testid="provider-chat-model"]').fill('fake-chat')
  await ops.locator('[data-testid="provider-save"]').click()
  const providerRow = ops.locator('[data-testid="provider-table"] .el-table__row', { hasText: `g2-${RUN}` })
  await providerRow.waitFor()
  await providerRow.locator('[data-testid="provider-test"]').click()
  const testResult = providerRow.locator('[data-testid="provider-test-result"]')
  await testResult.filter({ hasText: '对话' }).waitFor()
  const testText = await testResult.innerText()
  check('添加模型供应商并检查连通', testText.includes('对话正常'), { testText })
  await shot(ops, '8-providers')
  state.providerId = (await json(`${API}/platform/v1/llm-providers`, { token: state.opsToken })).items.find(
    (p) => p.name === `g2-${RUN}`,
  )?.id

  state.words = (await json(`${API}/platform/v1/settings/content-policy`, { token: state.opsToken })).words
  await platformMenu(ops, 'content')
  const words = ops.locator('textarea[data-testid="content-words"]')
  await words.waitFor()
  await words.fill([...state.words, WORD].join('\n'))
  await ops.locator('[data-testid="content-save"]').click()
  await ops.locator('.el-message--success', { hasText: '所有租户立即生效' }).last().waitFor()
  await json(`${API}/api/v1/ai/settings`, { method: 'PUT', token: admin, body: { enabled: true } })
  const outcome = await json(`${API}/api/v1/ai/test`, {
    method: 'POST',
    token: admin,
    body: { question: `你们卖${WORD}吗` },
  })
  check('全局敏感词：客户提到时 AI 转人工', outcome.action === 'handoff' && outcome.reason === 'sensitive', outcome)

  // ---- 6. 系统健康 ----
  await platformMenu(ops, 'health')
  await ops.locator('[data-testid="health-components"] .el-card').first().waitFor()
  const healthText = await ops.locator('[data-testid="health-components"]').innerText()
  const ok = (name) => new RegExp(`${name}\\s*正常`).test(healthText)
  check(
    '系统健康：数据库、Redis、OpenIM、对象存储、实时消费进程、调度进程正常',
    ['数据库', 'Redis', 'OpenIM', '对象存储', '实时消费进程', '调度进程'].every(ok),
    { healthText },
  )
  await shot(ops, '9-health')

  // ---- 7. 平台运维访问授权 ----
  await settingsTab(page, '平台访问授权')
  await page.locator('input[data-testid="grant-reason"]').fill('排查消息延迟')
  await page.locator('[data-testid="grant-submit"]').click()
  await page.locator('[data-testid="grant-table"] .el-tag', { hasText: '有效' }).waitFor()
  await platformMenu(ops, 'tenants')
  await ops.locator('[data-testid="tenant-table"] .el-table__row', { hasText: TENANT }).locator('[data-testid="tenant-detail-button"]').click()
  await ops.locator('.el-tabs__item', { hasText: '运维访问' }).click()
  await ops.locator('[data-testid="support-sessions-button"]').click()
  await ops.locator('[data-testid="support-sessions"]').waitFor()
  await shot(ops, '10-support')
  await settingsTab(page, '平台访问授权')
  const accessText = await page.locator('[data-testid="access-table"]').innerText()
  check('租户授权后运营可以查看会话，租户看到访问记录', accessText.includes('查看会话列表'), { accessText })

  // ---- 8. 导出、注销与删除 ----
  await settingsTab(page, '数据与注销')
  await page.locator('[data-testid="export-request"]').click()
  const exported = await waitFor(async () => {
    await page.locator('[data-testid="data-tab"] button', { hasText: '刷新' }).click()
    return (await page.locator('[data-testid="export-download"]').count()) > 0
  }, 90000)
  check('导出数据：调度进程生成导出文件', exported)
  if (exported) {
    const [first] = (await json(`${API}/api/v1/tenant/exports`, { token: admin })).items
    const link = await json(`${API}/api/v1/tenant/exports/${first.id}/download`, { token: admin })
    const zip = Buffer.from(await (await fetch(link.url)).arrayBuffer())
    check('下载的导出文件是 ZIP', zip.subarray(0, 2).toString() === 'PK', { size: zip.length })
  }
  await shot(page, '11-export')
  await page.locator('[data-testid="closure-open"]').click()
  await page.locator('input[data-testid="closure-password"]').fill(PASSWORD)
  await page.locator('input[data-testid="closure-code"]').fill(TENANT)
  await page.locator('[data-testid="closure-submit"]').click()
  await page.locator('[data-testid="closure-status"]').waitFor()
  await shot(page, '12-closure')

  await platformMenu(ops, 'tenants')
  const closingRow = ops.locator('[data-testid="tenant-table"] .el-table__row', { hasText: TENANT })
  await closingRow.locator('.el-tag', { hasText: '注销中' }).waitFor()
  check('租户申请注销后，运营后台显示注销中', true)
  await closingRow.locator('[data-testid="tenant-detail-button"]').click()
  await ops.locator('.el-tabs__item', { hasText: '注销' }).click()
  await ops.locator('[data-testid="purge-button"]').click()
  await messageBoxInput(ops, TENANT)
  const digest = ops.locator('[data-testid="deletion-digest"]')
  await digest.waitFor()
  const digestText = (await digest.innerText()).trim()
  check('立即删除数据后生成删除记录（SHA-256）', /^[0-9a-f]{64}$/.test(digestText), { digestText })
  await shot(ops, '13-purged')
  await platformMenu(ops, 'deletions')
  const deletionRow = ops.locator('[data-testid="deletion-table"] .el-table__row', { hasText: TENANT })
  await deletionRow.waitFor()
  check('删除记录页列出这个租户', (await deletionRow.innerText()).includes(digestText.slice(0, 16)))
  let loginAfter = null
  try {
    await json(`${API}/api/v1/auth/login`, {
      method: 'POST',
      body: { tenant_code: TENANT, username: 'admin', password: PASSWORD },
    })
  } catch (error) {
    loginAfter = error.status
  }
  check('数据删除后员工不能再登录', loginAfter === 401, { loginAfter })

  await platformMenu(ops, 'audit')
  await ops.locator('input[data-testid="audit-action"]').fill('tenant')
  await ops.locator('input[data-testid="audit-action"]').press('Enter')
  await ops
    .locator('[data-testid="audit-table"] .el-table__row', { hasText: TENANT })
    .filter({ hasText: 'tenant.purge' })
    .first()
    .waitFor()
  await shot(ops, '14-audit')
  check('审计日志可以按动作筛选，包含删除数据', true)

  // ---- 9. 关闭自助注册 ----
  await platformMenu(ops, 'settings')
  await ops.locator('[data-testid="signup-enabled"]').click()
  await ops.locator('[data-testid="policy-save"]').click()
  await ops.locator('.el-message--success', { hasText: '已保存' }).last().waitFor()
  const closed = await newPage(browser, 'signup')
  await closed.goto(`${CONSOLE}/signup`)
  await closed.locator('.el-result', { hasText: '暂未开放自助注册' }).waitFor()
  check('关闭自助注册后，注册页提示暂未开放', true)
  await shot(closed, '15-signup-closed')
  await json(`${API}/platform/v1/settings/tenant-policy`, {
    method: 'PUT',
    token: state.opsToken,
    body: { ...(await json(`${API}/platform/v1/settings/tenant-policy`, { token: state.opsToken })), signup_enabled: true },
  })
}

/** 恢复开发环境：删除测试用的供应商和敏感词，关闭运营账号的二次验证。 */
async function cleanup() {
  let token = state.opsToken
  if (!token && state.secret) {
    // 启用二次验证之后中途失败：用验证码登录后再关闭。
    token = (
      await json(`${API}/platform/v1/auth/login`, {
        method: 'POST',
        body: { username: PLATFORM_USER, password: PLATFORM_PASSWORD, otp: totp(state.secret, 3) },
      })
    ).access_token
  }
  if (!token) return
  if (state.providerId) {
    await fetch(`${API}/platform/v1/llm-providers/${state.providerId}`, {
      method: 'DELETE',
      headers: { authorization: `Bearer ${token}` },
    })
  }
  if (state.words) {
    const policy = await json(`${API}/platform/v1/settings/content-policy`, { token })
    await json(`${API}/platform/v1/settings/content-policy`, {
      method: 'PUT',
      token,
      body: { ...policy, words: state.words },
    })
  }
  if (state.secret) {
    await json(`${API}/platform/v1/auth/mfa/disable`, {
      method: 'POST',
      token,
      body: { password: PLATFORM_PASSWORD, code: totp(state.secret) },
    })
  }
}

;(async () => {
  if (!PLATFORM_PASSWORD) {
    console.error('请设置 PLATFORM_PASSWORD')
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
    await cleanup().catch((error) => summary.checks.push(`FAIL 清理 -> ${error.message}`))
    await browser.close()
    fs.writeFileSync(`${SHOTS}/summary.json`, JSON.stringify(summary, null, 2))
    console.log(JSON.stringify(summary, null, 2))
  }
  process.exit(summary.checks.some((c) => c.startsWith('FAIL')) ? 1 : 0)
})().catch((error) => {
  console.error(error)
  process.exit(1)
})
