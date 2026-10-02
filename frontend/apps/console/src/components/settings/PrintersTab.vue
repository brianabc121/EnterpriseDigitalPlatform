<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref } from 'vue'

import { api, formatDateTime } from '../../api'
import { resetPrinterOptions } from '../../printer-options'
import {
  brandInfo,
  PRINTER_STATUS_TAG,
  USE_SHORT,
  type Printer,
  type PrinterUse,
} from '../../printing'
import PrinterDialog from './PrinterDialog.vue'
import PrintJobsTable from './PrintJobsTable.vue'

/**
 * 设置 → 打印（设计文档 §29.4）：接入 80mm 云小票打印机（芯烨云、飞鹅云）。配置好之后，工人领取订单时
 * 自动打印加工单、开领料单时自动打印领料单；每张小票写明打印人和第几次打印。下面是打印记录。
 */
const printers = ref<Printer[]>([])
const loading = ref(false)
const dialogOpen = ref(false)
const editing = ref<Printer | null>(null)
const busy = ref<string | null>(null)
const jobs = ref<InstanceType<typeof PrintJobsTable> | null>(null)

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/print/printers')
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  printers.value = data.items
}

function add(): void {
  editing.value = null
  dialogOpen.value = true
}

function edit(printer: Printer): void {
  editing.value = printer
  dialogOpen.value = true
}

function replace(printer: Printer): void {
  resetPrinterOptions()
  const exists = printers.value.some((p) => p.id === printer.id)
  printers.value = exists
    ? printers.value.map((p) => (p.id === printer.id ? printer : p))
    : [...printers.value, printer]
}

async function test(printer: Printer): Promise<void> {
  busy.value = printer.id
  const { data, error } = await api.POST('/api/v1/print/printers/{printer_id}/test', {
    params: { path: { printer_id: printer.id } },
  })
  busy.value = null
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('测试页已发往打印机，结果见下面的打印记录')
  jobs.value?.reload()
}

async function check(printer: Printer): Promise<void> {
  busy.value = printer.id
  const { data, error } = await api.POST('/api/v1/print/printers/{printer_id}/check', {
    params: { path: { printer_id: printer.id } },
  })
  busy.value = null
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  replace(data)
  ElMessage.info(`${data.name}：${data.status_label}${data.last_error ? `（${data.last_error}）` : ''}`)
}

async function remove(printer: Printer): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `删除后不再往 ${printer.name} 打印，打印记录保留。`,
      '删除打印机',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  const { error, response } = await api.DELETE('/api/v1/print/printers/{printer_id}', {
    params: { path: { printer_id: printer.id } },
  })
  if (!response.ok) {
    ElMessage.error(errorMessage(error))
    return
  }
  printers.value = printers.value.filter((p) => p.id !== printer.id)
  resetPrinterOptions()
  ElMessage.success('已删除')
}

function uses(printer: Printer): string {
  return printer.uses.map((u: PrinterUse) => USE_SHORT[u]).join('、') || '只手工打印'
}

onMounted(load)
</script>

<template>
  <div>
    <el-alert
      type="info"
      :closable="false"
      show-icon
      class="tip"
      title="接入 80mm 云小票打印机（芯烨云、飞鹅云）后：工人领取订单时自动打印加工单，开领料单时自动打印领料单；每张小票写明打印人和第几次打印，也可以在加工页和单据里手工重打。"
      description="打印机只要连网（网线、Wi-Fi 或 4G）就能用，不需要电脑。开发者密钥加密保存，不会显示。"
    />
    <div class="bar">
      <el-button type="primary" data-testid="add-printer" @click="add">添加打印机</el-button>
    </div>
    <el-table
      v-loading="loading"
      :data="printers"
      data-testid="printer-table"
      empty-text="还没有接入打印机"
    >
      <el-table-column label="打印机" min-width="180">
        <template #default="{ row }">
          <div class="name" data-testid="printer-name-cell">{{ row.name }}</div>
          <small class="muted">{{ brandInfo(row.brand).label }} · {{ row.sn }}</small>
        </template>
      </el-table-column>
      <el-table-column label="自动打印" min-width="130">
        <template #default="{ row }">
          <span data-testid="printer-uses">{{ uses(row) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="份数" width="70" prop="copies" />
      <el-table-column label="状态" min-width="170">
        <template #default="{ row }">
          <el-tag :type="PRINTER_STATUS_TAG[row.status as Printer['status']]" data-testid="printer-status">
            {{ row.enabled ? row.status_label : '已停用' }}
          </el-tag>
          <div v-if="row.last_error" class="error" data-testid="printer-error">
            {{ row.last_error }}
          </div>
          <div v-if="row.status_checked_at" class="muted small">
            {{ formatDateTime(row.status_checked_at) }} 检查
          </div>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="260" fixed="right">
        <template #default="{ row }">
          <el-button
            link
            type="primary"
            :disabled="!row.enabled"
            :loading="busy === row.id"
            :data-testid="`test-printer-${row.sn}`"
            @click="test(row)"
          >
            测试打印
          </el-button>
          <el-button
            link
            type="primary"
            :loading="busy === row.id"
            :data-testid="`check-printer-${row.sn}`"
            @click="check(row)"
          >
            检查状态
          </el-button>
          <el-button link type="primary" :data-testid="`edit-printer-${row.sn}`" @click="edit(row)">
            修改
          </el-button>
          <el-button link type="danger" :data-testid="`delete-printer-${row.sn}`" @click="remove(row)">
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <h3 class="subtitle">打印记录</h3>
    <PrintJobsTable ref="jobs" />
    <PrinterDialog v-model="dialogOpen" :printer="editing" @saved="replace" />
  </div>
</template>

<style scoped>
.tip {
  margin-bottom: 12px;
}

.bar {
  margin-bottom: 12px;
}

.name {
  font-weight: 500;
}

.muted {
  color: var(--el-text-color-secondary);
}

.small {
  font-size: 12px;
}

.error {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.4;
  color: var(--el-color-danger);
}

.subtitle {
  margin: 24px 0 12px;
  font-size: 15px;
}
</style>
