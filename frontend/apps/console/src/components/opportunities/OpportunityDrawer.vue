<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import {
  ACTIVITY_LABEL,
  actionsOf,
  activityText,
  amountText,
  closeText,
  dueText,
  isoDate,
  LEVEL_LABEL,
  LEVEL_TAG,
  LEVELS,
  METHOD_LABEL,
  METHODS,
  SOURCE_TEXT,
  stageDaysText,
  STATUS_LABEL,
  STATUS_TAG,
  stepState,
  type Activity,
  type Digest,
  type FollowMethod,
  type Opportunity,
  type OpportunityLevel,
  type ProductRef,
  type Stage,
} from '../../opportunities'
import { useAuthStore } from '../../stores/auth'
import SessionDrawer from '../sessions/SessionDrawer.vue'
import LostDialog from './LostDialog.vue'
import NextStepDialog from './NextStepDialog.vue'
import WonDialog from './WonDialog.vue'

/**
 * 商机详情（设计文档 §40.8）：顶部是阶段步骤条（点一下就换阶段）和赢单 / 输单 / 重新跟进；左边字段（名称、
 * 客户、等级、预计金额、预计成交日、成交概率、负责人、来源、想要什么、顾虑、关联商品）就地修改；右边时间线、
 * 记一次跟进、安排下一步（待办）、AI 写跟进话术、AI 小结；关联的会话、订单、合同、待办各一行可以点开；
 * 确认或忽略 AI 的建议。
 */
const props = defineProps<{ opportunityId: string | null }>()
const emit = defineEmits<{ close: []; changed: [] }>()

const auth = useAuthStore()
const opportunity = ref<Opportunity | null>(null)
const stages = ref<Stage[]>([])
const loading = ref(false)
const busy = ref('')
const staff = ref<Schemas['StaffOut'][]>([])
const viewingSession = ref<string | null>(null)
const wonOpen = ref(false)
const lostOpen = ref(false)
const stepOpen = ref(false)
const edit = reactive({
  name: '',
  level: 'medium' as OpportunityLevel,
  amount: null as number | null,
  expectedCloseAt: '',
  probability: null as number | null,
  nextFollowAt: '',
  ownerId: '',
  interest: '',
  concerns: '',
  products: [] as ProductRef[],
})
const follow = reactive({ method: 'phone' as FollowMethod, content: '', nextFollowAt: '' })
const message = reactive({ text: '', knowledge: [] as string[], shown: false })
const digest = ref<Digest | null>(null)
const productOptions = ref<Schemas['ProductOut'][]>([])
const picked = ref('')
const searching = ref(false)

const open = computed({
  get: () => props.opportunityId !== null,
  set: (value) => {
    if (!value) emit('close')
  },
})
const actions = computed(() => actionsOf(opportunity.value?.status ?? 'dismissed'))
const canManage = computed(() => !!opportunity.value?.can_manage)
const editable = computed(
  () =>
    canManage.value &&
    (opportunity.value?.status === 'active' || opportunity.value?.status === 'suggested'),
)
const canPickOwner = computed(() => !!opportunity.value?.can_assign && auth.can('staff:read'))
const canPickProducts = computed(() => auth.can('order:read'))
const amountVisible = computed(() => !!opportunity.value?.amount_visible)
const today = computed(() => isoDate(new Date()))
const openStages = computed(() => stages.value.filter((s) => s.kind === 'open'))
const staffNames = computed(() => new Map(staff.value.map((s) => [s.id, s.display_name])))
const closeOverdue = computed(
  () =>
    !!opportunity.value?.expected_close_at &&
    opportunity.value.status === 'active' &&
    opportunity.value.expected_close_at < today.value,
)

