// P25 验收：企业资料（设计文档 §36）——文件夹、一次选多个文件直传模拟 OSS（大视频分片上传、显示进度）、
// 封面、在线查看（视频、PDF、图片、文字资料）、下载、写文字资料和修改正文、搜索和筛选、分享给客户（客户不用
// 登录打开 Widget 上的分享页，停用后失效）、加入知识库、病毒扫描拦截、删除和文件夹管理、坐席的权限，在浏览器
// 里走通。
//
// 1. 准备：企业和管理员、坐席小艾；在本地生成 PDF、PNG（浏览器生成）、约 6 MB 的 MP4（ffmpeg）、含 EICAR 测试串的
//    TXT 和一个 .exe。
// 2. 管理员新建文件夹"产品资料"和下级"安装教程"，选中"安装教程"后一次选 5 个文件：.exe 被拦下；其余 4 个
//    依次上传（限速后能看到视频的上传进度）；视频超过 4 MB 走分片上传（每片 1 MB），从 OSS 下载回来和原文件
//    一样。
// 3. 病毒扫描：含 EICAR 的文件被拦截，上传的人收到站内信，点开显示"含有病毒，已拦截"，不能再下载。
// 4. 查看：视频能播放（截帧作封面）、PDF 和图片在线看，浏览次数增加；下载 PDF 的文件名是原来的。
// 5. 文字资料：写一份"门锁安装说明"（标题、列表、表格），预览排版；搜索正文里的词能找到；修改正文。
// 6. 修改名称、说明、标签，移到上一级文件夹；按标签、类型筛选；列表显示。
// 7. 分享：生成 7 天的链接，手机尺寸的浏览器不登录打开——视频能播放、能下载；文字资料的分享页排版显示；
//    打开次数；停用后提示失效。
// 8. 加入知识库：文字资料导入知识库（kb-jobs），知识库里有这条知识。
// 9. 文件夹和删除：有资料的文件夹不能删；删除资料后 OSS 上的文件不在了；空文件夹能删；改名。
// 10. 小艾：有"资料"菜单，能看、能上传；没有文件夹管理，不能修改、删除管理员的资料，能删除自己的。
// 11. 运营后台的健康检查里企业资料存储正常；页面上显示已用空间。
//
// 前置：scripts/ci/start-stack.sh 启动的平台（模拟 OSS :8905、模拟 clamd :3310），e2e.env 里分片上传的阈值是
// 4 MB、每片 1 MB；需要 ffmpeg 生成视频。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<密码> node scripts/e2e/p25-materials-acceptance.cjs
const { chromium } = require('playwright')
const { execFile } = require('child_process')
const { promisify } = require('util')
const crypto = require('crypto')
const fs = require('fs')
const os = require('os')
const path = require('path')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const PLATFORM_USER = env('PLATFORM_USER', 'ops')
const PLATFORM_PASSWORD = process.env.PLATFORM_PASSWORD
const SHOTS = env('SHOTS', 'e2e-shots/p25')
const BACKEND_DIR = env('BACKEND_DIR', path.resolve(__dirname, '../../backend'))
const RUN = Date.now().toString(36).slice(-5)
const TENANT = `material-${RUN}`
const PASSWORD = 'demo-pass-2026'
const PART_SIZE = 1024 * 1024
// 拼起来，免得仓库里的文件被杀毒软件当成病毒。
const EICAR = ['X5O!P%@AP[4\\PZX54(P^)7CC)7}$', 'EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*'].join('')
const TEXT_BODY = [
  '# 门锁安装说明',
  '',
  '安装前**先断电**，准备好螺丝刀和开孔器。',
  '- 拆下旧锁',
  '- 装上新锁，样板间在九亭镇 88 号',
  '',
  '| 型号 | 开孔尺寸 |',
  '| --- | --- |',
  '| X1 | 24 mm |',
].join('\n')

const summary = { tenant: TENANT, checks: [], consoleErrors: [] }
const check = (name, ok, detail) =>
  summary.checks.push(
    `${ok ? 'PASS' : 'FAIL'} ${name}${ok || detail === undefined ? '' : ` -> ${JSON.stringify(detail)}`}`,
  )
const state = { contexts: [], pages: [], files: {} }

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
  const response = await fetch(url, { headers: token ? { authorization: `Bearer ${token}` } : {} })
  return response.status
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

