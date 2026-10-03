// 员工导图验收：最顶部是企业所有者的卡片（§39.5）；从它向左、右、下三个方向悬停"＋"先生成待完善卡片（不弹表单），
// 刷新后仍在；点"完善信息"
// 填写员工资料后卡片变成员工，刷新后仍在；员工多了一排放不下时换行，整张图不超出显示框，可以缩放（§39.9）；卡片互不重叠。
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
    // 员工读完之前最顶部先显示企业卡片；等企业所有者的卡片出来。
    const root = page.locator('[data-root="true"]')
    await page.locator('[data-root="true"]:not([data-testid="staff-node-company"])').waitFor()
    const rootText = await root.innerText()
    const rootId = await root.getAttribute('data-testid')
    const company = (await root.locator('[data-testid="root-company"] strong').innerText()).trim()
    // 卡片的操作都在右上角的"编辑"图标里（§39.8）：最顶部的卡片里没有"停用"。
    const owner = rootId.replace('staff-node-', '')
    await page.getByTestId(`card-menu-${owner}`).click()
    const actions = page.getByTestId(`card-actions-${owner}`)
    await actions.waitFor()
    const items = await actions.locator('.el-dropdown-menu__item').allInnerTexts()
    await page.getByTestId('staff-tree').click({ position: { x: 5, y: 5 } })
    await actions.waitFor({ state: 'hidden' })
    check(
      'the top card is the enterprise owner under the enterprise name, without 停用',
      rootText.includes('企业所有者') && rootId !== 'staff-node-company' && company.length > 0 &&
        items.length > 0 && !items.some((text) => text.includes('停用')),
      { rootId, company, rootText, items },
    )
    for (const direction of ['left', 'right', 'down']) {
      const username = `diagram-${direction}-${RUN}`
      await root.hover()
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
    // 一排放不下时换行（§39.9）：再加 8 位员工（没有布局，排在企业所有者下方），整张图不超出显示框。
    const token = (await json(`${API}/api/v1/auth/login`, {
      method: 'POST',
      body: { tenant_code: tenant, username: login, password },
    })).access_token
    const many = Array.from({ length: 8 }, (_, index) => `wrap${index + 1}-${RUN}`)
    for (const [index, username] of many.entries()) {
      await json(`${API}/api/v1/staff`, {
        method: 'POST',
        token,
        body: { username, display_name: `换行${index + 1}`, password: `Diagram-${RUN}-test`, role_codes: ['agent'] },
      })
    }
    await page.reload()
    await page.getByTestId(`staff-node-${many[many.length - 1]}`).waitFor()
    await page.waitForTimeout(600)
    const frame = await page.locator('.tree-viewport').evaluate((element) => {
      const box = element.getBoundingClientRect()
      const root = element.querySelector('[data-root="true"]').getBoundingClientRect()
      return { scrollWidth: element.scrollWidth, clientWidth: element.clientWidth, left: box.left, right: box.right, rootLeft: root.left, rootRight: root.right }
    })
    const rows = new Set(
      await page.locator('[data-testid^="staff-node-wrap"]').evaluateAll((elements) => elements.map((element) => element.offsetTop)),
    )
    check(
      'many staff cards wrap into rows inside the frame: no sideways scrolling, the top card in view',
      frame.scrollWidth <= frame.clientWidth + 1 && rows.size >= 2 && frame.rootLeft >= frame.left && frame.rootRight <= frame.right,
      { frame, rows: [...rows] },
    )
    await page.screenshot({ path: `${SHOTS}/staff-diagram-wrap.png` })
    // 缩放：缩小一档是 90%，点百分比回到 100%，"适应宽度"是默认的。
    const zoomValue = page.getByTestId('tree-zoom-value')
    const fitPressed = async () => (await page.getByTestId('tree-zoom-fit').getAttribute('aria-pressed')) === 'true'
    const scale = () => page.locator('.staff-tree').evaluate((element) => element.style.transform)
    const before = { value: (await zoomValue.innerText()).trim(), fit: await fitPressed() }
    await page.getByTestId('tree-zoom-out').click()
    const out = { value: (await zoomValue.innerText()).trim(), scale: await scale(), fit: await fitPressed() }
    await page.screenshot({ path: `${SHOTS}/staff-diagram-zoom.png` })
    await zoomValue.click()
    const reset = { value: (await zoomValue.innerText()).trim(), scale: await scale() }
    await page.getByTestId('tree-zoom-fit').click()
    const fit = { value: (await zoomValue.innerText()).trim(), fit: await fitPressed() }
    check(
      'zoom: 适应宽度 by default, 缩小 to 90%, the percentage back to 100%, 适应宽度 again',
      before.value === '100%' && before.fit && out.value === '90%' && out.scale.includes('scale(0.9)') && !out.fit &&
        reset.value === '100%' && !reset.scale && fit.value === '100%' && fit.fit,
      { before, out, reset, fit },
    )
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
