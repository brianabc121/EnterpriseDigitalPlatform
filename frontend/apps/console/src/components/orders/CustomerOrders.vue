<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import {
  money,
  ORDER_STATUS,
  ORDER_STATUS_TAG,
  ORDERS_CHANGED,
  PAYMENT_STATUS,
  type Order,
} from '../../orders'
import { useAuthStore } from '../../stores/auth'
import OrderDrawer from './OrderDrawer.vue'
import OrderFormDialog from './OrderFormDialog.vue'

/**
 * 当前客户的订单（工作台右栏、企业微信侧边栏、手机工作台，设计文档 §25.9）：可以新建订单，
 * 或选中几条消息（侧边栏里粘贴客户的话）让 AI 预填；可以把订单摘要和跟踪链接放进回复框发给客户。
 */
const props = defineProps<{
  customerId: string
  customerName?: string | null
  sessionId?: string | null
  source?: 'staff' | 'copilot' | 'sidebar'
}>()
const emit = defineEmits<{ insert: [text: string] }>()

const auth = useAuthStore()
const canCreate = computed(() => auth.can('order:create'))
const items = ref<Order[]>([])
const loading = ref(false)
const openId = ref<string | null>(null)
const creating = ref(false)
const prefill = ref<Schemas['OrderSuggestion'] | null>(null)
const picking = ref(false)
const messages = ref<Schemas['MessageOut'][]>([])
const picked = ref<string[]>([])
const pasted = ref('')
const extracting = ref(false)

async function load(): Promise<void> {
  loading.value = true
  const { data } = await api.GET('/api/v1/orders', {
    params: { query: { customer_id: props.customerId, limit: 20 } },
  })
  loading.value = false
  items.value = data?.items ?? []
}

function create(suggestion: Schemas['OrderSuggestion'] | null = null): void {
  prefill.value = suggestion
  creating.value = true
}

async function pick(): Promise<void> {
  pasted.value = ''
  messages.value = []
  picked.value = []
  if (props.sessionId) {
    const { data, error } = await api.GET('/api/v1/sessions/{session_id}/messages', {
      params: { path: { session_id: props.sessionId }, query: { limit: 50 } },
    })
    if (!data) {
      ElMessage.error(errorMessage(error))
      return
    }
    // 接口按时间倒序返回：取最近的 20 条文字消息，按时间顺序显示；默认选中客户最近的几句。
    messages.value = [...data.items]
      .reverse()
      .filter((m) => m.text_plain && m.sender_type !== 'system')
      .slice(-20)
    picked.value = messages.value
      .filter((m) => m.sender_type === 'customer')
      .slice(-5)
      .map((m) => m.id)
  }
  picking.value = true
}

async function extract(): Promise<void> {
  if (!picked.value.length && !pasted.value.trim()) {
    ElMessage.warning(props.sessionId ? '请选择消息' : '请粘贴客户的话')
    return
  }
  extracting.value = true
  const { data, error } = await api.POST('/api/v1/orders/extract', {
    body: {
      session_id: picked.value.length ? (props.sessionId ?? null) : null,
      message_ids: picked.value,
      text: pasted.value.trim() || null,
    },
  })
  extracting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  picking.value = false
  create(data)
}

/** 把订单摘要和跟踪链接放进回复框（由员工确认后发出）。 */
async function share(order: Order): Promise<void> {
  const { data, error } = await api.GET('/api/v1/orders/{order_id}', {
    params: { path: { order_id: order.id } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  const parts = [
    `您的订单 ${data.no}（${ORDER_STATUS[data.status]}）：${data.summary}，合计 ${money(data.total)}。`,
  ]
  if (data.shipping_company) parts.push(`物流：${data.shipping_company} ${data.tracking_no ?? ''}。`)
  if (data.tracking_active) parts.push(`查看订单进度：${data.tracking_url}`)
  emit('insert', parts.join(''))
}

function onChanged(): void {
  void load()
}

watch(() => props.customerId, load)
onMounted(() => {
  void load()
  window.addEventListener(ORDERS_CHANGED, onChanged)
})
onBeforeUnmount(() => window.removeEventListener(ORDERS_CHANGED, onChanged))
</script>

<template>
  <div v-loading="loading" class="customer-orders" data-testid="customer-orders">
    <div v-if="canCreate" class="toolbar">
      <el-button size="small" type="primary" data-testid="customer-order-new" @click="create()">新建订单</el-button>
      <el-button size="small" data-testid="customer-order-pick" @click="pick">AI 预填</el-button>
    </div>
    <div v-for="o in items" :key="o.id" class="item" data-testid="customer-order" @click="openId = o.id">
      <div class="line">
        <el-tag size="small" :type="ORDER_STATUS_TAG[o.status]">{{ ORDER_STATUS[o.status] }}</el-tag>
        <span class="no">{{ o.no }}</span>
        <span class="total">{{ money(o.total) }}</span>
      </div>
      <div class="summary">{{ o.summary }}</div>
      <div class="muted">
        {{ PAYMENT_STATUS[o.payment_status] }} · {{ formatDateTime(o.created_at) }}
        <el-button
          v-if="o.status !== 'draft'"
          link
          type="primary"
          size="small"
          class="share"
          data-testid="customer-order-share"
          @click.stop="share(o)"
          >发给客户</el-button
        >
      </div>
    </div>
    <el-empty v-if="!items.length && !loading" :image-size="40" description="这位客户还没有订单" />

    <el-dialog v-model="picking" title="AI 预填订单" width="520px" append-to-body>
      <template v-if="messages.length">
        <p class="muted">选择包含商品、数量和收货信息的消息：</p>
        <el-checkbox-group v-model="picked" class="messages">
          <el-checkbox v-for="m in messages" :key="m.id" :value="m.id" data-testid="order-pick-message">
            <span class="muted">{{ m.sender_type === 'customer' ? '客户' : '客服' }}：</span>{{ m.text_plain }}
          </el-checkbox>
        </el-checkbox-group>
      </template>
      <el-input
        v-else
        v-model="pasted"
        type="textarea"
        :rows="5"
        maxlength="4000"
        placeholder="粘贴客户说的话，例如：要两台黑色的智能门锁，寄到上海市……，收货人王先生，电话……"
        data-testid="order-pick-text"
      />
      <template #footer>
        <el-button @click="picking = false">取消</el-button>
        <el-button type="primary" :loading="extracting" data-testid="order-pick-extract" @click="extract"
          >AI 预填</el-button
        >
      </template>
    </el-dialog>

    <OrderFormDialog
      v-model="creating"
      :customer-id="customerId"
      :customer-name="customerName"
      :session-id="sessionId"
      :source="source ?? 'copilot'"
      :prefill="prefill"
      @saved="load"
    />
    <OrderDrawer :order-id="openId" size="640px" @close="openId = null" @changed="load" />
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}

.item {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  padding: 6px 8px;
  margin-bottom: 6px;
  cursor: pointer;
  font-size: 13px;
}

.line {
  display: flex;
  align-items: center;
  gap: 6px;
}

.no {
  font-weight: 500;
}

.total {
  margin-left: auto;
}

.summary {
  margin: 2px 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.share {
  float: right;
}

.messages {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  max-height: 320px;
  overflow-y: auto;
}

.messages :deep(.el-checkbox__label) {
  white-space: normal;
}
</style>
