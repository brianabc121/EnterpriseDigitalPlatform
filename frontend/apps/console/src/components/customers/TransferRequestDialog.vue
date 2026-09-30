<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { reactive, ref, watch } from 'vue'

import { api } from '../../api'
import { useAuthStore } from '../../stores/auth'

const props = defineProps<{ customer: Schemas['CustomerOut'] | null }>()
const visible = defineModel<boolean>({ required: true })
const emit = defineEmits<{ done: [] }>()

const auth = useAuthStore()
const colleagues = ref<Schemas['TransferAgent'][]>([])
const saving = ref(false)
const form = reactive({ to: 'me' as 'me' | 'colleague', staffId: '', reason: '' })

watch(visible, async (open) => {
  if (!open) return
  const mine = props.customer?.owner_id === auth.me?.id
  Object.assign(form, { to: mine ? 'colleague' : 'me', staffId: '', reason: '' })
  colleagues.value = []
  // 坐席只能看到在线同事的名单。
  if (auth.can('workbench:use')) {
    const { data } = await api.GET('/api/v1/transfer-targets')
    colleagues.value = (data?.agents ?? []).filter((a) => a.staff_id !== props.customer?.owner_id)
  }
})

async function submit(): Promise<void> {
  if (!props.customer) return
  if (form.to === 'colleague' && !form.staffId) {
    ElMessage.warning('请选择同事')
    return
  }
  if (!form.reason.trim()) {
    ElMessage.warning('请填写申请原因')
    return
  }
  saving.value = true
  const { data, error } = await api.POST('/api/v1/customers/{customer_id}/transfer-requests', {
    params: { path: { customer_id: props.customer.id } },
    body: {
      to_owner_id: form.to === 'colleague' ? form.staffId : null,
      reason: form.reason.trim(),
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已提交申请，等待管理员审批')
  visible.value = false
  emit('done')
}
</script>

<template>
  <el-dialog v-model="visible" title="申请转移客户" width="460px" data-testid="transfer-request-dialog">
    <p v-if="customer" class="summary">
      客户：{{ customer.display_name }}，当前归属：{{ customer.owner_display_name ?? '无' }}。
      管理员审批通过后才会转移。
    </p>
    <el-form label-width="72px" @submit.prevent="submit">
      <el-form-item label="转给">
        <el-radio-group v-model="form.to">
          <el-radio value="me" :disabled="customer?.owner_id === auth.me?.id">我自己</el-radio>
          <el-radio value="colleague">同事</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item v-if="form.to === 'colleague'" label="同事">
        <el-select
          v-model="form.staffId"
          filterable
          placeholder="选择在线同事"
          no-data-text="没有其他在线同事"
          data-testid="transfer-request-to"
        >
          <el-option
            v-for="a in colleagues"
            :key="a.staff_id"
            :label="a.display_name"
            :value="a.staff_id"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="原因" required>
        <el-input
          v-model="form.reason"
          type="textarea"
          :rows="3"
          maxlength="500"
          placeholder="例如：客户希望由熟悉业务的同事继续跟进"
          data-testid="transfer-request-reason"
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button
        type="primary"
        :loading="saving"
        data-testid="transfer-request-submit"
        @click="submit"
      >
        提交申请
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.summary {
  margin-top: 0;
  color: var(--el-text-color-secondary);
}
</style>
