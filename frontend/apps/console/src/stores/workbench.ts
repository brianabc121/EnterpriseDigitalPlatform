import { errorMessage, type Schemas } from '@edp/api-client'
import {
  createImClient,
  type ChatMessage,
  type ConnectionState,
  type ImClient,
  type ImSignal,
} from '@edp/im-client'
import { ElNotification } from 'element-plus'
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api } from '../api'
import {
  fromApi,
  fromIm,
  mergeMessages,
  pendingMessage,
  type WorkbenchMessage,
} from '../workbench/messages'
import { useAuthStore } from './auth'

export type AgentStatus = Schemas['AgentStatus']
type Session = Schemas['SessionOut']

/** 心跳间隔；后端超过 90 秒没有心跳就把坐席置为离线。 */
export const HEARTBEAT_MS = 30_000
/** 会话列表和当前会话消息的兜底刷新间隔（信令或 IM 推送丢失时，最多延迟这么久）。 */
export const POLL_MS = 10_000
/**
 * OpenIM 每秒批量上报一次用户上线，推送服务要过一两秒才把刚连上的用户当作在线用户推送。
 * 连上 IM 后等这么久再把接待状态设为在线，避免分配信令和新消息在这段时间里推送不到。
 */
export const IM_ONLINE_GRACE_MS = 2_000
const HISTORY_PAGE = 50

export const STATUS_LABEL: Record<AgentStatus, string> = {
  online: '在线',
  busy: '忙碌',
  away: '小休',
  offline: '离线',
}

/**
 * 坐席工作台的状态：接待状态与心跳、会话列表、各 Room 的消息、IM 连接与信令。
 *
 * - 会话列表以平台接口为准；信令（新分配、结束、退回）只是提示"该刷新了"，
 *   另外每次心跳时也刷新一次，信令丢失时最多延迟一个心跳周期。
 * - 消息历史走平台接口，增量走 IM；坐席发送走平台接口（见 workbench/messages.ts）。
 */
