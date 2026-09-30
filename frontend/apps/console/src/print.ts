/**
 * 打印单据（设计文档 §25.14）：只打印单据本身——单号、日期、明细、合计、开单人和确认人、签字栏。
 * 在新窗口里生成一张简单的打印页，不受控制台页面样式的影响。
 */
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
