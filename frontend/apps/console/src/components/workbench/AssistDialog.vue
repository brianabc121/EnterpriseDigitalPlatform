<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, ref, watch } from 'vue'

import { api } from '../../api'
import { useWorkbenchStore } from '../../stores/workbench'

const props = defineProps<{ session: Schemas['SessionDetail'] | Schemas['SessionOut'] }>()
const visible = defineModel<boolean>({ required: true })

const wb = useWorkbenchStore()
const agents = ref<Schemas['TransferAgent'][]>([])
const staffId = ref('')
const saving = ref(false)

/** 已经在旁听或协助的同事不再列出。 */
const choices = computed(() => {
  const joined = new Set(
    'watchers' in props.session ? (props.session.watchers ?? []).map((w) => w.staff_id) : [],
  )
  return agents.value.filter((a) => !joined.has(a.staff_id))
})

watch(visible, async (open) => {
  if (!open) return
  staffId.value = ''
  const { data, error } = await api.GET('/api/v1/transfer-targets')
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  agents.value = data.agents
})

async function submit(): Promise<void> {
  if (!staffId.value) {
    ElMessage.warning('请选择同事')
    return
  }
  saving.value = true
  try {
    await wb.inviteAssist(props.session, staffId.value)
    ElMessage.success('已邀请，同事加入后可以一起回复客户')
    visible.value = false
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <el-dialog v-model="visible" title="邀请协助" width="420px" data-testid="assist-dialog">
    <el-form label-width="64px" @submit.prevent="submit">
      <el-form-item label="同事">
        <el-select
          v-model="staffId"
          placeholder="选择在线坐席"
          no-data-text="没有其他在线坐席"
          data-testid="assist-agent"
        >
          <el-option
            v-for="a in choices"
            :key="a.staff_id"
            :label="`${a.display_name}（${a.active_sessions}/${a.max_concurrency}）`"
            :value="a.staff_id"
          />
        </el-select>
      </el-form-item>
      <p class="hint">
        同事加入会话后可以看到聊天记录和客户资料，并直接回复客户；客户会看到"客服 X
        加入了会话"。会话仍由您负责，结束或交还 AI 时同事自动退出。
      </p>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" data-testid="assist-submit" @click="submit">
        邀请
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
