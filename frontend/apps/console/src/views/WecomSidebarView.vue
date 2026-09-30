<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api, formatDateTime } from '../api'
import KbSearchPanel from '../components/knowledge/KbSearchPanel.vue'
import CustomerOrders from '../components/orders/CustomerOrders.vue'
import CustomerTodos from '../components/todos/CustomerTodos.vue'
import QuickReplies from '../components/workbench/QuickReplies.vue'
import { useAuthStore } from '../stores/auth'
import { SIDEBAR_ORIGIN, TRANSFER_KIND, TRANSFER_STATUS, inWecom } from '../wecom'
import { loadJssdk, type WecomChat } from '../wecom-jssdk'

/**
 * 企业微信聊天工具栏侧边栏（设计 §10.5、§17.4）：当前客户（或客户群）的档案和标签（可修改）、
 * 历史会话摘要；粘贴客户的问题获取 AI 建议回复，或用快捷话术、知识检索，一键发送到当前聊天
 * （JS-SDK sendChatMessage）；单聊里可以一键拉上接单员建群（openEnterpriseChat）。
 *
 * 不在企业微信里打开时（开发、验收），可以用 ?external_userid= 或 ?chat_id= 指定客户或群，
 * 发送只记录到平台，不会发到企业微信。
 */
type Origin = keyof typeof SIDEBAR_ORIGIN

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const phase = ref<'loading' | 'login' | 'ready' | 'error'>('loading')
const problem = ref('')
const chat = ref<WecomChat | null>(null)
const sdk = ref<Awaited<ReturnType<typeof loadJssdk>> | null>(null)
const context = ref<Schemas['SidebarContext'] | null>(null)
const question = ref('')
const drafting = ref(false)
const suggestions = ref<string[]>([])
const knowledge = ref<Schemas['KnowledgeRef'][]>([])
const editor = ref('')
const editorOrigin = ref<Origin>('manual')
const sending = ref(false)

const tagOptions = ref<string[]>([])
const editingTags = ref(false)
const tagDraft = ref<string[]>([])
const savingTags = ref(false)
const members = ref<Schemas['SidebarMemberOut'][]>([])
const groupDialog = ref(false)
const groupForm = reactive({ userIds: [] as string[], name: '' })
const creatingGroup = ref(false)

const devMode = computed(() => sdk.value === null)
const customer = computed(() => context.value?.customer ?? null)

function query(name: string): string {
  const value = route.query[name]
  return typeof value === 'string' ? value : ''
}

async function signIn(): Promise<void> {
  const corp = query('corp')
  if (inWecom() && corp) {
    const { data, error } = await api.GET('/api/v1/auth/wecom/oauth', {
      params: { query: { corp_id: corp, next: route.fullPath } },
    })
    if (data) {
      window.location.replace(data.url)
      return
    }
    problem.value = errorMessage(error)
  }
  phase.value = 'login'
}

async function resolveChat(): Promise<WecomChat | null> {
  const external = query('external_userid')
  const chatId = query('chat_id')
  if (external) return { kind: 'contact', id: external }
  if (chatId) return { kind: 'group', id: chatId }
  if (!inWecom()) return null
  const { data, error } = await api.GET('/api/v1/wecom/jssdk-config', {
    params: { query: { url: window.location.href.split('#')[0]! } },
  })
  if (!data) throw new Error(errorMessage(error))
  sdk.value = await loadJssdk(data)
  return sdk.value.currentChat()
}

async function loadContext(): Promise<void> {
  if (!chat.value) return
  const params =
    chat.value.kind === 'contact' ? { external_userid: chat.value.id } : { chat_id: chat.value.id }
  const { data, error } = await api.GET('/api/v1/sidebar/context', { params: { query: params } })
  if (!data) throw new Error(errorMessage(error))
  context.value = data
}

async function suggest(): Promise<void> {
  const text = question.value.trim()
  if (!text) {
    ElMessage.warning('请先粘贴或输入客户的问题')
    return
  }
  drafting.value = true
  const { data, error } = await api.POST('/api/v1/sidebar/suggestions', {
    body: {
      question: text,
      external_userid: chat.value?.kind === 'contact' ? chat.value.id : null,
    },
  })
  drafting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  suggestions.value = data.suggestions
  knowledge.value = data.knowledge
  if (!data.suggestions.length) ElMessage.info('知识库里没有找到相关内容')
}

