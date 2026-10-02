<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { api, formatDateTime } from '../../api'
import {
  aiUsage,
  amountText,
  categoryOptions,
  categoryPath,
  categoryTree,
  contractBlocks,
  EXPIRY_LABEL,
  missing,
  placeholders,
  STATUS_LABEL,
  STATUS_TAG,
  type Category,
  type Contract,
  type TemplateField,
} from '../../contracts'
import { downloadBlob } from '../../download'
import { money } from '../../orders'
import { contractHtml, printHtml } from '../../print'
import { useAuthStore } from '../../stores/auth'
import HistoryDrawer from '../history/HistoryDrawer.vue'
import ContractPreview from './ContractPreview.vue'
import SignDialog from './SignDialog.vue'

/**
 * 合同的编辑页（§34.4）：上方是合同信息、AI 的提醒和依据（点开看知识原文）、填写项（待填写的标红）；
 * 下方左边是正文（简单 Markdown），右边是排好版的预览。草稿可以修改、定稿；已定稿的可以退回修改、
 * 登记签署；都可以导出 Word、打印、另存为模板、看修改历史，没有定稿的可以作废。
 */
const props = defineProps<{ contractId: string | null; categories: Category[]; builtin: TemplateField[] }>()
const emit = defineEmits<{ close: []; changed: [] }>()

type Customer = Schemas['CustomerOut']
type OrderRow = Schemas['OrderOut']
type ContractUpdate = Schemas['ContractUpdate']

const CUSTOM_FIELD = '__custom__'
const TEMPLATE_HINT = '填写项保留成 {{名称}}，用这个模板新建合同时再填写。'
const SYNTAX_HELP =
  '# 合同名称，## 条款标题，### 小标题；每行一段；"- " 开头是列表；"|" 开头的几行是表格；' +
  '**加粗**；{{名称}} 是填写项。'

const auth = useAuthStore()
const router = useRouter()
const contract = ref<Contract | null>(null)
const loading = ref(false)
const saving = ref(false)
const acting = ref(false)
const historyOpen = ref(false)
const signOpen = ref(false)
const customers = ref<Customer[]>([])
const orders = ref<OrderRow[]>([])
const staff = ref<{ id: string; name: string }[]>([])
const searching = ref(false)
const bodyInput = ref<{ textarea?: HTMLTextAreaElement } | null>(null)
const asTemplate = reactive({ open: false, name: '', category: [] as string[], description: '' })

const form = reactive({
  title: '',
  category: [] as string[],
  customerId: '',
  orderId: '',
  ownerId: '',
  amount: '',
  startDate: '',
  endDate: '',
  body: '',
  values: {} as Record<string, string>,
})
const saved = ref('')

const open = computed({
  get: () => props.contractId !== null,
  set: (value) => {
    if (!value) emit('close')
  },
})
const editable = computed(() => contract.value?.can_edit === true)
const handler = computed(() => contract.value?.can_manage === true)
const manageAll = computed(() => auth.can('contract:manage'))
const options = computed(() => categoryOptions(categoryTree(props.categories)))
const builtinByName = computed(() => new Map(props.builtin.map((f) => [f.name, f])))

/** 正文里的填写项（按出现的先后）：说明来自模板或内置填写项。 */
const fieldList = computed(() => {
  const known = new Map((contract.value?.fields ?? []).map((f) => [f.name, f]))
  return placeholders(form.body).map((name) => {
    const builtin = builtinByName.value.get(name)
    const field = known.get(name) ?? builtin
    return {
      name,
      hint: field?.hint ?? '',
      builtin: builtin !== undefined || field?.builtin === true,
      multiline: name === '标的清单' || (form.values[name] ?? '').includes('\n'),
    }
  })
})
const missingNames = computed(() => missing(form.body, form.values))

function cleaned(values: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(values).filter(([, v]) => v.trim()))
}

function state(): string {
  return JSON.stringify({ ...form, values: cleaned(form.values) })
}

const dirty = computed(() => contract.value !== null && state() !== saved.value)

