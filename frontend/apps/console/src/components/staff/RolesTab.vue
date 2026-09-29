<script setup lang="ts">
import { errorMessage, type Permission, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { api } from '../../api'
import { useAuthStore } from '../../stores/auth'

const emit = defineEmits<{ changed: [] }>()

const auth = useAuthStore()
const canManage = computed(() => auth.can('staff:manage'))
const roles = ref<Schemas['RoleOut'][]>([])
const catalog = ref<Schemas['PermissionInfo'][]>([])
const loading = ref(false)

const names = computed(() => new Map(catalog.value.map((p) => [p.code, p.name])))
const groups = computed(() => {
  const byGroup = new Map<string, Schemas['PermissionInfo'][]>()
  for (const item of catalog.value) {
    byGroup.set(item.group, [...(byGroup.get(item.group) ?? []), item])
  }
  return [...byGroup.entries()]
})

const dialogOpen = ref(false)
const saving = ref(false)
const editing = ref<Schemas['RoleOut'] | null>(null)
const form = reactive<{ code: string; name: string; permissions: Permission[] }>({
  code: '',
  name: '',
  permissions: [],
})

async function load(): Promise<void> {
  loading.value = true
  const [rolesResult, catalogResult] = await Promise.all([
    api.GET('/api/v1/roles'),
    api.GET('/api/v1/permissions'),
  ])
  loading.value = false
  if (!rolesResult.data || !catalogResult.data) {
    ElMessage.error(errorMessage(rolesResult.error ?? catalogResult.error))
    return
  }
  roles.value = rolesResult.data.items
  catalog.value = catalogResult.data.items
}

function openEditor(role: Schemas['RoleOut'] | null): void {
  editing.value = role
  Object.assign(form, {
    code: role?.code ?? '',
    name: role?.name ?? '',
    permissions: [...((role?.permissions ?? []) as Permission[])],
  })
  dialogOpen.value = true
}

/** 只能授予自己拥有的权限。 */
function grantable(code: Permission): boolean {
  return auth.can(code)
}

async function save(): Promise<void> {
  if (!form.name.trim() || form.permissions.length === 0) {
    ElMessage.warning('请填写名称并至少选择一项权限')
    return
  }
  saving.value = true
  const result = editing.value
    ? await api.PATCH('/api/v1/roles/{role_id}', {
        params: { path: { role_id: editing.value.id } },
        body: { name: form.name.trim(), permissions: form.permissions },
      })
    : await api.POST('/api/v1/roles', {
        body: { code: form.code.trim(), name: form.name.trim(), permissions: form.permissions },
      })
  saving.value = false
  if (!result.data) {
    ElMessage.error(errorMessage(result.error))
    return
  }
  ElMessage.success('已保存角色')
  dialogOpen.value = false
  await load()
  emit('changed')
}

async function remove(role: Schemas['RoleOut']): Promise<void> {
  try {
    await ElMessageBox.confirm(`删除角色"${role.name}"？`, '删除角色', { type: 'warning' })
  } catch {
    return
  }
  const { error, response } = await api.DELETE('/api/v1/roles/{role_id}', {
    params: { path: { role_id: role.id } },
  })
  if (!response.ok) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已删除角色')
  await load()
  emit('changed')
}

onMounted(load)
defineExpose({ load })
</script>

<template>
  <div>
    <div class="toolbar">
      <span class="hint">系统角色随版本更新；自定义角色的权限不能超出你自己拥有的权限。</span>
      <el-button v-if="canManage" type="primary" data-testid="new-role" @click="openEditor(null)">
        新建角色
      </el-button>
    </div>
    <el-table v-loading="loading" :data="roles" data-testid="role-table">
      <el-table-column label="角色" width="180">
        <template #default="{ row }">
          {{ row.name }}
          <el-tag v-if="row.is_system" size="small" type="info" disable-transitions>系统</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="code" label="代码" width="160" />
      <el-table-column label="权限" min-width="320">
        <template #default="{ row }">
          <el-tag
            v-for="p in row.permissions"
            :key="p"
            size="small"
            class="perm"
            disable-transitions
          >
            {{ names.get(p) ?? p }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="members" label="员工数" width="90" />
      <el-table-column v-if="canManage" label="" width="120">
        <template #default="{ row }">
          <template v-if="!row.is_system">
            <el-button link type="primary" size="small" @click="openEditor(row)">编辑</el-button>
            <el-button link type="danger" size="small" @click="remove(row)">删除</el-button>
          </template>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog
      v-model="dialogOpen"
      :title="editing ? '编辑角色' : '新建角色'"
      width="640px"
      data-testid="role-dialog"
    >
      <el-form label-width="72px" @submit.prevent="save">
        <el-form-item label="代码" required>
          <el-input
            v-model="form.code"
            :disabled="!!editing"
            placeholder="小写字母开头，如 quality"
          />
        </el-form-item>
        <el-form-item label="名称" required>
          <el-input v-model="form.name" maxlength="64" />
        </el-form-item>
        <el-form-item label="权限" required>
          <el-checkbox-group v-model="form.permissions" class="perms">
            <div v-for="[group, items] in groups" :key="group" class="group">
              <div class="group-name">{{ group }}</div>
              <el-checkbox
                v-for="item in items"
                :key="item.code"
                :value="item.code"
                :disabled="!grantable(item.code)"
              >
                {{ item.name }}
              </el-checkbox>
            </div>
          </el-checkbox-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogOpen = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.hint {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.perm {
  margin: 2px 4px 2px 0;
}

.perms {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.group-name {
  font-weight: 500;
  color: var(--el-text-color-regular);
  line-height: 24px;
}
</style>
