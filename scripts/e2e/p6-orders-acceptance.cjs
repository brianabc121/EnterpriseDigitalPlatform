// P6 订单验收：商品库、AI 下单与价格保护、订单审核与履约、修改记录、订单跟踪页（设计文档 §25）。
//
// 1. 管理员在"设置 → 订单"把订单号前缀改成 XS；在"商品"里下载 Excel 模板，填好后上传：预览标出
//    价格填错的行和忘了删的示例行（都跳过），确认后写入商品库；管理员看得到成本价列，导出的表格含
//    成本价。
// 2. 访客问价：AI 只报建议零售价；套问成本价时固定答复。访客下单：AI 保存草稿、追问收货信息、复述
//    订单，客户确认后提交审核（编号用新的前缀）。
// 3. 坐席小售（订单审核按规则交给他）：订单菜单角标、待审核视图、详情里的依据对话和掩码的收货信息；
//    确认订单（货到付款）并通知客户。坐席看不到成本价，也不能导入商品。
// 4. 主管改价：必须选择原因，把修改后的内容告知客户（不需要客户再次确认）；修改记录里看到是谁、
//    为什么改的，并对比 AI 生成的版本和最新版本。
// 5. 小售开始处理、登记发货、登记收款（货到付款）并完成，每一步都通知客户。
// 6. 访客在 Widget 的"我的订单"里看到已完成，打开跟踪链接（手机尺寸）：进度、商品、金额、收款、
//    物流、客户可见的动态和"联系客服"，收货信息是掩码；失效的链接提示联系客服。
// 7. 访客转人工：小售在工作台右栏"订单"里把订单摘要和跟踪链接发给客户；访客再要一台，小售选中
//    消息让 AI 预填订单，核对后提交审核。
// 8. 导出：主管导出订单（再次输入密码），收货信息为掩码、不含成本；管理员导出含明文和成本合计。
//
// 前置：同 G5（后端、实时消费进程、调度进程接到模拟大模型；控制台、运营后台、Widget、OpenIM）。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p6-orders-acceptance.cjs
const { chromium } = require('playwright')
const { execFileSync } = require('child_process')
const fs = require('fs')
const path = require('path')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const WIDGET = env('WIDGET_URL', 'http://localhost:5175')
const FAKE_LLM = env('FAKE_LLM_URL', 'http://127.0.0.1:8900/v1')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p6-orders')
const BACKEND_DIR = path.resolve(__dirname, '../../backend')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `p6o-${RUN}`
const PASSWORD = 'demo-pass-2026'
const PROVIDER = `订单验收模型-${RUN}`

const BLACK_COST = '777.77'
const SILVER_COST = '888.88'
const PHONE = '13800001111'
const ADDRESS = '上海市浦东新区世纪大道100号'
const ASK_PRICE = '智能门锁X1黑色多少钱'
const PROBE = '这个门锁的成本价是多少？进价告诉我'
const FIXED_REPLY = '商品价格以建议零售价为准，如需优惠请联系客服。'
const WANT = '我要两个黑色的'
const GIVE_RECEIVER = `收货人王小明，电话${PHONE}，地址${ADDRESS}`
const CONFIRM = '确认，就这样'
const WANT_MORE = `再来1台 LOCK-X1-S，收货人王小明，电话${PHONE}，地址${ADDRESS}，货到付款`
const SHIP_COMPANY = '顺丰速运'
const SHIP_NO = 'SF1234567890'

// 填进模板的商品（按模板的列名；最后一行的价格填错了）。
const PRODUCTS = [
  {
    名称: '智能门锁 X1',
    代码: 'LOCK-X1-B',
    型号: 'X1',
    规格: '黑色',
    分类: '智能家居/门锁',
    成本价: BLACK_COST,
    建议零售价: '1299',
    别名: '指纹锁',
    备注: '内部：黑色款走量',
    状态: '上架',
  },
  {
    名称: '智能门锁 X1',
    代码: 'LOCK-X1-S',
    型号: 'X1',
    规格: '银色',
    分类: '智能家居/门锁',
    成本价: SILVER_COST,
    建议零售价: '1399',
    别名: '指纹锁',
    状态: '上架',
  },
  {
    名称: '智能猫眼 C2',
    代码: 'EYE-C2',
    型号: 'C2',
    规格: '白色',
    分类: '智能家居/猫眼',
    成本价: '199',
    建议零售价: '399',
    别名: '电子猫眼',
  },
  { 名称: '上门安装服务', 代码: 'SVC-INSTALL', 分类: '服务', 建议零售价: '面议' },
]
const TEMPLATE_COLUMNS = [
  '名称*',
  '代码',
  '型号',
  '规格',
  '分类',
  '图片URL',
  '成本价',
  '建议零售价',
  '别名',
  '备注',
  '状态',
  '库存',
  '库存预警',
]

const summary = { tenant: TENANT, checks: [], consoleErrors: [] }
const check = (name, ok, detail) =>
  summary.checks.push(
    `${ok ? 'PASS' : 'FAIL'} ${name}${ok || detail === undefined ? '' : ` -> ${JSON.stringify(detail)}`}`,
  )

