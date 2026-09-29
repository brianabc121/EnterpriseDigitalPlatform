<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'

import { api } from '../../api'

type Group = Schemas['SkillGroupOut']

const groups = ref<Group[]>([])
const staff = ref<Schemas['StaffOut'][]>([])
const loading = ref(false)
const dialogOpen = ref(false)
const saving = ref(false)
const editing = ref<Group | null>(null)
const form = reactive({ name: '', members: [] as string[], leads: [] as string[] })

async function load(): Promise<void> {
  loading.value = true
  const [g, s] = await Promise.all([api.GET('/api/v1/skill-groups'), api.GET('/api/v1/staff')])
  loading.value = false
  if (!g.data) {
    ElMessage.error(errorMessage(g.error))
    return
  }
  groups.value = g.data.items
  staff.value = (s.data?.items ?? []).filter((m) => m.status === 'active')
}

function openDialog(group: Group | null): void {
  editing.value = group
  form.name = group?.name ?? ''
  form.members = group?.members.map((m) => m.staff_id) ?? []
  form.leads = group?.members.filter((m) => m.is_lead).map((m) => m.staff_id) ?? []
  dialogOpen.value = true
}

function nameOf(id: string): string {
  return staff.value.find((m) => m.id === id)?.display_name ?? ''
}

async function save(): Promise<void> {
  const body = {
    name: form.name.trim(),
    members: form.members.map((id) => ({ staff_id: id, is_lead: form.leads.includes(id) })),
  }
  saving.value = true
  const { error } = editing.value
    ? await api.PATCH('/api/v1/skill-groups/{group_id}', {
        params: { path: { group_id: editing.value.id } },
        body,
      })
    : await api.POST('/api/v1/skill-groups', { body })
  saving.value = false
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已保存')
  dialogOpen.value = false
  await load()
}

async function remove(group: Group): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定删除技能组"${group.name}"吗？`, '删除技能组', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  const { error } = await api.DELETE('/api/v1/skill-groups/{group_id}', {
    params: { path: { group_id: group.id } },
  })
  if (error) ElMessage.error(errorMessage(error))
  await load()
}

onMounted(load)
</script>

<template>
  <div>
    <div class="toolbar">
      <span class="hint">
        路由策略可以把会话分给指定技能组；组长（主管角色）可以查看组内成员的会话和客户。
      </span>
      <el-button type="primary" data-testid="new-group" @click="openDialog(null)"
        >新建技能组</el-button
      >
    </div>
    <el-table
      v-loading="loading"
      :data="groups"
      data-testid="groups-table"
      empty-text="还没有技能组"
    >
      <el-table-column prop="name" label="名称" min-width="140" />
      <el-table-column label="成员" min-width="320">
        <template #default="{ row }">
          <el-tag
            v-for="m in row.members"
            :key="m.staff_id"
            size="small"
            :type="m.is_lead ? 'warning' : 'info'"
            class="member"
          >
            {{ m.display_name }}{{ m.is_lead ? '（组长）' : '' }}
          </el-tag>
          <span v-if="row.members.length === 0" class="hint">暂无成员</span>
        </template>
      </el-table-column>
      <el-table-column label="" width="120">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="openDialog(row)">编辑</el-button>
          <el-button link type="danger" size="small" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialogOpen" :title="editing ? '编辑技能组' : '新建技能组'" width="520px">
      <el-form label-width="72px" data-testid="group-form">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" maxlength="64" data-testid="group-name" />
        </el-form-item>
        <el-form-item label="成员">
          <el-select
            v-model="form.members"
            multiple
            filterable
            placeholder="选择员工"
            data-testid="group-members"
          >
            <el-option v-for="m in staff" :key="m.id" :label="m.display_name" :value="m.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="组长">
          <el-checkbox-group v-model="form.leads">
            <el-checkbox v-for="id in form.members" :key="id" :value="id">{{
              nameOf(id)
            }}</el-checkbox>
          </el-checkbox-group>
          <span v-if="form.members.length === 0" class="hint">先选择成员</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogOpen = false">取消</el-button>
        <el-button
          type="primary"
          :loading="saving"
          :disabled="!form.name.trim()"
          data-testid="save-group"
          @click="save"
        >
          保存
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.member {
  margin: 2px 6px 2px 0;
}
</style>
