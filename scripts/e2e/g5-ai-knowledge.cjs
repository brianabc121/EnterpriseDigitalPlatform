// AI 与知识增强的浏览器验收（G5）：
//
// 1. 运营后台：添加支持工具调用、带重排序模型的供应商，指定给验收租户并设置并发上限；为"AI 回复"
//    保存新的提示词版本并启用（结束时改回内置）。
// 2. 控制台：AI 接待打开工具调用和分段发送；知识库空间树里新建"售后"空间和"物流"分类，新建必读问答
//    （负责人小博、推送给售后组）；渠道设置限定只用"售后"空间的知识、单独调整转人工灵敏度。
// 3. 知识导入：上传 Markdown 文档、抓取本地的帮助中心网站，导入记录显示完成，站内信提醒。
// 4. 访客：长回答在"正在输入"之后分段发送；访客评价 AI 回答"没用"，知识的访客评价计数更新。
// 5. 工具调用：访客留下公司和电话，AI 登记线索；转人工后小艾在客户资料里确认写入档案。
// 6. 坐席助手：客户情绪激动、坐席使用承诺用语时，工作台显示实时提醒。
// 7. 会话小结：小艾结束会话后生成小结，修改标签后确认写入客户档案；访客评价满意。
// 8. 按技能组推送与到期提醒：小艾的知识动态里有推送给售后组的必读知识，小博没有；知识即将到期时
//    负责人小博收到站内信。
// 9. 优秀话术：满意会话经提炼后出现话术候选，审核通过后成为共享快捷话术；知识详情显示使用与满意度；
//    运营后台的大模型用量页看到这个租户的调用和费用。
//
// 前置：同 P3（后端、实时消费进程、调度进程接到模拟大模型；控制台、运营后台、Widget、OpenIM）。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/g5-ai-knowledge.cjs
const { chromium } = require('playwright')
const { execFileSync, spawn } = require('child_process')
const fs = require('fs')
const os = require('os')
const path = require('path')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PLATFORM = env('PLATFORM_URL', 'http://localhost:5174')
const WIDGET = env('WIDGET_URL', 'http://localhost:5175')
const FAKE_LLM = env('FAKE_LLM_URL', 'http://127.0.0.1:8900/v1')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/g5')
const HELP_PORT = Number(env('HELP_PORT', '8902'))
const BACKEND_DIR = path.resolve(__dirname, '../../backend')
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `g5-${RUN}`
const PASSWORD = 'demo-pass-2026'
const PROVIDER = `验收模型-${RUN}`
// 超过 120 字：网页渠道分段发送。
const SHIPPING =
  '发货说明：下单后仓库会在二十四小时内打包，打包完成后短信通知您快递单号。' +
  '您可以在订单详情页查看物流轨迹，一般两到三天送达，偏远地区五到七天。' +
  '签收时请当面检查外包装，如有破损可以拒收并联系我们重新发货。\n' +
  '温馨提示：节假日期间快递量较大，送达时间可能延后一到两天；需要指定送达时间的，' +
  '请在下单备注里写明，我们会尽量安排。'
const PURCHASE = '批量采购请留下联系方式，销售会在一个工作日内回电。'
const APOLOGY = '非常抱歉让您久等了，我保证今天下班前给您处理好并回复进度。'

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

/** 在后端目录执行命令行工具（知识导入、到期提醒、提炼等平时由调度进程执行的任务）。 */
function cli(...args) {
  const output = execFileSync('uv', ['run', 'python', '-m', 'app.cli', ...args], {
    cwd: BACKEND_DIR,
    env: process.env,
    encoding: 'utf-8',
  })
  const lines = output.trim().split('\n')
  return JSON.parse(lines[lines.length - 1])
}

async function control(body) {
  return json(`${FAKE_LLM}/_control`, { method: 'POST', body: { mode: 'normal', ...body } })
}

// ---- 本地的"帮助中心"网站（抓取导入用） ----

