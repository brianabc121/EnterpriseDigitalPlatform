<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { api, formatDateTime } from '../api'
import HandoverDialog from '../components/customers/HandoverDialog.vue'
import RolesTab from '../components/staff/RolesTab.vue'
import StaffAccessEditor from '../components/staff/StaffAccessEditor.vue'
import { accessBody, accessOf, accessSummary, emptyAccess, type AccessForm } from '../staffAccess'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const tab = ref('staff')
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
/** 权限点的名称和分组（"页面和权限"里按分组勾选，§31）。 */
const catalog = ref<Schemas['PermissionInfo'][]>([])
const permissionNames = computed(() => new Map(catalog.value.map((p) => [p.code, p.name])))

const dialogVisible = ref(false)
const saving = ref(false)
const form = reactive({ username: '', displayName: '', password: '', roleCodes: ['agent'] })
const createAccess = ref<AccessForm>(emptyAccess())

const editOpen = ref(false)
const editing = ref<Schemas['StaffOut'] | null>(null)
const editForm = reactive<{ displayName: string; roleCodes: string[] }>({
  displayName: '',
  roleCodes: [],
})
const editAccess = ref<AccessForm>(emptyAccess())

const resetOpen = ref(false)
const resetPassword = ref('')

async function load(): Promise<void> {
  loading.value = true
  const [staffResult, rolesResult, catalogResult] = await Promise.all([
    api.GET('/api/v1/staff'),
    api.GET('/api/v1/roles'),
    api.GET('/api/v1/permissions'),
  ])
  loading.value = false
  if (!staffResult.data || !rolesResult.data) {
    ElMessage.error(errorMessage(staffResult.error ?? rolesResult.error))
    return
  }
  staff.value = staffResult.data.items
  roles.value = rolesResult.data.items
  if (catalogResult.data) catalog.value = catalogResult.data.items
}

function openCreate(): void {
  Object.assign(form, { username: '', displayName: '', password: '', roleCodes: ['agent'] })
  createAccess.value = emptyAccess()
  dialogVisible.value = true
}

/** 自定义时至少要勾一个页面。 */
function accessReady(access: AccessForm): boolean {
  if (access.mode === 'custom' && access.menus.length === 0) {
    ElMessage.warning('自定义时请至少勾选一个页面')
    return false
  }
  return true
}

