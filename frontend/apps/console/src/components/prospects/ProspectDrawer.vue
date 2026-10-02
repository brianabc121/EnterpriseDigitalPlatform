<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import {
  actionsOf,
  dueText,
  isoDate,
  LEVEL_LABEL,
  LEVEL_TAG,
  LEVELS,
  METHOD_LABEL,
  METHODS,
  SOURCE_TEXT,
  STATUS_LABEL,
  STATUS_TAG,
  type FollowMethod,
  type Prospect,
  type ProspectLevel,
} from '../../prospects'
import { useAuthStore } from '../../stores/auth'
import SessionDrawer from '../sessions/SessionDrawer.vue'

/**
 * 意向详情（设计文档 §35.4、§35.5）：意向信息（可以修改）、依据的会话、跟进记录和"记一次跟进"、
 * "AI 写跟进话术"、成交 / 放弃 / 重新跟进、确认或忽略 AI 的建议。
 */
const props = defineProps<{ prospectId: string | null }>()
const emit = defineEmits<{ close: []; changed: [] }>()

const auth = useAuthStore()
const prospect = ref<Prospect | null>(null)
const loading = ref(false)
const busy = ref('')
const staff = ref<Schemas['StaffOut'][]>([])
const viewingSession = ref<string | null>(null)
const edit = reactive({
  level: 'medium' as ProspectLevel,
  interest: '',
  concerns: '',
  nextFollowAt: '',
  followerId: '',
})
const follow = reactive({ method: 'phone' as FollowMethod, content: '', nextFollowAt: '' })
const message = reactive({ text: '', knowledge: [] as string[], shown: false })

const open = computed({
  get: () => props.prospectId !== null,
  set: (value) => {
    if (!value) emit('close')
  },
})
const actions = computed(() => actionsOf(prospect.value?.status ?? 'dismissed'))
const editable = computed(() => prospect.value?.status === 'active' || prospect.value?.status === 'suggested')
const canPickFollower = computed(
  () => !!prospect.value?.can_assign && auth.can('staff:read'),
)
const today = computed(() => isoDate(new Date()))
const staffNames = computed(() => new Map(staff.value.map((s) => [s.id, s.display_name])))

function fill(data: Prospect): void {
  prospect.value = data
  Object.assign(edit, {
    level: data.level,
    interest: data.interest ?? '',
    concerns: data.concerns ?? '',
    nextFollowAt: data.next_follow_at ?? '',
    followerId: data.follower_id ?? '',
  })
}

async function load(): Promise<void> {
  if (!props.prospectId) return
  loading.value = true
  const { data, error } = await api.GET('/api/v1/prospects/{prospect_id}', {
    params: { path: { prospect_id: props.prospectId } },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    emit('close')
    return
  }
  fill(data)
  if (canPickFollower.value && staff.value.length === 0) {
    const { data: list } = await api.GET('/api/v1/staff')
    staff.value = list?.items.filter((s) => s.status === 'active') ?? []
  }
}

function path() {
  return { params: { path: { prospect_id: props.prospectId ?? '' } } }
}

async function done(
  action: string,
  request: Promise<{ data?: Prospect; error?: unknown }>,
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

async function saveInfo(): Promise<void> {
  const current = prospect.value
  if (!current) return
  const body: Schemas['ProspectUpdate'] = {}
  if (edit.level !== current.level) body.level = edit.level
  if (edit.interest.trim() !== (current.interest ?? '')) body.interest = edit.interest
  if (edit.concerns.trim() !== (current.concerns ?? '')) body.concerns = edit.concerns
  if ((edit.nextFollowAt || null) !== current.next_follow_at) body.next_follow_at = edit.nextFollowAt || null
  if (canPickFollower.value && (edit.followerId || null) !== current.follower_id) {
    body.follower_id = edit.followerId || null
  }
  if (Object.keys(body).length === 0) {
    ElMessage.info('没有修改')
    return
  }
  await done('save', api.PATCH('/api/v1/prospects/{prospect_id}', { ...path(), body }), '已保存')
}

async function addFollowup(): Promise<void> {
  if (!follow.content.trim()) {
    ElMessage.warning('请填写跟进的内容')
    return
  }
  const saved = await done(
    'follow',
    api.POST('/api/v1/prospects/{prospect_id}/followups', {
      ...path(),
      body: {
        method: follow.method,
        content: follow.content.trim(),
        next_follow_at: follow.nextFollowAt || null,
      },
    }),
    '已记一次跟进',
  )
  if (saved) Object.assign(follow, { content: '', nextFollowAt: '' })
}

async function markWon(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '这个客户的订单确认后会自动标记成交；没有在系统里下单时可以手动标记。',
      '标记成交',
      { confirmButtonText: '标记成交', cancelButtonText: '取消', type: 'success' },
    )
  } catch {
    return
  }
  await done('won', api.POST('/api/v1/prospects/{prospect_id}/won', path()), '已标记成交')
}