async function until(label, probe, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs
  let last
  while (Date.now() < deadline) {
    last = await probe()
    if (last) return last
    await sleep(500)
  }
  throw new Error(`timed out waiting for ${label}`)
}

const sha256 = (buffer) => crypto.createHash('sha256').update(buffer).digest('hex')

/** 管理员看到的资料（按名称）。 */
async function materials(query = '') {
  const page = await json(`${API}/api/v1/materials?limit=100${query}`, { token: state.admin })
  return page
}

async function materialNamed(name, token = state.admin) {
  const page = await json(`${API}/api/v1/materials?limit=100&q=${encodeURIComponent(name)}`, { token })
  return page.items.find((m) => m.name === name)
}

async function link(id, purpose, token = state.admin) {
  return json(`${API}/api/v1/materials/${id}/link?purpose=${purpose}`, { token })
}

// ---- 1. 准备 ----

async function prepare(browser) {
  const platform = await json(`${API}/platform/v1/auth/login`, {
    method: 'POST',
    body: { username: PLATFORM_USER, password: PLATFORM_PASSWORD },
  })
  state.ops = platform.access_token
  await json(`${API}/platform/v1/tenants`, {
    method: 'POST',
    token: state.ops,
    body: {
      code: TENANT,
      name: `资料验收 ${RUN}`,
      admin: { username: 'admin', display_name: '管理员', password: PASSWORD },
    },
  })
  state.admin = await login('admin')
  await json(`${API}/api/v1/staff`, {
    method: 'POST',
    token: state.admin,
    body: { username: 'alice', display_name: '小艾', password: PASSWORD, role_codes: ['agent'] },
  })
  state.alice = await login('alice')

  // 本地文件：PDF 和 PNG 用浏览器生成，视频用 ffmpeg 生成（6 MB 左右，超过 4 MB 走分片上传）。
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'p25-'))
  state.dir = dir
  const page = await browser.newPage()
  await page.setContent(
    '<div id="card" style="width:480px;height:300px;display:flex;align-items:center;justify-content:center;' +
      'font:28px sans-serif;color:#fff;background:linear-gradient(135deg,#409eff,#67c23a)">EDP Door Lock X1</div>',
  )
  const files = state.files
  files.pdf = path.join(dir, '产品介绍.pdf')
  fs.writeFileSync(files.pdf, await page.pdf({ width: '600px', height: '400px', printBackground: true }))
  files.png = path.join(dir, '门锁外观.png')
  fs.writeFileSync(files.png, await page.locator('#card').screenshot())
  await page.close()
  // Playwright 的 Chromium 不带 H.264 解码，测试视频用 VP9 编码的 MP4（加噪点，6 秒约 5.8 MB）。
  files.mp4 = path.join(dir, '安装演示.mp4')
  await run('ffmpeg', [
    ...['-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=25', '-t', '6'],
    ...['-vf', 'noise=alls=30:allf=t', '-c:v', 'libvpx-vp9', '-deadline', 'realtime', '-cpu-used', '8'],
    ...['-b:v', '8M', '-minrate', '8M', '-maxrate', '8M', '-pix_fmt', 'yuv420p', '-movflags', '+faststart'],
    files.mp4,
  ])
  files.eicar = path.join(dir, '报价单.txt')
  fs.writeFileSync(files.eicar, `${EICAR}\n报价单（测试病毒扫描）\n`)
  files.exe = path.join(dir, 'setup.exe')
  fs.writeFileSync(files.exe, 'MZ not really a program')
  files.note = path.join(dir, '小艾的笔记.txt')
  fs.writeFileSync(files.note, '客户常问：门锁没电了怎么办？用 Type-C 应急供电。\n')
  state.videoSize = fs.statSync(files.mp4).size
  state.videoHash = sha256(fs.readFileSync(files.mp4))
  check('the test video is over the multipart threshold', state.videoSize > 4 * PART_SIZE, state.videoSize)
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

