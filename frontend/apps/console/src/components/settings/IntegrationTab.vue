<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import {
  DELIVERY_STATE,
  deliveryState,
  EVENT,
  EVENTS,
  prettyJson,
  SCOPE,
  SCOPES,
  type ApiKey,
  type Delivery,
  type DeliveryState,
  type Endpoint,
  type EventName,
  type Scope,
} from '../../integration'
import { copyText } from '../../orders'

/**
 * 设置 → 企业系统对接（设计文档 §25.8）：接口密钥（按权限范围授权，完整密钥只显示一次）、
 * 推送地址（订阅的事件、签名密钥只显示一次、测试推送）和推送记录（失败重试、死信与重发）。
 */
const keys = ref<ApiKey[]>([])
const endpoints = ref<Endpoint[]>([])
const deliveries = ref<Delivery[]>([])
const deliveryTotal = ref(0)
const loading = ref(false)
const busy = ref(false)
const filters = reactive({ endpointId: '', status: '' as DeliveryState | '' })
const keyForm = reactive({ open: false, name: '', scopes: [] as Scope[] })
const hookForm = reactive({
  open: false,
  id: null as string | null,
  name: '',
  url: '',
  events: [] as EventName[],
  enabled: true,
})
// 只显示一次的密钥（新建接口密钥、新建推送地址或更换签名密钥之后）。
const secret = reactive({ open: false, title: '', value: '', hint: '' })
const viewing = ref<Delivery | null>(null)

const activeKeys = computed(() => keys.value.filter((k) => !k.revoked_at).length)

async function loadKeys(): Promise<void> {
  const { data, error } = await api.GET('/api/v1/admin/api-keys')
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  keys.value = data.items
}

async function loadEndpoints(): Promise<void> {
  const { data } = await api.GET('/api/v1/admin/webhooks')
  endpoints.value = data?.items ?? []
}

async function loadDeliveries(): Promise<void> {
  loading.value = true
  const { data } = await api.GET('/api/v1/admin/webhook-deliveries', {
    params: {
      query: {
        endpoint_id: filters.endpointId || undefined,
        status: filters.status || undefined,
        limit: 50,
      },
    },
  })
  loading.value = false
  deliveries.value = data?.items ?? []
  deliveryTotal.value = data?.total ?? 0
}

async function refresh(): Promise<void> {
  await Promise.all([loadKeys(), loadEndpoints(), loadDeliveries()])
}

function showSecret(title: string, value: string, hint: string): void {
  Object.assign(secret, { open: true, title, value, hint })
}

async function copySecret(): Promise<void> {
  if (await copyText(secret.value)) ElMessage.success('已复制')
}

// ---- 接口密钥 ----

function newKey(): void {
  Object.assign(keyForm, { open: true, name: '', scopes: [] })
}

async function saveKey(): Promise<void> {
  if (!keyForm.name.trim() || !keyForm.scopes.length) {
    ElMessage.warning('请填写用途并至少选择一项权限')
    return
  }
  busy.value = true
  const { data, error } = await api.POST('/api/v1/admin/api-keys', {
    body: { name: keyForm.name.trim(), scopes: keyForm.scopes },
  })
  busy.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  keyForm.open = false
  showSecret(
    '接口密钥已创建',
    data.key,
    '完整的密钥只显示这一次，请复制后交给企业系统的开发人员，放在请求头 Authorization: Bearer <密钥> 里。',
  )
  await loadKeys()
}