function fill(data: Opportunity): void {
  opportunity.value = data
  Object.assign(edit, {
    name: data.name,
    level: data.level,
    amount: data.amount === null || data.amount === undefined ? null : Number(data.amount),
    expectedCloseAt: data.expected_close_at ?? '',
    probability: data.probability,
    nextFollowAt: data.next_follow_at ?? '',
    ownerId: data.owner_id ?? '',
    interest: data.interest ?? '',
    concerns: data.concerns ?? '',
    products: (data.products ?? []).map((p) => ({ ...p })),
  })
}

/** 赢单的"再开一个商机"返回新的一条：之后的操作都按详情里的 id。 */
function path() {
  return { params: { path: { opportunity_id: opportunity.value?.id ?? props.opportunityId ?? '' } } }
}

async function load(): Promise<void> {
  if (!props.opportunityId) return
  loading.value = true
  const [detail, stageList] = await Promise.all([
    api.GET('/api/v1/opportunities/{opportunity_id}', {
      params: { path: { opportunity_id: props.opportunityId } },
    }),
    stages.value.length === 0 ? api.GET('/api/v1/opportunities/stages') : Promise.resolve(null),
  ])
  loading.value = false
  if (stageList?.data) stages.value = stageList.data
  if (!detail.data) {
    ElMessage.error(errorMessage(detail.error))
    emit('close')
    return
  }
  fill(detail.data)
  if (canPickOwner.value && staff.value.length === 0) {
    const { data: list } = await api.GET('/api/v1/staff')
    staff.value = list?.items.filter((s) => s.status === 'active') ?? []
  }
}

async function done(
  action: string,
  request: Promise<{ data?: Opportunity; error?: unknown }>,
  success: string,
): Promise<boolean> {
  busy.value = action
  const { data, error } = await request
  busy.value = ''
  if (!data) {
    ElMessage.error(errorMessage(error))
    return false
  }
  fill(data)
  ElMessage.success(success)
  emit('changed')
  return true
}

async function moveTo(stage: Stage): Promise<void> {
  const current = opportunity.value
  if (!current || !actions.value.move || stage.id === current.stage_id) return
  await done(
    'stage',
    api.POST('/api/v1/opportunities/{opportunity_id}/stage', { ...path(), body: { stage_id: stage.id } }),
    `已移到「${stage.name}」`,
  )
}

function productsKey(products: ProductRef[]): string {
  return JSON.stringify(products.map((p) => [p.product_id ?? null, p.name.trim(), p.quantity ?? 1]))
}

async function saveInfo(): Promise<void> {
  const current = opportunity.value
  if (!current) return
  const body: Schemas['OpportunityUpdate'] = {}
  if (edit.name.trim() && edit.name.trim() !== current.name) body.name = edit.name.trim()
  if (edit.level !== current.level) body.level = edit.level
  if (edit.interest.trim() !== (current.interest ?? '')) body.interest = edit.interest
  if (edit.concerns.trim() !== (current.concerns ?? '')) body.concerns = edit.concerns
  const amountBefore = current.amount === null || current.amount === undefined ? null : Number(current.amount)
  if (amountVisible.value && (edit.amount ?? null) !== amountBefore) {
    body.amount = edit.amount === null ? null : edit.amount.toFixed(2)
  }
  if ((edit.expectedCloseAt || null) !== current.expected_close_at) {
    body.expected_close_at = edit.expectedCloseAt || null
  }
  if (edit.probability !== null && edit.probability !== current.probability) body.probability = edit.probability
  if ((edit.nextFollowAt || null) !== current.next_follow_at) body.next_follow_at = edit.nextFollowAt || null
  if (canPickOwner.value && (edit.ownerId || null) !== current.owner_id) body.owner_id = edit.ownerId || null
  const products = edit.products.filter((p) => p.name.trim())
  if (productsKey(products) !== productsKey(current.products ?? [])) {
    body.products = products.map((p) => ({ ...p, name: p.name.trim(), quantity: p.quantity ?? 1 }))
  }
  if (Object.keys(body).length === 0) {
    ElMessage.info('没有修改')
    return
  }
  await done('save', api.PATCH('/api/v1/opportunities/{opportunity_id}', { ...path(), body }), '已保存')
}

