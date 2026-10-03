<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { api, formatDateTime } from '../api'
import PasswordDialog from '../components/account/PasswordDialog.vue'
import HandoverDialog from '../components/customers/HandoverDialog.vue'
import ResetPasswordDialog from '../components/staff/ResetPasswordDialog.vue'
import RolesTab from '../components/staff/RolesTab.vue'
import StaffAccessEditor from '../components/staff/StaffAccessEditor.vue'
import StaffTree from '../components/staff/StaffTree.vue'
import { accessBody, accessOf, accessSummary, emptyAccess, type AccessForm } from '../staffAccess'
import { assignableRoles, manageAccess, manageHint } from '../staffManage'
import type { DiagramDirection } from '../staffDiagram'
import { normalizeRoleName } from '../roleNames'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const tab = ref('staff')
const staff = ref<Schemas['StaffOut'][]>([])
const diagramNodes = ref<Schemas['StaffDiagramNodeOut'][]>([])
const draftId = ref<string | null>(null)
const adding = ref(false)
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
/** 分配角色时可以选的角色：企业所有者只能由平台创建，不显示（§39.5）。 */
const assignable = computed(() => assignableRoles(roles.value))
/** 权限点的名称和分组（"页面和权限"里按分组勾选，§31）。 */
const catalog = ref<Schemas['PermissionInfo'][]>([])
const permissionNames = computed(() => new Map(catalog.value.map((p) => [p.code, p.name])))

const branch = ref<{ parentId: string | null; direction: DiagramDirection; name: string } | null>(null)
const focusId = ref<string | null>(null)
const directionNames = { left: '左侧', right: '右侧', down: '下方' }
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

// 重置密码（§38.4）：全部角色都可以；自己的卡片上是修改密码（要输入当前密码）。
const resetOpen = ref(false)
const resetting = ref<Schemas['StaffOut'] | null>(null)
const passwordOpen = ref(false)

// 编辑、重置密码、停用/启用、删除卡片（§38.4、§39.1、§39.5）：权限高于自己的员工按钮置灰并提示，和后端的
// 规则一致；自己的卡片不能停用（不显示）和删除（置灰）；企业所有者的卡片（最顶部）没有停用和删除，只有
// 本人能编辑。
function manageState(member: Schemas['StaffOut']) {
  return manageAccess(member, { id: auth.me?.id ?? '', permissions: auth.permissions })
}

function deleteHint(member: Schemas['StaffOut']): string | null {
  return manageHint(manageState(member), '删除')
}

async function load(): Promise<void> {
  loading.value = true
  const [staffResult, rolesResult, catalogResult, nodesResult] = await Promise.all([
    api.GET('/api/v1/staff'),
    api.GET('/api/v1/roles'),
    api.GET('/api/v1/permissions'),
    api.GET('/api/v1/staff/diagram/nodes'),
  ])
  loading.value = false
  if (!staffResult.data || !rolesResult.data || !nodesResult.data) {
    ElMessage.error(errorMessage(staffResult.error ?? rolesResult.error ?? nodesResult.error))
    return
  }
  diagramNodes.value = nodesResult.data.items
  staff.value = staffResult.data.items
  roles.value = rolesResult.data.items.map(normalizeRoleName)
  if (catalogResult.data) catalog.value = catalogResult.data.items
}

function openCreate(): void {
  branch.value = null
  draftId.value = null
  Object.assign(form, { username: '', displayName: '', password: '', roleCodes: ['agent'] })
  createAccess.value = emptyAccess()
  dialogVisible.value = true
}

async function openBranch(parentId: string | null, direction: DiagramDirection): Promise<void> {
  if (adding.value || !canManage.value) return
  adding.value = true
  try {
    const { data, error } = await api.POST('/api/v1/staff/diagram/nodes', { body: { parent_id: parentId, direction } })
    if (!data) { ElMessage.error(errorMessage(error)); return }
    diagramNodes.value.push(data)
    focusId.value = data.id
  } catch {
    ElMessage.error('新增卡片失败，请检查连接后重试')
  } finally { adding.value = false }
}

