<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import { useAuthStore } from '../../stores/auth'
import {
  describeEvent,
  dueState,
  PRIORITY,
  PRIORITY_TAG,
  REJECT_REASON,
  REJECT_REASONS,
  TODO_SOURCE,
  TODO_STATUS,
  TODO_STATUS_TAG,
  todosChanged,
  type Todo,
  type TodoDetail,
} from '../../todos'
import SessionDrawer from '../sessions/SessionDrawer.vue'
import ConfirmDialog from './ConfirmDialog.vue'

/** 待办详情：字段、依据的对话、动态与评论，以及按权限显示的处理操作。 */
const props = withDefaults(defineProps<{ todoId: string | null; size?: string }>(), {
  size: '640px',
})
const emit = defineEmits<{ close: []; changed: [] }>()

const auth = useAuthStore()
const detail = ref<TodoDetail | null>(null)
const loading = ref(false)
const acting = ref(false)
const viewing = ref<string | null>(null)
const revealed = ref<Schemas['FieldValue'][] | null>(null)
const options = ref<Schemas['AssigneeOptions']>({ staff: [], groups: [] })
const comment = reactive({ text: '', mentions: [] as string[] })
const note = ref('')

const confirmOpen = ref(false)
const reject = reactive({ open: false, reason: 'not_real' as Schemas['RejectReason'], note: '' })
const merge = reactive({ open: false, targetId: '', candidates: [] as Todo[] })
const done = reactive({ open: false, result: '', notify: true, notice: '' })
const assign = reactive({ open: false, mode: 'staff' as 'staff' | 'group', staffId: '', groupId: '', note: '' })
const reschedule = reactive({ open: false, dueAt: '', reason: '' })
const notice = reactive({ open: false, text: '' })

const open = computed({
  get: () => props.todoId !== null,
  set: (value) => {
    if (!value) emit('close')
  },
})
const names = computed(() => new Map(options.value.staff.map((s) => [s.id, s.name])))
const fields = computed(() => revealed.value ?? detail.value?.fields ?? [])
const hasSensitive = computed(() => detail.value?.fields.some((f) => f.sensitive) ?? false)
const due = computed(() => (detail.value ? dueState(detail.value) : null))
const active = computed(() =>
  ['open', 'in_progress', 'waiting'].includes(detail.value?.status ?? ''),
)
const canWork = computed(() => auth.can('todo:handle') || auth.can('todo:assign'))
const handler = computed(() => {
  const d = detail.value
  if (!d) return ''
  if (d.assignee_name) return d.assignee_name
  return d.skill_group_name ? `${d.skill_group_name}（待认领）` : '公共待认领池'
})

async function load(): Promise<void> {
  const id = props.todoId
  if (!id) return
  loading.value = true
  const { data, error } = await api.GET('/api/v1/todos/{todo_id}', {
    params: { path: { todo_id: id } },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    emit('close')
    return
  }
  detail.value = data
  note.value = data.progress_note ?? ''
}

watch(
  () => props.todoId,
  async (id) => {
    detail.value = null
    revealed.value = null
    if (!id) return
    await load()
    if (!options.value.staff.length) {
      const { data } = await api.GET('/api/v1/todos/assignees')
      if (data) options.value = data
    }
  },
  { immediate: true },
)

type Result = { data?: unknown; error?: unknown }

async function run(request: () => Promise<Result>, message: string): Promise<boolean> {
  acting.value = true
  const { data, error } = await request()
  acting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return false
  }
  ElMessage.success(message)
  todosChanged()
  await load()
  emit('changed')
  return true
}

const path = () => ({ params: { path: { todo_id: props.todoId ?? '' } } })

function simple(action: 'claim' | 'start' | 'resume', message: string): Promise<boolean> {
  const requests = {
    claim: () => api.POST('/api/v1/todos/{todo_id}/claim', path()),
    start: () => api.POST('/api/v1/todos/{todo_id}/start', path()),
    resume: () => api.POST('/api/v1/todos/{todo_id}/resume', path()),
  }
  return run(requests[action], message)
}

async function prompt(title: string, label: string): Promise<string | null> {
  try {
    const result = await ElMessageBox.prompt(label, title, {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      inputPattern: /\S/,
      inputErrorMessage: '请填写',
    })
    return (result as { value: string }).value.trim()
  } catch {
    return null
  }
}