async function searchProducts(q: string): Promise<void> {
  searching.value = true
  const { data } = await api.GET('/api/v1/products', {
    params: { query: { q: q.trim() || undefined, limit: 20, offset: 0 } },
  })
  searching.value = false
  productOptions.value = data?.items ?? []
}

function pickProduct(id: string): void {
  const product = productOptions.value.find((p) => p.id === id)
  picked.value = ''
  if (!product || edit.products.some((p) => p.product_id === product.id)) return
  edit.products.push({ product_id: product.id, name: product.name, quantity: 1 })
}

function addProduct(): void {
  edit.products.push({ product_id: null, name: '', quantity: 1 })
}

async function addFollowup(): Promise<void> {
  if (!follow.content.trim()) {
    ElMessage.warning('请填写跟进的内容')
    return
  }
  const saved = await done(
    'follow',
    api.POST('/api/v1/opportunities/{opportunity_id}/followups', {
      ...path(),
      body: {
        kind: 'followup',
        method: follow.method,
        content: follow.content.trim(),
        next_follow_at: follow.nextFollowAt || null,
      },
    }),
    '已记一次跟进',
  )
  if (saved) Object.assign(follow, { content: '', nextFollowAt: '' })
}

async function reopen(): Promise<void> {
  const won = opportunity.value?.status === 'won'
  await done(
    'reopen',
    api.POST('/api/v1/opportunities/{opportunity_id}/reopen', { ...path(), body: {} }),
    won ? '已再开一个商机' : '已重新跟进',
  )
}

async function accept(): Promise<void> {
  await done('accept', api.POST('/api/v1/opportunities/{opportunity_id}/accept', path()), '已转入商机')
}

async function dismiss(): Promise<void> {
  busy.value = 'dismiss'
  const { error, response } = await api.POST('/api/v1/opportunities/{opportunity_id}/dismiss', path())
  busy.value = ''
  if (!response.ok) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已忽略，30 天内 AI 不再建议这位客户')
  emit('changed')
  emit('close')
}

async function writeMessage(): Promise<void> {
  busy.value = 'message'
  const { data, error } = await api.POST('/api/v1/opportunities/{opportunity_id}/message', path())
  busy.value = ''
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  Object.assign(message, { text: data.text, knowledge: data.knowledge, shown: true })
}

async function copyMessage(): Promise<void> {
  try {
    await navigator.clipboard.writeText(message.text)
    ElMessage.success('已复制，修改后发给客户')
  } catch {
    ElMessage.warning('复制失败，请手动选中文字复制')
  }
}

async function summarize(): Promise<void> {
  busy.value = 'summary'
  const { data, error } = await api.POST('/api/v1/opportunities/{opportunity_id}/summary', path())
  busy.value = ''
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  digest.value = data
  // 小结记进了时间线。
  const { data: fresh } = await api.GET('/api/v1/opportunities/{opportunity_id}', path())
  if (fresh) fill(fresh)
}

function closed(data: Opportunity): void {
  fill(data)
  emit('changed')
}

function kindTag(item: Activity): 'primary' | 'success' | 'warning' | 'danger' | 'info' {
  if (item.kind === 'followup') return 'primary'
  if (item.kind === 'stage' || item.kind === 'order' || item.kind === 'contract' || item.kind === 'payment') {
    return 'success'
  }
  if (item.kind === 'ai' || item.kind === 'session') return 'warning'
  return 'info'
}

/** 时间线里的订单、合同、待办各一行可以点开。 */
function linkOf(item: Activity): string | null {
  if (!item.linked_id) return null
  if (item.linked_type === 'order') return `/orders?id=${item.linked_id}`
  if (item.linked_type === 'contract') return `/contracts?id=${item.linked_id}`
  if (item.linked_type === 'todo') return `/todos?id=${item.linked_id}`
  return null
}

