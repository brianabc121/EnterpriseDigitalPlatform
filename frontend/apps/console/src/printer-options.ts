/**
 * 可用的云打印机（设计文档 §29.5）：加工页和单据详情的打印按钮用。一次会话里每种小票只查一次，
 * 设置里改了打印机后调用 resetPrinterOptions。
 */
import { api } from './api'
import type { PrinterOptions, PrinterUse } from './printing'

const cache: Partial<Record<PrinterUse, Promise<PrinterOptions | null>>> = {}

export function printerOptions(kind: PrinterUse): Promise<PrinterOptions | null> {
  cache[kind] ??= api
    .GET('/api/v1/print/options', { params: { query: { kind } } })
    .then(({ data }) => data ?? null)
    .catch(() => null)
  return cache[kind]!
}

export function resetPrinterOptions(): void {
  delete cache.order
  delete cache.requisition
}
