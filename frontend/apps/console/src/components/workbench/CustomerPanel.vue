<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'

import { api, formatDateTime } from '../../api'
import ContactFields from '../customers/ContactFields.vue'
import CustomerOpportunity from '../opportunities/CustomerOpportunity.vue'
import CustomerWecomInfo from '../wecom/CustomerWecomInfo.vue'

const props = defineProps<{ customerId: string }>()

const customer = ref<Schemas['CustomerDetail'] | null>(null)
const history = ref<Schemas['SessionOut'][]>([])
const leads = ref<Schemas['LeadDraftOut'][]>([])
const summaries = ref<Schemas['CustomerSummaryOut'][]>([])
const deciding = ref<string | null>(null)

const LEAD_FIELDS: [keyof Schemas['LeadFields'], string][] = [
  ['name', '称呼'],
  ['company', '公司'],
  ['phone', '手机号'],
  ['email', '邮箱'],
  ['requirement', '需求'],
]
const saving = ref(false)
const newTag = ref('')
const form = reactive({ displayName: '', notes: '' })

const CHANNEL_TYPE: Record<string, string> = {
  web: '网页',
  wecom_kf: '微信客服',
  wecom_contact: '企业微信客户联系',
  email: '邮件',
}

const CLOSE_REASON: Record<string, string> = {
  agent: '坐席结束',
  idle_timeout: '超时结束',
  leave_message: '转为留言',
  ai_resolved: 'AI 已解决',
}

async function load(): Promise<void> {
  const path = { params: { path: { customer_id: props.customerId } } }
  const [detail, sessions, drafts, notes] = await Promise.all([
    api.GET('/api/v1/customers/{customer_id}', path),
    api.GET('/api/v1/sessions', {
      params: { query: { customer_id: props.customerId, limit: 5 } },
    }),
    api.GET('/api/v1/customers/{customer_id}/lead-drafts', path),
    api.GET('/api/v1/customers/{customer_id}/summaries', path),
  ])
  leads.value = drafts.data?.items ?? []
  summaries.value = notes.data?.items ?? []
  if (!detail.data) {
    ElMessage.error(errorMessage(detail.error))
    return
  }
  customer.value = detail.data
  form.displayName = detail.data.display_name
  form.notes = detail.data.notes ?? ''
  history.value = sessions.data?.items ?? []
}

