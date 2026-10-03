<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { reactive, ref, watch } from 'vue'

import { api } from '../../api'
import type { LostReason, Opportunity, OpportunitySummary } from '../../opportunities'

/**
 * 输单（设计文档 §40.5）：必须选原因分类（商机设置里的，默认价格、竞品、需求消失、没有回应、其他），可以写
 * 说明；输单后 30 天内 AI 不再建议这个客户。看板拖到"输单"列和详情里点"输单"都用这个对话框。
 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{ opportunity: Pick<OpportunitySummary, 'id' | 'name'> | null }>()
const emit = defineEmits<{ done: [opportunity: Opportunity] }>()

const reasons = ref<LostReason[]>([])
const form = reactive({ code: '', reason: '' })
const saving = ref(false)

async function load(): Promise<void> {
  Object.assign(form, { code: '', reason: '' })
  if (reasons.value.length === 0) {
    const { data } = await api.GET('/api/v1/opportunities/settings')
    reasons.value = data?.settings.lost_reasons ?? [{ code: 'other', name: '其他' }]
  }
  form.code = reasons.value[0]?.code ?? 'other'
}

async function confirm(): Promise<void> {
  if (!props.opportunity) return
  if (!form.code) {
    ElMessage.warning('请选择输单的原因')
    return
  }
  saving.value = true
  const { data, error } = await api.POST('/api/v1/opportunities/{opportunity_id}/lost', {
    params: { path: { opportunity_id: props.opportunity.id } },
    body: { reason_code: form.code, reason: form.reason.trim() || null },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已输单，30 天内 AI 不再建议这位客户')
  open.value = false
  emit('done', data)
}

watch(open, (value) => {
  if (value) void load()
})
</script>

<template>
  <el-dialog
    v-model="open"
    :title="opportunity ? `输单：${opportunity.name}` : '输单'"
    width="480px"
    append-to-body
    data-testid="opp-lost-dialog"
  >
    <el-form label-width="84px" @submit.prevent="confirm">
      <el-form-item label="原因" required>
        <el-radio-group v-model="form.code" data-testid="opp-lost-code">
          <el-radio-button v-for="reason in reasons" :key="reason.code" :value="reason.code">
            {{ reason.name }}
          </el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="说明">
        <el-input
          v-model="form.reason"
          type="textarea"
          :rows="3"
          maxlength="500"
          placeholder="例如：已经买了别家的；方便以后再跟进时参考"
          data-testid="opp-lost-text"
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button type="danger" plain :loading="saving" data-testid="opp-lost-confirm" @click="confirm">
        输单
      </el-button>
    </template>
  </el-dialog>
</template>
