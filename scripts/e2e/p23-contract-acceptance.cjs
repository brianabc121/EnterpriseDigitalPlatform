// P23 验收：项目合同管理（设计文档 §34）——多层级分类、上传模板、AI 按需求和知识库起草、填写项、定稿、
// 导出 Word、登记签署、快到期提醒、另存为模板、按模板新建、作废和查看范围，在浏览器里走通。
//
// 1. 准备：规章制度《售后服务制度》（质保 12 个月）、两个商品、客户"华东分公司"和他的订单；坐席小艾。
// 2. 管理员打开"合同"：添加常用分类，在"销售合同"下新建"定制加工"；合同设置里填我方信息。
// 3. 模板：上传 Word 模板，空白识别成 3 个填写项；给"交货地点"填默认值。
// 4. AI 生成合同：写需求、选客户和订单——AI 引用规章制度写质保条款，标的清单、金额、我方信息由系统填写；
//    插入一个"交货地点"填写项后定稿被拦下，填好再定稿；导出 Word；登记签署（结束日期 20 天后、上传扫描件），
//    列表"快到期"里有它；AI 唤醒提醒负责人，提醒的链接打开这份合同；修改历史；另存为模板。
// 5. 订单详情的"生成合同"带上订单打开 AI 生成合同；按模板新建合同（默认值填上），作废。
// 6. 小艾只看到自己的合同，没有合同设置和分类管理；她新建的合同负责人是她。
//
// 前置：与 p22-wake-acceptance.cjs 相同（后端、实时消费进程、调度进程接到模拟大模型 :8900）。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<密码> node scripts/e2e/p23-contract-acceptance.cjs
const { chromium } = require('playwright')
const { execFile } = require('child_process')
const { promisify } = require('util')
const fs = require('fs')
const path = require('path')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p23')
const BACKEND_DIR = env('BACKEND_DIR', path.resolve(__dirname, '../../backend'))
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `contract-${RUN}`
const PASSWORD = 'demo-pass-2026'
const POLICY = [
  '# 售后服务制度',
  '## 质保',
  '产品质保期 12 个月，质保期内非人为损坏免费维修。',
  '## 违约',
  '逾期交货每天按合同金额的千分之五支付违约金。',
].join('\n')
const REQUIREMENT = '给华东分公司定制门锁和门铃，30 天交货，预付 30%，验收后付清。'
const PARTY = '上海某某智能科技有限公司'

const summary = { tenant: TENANT, checks: [], consoleErrors: [] }
const check = (name, ok, detail) =>
  summary.checks.push(
    `${ok ? 'PASS' : 'FAIL'} ${name}${ok || detail === undefined ? '' : ` -> ${JSON.stringify(detail)}`}`,
  )
const state = {}

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

const run = promisify(execFile)

