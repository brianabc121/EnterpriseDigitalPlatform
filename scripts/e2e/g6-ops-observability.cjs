// 工程与运维的浏览器验收（G6）：
//
// 1. 共享组件：访客发来的链接在坐席工作台里可以点击。
// 2. 运营后台"运维"：死信列表看到处理失败的事件，重新处理后消失；IM 发件箱里最终失败的系统提示
//    不显示正文，重试后送达访客。
// 3. 租户详情"限流"：把员工接口请求设为每分钟 40 次，超过后控制台的请求返回 429；清空后恢复。
// 4. 系统健康：消息分区正常，本月之后至少建好 3 个月的分区。
// 5. 指标：配置了 METRICS_URLS（逗号分隔，API、实时消费、调度进程的 /metrics）时检查平台指标。
//
// 前置：同 M4（后端、实时消费进程、调度进程、控制台、运营后台、Widget、OpenIM）。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/g6-ops-observability.cjs
const { chromium } = require('playwright')
const { execFileSync } = require('child_process')
const fs = require('fs')
const path = require('path')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PLATFORM = env('PLATFORM_URL', 'http://localhost:5174')
const WIDGET = env('WIDGET_URL', 'http://localhost:5175')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const METRICS_URLS = env('METRICS_URLS', '')
const SHOTS = env('SHOTS', 'e2e-shots/g6')
const BACKEND_DIR = path.resolve(__dirname, '../../backend')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `g6-${RUN}`
const PASSWORD = 'demo-pass-2026'
const LINK = `https://example.com/orders/${RUN}`
const NOTICE = `【验收】运维重试送达的提示 ${RUN}`

const summary = { tenant: TENANT, checks: [], consoleErrors: [] }
const check = (name, ok, detail) =>
  summary.checks.push(
    `${ok ? 'PASS' : 'FAIL'} ${name}${ok || detail === undefined ? '' : ` -> ${JSON.stringify(detail)}`}`,
  )