async function save(changes: Schemas['CustomerUpdate']): Promise<void> {
  saving.value = true
  const { data, error } = await api.PATCH('/api/v1/customers/{customer_id}', {
    params: { path: { customer_id: props.customerId } },
    body: changes,
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  customer.value = data
  ElMessage.success('已保存')
}

async function saveProfile(): Promise<void> {
  if (!form.displayName.trim()) {
    ElMessage.warning('客户名称不能为空')
    return
  }
  await save({ display_name: form.displayName.trim(), notes: form.notes })
}

async function addTag(): Promise<void> {
  const tag = newTag.value.trim()
  newTag.value = ''
  if (!tag || !customer.value || customer.value.tags.includes(tag)) return
  await save({ tags: [...customer.value.tags, tag] })
}

async function removeTag(tag: string): Promise<void> {
  if (!customer.value) return
  await save({ tags: customer.value.tags.filter((t) => t !== tag) })
}

/** 确认（写入客户档案）或忽略 AI 登记的线索。 */
async function decideLead(draft: Schemas['LeadDraftOut'], confirm: boolean): Promise<void> {
  deciding.value = draft.id
  const path = { params: { path: { draft_id: draft.id } } }
  const { data, error } = confirm
    ? await api.POST('/api/v1/customers/lead-drafts/{draft_id}/confirm', path)
    : await api.POST('/api/v1/customers/lead-drafts/{draft_id}/discard', path)
  deciding.value = null
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(confirm ? '已写入客户档案' : '已忽略')
  await load()
}

/** 身份所在渠道的标题：渠道名称，渠道类型与名称不同时附上类型。 */
function identityTitle(identity: Schemas['CustomerIdentityOut']): string {
  const kind = CHANNEL_TYPE[identity.channel_type] ?? identity.channel_type
  return identity.channel_name === kind || identity.channel_name.includes(kind)
    ? identity.channel_name
    : `${identity.channel_name}（${kind}）`
}

function profileText(value: unknown): string {
  return typeof value === 'string' && value ? value : '—'
}

onMounted(load)
</script>

<template>
  <aside class="panel" data-testid="customer-panel">
    <template v-if="customer">
      <section class="block">
        <h3>客户资料</h3>
        <el-form label-position="top" size="small">
          <el-form-item label="名称">
            <el-input v-model="form.displayName" maxlength="128" data-testid="customer-name" />
          </el-form-item>
          <el-form-item label="归属坐席">
            <span>{{ customer.owner_display_name ?? '未分配' }}</span>
          </el-form-item>
          <el-form-item label="标签">
            <div class="tags">
              <el-tag
                v-for="tag in customer.tags"
                :key="tag"
                closable
                size="small"
                @close="removeTag(tag)"
              >
                {{ tag }}
              </el-tag>
              <el-input
                v-model="newTag"
                size="small"
                class="tag-input"
                placeholder="添加标签"
                maxlength="32"
                data-testid="customer-tag-input"
                @keyup.enter="addTag"
              />
            </div>
          </el-form-item>
          <el-form-item label="备注">
            <el-input
              v-model="form.notes"
              type="textarea"
              :rows="3"
              maxlength="4000"
              data-testid="customer-notes"
            />
          </el-form-item>
          <el-button
            type="primary"
            size="small"
            :loading="saving"
            data-testid="save-customer"
            @click="saveProfile"
          >
            保存
          </el-button>
        </el-form>
      </section>

      <CustomerOpportunity :customer-id="customer.id" :customer-name="customer.display_name" />

      <section v-if="leads.some((l) => l.status === 'pending')" class="block" data-testid="lead-drafts">
        <h3>AI 登记的线索 <span class="muted small">确认后写入客户档案</span></h3>
        <div v-for="lead in leads.filter((l) => l.status === 'pending')" :key="lead.id" class="lead">
          <dl>
            <template v-for="[key, label] in LEAD_FIELDS" :key="key">
              <template v-if="lead.fields[key]">
                <dt>{{ label }}</dt>
                <dd>{{ lead.fields[key] }}</dd>
              </template>
            </template>
          </dl>
          <div class="lead-actions">
            <el-button
              size="small"
              :disabled="deciding === lead.id"
              data-testid="lead-discard"
              @click="decideLead(lead, false)"
            >
              忽略
            </el-button>
            <el-button
              type="primary"
              size="small"
              :loading="deciding === lead.id"
              data-testid="lead-confirm"
              @click="decideLead(lead, true)"
            >
              确认写入
            </el-button>
          </div>
        </div>
      </section>

      <section class="block">
        <h3>联系方式</h3>
        <ContactFields :customer="customer" @saved="(c) => (customer = c)" />
      </section>

      <section v-for="identity in customer.identities" :key="identity.id" class="block">
        <h3>{{ identityTitle(identity) }}</h3>
        <dl v-if="identity.channel_type.startsWith('wecom')">
          <dt>微信昵称</dt>
          <dd>{{ profileText(identity.profile.nickname) }}</dd>
          <dt v-if="identity.profile.corp_name">企业</dt>
          <dd v-if="identity.profile.corp_name">{{ profileText(identity.profile.corp_name) }}</dd>
          <dt>unionid</dt>
          <dd>{{ identity.profile.unionid ? '已关联' : '—' }}</dd>
          <dt>最近联系</dt>
          <dd>{{ identity.last_seen_at ? formatDateTime(identity.last_seen_at) : '—' }}</dd>
        </dl>
        <dl v-else-if="identity.channel_type === 'email'" data-testid="email-identity">
          <dt>发件人名称</dt>
          <dd>{{ profileText(identity.profile.name) }}</dd>
          <dt>最近来信</dt>
          <dd>{{ identity.last_seen_at ? formatDateTime(identity.last_seen_at) : '—' }}</dd>
        </dl>
        <dl v-else>
          <dt>来源页面</dt>
          <dd>{{ profileText(identity.profile.first_page) }}</dd>
          <dt>来源</dt>
          <dd>{{ profileText(identity.profile.referrer) }}</dd>
          <dt>浏览器</dt>
          <dd class="ua">{{ profileText(identity.profile.user_agent) }}</dd>
          <dt>最近访问</dt>
          <dd>{{ identity.last_seen_at ? formatDateTime(identity.last_seen_at) : '—' }}</dd>
        </dl>
      </section>

      <CustomerWecomInfo
        v-if="customer.identities.some((i) => i.channel_type.startsWith('wecom'))"
        :customer-id="customer.id"
      />

      <section v-if="summaries.length" class="block" data-testid="customer-summaries">
        <h3>会话小结</h3>
        <div v-for="item in summaries" :key="item.session_id" class="summary">
          <div class="muted small">
            {{ item.confirmed_at ? formatDateTime(item.confirmed_at) : '' }}
            <el-tag v-for="t in item.tags" :key="t" size="small" type="info" class="summary-tag">{{
              t
            }}</el-tag>
          </div>
          <p>{{ item.summary }}</p>
        </div>
      </section>

      <section class="block">
        <h3>最近会话</h3>
        <div v-for="s in history" :key="s.id" class="history">
          <span>{{ formatDateTime(s.created_at) }}</span>
          <span class="muted">
            {{
              s.status === 'closed' ? (CLOSE_REASON[s.close_reason ?? ''] ?? '已结束') : '进行中'
            }}
            <template v-if="s.assignee_display_name"> · {{ s.assignee_display_name }}</template>
          </span>
        </div>
        <el-empty v-if="history.length === 0" :image-size="40" description="暂无" />
      </section>
    </template>
  </aside>
</template>

<style scoped>
.panel {
  overflow-y: auto;
  padding: 4px 12px 12px;
}

.block {
  border-bottom: 1px solid var(--el-border-color-lighter);
  padding: 8px 0;
}

.block:last-child {
  border-bottom: none;
}

h3 {
  font-size: 13px;
  font-weight: 600;
  margin: 4px 0 8px;
}

.tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.tag-input {
  width: 96px;
}

dl {
  display: grid;
  grid-template-columns: 64px 1fr;
  gap: 4px 8px;
  margin: 0;
  font-size: 12px;
}

dt {
  color: var(--el-text-color-secondary);
}

dd {
  margin: 0;
  word-break: break-all;
}

.ua {
  font-size: 11px;
}

.history {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  padding: 2px 0;
}

.muted {
  color: var(--el-text-color-secondary);
}

.small {
  font-size: 12px;
  font-weight: normal;
}

.lead {
  padding: 6px 8px;
  margin-bottom: 6px;
  border-radius: 4px;
  background: var(--el-color-primary-light-9);
}

.lead-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
  margin-top: 6px;
}

.summary p {
  margin: 2px 0 8px;
  font-size: 12px;
  white-space: pre-wrap;
}

.summary-tag {
  margin-left: 4px;
}
</style>
