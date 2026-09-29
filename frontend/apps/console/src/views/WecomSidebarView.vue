<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api, formatDateTime } from '../api'
import { useAuthStore } from '../stores/auth'
import { SIDEBAR_ORIGIN, TRANSFER_STATUS, inWecom } from '../wecom'
import { loadJssdk, type WecomChat } from '../wecom-jssdk'

/**
 * 企业微信聊天工具栏侧边栏（设计 §10.5）：当前客户（或客户群）的档案、历史会话摘要，
 * 粘贴客户的问题获取 AI 建议回复，一键发送到当前聊天（JS-SDK sendChatMessage）。
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
        <div v-if="customer.tags.length" class="tags">
          <el-tag v-for="t in customer.tags" :key="t" size="small">{{ t }}</el-tag>
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
          在职继承：{{ TRANSFER_STATUS[context.wecom.transfers[0]!.status] ?? '' }}
        </div>
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
  gap: 4px;
  margin-top: 6px;
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