function startHelpSite() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'edp-help-'))
  fs.mkdirSync(path.join(root, 'docs'))
  const page = (title, body) =>
    `<!doctype html><html><head><meta charset="utf-8"><title>${title}</title></head><body>` +
    `<nav><a href="/docs/">帮助中心首页</a> 导航菜单</nav>${body}<footer>版权所有</footer></body></html>`
  fs.writeFileSync(path.join(root, 'robots.txt'), 'User-agent: *\nDisallow: /docs/internal/\n')
  fs.writeFileSync(
    path.join(root, 'docs', 'index.html'),
    page(
      '帮助中心',
      '<h1>帮助中心</h1><a href="invoice.html">发票</a><a href="members.html">会员</a>' +
        '<a href="internal/secret.html">内部</a>',
    ),
  )
  fs.writeFileSync(
    path.join(root, 'docs', 'invoice.html'),
    page(
      '发票说明',
      '<main><h1>发票说明</h1><p>在订单详情里申请电子发票，开具后发送到您的邮箱。</p>' +
        '<h2>抬头</h2><p>支持个人和企业抬头，企业抬头需要填写税号。</p></main>',
    ),
  )
  fs.writeFileSync(
    path.join(root, 'docs', 'members.html'),
    page(
      '会员权益',
      '<main><h1>会员权益</h1><p>会员享受免运费和优先发货，每月还有专属优惠券。</p></main>',
    ),
  )
  fs.mkdirSync(path.join(root, 'docs', 'internal'))
  fs.writeFileSync(path.join(root, 'docs', 'internal', 'secret.html'), page('内部', '<p>内部资料</p>'))
  const server = spawn('python3', ['-m', 'http.server', String(HELP_PORT), '--bind', '127.0.0.1'], {
    cwd: root,
    stdio: 'ignore',
  })
  return { server, url: `http://127.0.0.1:${HELP_PORT}/docs/` }
}

