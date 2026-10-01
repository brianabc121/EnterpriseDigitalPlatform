// P14 验收：表单填写知识库——开单越用越准（设计文档 §25.18）。
//
// 1. 以往 3 张防盗门订单的领料单都已确认：每樘实际领合页 4 个（配方 3 个）、另外领膨胀螺丝 8 个。
//    系统学到"膨胀螺丝 8 个/樘"（配方里没有、常领，生效），合页和配方不一致标"待确认"（不改配方）。
// 2. 客服小美以往下单时把 1.5m×1.8m 的铝合金窗叫"大窗"：第一次在录入行输入"大窗"只有相近的，
//    换成规格才找到；提交后学到"大窗"（第二次）开始生效。纱窗总和大窗一起开（3 张里 3 张）。
// 3. 再下单：输入"大窗"，大窗排在最前面、标"学到的"，回车加入；录入行空着时点一下，先列出
//    "常一起开"的纱窗（写明一起开过几次），回车加入。
// 4. 工人老王领取防盗门订单：自动填好的领料单里有膨胀螺丝 16 个（标"学到的"，写明依据），合页仍按
//    配方 6 个。
// 5. 管理员打开知识库的"表单知识"：各状态的数量；知识写成一句话；点"待确认"只看待确认的，打开合页的
//    用量（实际约 4 个，配方 3 个），"更新配方"后配方改为 4 个，之后的领料单按新配方。
// 6. 学习记录：每次提交的表单和判断结果（学到、生效、待确认、用到）。
// 7. 手工新增"小窗"→ 1.2m×1.5m 的铝合金窗：立即生效、固定；联想里"小窗"找到它。
// 8. 设置里关掉"自动生效"：之后学到的"小门"标"待确认"，确认生效后联想里才用上。
// 9. 坐席（没有维护权限）能看表单知识，没有新增、设置和处理的按钮。
//
// 前置：后端（含 worker：学习在 worker 里判断）、控制台。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p14-form-knowledge-acceptance.cjs
const { chromium } = require('playwright')
const fs = require('fs')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p14-form-knowledge')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `p14-${RUN}`
const PASSWORD = 'demo-pass-2026'
const DESKTOP = { width: 1440, height: 900 }
const RECEIVER = { name: '李女士', phone: '13800001111', address: '上海市浦东新区世纪大道100号' }
const ENTRY = 'order-add-product'

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

async function login(username) {
  const tokens = await json(`${API}/api/v1/auth/login`, {
    method: 'POST',
    body: { tenant_code: TENANT, username, password: PASSWORD },
  })
  return tokens.access_token
}

const product = (token, body) => json(`${API}/api/v1/products`, { method: 'POST', token, body })

/** 等 worker 判断完所有提交（学习记录里没有"还没判断"的）。 */
async function settle(token) {
  for (let i = 0; i < 60; i += 1) {
    const counts = await json(`${API}/api/v1/form-kb/summary`, { token })
    if (counts.pending === 0) return counts
    await sleep(500)
  }
  throw new Error('学习记录 30 秒还没判断完（worker 在运行吗？）')
}

async function order(token, customerId, lines, confirm = false) {
  const created = await json(`${API}/api/v1/orders`, {
    method: 'POST',
    token,
    body: {
      customer_id: customerId,
      items: lines.map(([product_id, quantity, entry]) => ({ product_id, quantity, ...(entry ? { entry } : {}) })),
      receiver: RECEIVER,
    },
  })
  if (confirm) {
    await json(`${API}/api/v1/orders/${created.id}/confirm`, {
      method: 'POST',
      token,
      body: { payment_method: 'cod', notify_customer: false },
    })
  }
  return created
}

async function entries(token, query = '') {
  const page = await json(`${API}/api/v1/form-kb/entries?limit=100${query}`, { token })
  return page.items
}