export const useWorkbenchStore = defineStore('workbench', () => {
  const auth = useAuthStore()

  const agent = ref<Schemas['MyAgentState'] | null>(null)
  const sessions = ref<Session[]>([])
  const queued = ref<Session[]>([])
  const active = ref<Session | null>(null)
  const messages = ref<Record<string, WorkbenchMessage[]>>({})
  const hasMore = ref<Record<string, boolean>>({})
  const unread = ref<Record<string, number>>({})
  const imState = ref<ConnectionState>('idle')
  const error = ref<string | null>(null)

  let im: ImClient | null = null
  let systemUserId = ''
  let myImUser = ''
  let timer: ReturnType<typeof setInterval> | null = null
  let pollTimer: ReturnType<typeof setInterval> | null = null
  let starting: Promise<void> | null = null
  const unsubscribe: (() => void)[] = []

  const canSeeQueue = computed(() => auth.can('session:read_all') || auth.can('session:read_team'))
  const canManageOthers = computed(() => auth.can('session:transfer_any'))
  const activeMessages = computed(() =>
    active.value ? (messages.value[active.value.room_id] ?? []) : [],
  )

  function isMine(session: Session): boolean {
    return session.assignee_id === auth.me?.id
  }

  // ---- 接待状态与心跳 ----

  async function setStatus(status: AgentStatus): Promise<void> {
    const { data, error: err } = await api.PUT('/api/v1/agent/state', { body: { status } })
    if (!data) throw new Error(errorMessage(err))
    agent.value = data
    await loadSessions()
  }

  async function heartbeat(): Promise<void> {
    const { data } = await api.POST('/api/v1/agent/heartbeat')
    if (data) agent.value = data
    await loadSessions()
  }

  // ---- 启动与停止 ----

  /** 进入工作台时调用（只执行一次）：连接 IM、上线、加载会话、开始心跳和兜底刷新。 */
  function start(): Promise<void> {
    starting ??= (async () => {
      error.value = null
      try {
        await connectIm()
        await new Promise((resolve) => setTimeout(resolve, IM_ONLINE_GRACE_MS))
        await setStatus('online')
      } catch (e) {
        error.value = e instanceof Error ? e.message : String(e)
      }
      timer = setInterval(() => void heartbeat(), HEARTBEAT_MS)
      pollTimer = setInterval(() => void poll(), POLL_MS)
    })()
    return starting
  }

  /** 兜底刷新：会话列表和当前会话最近的消息以平台接口为准。 */
  async function poll(): Promise<void> {
    await loadSessions()
    if (active.value) {
      await loadHistory(active.value.room_id, undefined, false).catch(() => undefined)
    }
  }

  /** 退出登录时调用：离线（未回复的会话立即退回队列）并断开 IM。 */
  async function stop(): Promise<void> {
    if (!starting) return
    if (timer) clearInterval(timer)
    if (pollTimer) clearInterval(pollTimer)
    timer = null
    pollTimer = null
    starting = null
    await api.PUT('/api/v1/agent/state', { body: { status: 'offline' } }).catch(() => undefined)
    unsubscribe.splice(0).forEach((off) => off())
    await im?.disconnect()
    im = null
    sessions.value = []
    queued.value = []
    active.value = null
    messages.value = {}
    unread.value = {}
    agent.value = null
  }

  async function connectIm(): Promise<void> {
    const { data, error: err } = await api.POST('/api/v1/agent/im-token')
    if (!data) throw new Error(errorMessage(err, '即时通讯服务暂时不可用'))
    systemUserId = data.system_user_id
    myImUser = data.user_id
    im = createImClient()
    unsubscribe.push(
      im.onState((state) => {
        // 断线重连后，期间的分配和消息可能没有推送到：从平台接口补齐。
        const recovered = state === 'connected' && imState.value === 'reconnecting'
        imState.value = state
        if (recovered) void poll()
      }),
      im.onMessage(onImMessage),
      im.onSignal(onSignal),
    )
    await im.connect({
      userID: data.user_id,
      token: data.token,
      apiAddr: data.api_url,
      wsAddr: data.ws_url,
      platformID: data.platform_id,
    })
  }

  // ---- 会话 ----

  async function loadSessions(): Promise<void> {
    const mine = await api.GET('/api/v1/sessions', {
      params: { query: { status: 'open', mine: true, limit: 100 } },
    })
    if (mine.data) {
      sessions.value = mine.data.items
      // 正在查看的会话如果不在列表里了（已结束或被退回），保留它并刷新状态，只读展示。
      if (active.value && !mine.data.items.some((s) => s.id === active.value!.id)) {
        await refreshActive()
      } else if (active.value) {
        active.value = mine.data.items.find((s) => s.id === active.value!.id) ?? active.value
      }
    }
    if (canSeeQueue.value) {
      const waiting = await api.GET('/api/v1/sessions', {
        params: { query: { status: 'queued', limit: 100 } },
      })
      if (waiting.data) queued.value = waiting.data.items
    }
  }

  async function refreshActive(): Promise<void> {
    if (!active.value) return
    const { data } = await api.GET('/api/v1/sessions/{session_id}', {
      params: { path: { session_id: active.value.id } },
    })
    if (data) active.value = data
  }

  async function open(session: Session): Promise<void> {
    active.value = session
    unread.value = { ...unread.value, [session.id]: 0 }
    await loadHistory(session.room_id)
  }

  async function loadHistory(roomId: string, before?: string, updateMore = true): Promise<void> {
    const { data, error: err } = await api.GET('/api/v1/rooms/{room_id}/messages', {
      params: { path: { room_id: roomId }, query: { limit: HISTORY_PAGE, before } },
    })
    if (!data) throw new Error(errorMessage(err))
    messages.value = {
      ...messages.value,
      [roomId]: mergeMessages(messages.value[roomId] ?? [], data.items.map(fromApi)),
    }
    if (updateMore) hasMore.value = { ...hasMore.value, [roomId]: data.has_more }
  }

  async function loadOlder(): Promise<void> {
    const roomId = active.value?.room_id
    const oldest = roomId ? messages.value[roomId]?.find((m) => m.id) : undefined
    if (roomId && oldest?.id) await loadHistory(roomId, oldest.id)
  }

  function addMessages(roomId: string, incoming: WorkbenchMessage[]): void {
    messages.value = {
      ...messages.value,
      [roomId]: mergeMessages(messages.value[roomId] ?? [], incoming),
    }
  }

  async function send(text: string, clientMsgID: string = crypto.randomUUID()): Promise<void> {
    const session = active.value
    const me = auth.me
    if (!session || !me) return
    addMessages(session.room_id, [
      pendingMessage(clientMsgID, text, { id: me.id, name: me.display_name }),
    ])
    const { data, error: err } = await api.POST('/api/v1/sessions/{session_id}/messages', {
      params: { path: { session_id: session.id } },
      body: { client_msg_id: clientMsgID, text },
    })
    if (data) {
      addMessages(session.room_id, [fromApi(data)])
      return
    }
    const failed = messages.value[session.room_id]?.find((m) => m.clientMsgID === clientMsgID)
    if (failed) addMessages(session.room_id, [{ ...failed, status: 'failed' }])
    throw new Error(errorMessage(err, '发送失败，请重试'))
  }

  async function retry(message: WorkbenchMessage): Promise<void> {
    if (message.clientMsgID && message.text) await send(message.text, message.clientMsgID)
  }

  async function close(session: Session): Promise<void> {
    const { data, error: err } = await api.POST('/api/v1/sessions/{session_id}/close', {
      params: { path: { session_id: session.id } },
    })
    if (!data) throw new Error(errorMessage(err))
    if (active.value?.id === session.id) active.value = data
    await loadSessions()
  }

  // ---- IM 推送 ----

  function onImMessage(message: ChatMessage): void {
    const session =
      sessions.value.find((s) => s.im_group_id === message.groupID) ??
      (active.value?.im_group_id === message.groupID ? active.value : undefined)
    if (!session) {
      // 刚分配的会话还没出现在列表里：刷新列表，历史在打开时从接口加载。
      void loadSessions()
      return
    }
    addMessages(session.room_id, [fromIm(message)])
    if (active.value?.id !== session.id && message.sendID !== myImUser) {
      unread.value = { ...unread.value, [session.id]: (unread.value[session.id] ?? 0) + 1 }
    }
  }

  function onSignal(signal: ImSignal): void {
    if (signal.sendID !== systemUserId) return
    if (signal.type === 'session.assigned') {
      ElNotification({ title: '新会话', message: '有新的客户分配给您', type: 'info' })
    }
    void loadSessions()
  }

  return {
    agent,
    sessions,
    queued,
    active,
    activeMessages,
    hasMore,
    unread,
    imState,
    error,
    canSeeQueue,
    canManageOthers,
    isMine,
    start,
    stop,
    setStatus,
    loadSessions,
    open,
    loadOlder,
    send,
    retry,
    close,
  }
})