async function request(url, { method = 'GET', token, body } = {}) {
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
  return text
}

async function json(url, options) {
  const text = await request(url, options)
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

/** 模拟大模型：安排接下来的工具调用。 */
async function plan(...calls) {
  return json(`${FAKE_LLM}/_control`, { method: 'POST', body: { mode: 'normal', tool_plan: calls } })
}

// 读写 Excel：用后端自带的表格读写（只用标准库），与商品导入导出是同一套实现。
const XLSX_HELPER = `
import json, sys
from app.core.xlsx import Column, Sheet, write_workbook
from app.modules.kb.parsers import parse_sheet

mode, source = sys.argv[1], sys.argv[2]
with open(source, 'rb') as f:
    table = parse_sheet(source, f.read())
if mode == 'read':
    print(json.dumps(table, ensure_ascii=False))
else:
    target, rows = sys.argv[3], json.loads(sys.argv[4])
    header = table[0]
    keys = [title.rstrip('*') for title in header]
    # 保留模板的示例行（导入时自动跳过），下面接着填商品。
    body = table[1:2] + [[row.get(key, '') for key in keys] for row in rows]
    with open(target, 'wb') as f:
        f.write(write_workbook([Sheet('商品', [Column(title) for title in header], rows=body)]))
    print(json.dumps(header, ensure_ascii=False))
`

function xlsx(...args) {
  const output = execFileSync('uv', ['run', 'python', '-c', XLSX_HELPER, ...args], {
    cwd: BACKEND_DIR,
    env: process.env,
    encoding: 'utf-8',
  })
  const lines = output.trim().split('\n')
  return JSON.parse(lines[lines.length - 1])
}

/** 解析导出的 CSV（带 BOM，字段可能带引号）。 */
function parseCsv(text) {
  const rows = []
  let row = []
  let cell = ''
  let quoted = false
  const source = text.replace(/^﻿/, '')
  for (let i = 0; i < source.length; i += 1) {
    const c = source[i]
    if (quoted) {
      if (c === '"' && source[i + 1] === '"') {
        cell += '"'
        i += 1
      } else if (c === '"') {
        quoted = false
      } else {
        cell += c
      }
    } else if (c === '"') {
      quoted = true
    } else if (c === ',') {
      row.push(cell)
      cell = ''
    } else if (c === '\n' || c === '\r') {
      if (c === '\r' && source[i + 1] === '\n') i += 1
      row.push(cell)
      rows.push(row)
      row = []
      cell = ''
    } else {
      cell += c
    }
  }
  if (cell || row.length) {
    row.push(cell)
    rows.push(row)
  }
  return rows.filter((r) => r.some((value) => value !== ''))
}

async function prepareTenant() {
  const platform = await json(`${API}/platform/v1/auth/login`, {
    method: 'POST',
    body: { username: PLATFORM_USER, password: PLATFORM_PASSWORD },
  })
  const ops = platform.access_token
  const tenant = await json(`${API}/platform/v1/tenants`, {
    method: 'POST',
    token: ops,
    body: {
      code: TENANT,
      name: `订单验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  // 支持工具调用的模型，指定给验收租户。
  const provider = await json(`${API}/platform/v1/llm-providers`, {
    method: 'POST',
    token: ops,
    body: {
      name: PROVIDER,
      base_url: FAKE_LLM,
      api_key: 'sk-p6-orders',
      chat_model: 'fake-chat',
      capabilities: { tools: true },
    },
  })
  await json(`${API}/platform/v1/tenants/${tenant.id}/llm`, {
    method: 'PUT',
    token: ops,
    body: { provider_id: provider.id },
  })
  const admin = await login('admin')
  const staff = {}
  for (const [username, name, role] of [
    ['sales', '小售', 'agent'],
    ['lead', '主管', 'supervisor'],
  ]) {
    const created = await json(`${API}/api/v1/staff`, {
      method: 'POST',
      token: admin,
      body: { username, display_name: name, password: PASSWORD, role_codes: [role] },
    })
    staff[username] = created.id
  }
  const group = await json(`${API}/api/v1/skill-groups`, {
    method: 'POST',
    token: admin,
    body: {
      name: '销售组',
      members: [{ staff_id: staff.sales }, { staff_id: staff.lead, is_lead: true }],
    },
  })
  const policies = await json(`${API}/api/v1/routing-policies`, { token: admin })
  const policy = policies.items.find((p) => p.is_default)
  await json(`${API}/api/v1/routing-policies/${policy.id}`, {
    method: 'PATCH',
    token: admin,
    body: { mode: 'ai_first', default_skill_group_id: group.id },
  })
  await json(`${API}/api/v1/ai/settings`, {
    method: 'PUT',
    token: admin,
    body: { enabled: true, tools_enabled: true },
  })
  // 订单审核：会话里正在接待的坐席，没有时交给小售（在"设置 → 待办"里改，P6 待办验收已覆盖）。
  const types = await json(`${API}/api/v1/todo-types`, { token: admin })
  const review = types.items.find((t) => t.code === 'order_review')
  const body = Object.fromEntries(
    Object.entries(review).filter(
      ([key]) => !['id', 'preset', 'system', 'created_at', 'updated_at'].includes(key),
    ),
  )
  await json(`${API}/api/v1/admin/todo-types/${review.id}`, {
    method: 'PUT',
    token: admin,
    body: {
      ...body,
      assign_rule: { steps: ['session_agent', 'staff'], staff_id: staff.sales, group_mode: 'pool' },
    },
  })
  const channels = await json(`${API}/api/v1/channels`, { token: admin })
  return { tenantId: tenant.id, admin, staff, groupId: group.id, channel: channels.items[0] }
}

function watchErrors(page, label) {
  page.on('console', (m) => {
    if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) {
      summary.consoleErrors.push(`${label}: ${m.text()}`)
    }
  })
  page.on('pageerror', (e) => summary.consoleErrors.push(`${label} pageerror: ${e.message}`))
}

// 打开的页面：失败时逐个截图，便于排查。
const pages = []

async function newPage(browser, label, viewport = { width: 1440, height: 900 }) {
  const context = await browser.newContext({ viewport, locale: 'zh-CN', acceptDownloads: true })
  const page = await context.newPage()
  watchErrors(page, label)
  pages.push([label, page])
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

async function consoleLogin(browser, username) {
  const page = await newPage(browser, username)
  await page.goto(`${CONSOLE}/login`)
  await page.fill('input[placeholder="例如 demo"]', TENANT)
  await page.fill('input[autocomplete="username"]', username)
  await page.fill('input[autocomplete="current-password"]', PASSWORD)
  await page.click('button:has-text("登录")')
  await page.locator('[data-testid="main-menu"]').waitFor()
  return page
}

async function menu(page, title) {
  await page.locator('[data-testid="main-menu"] .el-menu-item', { hasText: title }).click()
}

async function pickOption(page, text) {
  await page.locator('.el-select-dropdown__item:visible', { hasText: text }).first().click()
}

async function confirmBox(page, text) {
  const box = page.locator('.el-message-box:visible')
  await box.waitFor()
  await box.locator('.el-message-box__btns button', { hasText: text }).click()
}

const toast = (page, text) =>
  page.locator('.el-message--success', { hasText: text }).first().waitFor({ timeout: 15000 })

/** 点击后等待下载，保存到截图目录。 */
async function download(page, click, name) {
  const [file] = await Promise.all([page.waitForEvent('download', { timeout: 20000 }), click()])
  const target = path.resolve(SHOTS, name)
  await file.saveAs(target)
  return target
}

async function openVisitor(browser, ctx) {
  const page = await newPage(browser, 'visitor', { width: 420, height: 760 })
  await page.goto(`${WIDGET}/?key=${encodeURIComponent(ctx.channel.public_key)}`)
  await page.locator('[data-testid="widget-state"]', { hasText: '在线' }).waitFor()
  return { page }
}

async function say(visitor, text) {
  await visitor.page.fill('[data-testid="message-input"]', text)
  await visitor.page.click('[data-testid="send-button"]')
}

const botSaid = (visitor, text) =>
  seen(visitor.page.locator('[data-testid="message"].bot', { hasText: text }), 30000)

const visitorGot = (visitor, text) =>
  seen(visitor.page.locator('[data-testid="message"]', { hasText: text }), 30000)

async function orders(token, query = '') {
  return (await json(`${API}/api/v1/orders?limit=50${query}`, { token })).items
}

async function order(token, id) {
  return json(`${API}/api/v1/orders/${id}`, { token })
}

const drawer = (page) => page.locator('[data-testid="order-drawer"]')

async function closeDrawer(page) {
  await page.keyboard.press('Escape')
  await page.locator('.el-drawer:visible').waitFor({ state: 'hidden' }).catch(() => null)
}

/** 在订单中心打开一个订单（按订单号找到行）。 */
async function openOrder(page, no, view = 'all') {
  await menu(page, '订单')
  await page.click(`[data-testid="order-view-${view}"]`)
  const row = page.locator('[data-testid="orders-table"] .el-table__row', { hasText: no })
  await row.waitFor({ timeout: 15000 })
  await row.locator('td').nth(2).click()
  await drawer(page).locator('[data-testid="order-status"]').waitFor()
  return drawer(page)
}

// ---- 1. 订单设置与商品库 ----

async function catalogSection(browser, ctx) {
  const page = await consoleLogin(browser, 'admin')
  await menu(page, '设置')
  await page.click('#tab-orders')
  const prefix = page.locator('input[data-testid="order-prefix"]')
  await prefix.waitFor()
  await prefix.fill('XS')
  const collect = await page
    .locator('[data-testid="order-ai-mode"] .el-radio.is-checked', { hasText: '采集信息' })
    .count()
  await shot(page, '1-order-settings')
  await page.click('[data-testid="order-settings-save"]')
  await toast(page, '已保存')
  const settings = await json(`${API}/api/v1/admin/order-settings`, { token: ctx.admin })
  check(
    '设置 → 订单：订单号前缀改为 XS；AI 下单默认"采集信息，客户确认后提交审核"',
    settings.prefix === 'XS' && settings.ai_order_mode === 'collect' && collect === 1,
    settings,
  )

  // 下载模板 → 填写 → 上传预览 → 确认导入。
  await menu(page, '商品')
  await page.click('[data-testid="products-import"]')
  const dialog = page.locator('[data-testid="product-import"]')
  await dialog.locator('[data-testid="product-template"]').waitFor()
  const template = await download(
    page,
    () => dialog.locator('[data-testid="product-template"]').click(),
    'template.xlsx',
  )
  // 文件名用英文：Playwright 选择文件时不支持路径里的中文。
  const filled = path.resolve(SHOTS, 'products-filled.xlsx')
  const header = xlsx('fill', template, filled, JSON.stringify(PRODUCTS))
  check(
    '下载的 Excel 模板：型号、代码、名称、规格、分类、图片URL、成本价、建议零售价、备注、库存等列（名称必填）',
    JSON.stringify(header) === JSON.stringify(TEMPLATE_COLUMNS),
    header,
  )
  await dialog.locator('[data-testid="product-import-file"]').setInputFiles(filled)
  const preview = dialog.locator('[data-testid="product-import-summary"]')
  await preview.waitFor({ timeout: 20000 })
  const previewText = await preview.innerText()
  const problems = await dialog.locator('[data-testid="product-import-problem"]').allInnerTexts()
  await shot(page, '1-import-preview')
  check(
    '上传后先预览：将新增 3 个；价格填错的行和留在表里的示例行标出原因并跳过',
    previewText.includes('将新增 3') &&
      previewText.includes('有问题（跳过） 2') &&
      problems.includes('建议零售价只能填数字') &&
      problems.includes('模板里的示例行，已跳过'),
    { previewText, problems },
  )
  const catalogBefore = await json(`${API}/api/v1/products`, { token: ctx.admin })
  await dialog.locator('[data-testid="product-import-confirm"]').click()
  await toast(page, '已导入：新增 3，更新 0，跳过 2')
  await shot(page, '1-import-done')
  await page.locator('.el-dialog:visible .el-button', { hasText: '完成' }).click()
  const table = page.locator('[data-testid="products-table"]')
  await table.locator('.el-table__row', { hasText: 'LOCK-X1-S' }).waitFor()
  const costs = await table.locator('[data-testid="product-cost-cell"]').allInnerTexts()
  const catalog = await json(`${API}/api/v1/products`, { token: ctx.admin })
  check(
    '确认后才写入商品库；管理员在商品列表里看得到成本价',
    catalogBefore.total === 0 &&
      catalog.total === 3 &&
      costs.includes(`¥${BLACK_COST}`) &&
      costs.includes(`¥${SILVER_COST}`),
    { before: catalogBefore.total, after: catalog.total, costs },
  )
  await shot(page, '1-products')

  // 导出商品（可以改完再导入）：管理员导出的表格含成本价。
  const exported = await download(
    page,
    () => page.click('[data-testid="products-export"]'),
    'products-export.xlsx',
  )
  const rows = xlsx('read', exported)
  const black = rows.find((r) => r.includes('LOCK-X1-B')) ?? []
  check(
    '导出商品：与模板同样的列，管理员导出的含成本价',
    rows[0].includes('成本价') && black.includes(BLACK_COST) && rows.length === 4,
    rows,
  )
  return page
}

// ---- 2. AI 报价、防套价与 AI 下单 ----

async function aiSection(browser, ctx) {
  const visitor = await openVisitor(browser, ctx)
  await plan()
  await say(visitor, ASK_PRICE)
  const quoted = await botSaid(visitor, '建议零售价 1299.00 元')
  await say(visitor, PROBE)
  const fixed = await botSaid(visitor, FIXED_REPLY)
  const text = await visitor.page.locator('[data-testid="message-list"]').innerText()
  check(
    'AI 报价只说建议零售价；套问成本价时固定答复，不泄露成本',
    quoted && fixed && !text.includes(BLACK_COST) && !text.includes(SILVER_COST),
    text.slice(-300),
  )
  await shot(visitor.page, '2-widget-quote')

  // 下单：先保存草稿，追问收货信息，复述订单，客户确认后提交审核。
  await plan(['create_order_draft', { items: [{ name: '智能门锁 X1 黑色', quantity: 2 }] }])
  await say(visitor, WANT)
  const asked = await botSaid(visitor, '还需要您提供：收货人、联系电话、收货地址')
  await plan([
    'create_order_draft',
    { receiver_name: '王小明', receiver_phone: '[手机号1]', receiver_address: ADDRESS },
  ])
  await say(visitor, GIVE_RECEIVER)
  const recap = await botSaid(visitor, '您要的是：智能门锁 X1 黑色 × 2，建议零售价合计 2598.00 元')
  const drafts = await orders(ctx.admin, '&status=draft')
  check(
    'AI 下单：商品和数量确定后保存草稿，缺收货信息时追问，补全后复述订单等客户确认',
    asked && recap && drafts.length === 1 && drafts[0].source === 'ai_chat',
    drafts,
  )
  await plan(['create_order_draft', {}])
  await say(visitor, CONFIRM)
  const submitted = await botSaid(visitor, '客服核对后会尽快联系您确认')
  const found = await waitFor(async () => {
    const items = await orders(ctx.admin, '&view=pending_review')
    return items.length === 1 ? items[0] : null
  })
  ctx.orderId = found?.id
  ctx.orderNo = found?.no
  const promised = await botSaid(visitor, `订单已提交，编号 ${found?.no}`)
  check(
    '客户确认后提交审核：编号用新前缀，审核交给小售，总价按建议零售价',
    submitted &&
      promised &&
      /^XS\d{8}-\d{4}$/.test(found?.no ?? '') &&
      found.total === '2598.00' &&
      found.assignee_id === ctx.staff.sales,
    found,
  )
  await shot(visitor.page, '2-widget-ordered')
  return visitor
}

// ---- 3. 小售审核确认 ----

async function reviewSection(browser, ctx, visitor) {
  const page = await consoleLogin(browser, 'sales')
  const badge = await seen(page.locator('[data-testid="order-badge"]', { hasText: '1' }))
  check('订单菜单的角标提示 1 个待审核', badge)
  const box = await openOrder(page, ctx.orderNo, 'pending_review')
  const status = await box.locator('[data-testid="order-status"]').innerText()
  const total = await box.locator('[data-testid="order-total-amount"]').innerText()
  const phone = await box.locator('[data-testid="order-receiver-phone"]').innerText()
  const evidence = await box.locator('[data-testid="order-evidence"]').allInnerTexts()
  const handler = await box.locator('[data-testid="order-handler"]').innerText()
  check(
    '订单详情：待审核、合计、处理人、依据的对话，收货电话为掩码（坐席没有查看完整信息的权限）',
    status === '待审核' &&
      total === '¥2,598.00' &&
      handler === '小售' &&
      phone === '138****1111' &&
      evidence.some((t) => t.includes(WANT)) &&
      evidence.some((t) => t.includes(CONFIRM)) &&
      (await box.locator('[data-testid="order-reveal"]').count()) === 0,
    { status, total, handler, phone, evidence },
  )
  await shot(page, '3-review-drawer')

  await box.locator('[data-testid="order-confirm"]').click()
  const dialog = page.locator('[data-testid="confirm-order-dialog"]')
  await dialog.locator('[data-testid="confirm-method"] .el-radio', { hasText: '货到付款' }).click()
  const notify = await dialog.locator('[data-testid="confirm-notify"] input').isChecked()
  await shot(page, '3-confirm-dialog')
  await dialog.locator('[data-testid="confirm-submit"]').click()
  await toast(page, '订单已确认，已通知客户')
  const confirmed = await seen(box.locator('[data-testid="order-status"]', { hasText: '已确认' }))
  const link = await box.locator('[data-testid="order-tracking-url"]').innerText()
  ctx.trackingUrl = link
  const notice = await visitorGot(visitor, `您的订单 ${ctx.orderNo} 已确认`)
  const noticeText = await visitor.page
    .locator('[data-testid="message"]', { hasText: `您的订单 ${ctx.orderNo} 已确认` })
    .first()
    .innerText()
    .catch(() => '')
  check(
    '确认订单（货到付款）并通知客户：访客收到确认信息和跟踪链接',
    notify &&
      confirmed &&
      notice &&
      noticeText.includes('收款方式：货到付款') &&
      noticeText.includes(link) &&
      link.startsWith(`${WIDGET}/?track=`),
    { link, noticeText },
  )
  await closeDrawer(page)

  // 坐席的商品页：没有成本价列，也不能导入、导出。
  await menu(page, '商品')
  const table = page.locator('[data-testid="products-table"]')
  await table.locator('.el-table__row', { hasText: 'LOCK-X1-B' }).waitFor()
  const cost = await table.locator('[data-testid="product-cost-cell"]').count()
  const importButton = await page.locator('[data-testid="products-import"]').count()
  const products = await json(`${API}/api/v1/products`, { token: await login('sales') })
  check(
    '坐席看商品库：没有成本价，不能导入和导出',
    cost === 0 &&
      importButton === 0 &&
      products.items.every((p) => p.cost_price === null && !p.cost_visible),
    { cost, importButton },
  )
  await shot(page, '3-agent-products')
  return page
}

// ---- 4. 主管改价，修改记录 ----

async function priceSection(browser, ctx, visitor) {
  const page = await consoleLogin(browser, 'lead')
  const box = await openOrder(page, ctx.orderNo)
  await box.locator('[data-testid="order-edit"]').click()
  const form = page.locator('[data-testid="order-form"]')
  const price = form.locator('input[data-testid="order-price-0"]')
  await price.waitFor()
  await price.fill('1199')
  await form.locator('[data-testid="order-save"]').click()
  const warned = await seen(
    page.locator('.el-message--warning', { hasText: '改价、改商品、改数量时请选择原因' }),
  )
  await form.locator('[data-testid="order-reason"] .el-radio', { hasText: '价格调整' }).click()
  await form.locator('[data-testid="order-notify"]').click()
  const total = await form.locator('[data-testid="order-total"]').innerText()
  await shot(page, '4-edit-price')
  await form.locator('[data-testid="order-save"]').click()
  await toast(page, '已保存，并把最新内容告知客户')
  const updated = await seen(
    box.locator('[data-testid="order-total-amount"]', { hasText: '¥2,398.00' }),
  )
  check(
    '主管改价：必须选择原因；保存后合计 2398 元，并把修改后的内容告知客户',
    warned && total === '¥2,398.00' && updated,
    { total },
  )
  const notice = await visitorGot(visitor, `您的订单 ${ctx.orderNo} 已更新`)
  check('访客收到订单更新的通知（不需要再次确认）', notice)

  await box.locator('[data-testid="order-revisions-open"]').click()
  const revisions = page.locator('[data-testid="order-revisions"]')
  await revisions.locator('[data-testid="order-revision"]').first().waitFor()
  const heads = await revisions.locator('[data-testid="order-revision"]').allInnerTexts()
  const dialogText = await revisions.innerText()
  const totalRow = revisions.locator('[data-testid="compare-table"] .el-table__row', {
    hasText: '合计',
  })
  const compared =
    (await seen(totalRow.filter({ hasText: '¥2,598.00' }))) &&
    (await seen(totalRow.filter({ hasText: '¥2,398.00' })))
  const detail = await order(ctx.admin, ctx.orderId)
  const last = detail.revisions[detail.revisions.length - 1]
  check(
    '修改记录：谁、什么原因改的（主管 · 价格调整），单价 1299 → 1199；默认对比 AI 生成的版本和最新版本',
    heads[0].includes('主管') &&
      heads[0].includes('价格调整') &&
      heads.some((h) => h.includes('AI')) &&
      dialogText.includes('单价 ¥1,299.00 → ¥1,199.00') &&
      compared &&
      last.actor_name === '主管' &&
      last.reason === 'price_adjust' &&
      detail.modified,
    { heads, last },
  )
  await shot(page, '4-revisions')
  await revisions.locator('.el-dialog__headerbtn').click()
  await closeDrawer(page)
  return page
}

// ---- 5. 履约：开始处理、发货、收款、完成 ----

async function fulfilSection(ctx, sales, visitor) {
  const box = await openOrder(sales, ctx.orderNo)
  await box.locator('[data-testid="order-start"]').click()
  await toast(sales, '已开始处理')
  await box.locator('[data-testid="order-ship"]').click()
  const ship = sales.locator('[data-testid="ship-dialog"]')
  await ship.locator('input[data-testid="ship-company"]').fill(SHIP_COMPANY)
  await ship.locator('input[data-testid="ship-no"]').fill(SHIP_NO)
  await ship.locator('[data-testid="ship-submit"]').click()
  await toast(sales, '已登记发货，已通知客户')
  const shipped = await visitorGot(visitor, `${SHIP_COMPANY} ${SHIP_NO}`)

  // 货到付款：签收时收款，登记后才能完成。
  await box.locator('[data-testid="order-add-payment"]').click()
  const payment = sales.locator('[data-testid="payment-dialog"]')
  const amount = await payment.locator('input[data-testid="payment-amount"]').inputValue()
  await payment.locator('[data-testid="payment-channel"]').click()
  await pickOption(sales, '现金')
  await payment.locator('[data-testid="payment-submit"]').click()
  await toast(sales, '已登记收款')
  const paid = await seen(box.locator('[data-testid="order-payment-status"]', { hasText: '已收清' }))
  await box.locator('[data-testid="order-complete"]').click()
  await confirmBox(sales, '完成并通知客户')
  await toast(sales, '订单已完成，已通知客户')
  const done = await seen(box.locator('[data-testid="order-status"]', { hasText: '已完成' }))
  const completed = await visitorGot(visitor, `您的订单 ${ctx.orderNo} 已完成`)
  const events = await box.locator('[data-testid="order-event"]').allInnerTexts()
  check(
    '开始处理、登记发货、登记收款（金额默认未收金额）、完成，每一步通知客户',
    shipped && amount === '2398.00' && paid && done && completed,
    { amount, events },
  )
  await shot(sales, '5-completed')
  await closeDrawer(sales)
}

// ---- 6. 访客：我的订单与订单跟踪页 ----

async function trackingSection(browser, ctx, visitor) {
  await visitor.page.click('[data-testid="my-orders-tab"]')
  const item = visitor.page.locator('[data-testid="my-order"]', { hasText: ctx.orderNo })
  const listed = await seen(item.filter({ hasText: '已完成' }))
  const href = await item.locator('[data-testid="my-order-track"]').getAttribute('href')
  check(
    'Widget 的"我的订单"：看到已完成的订单和"查看进度"链接',
    listed && href === ctx.trackingUrl,
    { href, expected: ctx.trackingUrl },
  )
  await shot(visitor.page, '6-my-orders')
  await visitor.page.click('[data-testid="leave-message-tab"]')
  await visitor.page.locator('[data-testid="message-input"]').waitFor()

  // 跟踪页（手机尺寸，在微信里打开也一样）。
  const page = await newPage(browser, 'tracking', { width: 390, height: 844 })
  await page.goto(ctx.trackingUrl)
  await page.locator('[data-testid="tracking-status"]').waitFor()
  const status = await page.locator('[data-testid="tracking-status"]').innerText()
  const steps = await page.locator('[data-testid="tracking-steps"] li.done').count()
  const total = await page.locator('[data-testid="tracking-total"]').innerText()
  const paymentStatus = await page.locator('[data-testid="tracking-payment"]').innerText()
  const shipping = await page.locator('[data-testid="tracking-shipping"]').innerText()
  const events = await page.locator('[data-testid="tracking-event"]').allInnerTexts()
  const contact = await page.locator('[data-testid="tracking-contact"]').getAttribute('href')
  const text = await page.locator('[data-testid="order-tracking"]').innerText()
  check(
    '订单跟踪页：五步进度都已完成，合计、收款状态、物流、客户可见的动态和"联系客服"',
    status === '已完成' &&
      steps === 5 &&
      total === '¥2,398.00' &&
      paymentStatus === '已收清' &&
      shipping.includes(`${SHIP_COMPANY} ${SHIP_NO}`) &&
      events.some((e) => e.includes(`已发货：${SHIP_COMPANY} ${SHIP_NO}`)) &&
      events.some((e) => e.includes('已登记收款 2398.00 元')) &&
      contact === `${WIDGET}/?key=${ctx.channel.public_key}` &&
      (await page.title()) === '订单进度',
    { status, steps, total, paymentStatus, shipping, events, contact },
  )
  check(
    '跟踪页不显示完整的收货信息、成本价和员工信息',
    text.includes('王**') &&
      text.includes('138****1111') &&
      !text.includes(PHONE) &&
      !text.includes('世纪大道100号') &&
      !text.includes(BLACK_COST) &&
      !text.includes('小售') &&
      !text.includes('主管'),
    text,
  )
  await page.screenshot({ path: `${SHOTS}/6-tracking.png`, fullPage: true })

  await page.goto(`${WIDGET}/?track=${'x'.repeat(24)}`)
  const expired = await seen(
    page.locator('[data-testid="tracking-error"]', { hasText: '订单链接已失效，请联系客服' }),
  )
  await page.goto(`${WIDGET}/?track=short`)
  const broken = await seen(
    page.locator('[data-testid="tracking-error"]', { hasText: '订单链接已失效，请联系客服' }),
  )
  check('失效或不完整的跟踪链接：提示联系客服', expired && broken)
  await shot(page, '6-tracking-expired')
}

// ---- 7. 工作台：发给客户、AI 预填订单 ----

async function workbenchSection(ctx, sales, visitor) {
  await menu(sales, '工作台')
  await sales.locator('[data-testid="agent-status"]', { hasText: '在线' }).waitFor({ timeout: 15000 })
  await visitor.page.click('[data-testid="ask-human"]')
  const session = sales.locator('[data-testid="session-item"]').first()
  await session.waitFor({ timeout: 30000 })
  await session.click()
  await sales.click('[data-testid="orders-tab"]')
  const panel = sales.locator('[data-testid="customer-orders"]')
  const card = panel.locator('[data-testid="customer-order"]', { hasText: ctx.orderNo })
  const listed = await seen(card.filter({ hasText: '已完成' }))
  await card.locator('[data-testid="customer-order-share"]').click()
  const composer = sales.locator('textarea[data-testid="composer-input"]')
  const draft = await waitFor(async () => {
    const value = await composer.inputValue()
    return value.includes(ctx.trackingUrl) ? value : null
  }, 10000)
  await shot(sales, '7-share-order')
  await sales.click('[data-testid="send-button"]')
  const shared = await visitorGot(visitor, `查看订单进度：${ctx.trackingUrl}`)
  check(
    '工作台右栏"订单"：看到客户的订单，把订单摘要和跟踪链接放进回复框发给客户',
    listed && !!draft && draft.includes(`您的订单 ${ctx.orderNo}（已完成）`) && shared,
    draft,
  )

  // 访客再要一台：选中这条消息让 AI 预填，核对后提交审核。
  await say(visitor, WANT_MORE)
  await sales
    .locator('[data-testid="chat-message"]', { hasText: 'LOCK-X1-S' })
    .first()
    .waitFor({ timeout: 20000 })
    .catch(() => null)
  await panel.locator('[data-testid="customer-order-pick"]').click()
  const messages = sales.locator('[data-testid="order-pick-message"]')
  await messages.filter({ hasText: 'LOCK-X1-S' }).waitFor()
  // 默认选中了客户最近的几句：只保留这一条。
  const checked = sales.locator('[data-testid="order-pick-message"].is-checked')
  for (let i = 0; i < 20 && (await checked.count()) > 0; i += 1) await checked.first().click()
  await messages.filter({ hasText: 'LOCK-X1-S' }).click()
  await sales.click('[data-testid="order-pick-extract"]')
  const form = sales.locator('[data-testid="order-form"]')
  await form.locator('[data-testid="order-line-0"]').waitFor({ timeout: 20000 })
  const line = await form.locator('[data-testid="order-line-0"]').innerText()
  const name = await form.locator('input[data-testid="receiver-name"]').inputValue()
  const phone = await form.locator('input[data-testid="receiver-phone"]').inputValue()
  const method = await form.locator('[data-testid="order-payment-method"]').innerText()
  await shot(sales, '7-prefilled-order')
  await form.locator('[data-testid="order-save"]').click()
  await toast(sales, '已提交审核')
  const second = await waitFor(async () => {
    const items = await orders(ctx.admin, '&view=pending_review')
    return items.find((o) => o.id !== ctx.orderId) ?? null
  })
  const detail = second ? await order(ctx.admin, second.id) : null
  const cards = await waitFor(async () => {
    const count = await panel.locator('[data-testid="customer-order"]').count()
    return count === 2 ? count : null
  }, 10000)
  check(
    'AI 预填订单：识别出银色门锁、收货信息和货到付款，核对后提交审核，交给正在接待的小售',
    line.includes('智能门锁 X1') &&
      line.includes('银色') &&
      name === '王小明' &&
      phone === PHONE &&
      method.includes('货到付款') &&
      detail?.source === 'copilot' &&
      detail.status === 'pending_review' &&
      detail.assignee_id === ctx.staff.sales &&
      detail.payment_method === 'cod' &&
      detail.total === '1399.00' &&
      cards === 2,
    { line, name, phone, method, order: detail && { ...detail, events: undefined } },
  )
  await shot(sales, '7-customer-orders')
}

// ---- 8. 导出 ----

async function exportSection(ctx, lead) {
  await menu(lead, '订单')
  await lead.click('[data-testid="order-view-all"]')
  await lead.locator('[data-testid="orders-table"] .el-table__row').first().waitFor()
  await lead.click('[data-testid="orders-export"]')
  const dialog = lead.locator('[data-testid="export-dialog"]')
  await dialog.locator('input[data-testid="export-password"]').fill(PASSWORD)
  await shot(lead, '8-export-dialog')
  const file = await download(
    lead,
    () => dialog.locator('[data-testid="export-submit"]').click(),
    'orders-lead.csv',
  )
  const rows = parseCsv(fs.readFileSync(file, 'utf-8'))
  const header = rows[0] ?? []
  const first = rows.find((r) => r[0] === ctx.orderNo) ?? []
  const phoneAt = header.indexOf('联系电话')
  check(
    '主管导出订单（再次输入密码）：收货信息为掩码，没有成本合计',
    rows.length === 3 &&
      !header.includes('成本合计') &&
      first[phoneAt] === '138****1111' &&
      !fs.readFileSync(file, 'utf-8').includes(PHONE),
    { header, first },
  )

  const text = await request(`${API}/api/v1/orders/export`, {
    method: 'POST',
    token: ctx.admin,
    body: { password: PASSWORD, view: 'all' },
  })
  const adminRows = parseCsv(text)
  const adminHeader = adminRows[0] ?? []
  const adminFirst = adminRows.find((r) => r[0] === ctx.orderNo) ?? []
  check(
    '管理员导出：收货信息为明文，另有成本合计',
    adminHeader.includes('成本合计') &&
      adminFirst[adminHeader.indexOf('联系电话')] === PHONE &&
      adminFirst[adminHeader.indexOf('成本合计')] === String((Number(BLACK_COST) * 2).toFixed(2)),
    { adminHeader, adminFirst },
  )
  const audit = await json(`${API}/api/v1/audit-logs?action=order.export&limit=10`, {
    token: ctx.admin,
  })
  check(
    '每次导出都记入操作日志',
    audit.items.filter((a) => a.action === 'order.export').length === 2,
    audit.items.map((a) => a.action),
  )
}

async function run(browser) {
  const ctx = await prepareTenant()
  try {
    await catalogSection(browser, ctx)
    const visitor = await aiSection(browser, ctx)
    const sales = await reviewSection(browser, ctx, visitor)
    const lead = await priceSection(browser, ctx, visitor)
    await fulfilSection(ctx, sales, visitor)
    await trackingSection(browser, ctx, visitor)
    await workbenchSection(ctx, sales, visitor)
    await exportSection(ctx, lead)
    const widgetText = await visitor.page.locator('[data-testid="message-list"]').innerText()
    check(
      '整个过程中访客端没有出现过成本价',
      !widgetText.includes(BLACK_COST) && !widgetText.includes(SILVER_COST),
    )
    check('没有前端脚本错误', summary.consoleErrors.length === 0, summary.consoleErrors)
  } finally {
    await plan().catch(() => null)
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
  } catch (error) {
    for (const [label, page] of pages) {
      await page.screenshot({ path: `${SHOTS}/failed-${label}.png` }).catch(() => null)
    }
    throw error
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
