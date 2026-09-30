<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'

import { api, formatDateTime } from '../../api'
import { useAuthStore } from '../../stores/auth'

const EXPORT_STATUS: Record<string, string> = {
  pending: '排队中',
  running: '生成中',
  done: '已完成',
  failed: '失败',
}

const auth = useAuthStore()
const exports = ref<Schemas['TenantExportOut'][]>([])
const closure = ref<Schemas['ClosureStatus'] | null>(null)
const busy = ref(false)
const dialogOpen = ref(false)
const form = reactive({ password: '', confirmCode: '', reason: '' })

async function load(): Promise<void> {
  const [list, status] = await Promise.all([
    api.GET('/api/v1/tenant/exports'),
    api.GET('/api/v1/tenant/closure'),
  ])
  if (!list.data || !status.data) {
    ElMessage.error(errorMessage(list.error ?? status.error))
    return
  }
  exports.value = list.data.items
  closure.value = status.data
}

async function requestExport(): Promise<void> {
  busy.value = true
  const { data, error } = await api.POST('/api/v1/tenant/exports')
  busy.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已开始导出，几分钟后刷新列表下载')
  await load()
}

async function download(item: Schemas['TenantExportOut']): Promise<void> {
  const { data, error } = await api.GET('/api/v1/tenant/exports/{export_id}/download', {
    params: { path: { export_id: item.id } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  window.location.href = data.url
}

function formatSize(bytes: number | null | undefined): string {
  if (!bytes) return '—'
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function openClosure(): void {
  Object.assign(form, { password: '', confirmCode: '', reason: '' })
  dialogOpen.value = true
}

async function requestClosure(): Promise<void> {
  busy.value = true
  const { data, error } = await api.POST('/api/v1/tenant/closure', {
    body: { password: form.password, confirm_code: form.confirmCode.trim(), reason: form.reason || null },
  })
  busy.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  dialogOpen.value = false
  closure.value = data
  // 注销期间功能停止：刷新当前用户的功能开关（菜单、提醒）。
  await Promise.all([load(), auth.fetchMe()])
}

async function cancelClosure(): Promise<void> {
  busy.value = true
  const { data, error } = await api.DELETE('/api/v1/tenant/closure')
  busy.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  closure.value = data
  ElMessage.success('已撤销注销')
  await auth.fetchMe()
}

onMounted(load)
</script>

<template>
  <div data-testid="data-tab">
    <h4>数据导出</h4>
    <p class="sub">
      导出本企业的全部业务数据：客户、会话与消息、知识库、员工、设置等（每张表一个 JSON Lines 文件）以及聊天中的文件，
      打包成 ZIP。口令和渠道密钥不导出。文件保留几天后自动删除。
    </p>
    <el-button type="primary" :loading="busy" data-testid="export-request" @click="requestExport">
      导出数据
    </el-button>
    <el-button @click="load">刷新</el-button>
    <el-table :data="exports" size="small" class="table" empty-text="暂无导出" data-testid="export-table">
      <el-table-column label="申请时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag disable-transitions :type="row.status === 'done' ? 'success' : row.status === 'failed' ? 'danger' : 'info'">
            {{ EXPORT_STATUS[row.status] ?? row.status }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="大小" width="100">
        <template #default="{ row }">{{ formatSize(row.size) }}</template>
      </el-table-column>
      <el-table-column label="有效期至" width="170">
        <template #default="{ row }">{{ row.expires_at ? formatDateTime(row.expires_at) : '—' }}</template>
      </el-table-column>
      <el-table-column label="操作">
        <template #default="{ row }">
          <el-button
            v-if="row.status === 'done'"
            link
            type="primary"
            data-testid="export-download"
            @click="download(row)"
          >
            下载
          </el-button>
          <span v-else-if="row.error" class="error">{{ row.error }}</span>
        </template>
      </el-table-column>
    </el-table>

    <h4>注销企业账号</h4>
    <template v-if="closure">
      <template v-if="closure.closing">
        <el-alert
          type="error"
          :closable="false"
          show-icon
          data-testid="closure-status"
          :title="`已申请注销，将于 ${formatDateTime(closure.scheduled_at ?? '')} 删除全部业务数据`"
          description="在此之前可以下载导出的数据，或撤销注销恢复服务。新访客已不能发起咨询，AI 接待等功能已停止。"
        />
        <el-button class="gap" :loading="busy" data-testid="closure-cancel" @click="cancelClosure">
          撤销注销
        </el-button>
      </template>
      <template v-else>
        <p class="sub">
          注销后先自动导出一次数据，{{ closure.retention_days }} 天后删除全部业务数据（不能恢复），并留下删除记录。
          保留期内可以撤销。
        </p>
        <el-button type="danger" plain data-testid="closure-open" @click="openClosure">申请注销</el-button>
      </template>
    </template>

    <el-dialog v-model="dialogOpen" title="申请注销企业账号" width="440px">
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        :title="`保留期结束后删除全部业务数据，不能恢复`"
      />
      <el-form label-width="96px" class="gap">
        <el-form-item label="登录密码" required>
          <el-input v-model="form.password" type="password" show-password data-testid="closure-password" />
        </el-form-item>
        <el-form-item label="企业代码" required>
          <el-input
            v-model="form.confirmCode"
            :placeholder="`请输入 ${auth.me?.tenant.code ?? ''} 确认`"
            data-testid="closure-code"
          />
        </el-form-item>
        <el-form-item label="原因">
          <el-input v-model="form.reason" type="textarea" :rows="2" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogOpen = false">取消</el-button>
        <el-button type="danger" :loading="busy" data-testid="closure-submit" @click="requestClosure">
          申请注销
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.table {
  margin-top: 12px;
}

.gap {
  margin-top: 12px;
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.error {
  font-size: 12px;
  color: var(--el-color-danger);
}

h4 {
  margin: 20px 0 8px;
}
</style>
