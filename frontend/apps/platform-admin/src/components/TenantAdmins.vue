<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'

import { api, formatDateTime } from '../api'
import { resetReasonError } from '../tenant-validation'

/**
 * 企业的管理员账号（设计文档 §38.2、§38.3）：企业拥有者（开通企业时创建的账号）和其他管理员；填写原因后重置
 * 管理员的密码。新密码是临时密码，管理员登录后要先设置新密码；重置记入企业的操作日志，企业的其他管理员收到提醒。
 */
type Admin = Schemas['TenantAdminOut']

const HINT =
  '企业拥有者是开通企业时创建的管理员账号。管理员忘记密码、企业里又没有别的管理员能重置时，在这里重置；' +
  '新密码是临时密码，管理员登录后要先设置自己的新密码。重置会记入企业的操作日志，企业的其他管理员会收到提醒。'

const props = defineProps<{ tenant: Schemas['TenantOut'] }>()

const admins = ref<Admin[]>([])
const loading = ref(false)
const resetOpen = ref(false)
const target = ref<Admin | null>(null)
const form = reactive({ reason: '', mode: 'generate' as 'generate' | 'manual', password: '' })
const saving = ref(false)
const result = ref<Schemas['PasswordResetResult'] | null>(null)

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/platform/v1/tenants/{tenant_id}/admins', {
    params: { path: { tenant_id: props.tenant.id } },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  admins.value = data.items
}

function openReset(admin: Admin): void {
  target.value = admin
  Object.assign(form, { reason: '', mode: 'generate', password: '' })
  result.value = null
  resetOpen.value = true
}

async function submit(): Promise<void> {
  if (!target.value) return
  const problem = resetReasonError(form.reason)
  if (problem) {
    ElMessage.warning(problem)
    return
  }
  if (form.mode === 'manual' && form.password.length < 8) {
    ElMessage.warning('新密码至少 8 位')
    return
  }
  saving.value = true
  const { data, error } = await api.POST('/platform/v1/tenants/{tenant_id}/admins/{staff_id}/password', {
    params: { path: { tenant_id: props.tenant.id, staff_id: target.value.id } },
    body: { reason: form.reason.trim(), password: form.mode === 'manual' ? form.password : null },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  result.value = data
  await load()
}

async function copy(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('浏览器不允许自动复制，请手动选择复制')
  }
}

onMounted(load)
</script>

<template>
  <div data-testid="tenant-admins">
    <p class="hint">{{ HINT }}</p>
    <el-table v-loading="loading" :data="admins" size="small" empty-text="这个企业没有管理员账号" data-testid="tenant-admins-table">
      <el-table-column label="用户名" min-width="160">
        <template #default="{ row }: { row: Admin }">
          {{ row.username }}
          <el-tag v-if="row.owner" size="small" type="warning" disable-transitions class="tag" data-testid="owner-tag">拥有者</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="display_name" label="姓名" min-width="120" />
      <el-table-column label="状态" width="150">
        <template #default="{ row }: { row: Admin }">
          <el-tag size="small" :type="row.status === 'active' ? 'success' : 'info'" disable-transitions>
            {{ row.status === 'active' ? '启用' : '停用' }}
          </el-tag>
          <el-tag v-if="row.must_change_password" size="small" type="warning" disable-transitions class="tag">待改密码</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="最近登录" width="170">
        <template #default="{ row }: { row: Admin }">
          {{ row.last_login_at ? formatDateTime(row.last_login_at) : '—' }}
        </template>
      </el-table-column>
      <el-table-column label="密码修改" width="170">
        <template #default="{ row }: { row: Admin }">
          {{ row.password_changed_at ? formatDateTime(row.password_changed_at) : '创建以来没有改过' }}
        </template>
      </el-table-column>
      <el-table-column label="" width="100">
        <template #default="{ row }: { row: Admin }">
          <el-button
            link
            type="primary"
            size="small"
            :disabled="tenant.status === 'closed'"
            :data-testid="`admin-reset-${row.username}`"
            @click="openReset(row)"
          >
            重置密码
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="resetOpen" title="重置管理员的密码" width="500px" data-testid="admin-reset-dialog">
      <template v-if="!result">
        <el-descriptions :column="1" border size="small" class="who">
          <el-descriptions-item label="企业">{{ tenant.name }}（{{ tenant.code }}）</el-descriptions-item>
          <el-descriptions-item label="账号">
            {{ target?.display_name }}（{{ target?.username }}）
            <el-tag v-if="target?.owner" size="small" type="warning" disable-transitions class="tag">拥有者</el-tag>
          </el-descriptions-item>
        </el-descriptions>
        <el-form label-position="top" @submit.prevent="submit">
          <el-form-item label="原因" required>
            <el-input
              v-model="form.reason"
              type="textarea"
              :rows="2"
              maxlength="200"
              show-word-limit
              placeholder="例如：企业负责人来电，核对营业执照后申请重置"
              data-testid="admin-reset-reason"
            />
          </el-form-item>
          <el-form-item label="新密码">
            <el-radio-group v-model="form.mode" data-testid="admin-reset-mode">
              <el-radio value="generate">自动生成</el-radio>
              <el-radio value="manual">手动设置</el-radio>
            </el-radio-group>
          </el-form-item>
          <el-form-item v-if="form.mode === 'manual'">
            <el-input
              v-model="form.password"
              type="password"
              show-password
              placeholder="至少 8 位"
              autocomplete="new-password"
              data-testid="admin-reset-password-input"
            />
          </el-form-item>
        </el-form>
        <el-alert
          type="warning"
          :closable="false"
          show-icon
          title="新密码是临时密码：管理员登录后要先设置新密码。这个账号现有的登录全部失效。"
        />
      </template>
      <div v-else data-testid="admin-reset-result">
        <el-alert type="success" :closable="false" show-icon :title="`已重置 ${target?.display_name} 的密码`" />
        <el-descriptions :column="1" border size="small" class="who">
          <el-descriptions-item label="企业代码">{{ tenant.code }}</el-descriptions-item>
          <el-descriptions-item label="用户名">{{ target?.username }}</el-descriptions-item>
          <el-descriptions-item v-if="result.temporary_password" label="临时密码">
            <span class="secret">
              <code data-testid="admin-reset-password">{{ result.temporary_password }}</code>
              <el-button size="small" data-testid="admin-reset-copy" @click="copy(result.temporary_password)">复制</el-button>
            </span>
          </el-descriptions-item>
        </el-descriptions>
        <p class="hint">
          请通过电话等可靠的渠道告知本人{{ result.temporary_password ? '，关闭后不能再查看临时密码' : '' }}。管理员用它登录后要先设置自己的新密码。</p>
      </div>
      <template #footer>
        <template v-if="!result">
          <el-button @click="resetOpen = false">取消</el-button>
          <el-button type="danger" :loading="saving" data-testid="admin-reset-submit" @click="submit">重置密码</el-button>
        </template>
        <el-button v-else type="primary" data-testid="admin-reset-done" @click="resetOpen = false">完成</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.hint {
  margin: 0 0 12px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.tag {
  margin-left: 6px;
}

.who {
  margin: 0 0 14px;
}

.secret {
  display: inline-flex;
  align-items: center;
  gap: 10px;
}

.secret code {
  padding: 2px 10px;
  border-radius: 6px;
  background: var(--el-fill-color-light);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 16px;
  letter-spacing: 1px;
  user-select: all;
}
</style>
