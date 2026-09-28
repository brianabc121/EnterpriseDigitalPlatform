<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'

import { api, formatDateTime } from '../../api'

const props = defineProps<{ customerId: string }>()

const customer = ref<Schemas['CustomerDetail'] | null>(null)
const history = ref<Schemas['SessionOut'][]>([])
const saving = ref(false)
const newTag = ref('')
const form = reactive({ displayName: '', notes: '' })

const CLOSE_REASON: Record<string, string> = {
  agent: '坐席结束',
  idle_timeout: '超时结束',
  leave_message: '转为留言',
  ai_resolved: 'AI 已解决',
}

async function load(): Promise<void> {
  const [detail, sessions] = await Promise.all([
    api.GET('/api/v1/customers/{customer_id}', {
      params: { path: { customer_id: props.customerId } },
    }),
    api.GET('/api/v1/sessions', {
      params: { query: { customer_id: props.customerId, limit: 5 } },
    }),
  ])
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

      <section v-for="identity in customer.identities" :key="identity.id" class="block">
        <h3>{{ identity.channel_name }}（{{ identity.channel_type }}）</h3>
        <dl>
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
</style>