async function revoke(key: ApiKey): Promise<void> {
  try {
    await ElMessageBox.confirm(`撤销「${key.name}」后，使用它的企业系统立即无法调用接口，不能恢复。`, '撤销接口密钥', {
      confirmButtonText: '撤销',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  const { error } = await api.POST('/api/v1/admin/api-keys/{key_id}/revoke', {
    params: { path: { key_id: key.id } },
  })
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已撤销')
  await loadKeys()
}

// ---- 推送地址 ----

function newHook(): void {
  Object.assign(hookForm, {
    open: true,
    id: null,
    name: '',
    url: '',
    events: ['order.created', 'order.confirmed', 'order.status_changed', 'order.cancelled'],
    enabled: true,
  })
}

function editHook(endpoint: Endpoint): void {
  Object.assign(hookForm, {
    open: true,
    id: endpoint.id,
    name: endpoint.name,
    url: endpoint.url,
    events: [...endpoint.events] as EventName[],
    enabled: endpoint.enabled,
  })
}

async function saveHook(): Promise<void> {
  if (!hookForm.name.trim() || !hookForm.url.trim() || !hookForm.events.length) {
    ElMessage.warning('请填写名称、地址，并至少订阅一个事件')
    return
  }
  const body = {
    name: hookForm.name.trim(),
    url: hookForm.url.trim(),
    events: hookForm.events,
    enabled: hookForm.enabled,
  }
  busy.value = true
  if (hookForm.id) {
    const { data, error } = await api.PUT('/api/v1/admin/webhooks/{endpoint_id}', {
      params: { path: { endpoint_id: hookForm.id } },
      body,
    })
    busy.value = false
    if (!data) {
      ElMessage.error(errorMessage(error))
      return
    }
    ElMessage.success('已保存')
  } else {
    const { data, error } = await api.POST('/api/v1/admin/webhooks', { body })
    busy.value = false
    if (!data) {
      ElMessage.error(errorMessage(error))
      return
    }
    showSecret(
      '推送地址已添加',
      data.secret,
      '签名密钥只显示这一次。企业系统收到推送后，用它校验请求头 X-EDP-Signature（见下方说明）。',
    )
  }
  hookForm.open = false
  await loadEndpoints()
}

async function testHook(endpoint: Endpoint): Promise<void> {
  busy.value = true
  const { data, error } = await api.POST('/api/v1/admin/webhooks/{endpoint_id}/test', {
    params: { path: { endpoint_id: endpoint.id } },
  })
  busy.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  if (data.ok) ElMessage.success(`推送成功（HTTP ${data.status}，${data.duration_ms} 毫秒）`)
  else ElMessage.error(`推送失败：${data.error ?? '未知原因'}`)
  await Promise.all([loadEndpoints(), loadDeliveries()])
}

async function rotate(endpoint: Endpoint): Promise<void> {
  try {
    await ElMessageBox.confirm('新的签名密钥立即生效，企业系统需要同时换成新密钥，否则会校验失败。', '更换签名密钥', {
      confirmButtonText: '更换',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  const { data, error } = await api.POST('/api/v1/admin/webhooks/{endpoint_id}/rotate-secret', {
    params: { path: { endpoint_id: endpoint.id } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  showSecret('签名密钥已更换', data.secret, '新的签名密钥只显示这一次，请交给企业系统的开发人员。')
}

async function removeHook(endpoint: Endpoint): Promise<void> {
  try {
    await ElMessageBox.confirm(`删除「${endpoint.name}」及它的推送记录？`, '删除推送地址', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  const { error } = await api.DELETE('/api/v1/admin/webhooks/{endpoint_id}', {
    params: { path: { endpoint_id: endpoint.id } },
  })
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已删除')
  await refresh()
}

// ---- 推送记录 ----

async function view(row: Delivery): Promise<void> {
  const { data, error } = await api.GET('/api/v1/admin/webhook-deliveries/{delivery_id}', {
    params: { path: { delivery_id: row.id } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  viewing.value = data
}

async function resend(row: Delivery): Promise<void> {
  const { error } = await api.POST('/api/v1/admin/webhook-deliveries/{delivery_id}/resend', {
    params: { path: { delivery_id: row.id } },
  })
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已安排重发，稍后推送')
  await Promise.all([loadDeliveries(), loadEndpoints()])
}

watch(() => [filters.endpointId, filters.status], loadDeliveries)
onMounted(refresh)
</script>

<template>
  <div class="integration" data-testid="integration-tab">
    <p class="sub">
      企业自己的系统（ERP、CRM 等）可以用接口密钥调用开放接口：同步商品和价格、查询订单、回传状态、物流和收款，
      创建订单和待办；订单和待办有变化时，平台把事件推送到下面的地址。对接后，订单确认之后的状态以企业系统回传的为准。
    </p>

    <div class="section-head">
      <h4>接口密钥</h4>
      <el-button size="small" type="primary" data-testid="api-key-new" @click="newKey">新建接口密钥</el-button>
    </div>
    <el-table :data="keys" size="small" empty-text="还没有接口密钥" data-testid="api-keys">
      <el-table-column prop="name" label="用途" min-width="140" />
      <el-table-column label="密钥" width="190">
        <template #default="{ row }"><span class="mono">{{ row.display }}</span></template>
      </el-table-column>
      <el-table-column label="权限" min-width="220">
        <template #default="{ row }">
          <el-tag v-for="s in row.scopes" :key="s" size="small" effect="plain" class="tag">{{ SCOPE[s] ?? s }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="创建" width="170">
        <template #default="{ row }">
          {{ formatDateTime(row.created_at) }}
          <div class="muted">{{ row.created_by_name ?? '' }}</div>
        </template>
      </el-table-column>
      <el-table-column label="最近使用" width="160">
        <template #default="{ row }">{{ row.last_used_at ? formatDateTime(row.last_used_at) : '—' }}</template>
      </el-table-column>
      <el-table-column label="" width="90">
        <template #default="{ row }">
          <el-tag v-if="row.revoked_at" size="small" type="info">已撤销</el-tag>
          <el-button v-else link type="danger" size="small" data-testid="api-key-revoke" @click="revoke(row)"
            >撤销</el-button
          >
        </template>
      </el-table-column>
    </el-table>
    <p class="muted">有效的密钥 {{ activeKeys }} 个（最多 20 个）。</p>

    <div class="section-head">
      <h4>推送地址</h4>
      <el-button size="small" type="primary" data-testid="webhook-new" @click="newHook">添加推送地址</el-button>
    </div>
    <el-table :data="endpoints" size="small" empty-text="还没有推送地址" data-testid="webhooks">
      <el-table-column label="名称" min-width="120">
        <template #default="{ row }">
          {{ row.name }}
          <el-tag v-if="!row.enabled" size="small" type="info">已停用</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="地址" min-width="200">
        <template #default="{ row }"><span class="mono url">{{ row.url }}</span></template>
      </el-table-column>
      <el-table-column label="订阅的事件" min-width="200">
        <template #default="{ row }">{{ row.events.map((e: string) => EVENT[e] ?? e).join('、') }}</template>
      </el-table-column>
      <el-table-column label="推送情况" width="170">
        <template #default="{ row }">
          <div v-if="row.pending" class="muted">等待中 {{ row.pending }}</div>
          <div v-if="row.dead" class="danger" data-testid="webhook-dead">失败 {{ row.dead }}</div>
          <div v-if="row.last_success_at" class="muted">最近成功 {{ formatDateTime(row.last_success_at) }}</div>
        </template>
      </el-table-column>
      <el-table-column label="" width="210">
        <template #default="{ row }">
          <el-button link type="primary" size="small" :disabled="busy" data-testid="webhook-test" @click="testHook(row)"
            >测试</el-button
          >
          <el-button link type="primary" size="small" data-testid="webhook-edit" @click="editHook(row)">修改</el-button>
          <el-button link size="small" @click="rotate(row)">更换密钥</el-button>
          <el-button link type="danger" size="small" @click="removeHook(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-collapse class="guide">
      <el-collapse-item title="推送格式与签名校验" name="guide">
        <ul class="muted">
          <li>POST JSON：{"id", "event", "created_at", "data": {"order": …} 或 {"todo": …}}，data.order 与开放接口返回的订单相同。</li>
          <li>请求头 X-EDP-Event 为事件名；X-EDP-Delivery 为推送记录 ID（重发时不变，可以据此去重）。</li>
          <li>
            X-EDP-Signature: t=时间戳,v1=签名。签名是用签名密钥对"时间戳.请求体"计算的 HMAC-SHA256（十六进制）；
            请校验签名，并拒绝时间戳与当前相差 5 分钟以上的请求。
          </li>
          <li>返回 2xx 视为成功；失败后按 1 分钟、5 分钟、15 分钟、1 小时、3 小时、6 小时重试，仍失败的停止重试，可以在下面重发。</li>
        </ul>
      </el-collapse-item>
    </el-collapse>

    <div class="section-head">
      <h4>推送记录</h4>
      <span>
        <el-select v-model="filters.endpointId" clearable placeholder="推送地址" size="small" class="filter">
          <el-option v-for="e in endpoints" :key="e.id" :label="e.name" :value="e.id" />
        </el-select>
        <el-select
          v-model="filters.status"
          clearable
          placeholder="状态"
          size="small"
          class="filter"
          data-testid="delivery-status-filter"
        >
          <el-option v-for="(v, k) in DELIVERY_STATE" :key="k" :label="v[0]" :value="k" />
        </el-select>
      </span>
    </div>
    <el-table v-loading="loading" :data="deliveries" size="small" empty-text="还没有推送" data-testid="deliveries">
      <el-table-column label="事件" width="150">
        <template #default="{ row }">{{ EVENT[row.event] ?? row.event }}</template>
      </el-table-column>
      <el-table-column prop="endpoint_name" label="推送地址" width="120" />
      <el-table-column label="状态" width="150">
        <template #default="{ row }">
          <el-tag size="small" :type="DELIVERY_STATE[deliveryState(row)][1]" data-testid="delivery-state">{{
            DELIVERY_STATE[deliveryState(row)][0]
          }}</el-tag>
          <span class="muted"> 第 {{ row.attempts }} 次</span>
        </template>
      </el-table-column>
      <el-table-column label="结果" min-width="200">
        <template #default="{ row }">
          <span v-if="row.last_error" class="danger">{{ row.last_error }}</span>
          <span v-else-if="row.delivered_at" class="muted">HTTP {{ row.last_status }}</span>
          <div v-if="row.next_attempt_at && row.status === 'pending'" class="muted">
            下次推送 {{ formatDateTime(row.next_attempt_at) }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="时间" width="160">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="" width="110">
        <template #default="{ row }">
          <el-button link type="primary" size="small" data-testid="delivery-view" @click="view(row)">查看</el-button>
          <el-button
            v-if="row.status !== 'pending'"
            link
            type="primary"
            size="small"
            data-testid="delivery-resend"
            @click="resend(row)"
            >重发</el-button
          >
        </template>
      </el-table-column>
    </el-table>
    <p class="muted">共 {{ deliveryTotal }} 条，显示最近 50 条。</p>

    <el-dialog v-model="keyForm.open" title="新建接口密钥" width="480px" append-to-body data-testid="api-key-dialog">
      <el-form label-width="72px">
        <el-form-item label="用途" required>
          <el-input v-model="keyForm.name" maxlength="64" placeholder="例如：ERP 订单同步" data-testid="api-key-name" />
        </el-form-item>
        <el-form-item label="权限" required>
          <el-checkbox-group v-model="keyForm.scopes" class="checks" data-testid="api-key-scopes">
            <el-checkbox v-for="[value, label] in SCOPES" :key="value" :value="value">{{ label }}</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="keyForm.open = false">取消</el-button>
        <el-button type="primary" :loading="busy" data-testid="api-key-save" @click="saveKey">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="hookForm.open"
      :title="hookForm.id ? '修改推送地址' : '添加推送地址'"
      width="560px"
      append-to-body
      data-testid="webhook-dialog"
    >
      <el-form label-width="84px">
        <el-form-item label="名称" required>
          <el-input v-model="hookForm.name" maxlength="64" data-testid="webhook-name" />
        </el-form-item>
        <el-form-item label="地址" required>
          <el-input v-model="hookForm.url" maxlength="1024" placeholder="https://" data-testid="webhook-url" />
        </el-form-item>
        <el-form-item label="订阅事件" required>
          <el-checkbox-group v-model="hookForm.events" class="checks" data-testid="webhook-events">
            <el-checkbox v-for="[value, label] in EVENTS" :key="value" :value="value">{{ label }}</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="hookForm.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="hookForm.open = false">取消</el-button>
        <el-button type="primary" :loading="busy" data-testid="webhook-save" @click="saveHook">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="secret.open" :title="secret.title" width="560px" append-to-body data-testid="secret-dialog">
      <el-alert type="warning" :closable="false" show-icon :title="secret.hint" />
      <div class="secret">
        <span class="mono" data-testid="secret-value">{{ secret.value }}</span>
        <el-button size="small" @click="copySecret">复制</el-button>
      </div>
      <template #footer>
        <el-button type="primary" data-testid="secret-done" @click="secret.open = false">已保存</el-button>
      </template>
    </el-dialog>

    <el-dialog
      :model-value="viewing !== null"
      :title="viewing ? `推送内容 · ${EVENT[viewing.event] ?? viewing.event}` : ''"
      width="720px"
      append-to-body
      @update:model-value="viewing = null"
    >
      <pre class="body" data-testid="delivery-body">{{ prettyJson(viewing?.body) }}</pre>
    </el-dialog>
  </div>
</template>

<style scoped>
.integration {
  max-width: 1100px;
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 20px 0 8px;
}

.section-head h4 {
  margin: 0;
}

.tag {
  margin: 0 4px 2px 0;
}

.mono {
  font-family: monospace;
  font-size: 12px;
}

.url {
  word-break: break-all;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.danger {
  color: var(--el-color-danger);
  font-size: 12px;
}

.filter {
  width: 140px;
  margin-left: 8px;
}

.checks {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
}

.guide {
  margin-top: 12px;
}

.secret {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  padding: 8px;
  background: var(--el-fill-color-light);
  border-radius: 4px;
  word-break: break-all;
}

.body {
  max-height: 480px;
  overflow: auto;
  background: var(--el-fill-color-light);
  padding: 8px;
  font-size: 12px;
}
</style>
