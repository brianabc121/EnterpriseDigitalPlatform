<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { api, formatDateTime } from '../api'
import HandoverDialog from '../components/customers/HandoverDialog.vue'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const staff = ref<Schemas['StaffOut'][]>([])
const roles = ref<Schemas['RoleOut'][]>([])
const loading = ref(false)
const canManage = computed(() => auth.can('staff:manage'))
const canHandover = computed(() => auth.can('customer:assign'))
const handoverOpen = ref(false)
const handoverFrom = ref<Schemas['StaffOut'] | null>(null)

function openHandover(member: Schemas['StaffOut']): void {
  handoverFrom.value = member
  handoverOpen.value = true
}
const roleNames = computed(() => new Map(roles.value.map((r) => [r.code, r.name])))

const dialogVisible = ref(false)
const saving = ref(false)
const form = reactive({ username: '', displayName: '', password: '', roleCodes: ['agent'] })

async function load(): Promise<void> {
  loading.value = true
  const [staffResult, rolesResult] = await Promise.all([
    api.GET('/api/v1/staff'),
    api.GET('/api/v1/roles'),
  ])
  loading.value = false
  if (!staffResult.data || !rolesResult.data) {
    ElMessage.error(errorMessage(staffResult.error ?? rolesResult.error))
    return
  }
  staff.value = staffResult.data.items
  roles.value = rolesResult.data.items
}

function openCreate(): void {
  Object.assign(form, { username: '', displayName: '', password: '', roleCodes: ['agent'] })
  dialogVisible.value = true
}

async function create(): Promise<void> {
  saving.value = true
  const { data, error } = await api.POST('/api/v1/staff', {
    body: {
      username: form.username.trim(),
      display_name: form.displayName.trim(),
      password: form.password,
      role_codes: form.roleCodes,
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已创建员工')
  dialogVisible.value = false
  await load()
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-header">
      <h2>员工</h2>
      <el-button v-if="canManage" type="primary" @click="openCreate">新建员工</el-button>
    </div>
    <el-table v-loading="loading" :data="staff" data-testid="staff-table">
      <el-table-column prop="username" label="用户名" min-width="140" />
      <el-table-column prop="display_name" label="姓名" min-width="120" />
      <el-table-column label="角色" min-width="200">
        <template #default="{ row }">
          <el-tag v-for="code in row.roles" :key="code" class="role">{{ roleNames.get(code) ?? code }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="row.status === 'active' ? 'success' : 'info'">
            {{ row.status === 'active' ? '启用' : '停用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="200">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column v-if="canHandover" label="" width="110">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="openHandover(row)">
            交接客户
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <HandoverDialog v-model="handoverOpen" :from="handoverFrom" :staff="staff" />

    <el-dialog v-model="dialogVisible" title="新建员工" width="480px">
      <el-form label-width="84px" @submit.prevent="create">
        <el-form-item label="用户名" required>
          <el-input v-model="form.username" placeholder="3-64 位字母、数字、._-" />
        </el-form-item>
        <el-form-item label="姓名" required>
          <el-input v-model="form.displayName" />
        </el-form-item>
        <el-form-item label="初始密码" required>
          <el-input v-model="form.password" type="password" show-password placeholder="至少 8 位" />
        </el-form-item>
        <el-form-item label="角色" required>
          <el-checkbox-group v-model="form.roleCodes">
            <el-checkbox v-for="r in roles" :key="r.code" :value="r.code">{{ r.name }}</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="create">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.role + .role {
  margin-left: 6px;
}
</style>
