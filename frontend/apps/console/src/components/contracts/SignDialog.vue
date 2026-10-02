<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { reactive, ref, watch } from 'vue'

import { api } from '../../api'
import type { Contract } from '../../contracts'
import { fileToBase64 } from '../../knowledge'
import { isoDate, zonedToday } from '../../profit'
import { useAuthStore } from '../../stores/auth'

/**
 * 登记签署（§34.4）：签订日期、开始和结束日期，可以上传签署后的扫描件（PDF、JPG、PNG，最大 20 MB）。
 * 状态变成"已签署"，结束日期快到时 AI 唤醒提醒负责人。
 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{ contract: Contract }>()
const emit = defineEmits<{ signed: [contract: Contract] }>()

const MAX_BYTES = 20 * 1024 * 1024
const ACCEPT = '.pdf,.jpg,.jpeg,.png'

const auth = useAuthStore()
const form = reactive({ signDate: '', startDate: '', endDate: '' })
const file = ref<File | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const saving = ref(false)

watch(open, (value) => {
  if (!value) return
  const today = isoDate(zonedToday(auth.me?.tenant.timezone))
  form.signDate = props.contract.sign_date ?? today
  form.startDate = props.contract.start_date ?? form.signDate
  form.endDate = props.contract.end_date ?? ''
  file.value = null
})

function pick(event: Event): void {
  const input = event.target as HTMLInputElement
  const chosen = input.files?.[0] ?? null
  input.value = ''
  if (chosen && chosen.size > MAX_BYTES) {
    ElMessage.warning('扫描件最大 20 MB')
    return
  }
  file.value = chosen
}

async function submit(): Promise<void> {
  if (!form.signDate) {
    ElMessage.warning('请填写签订日期')
    return
  }
  if (form.startDate && form.endDate && form.endDate < form.startDate) {
    ElMessage.warning('结束日期不能早于开始日期')
    return
  }
  saving.value = true
  const scan = file.value ? { filename: file.value.name, content_base64: await fileToBase64(file.value) } : null
  const { data, error } = await api.POST('/api/v1/contracts/{contract_id}/sign', {
    params: { path: { contract_id: props.contract.id } },
    body: {
      sign_date: form.signDate,
      start_date: form.startDate || null,
      end_date: form.endDate || null,
      scan,
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已登记签署')
  open.value = false
  emit('signed', data)
}
</script>

<template>
  <el-dialog v-model="open" title="登记签署" width="460px" append-to-body data-testid="contract-sign-dialog">
    <el-form label-width="84px" :disabled="saving">
      <el-form-item label="签订日期" required>
        <el-date-picker v-model="form.signDate" type="date" value-format="YYYY-MM-DD" data-testid="contract-sign-date" />
      </el-form-item>
      <el-form-item label="开始日期">
        <el-date-picker v-model="form.startDate" type="date" value-format="YYYY-MM-DD" data-testid="contract-start-date" />
      </el-form-item>
      <el-form-item label="结束日期">
        <el-date-picker
          v-model="form.endDate"
          type="date"
          value-format="YYYY-MM-DD"
          placeholder="不填表示长期有效"
          data-testid="contract-end-date"
        />
        <div class="hint">结束日期快到时，AI 唤醒会提醒合同的负责人。</div>
      </el-form-item>
      <el-form-item label="扫描件">
        <input
          ref="fileInput"
          type="file"
          :accept="ACCEPT"
          class="file-input"
          data-testid="contract-scan-input"
          @change="pick"
        />
        <el-button size="small" @click="fileInput?.click()">选择文件</el-button>
        <span v-if="file" class="file-name" data-testid="contract-scan-name">{{ file.name }}</span>
        <span v-else class="hint inline">PDF、JPG、PNG，最大 20 MB</span>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button :disabled="saving" @click="open = false">取消</el-button>
      <el-button type="primary" :loading="saving" data-testid="contract-sign-submit" @click="submit">登记签署</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.hint {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}

.hint.inline {
  margin: 0 0 0 8px;
}

.file-input {
  display: none;
}

.file-name {
  margin-left: 8px;
  font-size: 13px;
}
</style>