function use(text: string, origin: Origin): void {
  editor.value = text
  editorOrigin.value = origin
}

async function send(): Promise<void> {
  const content = editor.value.trim()
  if (!content || !chat.value) return
  sending.value = true
  try {
    if (sdk.value) await sdk.value.sendText(content)
    const { error } = await api.POST('/api/v1/sidebar/sent', {
      body: {
        content,
        origin: editorOrigin.value,
        external_userid: chat.value.kind === 'contact' ? chat.value.id : null,
        chat_id: chat.value.kind === 'group' ? chat.value.id : null,
        // 回复针对的客户问题：一问一答会进入知识沉淀流水线（设计 §12.3）。
        question: question.value.trim() || null,
      },
    })
    if (error) throw new Error(errorMessage(error))
    ElMessage.success(devMode.value ? '已记录（开发模式：没有发到企业微信）' : '已发送')
    editor.value = ''
    editorOrigin.value = 'manual'
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    sending.value = false
  }
}

onMounted(async () => {
  await auth.restore()
  if (!auth.isAuthenticated) {
    await signIn()
    return
  }
  try {
    chat.value = await resolveChat()
    if (!chat.value) {
      phase.value = 'error'
      problem.value = '请在企业微信的客户单聊或客户群里，从聊天工具栏打开'
      return
    }
    await loadContext()
    phase.value = 'ready'
  } catch (e) {
    phase.value = 'error'
    problem.value = e instanceof Error ? e.message : String(e)
  }
})

async function editTags(): Promise<void> {
  tagDraft.value = [...(customer.value?.tags ?? [])]
  editingTags.value = true
  if (!tagOptions.value.length) {
    const { data } = await api.GET('/api/v1/sidebar/tags')
    tagOptions.value = data?.items ?? []
  }
}

async function saveTags(): Promise<void> {
  const current = customer.value
  if (!current || !context.value) return
  savingTags.value = true
  const { data, error } = await api.PUT('/api/v1/sidebar/customers/{customer_id}/tags', {
    params: { path: { customer_id: current.id } },
    body: { tags: tagDraft.value },
  })
  savingTags.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  context.value = { ...context.value, customer: data }
  editingTags.value = false
  ElMessage.success('标签已更新')
}

async function openGroup(): Promise<void> {
  groupForm.userIds = []
  groupForm.name = `${customer.value?.display_name ?? '客户'}的服务群`.slice(0, 40)
  groupDialog.value = true
  if (!members.value.length) {
    const { data, error } = await api.GET('/api/v1/sidebar/members')
    if (!data) ElMessage.error(errorMessage(error))
    members.value = data?.items ?? []
  }
}

async function createGroup(): Promise<void> {
  if (!sdk.value || !chat.value || chat.value.kind !== 'contact') {
    ElMessage.warning('一键建群需要在企业微信的客户单聊里使用')
    return
  }
  creatingGroup.value = true
  try {
    const chatId = await sdk.value.createGroup(groupForm.userIds, [chat.value.id], groupForm.name)
    if (!chatId) throw new Error('企业微信没有返回群 ID')
    const { data, error } = await api.POST('/api/v1/sidebar/groups', {
      body: { chat_id: chatId, external_userid: chat.value.id },
    })
    if (!data) throw new Error(errorMessage(error))
    groupDialog.value = false
    ElMessage.success(`已建群「${data.name || '客户群'}」`)
    await loadContext()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    creatingGroup.value = false
  }
}

function goLogin(): void {
  void router.push({ name: 'login', query: { redirect: route.fullPath } })
}
</script>

