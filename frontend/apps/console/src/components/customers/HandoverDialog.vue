<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { reactive, ref, watch } from 'vue'

import { api } from '../../api'
import { transferSummary } from '../../wecom'

const props = defineProps<{ from: Schemas['StaffOut'] | null; staff: Schemas['StaffOut'][] }>()
const visible = defineModel<boolean>({ required: true })

const groups = ref<Schemas['SkillGroupOut'][]>([])
const saving = ref(false)
const form = reactive({
  kind: 'staff' as 'staff' | 'group',
  ownerId: '',
  groupId: '',
  note: '',
  syncWecom: false,
  transferGroups: false,
})

watch(visible, async (open) => {
  if (!open) return
  Object.assign(form, {
    kind: 'staff',
    ownerId: '',
    groupId: '',
    note: '',
    syncWecom: false,
    transferGroups: false,
  })
  const { data } = await api.GET('/api/v1/skill-groups')
  groups.value = data?.items ?? []
})

async function submit(): Promise<void> {
  if (!props.from) return
  const toStaff = form.kind === 'staff'
  if (toStaff ? !form.ownerId : !form.groupId) {
    ElMessage.warning(toStaff ? '请选择接手的员工' : '请选择技能组')
    return
  }
  saving.value = true
  const { data, error } = await api.POST('/api/v1/customers/handover/{staff_id}', {
    params: { path: { staff_id: props.from.id } },
    body: {
      to_owner_id: toStaff ? form.ownerId : null,
      to_group_id: toStaff ? null : form.groupId,
      note: form.note || null,
      sync_wecom: form.syncWecom,
      transfer_groups: form.transferGroups,
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(transferSummary(data.transferred, data.wecom).replace('已转移', '已交接'))
  visible.value = false
}
</script>

<template>
  <el-dialog v-model="visible" :title="`交接客户 · ${from?.display_name ?? ''}`" width="460px">
    <p class="summary">把该员工名下的全部客户转给一位同事，或平均分给一个技能组的成员。</p>
    <el-form label-width="84px" @submit.prevent="submit">
      <el-form-item label="接手方">
        <el-radio-group v-model="form.kind">
          <el-radio value="staff">员工</el-radio>
          <el-radio value="group">技能组（平均分配）</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item v-if="form.kind === 'staff'" label="员工">
        <el-select v-model="form.ownerId" filterable placeholder="选择员工">
          <el-option
            v-for="s in staff.filter((s) => s.status === 'active' && s.id !== from?.id)"
            :key="s.id"
            :label="s.display_name"
            :value="s.id"
          />
        </el-select>
      </el-form-item>
      <el-form-item v-else label="技能组">
        <el-select v-model="form.groupId" placeholder="选择技能组" no-data-text="还没有技能组">
          <el-option v-for="g in groups" :key="g.id" :label="g.name" :value="g.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="说明">
        <el-input v-model="form.note" maxlength="500" placeholder="可选，例如离职交接" />
      </el-form-item>
      <el-form-item label="企业微信">
        <div class="checks">
          <el-checkbox v-model="form.syncWecom" data-testid="handover-sync-wecom">
            同时变更企业微信里的添加人（已离职的成员走离职继承）
          </el-checkbox>
          <el-checkbox v-model="form.transferGroups" data-testid="handover-groups">
            同时把他作为群主的客户群转给接手的员工
          </el-checkbox>
        </div>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="submit">交接</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.checks {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
}

.summary {
  margin-top: 0;
  color: var(--el-text-color-secondary);
}
</style>
