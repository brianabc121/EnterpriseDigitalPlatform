<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref } from 'vue'

import { api, formatDateTime } from '../api'

/** 租户数据密钥（信封加密）：查看版本，轮换并重新加密现有密文。 */
const props = defineProps<{ tenantId: string }>()

const keys = ref<Schemas['TenantKeys'] | null>(null)
const rotating = ref(false)

async function load(): Promise<void> {
  const { data, error } = await api.GET('/platform/v1/tenants/{tenant_id}/keys', {
    params: { path: { tenant_id: props.tenantId } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  keys.value = data
}

async function rotate(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '生成新版本的数据密钥，并把这个租户现有的密文（渠道凭证、自带模型密钥、客户手机号和邮箱）换成新版本加密。旧版本保留用于解密。',
      '轮换数据密钥',
      { type: 'warning', confirmButtonText: '轮换' },
    )
  } catch {
    return
  }
  rotating.value = true
  const { data, error } = await api.POST('/platform/v1/tenants/{tenant_id}/keys/rotate', {
    params: { path: { tenant_id: props.tenantId } },
  })
  rotating.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  const total = Object.values(data.reencrypted).reduce((sum, n) => sum + n, 0)
  ElMessage.success(`已轮换到第 ${data.version} 版，重新加密 ${total} 条密文`)
  if (data.failed) ElMessage.warning(`${data.failed} 条密文无法解密，没有重新加密`)
  await load()
}

onMounted(load)
</script>

<template>
  <div v-if="keys" data-testid="tenant-keys">
    <el-descriptions :column="3" border>
      <el-descriptions-item label="当前版本">
        {{ keys.current ?? '还没有生成' }}
      </el-descriptions-item>
      <el-descriptions-item label="版本数">{{ keys.versions.length }}</el-descriptions-item>
      <el-descriptions-item label="旧版本密文">{{ keys.stale }}</el-descriptions-item>
    </el-descriptions>
    <el-table :data="keys.versions" class="versions" empty-text="还没有加密过数据">
      <el-table-column prop="version" label="版本" width="100" />
      <el-table-column label="创建时间">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
    </el-table>
    <p class="hint">
      数据密钥用主密钥（EDP_DATA_ENCRYPTION_KEY）包装后保存，这里不显示密钥内容。更换主密钥请在服务器上执行
      <code>python -m app.cli rewrap-keys --old-key-env EDP_OLD_DATA_ENCRYPTION_KEY</code>。
      租户注销删除数据时密钥一并删除，残留的密文无法再解密。
    </p>
    <el-button type="warning" :loading="rotating" data-testid="rotate-key" @click="rotate">
      轮换数据密钥
    </el-button>
  </div>
</template>

<style scoped>
.versions {
  margin: 12px 0;
}

.hint {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.6;
}
</style>