async function wait(): Promise<void> {
  const text = await prompt('等待客户', '需要客户补充什么？（等待期间暂停计时）')
  if (text === null) return
  await run(() => api.POST('/api/v1/todos/{todo_id}/wait', { ...path(), body: { note: text } }), '已改为等待客户')
}

async function cancel(): Promise<void> {
  const reason = await prompt('取消待办', '取消原因')
  if (reason === null) return
  await run(() => api.POST('/api/v1/todos/{todo_id}/cancel', { ...path(), body: { reason } }), '已取消')
}

async function reopen(): Promise<void> {
  const reason = await prompt('重新打开', '客户再次提出了什么？')
  if (reason === null) return
  await run(() => api.POST('/api/v1/todos/{todo_id}/reopen', { ...path(), body: { reason } }), '已重新打开')
}

async function submitReject(): Promise<void> {
  const ok = await run(
    () =>
      api.POST('/api/v1/todos/{todo_id}/discard', {
        ...path(),
        body: { reason: reject.reason, note: reject.note.trim() || null },
      }),
    '已驳回',
  )
  if (ok) reject.open = false
}

async function openMerge(): Promise<void> {
  const d = detail.value
  if (!d?.customer_id) return
  const { data } = await api.GET('/api/v1/todos', {
    params: { query: { customer_id: d.customer_id, limit: 50 } },
  })
  merge.candidates = (data?.items ?? []).filter(
    (t) => t.id !== d.id && ['open', 'in_progress', 'waiting', 'done'].includes(t.status),
  )
  merge.targetId = merge.candidates[0]?.id ?? ''
  merge.open = true
}

async function submitMerge(): Promise<void> {
  if (!merge.targetId) return
  const ok = await run(
    () => api.POST('/api/v1/todos/{todo_id}/merge', { ...path(), body: { target_id: merge.targetId } }),
    '已合并',
  )
  if (ok) merge.open = false
}

function openDone(): void {
  Object.assign(done, { open: true, result: '', notify: !!detail.value?.customer_id, notice: '' })
}

async function submitDone(): Promise<void> {
  if (!done.result.trim()) {
    ElMessage.warning('请填写处理结果')
    return
  }
  const ok = await run(
    () =>
      api.POST('/api/v1/todos/{todo_id}/done', {
        ...path(),
        body: {
          result: done.result.trim(),
          notify_customer: done.notify,
          notice: done.notice.trim() || null,
        },
      }),
    done.notify ? '已完成，并通知客户' : '已完成',
  )
  if (ok) done.open = false
}

function openAssign(): void {
  Object.assign(assign, { open: true, mode: 'staff', staffId: '', groupId: '', note: '' })
}

async function submitAssign(): Promise<void> {
  const body =
    assign.mode === 'staff'
      ? { assignee_id: assign.staffId || null, note: assign.note.trim() || null }
      : { skill_group_id: assign.groupId || null, note: assign.note.trim() || null }
  const ok = await run(() => api.POST('/api/v1/todos/{todo_id}/assign', { ...path(), body }), '已转交')
  if (ok) assign.open = false
}

async function submitReschedule(): Promise<void> {
  if (!reschedule.dueAt || !reschedule.reason.trim()) {
    ElMessage.warning('请选择新的截止时间并填写原因')
    return
  }
  const ok = await run(
    () =>
      api.POST('/api/v1/todos/{todo_id}/reschedule', {
        ...path(),
        body: { due_at: reschedule.dueAt, reason: reschedule.reason.trim() },
      }),
    '已改期',
  )
  if (ok) reschedule.open = false
}

async function submitNotice(): Promise<void> {
  if (!notice.text.trim()) return
  acting.value = true
  const { data, error } = await api.POST('/api/v1/todos/{todo_id}/notify', {
    ...path(),
    body: { text: notice.text.trim() },
  })
  acting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  if (data.status === 'sent') ElMessage.success('已发送给客户')
  else ElMessage.warning(data.reason ?? '未能通知客户')
  notice.open = false
  await load()
}

async function saveNote(): Promise<void> {
  await run(
    () => api.PATCH('/api/v1/todos/{todo_id}', { ...path(), body: { progress_note: note.value } }),
    '进度说明已保存',
  )
}

async function sendComment(): Promise<void> {
  if (!comment.text.trim()) return
  const ok = await run(
    () =>
      api.POST('/api/v1/todos/{todo_id}/comments', {
        ...path(),
        body: { text: comment.text.trim(), mentions: comment.mentions },
      }),
    '已评论',
  )
  if (ok) Object.assign(comment, { text: '', mentions: [] })
}