async function success(page, text, timeout = 20_000) {
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

/** 确认框（ElMessageBox.confirm）：点按钮。 */
async function confirm(page, button) {
  const box = page.locator('.el-message-box:visible')
  await box.waitFor()
  await box.locator('button', { hasText: button }).click()
  await box.waitFor({ state: 'hidden' }).catch(() => null)
}

async function closeDrawer(page) {
  await page.locator('.el-drawer__close-btn:visible').last().click()
  await page.locator('[data-testid="material-drawer"]').waitFor({ state: 'hidden' }).catch(() => null)
}

async function openMaterials(page) {
  await page.goto(`${CONSOLE}/materials`)
  await page.waitForSelector('[data-testid="material-folder-tree"]')
  await page.locator('[data-testid="material-usage"]').waitFor()
}

async function folder(page, name) {
  await page.locator(`[data-testid="material-folder-${name}"]`).click()
}

async function card(page, name) {
  const item = page.locator(`[data-testid="material-card-${name}"]`)
  await item.waitFor()
  return item
}

async function openCard(page, name) {
  await (await card(page, name)).click()
  const drawer = page.locator('[data-testid="material-drawer"]')
  await drawer.waitFor()
  await drawer.locator('[data-testid="material-facts"]').waitFor()
  return drawer
}

async function folderMenu(page, name, action) {
  await page.locator(`[data-testid="material-folder-more-${name}"]`).click()
  await page.locator('.el-dropdown-menu__item:visible', { hasText: action }).first().click()
}

// ---- 2. 文件夹和上传 ----

async function foldersAndUpload(page) {
  await openMaterials(page)
  const usage = await page.locator('[data-testid="material-usage"]').innerText()
  check('the page shows the storage used', usage.includes('企业资料已用 0 B'), usage)

  await page.click('[data-testid="material-folder-create"]')
  await prompt(page, '产品资料', '确定')
  await success(page, '已新建')
  await page.locator('[data-testid="material-folder-产品资料"]').waitFor()
  await folderMenu(page, '产品资料', '新建下级文件夹')
  await prompt(page, '安装教程', '确定')
  await success(page, '已新建')
  await page.locator('[data-testid="material-folder-安装教程"]').waitFor()
  await folder(page, '安装教程')
  check('a two-level folder tree is created', true)

  // 记下浏览器直传 OSS 的请求：视频应该分片上传。
  const putRequests = []
  page.on('request', (request) => {
    if (request.method() === 'PUT' && request.url().startsWith('http://127.0.0.1:8905/')) {
      putRequests.push(request.url())
    }
  })
  // 限速上传（1 MB/s），能看到视频的上传进度。
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('Network.enable')
  await cdp.send('Network.emulateNetworkConditions', {
    offline: false,
    latency: 0,
    downloadThroughput: -1,
    uploadThroughput: 1024 * 1024,
  })
  const files = state.files
  await page.setInputFiles('[data-testid="material-file-input"]', [
    files.pdf,
    files.png,
    files.mp4,
    files.eicar,
    files.exe,
  ])
  const rejected = page.locator('.el-message--error', { hasText: '不支持「setup.exe」' })
  check(
    'the .exe is rejected before uploading',
    await rejected.waitFor({ timeout: 5000 }).then(
      () => true,
      () => false,
    ),
  )
  const progress = page.locator('[data-testid="material-upload-progress"]')
  await progress.waitFor({ timeout: 30_000 })
  await until('the video upload to show progress', async () => {
    const name = await page.locator('[data-testid="material-upload-item"]').first().innerText()
    const percent = Number((await progress.locator('.el-progress__text').innerText()).replace('%', ''))
    return name.includes('安装演示.mp4') && percent > 10 && percent < 100
  })
  await page.screenshot({ path: `${SHOTS}/p25-01-uploading.png` })
  const queued = await page.locator('[data-testid="material-upload-item"]').allInnerTexts()
  check(
    'the upload queue shows the video with its progress and the file waiting after it',
    queued.length === 2 && queued[0].includes('安装演示.mp4') && queued[1].includes('等待上传'),
    queued,
  )
  await page.locator('[data-testid="material-uploads"]').waitFor({ state: 'hidden', timeout: 60_000 })
  await cdp.send('Network.emulateNetworkConditions', {
    offline: false,
    latency: 0,
    downloadThroughput: -1,
    uploadThroughput: -1,
  })

  for (const name of ['产品介绍', '门锁外观', '安装演示', '报价单']) await card(page, name)
  const page1 = await materials()
  const folderIds = new Set(page1.items.map((m) => m.folder_path))
  check(
    'four files are uploaded into 产品资料 / 安装教程 and are ready',
    page1.total === 4 &&
      page1.items.every((m) => m.status === 'ready') &&
      folderIds.size === 1 &&
      folderIds.has('产品资料 / 安装教程'),
    page1.items.map((m) => [m.name, m.status, m.folder_path]),
  )
  const parts = putRequests.filter((url) => url.includes('partNumber='))
  const expectedParts = Math.ceil(state.videoSize / PART_SIZE)
  check(
    `the video is uploaded in ${expectedParts} parts straight to OSS, the small files in one PUT each`,
    parts.length === expectedParts && putRequests.length - parts.length === 3,
    { parts: parts.length, total: putRequests.length },
  )
  const video = page1.items.find((m) => m.name === '安装演示')
  state.videoId = video.id
  const download = await link(video.id, 'download')
  const response = await fetch(download.url)
  const body = Buffer.from(await response.arrayBuffer())
  check(
    'the video downloaded from OSS is identical to the original',
    response.ok && body.length === state.videoSize && sha256(body) === state.videoHash,
    { status: response.status, size: body.length },
  )
  const tree = await page.locator('[data-testid="material-folder-tree"]').innerText()
  check('folder counts include sub-folders', /产品资料\s*4/.test(tree) && /安装教程\s*4/.test(tree), tree)
  const usageAfter = await page.locator('[data-testid="material-usage"]').innerText()
  check('the storage used grows', usageAfter.includes('MB'), usageAfter)
  await page.screenshot({ path: `${SHOTS}/p25-02-grid.png` })
}

// ---- 3. 病毒扫描 ----

async function virusScan(page) {
  // 调度进程每分钟也会扫描一批，所以不看命令行这一次扫到几个，看结果。
  await cli('security-jobs')
  const infected = await until('the scan to block the EICAR file', async () => {
    const found = await materialNamed('报价单')
    return found?.scan_status !== 'pending' && found
  })
  check('the infected file is blocked', infected?.status === 'blocked' && infected.scan_status === 'infected', infected)
  const clean = await materialNamed('产品介绍')
  check('the clean PDF passes the scan', clean?.scan_status === 'clean', clean?.scan_status)
  check(
    'a blocked file can no longer be downloaded',
    (await status(`${API}/api/v1/materials/${infected.id}/link?purpose=download`, state.admin)) === 410,
  )
  const notes = await json(`${API}/api/v1/notifications?limit=20`, { token: state.admin })
  const note = (notes.items ?? []).find((n) => n.kind === 'material_blocked')
  check(
    'the uploader is notified with a link to the material',
    note?.title === '资料含有病毒，已被拦截：报价单' && note.link === `/materials?id=${infected.id}`,
    note,
  )
  await page.goto(`${CONSOLE}${note.link}`)
  const drawer = page.locator('[data-testid="material-drawer"]')
  await drawer.waitFor()
  await drawer.locator('[data-testid="material-blocked"]').waitFor()
  const buttons = await drawer.locator('[data-testid="material-download"], [data-testid="material-share-create"]').count()
  check('the notification link opens the blocked material, which cannot be downloaded or shared', buttons === 0)
  await page.screenshot({ path: `${SHOTS}/p25-03-blocked.png` })
  await closeDrawer(page)
}

// ---- 4. 查看和下载 ----

async function viewing(page) {
  await openMaterials(page)
  const videoCard = await card(page, '安装演示')
  const cover = await videoCard.locator('img').getAttribute('src')
  check('the video card uses an OSS snapshot as its cover', (cover ?? '').includes('x-oss-process=video'), cover)

  let drawer = await openCard(page, '安装演示')
  const player = drawer.locator('video[data-testid="material-video"]')
  await player.waitFor()
  const ready = await until('the video to load', () => player.evaluate((v) => v.readyState >= 1 && v.duration > 5), 30_000).catch(() => false)
  const src = await player.getAttribute('src')
  check('the video plays from a signed OSS address', !!ready && (src ?? '').includes('x-oss-signature='), src)
  await page.screenshot({ path: `${SHOTS}/p25-04-video.png` })
  await closeDrawer(page)

  drawer = await openCard(page, '产品介绍')
  const frame = drawer.locator('iframe[data-testid="material-pdf"]')
  await frame.waitFor()
  const pdfUrl = await frame.getAttribute('src')
  const pdf = await fetch(pdfUrl)
  check(
    'the PDF opens inline from OSS',
    pdf.ok &&
      (pdf.headers.get('content-type') ?? '').startsWith('application/pdf') &&
      (pdf.headers.get('content-disposition') ?? '').startsWith('inline'),
    { status: pdf.status, type: pdf.headers.get('content-type'), disposition: pdf.headers.get('content-disposition') },
  )
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    drawer.locator('[data-testid="material-download"]').click(),
  ])
  const saved = path.join(state.dir, 'downloaded.pdf')
  await download.saveAs(saved)
  check(
    'downloading keeps the original file name and content',
    download.suggestedFilename() === '产品介绍.pdf' &&
      sha256(fs.readFileSync(saved)) === sha256(fs.readFileSync(state.files.pdf)),
    download.suggestedFilename(),
  )
  await closeDrawer(page)

  drawer = await openCard(page, '门锁外观')
  const image = drawer.locator('[data-testid="material-image"] img')
  await image.waitFor()
  const width = await until('the image to load', () => image.evaluate((img) => img.complete && img.naturalWidth), 15_000).catch(() => 0)
  check('the image shows in the preview', width > 0, width)
  await closeDrawer(page)

  const pdfMaterial = await materialNamed('产品介绍')
  const videoMaterial = await materialNamed('安装演示')
  check(
    'views and downloads are counted',
    pdfMaterial.views >= 1 && pdfMaterial.downloads === 1 && videoMaterial.views >= 1 && videoMaterial.downloads >= 1,
    [pdfMaterial.views, pdfMaterial.downloads, videoMaterial.views, videoMaterial.downloads],
  )
}

