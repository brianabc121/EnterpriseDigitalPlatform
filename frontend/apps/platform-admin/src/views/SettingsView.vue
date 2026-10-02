<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'

import { api } from '../api'

const form = reactive<Schemas['TenantPolicy']>({
  signup_enabled: true,
  signup_plan_code: 'trial',
  grace_days: 7,
  retention_days: 30,
  export_ttl_days: 7,
})
const plans = ref<Schemas['PlanOut'][]>([])
const saving = ref(false)
// 读到现在的设置后再显示表单：否则加载完成时会覆盖已经改过的开关和数字。
const loaded = ref(false)

async function load(): Promise<void> {
  const [policy, planList] = await Promise.all([
    api.GET('/platform/v1/settings/tenant-policy'),
    api.GET('/platform/v1/plans'),
  ])
  if (!policy.data) {
    ElMessage.error(errorMessage(policy.error))
    return
  }
  Object.assign(form, policy.data)
  plans.value = (planList.data?.items ?? []).filter((p) => p.status === 'active')
  loaded.value = true
}

async function save(): Promise<void> {
  saving.value = true
  const { data, error } = await api.PUT('/platform/v1/settings/tenant-policy', { body: { ...form } })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已保存')
}

onMounted(load)
</script>

<template>
  <div v-loading="!loaded" class="page">
    <h2>平台设置</h2>
    <el-form v-if="loaded" label-width="150px" data-testid="tenant-policy">
      <el-divider content-position="left">自助注册</el-divider>
      <el-form-item label="开放自助注册">
        <el-switch v-model="form.signup_enabled" data-testid="signup-enabled" />
      </el-form-item>
      <el-form-item label="注册后的套餐">
        <el-select v-model="form.signup_plan_code">
          <el-option v-for="p in plans" :key="p.code" :label="p.name" :value="p.code" />
        </el-select>
      </el-form-item>
      <el-divider content-position="left">订阅与注销</el-divider>
      <el-form-item label="到期宽限期（天）">
        <el-input-number v-model="form.grace_days" :min="0" :max="90" />
        <span class="sub">订阅到期后过了宽限期停用租户，续费后自动恢复</span>
      </el-form-item>
      <el-form-item label="注销保留期（天）">
        <el-input-number v-model="form.retention_days" :min="0" :max="365" />
        <span class="sub">申请注销后保留这么久再删除数据，期间可以下载导出文件或撤销</span>
      </el-form-item>
      <el-form-item label="导出文件保留（天）">
        <el-input-number v-model="form.export_ttl_days" :min="1" :max="30" />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" :loading="saving" data-testid="policy-save" @click="save">保存</el-button>
      </el-form-item>
    </el-form>
  </div>
</template>

<style scoped>
.page {
  max-width: 720px;
}

h2 {
  margin: 0 0 8px;
  font-size: 18px;
}

.sub {
  margin-left: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
