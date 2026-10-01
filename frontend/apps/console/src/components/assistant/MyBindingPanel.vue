<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onBeforeUnmount, onMounted, ref } from 'vue'

import { api, formatDateTime } from '../../api'
import { PROVIDER_NAME } from '../../assistant'

/** 我的绑定：获取 6 位绑定码（10 分钟有效），在 IM 里对助理说"绑定 123456"；查看和解除已绑定的渠道。 */
const bindings = ref<Schemas['MyBindings'] | null>(null)
const code = ref<Schemas['BindingCodeOut'] | null>(null)
const remaining = ref(0)
const loading = ref(false)
const issuing = ref(false)
let timer: ReturnType<typeof setInterval> | undefined

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/assistant/bindings')
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  bindings.value = data
}

function tick(): void {
  if (!code.value) return
  remaining.value = Math.max(0, Math.round((new Date(code.value.expires_at).getTime() - Date.now()) / 1000))
  if (remaining.value === 0) code.value = null
}

async function issue(): Promise<void> {
  issuing.value = true
  const { data, error } = await api.POST('/api/v1/assistant/binding-code')
  issuing.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  code.value = data
  tick()
}

async function unbind(id: string, name: string): Promise<void> {
  try {
    await ElMessageBox.confirm(`解除后在 ${name} 里要重新绑定才能提问和收到通知。`, '解除绑定', {
      confirmButtonText: '解除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  const { error } = await api.DELETE('/api/v1/assistant/bindings/{identity_id}', {
    params: { path: { identity_id: id } },
  })
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  await load()
}

onMounted(() => {
  void load()
  timer = setInterval(tick, 1000)
})
onBeforeUnmount(() => clearInterval(timer))
</script>

<template>
  <div v-loading="loading" class="panel" data-testid="my-binding">
    <el-alert
      v-if="bindings && !bindings.bots.length"
      type="info"
      :closable="false"
      show-icon
      title="企业还没有接入 IM 机器人，可以先在上面的「对话」里使用助理；接入后在这里绑定。"
    />
    <template v-else-if="bindings">
      <p>
        企业接入了：
        <el-tag v-for="b in bindings.bots" :key="b.id" size="small" class="bot">
          {{ PROVIDER_NAME[b.provider] }} · {{ b.name }}
        </el-tag>
      </p>
      <p class="muted">
        在 IM 里找到助理，发送「绑定 + 绑定码」即可对应到你的员工账号；绑定后可以直接提问，平台的提醒也会发到那里。
        企业微信里已经绑定成员的不需要绑定码。
      </p>
      <div class="code-row">
        <el-button type="primary" :loading="issuing" data-testid="binding-code-issue" @click="issue">
          获取绑定码
        </el-button>
        <template v-if="code">
          <span class="code" data-testid="binding-code">{{ code.code }}</span>
          <span class="muted">{{ remaining }} 秒后失效 · {{ code.hint }}</span>
        </template>
      </div>
    </template>
    <el-table v-if="bindings?.items.length" :data="bindings.items" class="table" data-testid="my-bindings">
      <el-table-column label="渠道" width="160">
        <template #default="{ row }">{{ PROVIDER_NAME[row.provider] }} · {{ row.bot_name }}</template>
      </el-table-column>
      <el-table-column prop="display_name" label="IM 账号" min-width="160" />
      <el-table-column label="绑定时间" width="180">
        <template #default="{ row }">{{ row.bound_at ? formatDateTime(row.bound_at) : '—' }}</template>
      </el-table-column>
      <el-table-column width="100">
        <template #default="{ row }">
          <el-button link type="danger" @click="unbind(row.id, row.bot_name)">解除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.panel {
  max-width: 820px;
}

.bot {
  margin-right: 6px;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.code-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 12px 0;
}

.code {
  font-size: 24px;
  font-weight: 600;
  letter-spacing: 4px;
}

.table {
  margin-top: 12px;
}
</style>