// ---- 5. 文字资料 ----

async function writing(page) {
  await folder(page, '产品资料')
  await page.click('[data-testid="material-write"]')
  const dialog = page.locator('[data-testid="material-text-dialog"]')
  await dialog.waitFor()
  await dialog.locator('[data-testid="material-text-name"]').fill('门锁安装说明')
  const tags = dialog.locator('[data-testid="material-text-tags"]')
  await tags.click()
  await page.keyboard.type('安装')
  await page.keyboard.press('Enter')
  await page.keyboard.press('Escape').catch(() => null)
  await dialog.locator('[data-testid="material-text-body"]').fill(TEXT_BODY)
  const preview = dialog.locator('[data-testid="material-text-preview"]')
  await preview.locator('h2', { hasText: '门锁安装说明' }).waitFor()
  check(
    'the editor previews the Markdown (heading, bold, list, table)',
    (await preview.locator('strong', { hasText: '先断电' }).count()) === 1 &&
      (await preview.locator('li').count()) === 2 &&
      (await preview.locator('table tr').count()) === 2,
  )
  await page.screenshot({ path: `${SHOTS}/p25-05-write.png` })
  await dialog.locator('[data-testid="material-text-save"]').click()
  await success(page, '已保存文字资料')
  const drawer = page.locator('[data-testid="material-drawer"]')
  await drawer.locator('[data-testid="material-markdown"]').waitFor()
  check(
    'the saved text opens laid out',
    (await drawer.locator('[data-testid="material-markdown"] li', { hasText: '拆下旧锁' }).count()) === 1,
  )
  const text = await materialNamed('门锁安装说明')
  state.textId = text.id
  check(
    'the text material is stored in 产品资料 with its tag',
    text.kind === 'text' && text.folder_path === '产品资料' && text.tags.includes('安装') && text.file_name === '门锁安装说明.md',
    text,
  )

  await drawer.locator('[data-testid="material-edit-text"]').click()
  const editor = page.locator('[data-testid="material-text-edit-dialog"]')
  await editor.waitFor()
  const body = editor.locator('[data-testid="material-text-body"]')
  check('editing starts from the current text', (await body.inputValue()) === TEXT_BODY)
  await body.fill(`${TEXT_BODY}\n- 调试指纹`)
  await editor.locator('[data-testid="material-text-save"]').click()
  await success(page, '已保存正文')
  await editor.waitFor({ state: 'hidden' })
  await drawer.locator('[data-testid="material-markdown"] li', { hasText: '调试指纹' }).waitFor()
  check('the edited text shows at once', true)
  await page.screenshot({ path: `${SHOTS}/p25-06-text.png` })
  await closeDrawer(page)

  await folder(page, '全部')
  await page.fill('[data-testid="material-search"]', '九亭镇')
  await page.press('[data-testid="material-search"]', 'Enter')
  await until('the search to narrow the list', async () => {
    const cards = await page.locator('[data-testid^="material-card-"]').count()
    return cards === 1 && (await page.locator('[data-testid="material-card-门锁安装说明"]').count()) === 1
  }, 15_000).catch(() => null)
  check(
    'searching a word in the text body finds the text material',
    (await page.locator('[data-testid^="material-card-"]').count()) === 1 &&
      (await page.locator('[data-testid="material-card-门锁安装说明"]').count()) === 1,
  )
  await page.fill('[data-testid="material-search"]', '')
  await page.press('[data-testid="material-search"]', 'Enter')
  await card(page, '安装演示')
}

