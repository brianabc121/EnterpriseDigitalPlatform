<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { api, formatDateTime } from '../api'
import TenantAdmins from '../components/TenantAdmins.vue'
import TenantBilling from '../components/TenantBilling.vue'
import TenantClosure from '../components/TenantClosure.vue'
import TenantKeys from '../components/TenantKeys.vue'
import TenantLlm from '../components/TenantLlm.vue'
import TenantRateLimits from '../components/TenantRateLimits.vue'
import TenantSupport from '../components/TenantSupport.vue'
import { TENANT_STATUS } from '../labels'

const props = defineProps<{ id: string }>()

const tenant = ref<Schemas['TenantOut'] | null>(null)
// 链接里可以带 tab=admins 等直接打开某个页签。
const initialTab = useRoute().query.tab
const tab = ref(typeof initialTab === 'string' ? initialTab : 'billing')

async function load(): Promise<void> {
  const { data, error } = await api.GET('/platform/v1/tenants/{tenant_id}', {
    params: { path: { tenant_id: props.id } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  tenant.value = data
}

onMounted(load)
</script>

<template>
  <div v-if="tenant">
    <el-page-header @back="$router.push({ name: 'tenants' })">
      <template #content>
        <span class="title">{{ tenant.name }}</span>
        <el-tag disable-transitions class="tag">{{ tenant.code }}</el-tag>
        <el-tag disable-transitions :type="tenant.status === 'active' ? 'success' : 'danger'" class="tag">
          {{ TENANT_STATUS[tenant.status] ?? tenant.status }}
        </el-tag>
        <span class="sub">开通于 {{ formatDateTime(tenant.created_at) }}</span>
      </template>
    </el-page-header>
    <el-tabs v-model="tab" class="tabs">
      <el-tab-pane label="套餐与账单" name="billing">
        <TenantBilling :tenant-id="tenant.id" @changed="load" />
      </el-tab-pane>
      <el-tab-pane label="管理员账号" name="admins" lazy>
        <TenantAdmins :tenant="tenant" />
      </el-tab-pane>
      <el-tab-pane label="大模型" name="llm" lazy>
        <TenantLlm :tenant-id="tenant.id" />
      </el-tab-pane>
      <el-tab-pane label="限流" name="rate-limits" lazy>
        <TenantRateLimits :tenant-id="tenant.id" />
      </el-tab-pane>
      <el-tab-pane label="数据密钥" name="keys" lazy>
        <TenantKeys :tenant-id="tenant.id" />
      </el-tab-pane>
      <el-tab-pane label="运维访问" name="support" lazy>
        <TenantSupport :tenant-id="tenant.id" />
      </el-tab-pane>
      <el-tab-pane label="注销" name="closure" lazy>
        <TenantClosure :tenant="tenant" @changed="load" />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.title {
  font-weight: 600;
  margin-right: 8px;
}

.tag {
  margin-right: 8px;
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.tabs {
  margin-top: 16px;
}
</style>