// ---- 准备 ----

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
      name: `AI 知识验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  const admin = await login('admin')
  const staff = {}
  for (const [username, name] of [
    ['alice', '小艾'],
    ['bob', '小博'],
  ]) {
    const created = await json(`${API}/api/v1/staff`, {
      method: 'POST',
      token: admin,
      body: { username, display_name: name, password: PASSWORD, role_codes: ['agent'] },
    })
    staff[username] = created.id
  }
  const group = await json(`${API}/api/v1/skill-groups`, {
    method: 'POST',
    token: admin,
    body: { name: '售后组', members: [{ staff_id: staff.alice }] },
  })
  const policies = await json(`${API}/api/v1/routing-policies`, { token: admin })
  const policy = policies.items.find((p) => p.is_default)
  await json(`${API}/api/v1/routing-policies/${policy.id}`, {
    method: 'PATCH',
    token: admin,
    body: { mode: 'ai_first', default_skill_group_id: group.id },
  })
  const channels = await json(`${API}/api/v1/channels`, { token: admin })
  return {
    tenantId: tenant.id,
    ops: platform.access_token,
    admin,
    staff,
    groupId: group.id,
    channel: channels.items[0],
  }
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

async function menu(page, title) {
  await page.locator('[data-testid="main-menu"] .el-menu-item', { hasText: title }).click()
}

async function confirmBox(page, text) {
  const box = page.locator('.el-message-box:visible')
  await box.waitFor()
  await box.locator('.el-message-box__btns button', { hasText: text }).click()
}

async function promptBox(page, value) {
  const box = page.locator('.el-message-box:visible')
  await box.waitFor()
  await box.locator('input').fill(value)
  await box.locator('.el-message-box__btns .el-button--primary').click()
  await box.waitFor({ state: 'hidden' })
}

/** 数字输入框：填写后按 Tab，失去焦点时才会提交数值。 */
async function fillNumber(locator, value) {
  await locator.fill(value)
  await locator.press('Tab')
}

async function pickOption(page, text) {
  await page.locator('.el-select-dropdown__item:visible', { hasText: text }).first().click()
}

// ---- 1. 运营后台 ----

async function platformSection(browser, ctx) {
  const ops = await newPage(browser, 'platform')
  await ops.goto(`${PLATFORM}/login`)
  await ops.fill('input[autocomplete="username"]', PLATFORM_USER)
  await ops.fill('input[autocomplete="current-password"]', PLATFORM_PASSWORD)
  await ops.click('button:has-text("登录")')
  await ops.locator('[data-testid="platform-menu"]').waitFor()

  await ops.locator('[data-testid="menu-providers"]').click()
  await ops.click('[data-testid="provider-create"]')
  await ops.locator('input[data-testid="provider-name"]').fill(PROVIDER)
  await ops.locator('input[data-testid="provider-url"]').fill(FAKE_LLM)
  await ops.locator('input[data-testid="provider-key"]').fill('sk-g5-acceptance')
  await ops.locator('input[data-testid="provider-chat-model"]').fill('fake-chat')
  await ops.locator('input[data-testid="provider-rerank"]').fill('fake-rerank')
  await ops.locator('[data-testid="provider-cap-tools"]').click()
  await fillNumber(ops.locator('[data-testid="provider-price-input"] input'), '1')
  await fillNumber(ops.locator('[data-testid="provider-price-output"] input'), '2')
  await shot(ops, '1-provider-form')
  await ops.click('[data-testid="provider-save"]')
  const row = ops.locator('[data-testid="provider-table"] .el-table__row', { hasText: PROVIDER })
  const capsShown =
    (await seen(row.locator('[data-testid="provider-caps"]', { hasText: '工具调用' }))) &&
    (await seen(row.locator('[data-testid="provider-caps"]', { hasText: '重排序' })))
  const providers = await json(`${API}/platform/v1/llm-providers`, { token: ctx.ops })
  const provider = providers.items.find((p) => p.name === PROVIDER)
  ctx.providerId = provider?.id
  check(
    '运营添加供应商：能力标签"工具调用"，重排序模型 fake-rerank',
    capsShown && provider?.capabilities.tools === true && provider.rerank_model === 'fake-rerank',
    provider,
  )

  await ops.locator('[data-testid="menu-tenants"]').click()
  const tenantRow = ops.locator('[data-testid="tenant-table"] .el-table__row', { hasText: TENANT })
  await tenantRow.waitFor()
  await tenantRow.locator('[data-testid="tenant-detail-button"]').click()
  await ops.locator('.el-tabs__item', { hasText: '大模型' }).click()
  const llmPanel = ops.locator('[data-testid="tenant-llm"]')
  await llmPanel.waitFor()
  await llmPanel.locator('.el-select').first().click()
  await pickOption(ops, PROVIDER)
  await fillNumber(ops.locator('[data-testid="tenant-llm-concurrency"] input'), '4')
  await llmPanel.locator('button', { hasText: '保存' }).click()
  const assigned = await waitFor(async () => {
    const info = await json(`${API}/platform/v1/tenants/${ctx.tenantId}/llm`, { token: ctx.ops })
    return info.provider_id === ctx.providerId && info.concurrency === 4 ? info : null
  })
  await shot(ops, '1-tenant-llm')
  check('指定给验收租户，并发上限 4', !!assigned, assigned)

  await ops.locator('[data-testid="menu-prompts"]').click()
  const card = ops.locator('[data-testid="prompt-card"]', { hasText: 'reply' }).first()
  await card.waitFor()
  await card.locator('[data-testid="prompt-new-version"]').click()
  const content = ops.locator('textarea[data-testid="prompt-content"]')
  await content.fill(`${await content.inputValue()}\n回答时先给结论，再补充细节。`)
  await ops.click('[data-testid="prompt-save"]')
  const prompts = await waitFor(async () => {
    const list = await json(`${API}/platform/v1/prompts`, { token: ctx.ops })
    const reply = list.items.find((p) => p.key === 'reply')
    return reply?.active_version ? reply : null
  })
  ctx.replyVersion = prompts?.active_version
  await shot(ops, '1-prompts')
  check(
    '"AI 回复"保存新的提示词版本并启用',
    !!prompts && prompts.versions.some((v) => v.active && v.content.includes('先给结论')),
    prompts?.versions,
  )
  return ops
}

// ---- 2. 控制台设置与知识库 ----

async function consoleSection(browser, ctx) {
  const page = await newPage(browser, 'admin')
  await consoleLogin(page, 'admin')

  await menu(page, 'AI 接待')
  await page.locator('[data-testid="ai-settings"]').waitFor()
  await page.locator('[data-testid="ai-enabled"]').click()
  await page.locator('[data-testid="ai-tools"]').click()
  await shot(page, '2-ai-settings')
  await page.click('[data-testid="ai-save"]')
  const settings = await waitFor(async () => {
    const current = await json(`${API}/api/v1/ai/settings`, { token: ctx.admin })
    return current.enabled && current.tools_enabled ? current : null
  })
  check(
    'AI 接待：启用，工具调用（模型支持）、分段发送、答案缓存打开',
    !!settings && settings.tools_supported && settings.segment_replies && settings.answer_cache,
    settings,
  )

  // 知识库：空间树新建空间和分类。
  await menu(page, '知识库')
  await page.locator('[data-testid="kb-space-tree"]').waitFor()
  await page.click('[data-testid="kb-space-create"]')
  await promptBox(page, '售后')
  const spaceNode = page.locator('[data-testid="kb-node-售后"]')
  await spaceNode.waitFor()
  await spaceNode.locator('.more').click()
  await page.locator('.el-dropdown-menu__item:visible', { hasText: '新建分类' }).click()
  await promptBox(page, '物流')
  await page.locator('[data-testid="kb-node-物流"]').waitFor()
  const spaces = await json(`${API}/api/v1/kb/spaces`, { token: ctx.admin })
  const space = spaces.items.find((s) => s.name === '售后')
  const category = space?.categories.find((c) => c.name === '物流')
  ctx.spaceId = space?.id
  ctx.categoryId = category?.id
  check('知识库空间树：新建"售后"空间和"物流"分类', !!category, spaces)

  // 在"物流"分类下新建必读问答：负责人小博，推送给售后组。
  await page.locator('[data-testid="kb-node-物流"]').click()
  await page.click('[data-testid="kb-create"]')
  await page.locator('[data-testid="kb-create-faq"]').click()
  await page.locator('input[data-testid="kb-title"]').fill('快递几天能到？')
  await page.locator('textarea[data-testid="kb-content"]').fill(SHIPPING)
  await page.locator('[data-testid="kb-owner"]').click()
  await pickOption(page, '小博')
  await page.locator('[data-testid="kb-audience"]').click()
  await pickOption(page, '售后组')
  await page.keyboard.press('Escape')
  await page.locator('[data-testid="kb-must-read"]').click()
  await shot(page, '2-kb-editor')
  await page.click('[data-testid="kb-save-publish"]')
  const item = await waitFor(async () => {
    const list = await json(`${API}/api/v1/kb/items?q=${encodeURIComponent('快递几天能到')}`, {
      token: ctx.admin,
    })
    return list.items.find((i) => i.status === 'published') ?? null
  })
  ctx.itemId = item?.id
  check(
    '新建必读问答：归入"售后 / 物流"，负责人小博，推送给售后组',
    !!item &&
      item.space_id === ctx.spaceId &&
      item.category_id === ctx.categoryId &&
      item.owner_id === ctx.staff.bob &&
      item.audience_group_ids.join() === ctx.groupId &&
      item.must_read,
    item,
  )
  const inCategory = await seen(
    page.locator('[data-testid="kb-table"] .el-table__row', { hasText: '售后 / 物流' }),
  )
  await shot(page, '2-kb-tree')
  check('按分类筛选，列表显示"售后 / 物流"', inCategory)

  // 其余知识（接口创建）：批量采购问答（售后空间）、三天后到期的活动规则（负责人小博）。
  await json(`${API}/api/v1/kb/items`, {
    method: 'POST',
    token: ctx.admin,
    body: {
      title: '批量采购有优惠吗？',
      content: PURCHASE,
      questions: ['想批量采购'],
      space_id: ctx.spaceId,
      publish: true,
    },
  })
  await json(`${API}/api/v1/kb/items`, {
    method: 'POST',
    token: ctx.admin,
    body: {
      title: '国庆活动规则',
      content: '国庆期间全场九折，满 299 元包邮。',
      owner_id: ctx.staff.bob,
      valid_to: new Date(Date.now() + 3 * 24 * 3600 * 1000).toISOString(),
      publish: true,
    },
  })

  // 渠道：只用"售后"空间的知识，转人工灵敏度单独设为 0.8。
  await menu(page, '设置')
  await page.locator('[data-testid="settings-tabs"] .el-tabs__item', { hasText: '渠道' }).click()
  await page.locator(`[data-testid="edit-channel-${ctx.channel.name}"]`).click()
  await page.locator('[data-testid="channel-editor"]').waitFor()
  await fillNumber(page.locator('[data-testid="channel-handoff"] input'), '0.8')
  await page.locator('[data-testid="channel-spaces"]').click()
  await pickOption(page, '售后')
  await page.keyboard.press('Escape')
  await shot(page, '2-channel-editor')
  await page.locator('[data-testid="save-channel"]').first().click()
  const channel = await waitFor(async () => {
    const list = await json(`${API}/api/v1/channels`, { token: ctx.admin })
    const found = list.items.find((c) => c.id === ctx.channel.id)
    return found?.kb_space_ids?.length ? found : null
  })
  check(
    '渠道设置：知识范围限定"售后"空间，转人工灵敏度 0.8',
    !!channel && channel.kb_space_ids.join() === ctx.spaceId && channel.ai.handoff_threshold === 0.8,
    channel,
  )
  await page.keyboard.press('Escape')
  await page.locator('[data-testid="channel-editor"]').waitFor({ state: 'hidden' })
  return page
}

// ---- 3. 知识导入 ----

async function importSection(page, ctx, help) {
  await menu(page, '知识库')
  await page.click('[data-testid="kb-open-import"]')
  const dialog = page.locator('[data-testid="kb-import"]')
  await dialog.waitFor()
  await dialog.locator('.el-tabs__item', { hasText: '上传文件' }).click()
  await page.setInputFiles('input[data-testid="kb-upload-file"]', {
    name: '售后服务手册.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from(
      '# 售后服务手册\n\n本手册说明退换货规则。\n\n## 退货\n\n签收后七天内可以申请退货。\n\n' +
        '### 运费\n\n质量问题由我们承担运费。\n',
    ),
  })
  await dialog.locator('[data-testid="kb-upload-name"]', { hasText: '售后服务手册.md' }).waitFor()
  await dialog.locator('[data-testid="kb-import-publish"]').click()
  await page.click('[data-testid="kb-import-start"]')
  await dialog.locator('[data-testid="kb-import-jobs"] .el-table__row', { hasText: '售后服务手册.md' }).waitFor()

  await dialog.locator('.el-tabs__item', { hasText: '抓取帮助中心' }).click()
  await dialog.locator('input[data-testid="kb-crawl-url"]').fill(help.url)
  await page.click('[data-testid="kb-import-start"]')
  await dialog.locator('[data-testid="kb-import-jobs"] .el-table__row', { hasText: help.url }).waitFor()
  // 调度进程每 10 秒领取一次导入任务；这里也可以立即执行一轮。
  cli('kb-jobs')
  const finished = await waitFor(async () => {
    const jobs = await json(`${API}/api/v1/kb/imports`, { token: ctx.admin })
    return jobs.items.length === 2 && jobs.items.every((j) => j.status === 'done') ? jobs : null
  }, 60000)
  const doneRows = await seen(
    dialog.locator('[data-testid="kb-import-status"]', { hasText: '已完成' }).nth(1),
    15000,
  )
  await shot(page, '3-imports')
  const crawl = finished?.items.find((j) => j.kind === 'crawl')
  const upload = finished?.items.find((j) => j.kind === 'document')
  check(
    '上传 Markdown 文档：生成一条已发布的文档知识',
    upload?.result.created === 1,
    upload,
  )
  check(
    '抓取帮助中心：两个网页生成知识，robots.txt 禁止的目录没有抓取',
    crawl?.result.created === 2 &&
      crawl.result.errors.some((e) => e.includes('robots.txt 不允许抓取')) &&
      doneRows,
    crawl,
  )
  const docs = await json(`${API}/api/v1/kb/items?kind=doc&limit=50`, { token: ctx.admin })
  const invoice = docs.items.find((i) => i.title === '发票说明')
  check(
    '抓取的网页只保留正文（去掉导航和页脚），记下来源网址',
    !!invoice &&
      invoice.source === 'crawl' &&
      invoice.source_url.endsWith('/docs/invoice.html') &&
      invoice.content.startsWith('# 发票说明') &&
      !invoice.content.includes('导航') &&
      !invoice.content.includes('版权'),
    invoice,
  )
  await page.keyboard.press('Escape')

  // 站内信：导入完成的提醒。
  await page.reload()
  await page.locator('[data-testid="notification-bell"] .el-badge__content', { hasText: '2' }).waitFor({ timeout: 15000 }).catch(() => null)
  await page.click('[data-testid="notification-bell"]')
  const notices = page.locator('[data-testid="notification"]')
  const importNotice = await seen(notices.filter({ hasText: '知识导入完成' }))
  await shot(page, '3-notifications')
  check('站内信：导入完成的提醒', importNotice)
  await notices.filter({ hasText: '知识导入完成' }).first().click()
  await page.waitForURL(/\/knowledge/)
}

// ---- 4–7. 访客、AI 与工作台 ----

async function openVisitor(browser, ctx, label) {
  const page = await newPage(browser, label, { width: 420, height: 760 })
  await page.goto(`${WIDGET}/?key=${encodeURIComponent(ctx.channel.public_key)}`)
  await page.locator('[data-testid="widget-state"]', { hasText: '在线' }).waitFor()
  const token = await page.evaluate(
    (k) => localStorage.getItem(`edp.visitor.${k}`),
    ctx.channel.public_key,
  )
  return { page, token, label }
}

async function say(visitor, text) {
  await visitor.page.fill('[data-testid="message-input"]', text)
  await visitor.page.click('[data-testid="send-button"]')
  await visitor.page.locator('[data-testid="message"].me', { hasText: text }).waitFor()
}

async function visitorState(visitor) {
  return json(`${API}/api/v1/visitor/session`, { headers: { 'X-Visitor-Token': visitor.token } })
}

async function openWorkbench(browser, username) {
  const page = await newPage(browser, username)
  await consoleLogin(page, username)
  await menu(page, '工作台')
  await page.locator('[data-testid="agent-status"]', { hasText: '在线' }).waitFor({ timeout: 15000 })
  return page
}

async function aiSection(browser, ctx) {
  const visitor = await openVisitor(browser, ctx, 'visitor')
  // 记录"正在输入"是否出现过（出现时间很短）。
  await visitor.page.evaluate(() => {
    window.__typingSeen = false
    new MutationObserver(() => {
      if (document.querySelector('[data-testid="typing"]')) window.__typingSeen = true
    }).observe(document.body, { subtree: true, childList: true })
  })
  await say(visitor, '快递几天能到')
  const bots = visitor.page.locator('[data-testid="message"].bot')
  await waitFor(async () => ((await bots.count()) >= 2 ? true : null))
  const texts = await bots.allInnerTexts()
  const typing = await visitor.page.evaluate(() => window.__typingSeen)
  await shot(visitor.page, '4-widget-segments')
  check(
    '长回答在"正在输入"之后分段发送',
    texts.length >= 2 && typing && texts.join('').replace(/\s|👍|👎|AI|智能客服/g, '').includes('签收时请当面检查外包装'),
    { texts, typing },
  )

  await bots.first().locator('[data-testid="vote-down"]').click()
  const rated = await waitFor(async () => {
    const item = await json(`${API}/api/v1/kb/items/${ctx.itemId}`, { token: ctx.admin })
    return item.visitor_dislikes === 1 ? item : null
  })
  const voteOn = await seen(bots.first().locator('[data-testid="vote-down"].on'))
  check('访客评价 AI 回答"没用"，知识的访客评价计数加一', !!rated && voteOn, rated)

  // 工具调用：模型登记访客留下的线索。
  await control({
    tool_plan: [
      [
        'save_lead_info',
        { name: '王先生', company: '星河科技', phone: '[手机号1]', requirement: '采购 100 台' },
      ],
    ],
  })
  await say(visitor, '我是星河科技的王先生，电话13800001111，想批量采购100台')
  await bots.filter({ hasText: '批量采购请留下联系方式' }).waitFor({ timeout: 20000 })
  const state = await visitorState(visitor)
  const session = await json(`${API}/api/v1/sessions/${state.session_id}`, { token: ctx.admin })
  ctx.customerId = session.customer_id
  const drafts = await json(`${API}/api/v1/customers/${ctx.customerId}/lead-drafts`, {
    token: ctx.admin,
  })
  const decisions = await json(`${API}/api/v1/sessions/${state.session_id}/ai-decisions`, {
    token: ctx.admin,
  })
  check(
    '工具调用：AI 登记线索（手机号只保存掩码），待坐席确认',
    drafts.items.length === 1 &&
      drafts.items[0].status === 'pending' &&
      drafts.items[0].fields.phone === '138****1111' &&
      decisions.items.some((d) => (d.signals.tools ?? []).includes('save_lead_info')),
    { drafts, signals: decisions.items.map((d) => d.signals.tools) },
  )

  // 转人工：小艾接待，在客户资料里确认线索。
  const alice = await openWorkbench(browser, 'alice')
  await visitor.page.click('[data-testid="ask-human"]')
  const item = alice.locator('[data-testid="session-item"]').first()
  await item.waitFor({ timeout: 30000 })
  await item.click()
  const leads = alice.locator('[data-testid="lead-drafts"]')
  await leads.waitFor({ timeout: 15000 })
  await shot(alice, '5-lead-draft')
  await leads.locator('[data-testid="lead-confirm"]').click()
  const customer = await waitFor(async () => {
    const c = await json(`${API}/api/v1/customers/${ctx.customerId}`, { token: ctx.admin })
    return c.display_name === '王先生' ? c : null
  })
  check(
    '坐席确认线索：客户名称、公司、手机号写入档案，需求记到备注',
    !!customer &&
      customer.company === '星河科技' &&
      customer.phone === '138****1111' &&
      customer.notes.includes('需求：采购 100 台'),
    customer,
  )

  // 坐席助手的实时提醒。
  await say(visitor, '等了一个星期还没发货，我很生气')
  const alerts = alice.locator('[data-testid="copilot-alert"]')
  const negative = await seen(alerts.filter({ hasText: '客户情绪' }))
  await alice.fill('textarea[data-testid="composer-input"]', APOLOGY)
  await alice.click('[data-testid="send-button"]')
  const promise = await seen(alerts.filter({ hasText: '承诺用语' }))
  await shot(alice, '6-copilot-alerts')
  const recorded = await json(`${API}/api/v1/sessions/${state.session_id}/alerts`, {
    token: ctx.admin,
  })
  check(
    '坐席助手：客户情绪负面、坐席使用承诺用语时实时提醒（留痕）',
    negative &&
      promise &&
      ['negative', 'promise'].every((k) => recorded.items.some((a) => a.kind === k)),
    recorded.items,
  )

  // 会话小结：结束会话后生成，改标签后确认写入客户档案。
  await alice.click('[data-testid="close-session"]')
  await confirmBox(alice, '结束')
  const card = alice.locator('[data-testid="session-summary"]')
  await card.waitFor({ timeout: 15000 })
  if (!(await seen(card.locator('textarea[data-testid="summary-text"]'), 3000))) {
    await card.locator('[data-testid="summary-generate"]').click()
  }
  await card.locator('textarea[data-testid="summary-text"]').waitFor({ timeout: 20000 })
  const tags = card.locator('input[data-testid="summary-tags"]')
  await tags.fill('批量采购')
  await tags.press('Enter')
  await shot(alice, '7-summary-draft')
  await card.locator('[data-testid="summary-confirm"]').click()
  await card.locator('[data-testid="summary-confirmed"]').waitFor()
  const profile = await json(`${API}/api/v1/customers/${ctx.customerId}`, { token: ctx.admin })
  const summaries = await json(`${API}/api/v1/customers/${ctx.customerId}/summaries`, {
    token: ctx.admin,
  })
  check(
    '会话小结：确认后标签并入客户标签，小结记到备注最前面',
    profile.tags.includes('批量采购') &&
      /^【\d{4}-\d{2}-\d{2} 会话小结】/.test(profile.notes) &&
      summaries.items.length === 1,
    { tags: profile.tags, notes: profile.notes },
  )

  // 访客评价满意（优秀话术的来源）。
  await visitor.page.locator('[data-testid="csat"]').waitFor({ timeout: 20000 })
  await visitor.page.click('[data-testid="csat-5"]')
  await visitor.page.click('[data-testid="csat-submit"]')
  await visitor.page.locator('[data-testid="csat-thanks"]').waitFor()
  return { alice, visitor }
}

// ---- 8. 按技能组推送与到期提醒 ----

async function pushSection(ctx, pages) {
  const aliceToken = await login('alice')
  const bobToken = await login('bob')
  const aliceFeed = await json(`${API}/api/v1/kb/feed`, { token: aliceToken })
  const bobFeed = await json(`${API}/api/v1/kb/feed`, { token: bobToken })
  await pages.alice.locator('[data-testid="feed-tab"]').click()
  const inPanel = await seen(
    pages.alice.locator('[data-testid="must-read-item"]', { hasText: '快递几天能到' }),
  )
  await shot(pages.alice, '8-feed')
  check(
    '按技能组推送：售后组的小艾收到必读知识，小博没有',
    inPanel &&
      aliceFeed.must_read.some((e) => e.title === '快递几天能到？') &&
      !bobFeed.must_read.some((e) => e.title === '快递几天能到？'),
    { alice: aliceFeed.must_read.map((e) => e.title), bob: bobFeed.must_read.map((e) => e.title) },
  )

  // 到期提醒平时每小时执行一次；导入那一步执行 kb-jobs 时已经发出，这里再执行一轮也不会重复提醒。
  const again = cli('kb-jobs')
  const inbox = await json(`${API}/api/v1/notifications`, { token: bobToken })
  const reminders = inbox.items.filter(
    (n) => n.kind === 'kb_expiring' && n.title.includes('国庆活动规则'),
  )
  check(
    '知识到期提醒：负责人小博收到一次"知识即将到期"站内信',
    reminders.length === 1 && again.expiry_reminders === 0,
    { again, inbox: inbox.items.map((n) => n.title) },
  )
}

// ---- 9. 优秀话术、知识数据与用量 ----

async function phraseSection(ctx, pages, ops) {
  const report = cli('kb-extract', '--tenant', TENANT)
  const admin = pages.admin
  await menu(admin, '知识库')
  await admin.locator('[data-testid="kb-tabs"] .el-tabs__item', { hasText: '审核台' }).click()
  await admin.locator('[data-testid="candidate-kind-filter"] .el-radio-button', { hasText: '优秀话术' }).click()
  const row = admin.locator('.el-table__row', { hasText: '保证今天下班前' }).first()
  const found = await seen(row, 20000)
  check('提炼：满意会话里坐席的回复成为优秀话术候选', found && report.phrases >= 1, report)
  if (found) {
    await row.click()
    const drawer = admin.locator('[data-testid="candidate-drawer"]')
    await drawer.waitFor()
    await admin.locator('input[data-testid="candidate-question"]').fill('道歉并承诺跟进')
    await shot(admin, '9-phrase-candidate')
    await admin.click('[data-testid="approve-candidate"]')
    const replies = await waitFor(async () => {
      const list = await json(`${API}/api/v1/quick-replies`, { token: await login('alice') })
      return list.items.find((r) => r.shared && r.title === '道歉并承诺跟进') ?? null
    })
    check('审核通过后成为共享快捷话术（分类"优秀话术"）', replies?.category === '优秀话术', replies)
    await admin.keyboard.press('Escape')
  }

  // 知识详情：使用与满意度。
  await admin.goto(`${CONSOLE}/knowledge?item=${ctx.itemId}`)
  const stats = admin.locator('[data-testid="kb-item-stats"]')
  const statsShown = await seen(stats.filter({ hasText: '没用 1' }))
  await shot(admin, '9-item-stats')
  const api = await json(`${API}/api/v1/kb/items/${ctx.itemId}/stats`, { token: ctx.admin })
  check(
    '知识详情：引用次数、访客评价、引用它的 AI 会话数',
    statsShown && api.visitor_dislikes === 1 && api.ai_sessions >= 1,
    api,
  )

  await ops.locator('[data-testid="menu-llm-usage"]').click()
  const totals = ops.locator('[data-testid="llm-usage-totals"]')
  await totals.waitFor()
  const usage = await json(`${API}/platform/v1/llm-usage?days=7`, { token: ctx.ops })
  const tenantRow = usage.by_tenant.find((r) => r.key === TENANT)
  await shot(ops, '9-llm-usage')
  check(
    '运营后台大模型用量：这个租户有调用和估算费用',
    !!tenantRow && tenantRow.calls > 0 && tenantRow.cost > 0,
    tenantRow ?? usage.by_tenant.slice(0, 3),
  )
}

async function cleanup(ctx) {
  await control({ tool_plan: [] }).catch(() => null)
  if (ctx.ops) {
    // 提示词是平台级的：改回内置模板，不影响其他验收。
    await json(`${API}/platform/v1/prompts/reply/activate`, {
      method: 'POST',
      token: ctx.ops,
      body: { version: null },
    }).catch(() => null)
  }
}

async function run(browser) {
  const help = startHelpSite()
  const ctx = await prepareTenant()
  try {
    const ops = await platformSection(browser, ctx)
    const admin = await consoleSection(browser, ctx)
    await importSection(admin, ctx, help)
    const pages = await aiSection(browser, ctx)
    pages.admin = admin
    await pushSection(ctx, pages)
    await phraseSection(ctx, pages, ops)
    check('没有前端脚本错误', summary.consoleErrors.length === 0, summary.consoleErrors)
  } finally {
    help.server.kill()
    await cleanup(ctx)
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
    await browser.close()
    fs.writeFileSync(`${SHOTS}/summary.json`, JSON.stringify(summary, null, 2))
    console.log(JSON.stringify(summary, null, 2))
  }
  process.exit(summary.checks.some((c) => c.startsWith('FAIL')) ? 1 : 0)
})().catch((error) => {
  console.error(error)
  process.exit(1)
})
