/**
 * 打印单据（设计文档 §25.14）：只打印单据本身——单号、日期、明细、合计、开单人和确认人、签字栏。
 * 在新窗口里生成一张简单的打印页，不受控制台页面样式的影响。
 */
import type { CustomerStatement } from './finance'
import { money } from './orders'
import type { WarehouseDocument } from './warehouse'
import { qty } from './warehouse'

function escape(value: unknown): string {
  return String(value ?? '').replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] ?? c,
  )
}

function day(value: string | null | undefined): string {
  if (!value) return ''
  const d = new Date(value)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 领料单、入库单的打印页。 */
export function documentHtml(doc: WarehouseDocument, company: string): string {
  const requisition = doc.kind === 'requisition'
  const rows = doc.lines
    .map(
      (line, i) => `<tr>
        <td>${i + 1}</td>
        <td>${escape(line.code)}</td>
        <td>${escape(line.name)}</td>
        <td>${escape(line.spec)}</td>
        <td>${escape(line.unit)}</td>
        <td class="num">${line.planned === null ? '' : escape(qty(line.planned))}</td>
        <td class="num">${escape(qty(line.quantity))}</td>
      </tr>`,
    )
    .join('')
  const signer = requisition ? '领料人' : '交货人'
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>${escape(doc.kind_label)} ${escape(doc.no)}</title>
<style>
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; margin: 24px; color: #111; }
  h1 { margin: 0 0 4px; font-size: 22px; text-align: center; letter-spacing: 4px; }
  .company { text-align: center; color: #555; margin-bottom: 16px; }
  .meta { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px 16px; margin-bottom: 12px; font-size: 13px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { border: 1px solid #333; padding: 6px 8px; }
  th { background: #f2f2f2; }
  .num { text-align: right; }
  tfoot td { font-weight: 600; }
  .sign { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-top: 32px; font-size: 13px; }
  .note { margin-top: 12px; font-size: 13px; }
  @media print { body { margin: 0; } }
</style></head><body>
<h1>${escape(doc.kind_label)}</h1>
<div class="company">${escape(company)}</div>
<div class="meta">
  <div>单号：${escape(doc.no)}</div>
  <div>开单时间：${escape(day(doc.submitted_at))}</div>
  <div>状态：${escape(doc.status_label)}</div>
  <div>关联订单：${escape(doc.order_no ?? '不关联订单')}</div>
  <div>开单人：${escape(doc.created_by_name)}</div>
  <div>确认人：${escape(doc.confirmed_by_name ?? '')}${doc.confirmed_at ? ` ${escape(day(doc.confirmed_at))}` : ''}</div>
</div>
<table>
  <thead><tr><th>#</th><th>代码</th><th>${requisition ? '材料' : '成品'}</th><th>规格</th><th>单位</th><th>建议数量</th><th>数量</th></tr></thead>
  <tbody>${rows}</tbody>
  <tfoot><tr><td colspan="7">共 ${doc.lines.length} 项</td></tr></tfoot>
</table>
${doc.note ? `<div class="note">备注：${escape(doc.note)}</div>` : ''}
<div class="sign"><div>开单人签字：</div><div>仓管签字：</div><div>${signer}签字：</div></div>
</body></html>`
}

const STATEMENT_KIND: Record<string, string> = { order: '订单', payment: '收款', refund: '退款' }

/** 客户对账单的打印页（§28.4）：企业名、客户、期间、期初、明细、期末、未收清的订单、制表人和日期。 */
export function statementHtml(statement: CustomerStatement, company: string): string {
  const rows = statement.lines
    .map(
      (line) => `<tr>
        <td>${escape(line.date)}</td>
        <td>${escape(STATEMENT_KIND[line.kind] ?? line.kind)}</td>
        <td>${escape(line.order_no)}</td>
        <td>${escape(line.description)}</td>
        <td class="num">${line.kind === 'payment' ? '−' : ''}${escape(money(line.amount))}</td>
        <td class="num">${escape(money(line.balance))}</td>
      </tr>`,
    )
    .join('')
  const open = statement.open_orders
    .map(
      (order) => `<tr>
        <td>${escape(order.no)}</td>
        <td>${escape(order.due_date ?? '未到期')}</td>
        <td class="num">${escape(money(order.total))}</td>
        <td class="num">${escape(money(Number(order.paid_amount) - Number(order.refunded_amount)))}</td>
        <td class="num">${escape(money(order.outstanding))}</td>
      </tr>`,
    )
    .join('')
  const customer = statement.company
    ? `${statement.customer_name}（${statement.company}）`
    : statement.customer_name
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>对账单 ${escape(statement.customer_name)}</title>
<style>
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; margin: 24px; color: #111; }
  h1 { margin: 0 0 4px; font-size: 22px; text-align: center; letter-spacing: 4px; }
  h2 { margin: 18px 0 8px; font-size: 15px; }
  .company { text-align: center; color: #555; margin-bottom: 16px; }
  .meta { display: grid; grid-template-columns: repeat(2, 1fr); gap: 6px 16px; margin-bottom: 12px; font-size: 13px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { border: 1px solid #333; padding: 6px 8px; }
  th { background: #f2f2f2; }
  .num { text-align: right; }
  tfoot td { font-weight: 600; }
  .foot { display: flex; justify-content: space-between; margin-top: 24px; font-size: 13px; }
</style></head><body>
<h1>对账单</h1>
<div class="company">${escape(company)}</div>
<div class="meta">
  <div>客户：${escape(customer)}</div>
  <div>期间：${escape(statement.period_from)} 至 ${escape(statement.period_to)}</div>
  <div>期初未收：${escape(money(statement.opening))}</div>
  <div>期末未收：${escape(money(statement.closing))}</div>
</div>
<table>
  <thead><tr><th>日期</th><th>类型</th><th>订单号</th><th>摘要</th><th class="num">金额</th><th class="num">未收余额</th></tr></thead>
  <tbody>${rows || '<tr><td colspan="6">期间内没有订单和收款</td></tr>'}</tbody>
  <tfoot><tr><td colspan="4">本期订单 ${escape(money(statement.orders_amount))}，收款 ${escape(money(statement.received))}，退款 ${escape(money(statement.refunded))}</td><td class="num" colspan="2">期末未收 ${escape(money(statement.closing))}</td></tr></tfoot>
</table>
<h2>未收清的订单</h2>
<table>
  <thead><tr><th>订单号</th><th>到期日</th><th class="num">合计</th><th class="num">已收</th><th class="num">未收</th></tr></thead>
  <tbody>${open || '<tr><td colspan="5">没有未收清的订单</td></tr>'}</tbody>
</table>
<div class="foot"><div>制表人：${escape(statement.generated_by)}</div><div>制表日期：${escape(day(statement.generated_at))}</div></div>
</body></html>`
}

/** 在新窗口里打开打印页并调出打印。 */
export function printHtml(html: string): boolean {
  const win = window.open('', '_blank', 'width=900,height=700')
  if (!win) return false
  win.document.open()
  win.document.write(html)
  win.document.close()
  win.focus()
  win.setTimeout(() => win.print(), 200)
  return true
}