// ---- 6. 修改、筛选 ----

async function editing(page) {
  const drawer = await openCard(page, '产品介绍')
  await drawer.locator('[data-testid="material-edit-name"]').fill('产品介绍（2026 版）')
  await drawer.locator('[data-testid="material-edit-description"]').fill('给经销商的产品手册')
  await drawer.locator('[data-testid="material-edit-tags"]').click()
  await page.keyboard.type('宣传')
  await page.keyboard.press('Enter')
  await page.keyboard.press('Escape').catch(() => null)
  // 移到上一级文件夹"产品资料"。
  await drawer.locator('[data-testid="material-edit-folder"] .el-cascader').click()
  const node = page.locator('.el-cascader-panel:visible .el-cascader-node', { hasText: '产品资料' }).first()
  await node.locator('.el-radio').click()
  await page.keyboard.press('Escape').catch(() => null)
  await drawer.locator('[data-testid="material-save"]').click()
  // 只等"已保存"，前面的"已保存正文"可能还没消失。
  await success(page, /^已保存$/)
  const pdf = await materialNamed('产品介绍（2026 版）')
  check(
    'name, description, tags and folder are saved',
    pdf?.description === '给经销商的产品手册' && pdf.tags.includes('宣传') && pdf.folder_path === '产品资料',
    pdf,
  )
  state.pdfId = pdf?.id
  await closeDrawer(page)

  await page.locator('[data-testid="material-tag-filter"]').click()
  await page.locator('.el-select-dropdown__item:visible', { hasText: '宣传' }).first().click()
  await until('the tag filter', async () => (await page.locator('[data-testid^="material-card-"]').count()) === 1, 15_000).catch(() => null)
  const tagged = await page.locator('[data-testid^="material-card-"]').count()
  check('filtering by tag shows the tagged material', tagged === 1 && (await page.locator('[data-testid="material-card-产品介绍（2026 版）"]').count()) === 1, tagged)
  await page.locator('[data-testid="material-tag-filter"] .el-select__suffix, [data-testid="material-tag-filter"] .el-icon').first().hover()
  await page.locator('[data-testid="material-tag-filter"] .el-select__clear').click().catch(() => null)

  await page.locator('[data-testid="material-kind-video"]').click()
  await until('the kind filter', async () => (await page.locator('[data-testid^="material-card-"]').count()) === 1, 15_000).catch(() => null)
  check(
    'filtering by kind shows only videos',
    (await page.locator('[data-testid^="material-card-"]').count()) === 1 &&
      (await page.locator('[data-testid="material-card-安装演示"]').count()) === 1,
  )
  await page.locator('[data-testid="material-kinds"] .el-radio-button', { hasText: '全部' }).click()
  await page.locator('[data-testid="material-mode-table"]').click()
  await page.locator('[data-testid="material-table"]').waitFor()
  await until('every material in the table', async () => (await page.locator('[data-testid="material-table"] .el-table__row').count()) === 5, 15_000).catch(() => null)
  const rows = await page.locator('[data-testid="material-table"] .el-table__row').count()
  check('the list mode shows every material in a table', rows === 5, rows)
  await page.screenshot({ path: `${SHOTS}/p25-07-list.png` })
  await page.locator('[data-testid="material-mode-grid"]').click()
  await page.locator('[data-testid="material-grid"]').waitFor()
}