async function reveal(): Promise<void> {
  const { data, error } = await api.POST('/api/v1/todos/{todo_id}/reveal', path())
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  revealed.value = data.fields
}
</script>

<template>
  <el-drawer v-model="open" :size="size" :title="detail ? detail.no : '待办'" data-testid="todo-drawer">
    <div v-loading="loading" class="body">
      <template v-if="detail">
        <div class="head">
          <el-tag>{{ detail.type_name }}</el-tag>
          <el-tag :type="TODO_STATUS_TAG[detail.status]" data-testid="todo-status">{{
            TODO_STATUS[detail.status]
          }}</el-tag>
          <el-tag :type="PRIORITY_TAG[detail.priority]" effect="plain"
            >优先级 {{ PRIORITY[detail.priority] }}</el-tag
          >
          <el-tag v-if="due === 'overdue'" type="danger">已逾期</el-tag>
          <el-tag v-if="detail.nudge_count" type="warning">客户催促 {{ detail.nudge_count }} 次</el-tag>
        </div>
        <h3 class="title" data-testid="todo-title-text">{{ detail.title }}</h3>
        <p v-if="detail.detail" class="text">{{ detail.detail }}</p>

        <section v-if="fields.length" class="block">
          <div class="block-head">
            <h4>字段</h4>
            <el-button
              v-if="hasSensitive && !revealed && auth.can('customer:view_sensitive')"
              link
              type="primary"
              size="small"
              data-testid="todo-reveal"
              @click="reveal"
            >
              查看完整信息（记审计）
            </el-button>
          </div>
          <dl>
            <template v-for="f in fields" :key="f.key">
              <dt>{{ f.label }}</dt>
              <dd :data-testid="`todo-field-${f.key}`">{{ f.value }}</dd>
            </template>
          </dl>
        </section>

        <section class="block">
          <dl>
            <dt>客户</dt>
            <dd>
              <router-link
                v-if="detail.customer_id"
                :to="`/customers?customer=${detail.customer_id}`"
                >{{ detail.customer_name }}</router-link
              >
              <span v-else>—</span>
            </dd>
            <template v-if="detail.order_id">
              <dt>关联订单</dt>
              <dd>
                <router-link :to="`/orders?id=${detail.order_id}`" data-testid="todo-order-link"
                  >查看订单</router-link
                >
              </dd>
            </template>
            <dt>来源</dt>
            <dd>
              {{ TODO_SOURCE[detail.source] ?? detail.source }}
              <span v-if="detail.confidence !== null" class="muted"
                >（AI 置信度 {{ Math.round((detail.confidence ?? 0) * 100) }}%）</span
              >
            </dd>
            <dt>{{ detail.status === 'pending' ? '确认人' : '处理人' }}</dt>
            <dd data-testid="todo-handler">{{ handler }}</dd>
            <dt>截止时间</dt>
            <dd :class="{ overdue: due === 'overdue' }">
              {{ detail.due_at ? formatDateTime(detail.due_at) : '—' }}
              <span v-if="detail.status === 'pending' && detail.due_at" class="muted">（建议）</span>
            </dd>
            <template v-if="detail.expected_at">
              <dt>客户期望</dt>
              <dd>{{ formatDateTime(detail.expected_at) }}</dd>
            </template>
            <dt>创建</dt>
            <dd>
              {{ formatDateTime(detail.created_at) }}
              <span v-if="detail.created_by_name" class="muted">{{ detail.created_by_name }}</span>
            </dd>
            <template v-if="detail.confirmed_at">
              <dt>确认</dt>
              <dd>{{ formatDateTime(detail.confirmed_at) }}</dd>
            </template>
            <template v-if="detail.first_response_at">
              <dt>首次响应</dt>
              <dd>{{ formatDateTime(detail.first_response_at) }}</dd>
            </template>
            <template v-if="detail.closed_at">
              <dt>结束</dt>
              <dd>{{ formatDateTime(detail.closed_at) }}</dd>
            </template>
            <template v-if="detail.result">
              <dt>处理结果</dt>
              <dd data-testid="todo-result">{{ detail.result }}</dd>
            </template>
            <template v-if="detail.reject_reason">
              <dt>驳回原因</dt>
              <dd>{{ REJECT_REASON[detail.reject_reason] }}</dd>
            </template>
            <template v-if="detail.close_note">
              <dt>说明</dt>
              <dd>{{ detail.close_note }}</dd>
            </template>
          </dl>
        </section>

        <section v-if="active && detail.allowed.handle && detail.customer_id" class="block">
          <h4>客户可见的进度说明</h4>
          <div class="inline">
            <el-input v-model="note" maxlength="500" placeholder="例如：已提交财务，预计明天开出" />
            <el-button :disabled="acting" @click="saveNote">保存</el-button>
          </div>
        </section>

        <section v-if="detail.evidence.length" class="block">
          <div class="block-head">
            <h4>依据的对话</h4>
            <el-button
              v-if="detail.session_id"
              link
              type="primary"
              size="small"
              @click="viewing = detail.session_id"
              >查看会话</el-button
            >
          </div>
          <p v-for="m in detail.evidence" :key="m.id" class="line" data-testid="todo-evidence">
            <span class="muted">{{ m.sender_type === 'customer' ? '客户' : '客服' }}：</span>{{ m.text }}
          </p>
        </section>

        <section class="block">
          <h4>动态</h4>
          <el-timeline>
            <el-timeline-item
              v-for="e in detail.events"
              :key="e.id"
              :timestamp="formatDateTime(e.created_at)"
            >
              <span data-testid="todo-event">{{ describeEvent(e, names) }}</span>
            </el-timeline-item>
          </el-timeline>
          <div v-if="canWork" class="comment">
            <el-input
              v-model="comment.text"
              type="textarea"
              :rows="2"
              maxlength="2000"
              placeholder="评论，可以 @ 同事"
              data-testid="todo-comment"
            />
            <div class="inline">
              <el-select
                v-model="comment.mentions"
                multiple
                filterable
                collapse-tags
                placeholder="@ 同事"
                class="mentions"
              >
                <el-option v-for="s in options.staff" :key="s.id" :label="s.name" :value="s.id" />
              </el-select>
              <el-button :disabled="acting" data-testid="todo-comment-send" @click="sendComment"
                >发送</el-button
              >
            </div>
          </div>
        </section>
      </template>
    </div>

    <template v-if="detail" #footer>
      <div class="actions">
        <template v-if="detail.status === 'pending' && detail.allowed.confirm">
          <el-button :disabled="acting" data-testid="todo-reject" @click="reject.open = true">驳回</el-button>
          <el-button v-if="detail.customer_id" :disabled="acting" @click="openMerge">合并</el-button>
          <el-button type="primary" :disabled="acting" data-testid="todo-confirm" @click="confirmOpen = true"
            >确认</el-button
          >
        </template>
        <el-button
          v-if="detail.allowed.claim"
          type="primary"
          :disabled="acting"
          data-testid="todo-claim"
          @click="simple('claim', '已认领')"
          >认领</el-button
        >
        <template v-if="active && detail.allowed.handle">
          <el-button v-if="detail.status === 'open'" :disabled="acting" data-testid="todo-start" @click="simple('start', '已开始处理')"
            >开始处理</el-button
          >
          <el-button v-if="detail.status !== 'waiting'" :disabled="acting" @click="wait">等待客户</el-button>
          <el-button v-else :disabled="acting" @click="simple('resume', '已恢复处理')">恢复处理</el-button>
          <el-button :disabled="acting" @click="reschedule.open = true">改期</el-button>
          <el-button :disabled="acting" data-testid="todo-assign" @click="openAssign">转交</el-button>
          <el-button v-if="detail.customer_id" :disabled="acting" @click="notice.open = true">通知客户</el-button>
          <el-button :disabled="acting" @click="cancel">取消</el-button>
          <el-button type="primary" :disabled="acting" data-testid="todo-done" @click="openDone">完成</el-button>
        </template>
        <el-button
          v-if="detail.status === 'done' && canWork"
          :disabled="acting"
          data-testid="todo-reopen"
          @click="reopen"
          >重新打开</el-button
        >
      </div>
    </template>

    <ConfirmDialog
      v-if="detail"
      v-model="confirmOpen"
      :todo="detail"
      @confirmed="
        () => {
          void load()
          emit('changed')
        }
      "
    />

    <el-dialog v-model="reject.open" title="驳回" width="420px" append-to-body>
      <el-form label-width="64px">
        <el-form-item label="原因">
          <el-radio-group v-model="reject.reason" data-testid="reject-reason">
            <el-radio v-for="[value, label] in REJECT_REASONS" :key="value" :value="value">{{ label }}</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="reject.note" type="textarea" :rows="2" maxlength="500" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="reject.open = false">取消</el-button>
        <el-button type="danger" :loading="acting" data-testid="reject-submit" @click="submitReject">驳回</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="merge.open" title="合并到这位客户已有的待办" width="480px" append-to-body>
      <el-radio-group v-model="merge.targetId" class="candidates">
        <el-radio v-for="t in merge.candidates" :key="t.id" :value="t.id">
          {{ t.no }} · {{ t.type_name }} · {{ t.title }}（{{ TODO_STATUS[t.status] }}）
        </el-radio>
      </el-radio-group>
      <el-empty v-if="!merge.candidates.length" :image-size="48" description="这位客户没有可以合并的待办" />
      <template #footer>
        <el-button @click="merge.open = false">取消</el-button>
        <el-button type="primary" :disabled="!merge.targetId" :loading="acting" @click="submitMerge">合并</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="done.open" title="完成待办" width="480px" append-to-body data-testid="done-dialog">
      <el-form label-position="top">
        <el-form-item label="处理结果" required>
          <el-input v-model="done.result" type="textarea" :rows="3" maxlength="2000" data-testid="done-result" />
        </el-form-item>
        <el-form-item v-if="detail?.customer_id">
          <el-checkbox v-model="done.notify" data-testid="done-notify">通知客户</el-checkbox>
        </el-form-item>
        <el-form-item v-if="done.notify" label="通知内容">
          <el-input
            v-model="done.notice"
            type="textarea"
            :rows="2"
            maxlength="1000"
            placeholder="不填使用类型的完成通知模板（处理结果会填入模板）"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="done.open = false">取消</el-button>
        <el-button type="primary" :loading="acting" data-testid="done-submit" @click="submitDone">完成</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="assign.open" title="转交" width="440px" append-to-body>
      <el-form label-width="64px">
        <el-form-item label="交给">
          <el-radio-group v-model="assign.mode">
            <el-radio value="staff">员工</el-radio>
            <el-radio value="group">技能组待认领</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="assign.mode === 'staff'" label="员工">
          <el-select v-model="assign.staffId" filterable data-testid="assign-staff">
            <el-option v-for="s in options.staff" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
        </el-form-item>
        <el-form-item v-else label="技能组">
          <el-select v-model="assign.groupId" filterable>
            <el-option v-for="g in options.groups" :key="g.id" :label="g.name" :value="g.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="assign.note" maxlength="500" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="assign.open = false">取消</el-button>
        <el-button type="primary" :loading="acting" data-testid="assign-submit" @click="submitAssign">转交</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="reschedule.open" title="改期" width="420px" append-to-body>
      <el-form label-width="96px">
        <el-form-item label="新的截止时间">
          <el-date-picker v-model="reschedule.dueAt" type="datetime" value-format="YYYY-MM-DDTHH:mm:ssZ" />
        </el-form-item>
        <el-form-item label="原因">
          <el-input v-model="reschedule.reason" maxlength="500" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="reschedule.open = false">取消</el-button>
        <el-button type="primary" :loading="acting" @click="submitReschedule">改期</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="notice.open" title="通知客户" width="440px" append-to-body>
      <p class="muted">
        官网访客会收到系统消息；微信客服在回复窗口内直接发送；企业微信客户需要在侧边栏由员工发送。
      </p>
      <el-input v-model="notice.text" type="textarea" :rows="3" maxlength="1000" />
      <template #footer>
        <el-button @click="notice.open = false">取消</el-button>
        <el-button type="primary" :loading="acting" @click="submitNotice">发送</el-button>
      </template>
    </el-dialog>

    <SessionDrawer :session-id="viewing" :staff-names="names" @close="viewing = null" />
  </el-drawer>
</template>

<style scoped>
.body {
  padding: 0 4px;
}

.head {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.title {
  margin: 12px 0 4px;
}

.text,
.line {
  white-space: pre-wrap;
  word-break: break-word;
  margin: 4px 0;
}

.line {
  font-size: 13px;
}

.block {
  border-top: 1px solid var(--el-border-color-lighter);
  padding: 8px 0;
}

.block-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

h4 {
  margin: 4px 0 8px;
  font-size: 13px;
}

dl {
  display: grid;
  grid-template-columns: 88px 1fr;
  gap: 4px 8px;
  margin: 0;
  font-size: 13px;
}

dt {
  color: var(--el-text-color-secondary);
}

dd {
  margin: 0;
  word-break: break-all;
}

.overdue {
  color: var(--el-color-danger);
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.inline {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}

.mentions {
  flex: 1;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}

.actions .el-button + .el-button {
  margin-left: 0;
}

.candidates {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
}
</style>