watch(
  () => props.opportunityId,
  (id) => {
    opportunity.value = null
    digest.value = null
    Object.assign(message, { text: '', knowledge: [], shown: false })
    Object.assign(follow, { method: 'phone', content: '', nextFollowAt: '' })
    if (id) void load()
  },
  { immediate: true },
)
</script>

<template>
  <el-drawer
    v-model="open"
    :title="opportunity ? `商机：${opportunity.name}` : '商机'"
    size="880px"
    class="opp-drawer"
    append-to-body
    data-testid="opp-drawer"
  >
    <div v-loading="loading" class="body">
      <template v-if="opportunity">
        <div class="head">
          <el-tag :type="STATUS_TAG[opportunity.status]" data-testid="opp-status">
            {{ STATUS_LABEL[opportunity.status] }}
          </el-tag>
          <el-tag :type="LEVEL_TAG[opportunity.level]" effect="plain">
            意向{{ LEVEL_LABEL[opportunity.level] }}
          </el-tag>
          <span v-if="amountVisible && opportunity.amount" class="amount" data-testid="opp-amount-text">
            {{ amountText(opportunity.amount) }}
          </span>
          <span class="muted">
            {{ SOURCE_TEXT[opportunity.source] }}
            <template v-if="opportunity.created_by_name">（{{ opportunity.created_by_name }}）</template>
            · {{ formatDateTime(opportunity.created_at) }}
          </span>
        </div>
        <div class="customer">
          <strong>{{ opportunity.customer_name }}</strong>
          <span v-if="opportunity.customer_company" class="muted"> · {{ opportunity.customer_company }}</span>
          <router-link
            :to="`/customers?customer=${opportunity.customer_id}`"
            class="small link"
            data-testid="opp-customer-link"
          >
            客户资料
          </router-link>
        </div>

        <div class="stage-bar" data-testid="opp-stage-bar">
          <button
            v-for="stage in openStages"
            :key="stage.id"
            type="button"
            class="step"
            :class="stepState(stage, opportunity, openStages)"
            :disabled="!actions.move || !canManage || busy !== ''"
            :title="`成交概率 ${stage.probability}%`"
            :data-testid="`opp-stage-${stage.code}`"
            @click="moveTo(stage)"
          >
            <span class="dot" :style="{ background: stage.color ?? undefined }" />
            {{ stage.name }}
          </button>
          <span
            v-if="opportunity.stage_kind !== 'open'"
            class="step current closed"
            :data-testid="`opp-stage-${opportunity.stage_code}`"
          >
            {{ opportunity.stage_name }}
          </span>
        </div>
        <div class="stage-info muted small" data-testid="opp-stage-info">
          在「{{ opportunity.stage_name }}」{{ stageDaysText(opportunity.days_in_stage) }}
          <el-tag v-if="opportunity.stale" size="small" type="danger" class="inline-tag">停滞</el-tag>
          <template v-if="opportunity.expected_close_at">
            · <span :class="{ overdue: closeOverdue }">{{ closeText(opportunity.expected_close_at, today) }}</span>
          </template>
          <template v-if="opportunity.next_follow_at && opportunity.status === 'active'">
            · 下次跟进
            <span
              class="due"
              :class="{ overdue: opportunity.overdue, today: opportunity.due_today }"
              data-testid="opp-due"
            >
              {{ dueText(opportunity.next_follow_at, today) }}
            </span>
          </template>
        </div>
        <div v-if="canManage" class="actions">
          <el-button v-if="actions.close" type="success" plain data-testid="opp-won" @click="wonOpen = true">
            赢单
          </el-button>
          <el-button v-if="actions.close" plain data-testid="opp-lost" @click="lostOpen = true">输单</el-button>
          <el-button
            v-if="actions.reopen"
            type="primary"
            plain
            :loading="busy === 'reopen'"
            data-testid="opp-reopen"
            @click="reopen"
          >
            {{ opportunity.status === 'won' ? '再开一个商机' : '重新跟进' }}
          </el-button>
          <el-button v-if="actions.follow" data-testid="opp-next-step-open" @click="stepOpen = true">
            安排下一步
          </el-button>
        </div>

        <el-alert
          v-if="actions.decide"
          type="warning"
          :closable="false"
          show-icon
          title="AI 建议把这位客户转入商机"
          class="block"
          data-testid="opp-suggestion"
        >
          <template #default>
            <div>确认后进入跟进；忽略后 30 天内 AI 不再建议这位客户。</div>
            <div v-if="canManage" class="decide">
              <el-button
                type="primary"
                size="small"
                :loading="busy === 'accept'"
                data-testid="opp-accept"
                @click="accept"
              >
                确认转入
              </el-button>
              <el-button size="small" :loading="busy === 'dismiss'" data-testid="opp-dismiss" @click="dismiss">
                忽略
              </el-button>
            </div>
          </template>
        </el-alert>

        <div class="columns">
          <section class="column">
            <h4>商机信息</h4>
            <el-form label-width="76px" size="small" :disabled="!editable">
              <el-form-item label="名称">
                <el-input v-model="edit.name" maxlength="128" data-testid="opp-edit-name" />
              </el-form-item>
              <el-form-item label="意向等级">
                <el-radio-group v-model="edit.level" data-testid="opp-edit-level">
                  <el-radio-button v-for="level in LEVELS" :key="level" :value="level">
                    {{ LEVEL_LABEL[level] }}
                  </el-radio-button>
                </el-radio-group>
              </el-form-item>
              <el-form-item v-if="amountVisible" label="预计金额">
                <el-input-number
                  v-model="edit.amount"
                  :min="0"
                  :precision="2"
                  :controls="false"
                  placeholder="元"
                  class="amount-input"
                  data-testid="opp-edit-amount"
                />
              </el-form-item>
              <el-form-item label="预计成交">
                <el-date-picker
                  v-model="edit.expectedCloseAt"
                  type="date"
                  value-format="YYYY-MM-DD"
                  placeholder="预计成交日"
                  data-testid="opp-edit-close"
                />
              </el-form-item>
              <el-form-item label="成交概率">
                <el-input-number
                  v-model="edit.probability"
                  :min="0"
                  :max="100"
                  :controls="false"
                  class="prob-input"
                  data-testid="opp-edit-probability"
                />
                <span class="unit">%（不填按阶段）</span>
              </el-form-item>
              <el-form-item label="下次跟进">
                <el-date-picker
                  v-model="edit.nextFollowAt"
                  type="date"
                  value-format="YYYY-MM-DD"
                  data-testid="opp-edit-next"
                />
              </el-form-item>
              <el-form-item label="负责人">
                <el-select
                  v-if="canPickOwner"
                  v-model="edit.ownerId"
                  clearable
                  placeholder="没有负责人"
                  data-testid="opp-edit-owner"
                >
                  <el-option v-for="s in staff" :key="s.id" :label="s.display_name" :value="s.id" />
                </el-select>
                <span v-else data-testid="opp-owner-name">{{ opportunity.owner_name ?? '没有负责人' }}</span>
              </el-form-item>
              <el-form-item label="想要什么">
                <el-input
                  v-model="edit.interest"
                  type="textarea"
                  :autosize="{ minRows: 2, maxRows: 5 }"
                  maxlength="1000"
                  data-testid="opp-edit-interest"
                />
              </el-form-item>
              <el-form-item label="顾虑">
                <el-input
                  v-model="edit.concerns"
                  type="textarea"
                  :autosize="{ minRows: 1, maxRows: 4 }"
                  maxlength="1000"
                  data-testid="opp-edit-concerns"
                />
              </el-form-item>
              <el-form-item label="关联商品">
                <div class="products" data-testid="opp-products">
                  <div v-for="(p, i) in edit.products" :key="i" class="product">
                    <el-input v-model="p.name" maxlength="100" placeholder="商品名称" class="product-name" />
                    <el-input-number
                      v-model="p.quantity"
                      :min="1"
                      :max="100000"
                      :controls="false"
                      class="product-qty"
                    />
                    <el-button v-if="editable" link type="danger" @click="edit.products.splice(i, 1)">
                      去掉
                    </el-button>
                  </div>
                  <div v-if="editable" class="product-add">
                    <el-select
                      v-if="canPickProducts"
                      v-model="picked"
                      filterable
                      remote
                      :remote-method="searchProducts"
                      :loading="searching"
                      placeholder="搜索商品加入"
                      class="product-pick"
                      data-testid="opp-product-pick"
                      @change="pickProduct"
                    >
                      <el-option v-for="o in productOptions" :key="o.id" :label="o.name" :value="o.id" />
                    </el-select>
                    <el-button link type="primary" data-testid="opp-product-add" @click="addProduct">
                      手填一个
                    </el-button>
                  </div>
                  <span v-if="!editable && edit.products.length === 0" class="muted">—</span>
                </div>
              </el-form-item>
              <el-form-item v-if="editable">
                <el-button type="primary" :loading="busy === 'save'" data-testid="opp-save" @click="saveInfo">
                  保存修改
                </el-button>
              </el-form-item>
            </el-form>
          </section>

          <section class="column">
            <h4>关联</h4>
            <dl class="facts" data-testid="opp-related">
              <template v-if="opportunity.session_id">
                <dt>依据的会话</dt>
                <dd>
                  <el-button
                    link
                    type="primary"
                    size="small"
                    data-testid="opp-session"
                    @click="viewingSession = opportunity.session_id"
                  >
                    查看会话
                  </el-button>
                </dd>
              </template>
              <dt>跟进</dt>
              <dd>
                {{ opportunity.follow_count }} 次
                <template v-if="opportunity.last_followed_at">
                  ，最近 {{ formatDateTime(opportunity.last_followed_at) }}
                </template>
              </dd>
              <template v-if="opportunity.order_no">
                <dt>成交订单</dt>
                <dd>
                  <router-link :to="`/orders?id=${opportunity.order_id}`" data-testid="opp-order">
                    {{ opportunity.order_no }}
                  </router-link>
                </dd>
              </template>
              <template v-if="opportunity.contract_no">
                <dt>合同</dt>
                <dd>
                  <router-link :to="`/contracts?id=${opportunity.contract_id}`" data-testid="opp-contract">
                    {{ opportunity.contract_no }}
                  </router-link>
                </dd>
              </template>
              <template v-if="opportunity.todos?.length">
                <dt>待办</dt>
                <dd>
                  <div v-for="t in opportunity.todos" :key="t.id" class="todo">
                    <router-link :to="`/todos?id=${t.id}`" :data-testid="`opp-todo-${t.id}`">
                      {{ t.no }} {{ t.title }}
                    </router-link>
                    <span class="muted small">
                      · {{ t.type_name }}
                      <template v-if="t.due_at"> · {{ formatDateTime(t.due_at) }} 截止</template>
                      <template v-if="t.assignee_name"> · {{ t.assignee_name }}</template>
                    </span>
                  </div>
                </dd>
              </template>
              <template v-if="opportunity.lost_reason_name || opportunity.lost_reason">
                <dt>输单原因</dt>
                <dd data-testid="opp-lost-reason">
                  {{ [opportunity.lost_reason_name, opportunity.lost_reason].filter(Boolean).join('：') }}
                </dd>
              </template>
              <template v-if="opportunity.closed_at">
                <dt>{{ opportunity.status === 'won' ? '赢单时间' : '关闭时间' }}</dt>
                <dd>{{ formatDateTime(opportunity.closed_at) }}</dd>
              </template>
            </dl>

            <template v-if="actions.follow && canManage">
              <h4 class="gap">
                AI 帮忙
                <span class="muted small">按想要什么、顾虑、最近的跟进和知识库写；AI 不直接联系客户</span>
              </h4>
              <div class="ai-buttons">
                <el-button :loading="busy === 'message'" data-testid="opp-write-message" @click="writeMessage">
                  {{ message.shown ? '重新写一段话术' : 'AI 写跟进话术' }}
                </el-button>
                <el-button :loading="busy === 'summary'" data-testid="opp-summary" @click="summarize">
                  {{ digest ? '重新小结' : 'AI 小结' }}
                </el-button>
              </div>
              <template v-if="message.shown">
                <el-input
                  v-model="message.text"
                  type="textarea"
                  :autosize="{ minRows: 3, maxRows: 8 }"
                  class="message"
                  data-testid="opp-message"
                />
                <div class="message-foot">
                  <span v-if="message.knowledge.length" class="muted small" data-testid="opp-message-refs">
                    参考：{{ message.knowledge.join('、') }}
                  </span>
                  <el-button size="small" data-testid="opp-copy-message" @click="copyMessage">复制</el-button>
                </div>
              </template>
              <div v-if="digest" class="digest" data-testid="opp-digest">
                <div><strong>到哪一步：</strong>{{ digest.status }}</div>
                <div><strong>客户在意：</strong>{{ digest.cares }}</div>
                <div><strong>建议下一步：</strong>{{ digest.next }}</div>
                <div class="muted small">AI 小结 · {{ formatDateTime(digest.generated_at) }}</div>
              </div>
            </template>

            <section v-if="actions.follow && canManage" class="gap" data-testid="opp-follow-form">
              <h4>记一次跟进</h4>
              <el-radio-group v-model="follow.method" size="small" class="methods">
                <el-radio-button v-for="method in METHODS" :key="method" :value="method">
                  {{ METHOD_LABEL[method] }}
                </el-radio-button>
              </el-radio-group>
              <el-input
                v-model="follow.content"
                type="textarea"
                :rows="3"
                maxlength="2000"
                placeholder="跟客户聊了什么、客户怎么说"
                data-testid="opp-follow-content"
              />
              <div class="follow-foot">
                <el-date-picker
                  v-model="follow.nextFollowAt"
                  type="date"
                  size="small"
                  value-format="YYYY-MM-DD"
                  placeholder="下次跟进（不填按默认天数）"
                  data-testid="opp-follow-next"
                />
                <el-button
                  type="primary"
                  size="small"
                  :loading="busy === 'follow'"
                  data-testid="opp-follow-save"
                  @click="addFollowup"
                >
                  记一次跟进
                </el-button>
              </div>
            </section>

            <h4 class="gap">时间线</h4>
            <el-timeline v-if="opportunity.activities.length" data-testid="opp-activities">
              <el-timeline-item
                v-for="item in opportunity.activities"
                :key="item.id"
                :timestamp="formatDateTime(item.created_at)"
                placement="top"
              >
                <div class="entry">
                  <el-tag size="small" effect="plain" :type="kindTag(item)">
                    {{ item.kind === 'followup' ? METHOD_LABEL[item.method] : ACTIVITY_LABEL[item.kind] }}
                  </el-tag>
                  <span class="muted small">{{ item.staff_name ?? (item.kind === 'ai' ? 'AI' : '系统') }}</span>
                  <el-button
                    v-if="item.session_id"
                    link
                    type="primary"
                    size="small"
                    @click="viewingSession = item.session_id"
                  >
                    查看会话
                  </el-button>
                  <router-link v-else-if="linkOf(item)" :to="linkOf(item) ?? ''" class="small">
                    查看{{ ACTIVITY_LABEL[item.kind] }}
                  </router-link>
                </div>
                <p class="content">{{ activityText(item) }}</p>
                <div v-if="item.next_follow_at" class="muted small">下次跟进 {{ item.next_follow_at }}</div>
              </el-timeline-item>
            </el-timeline>
            <el-empty v-else :image-size="48" description="还没有动态" />
          </section>
        </div>
      </template>
    </div>
  </el-drawer>
  <SessionDrawer :session-id="viewingSession" :staff-names="staffNames" @close="viewingSession = null" />
  <WonDialog v-model="wonOpen" :opportunity="opportunity" @done="closed" />
  <LostDialog v-model="lostOpen" :opportunity="opportunity" @done="closed" />
  <NextStepDialog v-model="stepOpen" :opportunity="opportunity" @done="closed" />