<template>
  <div class="sidebar" data-testid="wecom-sidebar">
    <div v-if="phase === 'loading'" v-loading="true" class="placeholder" />

    <el-result v-else-if="phase === 'login'" icon="info" title="请先登录" :sub-title="problem">
      <template #extra>
        <el-button type="primary" @click="goLogin">登录控制台</el-button>
      </template>
    </el-result>

    <el-result
      v-else-if="phase === 'error'"
      icon="warning"
      title="暂时无法使用"
      :sub-title="problem"
    />

    <template v-else>
      <el-alert
        v-if="devMode"
        type="info"
        :closable="false"
        title="开发模式：不在企业微信里，发送只记录到平台"
        class="dev"
      />

      <section v-if="customer" class="card" data-testid="sidebar-customer">
        <div class="name">{{ customer.display_name }}</div>
        <div class="muted">
          归属：{{ customer.owner_name ?? '未分配' }}
          <template v-if="context?.wecom?.unionid"> · 已关联微信开放平台</template>
        </div>
        <div v-if="!editingTags" class="tags" data-testid="sidebar-tags">
          <el-tag v-for="t in customer.tags" :key="t" size="small">{{ t }}</el-tag>
          <el-button link type="primary" size="small" data-testid="sidebar-edit-tags" @click="editTags">
            {{ customer.tags.length ? '改标签' : '打标签' }}
          </el-button>
        </div>
        <div v-else class="tag-editor">
          <el-select
            v-model="tagDraft"
            multiple
            filterable
            allow-create
            default-first-option
            :multiple-limit="20"
            placeholder="选择或输入标签"
            data-testid="sidebar-tag-select"
          >
            <el-option v-for="t in tagOptions" :key="t" :label="t" :value="t" />
          </el-select>
          <div class="row-actions">
            <el-button size="small" @click="editingTags = false">取消</el-button>
            <el-button
              size="small"
              type="primary"
              :loading="savingTags"
              data-testid="sidebar-save-tags"
              @click="saveTags"
            >
              保存
            </el-button>
          </div>
        </div>
        <p v-if="customer.notes" class="notes">{{ customer.notes }}</p>
        <div v-if="context?.wecom?.follows.length" class="follows">
          <div v-for="f in context.wecom.follows" :key="f.userid" class="muted">
            {{ f.staff_name ?? f.member_name ?? f.userid }} 添加
            <template v-if="f.remark">（备注：{{ f.remark }}）</template>
            <template v-if="f.deleted"> · 已解除</template>
          </div>
        </div>
        <div v-if="context?.wecom?.transfers.length" class="muted">
          {{ TRANSFER_KIND[context.wecom.transfers[0]!.kind] ?? '客户继承' }}：
          {{ TRANSFER_STATUS[context.wecom.transfers[0]!.status] ?? '' }}
        </div>
        <div v-if="context?.wecom?.group_chats.length" class="muted">
          所在客户群：{{ context.wecom.group_chats.map((g) => g.name || '客户群').join('、') }}
        </div>
        <el-button
          class="full"
          size="small"
          :disabled="devMode"
          :title="devMode ? '需要在企业微信里使用' : ''"
          data-testid="sidebar-create-group"
          @click="openGroup"
        >
          一键建群（拉上接单员）
        </el-button>
      </section>

      <section v-if="context?.group" class="card" data-testid="sidebar-group">
        <div class="name">{{ context.group.name || '客户群' }}</div>
        <div class="muted">
          群主 {{ context.group.owner_name ?? context.group.owner_userid }} ·
          {{ context.group.member_count }} 人
        </div>
        <div class="tags">
          <el-tag v-for="c in context.group_customers" :key="c.id" size="small" type="info">
            {{ c.display_name }}
          </el-tag>
        </div>
        <div v-if="context.group.summary" class="summary" data-testid="sidebar-group-summary">
          群聊摘要：{{ context.group.summary }}
          <template v-if="context.group.sentiment">（情绪：{{ context.group.sentiment }}）</template>
        </div>
      </section>

      <section v-if="customer && auth.can('todo:read')" class="card" data-testid="sidebar-todos">
        <h4>待办</h4>
        <CustomerTodos
          :customer-id="customer.id"
          :customer-name="customer.display_name"
          source="sidebar"
        />
      </section>

      <section
        v-if="customer && auth.can('order:read') && auth.me?.features?.orders !== false"
        class="card"
        data-testid="sidebar-orders"
      >
        <h4>订单</h4>
        <CustomerOrders
          :customer-id="customer.id"
          :customer-name="customer.display_name"
          source="sidebar"
          @insert="(text: string) => use(text, 'manual')"
        />
      </section>

      <section v-if="context?.sessions.length" class="card">
        <h4>最近的会话</h4>
        <div v-for="s in context.sessions" :key="s.id" class="session">
          <div class="muted">{{ s.channel_name }} · {{ formatDateTime(s.created_at) }}</div>
          <div v-if="s.summary" class="summary">{{ s.summary }}</div>
        </div>
      </section>

      <section class="card">
        <h4>AI 建议回复</h4>
        <el-input
          v-model="question"
          type="textarea"
          :rows="3"
          placeholder="粘贴或输入客户的问题"
          data-testid="sidebar-question"
        />
        <el-button
          class="full"
          type="primary"
          plain
          :loading="drafting"
          data-testid="sidebar-suggest"
          @click="suggest"
        >
          生成建议
        </el-button>
        <button
          v-for="(s, i) in suggestions"
          :key="i"
          type="button"
          class="suggestion"
          data-testid="sidebar-suggestion"
          @click="use(s, 'suggestion')"
        >
          {{ s }}
        </button>
        <div v-if="knowledge.length" class="muted">
          依据：{{ knowledge.map((k) => k.title).join('、') }}
        </div>
      </section>

      <section class="card" data-testid="sidebar-library">
        <div class="library-head">
          <h4>快捷话术与知识</h4>
          <QuickReplies @pick="(text: string) => use(text, 'quick_reply')" />
        </div>
        <KbSearchPanel insertable @insert="(text: string) => use(text, 'knowledge')" />
      </section>

      <section class="card">
        <h4>发送到当前聊天</h4>
        <el-input
          v-model="editor"
          type="textarea"
          :rows="4"
          placeholder="点上面的建议放到这里，可以修改后发送"
          data-testid="sidebar-editor"
        />
        <div class="muted">来源：{{ SIDEBAR_ORIGIN[editorOrigin] }}</div>
        <el-button
          class="full"
          type="primary"
          :loading="sending"
          :disabled="!editor.trim()"
          data-testid="sidebar-send"
          @click="send"
        >
          发送
        </el-button>
      </section>
    </template>

    <el-dialog v-model="groupDialog" title="一键建群" width="92%" class="group-dialog">
      <el-form label-position="top">
        <el-form-item label="拉上的同事（接单员等）">
          <el-select
            v-model="groupForm.userIds"
            multiple
            filterable
            placeholder="选择企业成员"
            data-testid="sidebar-group-members"
          >
            <el-option
              v-for="m in members"
              :key="m.userid"
              :label="m.staff_name ? `${m.name}（${m.staff_name}）` : m.name"
              :value="m.userid"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="群名称">
          <el-input v-model="groupForm.name" maxlength="40" data-testid="sidebar-group-name" />
        </el-form-item>
        <p class="muted">建群需要在企业微信里确认；含客户时群成员最多 40 人。</p>
      </el-form>
      <template #footer>
        <el-button @click="groupDialog = false">取消</el-button>
        <el-button
          type="primary"
          :loading="creatingGroup"
          data-testid="sidebar-group-create"
          @click="createGroup"
        >
          发起建群
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.sidebar {
  max-width: 480px;
  min-height: 100%;
  margin: 0 auto;
  padding: 10px;
  box-sizing: border-box;
  background: var(--el-fill-color-lighter);
}

