<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { reactive, ref, watch } from 'vue'

import { api } from '../../api'
import type { ContractParty, ContractSettings } from '../../contracts'

/**
 * 合同设置（§34.7，合同管理权限）：我方信息（填写 {{我方名称}} 等内置填写项，名称为空时用企业的名称）、
 * 编号前缀（编号如 HT20261002-0001）、已签署的合同结束日期在几天内时算"快到期"。
 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{ settings: ContractSettings | null }>()
const emit = defineEmits<{ saved: [settings: ContractSettings] }>()

const PARTY_FIELDS: [keyof ContractParty, string, string][] = [
  ['name', '企业全称', '不填时用企业的名称'],
  ['address', '地址', ''],
  ['phone', '电话', ''],
  ['tax_no', '税号', '统一社会信用代码'],
  ['bank', '开户行', ''],
  ['account', '账号', ''],
  ['representative', '代表', '法定代表人或授权代表'],
]

const EMPTY_PARTY: ContractParty = {
  name: '',
  address: '',
  phone: '',
  tax_no: '',
  bank: '',
  account: '',
  representative: '',
}
const form = reactive<{ party: ContractParty; prefix: string; expiring_days: number }>({
  party: { ...EMPTY_PARTY },
  prefix: 'HT',
  expiring_days: 30,
})
const saving = ref(false)

watch(open, (value) => {
  if (!value || !props.settings) return
  form.party = { ...EMPTY_PARTY, ...props.settings.party }
  form.prefix = props.settings.prefix
  form.expiring_days = props.settings.expiring_days
})

async function submit(): Promise<void> {
  if (!/^[A-Za-z0-9-]{1,8}$/.test(form.prefix)) {
    ElMessage.warning('编号前缀只能是 1–8 位字母、数字或短横线')
    return
  }
  saving.value = true
  const { data, error } = await api.PUT('/api/v1/contracts/settings', { body: form })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已保存')
  open.value = false
  emit('saved', data.settings)
}
</script>

<template>
  <el-dialog v-model="open" title="合同设置" width="560px" append-to-body data-testid="contract-settings-dialog">
    <el-form label-width="96px" :disabled="saving">
      <h4 class="section">我方信息</h4>
      <p class="hint">填写合同里的 我方名称、我方地址 等内置填写项。</p>
      <el-form-item v-for="[key, label, hint] in PARTY_FIELDS" :key="key" :label="label">
        <el-input v-model="form.party[key]" :placeholder="hint" :data-testid="`contract-party-${key}`" />
      </el-form-item>
      <h4 class="section">编号和提醒</h4>
      <el-form-item label="编号前缀">
        <el-input v-model="form.prefix" maxlength="8" class="short" data-testid="contract-prefix" />
        <span class="hint inline">编号如 {{ form.prefix || 'HT' }}20261002-0001</span>
      </el-form-item>
      <el-form-item label="快到期">
        <el-input-number v-model="form.expiring_days" :min="1" :max="365" data-testid="contract-expiring-days" />
        <span class="hint inline">天内结束的已签署合同算快到期，AI 唤醒提醒负责人</span>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button :disabled="saving" @click="open = false">取消</el-button>
      <el-button type="primary" :loading="saving" data-testid="contract-settings-save" @click="submit">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.section {
  margin: 0 0 4px;
  font-size: 14px;
}

.hint {
  margin: 0 0 10px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.hint.inline {
  margin: 0 0 0 8px;
}

.short {
  width: 120px;
}
</style>