// 租户、员工（客服小美、工人老王、仓管小陈）、成品（两个规格的铝合金窗、防盗门、纱窗）、材料（门板、
// 锁具、合页、膨胀螺丝）、防盗门的配方（门板 2 块、锁具 1 套、合页 3 个）、客户。
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
      name: `表单知识验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  const admin = await login('admin')
  const staff = {}
  for (const [username, name, role] of [
    ['mei', '客服小美', 'agent'],
    ['wang', '工人老王', 'worker'],
    ['cang', '仓管小陈', 'keeper'],
  ]) {
    const created = await json(`${API}/api/v1/staff`, {
      method: 'POST',
      token: admin,
      body: { username, display_name: name, password: PASSWORD, role_codes: [role] },
    })
    staff[username] = created.id
  }
  await json(`${API}/api/v1/warehouse/settings`, {
    method: 'PUT',
    token: admin,
    body: { confirm_required: true, keeper_id: staff.cang },
  })
  const g = {}
  for (const [key, body] of [
    ['window', { code: 'WIN-01', name: '铝合金窗', unit: '樘', retail_price: '1800', spec: '1.2m×1.5m', category: '门窗' }],
    ['wide', { code: 'WIN-03', name: '铝合金窗', unit: '樘', retail_price: '2200', spec: '1.5m×1.8m', category: '门窗' }],
    ['door', { code: 'DOOR-01', name: '防盗门', unit: '樘', retail_price: '2600', spec: '甲级', category: '门窗' }],
    ['net', { code: 'NET-01', name: '纱窗', unit: '扇', retail_price: '120', spec: '隐形', category: '门窗' }],
  ]) {
    g[key] = await product(admin, body)
  }
  const m = {}
  for (const [key, code, name, unit] of [
    ['panel', 'DB-1', '门板', '块'],
    ['lockset', 'LK-1', '锁具', '套'],
    ['hinge', 'HG-1', '合页', '个'],
    ['anchor', 'MB-8', '膨胀螺丝', '个'],
  ]) {
    m[key] = await product(admin, { code, name, unit, kind: 'material', category: '五金' })
    await json(`${API}/api/v1/products/${m[key].id}/stock`, {
      method: 'POST',
      token: admin,
      body: { mode: 'set', quantity: 200, note: '期初盘点' },
    })
  }
  await json(`${API}/api/v1/products/${g.door.id}/materials`, {
    method: 'PUT',
    token: admin,
    body: {
      items: [
        { material_id: m.panel.id, quantity: 2 },
        { material_id: m.lockset.id, quantity: 1 },
        { material_id: m.hinge.id, quantity: 3 },
      ],
    },
  })
  const mei = await login('mei')
  const customer = await json(`${API}/api/v1/customers`, {
    method: 'POST',
    token: mei,
    body: { display_name: '李女士' },
  })
  return { admin, mei, wang: await login('wang'), cang: await login('cang'), staff, g, m, customerId: customer.id }
}

function watchErrors(page, label) {
  page.on('console', (msg) => {
    if (msg.type() === 'error' && !msg.text().startsWith('Failed to load resource')) {
      summary.consoleErrors.push(`${label}: ${msg.text()}`)
    }
  })
  page.on('pageerror', (e) => summary.consoleErrors.push(`${label} pageerror: ${e.message}`))
}

const pages = []

async function consoleLogin(browser, username) {
  const context = await browser.newContext({ viewport: DESKTOP, locale: 'zh-CN' })
  const page = await context.newPage()
  watchErrors(page, username)
  pages.push([username, page])
  await page.goto(`${CONSOLE}/login`)
  await page.fill('input[placeholder="例如 demo"]', TENANT)
  await page.fill('input[autocomplete="username"]', username)
  await page.fill('input[autocomplete="current-password"]', PASSWORD)
  await page.click('button:has-text("登录")')
  await page.locator('[data-testid="main-menu"]').waitFor()
  return page
}

async function shot(page, name, fullPage = false) {
  await page.waitForTimeout(600)
  await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage })
}

// el-select（可搜索）：输入关键字后点选。
async function pick(page, select, text) {
  await select.click()
  await page.keyboard.type(text)
  const option = page.locator('.el-select-dropdown__item:visible', { hasText: text }).first()
  await option.waitFor()
  await option.click()
}

/** 录入行的下拉：候选（名称、代码、标签、说明）和标题。 */
async function dropdown(scope) {
  const options = await scope.locator(`[data-testid="${ENTRY}-option"]`).evaluateAll((els) =>
    els.map((el) => ({
      name: el.getAttribute('data-name'),
      code: el.getAttribute('data-code'),
      match: el.getAttribute('data-match'),
      tag: el.querySelector('.tag')?.textContent?.trim() ?? null,
      note: el.querySelector('.note')?.textContent?.trim() ?? null,
    })),
  )
  const title = scope.locator(`[data-testid="${ENTRY}-title"]`)
  return { options, title: (await title.count()) ? (await title.innerText()).trim() : null }
}

/** 录入行输入后等这次输入的候选出来（下拉的 data-query 是这次的输入）。 */
async function suggest(scope, text) {
  await scope.locator(`input[data-testid="${ENTRY}-input"]`).fill(text)
  await scope.page().waitForFunction(
    ([sel, query]) => document.querySelector(sel)?.getAttribute('data-query') === query,
    [`[data-testid="${ENTRY}-options"]`, text],
  )
  return dropdown(scope)
}

async function newOrder(page) {
  await page.goto(`${CONSOLE}/orders`)
  await page.locator('[data-testid="new-order"]').click()
  const form = page.locator('[data-testid="order-form"]:visible')
  await form.waitFor()
  await pick(page, form.locator('[data-testid="order-customer"]'), '李女士')
  return form
}

async function saveOrder(page, form) {
  for (const [field, value] of Object.entries(RECEIVER)) {
    await form.locator(`input[data-testid="receiver-${field}"]`).fill(value)
  }
  await page.keyboard.press('Control+S')
  await form.waitFor({ state: 'hidden' })
  await page.locator('[data-testid="order-drawer"] [data-testid="order-edit"]').waitFor()
  await page.keyboard.press('Escape')
}

const lineNames = (form) => form.locator('[data-testid^="order-line-"] .item-sub').allInnerTexts()

// ---- 1. 以往的领料：学到常领的材料，和配方不一致的标待确认 ----

async function usageHistory(ctx) {
  const { admin, wang, cang, g, m } = ctx
  for (let i = 0; i < 3; i += 1) {
    const past = await order(admin, ctx.customerId, [[g.door.id, 1]], true)
    await json(`${API}/api/v1/production/orders/${past.id}/claim`, { method: 'POST', token: wang })
    const doc = await json(`${API}/api/v1/warehouse/documents`, {
      method: 'POST',
      token: wang,
      body: {
        kind: 'requisition',
        order_id: past.id,
        lines: [
          { product_id: m.panel.id, quantity: 2 },
          { product_id: m.lockset.id, quantity: 1 },
          { product_id: m.hinge.id, quantity: 4 },
          { product_id: m.anchor.id, quantity: 8 },
        ],
      },
    })
    await json(`${API}/api/v1/warehouse/documents/${doc.id}/confirm`, { method: 'POST', token: cang, body: {} })
  }
  await settle(admin)
  const usage = await entries(admin, '&kind=usage')
  const anchor = usage.find((e) => e.related.id === m.anchor.id)
  const hinge = usage.find((e) => e.related.id === m.hinge.id)
  check(
    '以往 3 张防盗门订单的领料单确认后：学到"膨胀螺丝 8 个/樘"（配方里没有、常领，生效）；合页实际 4 个、配方 3 个，标"待确认"（配方不变）；门板、锁具和配方一致，不单独记',
    usage.length === 2 &&
      anchor?.status === 'active' &&
      anchor.basis === 'extra' &&
      anchor.value === 8 &&
      hinge?.review === 'recipe' &&
      hinge.basis === 'deviation' &&
      hinge.value === 4 &&
      hinge.recipe === 3,
    usage.map((e) => [e.sentence, e.status, e.review, e.basis]),
  )
}

// ---- 2、3. 叫法和搭配：从下单里学到，再下单时用上 ----

async function aliasSection(browser, ctx) {
  const { mei, g } = ctx
  // 以往两张订单：一次输入"大窗"在相近的候选里选了第二个（1.5m×1.8m 的铝合金窗），都和纱窗一起开。
  await order(mei, ctx.customerId, [
    [g.wide.id, 1, { query: '大窗', via: 'suggest', match: 'similar', rank: 1 }],
    [g.net.id, 1],
  ])
  await order(mei, ctx.customerId, [
    [g.wide.id, 2],
    [g.net.id, 2],
  ])
  await settle(ctx.admin)

  const page = await consoleLogin(browser, 'mei')
  let form = await newOrder(page)
  const before = await suggest(form, '大窗')
  await shot(page, '1-before-learning')
  const spec = await suggest(form, '1.5*1.8')
  await form.locator(`input[data-testid="${ENTRY}-input"]`).press('Enter')
  await form.locator('[data-testid="order-line-0"]').waitFor()
  await suggest(form, '纱窗')
  await form.locator(`input[data-testid="${ENTRY}-input"]`).press('Enter')
  await form.locator('[data-testid="order-line-1"]').waitFor()
  const firstLines = await lineNames(form)
  await saveOrder(page, form)
  await settle(ctx.admin)
  const alias = (await entries(ctx.admin, '&kind=alias')).find((e) => e.text === '大窗')
  const companion = (await entries(ctx.admin, '&kind=companion')).find(
    (e) => e.product.id === g.wide.id && e.related.id === g.net.id,
  )
  check(
    '学到之前输入"大窗"没有"学到的"候选；换成规格 1.5*1.8 找到大窗提交后，"大窗"第二次选了 1.5m×1.8m 的铝合金窗，开始生效；纱窗 3 张里 3 张和它一起开，开始生效',
    before.options.every((o) => o.tag !== '学到的') &&
      spec.options[0]?.code === 'WIN-03' &&
      firstLines.join('|').includes('WIN-03') &&
      alias?.status === 'active' &&
      alias.product.id === g.wide.id &&
      alias.evidence === 2 &&
      companion?.status === 'active',
    { before, spec: spec.options[0], firstLines, alias, companion },
  )

  form = await newOrder(page)
  const learned = await suggest(form, '大窗')
  await shot(page, '2-learned-alias')
  await form.locator(`input[data-testid="${ENTRY}-input"]`).press('Enter')
  await form.locator('[data-testid="order-line-0"]').waitFor()
  const added = await form.locator('[data-testid="order-line-0"] .item-sub').innerText()
  check(
    '再下单输入"大窗"：1.5m×1.8m 的铝合金窗排在最前面、标"学到的"，回车直接加入',
    learned.options[0]?.code === 'WIN-03' && learned.options[0].tag === '学到的' && added.includes('WIN-03'),
    { learned, added },
  )

  // 录入行空着时点一下：先列出常和单上商品一起开的。
  await form.locator(`input[data-testid="${ENTRY}-input"]`).click()
  await page.waitForFunction(
    (sel) => document.querySelector(sel)?.textContent?.trim() === '常一起开的 · 最近用过的',
    `[data-testid="${ENTRY}-title"]`,
  )
  const together = await dropdown(form)
  await shot(page, '3-companion')
  await form.locator(`input[data-testid="${ENTRY}-input"]`).press('Enter')
  await form.locator('[data-testid="order-line-1"]').waitFor()
  const lines = await lineNames(form)
  check(
    '录入行空着时点一下：标题"常一起开的 · 最近用过的"，纱窗排在最前面、标"常一起开"、写明"和 铝合金窗 一起开过 3/3 次"，回车加入',
    together.options[0]?.code === 'NET-01' &&
      together.options[0].tag === '常一起开' &&
      together.options[0].note === '和 铝合金窗 一起开过 3/3 次' &&
      lines.join('|').includes('NET-01'),
    { together, lines },
  )
  await saveOrder(page, form)
  await settle(ctx.admin)
  const used = (await entries(ctx.admin, '&kind=alias')).find((e) => e.text === '大窗')
  const usedCompanion = (await entries(ctx.admin, '&kind=companion')).find(
    (e) => e.product.id === g.wide.id && e.related.id === g.net.id,
  )
  check(
    '提交后记下用到了：叫法"大窗"、搭配各用到 1 次',
    used?.hits === 1 && usedCompanion?.hits === 1,
    { alias: used?.hits, companion: usedCompanion?.hits },
  )
  ctx.lastOrder = (await json(`${API}/api/v1/orders?limit=1`, { token: mei })).items[0]
  ctx.meiPage = page
}

// ---- 4. 工人领取：领料单里有学到的材料 ----

async function requisitionSection(browser, ctx) {
  const doorOrder = await order(ctx.admin, ctx.customerId, [[ctx.g.door.id, 2]], true)
  ctx.doorOrder = doorOrder
  const page = await consoleLogin(browser, 'wang')
  await page.locator('[data-testid="production-view-pool"]').click()
  const card = page.locator(`[data-testid="production-order"][data-order-no="${doorOrder.no}"]`)
  await card.waitFor()
  await card.locator('[data-testid="production-claim"]').click()
  const editor = page.locator('[data-testid="document-editor"]:visible')
  await editor.locator('[data-testid="document-line-basis"]').first().waitFor()
  const rows = await editor.locator('[data-testid="document-row"]').evaluateAll((els) =>
    els.map((row) => ({
      name: row.getAttribute('data-name'),
      quantity: row.querySelector('[data-testid="document-line-quantity"] input')?.value,
      basis: [...row.querySelectorAll('[data-testid="document-line-basis"]')].map((el) => el.textContent.trim()),
      learned: Boolean(row.querySelector('[data-testid="document-line-learned"]')),
    })),
  )
  await shot(page, '4-learned-material')
  const anchor = rows.find((r) => r.name === '膨胀螺丝')
  const hinge = rows.find((r) => r.name === '合页')
  check(
    '工人领取 2 樘防盗门：自动填好的领料单里有膨胀螺丝 16 个，标"学到的"，写明"防盗门 甲级 8 个/樘 × 2 樘（学到的：以往 3 个订单常领）"；合页仍按配方 6 个',
    anchor?.quantity === '16' &&
      anchor.learned &&
      anchor.basis.join('|') === '防盗门 甲级 8 个/樘 × 2 樘（学到的：以往 3 个订单常领）' &&
      hinge?.quantity === '6' &&
      !hinge.learned,
    rows,
  )
  await editor.locator('[data-testid="doc-close"]').click()
  await editor.waitFor({ state: 'hidden' })
}

// ---- 5～8. 知识库的"表单知识" ----

async function rows(page) {
  await page.locator('[data-testid="formkb-table"] .el-loading-mask').waitFor({ state: 'hidden' }).catch(() => null)
  return page
    .locator('[data-testid="formkb-table"] .el-table__body-wrapper .el-table__row')
    .evaluateAll((els) => els.map((el) => el.innerText.replace(/\s+/g, ' ').trim()))
}

async function counts(page) {
  const text = async (name) => (await page.locator(`[data-testid="formkb-count-${name}"] b`).innerText()).trim()
  return {
    active: await text('active'),
    observing: await text('observing'),
    review: await text('review'),
    pending: await text('pending'),
  }
}

/** 等表单知识的列表满足条件（列表加载是异步的）。 */
async function waitRows(page, predicate) {
  let listed = []
  for (let i = 0; i < 40; i += 1) {
    listed = await rows(page)
    if (predicate(listed)) return listed
    await page.waitForTimeout(250)
  }
  throw new Error(`表单知识的列表不符合预期：${JSON.stringify(listed)}`)
}

async function openRow(page, text) {
  await page.locator('[data-testid="formkb-table"] .el-table__row', { hasText: text }).first().click()
  const drawer = page.locator('[data-testid="formkb-drawer"]')
  await drawer.locator('[data-testid="formkb-drawer-sentence"]').waitFor()
  return drawer
}

async function closeDrawer(page) {
  await page.keyboard.press('Escape')
  await page.locator('[data-testid="formkb-drawer"]').waitFor({ state: 'hidden' })
}

async function knowledgeSection(browser, ctx) {
  const page = await consoleLogin(browser, 'admin')
  await page.goto(`${CONSOLE}/knowledge?tab=form`)
  await page.locator('[data-testid="formkb-panel"]').waitFor()
  await waitRows(page, (list) => list.length > 0)
  const numbers = await counts(page)
  const listed = await rows(page)
  await shot(page, '5-form-knowledge', true)
  check(
    '知识库的"表单知识"：生效、观察中、待确认、还没判断的数量；知识写成一句话（叫法、搭配、用量），待确认的排在前面',
    numbers.review === '1' &&
      numbers.pending === '0' &&
      Number(numbers.active) >= 4 &&
      listed[0]?.includes('防盗门 甲级 每樘用 合页 4 个（配方 3 个）') &&
      listed[0].includes('待确认') &&
      ['输入「大窗」→ 铝合金窗 1.5m×1.8m', '开 铝合金窗 1.5m×1.8m 时常一起开 纱窗 隐形', '防盗门 甲级 每樘用 膨胀螺丝 8 个'].every(
        (t) => listed.some((row) => row.includes(t)),
      ),
    { numbers, listed },
  )

  // 只看待确认的：打开合页的用量，更新配方。
  await page.locator('[data-testid="formkb-count-review"]').click()
  await waitRows(page, (list) => list.length === 1)
  const drawer = await openRow(page, '合页')
  const review = (await drawer.locator('[data-testid="formkb-drawer-review"]').innerText()).replace(/\s+/g, ' ')
  const evidence = await drawer.locator('[data-testid="formkb-drawer-evidence"] .el-table__row').count()
  await shot(page, '6-review-recipe')
  await drawer.locator('[data-testid="formkb-drawer-recipe"]').click()
  await page.locator('.el-message-box').getByRole('button', { name: '写进配方' }).click()
  await drawer.locator('[data-testid="formkb-drawer-review"]').waitFor({ state: 'hidden' })
  const log = (await drawer.locator('[data-testid="formkb-drawer-log"]').innerText()).replace(/\s+/g, ' ')
  const recipe = await json(`${API}/api/v1/products/${ctx.g.door.id}/materials`, { token: ctx.admin })
  const draft = await json(`${API}/api/v1/warehouse/drafts?kind=requisition&order_id=${ctx.doorOrder.id}`, {
    token: ctx.wang,
  })
  const hinge = draft.lines.find((l) => l.product_id === ctx.m.hinge.id)
  check(
    '点"待确认"只看待确认的；打开合页的用量：写明"最近 3 张订单每樘实际约 4 个，配方是 3 个"，列出依据的 3 张领料单；"更新配方"后配方改为 4 个，变化记录里有"写进配方"，之后的领料单按新配方（2 樘 8 个）',
    review.includes('实际用量和配方不一致') &&
      review.includes('最近 3 张订单每樘实际约 4 个，配方是 3 个') &&
      evidence === 3 &&
      log.includes('写进配方') &&
      recipe.items.find((i) => i.material_id === ctx.m.hinge.id)?.quantity === 4 &&
      hinge?.quantity === 8,
    { review, evidence, log, recipe: recipe.items, hinge },
  )
  await closeDrawer(page)

  // 学习记录：每次提交的判断结果。
  await page.locator('[data-testid="formkb-view"]').getByText('学习记录').click()
  const record = page.locator('[data-testid="formkb-records"] .el-table__row', { hasText: ctx.lastOrder.no })
  await record.waitFor()
  const recordText = (await record.innerText()).replace(/\s+/g, ' ')
  const all = await page
    .locator('[data-testid="formkb-records"] .el-table__row')
    .evaluateAll((els) => els.map((el) => el.innerText.replace(/\s+/g, ' ')))
  await shot(page, '7-learning-records', true)
  check(
    '学习记录：每次提交的订单、领料单和判断结果——最近一张订单"用到"了叫法"大窗"和搭配；领料单确认时"学到"膨胀螺丝、合页"待确认"',
    recordText.includes('下单') &&
      recordText.includes('用到') &&
      recordText.includes('输入「大窗」→ 铝合金窗 1.5m×1.8m') &&
      all.some((t) => t.includes('确认') && t.includes('待确认') && t.includes('合页')) &&
      all.some((t) => t.includes('生效') && t.includes('膨胀螺丝')),
    { recordText, all: all.slice(0, 12) },
  )

  // 手工新增"小窗"。
  await page.locator('[data-testid="formkb-view"]').getByText('知识', { exact: true }).click()
  await page.locator('[data-testid="formkb-create"]').click()
  const dialog = page.locator('[data-testid="formkb-dialog"]')
  await dialog.locator('[data-testid="formkb-dialog-text"]').fill('小窗')
  await pick(page, dialog.locator('[data-testid="formkb-dialog-product"]'), 'WIN-01')
  await shot(page, '8-add-alias')
  await dialog.locator('[data-testid="formkb-dialog-save"]').click()
  await dialog.waitFor({ state: 'hidden' })
  const created = page.locator('[data-testid="formkb-drawer"]')
  await created.locator('[data-testid="formkb-drawer-sentence"]').waitFor()
  const sentence = (await created.locator('[data-testid="formkb-drawer-sentence"]').innerText()).trim()
  const tags = await created.locator('.tags').innerText()
  const found = await json(`${API}/api/v1/products/suggest?q=${encodeURIComponent('小窗')}`, { token: ctx.mei })
  check(
    '手工新增叫法"小窗"→ 1.2m×1.5m 的铝合金窗：立即生效、固定；坐席下单时输入"小窗"排在最前面（学到的）',
    sentence === '输入「小窗」→ 铝合金窗 1.2m×1.5m' &&
      ['生效', '手工添加', '固定'].every((t) => tags.includes(t)) &&
      found.items[0]?.product.code === 'WIN-01' &&
      found.items[0].field === 'learned',
    { sentence, tags, found: found.items.map((i) => [i.product.code, i.field]) },
  )
  await closeDrawer(page)

  // 关掉自动生效：之后学到的标待确认。
  await page.locator('[data-testid="formkb-settings"]').click()
  await page.locator('[data-testid="formkb-auto-activate"]').click()
  await page.locator('[data-testid="formkb-settings-save"]').click()
  await page.locator('[data-testid="formkb-settings-form"]').waitFor({ state: 'hidden' })
  const settings = await json(`${API}/api/v1/form-kb/settings`, { token: ctx.admin })
  for (let i = 0; i < 2; i += 1) {
    await order(ctx.mei, ctx.customerId, [[ctx.g.door.id, 1, { query: '小门', via: 'suggest', match: 'similar', rank: 2 }]])
  }
  await settle(ctx.admin)
  const waiting = await json(`${API}/api/v1/products/suggest?q=${encodeURIComponent('小门')}`, { token: ctx.mei })
  await page.locator('[data-testid="formkb-count-review"]').click()
  await waitRows(page, (list) => list.length === 1 && list[0].includes('小门'))
  const pending = await openRow(page, '小门')
  const note = (await pending.locator('[data-testid="formkb-drawer-review"]').innerText()).replace(/\s+/g, ' ')
  await pending.locator('[data-testid="formkb-drawer-activate"]').click()
  await page.waitForFunction(
    () => document.querySelector('[data-testid="formkb-drawer-status"]')?.textContent?.trim() === '生效',
  )
  const activated = await json(`${API}/api/v1/products/suggest?q=${encodeURIComponent('小门')}`, { token: ctx.mei })
  check(
    '设置里关掉"自动生效"后：两次把"小门"选成防盗门，标"待确认"（达到生效条件，确认后用上），确认前联想里不用；"确认生效"后输入"小门"防盗门排在最前面',
    settings.auto_activate === false &&
      note.includes('达到生效条件，确认后用上') &&
      waiting.items.every((i) => i.field !== 'learned') &&
      activated.items[0]?.product.code === 'DOOR-01' &&
      activated.items[0].field === 'learned',
    { settings, note, waiting: waiting.items.map((i) => i.field), activated: activated.items.map((i) => [i.product.code, i.field]) },
  )
  await closeDrawer(page)
}

// ---- 9. 坐席只能看 ----

async function agentSection(ctx) {
  const page = ctx.meiPage
  await page.goto(`${CONSOLE}/knowledge?tab=form`)
  await page.locator('[data-testid="formkb-panel"]').waitFor()
  await waitRows(page, (list) => list.length > 0)
  const buttons = await page.locator('[data-testid="formkb-create"], [data-testid="formkb-settings"]').count()
  const drawer = await openRow(page, '大窗')
  const actions = await drawer.locator('[data-testid^="formkb-drawer-"]:is(button)').count()
  await shot(page, '9-agent-read-only')
  check('坐席能看表单知识，没有"新增知识""设置"，打开知识也没有处理按钮', buttons === 0 && actions === 0, {
    buttons,
    actions,
  })
}

async function run(browser) {
  const ctx = await prepareTenant()
  await usageHistory(ctx)
  await aliasSection(browser, ctx)
  await requisitionSection(browser, ctx)
  await knowledgeSection(browser, ctx)
  await agentSection(ctx)
  check('没有前端脚本错误', summary.consoleErrors.length === 0, summary.consoleErrors)
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