// ---- 7. 分享 ----

async function sharing(browser, page) {
  let drawer = await openCard(page, '安装演示')
  await drawer.locator('[data-testid="material-share-create"]').click()
  await success(page, '已生成分享链接')
  const shareUrl = await drawer.locator('[data-testid="material-share-url"]').first().inputValue()
  check('a 7-day share link is created', shareUrl.includes('?share='), shareUrl)
  await closeDrawer(page)

  drawer = await openCard(page, '门锁安装说明')
  await drawer.locator('[data-testid="material-share-create"]').click()
  await success(page, '已生成分享链接')
  const textUrl = await drawer.locator('[data-testid="material-share-url"]').first().inputValue()
  await closeDrawer(page)

  const phone = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 2,
    isMobile: true,
    hasTouch: true,
    locale: 'zh-CN',
  })
  state.contexts.push(phone)
  const customer = await phone.newPage()
  state.pages.push(customer)
  watchErrors(customer, 'customer')
  await customer.goto(shareUrl)
  await customer.locator('[data-testid="share-name"]').waitFor()
  const player = customer.locator('video[data-testid="share-video"]')
  await player.waitFor()
  const ready = await until('the shared video to load', () => player.evaluate((v) => v.readyState >= 1), 30_000).catch(() => false)
  const href = await customer.locator('[data-testid="share-download"]').getAttribute('href')
  const company = await customer.locator('.header .title').innerText()
  check(
    'the customer opens the share page without logging in: video plays, download offered',
    !!ready &&
      (await customer.locator('[data-testid="share-name"]').innerText()) === '安装演示' &&
      company.startsWith('资料验收') &&
      (href ?? '').includes('response-content-disposition='),
    { company, href },
  )
  await customer.screenshot({ path: `${SHOTS}/p25-08-share-video.png` })

  await customer.goto(textUrl)
  const shared = customer.locator('[data-testid="share-text"]')
  await shared.waitFor()
  check(
    'the shared text material is laid out for the customer',
    (await shared.locator('h2', { hasText: '门锁安装说明' }).count()) === 1 &&
      (await shared.locator('li', { hasText: '调试指纹' }).count()) === 1,
  )
  await customer.screenshot({ path: `${SHOTS}/p25-09-share-text.png` })

  drawer = await openCard(page, '安装演示')
  const row = drawer.locator('[data-testid="material-share"]').first()
  await row.waitFor()
  check('the share shows how often it was opened', (await row.innerText()).includes('打开 1 次'), await row.innerText())
  await row.locator('[data-testid="material-share-disable"]').click()
  await confirm(page, '停用')
  await success(page, '已停用')
  await closeDrawer(page)
  await customer.goto(shareUrl)
  const error = customer.locator('[data-testid="share-error"]')
  await error.waitFor()
  check('a disabled link tells the customer it has expired', (await error.innerText()).includes('失效'), await error.innerText())
}