</template>

<style>
/* 抽屉挂在 body 下：手机上不超出屏幕。 */
.opp-drawer.el-drawer {
  max-width: 100vw;
}
</style>

<style scoped>
.body {
  min-height: 200px;
}

.head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.amount {
  font-weight: 600;
}

.customer {
  margin-top: 6px;
}

.link {
  margin-left: 10px;
}

.stage-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 14px;
}

.step {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 12px;
  font: inherit;
  font-size: 13px;
  color: var(--el-text-color-regular);
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 16px;
  cursor: pointer;
}

.step:disabled {
  cursor: default;
}

.step.done {
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  border-color: var(--el-color-primary-light-7);
}

.step.current {
  color: #fff;
  background: var(--el-color-primary);
  border-color: var(--el-color-primary);
}

.step.current.closed {
  background: var(--el-color-success);
  border-color: var(--el-color-success);
}

.step.todo:not(:disabled):hover {
  border-color: var(--el-color-primary);
}

.dot {
  width: 8px;
  height: 8px;
  background: var(--el-border-color);
  border-radius: 50%;
}

.step.current .dot {
  background: #fff;
}

.stage-info {
  margin-top: 8px;
}

.inline-tag {
  margin-left: 4px;
}

.overdue,
.due.overdue {
  color: var(--el-color-danger);
}

