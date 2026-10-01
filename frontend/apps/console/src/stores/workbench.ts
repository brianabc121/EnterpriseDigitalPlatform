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
  outgoingOf,
  pendingMessage,
  sendBody,
  senderTypeOf,
  type Outgoing,
  type WorkbenchMessage,
} from '../workbench/messages'
import { uploadFile } from '../workbench/upload'
import { useAuthStore } from './auth'

export type AgentStatus = Schemas['AgentStatus']
type Session = Schemas['SessionOut']
/** 会话详情（带事件和正在旁听、协助的员工）。 */
type SessionDetail = Schemas['SessionDetail']
export interface CopilotAlert {
  id: string
  kind: string
  text: string
  createdAt: string
}

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
 * - 未读数以平台记录为准（我接待的会话，刷新页面后也在）：打开会话、正在看的会话来了新消息时
 *   标记已读；两次刷新之间按 IM 推送的客户消息累加。
 */
export const useWorkbenchStore = defineStore('workbench', () => {
  const auth = useAuthStore()

  const agent = ref<Schemas['MyAgentState'] | null>(null)
  const sessions = ref<Session[]>([])
  const queued = ref<Session[]>([])
  /** 进行中：AI 接待和其他坐席接待中的会话（主管、管理员可见，可以旁听、转人工）。 */
  const ongoing = ref<Session[]>([])
  /** 我正在旁听或协助的会话。 */
  const watching = ref<Session[]>([])
  /** 我最近结束的会话（打开"已结束"页签时加载）。 */
  const closed = ref<Session[]>([])
  const active = ref<Session | SessionDetail | null>(null)
  const replyWindow = ref<Schemas['ReplyWindowOut'] | null>(null)
  /** 坐席助手的实时提醒（按会话），打开会话时从接口加载，之后由信令追加。 */
  const alerts = ref<Record<string, CopilotAlert[]>>({})
  const messages = ref<Record<string, WorkbenchMessage[]>>({})
  const hasMore = ref<Record<string, boolean>>({})
  const unread = ref<Record<string, number>>({})
  const imState = ref<ConnectionState>('idle')
  /** 发给我、等待确认的转接。 */
  const incoming = ref<Schemas['TransferOut'][]>([])
  /** 我发起、等待对方确认的转接（按会话）。 */
  const outgoing = ref<Record<string, Schemas['TransferOut']>>({})
  const error = ref<string | null>(null)
  /** 要放进回复框的内容（来自知识检索、AI 建议），ChatPanel 监听后插入。 */
  const composerInsert = ref<{ text: string; seq: number } | null>(null)

  let im: ImClient | null = null
  let systemUserId = ''
  let myImUser = ''
  /** 正在标记已读的会话（避免重复请求）。 */
  const marking = new Set<string>()
  /** 邮件会话来了新消息，等一会儿从平台接口取完整的邮件（IM 里只是文字镜像）。 */
  const emailReloads = new Map<string, ReturnType<typeof setTimeout>>()
  let timer: ReturnType<typeof setInterval> | null = null
  let pollTimer: ReturnType<typeof setInterval> | null = null
  let starting: Promise<void> | null = null
  const unsubscribe: (() => void)[] = []

  const canSeeQueue = computed(() => auth.can('session:read_all') || auth.can('session:read_team'))
  const canManageOthers = computed(() => auth.can('session:transfer_any'))
  const canMonitor = computed(() => auth.can('session:monitor'))
  const activeMessages = computed(() =>
    active.value ? (messages.value[active.value.room_id] ?? []) : [],
  )

  function isMine(session: Session): boolean {
    return session.assignee_id === auth.me?.id
  }

  /** 我在会话里的身份：assignee（接待）、monitor（旁听）、assist（协助）；都不是时为空。 */
  function roleIn(session: Session): string | null {
    if (isMine(session)) return 'assignee'
    return session.my_role ?? watching.value.find((s) => s.id === session.id)?.my_role ?? null
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
        await loadIncoming()
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
    await Promise.all([loadSessions(), loadIncoming()])
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
    ongoing.value = []
    watching.value = []
    closed.value = []
    incoming.value = []
    outgoing.value = {}
    active.value = null
    messages.value = {}
    unread.value = {}
    agent.value = null
    emailReloads.forEach((pending) => clearTimeout(pending))
    emailReloads.clear()
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
      syncUnread(mine.data.items)
      // 正在查看的会话如果不在列表里了（已结束或被退回），保留它并刷新状态，只读展示。
      if (active.value && !mine.data.items.some((s) => s.id === active.value!.id)) {
        await refreshActive()
      } else if (active.value) {
        const fresh = mine.data.items.find((s) => s.id === active.value!.id)
        // 列表项没有旁听、协助的员工：保留详情里的，其余字段用最新的。
        if (fresh) active.value = { ...active.value, ...fresh }
      }
    }
    const watched = await api.GET('/api/v1/sessions', {
      params: { query: { status: 'open', watching: true, limit: 100 } },
    })
    if (watched.data) watching.value = watched.data.items
    if (canSeeQueue.value) {
      const [waiting, serving] = await Promise.all([
        api.GET('/api/v1/sessions', { params: { query: { status: 'queued', limit: 100 } } }),
        api.GET('/api/v1/sessions', { params: { query: { status: 'serving', limit: 100 } } }),
      ])
      if (waiting.data) queued.value = waiting.data.items
      if (serving.data) ongoing.value = serving.data.items.filter((s) => !isMine(s))
    }
  }

  /** 我接待的会话的未读数以平台为准；正在看的会话是 0（平台上还有未读时标记已读）。 */
  function syncUnread(list: Session[]): void {
    const next = { ...unread.value }
    for (const s of list) {
      if (active.value?.id === s.id) {
        next[s.id] = 0
        if (s.unread) void markRead(s)
      } else {
        next[s.id] = s.unread ?? 0
      }
    }
    unread.value = next
  }

  /** 接待坐席看过了这个会话（其他人查看不改变接待坐席的未读）。 */
  async function markRead(session: Session): Promise<void> {
    if (!isMine(session) || session.status === 'closed' || marking.has(session.id)) return
    marking.add(session.id)
    try {
      await api.POST('/api/v1/sessions/{session_id}/read', {
        params: { path: { session_id: session.id } },
      })
    } catch {
      // 下次刷新列表时再标记。
    } finally {
      marking.delete(session.id)
    }
  }

  function reloadEmails(session: Session): void {
    const pending = emailReloads.get(session.room_id)
    if (pending) clearTimeout(pending)
    emailReloads.set(
      session.room_id,
      setTimeout(() => {
        emailReloads.delete(session.room_id)
        void loadHistory(session.room_id, undefined, false).catch(() => undefined)
      }, 300),
    )
  }

  async function loadClosed(): Promise<void> {
    const { data } = await api.GET('/api/v1/sessions', {
      params: { query: { status: 'closed', mine: true, limit: 30 } },
    })
    if (data) closed.value = data.items
  }

  async function refreshActive(): Promise<void> {
    const id = active.value?.id
    if (!id) return
    const { data, response } = await api.GET('/api/v1/sessions/{session_id}', {
      params: { path: { session_id: id } },
    })
    if (active.value?.id !== id) return
    if (data) active.value = data
    // 协助结束后看不到这个会话了。
    else if (response.status === 404) active.value = null
  }

  async function open(session: Session): Promise<void> {
    active.value = session
    replyWindow.value = null
    unread.value = { ...unread.value, [session.id]: 0 }
    await Promise.all([
      loadHistory(session.room_id),
      refreshReplyWindow(),
      refreshActive(),
      loadAlerts(session.id),
      markRead(session),
    ])
  }

  async function loadAlerts(sessionId: string): Promise<void> {
    const { data } = await api.GET('/api/v1/sessions/{session_id}/alerts', {
      params: { path: { session_id: sessionId } },
    })
    if (!data) return
    const me = auth.me?.id
    alerts.value = {
      ...alerts.value,
      [sessionId]: data.items
        .filter((a) => !a.staff_id || a.staff_id === me)
        .map((a) => ({ id: a.id, kind: a.kind, text: a.text, createdAt: a.created_at })),
    }
  }

  function addAlert(signal: ImSignal): void {
    const sessionId = String(signal.data.session_id ?? '')
    if (!sessionId) return
    const alert: CopilotAlert = {
      id: `live-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
      kind: String(signal.data.kind ?? ''),
      text: String(signal.data.text ?? ''),
      createdAt: new Date().toISOString(),
    }
    alerts.value = { ...alerts.value, [sessionId]: [...(alerts.value[sessionId] ?? []), alert] }
    if (active.value?.id !== sessionId) {
      ElNotification({ title: '坐席助手', message: alert.text, type: 'warning' })
    }
  }

  /** 正在旁听、协助这个会话的员工（打开会话后从详情加载）。 */
  function watchersOf(session: Session | SessionDetail): Schemas['WatcherOut'][] {
    return 'watchers' in session ? (session.watchers ?? []) : []
  }

  /** 当前会话的回复限制（微信客服：客户最后一次发消息后 48 小时内最多 5 条）。 */
  async function refreshReplyWindow(): Promise<void> {
    const session = active.value
    if (!session) return
    const { data } = await api.GET('/api/v1/sessions/{session_id}/reply-window', {
      params: { path: { session_id: session.id } },
    })
    if (data && active.value?.id === session.id) replyWindow.value = data
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

  async function send(
    out: Outgoing | string,
    clientMsgID: string = crypto.randomUUID(),
  ): Promise<void> {
    const session = active.value
    const me = auth.me
    if (!session || !me) return
    const content: Outgoing = typeof out === 'string' ? { type: 'text', text: out } : out
    addMessages(session.room_id, [
      pendingMessage(clientMsgID, content, { id: me.id, name: me.display_name }),
    ])
    const { data, error: err } = await api.POST('/api/v1/sessions/{session_id}/messages', {
      params: { path: { session_id: session.id } },
      body: sendBody(clientMsgID, content),
    })
    if (data) {
      addMessages(session.room_id, [fromApi(data)])
      void refreshReplyWindow()
      return
    }
    const reason = errorMessage(err, '发送失败，请重试')
    const failed = messages.value[session.room_id]?.find((m) => m.clientMsgID === clientMsgID)
    if (failed) addMessages(session.room_id, [{ ...failed, status: 'failed', error: reason }])
    void refreshReplyWindow()
    throw new Error(reason)
  }

  /** 上传图片或文件后发送。上传失败时不产生消息。 */
  async function sendFile(file: File): Promise<void> {
    const uploaded = await uploadFile(file)
    await send({ type: uploaded.kind, attachment: uploaded.attachment })
  }

  async function retry(message: WorkbenchMessage): Promise<void> {
    const out = outgoingOf(message)
    if (message.clientMsgID && out) await send(out, message.clientMsgID)
  }

  function insertIntoComposer(text: string): void {
    composerInsert.value = { text, seq: (composerInsert.value?.seq ?? 0) + 1 }
  }

  /** 坐席助手：根据对话和知识库给出建议回复。 */
  async function suggest(session: Session): Promise<Schemas['SuggestionList']> {
    const { data, error: err } = await api.POST('/api/v1/sessions/{session_id}/suggestions', {
      params: { path: { session_id: session.id } },
    })
    if (!data) throw new Error(errorMessage(err))
    return data
  }

  async function sessionAction(
    session: Session,
    action: 'return-to-ai' | 'handoff' | 'monitor',
  ): Promise<void> {
    const path = `/api/v1/sessions/{session_id}/${action}` as const
    const { error: err, response } = await api.POST(path, {
      params: { path: { session_id: session.id } },
    })
    if (!response.ok) throw new Error(errorMessage(err))
    await loadSessions()
    if (action === 'return-to-ai' && active.value?.id === session.id) active.value = null
    else await refreshActive()
  }

  /** 交还 AI：会话回到 AI 接待，我退出服务群。 */
  async function returnToAi(session: Session): Promise<void> {
    await sessionAction(session, 'return-to-ai')
  }

  /** 主管把 AI 接待中的会话转人工。 */
  async function handoff(session: Session): Promise<void> {
    await sessionAction(session, 'handoff')
  }

  /** 旁听：加入服务群实时查看，客户看不到。 */
  async function monitor(session: Session): Promise<void> {
    await sessionAction(session, 'monitor')
    await loadHistory(session.room_id)
  }

  async function inviteAssist(session: Session, staffId: string): Promise<void> {
    const { data, error: err } = await api.POST('/api/v1/sessions/{session_id}/assists', {
      params: { path: { session_id: session.id } },
      body: { staff_id: staffId },
    })
    if (!data) throw new Error(errorMessage(err))
    if (active.value?.id === session.id) active.value = data
  }

  /** 退出旁听或协助（staffId 为空时是我自己；接待坐席、主管可以请协助者退出）。 */
  async function leave(session: Session, staffId?: string): Promise<void> {
    const me = staffId ?? auth.me?.id
    if (!me) return
    const { error: err, response } = await api.DELETE(
      '/api/v1/sessions/{session_id}/watchers/{staff_id}',
      { params: { path: { session_id: session.id, staff_id: me } } },
    )
    if (!response.ok) throw new Error(errorMessage(err))
    if (!staffId && active.value?.id === session.id) active.value = null
    await loadSessions()
    if (staffId) await refreshActive()
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
      watching.value.find((s) => s.im_group_id === message.groupID) ??
      (active.value?.im_group_id === message.groupID ? active.value : undefined)
    if (!session) {
      // 刚分配的会话还没出现在列表里：刷新列表，历史在打开时从接口加载。
      void loadSessions()
      return
    }
    addMessages(session.room_id, [fromIm(message)])
    const fromCustomer = senderTypeOf(message.sendID) === 'customer'
    if (active.value?.id === session.id) {
      // 邮件在 IM 里只是文字镜像：正在看的会话从平台接口取完整的邮件（其他会话打开时加载）。
      if (session.channel_type === 'email' && message.sendID !== myImUser) reloadEmails(session)
      // 正在看：客户的新消息直接算已读；渠道的回复额度会重置。
      if (fromCustomer) {
        void markRead(session)
        void refreshReplyWindow()
      }
    } else if (fromCustomer) {
      unread.value = { ...unread.value, [session.id]: (unread.value[session.id] ?? 0) + 1 }
    }
  }

  // ---- 转接 ----

  /** 发给我、等待确认的转接。 */
  async function loadIncoming(): Promise<void> {
    const { data } = await api.GET('/api/v1/transfers/pending')
    if (data) incoming.value = data.items
  }

  async function requestTransfer(
    session: Session,
    body: Schemas['TransferRequest'],
  ): Promise<Schemas['TransferOut']> {
    const { data, error: err } = await api.POST('/api/v1/sessions/{session_id}/transfer', {
      params: { path: { session_id: session.id } },
      body,
    })
    if (!data) throw new Error(errorMessage(err))
    if (data.status === 'pending') outgoing.value = { ...outgoing.value, [session.id]: data }
    await loadSessions()
    return data
  }

  async function decideTransfer(
    transfer: Schemas['TransferOut'],
    decision: 'accept' | 'reject' | 'cancel',
  ): Promise<void> {
    const path = `/api/v1/transfers/{transfer_id}/${decision}` as const
    const { data, error: err } = await api.POST(path, {
      params: { path: { transfer_id: transfer.id } },
    })
    incoming.value = incoming.value.filter((t) => t.id !== transfer.id)
    forgetOutgoing(transfer.id)
    if (!data) throw new Error(errorMessage(err))
    await loadSessions()
    if (decision === 'accept') {
      const session = sessions.value.find((s) => s.id === transfer.session_id)
      if (session) await open(session)
    }
  }

  function forgetOutgoing(transferId: unknown): void {
    outgoing.value = Object.fromEntries(
      Object.entries(outgoing.value).filter(([, t]) => t.id !== transferId),
    )
  }

  const TRANSFER_RESULT: Record<string, [string, 'success' | 'warning' | 'info']> = {
    'transfer.accepted': ['对方已接受转接', 'success'],
    'transfer.rejected': ['对方拒绝了转接，会话仍由您接待', 'warning'],
    'transfer.expired': ['对方未及时接受，会话仍由您接待', 'warning'],
  }

  function onSignal(signal: ImSignal): void {
    // "正在输入"是发给访客看的群信令（旁听者也会收到），与会话列表无关。
    if (signal.sendID !== systemUserId || signal.type === 'typing') return
    if (signal.type === 'copilot.alert') {
      addAlert(signal)
      return
    }
    if (signal.type === 'session.assigned' && !signal.data.transfer_id) {
      ElNotification({ title: '新会话', message: '有新的客户分配给您', type: 'info' })
    }
    if (signal.type === 'transfer.requested' || signal.type === 'transfer.cancelled') {
      void loadIncoming()
    }
    if (signal.type === 'session.assist') {
      ElNotification({ title: '邀请协助', message: '同事邀请您协助接待一位客户', type: 'info' })
    }
    const result = TRANSFER_RESULT[signal.type]
    if (result) {
      forgetOutgoing(signal.data.transfer_id)
      ElNotification({ title: '转接', message: result[0], type: result[1] })
    }
    void loadSessions()
  }

  return {
    agent,
    sessions,
    queued,
    ongoing,
    watching,
    closed,
    loadClosed,
    canMonitor,
    roleIn,
    watchersOf,
    returnToAi,
    handoff,
    monitor,
    inviteAssist,
    leave,
    active,
    replyWindow,
    refreshReplyWindow,
    alerts,
    activeMessages,
    hasMore,
    unread,
    imState,
    incoming,
    outgoing,
    error,
    composerInsert,
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
    sendFile,
    retry,
    insertIntoComposer,
    suggest,
    close,
    requestTransfer,
    decideTransfer,
  }
})