// ---- 8. 加入知识库 ----

async function knowledge(page) {
  const drawer = await openCard(page, '门锁安装说明')
  await drawer.locator('[data-testid="material-knowledge"]').click()
  await page.locator('[data-testid="material-knowledge-save"]').click()
  await success(page, '已开始加入知识库')
  await closeDrawer(page)
  // 调度进程也会执行导入任务：等任务完成。
  await cli('kb-jobs')
  const job = await until('the import job to finish', async () => {
    const jobs = await json(`${API}/api/v1/kb/imports`, { token: state.admin })
    const found = jobs.items.find((j) => j.source === '门锁安装说明.md')
    return found && ['done', 'failed'].includes(found.status) && found
  })
  const items = await json(`${API}/api/v1/kb/items?q=${encodeURIComponent('门锁安装')}`, { token: state.admin })
  check(
    'the text material is imported into the knowledge base as drafts',
    job?.status === 'done' && job.result.created >= 1 && items.items.some((i) => i.status === 'draft'),
    { job, items: items.items.map((i) => [i.title, i.status]) },
  )
}

// ---- 9. 文件夹和删除 ----

async function foldersAndDelete(page) {
  await openMaterials(page)
  await folderMenu(page, '安装教程', '删除')
  await confirm(page, '删除')
  const refused = page.locator('.el-message--error').last()
  await refused.waitFor()
  check('a folder with materials cannot be deleted', (await refused.innerText()).length > 0, await refused.innerText())

  const image = await materialNamed('门锁外观')
  const before = await link(image.id, 'download')
  const drawer = await openCard(page, '门锁外观')
  await drawer.locator('[data-testid="material-delete"]').click()
  await confirm(page, '删除')
  await success(page, '已删除')
  await page.locator('[data-testid="material-card-门锁外观"]').waitFor({ state: 'detached' })
  const gone = await fetch(before.url)
  check('deleting a material deletes its file on OSS', gone.status === 404, gone.status)

  await page.click('[data-testid="material-folder-create"]')
  await prompt(page, '临时', '确定')
  await success(page, '已新建')
  await folderMenu(page, '临时', '删除')
  await confirm(page, '删除')
  await success(page, '已删除')
  await page.locator('[data-testid="material-folder-临时"]').waitFor({ state: 'detached' })
  await folderMenu(page, '产品资料', '改名')
  await prompt(page, '产品资料库', '确定')
  await success(page, '已改名')
  await page.locator('[data-testid="material-folder-产品资料库"]').waitFor()
  const renamed = await materialNamed('门锁安装说明')
  check('an empty folder is deleted and renaming a folder updates the paths', renamed.folder_path === '产品资料库', renamed.folder_path)
}