function fill(c: Contract): void {
  contract.value = c
  form.title = c.title
  form.category = categoryPath(props.categories, c.category_id)
  form.customerId = c.customer_id ?? ''
  form.orderId = c.order_id ?? ''
  form.ownerId = c.owner_id ?? ''
  form.amount = c.amount ?? ''
  form.startDate = c.start_date ?? ''
  form.endDate = c.end_date ?? ''
  form.body = c.body
  form.values = { ...c.field_values }
  if (c.customer_id && !customers.value.some((x) => x.id === c.customer_id)) {
    customers.value = [{ id: c.customer_id, display_name: c.customer_name ?? '客户' } as Customer, ...customers.value]
  }
  if (c.owner_id && !staff.value.some((s) => s.id === c.owner_id)) {
    staff.value = [{ id: c.owner_id, name: c.owner_name ?? '负责人' }, ...staff.value]
  }
  saved.value = state()
}

async function load(): Promise<void> {
  const id = props.contractId
  if (!id) return
  loading.value = true
  const { data, error } = await api.GET('/api/v1/contracts/{contract_id}', {
    params: { path: { contract_id: id } },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    emit('close')
    return
  }
  customers.value = []
  orders.value = []
  fill(data)
  if (data.can_edit) await loadOrders()
}

async function loadStaff(): Promise<void> {
  if (!manageAll.value || !auth.can('staff:read')) return
  const { data } = await api.GET('/api/v1/staff')
  const list = (data?.items ?? []).filter((s) => s.status === 'active').map((s) => ({ id: s.id, name: s.display_name }))
  const current = staff.value.filter((s) => !list.some((x) => x.id === s.id))
  staff.value = [...current, ...list]
}

async function searchCustomers(q: string): Promise<void> {
  searching.value = true
  const { data } = await api.GET('/api/v1/customers', { params: { query: { q: q || undefined, limit: 20 } } })
  searching.value = false
  const found = data?.items ?? []
  const current = customers.value.find((c) => c.id === form.customerId)
  customers.value = current && !found.some((c) => c.id === current.id) ? [current, ...found] : found
}

async function loadOrders(): Promise<void> {
  if (!form.customerId || !auth.can('order:read')) {
    orders.value = []
    return
  }
  const { data } = await api.GET('/api/v1/orders', {
    params: { query: { customer_id: form.customerId, limit: 50 } },
  })
  orders.value = (data?.items ?? []).filter((o) => o.status !== 'cancelled' || o.id === form.orderId)
}

function changeCustomer(): void {
  form.orderId = ''
  void loadOrders()
}

watch(
  () => props.contractId,
  (id) => {
    contract.value = null
    if (id) {
      void load()
      void loadStaff()
    }
  },
  { immediate: true },
)

async function beforeClose(done: () => void): Promise<void> {
  if (dirty.value) {
    try {
      await ElMessageBox.confirm('有没有保存的修改，关闭后会丢失。', '关闭', {
        confirmButtonText: '不保存，关闭',
        cancelButtonText: '继续编辑',
        type: 'warning',
      })
    } catch {
      return
    }
  }
  done()
}

/** 只提交改过的：草稿改正文、填写项、客户、订单、金额、日期；分类、负责人什么状态都可以改。 */
function changes(c: Contract): ContractUpdate {
  const body: ContractUpdate = {}
  const category = form.category.at(-1) ?? null
  if (category !== c.category_id) body.category_id = category
  if (form.ownerId && form.ownerId !== c.owner_id) body.owner_id = form.ownerId
  if (c.status !== 'draft') return body
  if (form.title.trim() && form.title.trim() !== c.title) body.title = form.title.trim()
  if ((form.customerId || null) !== c.customer_id) body.customer_id = form.customerId || null
  if ((form.orderId || null) !== c.order_id) body.order_id = form.orderId || null
  const amount = String(form.amount ?? '').trim()
  if ((amount || null) !== (c.amount ?? null)) body.amount = amount || null
  if ((form.startDate || null) !== c.start_date) body.start_date = form.startDate || null
  if ((form.endDate || null) !== c.end_date) body.end_date = form.endDate || null
  if (form.body !== c.body) body.body = form.body
  const values = cleaned(form.values)
  if (JSON.stringify(values) !== JSON.stringify(cleaned(c.field_values))) body.field_values = values
  return body
}

async function save(notify = true): Promise<boolean> {
  const c = contract.value
  if (!c) return false
  const body = changes(c)
  if (!Object.keys(body).length) {
    saved.value = state()
    return true
  }
  if (body.amount && !/^\d+(\.\d{1,2})?$/.test(body.amount as string)) {
    ElMessage.warning('金额只能是数字，最多两位小数')
    return false
  }
  saving.value = true
  const { data, error } = await api.PATCH('/api/v1/contracts/{contract_id}', {
    params: { path: { contract_id: c.id } },
    body,
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return false
  }
  fill(data)
  if (notify) ElMessage.success('已保存')
  emit('changed')
  return true
}

function done(data: Contract | undefined, error: unknown, message: string): boolean {
  if (!data) {
    ElMessage.error(errorMessage(error))
    return false
  }
  fill(data)
  ElMessage.success(message)
  emit('changed')
  return true
}

async function finalize(): Promise<void> {
  const c = contract.value
  if (!c || !(await save(false))) return
  const left = contract.value?.missing ?? []
  if (left.length) {
    ElMessage.warning(`还有没填的填写项：${left.join('、')}`)
    return
  }
  acting.value = true
  const { data, error } = await api.POST('/api/v1/contracts/{contract_id}/finalize', {
    params: { path: { contract_id: c.id } },
  })
  acting.value = false
  done(data, error, '已定稿，正文锁定')
}

async function reopen(): Promise<void> {
  const c = contract.value
  if (!c) return
  acting.value = true
  const { data, error } = await api.POST('/api/v1/contracts/{contract_id}/reopen', {
    params: { path: { contract_id: c.id } },
  })
  acting.value = false
  done(data, error, '已退回修改')
}

async function voidContract(): Promise<void> {
  const c = contract.value
  if (!c) return
  let reason: string
  try {
    const result = await ElMessageBox.prompt('写下作废的原因。合同和修改历史都会保留。', '作废合同', {
      confirmButtonText: '作废',
      cancelButtonText: '取消',
      inputPlaceholder: '例如：客户取消合作',
      inputPattern: /\S/,
      inputErrorMessage: '请填写原因',
    })
    reason = (result as { value: string }).value.trim().slice(0, 500)
  } catch {
    return
  }
  acting.value = true
  const { data, error } = await api.POST('/api/v1/contracts/{contract_id}/void', {
    params: { path: { contract_id: c.id } },
    body: { reason },
  })
  acting.value = false
  done(data, error, '已作废')
}

async function remove(): Promise<void> {
  const c = contract.value
  if (!c) return
  try {
    await ElMessageBox.confirm(`删除草稿「${c.title}」？删除后不能恢复。`, '删除', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  acting.value = true
  const { error } = await api.DELETE('/api/v1/contracts/{contract_id}', {
    params: { path: { contract_id: c.id } },
  })
  acting.value = false
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已删除')
  saved.value = state()
  emit('changed')
  emit('close')
}

function onSigned(data: Contract): void {
  fill(data)
  emit('changed')
}

async function exportWord(): Promise<void> {
  const c = contract.value
  if (!c || (editable.value && !(await save(false)))) return
  const { data, error } = await api.GET('/api/v1/contracts/{contract_id}/docx', {
    params: { path: { contract_id: c.id } },
    parseAs: 'blob',
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  downloadBlob(data as Blob, `${c.no} ${c.title}.docx`)
}

function print(): void {
  const c = contract.value
  if (!c) return
  if (!printHtml(contractHtml(contractBlocks(form.body, form.values), `${c.no} ${form.title || c.title}`))) {
    ElMessage.warning('浏览器拦截了打印窗口，请允许弹出窗口后再试')
  }
}

async function openScan(): Promise<void> {
  const c = contract.value
  if (!c) return
  const { data, error } = await api.GET('/api/v1/contracts/{contract_id}/scan', {
    params: { path: { contract_id: c.id } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  window.open(data.url, '_blank', 'noopener')
}

function startSaveAsTemplate(): void {
  const c = contract.value
  if (!c) return
  asTemplate.name = `${form.title || c.title}模板`.slice(0, 128)
  asTemplate.category = categoryPath(props.categories, c.category_id)
  asTemplate.description = ''
  asTemplate.open = true
}

async function saveAsTemplate(): Promise<void> {
  const c = contract.value
  if (!c || !asTemplate.name.trim()) return
  if (editable.value && !(await save(false))) return
  acting.value = true
  const { data, error } = await api.POST('/api/v1/contracts/{contract_id}/save-as-template', {
    params: { path: { contract_id: c.id } },
    body: {
      name: asTemplate.name.trim(),
      category_id: asTemplate.category.at(-1) ?? null,
      description: asTemplate.description.trim() || null,
    },
  })
  acting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  asTemplate.open = false
  ElMessage.success(`已存为模板「${data.name}」`)
  emit('changed')
}

/** 在光标处插入 {{名称}}。 */
function insertField(name: string): void {
  const textarea = bodyInput.value?.textarea
  const token = `{{${name}}}`
  if (!textarea) {
    form.body += token
    return
  }
  const start = textarea.selectionStart ?? form.body.length
  const end = textarea.selectionEnd ?? start
  form.body = form.body.slice(0, start) + token + form.body.slice(end)
  requestAnimationFrame(() => {
    textarea.focus()
    textarea.setSelectionRange(start + token.length, start + token.length)
  })
}

function onInsert(command: string): void {
  if (command === CUSTOM_FIELD) void insertCustom()
  else insertField(command)
}

async function insertCustom(): Promise<void> {
  try {
    const result = await ElMessageBox.prompt('填写项的名称，例如"交货地点"。', '插入填写项', {
      confirmButtonText: '插入',
      cancelButtonText: '取消',
      inputPattern: /^[^{}\n]{1,40}$/,
      inputErrorMessage: '1–40 个字，不能有花括号',
    })
    insertField((result as { value: string }).value.trim())
  } catch {
    // 取消
  }
}

function knowledgeLink(id: string): string {
  return router.resolve({ path: '/knowledge', query: { item: id } }).href
}

function orderLink(id: string): string {
  return router.resolve({ path: '/orders', query: { id } }).href
}
</script>

<template>
  <el-drawer
    v-model="open"
    direction="rtl"
    size="100%"
    :before-close="beforeClose"
    class="contract-editor"
    data-testid="contract-editor"
  >
    <template #header>
      <div v-if="contract" class="head">
        <span class="no" data-testid="contract-no">{{ contract.no }}</span>
        <el-input
          v-if="editable"
          v-model="form.title"
          maxlength="200"
          class="title-input"
          placeholder="合同名称"
          data-testid="contract-title"
        />
        <h3 v-else class="title">{{ contract.title }}</h3>
        <el-tag :type="STATUS_TAG[contract.status]" data-testid="contract-status">{{ STATUS_LABEL[contract.status] }}</el-tag>
        <el-tag
          v-if="contract.expiry"
          :type="contract.expiry === 'expired' ? 'danger' : 'warning'"
          effect="plain"
          data-testid="contract-expiry"
          >{{ EXPIRY_LABEL[contract.expiry] }}</el-tag
        >
        <el-tag v-if="contract.ai_generated" type="info" effect="plain">AI 起草</el-tag>
        <span v-if="dirty" class="unsaved" data-testid="contract-unsaved">有修改未保存</span>
      </div>
    </template>

    <div v-loading="loading" class="editor">
      <template v-if="contract">
        <el-alert
          v-if="contract.status === 'void'"
          type="error"
          :closable="false"
          show-icon
          :title="`已作废：${contract.void_reason ?? ''}`"
          class="banner"
          data-testid="contract-void-reason"
        />
        <el-alert
          v-else-if="contract.status === 'final'"
          type="info"
          :closable="false"
          show-icon
          title="已定稿，正文已锁定。签署后登记签署；需要修改时先退回修改。"
          class="banner"
        />

        <section class="info" data-testid="contract-info">
          <el-form label-width="72px" size="small" class="info-form">
            <el-form-item label="分类">
              <el-cascader
                v-model="form.category"
                :options="options"
                :props="{ checkStrictly: true }"
                :disabled="!handler"
                clearable
                placeholder="未分类"
                class="wide"
                data-testid="contract-category"
              />
            </el-form-item>
            <el-form-item label="客户">
              <el-select
                v-if="editable"
                v-model="form.customerId"
                clearable
                filterable
                remote
                :remote-method="searchCustomers"
                :loading="searching"
                placeholder="输入名称或公司搜索"
                class="wide"
                data-testid="contract-customer"
                @change="changeCustomer"
              >
                <el-option
                  v-for="c in customers"
                  :key="c.id"
                  :label="c.company ? `${c.company}（${c.display_name}）` : c.display_name"
                  :value="c.id"
                />
              </el-select>
              <span v-else>{{ contract.customer_name ?? '—' }}</span>
            </el-form-item>
            <el-form-item label="订单">
              <el-select
                v-if="editable"
                v-model="form.orderId"
                clearable
                :disabled="!form.customerId"
                :placeholder="form.customerId ? '标的清单和金额取自订单' : '先选客户'"
                class="wide"
                data-testid="contract-order"
              >
                <el-option v-for="o in orders" :key="o.id" :label="`${o.no} · ${money(o.total)}`" :value="o.id" />
                <el-option
                  v-if="contract.order_id && !orders.some((o) => o.id === contract?.order_id)"
                  :label="contract.order_no ?? '订单'"
                  :value="contract.order_id"
                />
              </el-select>
              <a v-else-if="contract.order_id" :href="orderLink(contract.order_id)" target="_blank" rel="noopener">{{
                contract.order_no
              }}</a>
              <span v-else>—</span>
            </el-form-item>
            <el-form-item label="负责人">
              <el-select
                v-if="manageAll && staff.length > 1"
                v-model="form.ownerId"
                filterable
                class="wide"
                data-testid="contract-owner"
              >
                <el-option v-for="s in staff" :key="s.id" :label="s.name" :value="s.id" />
              </el-select>
              <span v-else>{{ contract.owner_name ?? '—' }}</span>
            </el-form-item>
            <el-form-item label="金额">
              <el-input
                v-if="editable"
                v-model="form.amount"
                placeholder="例如 12000"
                class="wide"
                data-testid="contract-amount"
              >
                <template #prefix>¥</template>
              </el-input>
              <span v-else>{{ amountText(contract.amount) }}</span>
            </el-form-item>
            <el-form-item label="期限" class="full-row">
              <template v-if="editable">
                <el-date-picker
                  v-model="form.startDate"
                  type="date"
                  value-format="YYYY-MM-DD"
                  placeholder="开始日期"
                  class="date"
                />
                <span class="to">至</span>
                <el-date-picker
                  v-model="form.endDate"
                  type="date"
                  value-format="YYYY-MM-DD"
                  placeholder="结束日期"
                  class="date"
                />
              </template>
              <span v-else>{{ contract.start_date ?? '—' }} 至 {{ contract.end_date ?? '长期' }}</span>
            </el-form-item>
          </el-form>
          <dl class="facts">
            <dt>签订日期</dt>
            <dd>{{ contract.sign_date ?? '—' }}</dd>
            <dt>模板</dt>
            <dd>{{ contract.template_name ?? '—' }}</dd>
            <dt>创建</dt>
            <dd>{{ contract.created_by_name ?? '—' }} · {{ formatDateTime(contract.created_at) }}</dd>
            <template v-if="contract.finalized_at">
              <dt>定稿</dt>
              <dd>{{ formatDateTime(contract.finalized_at) }}</dd>
            </template>
            <template v-if="contract.signed_at">
              <dt>登记签署</dt>
              <dd>{{ formatDateTime(contract.signed_at) }}</dd>
            </template>
            <template v-if="contract.scan_name">
              <dt>扫描件</dt>
              <dd>
                <el-button link type="primary" size="small" data-testid="contract-scan-open" @click="openScan">{{
                  contract.scan_name
                }}</el-button>
              </dd>
            </template>
          </dl>
        </section>

        <section v-if="contract.ai" class="ai" data-testid="contract-ai">
          <div class="block-head">
            <h4>AI 起草的依据</h4>
            <span class="muted">{{ aiUsage(contract.ai) }}</span>
          </div>
          <p v-if="contract.requirement" class="requirement">需求：{{ contract.requirement }}</p>
          <div v-if="contract.ai.notes?.length" class="notes" data-testid="contract-ai-notes">
            <p class="notes-title">需要确认：</p>
            <ul>
              <li v-for="(note, i) in contract.ai.notes" :key="i">{{ note }}</li>
            </ul>
          </div>
          <div v-if="contract.ai.knowledge?.length" class="refs" data-testid="contract-ai-knowledge">
            <span class="muted">参考的知识：</span>
            <a
              v-for="k in contract.ai.knowledge"
              :key="k.item_id"
              :href="knowledgeLink(k.item_id)"
              target="_blank"
              rel="noopener"
              class="ref"
              :class="{ used: k.used }"
              :title="k.used ? '起草时用到了' : '检索到，没有用到'"
            >
              <el-tag size="small" :type="k.policy ? 'warning' : 'info'" :effect="k.used ? 'light' : 'plain'"
                >{{ k.policy ? '规章制度' : '知识' }}</el-tag
              >
              {{ k.title }}<template v-if="k.version"> · 第 {{ k.version }} 版</template>
              <span v-if="k.used" class="used-mark">已引用</span>
            </a>
          </div>
        </section>

        <section v-if="fieldList.length" class="fields" data-testid="contract-fields">
          <div class="block-head">
            <h4>填写项</h4>
            <span v-if="missingNames.length" class="missing-count" data-testid="contract-missing-count"
              >{{ missingNames.length }} 项待填写</span
            >
            <span v-else class="muted">都已填写</span>
          </div>
          <div class="field-grid">
            <div
              v-for="f in fieldList"
              :key="f.name"
              class="field"
              :class="{ missing: missingNames.includes(f.name), multiline: f.multiline }"
              :data-testid="`contract-field-${f.name}`"
            >
              <label>
                {{ f.name }}
                <el-tag v-if="f.builtin" size="small" type="info" effect="plain" class="builtin">系统填写</el-tag>
              </label>
              <el-input
                v-model="form.values[f.name]"
                :type="f.multiline ? 'textarea' : 'text'"
                :autosize="f.multiline ? { minRows: 3, maxRows: 10 } : undefined"
                :disabled="!editable"
                :placeholder="f.builtin && !form.values[f.name] ? '保存时按数据填写' : f.hint || '待填写'"
                size="small"
              />
              <div v-if="f.hint" class="hint">{{ f.hint }}</div>
            </div>
          </div>
        </section>

        <section class="body" :class="{ split: editable }">
          <div v-if="editable" class="source">
            <div class="pane-head">
              <h4>正文</h4>
              <el-dropdown trigger="click" @command="onInsert">
                <el-button size="small" data-testid="contract-insert-field">插入填写项</el-button>
                <template #dropdown>
                  <el-dropdown-menu class="insert-menu">
                    <el-dropdown-item v-for="f in builtin" :key="f.name" :command="f.name">
                      {{ f.name }} <span class="muted">· {{ f.hint }}</span>
                    </el-dropdown-item>
                    <el-dropdown-item divided :command="CUSTOM_FIELD">其他填写项…</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
            <el-input
              ref="bodyInput"
              v-model="form.body"
              type="textarea"
              :autosize="{ minRows: 24 }"
              class="mono"
              data-testid="contract-body"
            />
            <p class="hint">{{ SYNTAX_HELP }}</p>
          </div>
          <div class="preview">
            <div class="pane-head">
              <h4>预览</h4>
              <span class="muted">和导出的 Word 同样排版</span>
            </div>
            <ContractPreview :body="form.body" :values="form.values" />
          </div>
        </section>
      </template>
    </div>

    <template v-if="contract" #footer>
      <div class="actions">
        <span class="left">
          <el-button data-testid="contract-history" @click="historyOpen = true">修改历史</el-button>
          <el-button :loading="saving" data-testid="contract-export" @click="exportWord">导出 Word</el-button>
          <el-button data-testid="contract-print" @click="print">打印</el-button>
          <el-button data-testid="contract-save-template" @click="startSaveAsTemplate">另存为模板</el-button>
        </span>
        <span>
          <el-button v-if="editable" type="danger" plain :disabled="acting" data-testid="contract-delete" @click="remove"
            >删除</el-button
          >
          <el-button
            v-if="handler && contract.status !== 'void'"
            :disabled="acting"
            data-testid="contract-void"
            @click="voidContract"
            >作废</el-button
          >
          <el-button
            v-if="editable || (handler && dirty)"
            :loading="saving"
            :disabled="acting || !dirty"
            data-testid="contract-save"
            @click="save()"
            >保存</el-button
          >
          <el-button
            v-if="editable"
            type="primary"
            :loading="acting"
            :disabled="saving"
            data-testid="contract-finalize"
            @click="finalize"
            >定稿</el-button
          >
          <el-button
            v-if="handler && contract.status === 'final'"
            :disabled="acting"
            data-testid="contract-reopen"
            @click="reopen"
            >退回修改</el-button
          >
          <el-button
            v-if="handler && contract.status === 'final'"
            type="primary"
            :disabled="acting"
            data-testid="contract-sign"
            @click="signOpen = true"
            >登记签署</el-button
          >
        </span>
      </div>
    </template>

    <SignDialog v-if="contract" v-model="signOpen" :contract="contract" @signed="onSigned" />
    <HistoryDrawer
      v-if="contract"
      v-model="historyOpen"
      record-type="contract"
      :record-id="contract.id"
      :title="`合同 ${contract.no}`"
    />
    <el-dialog v-model="asTemplate.open" title="另存为模板" width="460px" append-to-body data-testid="contract-template-dialog">
      <el-form label-width="72px">
        <el-form-item label="名称" required>
          <el-input v-model="asTemplate.name" maxlength="128" data-testid="contract-template-name" />
        </el-form-item>
        <el-form-item label="分类">
          <el-cascader
            v-model="asTemplate.category"
            :options="options"
            :props="{ checkStrictly: true }"
            clearable
            placeholder="未分类"
            class="wide"
          />
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="asTemplate.description" type="textarea" :rows="2" maxlength="1000" />
        </el-form-item>
      </el-form>
      <p class="hint">{{ TEMPLATE_HINT }}</p>
      <template #footer>
        <el-button @click="asTemplate.open = false">取消</el-button>
        <el-button type="primary" :loading="acting" data-testid="contract-template-submit" @click="saveAsTemplate"
          >保存</el-button
        >
      </template>
    </el-dialog>
  </el-drawer>
</template>

<style scoped>
.head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.no {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  color: var(--el-text-color-secondary);
}

.title {
  margin: 0;
  font-size: 18px;
}

.title-input {
  width: min(420px, 100%);
}

.unsaved {
  font-size: 12px;
  color: var(--el-color-warning);
}

.editor {
  min-height: 200px;
}

.banner {
  margin-bottom: 12px;
}

.info {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(220px, 1fr);
  gap: 12px 24px;
  margin-bottom: 12px;
}

.info-form {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  column-gap: 16px;
}

.info-form :deep(.el-form-item) {
  margin-bottom: 10px;
}

.wide {
  width: 100%;
}

.full-row {
  grid-column: 1 / -1;
}

.date {
  width: 180px !important;
}

.to {
  margin: 0 6px;
  color: var(--el-text-color-secondary);
}

.facts {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 4px 12px;
  margin: 0;
  font-size: 13px;
}

.facts dt {
  color: var(--el-text-color-secondary);
}

.facts dd {
  margin: 0;
}

.block-head,
.pane-head {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}

.block-head h4,
.pane-head h4 {
  margin: 0;
  font-size: 14px;
}

.ai,
.fields {
  margin-bottom: 12px;
  padding: 12px 16px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
}

.ai {
  background: var(--el-fill-color-lighter);
}

.requirement {
  margin: 0 0 8px;
  font-size: 13px;
}

.notes {
  margin-bottom: 8px;
  padding: 8px 12px;
  border-radius: 6px;
  background: var(--el-color-warning-light-9);
  font-size: 13px;
}

.notes-title {
  margin: 0 0 4px;
  font-weight: 500;
}

.notes ul {
  margin: 0;
  padding-left: 20px;
}

.refs {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 14px;
  font-size: 13px;
}

.ref {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--el-text-color-regular);
  text-decoration: none;
}

.ref:hover {
  color: var(--el-color-primary);
}

.ref:not(.used) {
  color: var(--el-text-color-secondary);
}

.used-mark {
  font-size: 12px;
  color: var(--el-color-success);
}

.missing-count {
  font-size: 13px;
  color: var(--el-color-danger);
}

.field-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 10px 16px;
}

.field.multiline {
  grid-column: 1 / -1;
}

.field label {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
  font-size: 13px;
}

.field.missing label {
  color: var(--el-color-danger);
}

.field.missing :deep(.el-input__wrapper),
.field.missing :deep(.el-textarea__inner) {
  box-shadow: 0 0 0 1px var(--el-color-danger-light-5) inset;
}

.builtin {
  transform: scale(0.9);
}

.body {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 16px;
}

.body.split {
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
}

.mono :deep(textarea) {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px;
  line-height: 1.7;
}

.hint {
  margin: 4px 0 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 8px;
}

.actions .left {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.actions .left .el-button + .el-button {
  margin-left: 0;
}

@media (max-width: 900px) {
  .info,
  .body.split {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