async function markLost(): Promise<void> {
  let reason: string
  try {
    const result = await ElMessageBox.prompt('写下放弃的原因，方便以后再跟进时参考。', '放弃跟进', {
      confirmButtonText: '放弃',
      cancelButtonText: '取消',
      inputPlaceholder: '例如：已经买了别家的',
      inputPattern: /\S/,
      inputErrorMessage: '请填写原因',
    })
    reason = (result as { value: string }).value.trim().slice(0, 500)
  } catch {
    return
  }
  await done(
    'lost',
    api.POST('/api/v1/prospects/{prospect_id}/lost', { ...path(), body: { reason } }),
    '已放弃跟进',
  )
}

async function reopen(): Promise<void> {
  await done('reopen', api.POST('/api/v1/prospects/{prospect_id}/reopen', path()), '已重新跟进')
}

async function accept(): Promise<void> {
  await done('accept', api.POST('/api/v1/prospects/{prospect_id}/accept', path()), '已转入意向客户')
}

async function dismiss(): Promise<void> {
  busy.value = 'dismiss'
  const { error, response } = await api.POST('/api/v1/prospects/{prospect_id}/dismiss', path())
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
  const { data, error } = await api.POST('/api/v1/prospects/{prospect_id}/message', path())
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

watch(
  () => props.prospectId,
  (id) => {
    prospect.value = null
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
    :title="prospect ? `意向客户：${prospect.customer_name}` : '意向客户'"
    size="560px"
    append-to-body
    data-testid="prospect-drawer"
  >
    <div v-loading="loading" class="body">
      <template v-if="prospect">
        <div class="head">
          <el-tag :type="STATUS_TAG[prospect.status]" data-testid="prospect-status">
            {{ STATUS_LABEL[prospect.status] }}
          </el-tag>
          <el-tag :type="LEVEL_TAG[prospect.level]" effect="plain">
            意向{{ LEVEL_LABEL[prospect.level] }}
          </el-tag>
          <span class="muted">
            {{ SOURCE_TEXT[prospect.source] }}
            <template v-if="prospect.created_by_name">（{{ prospect.created_by_name }}）</template>
            · {{ formatDateTime(prospect.created_at) }}
          </span>
        </div>
        <div v-if="prospect.customer_company" class="muted company">{{ prospect.customer_company }}</div>

        <el-alert
          v-if="actions.decide"
          type="warning"
          :closable="false"
          show-icon
          title="AI 建议把这位客户转入意向客户"
          class="block"
          data-testid="prospect-suggestion"
        >
          <template #default>
            <div>确认后进入跟进名单；忽略后 30 天内 AI 不再建议这位客户。</div>
            <div class="decide">
              <el-button
                type="primary"
                size="small"
                :loading="busy === 'accept'"
                data-testid="prospect-accept"
                @click="accept"
              >
                确认转入
              </el-button>
              <el-button size="small" :loading="busy === 'dismiss'" data-testid="prospect-dismiss" @click="dismiss">
                忽略
              </el-button>
            </div>
          </template>
        </el-alert>

        <section class="block">
          <h4>意向</h4>
          <el-form label-width="76px" size="small" :disabled="!editable">
            <el-form-item label="意向等级">
              <el-radio-group v-model="edit.level" data-testid="prospect-edit-level">
                <el-radio-button v-for="level in LEVELS" :key="level" :value="level">
                  {{ LEVEL_LABEL[level] }}
                </el-radio-button>
              </el-radio-group>
            </el-form-item>
            <el-form-item label="想要什么">
              <el-input
                v-model="edit.interest"
                type="textarea"
                :autosize="{ minRows: 2, maxRows: 5 }"
                maxlength="1000"
                data-testid="prospect-edit-interest"
              />
            </el-form-item>
            <el-form-item label="顾虑">
              <el-input
                v-model="edit.concerns"
                type="textarea"
                :autosize="{ minRows: 1, maxRows: 4 }"
                maxlength="1000"
                data-testid="prospect-edit-concerns"
              />
            </el-form-item>
            <el-form-item label="下次跟进">
              <el-date-picker
                v-model="edit.nextFollowAt"
                type="date"
                value-format="YYYY-MM-DD"
                data-testid="prospect-edit-next"
              />
              <span
                v-if="prospect.next_follow_at"
                class="due"
                :class="{ overdue: prospect.overdue, today: prospect.due_today }"
                data-testid="prospect-due"
              >
                {{ dueText(prospect.next_follow_at, today) }}
              </span>
            </el-form-item>
            <el-form-item label="跟进人">
              <el-select
                v-if="canPickFollower"
                v-model="edit.followerId"
                clearable
                placeholder="没有跟进人"
                data-testid="prospect-edit-follower"
              >
                <el-option v-for="s in staff" :key="s.id" :label="s.display_name" :value="s.id" />
              </el-select>
              <span v-else>{{ prospect.follower_name ?? '没有跟进人' }}</span>
            </el-form-item>
            <el-form-item v-if="editable">
              <el-button
                type="primary"
                :loading="busy === 'save'"
                data-testid="prospect-save"
                @click="saveInfo"
              >
                保存修改
              </el-button>
            </el-form-item>
          </el-form>
          <dl class="facts">
            <template v-if="prospect.session_id">
              <dt>依据的会话</dt>
              <dd>
                <el-button
                  link
                  type="primary"
                  size="small"
                  data-testid="prospect-session"
                  @click="viewingSession = prospect.session_id"
                >
                  查看会话
                </el-button>
              </dd>
            </template>
            <dt>跟进</dt>
            <dd>
              {{ prospect.follow_count }} 次
              <template v-if="prospect.last_followed_at">
                ，最近 {{ formatDateTime(prospect.last_followed_at) }}
              </template>
            </dd>
            <template v-if="prospect.order_no">
              <dt>成交订单</dt>
              <dd>
                <router-link :to="`/orders?id=${prospect.order_id}`" data-testid="prospect-order">
                  {{ prospect.order_no }}
                </router-link>
              </dd>
            </template>
            <template v-if="prospect.lost_reason">
              <dt>放弃原因</dt>
              <dd data-testid="prospect-lost-reason">{{ prospect.lost_reason }}</dd>
            </template>
            <template v-if="prospect.closed_at">
              <dt>{{ prospect.status === 'won' ? '成交时间' : '关闭时间' }}</dt>
              <dd>{{ formatDateTime(prospect.closed_at) }}</dd>
            </template>
          </dl>
          <div class="actions">
            <el-button
              v-if="actions.close"
              type="success"
              plain
              :loading="busy === 'won'"
              data-testid="prospect-won"
              @click="markWon"
            >
              标记成交
            </el-button>
            <el-button
              v-if="actions.close"
              plain
              :loading="busy === 'lost'"
              data-testid="prospect-lost"
              @click="markLost"
            >
              放弃
            </el-button>
            <el-button
              v-if="actions.reopen"
              type="primary"
              plain
              :loading="busy === 'reopen'"
              data-testid="prospect-reopen"
              @click="reopen"
            >
              重新跟进
            </el-button>
          </div>
        </section>

        <section v-if="actions.follow" class="block">
          <h4>
            AI 写跟进话术
            <span class="muted small">按想要什么、顾虑、最近的跟进和知识库写，修改后自己发给客户</span>
          </h4>
          <el-button
            :loading="busy === 'message'"
            data-testid="prospect-write-message"
            @click="writeMessage"
          >
            {{ message.shown ? '重新写一段' : 'AI 写跟进话术' }}
          </el-button>
          <template v-if="message.shown">
            <el-input
              v-model="message.text"
              type="textarea"
              :autosize="{ minRows: 3, maxRows: 8 }"
              class="message"
              data-testid="prospect-message"
            />
            <div class="message-foot">
              <span v-if="message.knowledge.length" class="muted small" data-testid="prospect-message-refs">
                参考：{{ message.knowledge.join('、') }}
              </span>
              <el-button size="small" data-testid="prospect-copy-message" @click="copyMessage">
                复制
              </el-button>
            </div>
          </template>
        </section>

        <section v-if="actions.follow" class="block" data-testid="prospect-follow-form">
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
            data-testid="prospect-follow-content"
          />
          <div class="follow-foot">
            <el-date-picker
              v-model="follow.nextFollowAt"
              type="date"
              size="small"
              value-format="YYYY-MM-DD"
              placeholder="下次跟进（不填按默认天数）"
              data-testid="prospect-follow-next"
            />
            <el-button
              type="primary"
              size="small"
              :loading="busy === 'follow'"
              data-testid="prospect-follow-save"
              @click="addFollowup"
            >
              记一次跟进
            </el-button>
          </div>
        </section>

        <section class="block">
          <h4>跟进记录</h4>
          <el-timeline v-if="prospect.followups.length" data-testid="prospect-followups">
            <el-timeline-item
              v-for="item in prospect.followups"
              :key="item.id"
              :timestamp="formatDateTime(item.created_at)"
              placement="top"
            >
              <div class="entry">
                <el-tag size="small" effect="plain">{{ METHOD_LABEL[item.method] }}</el-tag>
                <span class="muted small">{{ item.staff_name ?? '系统' }}</span>
                <el-button
                  v-if="item.session_id"
                  link
                  type="primary"
                  size="small"
                  @click="viewingSession = item.session_id"
                >
                  查看会话
                </el-button>
              </div>
              <p class="content">{{ item.content }}</p>
              <div v-if="item.next_follow_at" class="muted small">下次跟进 {{ item.next_follow_at }}</div>
            </el-timeline-item>
          </el-timeline>
          <el-empty v-else :image-size="48" description="还没有跟进记录" />
        </section>
      </template>
    </div>
  </el-drawer>
  <SessionDrawer
    :session-id="viewingSession"
    :staff-names="staffNames"
    @close="viewingSession = null"
  />
</template>

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

.company {
  margin-top: 4px;
}

.block {
  margin-top: 16px;
}

h4 {
  margin: 0 0 10px;
  font-size: 14px;
}

.decide {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}

.due {
  margin-left: 10px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.due.today {
  color: var(--el-color-warning);
}

.due.overdue {
  color: var(--el-color-danger);
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

.actions {
  display: flex;
  gap: 8px;
  margin-top: 12px;
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

.methods {
  margin-bottom: 8px;
}

.entry {
  display: flex;
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
