<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { reactive, ref, watch } from 'vue'

import { api } from '../../api'
import { transferSummary } from '../../wecom'

const props = defineProps<{ customerIds: string[] }>()
const visible = defineModel<boolean>({ required: true })
const emit = defineEmits<{ done: [] }>()

const staff = ref<Schemas['StaffOut'][]>([])
const saving = ref(false)
const form = reactive({ ownerId: '', note: '', syncWecom: false })

watch(visible, async (open) => {
  if (!open) return
  Object.assign(form, { ownerId: '', note: '', syncWecom: false })
  const { data } = await api.GET('/api/v1/staff')
  staff.value = data?.items.filter((s) => s.status === 'active') ?? []
})

async function submit(): Promise<void> {
  if (!form.ownerId) {
    ElMessage.warning('请选择新的归属坐席')
    return
  }
  saving.value = true
  const { data, error } = await api.POST('/api/v1/customers/transfer', {
    body: {
      customer_ids: props.customerIds,
      to_owner_id: form.ownerId,
      note: form.note || null,
      sync_wecom: form.syncWecom,
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(transferSummary(data.transferred, data.wecom))
  visible.value = false
  emit('done')
}
</script>

<template>
  <el-dialog v-model="visible" title="转移客户归属" width="440px">
    <p class="summary">已选择 {{ customerIds.length }} 位客户。转移后原坐席不再能看到这些客户。</p>
    <el-form label-width="84px" @submit.prevent="submit">
      <el-form-item label="新归属" required>
        <el-select v-model="form.ownerId" filterable placeholder="选择员工" data-testid="new-owner">
          <el-option v-for="s in staff" :key="s.id" :label="s.display_name" :value="s.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="说明">
        <el-input v-model="form.note" maxlength="500" placeholder="可选，例如调整负责区域" />
      </el-form-item>
      <el-form-item label="企业微信">
        <el-checkbox v-model="form.syncWecom" data-testid="sync-wecom">
          同时变更企业微信里的添加人（在职继承）
        </el-checkbox>
        <div class="hint">90 天内每位客户最多转接 2 次，客户 24 小时后自动接替</div>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="submit">转移</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.summary {
  margin-top: 0;
  color: var(--el-text-color-secondary);
}

.hint {
  font-size: 12px;
  line-height: 1.4;
  color: var(--el-text-color-secondary);
}
</style>
