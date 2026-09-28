<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api } from '../../api'
import { useAuthStore } from '../../stores/auth'
import { useWorkbenchStore } from '../../stores/workbench'

const props = defineProps<{ session: Schemas['SessionOut'] }>()
const visible = defineModel<boolean>({ required: true })

const auth = useAuthStore()
const wb = useWorkbenchStore()
const targets = ref<Schemas['TransferTargets']>({ agents: [], groups: [] })
const saving = ref(false)
const form = reactive({
  kind: 'agent' as 'agent' | 'group',
  staffId: '',
  groupId: '',
  note: '',
  force: false,
  ownership: false,
})

const canForce = computed(() => auth.can('session:transfer_any'))
const canMoveOwner = computed(() => auth.can('customer:assign'))

async function load(): Promise<void> {
  const { data, error } = await api.GET('/api/v1/transfer-targets')
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  targets.value = data
}

watch(visible, (open) => {
  if (!open) return
  Object.assign(form, {
    kind: 'agent',
    staffId: '',
    groupId: '',
    note: '',
    force: false,
    ownership: false,
  })
  void load()
})

async function submit(): Promise<void> {
  const toAgent = form.kind === 'agent'
  if (toAgent ? !form.staffId : !form.groupId) {
    ElMessage.warning(toAgent ? '请选择坐席' : '请选择技能组')
    return
  }
  saving.value = true
  try {
    const result = await wb.requestTransfer(props.session, {
      to_staff_id: toAgent ? form.staffId : null,
      to_group_id: toAgent ? null : form.groupId,
      note: form.note.trim() || null,
      force: toAgent && form.force,
      transfer_ownership: toAgent && form.ownership,
    })
    ElMessage.success(result.status === 'pending' ? '已发送转接请求，等待对方接受' : '已转接')
    visible.value = false
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <el-dialog v-model="visible" title="转接会话" width="460px" data-testid="transfer-dialog">
    <el-form label-width="84px" @submit.prevent="submit">
      <el-form-item label="转给">
        <el-radio-group v-model="form.kind">
          <el-radio value="agent">坐席</el-radio>
          <el-radio value="group">技能组</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item v-if="form.kind === 'agent'" label="坐席">
        <el-select
          v-model="form.staffId"
          placeholder="选择在线坐席"
          no-data-text="没有其他在线坐席"
          data-testid="transfer-agent"
        >
          <el-option
            v-for="a in targets.agents"
            :key="a.staff_id"
            :label="`${a.display_name}（${a.active_sessions}/${a.max_concurrency}）`"
            :value="a.staff_id"
          />
        </el-select>
      </el-form-item>
      <el-form-item v-else label="技能组">
        <el-select v-model="form.groupId" placeholder="选择技能组" no-data-text="还没有技能组">
          <el-option v-for="g in targets.groups" :key="g.id" :label="g.name" :value="g.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="备注">
        <el-input
          v-model="form.note"
          type="textarea"
          :rows="2"
          maxlength="500"
          placeholder="对方接手时可以看到，例如客户的诉求"
        />
      </el-form-item>
      <el-form-item v-if="form.kind === 'agent' && (canForce || canMoveOwner)" label="选项">
        <el-checkbox v-if="canForce" v-model="form.force">强制转接（不需要对方确认）</el-checkbox>
        <el-checkbox v-if="canMoveOwner" v-model="form.ownership">同时转移客户归属</el-checkbox>
      </el-form-item>
      <p class="hint">
        转给坐席需要对方在 60 秒内接受，超时或拒绝时会话仍由您接待；转给技能组会重新排队分配。
      </p>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" data-testid="transfer-submit" @click="submit">
        转接
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin: 0;
}
</style>
