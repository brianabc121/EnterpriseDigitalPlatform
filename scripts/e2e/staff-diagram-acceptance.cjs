// 手动验收：指定专用测试租户和账号；会创建 3 位测试员工。不会自动运行。
const assert = require('node:assert/strict')
const { chromium } = require('playwright')

async function main() {
  const { DIAGRAM_TENANT, DIAGRAM_USERNAME, DIAGRAM_PASSWORD } = process.env
  assert(DIAGRAM_TENANT && DIAGRAM_USERNAME && DIAGRAM_PASSWORD, '请设置专用测试租户的 DIAGRAM_TENANT、DIAGRAM_USERNAME、DIAGRAM_PASSWORD')
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage()
    const url = process.env.CONSOLE_URL || 'http://localhost:5173'
    await page.goto(`${url}/staff`)
    await page.getByLabel('企业代码').fill(DIAGRAM_TENANT)
    await page.getByLabel('用户名', { exact: true }).fill(DIAGRAM_USERNAME)
    await page.getByLabel('密码', { exact: true }).fill(DIAGRAM_PASSWORD)
    await page.getByRole('button', { name: '登录', exact: true }).click()
    await page.getByTestId('staff-tree').waitFor()
    const run = Date.now().toString(36)
    for (const direction of ['left', 'right', 'down']) {
      const username = `diagram-${direction}-${run}`
      await page.getByTestId('staff-node-company').hover()
      await page.getByTestId(`branch-company-${direction}`).click()
      const dialog = page.getByTestId('staff-create')
      const draft = page.locator('[data-testid^="staff-draft-"]').last()
      await draft.waitFor()
      assert(!(await dialog.isVisible()), '新增卡片不能直接打开员工表单')
      const draftTestId = await draft.getAttribute('data-testid')
      await page.reload()
      await page.getByTestId(draftTestId).getByRole('button', { name: '完善信息' }).click()
      assert(await dialog.getByTestId('branch-context').isVisible())
      await dialog.getByLabel('用户名', { exact: true }).fill(username)
      await dialog.getByLabel('姓名', { exact: true }).fill(`分支${direction}`)
      await dialog.getByLabel('初始密码', { exact: true }).fill(`Diagram-${run}-test`)
      await dialog.getByRole('button', { name: '保存', exact: true }).click()
      await page.getByTestId(`staff-node-${username}`).waitFor()
      await page.reload()
      await page.getByTestId(`staff-node-${username}`).waitFor()
    }
    const boxes = await page.locator('[data-node-id]').evaluateAll((elements) => elements.map((element) => ({ x: element.offsetLeft, y: element.offsetTop, width: element.offsetWidth, height: element.offsetHeight })))
    for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
      const a = boxes[i], b = boxes[j]
      assert(a.x + a.width <= b.x || b.x + b.width <= a.x || a.y + a.height <= b.y || b.y + b.height <= a.y, '卡片不能重叠')
    }
    console.log('PASS: 悬停加号、先生成卡片、刷新保留、再完善员工、卡片不重叠')
  } finally { await browser.close() }
}
main().catch((error) => { console.error(error); process.exitCode = 1 })