.due.today {
  color: var(--el-color-warning);
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}

.actions .el-button + .el-button {
  margin-left: 0;
}

.block {
  margin-top: 16px;
}

.decide {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}

.columns {
  display: grid;
  grid-template-columns: minmax(0, 5fr) minmax(0, 6fr);
  gap: 24px;
  margin-top: 16px;
}

@media (max-width: 760px) {
  .columns {
    grid-template-columns: 1fr;
  }
}

.column {
  min-width: 0;
}

h4 {
  margin: 0 0 10px;
  font-size: 14px;
}

h4.gap,
section.gap {
  margin-top: 16px;
}

.amount-input,
.prob-input {
  width: 140px;
}

.unit {
  margin-left: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.products {
  width: 100%;
}

.product {
  display: flex;
  gap: 6px;
  align-items: center;
  margin-bottom: 6px;
}

.product-name {
  flex: 1;
}

.product-qty {
  width: 80px;
}

.product-add {
  display: flex;
  gap: 8px;
  align-items: center;
}

.product-pick {
  width: 180px;
}

.facts {
  display: grid;
  grid-template-columns: 76px 1fr;
  gap: 6px 12px;
  margin: 4px 0 0;
  font-size: 13px;
}

.facts dt {
  color: var(--el-text-color-secondary);
  text-align: right;
}

.facts dd {
  margin: 0;
}

.todo + .todo {
  margin-top: 4px;
}

.ai-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.ai-buttons .el-button + .el-button {
  margin-left: 0;
}

.message {
  margin-top: 10px;
}

.message-foot,
.follow-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 8px;
}

.digest {
  margin-top: 10px;
  padding: 10px 12px;
  font-size: 13px;
  line-height: 1.7;
  background: var(--el-fill-color-light);
  border-radius: 6px;
}

.methods {
  margin-bottom: 8px;
}

.entry {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.content {
  margin: 6px 0 2px;
  white-space: pre-wrap;
}

.muted {
  color: var(--el-text-color-secondary);
}

.small {
  font-size: 12px;
  font-weight: normal;
}
</style>