/** 后端命令行（见 p22-wake-acceptance.cjs）。 */
async function cli(...args) {
  const { stdout } = await run('uv', ['run', 'python', '-m', 'app.cli', ...args], {
    cwd: BACKEND_DIR,
    env: process.env,
    encoding: 'utf-8',
    maxBuffer: 16 * 1024 * 1024,
  })
  const lines = stdout.trim().split('\n')
  return JSON.parse(lines[lines.length - 1])
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function until(label, probe, timeoutMs = 90_000) {
  const deadline = Date.now() + timeoutMs
  let last
  while (Date.now() < deadline) {
    last = await probe()
    if (last) return last
    await sleep(1000)
  }
  throw new Error(`timed out waiting for ${label}`)
}

/** 企业时区（上海）的日期，YYYY-MM-DD。 */
function shanghaiDate(offsetDays = 0) {
  const day = new Date(Date.now() + offsetDays * 86_400_000)
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(day)
}

/** 一份带空白的 Word 模板（用后端生成 .docx 的代码，和用户从 Word 另存的一样能解析）。 */
async function wordTemplate(file) {
  const code = [
    'import sys',
    'from app.core import docx',
    'items = [',
    '    docx.Item("title", [docx.Span("工服定制合同")]),',
    '    docx.Item("paragraph", [docx.Span("甲方：________  乙方：{{我方名称}}")]),',
    '    docx.Item("heading", [docx.Span("一、标的")]),',
    '    docx.Item("paragraph", [docx.Span("{{标的清单}}")]),',
    '    docx.Item("heading", [docx.Span("二、交货")]),',
    '    docx.Item("paragraph", [docx.Span("交货日期：____年__月__日，交货地点：【    】")]),',
    '    docx.Item("paragraph", [docx.Span("合同金额：{{合同金额}} 元（{{合同金额大写}}）")]),',
    ']',
    'open(sys.argv[1], "wb").write(docx.build(items, title="工服定制合同"))',
  ].join('\n')
  await run('uv', ['run', 'python', '-c', code, file], { cwd: BACKEND_DIR, env: process.env })
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
      name: `合同验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  const admin = await login('admin')
  state.admin = admin
  await json(`${API}/api/v1/staff`, {
    method: 'POST',
    token: admin,
    body: { username: 'alice', display_name: '小艾', password: PASSWORD, role_codes: ['agent'] },
  })

  // 规章制度：AI 起草的质保条款以它为准。
  await json(`${API}/api/v1/kb/items`, {
    method: 'POST',
    token: admin,
    body: { kind: 'doc', title: '售后服务制度', content: POLICY, policy: true, publish: true },
  })
  await until('the policy to be searchable', async () => {
    const found = await json(`${API}/api/v1/kb/search?q=${encodeURIComponent('质保')}`, { token: admin })
    return (found.items ?? found.hits ?? found).length > 0
  })

  const lock = await json(`${API}/api/v1/products`, {
    method: 'POST',
    token: admin,
    body: { code: 'LOCK-X1', name: '智能门锁 X1', retail_price: '1299', cost_price: '800' },
  })
  const bell = await json(`${API}/api/v1/products`, {
    method: 'POST',
    token: admin,
    body: { code: 'BELL-D1', name: '可视门铃 D1', retail_price: '199', cost_price: '90' },
  })
  const customer = await json(`${API}/api/v1/customers`, {
    method: 'POST',
    token: admin,
    body: { display_name: '张经理', company: '华东分公司', phone: '13900001234' },
  })
  const order = await json(`${API}/api/v1/orders`, {
    method: 'POST',
    token: admin,
    body: {
      customer_id: customer.id,
      items: [
        { product_id: lock.id, quantity: 2 },
        { product_id: bell.id, quantity: 3 },
      ],
      receiver: { name: '张经理', phone: '13900001234', address: '上海市松江区九亭镇 88 号' },
    },
  })
  state.customerId = customer.id
  state.orderId = order.id
  state.orderNo = order.no
  state.orderTotal = Number(order.total)
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
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN', acceptDownloads: true })
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

async function success(page, text, timeout = 15_000) {
  await page.locator('.el-message--success', { hasText: text }).last().waitFor({ timeout })
}

/** 弹出的输入框（ElMessageBox.prompt）：填写后点按钮。 */
async function prompt(page, value, button) {
  const box = page.locator('.el-message-box:visible')
  await box.waitFor()
  await box.locator('input').fill(value)
  await box.locator('button', { hasText: button }).click()
  await box.waitFor({ state: 'hidden' }).catch(() => null)
}

// el-select（可搜索）：输入关键字后点选。
async function pick(page, select, text) {
  await select.click()
  await page.keyboard.type(text)
  const option = page.locator('.el-select-dropdown__item:visible', { hasText: text }).first()
  await option.waitFor()
  await option.click()
}

// 普通下拉：点开后点选。
async function choose(page, select, text) {
  const option = page.locator('.el-select-dropdown__item:visible', { hasText: text }).first()
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await select.click()
    if (await option.waitFor({ timeout: 5000 }).then(() => true, () => false)) break
  }
  await option.click()
}

async function tab(page, label) {
  await page.locator('[data-testid="contract-tabs"] .el-tabs__item', { hasText: label }).first().click()
}

async function closeDrawer(page) {
  await page.locator('.el-drawer__close-btn:visible').last().click()
}

async function fieldValue(page, name) {
  const field = page.locator(`[data-testid="contract-field-${name}"]`)
  return field.locator('input, textarea').first().inputValue()
}

async function categories(page) {
  await page.goto(`${CONSOLE}/contracts`)
  await page.waitForSelector('[data-testid="contract-category-tree"]')
  await page.click('[data-testid="contract-category-defaults"]')
  await success(page, '已添加常用分类')
  await page.waitForSelector('[data-testid="contract-category-销售合同"]')
  const tree = await page.locator('[data-testid="contract-category-tree"]').innerText()
  check(
    'common categories added',
    ['销售合同', '采购合同', '服务合同', '租赁合同', '其他'].every((name) => tree.includes(name)),
    tree,
  )
  // 在"销售合同"下新建"定制加工"。
  await page.locator('[data-testid="contract-category-销售合同"]').hover()
  await page.click('[data-testid="contract-category-more-销售合同"]')
  await page.locator('.el-dropdown-menu__item:visible', { hasText: '新建下级分类' }).click()
  await prompt(page, '定制加工', '确定')
  await success(page, '已新建')
  await page.waitForSelector('[data-testid="contract-category-定制加工"]')
  const listed = await json(`${API}/api/v1/contracts/categories`, { token: state.admin })
  const sales = listed.items.find((c) => c.name === '销售合同')
  const custom = listed.items.find((c) => c.name === '定制加工')
  check('sub-category created under 销售合同', custom && custom.parent_id === sales.id, listed.items)
  state.categoryId = custom.id
  await page.click('[data-testid="contract-category-定制加工"]')

  // 合同设置：我方信息（填写 {{我方名称}} 等内置填写项）。
  await page.click('[data-testid="contract-settings-open"]')
  await page.locator('input[data-testid="contract-party-name"]').fill(PARTY)
  await page.locator('input[data-testid="contract-party-address"]').fill('上海市松江区新桥镇 66 号')
  await page.locator('input[data-testid="contract-party-representative"]').fill('王总')
  await page.click('[data-testid="contract-settings-save"]')
  await success(page, '已保存')
  const saved = await json(`${API}/api/v1/contracts/settings`, { token: state.admin })
  check(
    'party info saved',
    saved.settings.party.name === PARTY && saved.settings.party.representative === '王总',
    saved.settings,
  )
}

async function uploadTemplate(page) {
  await tab(page, '模板')
  await page.click('[data-testid="template-upload"]')
  // 文件名用中文：内容另写一个英文名的临时文件，再按名称和内容交给页面（Playwright 按中文路径上传会静默失败）。
  const file = path.resolve(SHOTS, 'template.docx')
  await wordTemplate(file)
  await page.setInputFiles('[data-testid="template-upload-input"]', {
    name: '工服定制合同.docx',
    mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    buffer: fs.readFileSync(file),
  })
  const title = await page.locator('input[data-testid="template-upload-title"]').inputValue()
  check('template name taken from the file name', title === '工服定制合同', title)
  await page.click('[data-testid="template-upload-submit"]')
  await success(page, '把 3 处空白识别成了填写项')
  await page.waitForSelector('[data-testid="template-fields"]')
  const fields = await page.locator('[data-testid="template-fields"]').innerText()
  check(
    'blanks became fields; built-in fields marked',
    ['甲方', '交货日期', '交货地点', '标的清单', '系统填写'].every((name) => fields.includes(name)),
    fields,
  )
  await page.locator('input[data-testid="template-default-交货地点"]').fill('上海仓')
  await page.click('[data-testid="template-save"]')
  await success(page, '已保存')
  await page.screenshot({ path: `${SHOTS}/p23-01-template.png`, fullPage: true })
  await closeDrawer(page)
  await page.waitForSelector('[data-testid="templates-table"] .el-table__row')
  const row = await page.locator('[data-testid="templates-table"] .el-table__row').first().innerText()
  check(
    'template listed in the selected category with its original file',
    row.includes('工服定制合同') && row.includes('定制加工') && row.includes('工服定制合同.docx'),
    row,
  )
  const templates = await json(`${API}/api/v1/contracts/templates`, { token: state.admin })
  state.templateId = templates.items.find((t) => t.name === '工服定制合同').id
}

async function aiDraft(page) {
  await tab(page, '合同')
  await page.click('[data-testid="contract-ai-generate"]')
  await page.locator('textarea[data-testid="contract-requirement"]').fill(REQUIREMENT)
  await pick(page, page.locator('[data-testid="contract-customer-select"]'), '华东')
  await choose(page, page.locator('[data-testid="contract-order-select"]'), state.orderNo)
  await page.click('[data-testid="contract-generate-submit"]')
  await success(page, 'AI 已起草合同', 60_000)
  await page.locator('[data-testid="contract-no"]').waitFor()
  state.contractNo = (await page.locator('[data-testid="contract-no"]').innerText()).trim()
  const contracts = await json(`${API}/api/v1/contracts`, { token: state.admin })
  state.contractId = contracts.items.find((c) => c.no === state.contractNo).id

  const titleValue = await page.locator('input[data-testid="contract-title"]').inputValue()
  const status = await page.locator('[data-testid="contract-status"]').innerText()
  check('AI draft opened in the editor', titleValue === '定制加工合同' && status.includes('草稿'), {
    titleValue,
    status,
  })
  const values = {
    customer: await fieldValue(page, '客户名称'),
    party: await fieldValue(page, '我方名称'),
    items: await fieldValue(page, '标的清单'),
    amount: await fieldValue(page, '合同金额'),
    delivery: await fieldValue(page, '交货期限'),
    phone: await fieldValue(page, '客户电话'),
  }
  check(
    'built-in fields filled by the system, delivery time from the requirement',
    values.customer === '华东分公司' &&
      values.party === PARTY &&
      values.items.includes('智能门锁 X1') &&
      values.items.includes('合计') &&
      Number(values.amount.replace(/,/g, '')) === state.orderTotal &&
      values.delivery === '合同签订后 30 天内' &&
      values.phone === '13900001234',
    values,
  )
  const ai = await page.locator('[data-testid="contract-ai"]').innerText()
  check(
    'AI basis: the policy is cited, notes to confirm, model and tokens',
    ai.includes('售后服务制度') && ai.includes('已引用') && ai.includes('请核对交货期限和付款方式') && ai.includes('fake-chat'),
    ai,
  )
  const preview = await page.locator('[data-testid="contract-preview"]').innerText()
  check(
    'warranty clause follows the policy; order lines laid out as a table',
    preview.includes('产品质保期 12 个月') && preview.includes('可视门铃 D1') && preview.includes(PARTY),
    preview.slice(0, 600),
  )
  const missing = await page.locator('[data-testid="contract-fields"] .block-head').innerText()
  check('nothing left to fill', missing.includes('都已填写'), missing)
  const category = await json(`${API}/api/v1/contracts/${state.contractId}`, { token: state.admin })
  check('contract filed under the selected category', category.category_id === state.categoryId, category.category_path)
  await page.screenshot({ path: `${SHOTS}/p23-02-ai-draft.png`, fullPage: true })

  // 正文里加一行"交货地点："并插入填写项：定稿被拦下，填好再定稿。
  const body = page.locator('textarea[data-testid="contract-body"]')
  await body.click()
  await page.keyboard.press('Control+End')
  await page.keyboard.type('\n交货地点：')
  await page.click('[data-testid="contract-insert-field"]')
  await page.locator('.el-dropdown-menu__item:visible', { hasText: '其他填写项' }).click()
  await prompt(page, '交货地点', '插入')
  await page.locator('[data-testid="contract-field-交货地点"].missing').waitFor()
  const count = await page.locator('[data-testid="contract-missing-count"]').innerText()
  check('inserted field shown as missing', count.includes('1 项待填写'), count)
  await page.click('[data-testid="contract-finalize"]')
  await page.locator('.el-message--warning', { hasText: '还有没填的填写项：交货地点' }).waitFor()
  check('cannot finalize with a missing field', true)
  await page.locator('[data-testid="contract-field-交货地点"] input').fill('上海市松江区仓库')
  await page.click('[data-testid="contract-finalize"]')
  await success(page, '已定稿')
  await page.locator('[data-testid="contract-status"]', { hasText: '已定稿' }).waitFor()
  const locked = (await page.locator('textarea[data-testid="contract-body"]').count()) === 0
  const finalPreview = await page.locator('[data-testid="contract-preview"]').innerText()
  check('finalized: body locked', locked && finalPreview.includes('交货地点：上海市松江区仓库'), { locked })

  // 导出 Word。
  const [download] = await Promise.all([page.waitForEvent('download'), page.click('[data-testid="contract-export"]')])
  const bytes = fs.readFileSync(await download.path())
  check(
    'exported a Word file',
    download.suggestedFilename().startsWith(state.contractNo) &&
      download.suggestedFilename().endsWith('.docx') &&
      bytes.subarray(0, 2).toString() === 'PK',
    download.suggestedFilename(),
  )

  // 登记签署：结束日期 20 天后，上传扫描件。
  await page.click('[data-testid="contract-sign"]')
  const dialog = page.locator('.el-dialog:visible', { hasText: '登记签署' })
  await dialog.waitFor()
  const end = dialog.locator('.el-form-item', { hasText: '结束日期' }).locator('input')
  await end.fill(shanghaiDate(20))
  await end.press('Enter')
  await page.setInputFiles('[data-testid="contract-scan-input"]', {
    name: '签字版.pdf',
    mimeType: 'application/pdf',
    buffer: Buffer.from('%PDF-1.4\n% signed copy\n%%EOF\n'),
  })
  await page.locator('[data-testid="contract-scan-name"]', { hasText: '签字版.pdf' }).waitFor()
  await page.click('[data-testid="contract-sign-submit"]')
  await success(page, '已登记签署')
  await dialog.waitFor({ state: 'hidden' })
  await page.locator('[data-testid="contract-status"]', { hasText: '已签署' }).waitFor()
  const expiry = await page.locator('[data-testid="contract-expiry"]').innerText()
  const scanLink = await page.locator('[data-testid="contract-scan-open"]').innerText()
  check('signed with a scan, shown as expiring', expiry.includes('快到期') && scanLink.includes('签字版.pdf'), {
    expiry,
    scanLink,
  })
  await page.screenshot({ path: `${SHOTS}/p23-03-signed.png`, fullPage: true })

  // 修改历史：起草、修改、定稿、签署。
  await page.click('[data-testid="contract-history"]')
  const history = page.locator('[data-testid="history-drawer"]')
  await history.locator('[data-testid="history-version"]').first().waitFor()
  const versions = await history.locator('[data-testid="history-version"]').count()
  check('history keeps a version per step', versions >= 4, versions)
  await closeDrawer(page)
  await history.waitFor({ state: 'hidden' }).catch(() => null)

  // 另存为模板。
  await page.click('[data-testid="contract-save-template"]')
  const name = await page.locator('input[data-testid="contract-template-name"]').inputValue()
  await page.click('[data-testid="contract-template-submit"]')
  await success(page, '已存为模板')
  const templates = await json(`${API}/api/v1/contracts/templates?q=${encodeURIComponent(name)}`, {
    token: state.admin,
  })
  const saved = templates.items.length
    ? await json(`${API}/api/v1/contracts/templates/${templates.items[0].id}`, { token: state.admin })
    : null
  check(
    'saved as a template with the fields kept',
    saved && name === '定制加工合同模板' && saved.body.includes('{{交货地点}}') && saved.body.includes('{{客户名称}}'),
    saved && saved.body.slice(0, 200),
  )
  await closeDrawer(page)
  await page.locator('[data-testid="contract-no"]').waitFor({ state: 'hidden' })

  // 列表：快到期。
  await page.click('[data-testid="contract-view-expiring"]')
  await page.waitForFunction(() => {
    const rows = document.querySelectorAll('[data-testid="contracts-table"] .el-table__row')
    return rows.length === 1 && rows[0].textContent.includes('快到期')
  })
  const label = await page.locator('[data-testid="contract-view-expiring"]').innerText()
  const row = await page.locator('[data-testid="contracts-table"] .el-table__row').first().innerText()
  check(
    'expiring view lists the contract',
    label.includes('快到期 1') && row.includes(state.contractNo) && row.includes('华东分公司'),
    { label, row },
  )
  await page.screenshot({ path: `${SHOTS}/p23-04-expiring.png`, fullPage: true })
}

async function wakeReminder(page) {
  // 新企业开通后调度进程登记的定时唤醒先执行完，再按每日巡检唤醒一次（新增规章制度排了一次 10 分钟后的
  // 知识库整理，不用等它）。
  await until(
    'the scheduled wake-ups of the new tenant',
    async () => {
      const runs = await json(`${API}/api/v1/wake/runs`, { token: state.admin })
      const scheduled = runs.items.filter((r) => r.trigger === 'schedule')
      return scheduled.length > 0 && scheduled.every((r) => !['queued', 'running'].includes(r.status))
    },
    180_000,
  )
  await cli('wake-run', TENANT, '--kind', 'daily', '--force')
  const findings = await json(`${API}/api/v1/wake/findings?view=all&status=open&limit=50`, { token: state.admin })
  const finding = findings.items.find((f) => f.check_code === 'contract_expiring')
  check(
    'AI wake-up reminds the owner of the expiring contract',
    finding && finding.title.includes(state.contractNo) && finding.title.includes('快到期'),
    findings.items.map((f) => f.title),
  )
  if (!finding) return
  await page.goto(`${CONSOLE}${finding.link}`)
  await page.locator('[data-testid="contract-no"]', { hasText: state.contractNo }).waitFor()
  check('the reminder link opens the contract', true)
  await page.goto(`${CONSOLE}/wake`)
  await page.waitForSelector('[data-testid="wake-finding"][data-check="contract_expiring"]')
  await page.screenshot({ path: `${SHOTS}/p23-05-wake.png`, fullPage: true })
}

async function fromOrder(page) {
  await page.goto(`${CONSOLE}/orders?id=${state.orderId}`)
  await page.click('[data-testid="order-contract"]')
  await page.waitForURL(/\/contracts/)
  const dialog = page.locator('.el-dialog:visible', { hasText: 'AI 生成合同' })
  await dialog.waitFor()
  await page.waitForFunction(
    (no) => document.querySelector('[data-testid="contract-order-select"]')?.textContent.includes(no),
    state.orderNo,
  )
  const customer = await page.locator('[data-testid="contract-customer-select"]').innerText()
  check('order details open AI generation with the order and customer', customer.includes('张经理'), customer)
  await page.screenshot({ path: `${SHOTS}/p23-06-from-order.png`, fullPage: true })
  await dialog.locator('button', { hasText: '取消' }).click()
  await dialog.waitFor({ state: 'hidden' })
}

async function fromTemplate(page) {
  await page.goto(`${CONSOLE}/contracts?tab=templates`)
  await page.locator('[data-testid="templates-table"] .el-table__row', { hasText: '工服定制合同' }).first().click()
  await page.click('[data-testid="template-use"]')
  const dialog = page.locator('.el-dialog:visible', { hasText: '新建合同' })
  await dialog.waitFor()
  await page
    .waitForFunction(
      () => document.querySelector('[data-testid="contract-template-select"]')?.textContent.includes('工服定制合同'),
      null,
      { timeout: 10_000 },
    )
    .catch(() => null)
  const template = await page.locator('[data-testid="contract-template-select"]').innerText()
  check('按模板新建 preselects the template', template.includes('工服定制合同'), template)
  await page.click('[data-testid="contract-generate-submit"]')
  await success(page, '已新建合同草稿')
  await page.locator('[data-testid="contract-no"]').waitFor()
  // 没选客户和订单：模板的默认值和我方信息填上，甲方、交货日期和订单的标的、金额留给人填。
  const place = await fieldValue(page, '交货地点')
  const party = await fieldValue(page, '我方名称')
  const count = await page.locator('[data-testid="contract-missing-count"]').innerText()
  check(
    'template default and party filled; the rest left to fill',
    place === '上海仓' && party === PARTY && count.includes('5 项待填写'),
    { place, party, count },
  )
  // 作废：写原因。
  await page.click('[data-testid="contract-void"]')
  await prompt(page, '客户取消合作', '作废')
  await success(page, '已作废')
  const reason = await page.locator('[data-testid="contract-void-reason"]').innerText()
  check('voided with the reason kept', reason.includes('客户取消合作'), reason)
  await page.screenshot({ path: `${SHOTS}/p23-07-void.png`, fullPage: true })
  await closeDrawer(page)
  await tab(page, '模板')
  await page.waitForSelector('[data-testid="templates-table"] .el-table__row')
  const used = await page
    .locator('[data-testid="templates-table"] .el-table__row', { hasText: '工服定制合同.docx' })
    .locator('[data-testid="template-row-used"]')
    .innerText()
  check('template use counted', used.trim() === '1', used)
}

async function agentScope(browser) {
  const { ctx, page } = await consoleLogin(browser, 'alice')
  const menu = await page.locator('[data-testid="main-menu"]').innerText()
  check('agent sees the 合同 menu', menu.includes('合同'), menu)
  await page.goto(`${CONSOLE}/contracts`)
  await page.waitForSelector('[data-testid="contracts-table"]')
  await page.waitForFunction(() => !document.querySelector('[data-testid="contracts-table"] .el-loading-mask'))
  const rows = await page.locator('[data-testid="contracts-table"] .el-table__row').count()
  const settings = await page.locator('[data-testid="contract-settings-open"]').count()
  const manage = await page.locator('[data-testid="contract-category-create"]').count()
  check("agent sees none of the admin's contracts and cannot manage", rows === 0 && settings === 0 && manage === 0, {
    rows,
    settings,
    manage,
  })
  await page.click('[data-testid="contract-new"]')
  await page.locator('input[data-testid="contract-title-input"]').fill('小艾的报价合同')
  await page.click('[data-testid="contract-generate-submit"]')
  await success(page, '已新建合同草稿')
  await page.locator('[data-testid="contract-no"]').waitFor()
  const info = await page.locator('[data-testid="contract-info"]').innerText()
  check('her new contract is hers', info.includes('小艾'), info)
  await page.screenshot({ path: `${SHOTS}/p23-08-agent.png`, fullPage: true })
  await closeDrawer(page)
  await page.waitForFunction(
    () => document.querySelectorAll('[data-testid="contracts-table"] .el-table__row').length === 1,
  )
  const all = await json(`${API}/api/v1/contracts`, { token: state.admin })
  check('admin sees every contract', all.total === 3, all.total)
  await ctx.close()
}

;(async () => {
  fs.mkdirSync(SHOTS, { recursive: true })
  await prepareTenant()
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined })
  try {
    const { ctx, page } = await consoleLogin(browser, 'admin')
    const menu = await page.locator('[data-testid="main-menu"]').innerText()
    check('admin sees the 合同 menu', menu.includes('合同'), menu)
    await categories(page)
    await uploadTemplate(page)
    await aiDraft(page)
    await wakeReminder(page)
    await fromOrder(page)
    await fromTemplate(page)
    await ctx.close()
    await agentScope(browser)
  } catch (error) {
    summary.checks.push(`FAIL exception -> ${error.stack || error}`)
  } finally {
    await browser.close()
  }
  check('no console errors', summary.consoleErrors.length === 0, summary.consoleErrors)
  fs.writeFileSync(`${SHOTS}/summary.json`, JSON.stringify(summary, null, 2))
  for (const line of summary.checks) console.log(line)
  const failed = summary.checks.filter((line) => line.startsWith('FAIL'))
  console.log(failed.length ? `${failed.length} FAILED` : `ALL ${summary.checks.length} PASSED`)
  process.exit(failed.length ? 1 : 0)
})()