async function create(): Promise<void> {
  if (!accessReady(createAccess.value)) return
  saving.value = true
  const { data, error } = await api.POST('/api/v1/staff', {
    body: {
      username: form.username.trim(),
      display_name: form.displayName.trim(),
      password: form.password,
      role_codes: form.roleCodes,
      access: accessBody(createAccess.value),
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

function openEdit(member: Schemas['StaffOut']): void {
  editing.value = member
  Object.assign(editForm, { displayName: member.display_name, roleCodes: [...member.roles] })
  editAccess.value = accessOf(member)
  editOpen.value = true
}

async function patch(
  member: Schemas['StaffOut'],
  body: Schemas['StaffUpdate'],
  done: string,
): Promise<boolean> {
  const { data, error } = await api.PATCH('/api/v1/staff/{staff_id}', {
    params: { path: { staff_id: member.id } },
    body,
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return false
  }
  ElMessage.success(done)
  await load()
  return true
}

async function saveEdit(): Promise<void> {
  if (!editing.value) return
  if (!editForm.displayName.trim() || editForm.roleCodes.length === 0) {
    ElMessage.warning('请填写姓名并至少选择一个角色')
    return
  }
  if (!accessReady(editAccess.value)) return
  saving.value = true
  const ok = await patch(
    editing.value,
    {
      display_name: editForm.displayName.trim(),
      role_codes: editForm.roleCodes,
      access: accessBody(editAccess.value),
    },
    '已保存',
  )
  saving.value = false
  if (ok) editOpen.value = false
}

async function toggleStatus(member: Schemas['StaffOut']): Promise<void> {
  const disabling = member.status === 'active'
  if (disabling) {
    try {
      await ElMessageBox.confirm(
        `停用后 ${member.display_name} 立即退出登录并下线，接待中的会话退回队列。名下客户请另行交接。`,
        '停用员工',
        { type: 'warning', confirmButtonText: '停用' },
      )
    } catch {
      return
    }
  }
  await patch(
    member,
    { status: disabling ? 'disabled' : 'active' },
    disabling ? '已停用' : '已启用',
  )
}

function openReset(member: Schemas['StaffOut']): void {
  editing.value = member
  resetPassword.value = ''
  resetOpen.value = true
}

async function saveReset(): Promise<void> {
  if (!editing.value) return
  if (resetPassword.value.length < 8) {
    ElMessage.warning('新密码至少 8 位')
    return
  }
  saving.value = true
  const { error, response } = await api.POST('/api/v1/staff/{staff_id}/password', {
    params: { path: { staff_id: editing.value.id } },
    body: { password: resetPassword.value },
  })
  saving.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已重置密码，员工需要用新密码重新登录')
  resetOpen.value = false
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-header">
      <h2>员工</h2>
      <el-button v-if="canManage && tab === 'staff'" type="primary" @click="openCreate">
        新建员工
      </el-button>
    </div>
    <el-tabs v-model="tab">
      <el-tab-pane label="员工" name="staff">
        <el-table v-loading="loading" :data="staff" data-testid="staff-table">
          <el-table-column prop="username" label="用户名" min-width="140" />
          <el-table-column prop="display_name" label="姓名" min-width="120" />
          <el-table-column label="角色" min-width="200">
            <template #default="{ row }">
              <el-tag v-for="code in row.roles" :key="code" class="role" disable-transitions>
                {{ roleNames.get(code) ?? code }}
              </el-tag>
              <el-tooltip v-if="row.access" placement="top">
                <template #content>
                  <div v-for="line in accessSummary(row, permissionNames)" :key="line">
                    {{ line }}
                  </div>
                </template>
                <el-tag
                  type="warning"
                  class="role"
                  disable-transitions
                  :data-testid="`custom-access-${row.username}`"
                  >自定义</el-tag
                >
              </el-tooltip>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag :type="row.status === 'active' ? 'success' : 'info'" disable-transitions>
                {{ row.status === 'active' ? '启用' : '停用' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="创建时间" width="180">
            <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column v-if="canManage || canHandover" label="" width="260">
            <template #default="{ row }">
              <template v-if="canManage">
                <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
                <el-button link type="primary" size="small" @click="openReset(row)">
                  重置密码
                </el-button>
                <el-button
                  v-if="row.id !== auth.me?.id"
                  link
                  :type="row.status === 'active' ? 'danger' : 'primary'"
                  size="small"
                  :data-testid="`toggle-${row.username}`"
                  @click="toggleStatus(row)"
                >
                  {{ row.status === 'active' ? '停用' : '启用' }}
                </el-button>
              </template>
              <el-button
                v-if="canHandover"
                link
                type="primary"
                size="small"
                @click="openHandover(row)"
              >
                交接客户
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
      <el-tab-pane label="角色" name="roles" lazy>
        <RolesTab @changed="load" />
      </el-tab-pane>
    </el-tabs>
    <HandoverDialog v-model="handoverOpen" :from="handoverFrom" :staff="staff" />

    <el-dialog
      v-model="dialogVisible"
      title="新建员工"
      width="min(860px, 96vw)"
      top="5vh"
      class="scroll-dialog"
      data-testid="staff-create"
    >
      <el-form label-position="top" @submit.prevent="create">
        <div class="fields">
          <el-form-item label="用户名" required>
            <el-input v-model="form.username" placeholder="3-64 位字母、数字、._-" />
          </el-form-item>
          <el-form-item label="姓名" required>
            <el-input v-model="form.displayName" />
          </el-form-item>
          <el-form-item label="初始密码" required>
            <el-input
              v-model="form.password"
              type="password"
              show-password
              placeholder="至少 8 位"
            />
          </el-form-item>
        </div>
        <el-form-item label="角色" required>
          <el-checkbox-group v-model="form.roleCodes" class="roles">
            <el-checkbox v-for="r in roles" :key="r.code" :value="r.code" border>
              {{ r.name }}
            </el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="页面和权限">
          <StaffAccessEditor
            v-if="dialogVisible"
            v-model="createAccess"
            :role-codes="form.roleCodes"
            :catalog="catalog"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="create">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="editOpen"
      title="编辑员工"
      width="min(860px, 96vw)"
      top="5vh"
      class="scroll-dialog"
      data-testid="staff-edit"
    >
      <el-form label-position="top" @submit.prevent="saveEdit">
        <div class="fields">
          <el-form-item label="用户名">
            <el-input :model-value="editing?.username" disabled />
          </el-form-item>
          <el-form-item label="姓名" required>
            <el-input v-model="editForm.displayName" maxlength="64" />
          </el-form-item>
        </div>
        <el-form-item label="角色" required>
          <el-checkbox-group v-model="editForm.roleCodes" class="roles">
            <el-checkbox v-for="r in roles" :key="r.code" :value="r.code" border>
              {{ r.name }}
            </el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="页面和权限">
          <StaffAccessEditor
            v-if="editOpen"
            v-model="editAccess"
            :role-codes="editForm.roleCodes"
            :catalog="catalog"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editOpen = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="resetOpen" title="重置密码" width="420px" data-testid="staff-reset">
      <p class="hint">为 {{ editing?.display_name }} 设置新密码；员工现有的登录全部失效。</p>
      <el-input
        v-model="resetPassword"
        type="password"
        show-password
        placeholder="新密码，至少 8 位"
        autocomplete="new-password"
      />
      <template #footer>
        <el-button @click="resetOpen = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveReset">重置</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.role + .role {
  margin-left: 6px;
}

.hint {
  margin: 0 0 12px;
  color: var(--el-text-color-secondary);
}

.fields {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  column-gap: 16px;
}

/* 勾选框组的字号是 0；带边框的角色勾选框之间留出间距。 */
.roles {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.roles .el-checkbox {
  margin-right: 0;
}
</style>