async function json(url, { method = 'GET', token, headers = {}, body } = {}) {
  const response = await fetch(url, {
    method,
    headers: {
      'content-type': 'application/json',
      ...(token ? { authorization: `Bearer ${token}` } : {}),
      ...headers,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const text = await response.text()
  if (!response.ok) throw new Error(`${method} ${url} -> ${response.status} ${text}`)
  return text ? JSON.parse(text) : null
}

async function waitFor(fn, timeout = 20000) {
  const deadline = Date.now() + timeout
  for (;;) {
    const value = await fn().catch(() => null)
    if (value || Date.now() > deadline) return value
    await new Promise((r) => setTimeout(r, 1000))
  }
}

async function login(username) {
  const tokens = await json(`${API}/api/v1/auth/login`, {
    method: 'POST',
    body: { tenant_code: TENANT, username, password: PASSWORD },
  })
  return tokens.access_token
}

/** 在后端目录执行一段 Python（注入验收用的失败事件和发件箱操作）。 */
function python(code, ...args) {
  const output = execFileSync('uv', ['run', 'python', '-c', code, ...args], {
    cwd: BACKEND_DIR,
    env: process.env,
    encoding: 'utf-8',
  })
  return output.trim().split('\n').pop()
}

const INJECT_DEAD_LETTER = `
import asyncio, sys, uuid
from redis.asyncio import Redis
from app.core.config import get_settings
from app.events.bus import DEAD_LETTER_STREAM, Event

async def main(tenant_id, room_id):
    redis = Redis.from_url(get_settings().redis_url)
    event = Event(type="message.received", tenant_id=uuid.UUID(tenant_id), key=room_id,
                  data={"message_id": str(uuid.uuid4())})
    entry = await redis.xadd(DEAD_LETTER_STREAM,
                             {**event.encode(), "error": "RuntimeError('验收注入的失败事件')"})
    print(entry.decode())
    await redis.aclose()

asyncio.run(main(*sys.argv[1:]))
`

const INJECT_FAILED_NOTICE = `
import asyncio, json, sys, uuid
import asyncpg
from app.core.config import get_settings

async def main(tenant_id, room_id, text):
    conn = await asyncpg.connect(get_settings().database_url_owner.replace("+asyncpg", ""))
    op_id = await conn.fetchval(
        "INSERT INTO im_ops (tenant_id, room_id, op, payload, status, attempts, next_attempt_at,"
        " last_error, done_at) VALUES ($1, $2, 'notice', $3::jsonb, 'failed', 12, now(),"
        " 'OpenIM unavailable（验收注入）', now()) RETURNING id",
        uuid.UUID(tenant_id), uuid.UUID(room_id), json.dumps({"text": text}))
    print(op_id)
    await conn.close()

asyncio.run(main(*sys.argv[1:]))
`

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
      name: `运维验收 ${RUN}`,
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
  return { tenantId: tenant.id, ops: platform.access_token, admin, channel: channels.items[0] }
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
  const context = await browser.newContext({ viewport, locale: 'zh-CN' })
  const page = await context.newPage()
  watchErrors(page, label)
  return page
}

async function shot(page, name) {
  await page.waitForTimeout(600)
  await page.screenshot({ path: `${SHOTS}/${name}.png` })
}

async function seen(locator, timeout = 20000) {
  return locator
    .first()
    .waitFor({ timeout })
    .then(() => true)
    .catch(() => false)
}

async function consoleLogin(page, username) {
  await page.goto(`${CONSOLE}/login`)
  await page.fill('input[placeholder="例如 demo"]', TENANT)
  await page.fill('input[autocomplete="username"]', username)
  await page.fill('input[autocomplete="current-password"]', PASSWORD)
  await page.click('button:has-text("登录")')
  await page.locator('[data-testid="main-menu"]').waitFor()
}

async function platformLogin(page) {
  await page.goto(`${PLATFORM}/login`)
  await page.fill('input[autocomplete="username"]', PLATFORM_USER)
  await page.fill('input[autocomplete="current-password"]', PLATFORM_PASSWORD)
  await page.click('button:has-text("登录")')
  await page.locator('[data-testid="platform-menu"]').waitFor()
}

async function confirmBox(page, text) {
  const box = page.locator('.el-message-box:visible')
  await box.waitFor()
  await box.locator('.el-message-box__btns button', { hasText: text }).click()
}

// ---- 1. 访客的链接在工作台里可以点击 ----

async function chatSection(browser, ctx) {
  const agent = await newPage(browser, 'alice')
  await consoleLogin(agent, 'alice')
  await agent.locator('[data-testid="main-menu"] .el-menu-item', { hasText: '工作台' }).click()
  await agent.locator('[data-testid="agent-status"]', { hasText: '在线' }).waitFor({ timeout: 15000 })

  const visitor = await newPage(browser, 'visitor', { width: 420, height: 760 })
  await visitor.goto(`${WIDGET}/?key=${encodeURIComponent(ctx.channel.public_key)}`)
  await visitor.locator('[data-testid="widget-state"]', { hasText: '在线' }).waitFor()
  await visitor.fill('[data-testid="message-input"]', `订单详情请看 ${LINK}，谢谢`)
  await visitor.click('[data-testid="send-button"]')

  await agent.locator('[data-testid="session-item"]').first().click({ timeout: 30000 })
  const link = agent.locator(`a.edp-link[href="${LINK}"]`)
  const clickable = await seen(link, 30000)
  const attrs = clickable
    ? await link.first().evaluate((a) => ({ target: a.target, rel: a.rel, text: a.textContent }))
    : null
  await shot(agent, '1-workbench-link')
  check(
    '访客发来的链接在工作台里可以点击（新窗口打开）',
    clickable && attrs.target === '_blank' && attrs.rel.includes('noopener') && attrs.text === LINK,
    attrs,
  )
  const widgetLink = await seen(visitor.locator(`a.edp-link[href="${LINK}"]`))
  check('访客自己发的链接在 Widget 里也可以点击', widgetLink)

  const rooms = await json(`${API}/api/v1/rooms`, { token: ctx.admin })
  ctx.roomId = rooms.items[0].id
  ctx.visitor = visitor
}

// ---- 2. 运维：死信与 IM 发件箱 ----

async function opsSection(browser, ctx) {
  const entry = python(INJECT_DEAD_LETTER, ctx.tenantId, ctx.roomId)
  const opId = Number(python(INJECT_FAILED_NOTICE, ctx.tenantId, ctx.roomId, NOTICE))

  const page = await newPage(browser, 'platform')
  await platformLogin(page)
  await page.locator('[data-testid="menu-ops"]').click()
  await page.locator('[data-testid="ops-view"]').waitFor()
  const row = page.locator('[data-testid="dead-table"] .el-table__row', { hasText: TENANT })
  const listed = await seen(row)
  const rowText = listed ? await row.first().innerText() : ''
  await shot(page, '2-dead-letters')
  check(
    '死信列表显示失败的事件、所属租户和错误',
    listed && rowText.includes('消息归入会话') && rowText.includes('验收注入的失败事件'),
    rowText,
  )
  await row.first().locator('.el-checkbox').click()
  await page.locator('[data-testid="dead-retry"]').click()
  await page.locator('.el-message--success', { hasText: '已重新处理 1 个' }).waitFor()
  const gone = await waitFor(async () => {
    const list = await json(`${API}/platform/v1/ops/dead-letters?tenant_id=${ctx.tenantId}`, {
      token: ctx.ops,
    })
    return list.items.every((i) => i.id !== entry) ? true : null
  })
  check('重新处理后死信消失（事件重新发布给实时消费进程）', !!gone)

  await page.locator('.el-tabs__item', { hasText: 'IM 发件箱' }).click()
  const opRow = page.locator('[data-testid="ops-table"] .el-table__row', { hasText: TENANT })
  const opListed = await seen(opRow)
  const opText = opListed ? await opRow.first().innerText() : ''
  await shot(page, '3-outbox-failed')
  check(
    '发件箱列出最终失败的系统提示，不显示消息正文',
    opListed && opText.includes('系统提示') && opText.includes('验收注入') && !opText.includes(NOTICE),
    opText,
  )
  await opRow.first().locator('.el-checkbox').click()
  await page.locator('[data-testid="ops-retry"]').click()
  await page.locator('.el-message--success', { hasText: '已重试 1 个' }).waitFor()
  const delivered = await seen(ctx.visitor.locator('[data-testid="message"]', { hasText: NOTICE }), 30000)
  const done = await waitFor(async () => {
    const list = await json(`${API}/platform/v1/ops/im-ops?status=failed&tenant_id=${ctx.tenantId}`, {
      token: ctx.ops,
    })
    return list.items.every((i) => i.id !== opId) ? true : null
  })
  await shot(ctx.visitor, '4-visitor-notice')
  check('重试后提示送达访客，发件箱里不再有这条失败记录', delivered && !!done, { delivered, done })
  ctx.platformPage = page
}

// ---- 3. 按租户限流 ----

async function rateLimitSection(ctx) {
  const page = ctx.platformPage
  await page.locator('[data-testid="menu-tenants"]').click()
  await page.locator('.el-table__row', { hasText: TENANT }).first().locator('a, button').first().click()
  await page.locator('.el-tabs__item', { hasText: '限流' }).click()
  const input = page.locator('[data-testid="rate-limit-api"] input')
  const save = async () => {
    const saved = page.waitForResponse(
      (r) => r.url().endsWith('/rate-limits') && r.request().method() === 'PUT',
    )
    await page.locator('[data-testid="rate-limits-save"]').click()
    await saved
    // 浏览器收到的响应是加密的（设计文档 §25.15），保存后通过接口读取。
    return json(`${API}/platform/v1/tenants/${ctx.tenantId}/rate-limits`, { token: ctx.ops })
  }
  await input.fill('40')
  await input.press('Tab')
  const set = await save()
  await page.locator('.el-message--success', { hasText: '已保存' }).first().waitFor()
  await shot(page, '5-rate-limits')
  check('运营后台把员工接口请求设为每分钟 40 次', set.effective && set.effective.api === 40, set)

  const statuses = await Promise.all(
    Array.from({ length: 60 }, () =>
      fetch(`${API}/api/v1/customers`, { headers: { authorization: `Bearer ${ctx.admin}` } }).then(
        (r) => r.status,
      ),
    ),
  )
  const limited = statuses.filter((s) => s === 429).length
  const ok = statuses.filter((s) => s === 200).length
  check('员工接口请求超过每分钟 40 次后返回 429', ok <= 40 && limited >= 20, { ok, limited })

  await input.fill('')
  await input.press('Tab')
  const cleared = await save()
  check('清空单独设置后保存为平台默认', cleared.overrides && cleared.overrides.api === null, cleared)
  const again = await fetch(`${API}/api/v1/customers`, {
    headers: { authorization: `Bearer ${ctx.admin}` },
  })
  const limits = await json(`${API}/platform/v1/tenants/${ctx.tenantId}/rate-limits`, { token: ctx.ops })
  check('清空后恢复平台默认，请求正常', again.status === 200 && limits.effective.api === limits.defaults.api, {
    status: again.status,
    effective: limits.effective,
  })
}

// ---- 4. 系统健康里的消息分区 ----

async function healthSection(ctx) {
  const page = ctx.platformPage
  await page.locator('[data-testid="menu-health"]').click()
  const card = page.locator('.el-card', { hasText: '消息分区' })
  const visible = await seen(card)
  const text = visible ? await card.first().innerText() : ''
  const report = await json(`${API}/platform/v1/health`, { token: ctx.ops })
  const partitions = report.components.find((c) => c.key === 'partitions')
  await shot(page, '6-health-partitions')
  check(
    '系统健康显示消息分区正常，本月之后至少建好 3 个月',
    visible && partitions?.status === 'ok' && report.metrics.message_partitions_ahead >= 3,
    { text, partitions, ahead: report.metrics.message_partitions_ahead },
  )
}

// ---- 5. 指标 ----

async function metricsSection() {
  if (!METRICS_URLS) return
  const bodies = await Promise.all(
    METRICS_URLS.split(',').map((url) => fetch(url.trim()).then((r) => r.text())),
  )
  const all = bodies.join('\n')
  const expected = [
    'edp_http_requests_total{method="GET",route="/api/v1/customers"',
    'edp_events_processed_total{',
    'edp_message_partitions_ahead ',
    `edp_tenant_info{code="${TENANT}"`,
    'edp_rate_limited_total{rule="tenant-api"',
    'edp_job_last_success_timestamp_seconds{task="partitions"}',
  ]
  const missing = expected.filter((line) => !all.includes(line))
  check('三个进程的 /metrics 有请求、事件、分区、租户、限流和调度任务的指标', missing.length === 0, missing)
}

async function main() {
  if (!PLATFORM_PASSWORD) throw new Error('PLATFORM_PASSWORD is required')
  fs.mkdirSync(SHOTS, { recursive: true })
  const ctx = await prepareTenant()
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined })
  try {
    await chatSection(browser, ctx)
    await opsSection(browser, ctx)
    await rateLimitSection(ctx)
    await healthSection(ctx)
    await metricsSection()
  } catch (error) {
    check('脚本执行没有异常', false, String(error && error.stack ? error.stack : error))
  } finally {
    await browser.close()
  }
  check('没有前端脚本错误', summary.consoleErrors.length === 0, summary.consoleErrors)
  fs.writeFileSync(`${SHOTS}/summary.json`, JSON.stringify(summary, null, 2))
  console.log(JSON.stringify(summary, null, 2))
  if (summary.checks.some((c) => c.startsWith('FAIL'))) process.exit(1)
}

main()