.placeholder {
  height: 200px;
}

.dev {
  margin-bottom: 8px;
}

.card {
  margin-bottom: 10px;
  padding: 10px 12px;
  background: var(--el-bg-color);
  border-radius: 8px;
}

.card h4 {
  margin: 0 0 8px;
  font-size: 14px;
}

.name {
  font-size: 16px;
  font-weight: 600;
}

.muted {
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.tags {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  margin-top: 6px;
}

.tag-editor {
  margin-top: 6px;
}

.tag-editor .el-select {
  width: 100%;
}

.row-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
  margin-top: 6px;
}

.library-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.notes {
  margin: 6px 0 0;
  font-size: 13px;
  white-space: pre-wrap;
}

.follows {
  margin-top: 6px;
}

.session + .session {
  margin-top: 8px;
}

.summary {
  font-size: 13px;
  white-space: pre-wrap;
}

.full {
  width: 100%;
  margin-top: 8px;
}

.suggestion {
  display: block;
  width: 100%;
  margin-top: 8px;
  padding: 8px 10px;
  text-align: left;
  font-size: 13px;
  line-height: 1.6;
  color: var(--el-text-color-primary);
  background: var(--el-color-primary-light-9);
  border: 1px solid var(--el-color-primary-light-7);
  border-radius: 6px;
  cursor: pointer;
}
</style>
