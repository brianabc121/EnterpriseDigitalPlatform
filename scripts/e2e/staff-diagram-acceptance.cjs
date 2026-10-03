// 员工导图验收：从企业卡片向左、右、下三个方向悬停"＋"先生成待完善卡片（不弹表单），刷新后仍在；点"完善信息"
// 填写员工资料后卡片变成员工，刷新后仍在；卡片互不重叠。
//
// 指定 DIAGRAM_TENANT、DIAGRAM_USERNAME、DIAGRAM_PASSWORD 时用这个专用测试企业（会创建 3 位测试员工）；不指定时用
// PLATFORM_PASSWORD 开通一个新的测试企业（和其他验收脚本一样，CI 里这样运行）。
// 运行：NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<密码> node scripts/e2e/staff-diagram-acceptance.cjs
const assert = require('node:assert/strict')
const fs = require('fs')
const { chromium } = require('playwright')

const env = (name, fallback) => process.env[name] || fallback
const API = env('API_URL', 'http://localhost:8000')
const CONSOLE = env('CONSOLE_URL', 'http://localhost:5173')
const SHOTS = env('SHOTS', 'e2e-shots/staff-diagram')
const RUN = Date.now().toString(36)

const summary = { checks: [], consoleErrors: [] }
const check = (name, ok, detail) =>
  summary.checks.push(
    `${ok ? 'PASS' : 'FAIL'} ${name}${ok || detail === undefined ? '' : ` -> ${JSON.stringify(detail)}`}`,
  )

async function json(url, { method = 'GET', token, body } = {}) {
  const response = await fetch(url, {
    method,
    headers: { 'content-type': 'application/json', ...(token ? { authorization: `Bearer ${token}` } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const text = await response.text()
  if (!response.ok) throw new Error(`${method} ${url} -> ${response.status} ${text}`)
  return text ? JSON.parse(text) : null
}

/** 专用测试企业：指定了就用指定的，否则开通一个。 */
async function account() {
  const { DIAGRAM_TENANT, DIAGRAM_USERNAME, DIAGRAM_PASSWORD, PLATFORM_PASSWORD } = process.env
  if (DIAGRAM_TENANT && DIAGRAM_USERNAME && DIAGRAM_PASSWORD) {
    return { tenant: DIAGRAM_TENANT, username: DIAGRAM_USERNAME, password: DIAGRAM_PASSWORD }
  }
  assert(PLATFORM_PASSWORD, '请设置 PLATFORM_PASSWORD，或者专用测试企业的 DIAGRAM_TENANT、DIAGRAM_USERNAME、DIAGRAM_PASSWORD')
  const ops = await json(`${API}/platform/v1/auth/login`, {
    method: 'POST',
    body: { username: env('PLATFORM_USER', 'ops'), password: PLATFORM_PASSWORD },
  })
  const tenant = `diagram-${RUN.slice(-5)}`
  const password = 'demo-pass-2026'
  await json(`${API}/platform/v1/tenants`, {
    method: 'POST',
    token: ops.access_token,
    body: { code: tenant, name: `员工导图验收 ${RUN.slice(-5)}`, admin: { username: 'admin', display_name: '张总', password } },
  })
  return { tenant, username: 'admin', password }
}

async function main() {
  fs.mkdirSync(SHOTS, { recursive: true })
  const { tenant, username: login, password } = await account()
  summary.tenant = tenant
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_PATH || undefined })
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' })
    page.on('pageerror', (e) => summary.consoleErrors.push(e.message))
    await page.goto(`${CONSOLE}/staff`)
    await page.getByLabel('企业代码').fill(tenant)
    await page.getByLabel('用户名', { exact: true }).fill(login)
    await page.getByLabel('密码', { exact: true }).fill(password)
    await page.getByRole('button', { name: '登录', exact: true }).click()
    await page.getByTestId('staff-tree').waitFor()
    for (const direction of ['left', 'right', 'down']) {
      const username = `diagram-${direction}-${RUN}`
      await page.getByTestId('staff-node-company').hover()
      await page.getByTestId(`branch-company-${direction}`).click()
      const dialog = page.getByTestId('staff-create')
      const draft = page.locator('[data-testid^="staff-draft-"]').last()
      await draft.waitFor()
      check(`${direction}: the ＋ adds a draft card without opening the staff form`, !(await dialog.isVisible()))
      const draftTestId = await draft.getAttribute('data-testid')
      await page.reload()
      await page.getByTestId(draftTestId).getByRole('button', { name: '完善信息' }).click()
      check(`${direction}: the draft card survives a reload and opens 完善员工信息`, await dialog.getByTestId('branch-context').isVisible())
      await dialog.getByLabel('用户名', { exact: true }).fill(username)
      await dialog.getByLabel('姓名', { exact: true }).fill(`分支${direction}`)
      await dialog.getByLabel('初始密码', { exact: true }).fill(`Diagram-${RUN}-test`)
      await dialog.getByRole('button', { name: '保存', exact: true }).click()
      await page.getByTestId(`staff-node-${username}`).waitFor()
      await page.reload()
      await page.getByTestId(`staff-node-${username}`).waitFor()
      check(`${direction}: the staff card stays after a reload`, true)
    }
    const boxes = await page.locator('[data-node-id]').evaluateAll((elements) =>
      elements.map((element) => ({ x: element.offsetLeft, y: element.offsetTop, width: element.offsetWidth, height: element.offsetHeight })),
    )
    let overlap = null
    for (let i = 0; i < boxes.length && !overlap; i++) {
      for (let j = i + 1; j < boxes.length; j++) {
        const a = boxes[i]
        const b = boxes[j]
        if (!(a.x + a.width <= b.x || b.x + b.width <= a.x || a.y + a.height <= b.y || b.y + b.height <= a.y)) {
          overlap = [a, b]
          break
        }
      }
    }
    check('cards never overlap', overlap === null, overlap)
    await page.waitForTimeout(600)
    await page.screenshot({ path: `${SHOTS}/staff-diagram.png` })
  } catch (error) {
    summary.checks.push(`FAIL exception -> ${error.stack || error}`)
  } finally {
    await browser.close()
  }
  check('no page errors', summary.consoleErrors.length === 0, summary.consoleErrors)
  fs.writeFileSync(`${SHOTS}/summary.json`, JSON.stringify(summary, null, 2))
  for (const line of summary.checks) console.log(line)
  const failed = summary.checks.filter((line) => line.startsWith('FAIL'))
  console.log(failed.length ? `${failed.length} FAILED` : `ALL ${summary.checks.length} PASSED`)
  process.exitCode = failed.length ? 1 : 0
}

main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