// ---- 10. 坐席 ----

async function agent(alice) {
  const menu = await alice.locator('[data-testid="main-menu"]').innerText()
  check('小艾 has the 资料 menu', menu.includes('资料'), menu)
  await openMaterials(alice)
  check('小艾 cannot manage folders', (await alice.locator('[data-testid="material-folder-create"]').count()) === 0)
  const drawer = await openCard(alice, '产品介绍（2026 版）')
  const controls = await drawer.locator('[data-testid="material-save"], [data-testid="material-delete"]').count()
  check("小艾 can view the admin's material but not edit or delete it", controls === 0 && (await drawer.locator('[data-testid="material-download"]').count()) === 1)
  await closeDrawer(alice)

  await alice.setInputFiles('[data-testid="material-file-input"]', [state.files.note])
  await success(alice, '已上传「小艾的笔记」')
  const own = await openCard(alice, '小艾的笔记')
  await own.locator('[data-testid="material-text-preview"]').waitFor()
  check('小艾 uploads a text document and reads it online', (await own.locator('[data-testid="material-text-preview"]').innerText()).includes('Type-C'))
  await alice.screenshot({ path: `${SHOTS}/p25-10-agent.png` })
  await own.locator('[data-testid="material-delete"]').click()
  await confirm(alice, '删除')
  await success(alice, '已删除')
  check('小艾 deletes her own material', !(await materialNamed('小艾的笔记')))
}

// ---- 11. 运营后台 ----

async function opsHealth() {
  const health = await json(`${API}/platform/v1/health`, { token: state.ops })
  const oss = (health.components ?? []).find((c) => c.key === 'oss')
  check('the platform health check shows OSS is reachable', oss?.status === 'ok', oss)
}

;(async () => {
  if (!PLATFORM_PASSWORD) {
    console.error('请通过环境变量 PLATFORM_PASSWORD 提供平台运营账号的密码')
    process.exit(2)
  }
  fs.mkdirSync(SHOTS, { recursive: true })
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined })
  try {
    await prepare(browser)
    const admin = await consoleLogin(browser, 'admin')
    await foldersAndUpload(admin)
    await virusScan(admin)
    await viewing(admin)
    await writing(admin)
    await editing(admin)
    await sharing(browser, admin)
    await knowledge(admin)
    await foldersAndDelete(admin)
    const alice = await consoleLogin(browser, 'alice')
    await agent(alice)
    await opsHealth()
  } catch (error) {
    summary.checks.push(`FAIL exception -> ${error.stack || error}`)
    for (const [i, page] of state.pages.entries()) {
      await page.screenshot({ path: `${SHOTS}/failure-${i + 1}.png` }).catch(() => undefined)
    }
  } finally {
    for (const ctx of state.contexts) await ctx.close().catch(() => undefined)
    await browser.close()
    if (state.dir) fs.rmSync(state.dir, { recursive: true, force: true })
  }
  check('no console errors', summary.consoleErrors.length === 0, summary.consoleErrors)
  fs.writeFileSync(`${SHOTS}/summary.json`, JSON.stringify(summary, null, 2))
  for (const line of summary.checks) console.log(line)
  const failed = summary.checks.filter((line) => line.startsWith('FAIL'))
  console.log(failed.length ? `${failed.length} FAILED` : `ALL ${summary.checks.length} PASSED`)
  process.exit(failed.length ? 1 : 0)
})()
