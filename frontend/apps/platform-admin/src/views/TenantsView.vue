<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'

import { api, formatDateTime } from '../api'

type Tenant = Schemas['TenantOut']

const tenants = ref<Tenant[]>([])
const loading = ref(false)
const dialogVisible = ref(false)
const saving = ref(false)
const form = reactive({
  code: '',
  name: '',
  adminUsername: 'admin',
  adminDisplayName: '管理员',
  adminPassword: '',
})

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/platform/v1/tenants')
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  tenants.value = data.items
}

function openCreate(): void {
  Object.assign(form, {
    code: '',
    name: '',
    adminUsername: 'admin',
    adminDisplayName: '管理员',
    adminPassword: '',
  })
  dialogVisible.value = true
}

async function create(): Promise<void> {
  saving.value = true
  const { data, error } = await api.POST('/platform/v1/tenants', {
    body: {
      code: form.code.trim(),
      name: form.name.trim(),
      admin: {
        username: form.adminUsername.trim(),
        display_name: form.adminDisplayName.trim(),
        password: form.adminPassword,
      },
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(`已开通：企业代码 ${data.code}，管理员 ${form.adminUsername}`)
  dialogVisible.value = false
  await load()
}

async function toggleStatus(tenant: Tenant): Promise<void> {
  const suspending = tenant.status === 'active'
  if (suspending) {
    const confirmed = await ElMessageBox.confirm(
      `停用后「${tenant.name}」的所有员工将无法登录，已登录的会话也会失效。`,
      '停用租户',
      { type: 'warning', confirmButtonText: '停用', cancelButtonText: '取消' },
    ).catch(() => false)
    if (!confirmed) return
  }
  const { data, error } = await api.PATCH('/platform/v1/tenants/{tenant_id}', {
    params: { path: { tenant_id: tenant.id } },
    body: { status: suspending ? 'suspended' : 'active' },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  await load()
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-header">
      <h2>租户</h2>
      <el-button type="primary" @click="openCreate">开通租户</el-button>
    </div>
    <el-table v-loading="loading" :data="tenants" data-testid="tenant-table" empty-text="暂无租户">
      <el-table-column prop="code" label="企业代码" width="160" />
      <el-table-column prop="name" label="企业名称" min-width="200" />
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="row.status === 'active' ? 'success' : 'danger'">
            {{ row.status === 'active' ? '正常' : '已停用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="开通时间" width="200">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="120">
        <template #default="{ row }">
          <el-button link :type="row.status === 'active' ? 'danger' : 'primary'" @click="toggleStatus(row)">
            {{ row.status === 'active' ? '停用' : '启用' }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialogVisible" title="开通租户" width="480px">
      <el-form label-width="96px" @submit.prevent="create">
        <el-form-item label="企业代码" required>
          <el-input v-model="form.code" placeholder="小写字母开头，3-32 位，可含数字和 -" />
        </el-form-item>
        <el-form-item label="企业名称" required>
          <el-input v-model="form.name" />
        </el-form-item>
        <el-divider content-position="left">首个管理员</el-divider>
        <el-form-item label="用户名" required>
          <el-input v-model="form.adminUsername" />
        </el-form-item>
        <el-form-item label="姓名" required>
          <el-input v-model="form.adminDisplayName" />
        </el-form-item>
        <el-form-item label="初始密码" required>
          <el-input v-model="form.adminPassword" type="password" show-password placeholder="至少 8 位" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="create">开通</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.page-header h2 {
  margin: 0;
  font-size: 18px;
}
</style>