async function deleteCard(cardId: string): Promise<void> {
  if (!canManage.value || adding.value || cardId === 'company') return
  const node = diagramNodes.value.find((item) => item.id === cardId)
  const member = staff.value.find((item) => item.id === (node?.staff_id ?? cardId))
  if (member && deleteHint(member)) return
  try {
    await ElMessageBox.confirm(member
      ? `确定删除 ${member.display_name} 的卡片和员工账号？该账号将无法登录。子卡片将接到上一级。`
      : '确定删除这张待完善卡片？子卡片将接到上一级。', '删除卡片', {
      type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消',
    })
  } catch { return }
  adding.value = true
  try {
    const { error } = await api.DELETE('/api/v1/staff/diagram/nodes/{card_id}', {
      params: { path: { card_id: cardId } },
    })
    if (error) { ElMessage.error(errorMessage(error)); return }
    ElMessage.success('卡片已删除')
    await load()
  } catch { ElMessage.error('删除失败，请检查连接后重试') }
  finally { adding.value = false }
}

function openDraft(nodeId: string): void {
  const node = diagramNodes.value.find((item) => item.id === nodeId)
  if (!node || node.staff_id) return
  openCreate()
  draftId.value = nodeId
  const parentNode = diagramNodes.value.find((item) => item.id === node.parent_id)
  const member = staff.value.find((item) => item.id === (parentNode?.staff_id ?? node.parent_id))
  branch.value = { parentId: node.parent_id, direction: node.direction, name: node.parent_id ? member?.display_name ?? '待完善员工' : auth.me?.tenant.name ?? '企业' }
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
  if (saving.value) return
  if (!form.username.trim() || !form.displayName.trim() || form.password.length < 8 || !form.roleCodes.length) {
    ElMessage.warning('请填写账号、姓名、至少 8 位密码，并选择角色')
    return
  }
  if (!accessReady(createAccess.value)) return
  saving.value = true
  const { data, error } = await api.POST('/api/v1/staff', {
    body: {
      username: form.username.trim(),
      display_name: form.displayName.trim(),
      password: form.password,
      role_codes: form.roleCodes,
      access: accessBody(createAccess.value),
      diagram_node_id: draftId.value,
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  focusId.value = draftId.value ?? data.id
  ElMessage.success('已创建员工')
  dialogVisible.value = false
  await load()
}

function openEdit(member: Schemas['StaffOut']): void {
  if (manageState(member) === 'owner') return
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
  resetting.value = member
  resetOpen.value = true
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
        <p class="hint">鼠标移到卡片边缘，点击“＋”新增卡片；再点击新卡片完善员工资料、角色和权限。连线不影响权限。</p>
        <StaffTree v-loading="loading" :staff="staff" :company="auth.me?.tenant.name ?? '企业'" :can-manage="canManage" :focus-id="focusId" :diagram-nodes="diagramNodes" :adding="adding" :delete-hint="deleteHint" data-testid="staff-tree" @add-branch="openBranch" @edit-draft="openDraft" @edit-staff="openEdit" @delete-card="deleteCard">
          <template #default="{ member: row }">
            <div class="staff-heading">
              <strong class="staff-name">{{ row.roles.includes('tenant_admin') ? '企业所有者' : row.roles.map((code) => roleNames.get(code) ?? code).join(' / ') || '员工' }}（{{ row.display_name }}）</strong>
              <span class="staff-states">
                <el-tag :type="row.status === 'active' ? 'success' : 'info'" size="small" disable-transitions>
                  {{ row.status === 'active' ? '启用' : '停用' }}
                </el-tag>
                <el-tooltip
                  v-if="row.must_change_password"
                  placement="top"
                  :content="`密码重置于 ${row.password_changed_at ? formatDateTime(row.password_changed_at) : '—'}，员工下次登录时要先设置新密码`"
                >
                  <el-tag type="warning" size="small" disable-transitions :data-testid="`must-change-${row.username}`">待改密码</el-tag>
                </el-tooltip>
              </span>
            </div>
            <div class="staff-username">{{ row.username }}</div>
            <div class="staff-tags">
              <el-tag v-for="code in row.roles" :key="code" size="small" disable-transitions>
                {{ roleNames.get(code) ?? code }}
              </el-tag>
              <el-tooltip v-if="row.access" placement="top">
                <template #content>
                  <div v-for="line in accessSummary(row, permissionNames)" :key="line">{{ line }}</div>
                </template>
                <el-tag type="warning" size="small" :data-testid="`custom-access-${row.username}`">自定义</el-tag>
              </el-tooltip>
            </div>
            <div class="staff-created">创建于 {{ formatDateTime(row.created_at) }}</div>
            <div v-if="canManage || canHandover" class="staff-actions">
              <template v-if="canManage">
                <el-tooltip :disabled="manageState(row) !== 'owner'" :content="manageHint('owner', '编辑') ?? ''" placement="top">
                  <span class="action">
                    <el-button link type="primary" size="small" :disabled="manageState(row) === 'owner'" :data-testid="`edit-${row.username}`" @click.stop="openEdit(row)">编辑</el-button>
                  </span>
                </el-tooltip>
                <el-button v-if="manageState(row) === 'self'" link type="primary" size="small" :data-testid="`password-${row.username}`" @click.stop="passwordOpen = true">修改密码</el-button>
                <el-tooltip v-else :disabled="manageState(row) === 'ok'" :content="manageHint(manageState(row), '重置') ?? ''" placement="top">
                  <span class="action">
                    <el-button link type="primary" size="small" :disabled="manageState(row) !== 'ok'" :data-testid="`reset-${row.username}`" @click.stop="openReset(row)">重置密码</el-button>
                  </span>
                </el-tooltip>
                <el-tooltip v-if="!row.is_owner && manageState(row) !== 'self'" :disabled="manageState(row) === 'ok'" :content="manageHint(manageState(row), row.status === 'active' ? '停用' : '启用') ?? ''" placement="top">
                  <span class="action">
                    <el-button link :type="row.status === 'active' ? 'danger' : 'primary'" size="small" :disabled="manageState(row) !== 'ok'" :data-testid="`toggle-${row.username}`" @click.stop="toggleStatus(row)">
                      {{ row.status === 'active' ? '停用' : '启用' }}
                    </el-button>
                  </span>
                </el-tooltip>
              </template>
              <el-button v-if="canHandover" link type="primary" size="small" @click.stop="openHandover(row)">交接客户</el-button>
            </div>
          </template>
        </StaffTree>
      </el-tab-pane>
      <el-tab-pane label="角色" name="roles" lazy>
        <RolesTab @changed="load" />
      </el-tab-pane>
    </el-tabs>
    <HandoverDialog v-model="handoverOpen" :from="handoverFrom" :staff="staff" />

    <el-dialog
      v-model="dialogVisible"
      :title="draftId ? '完善员工信息' : '新建员工'"
      width="min(860px, 96vw)"
      top="5vh"
      class="scroll-dialog"
      data-testid="staff-create"
    >
      <p v-if="branch" class="hint" data-testid="branch-context">在“{{ branch.name }}”{{ directionNames[branch.direction] }}的待完善卡片；保存后创建员工账号，角色和权限请独立设置。</p>
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
            <el-checkbox v-for="r in assignable" :key="r.code" :value="r.code" border>
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
          <p v-if="editing?.is_owner" class="owner-role" data-testid="owner-role-fixed">企业所有者（由平台在开通企业时创建，角色不能修改）</p>
          <el-checkbox-group v-else v-model="editForm.roleCodes" class="roles">
            <el-checkbox v-for="r in assignable" :key="r.code" :value="r.code" border>
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

    <ResetPasswordDialog v-model="resetOpen" :member="resetting" @done="load" />
    <PasswordDialog v-model="passwordOpen" />
  </div>
</template>

<style scoped>
.staff-heading { display: flex; flex-direction: column; align-items: flex-start; gap: 10px; }
.staff-name { font-size: 15px; line-height: 1.5; overflow-wrap: anywhere; }
.staff-username { color: var(--el-text-color-secondary); font-size: 13px; margin-top: 8px; overflow-wrap: anywhere; }
.staff-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
.staff-created { color: var(--el-text-color-secondary); font-size: 12px; margin-top: 12px; }
.staff-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; border-top: 1px solid var(--el-border-color-lighter); padding-top: 12px; }
.staff-actions .el-button { margin-left: 0; }
/* 状态和"待改密码"并排；置灰的按钮外面套一层，悬停时才能显示提示。 */
.staff-states { display: inline-flex; flex-wrap: wrap; gap: 6px; }
.action { display: inline-flex; }
.owner-role { margin: 0; color: var(--el-text-color-regular); }

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
