/** 个人待办（设计文档 §27.2）用到的名称和小工具，与后端 app/modules/tasks 一致。 */
import type { Schemas } from '@edp/api-client'

export type Task = Schemas['TaskOut']
export type TaskView = 'mine' | 'assigned' | 'all'
export type TaskStatus = Schemas['TaskStatus']
export type TaskPriority = Schemas['TaskPriority']
type TagType = 'primary' | 'success' | 'info' | 'warning' | 'danger'

export const TASK_STATUS: Record<string, string> = {
  open: '待办',
  done: '已完成',
  cancelled: '已取消',
}

export const TASK_STATUS_TAG: Record<string, TagType> = {
  open: 'primary',
  done: 'success',
  cancelled: 'info',
}

export const TASK_SOURCE: Record<string, string> = {
  self: '自己新建',
  assigned: '交办',
  assistant: 'AI 助理',
  system: '系统',
}

export const TASK_PRIORITY: [TaskPriority, string][] = [
  ['urgent', '紧急'],
  ['high', '高'],
  ['normal', '普通'],
  ['low', '低'],
]
export const PRIORITY_LABEL: Record<string, string> = Object.fromEntries(TASK_PRIORITY)
export const PRIORITY_TAG: Record<string, TagType> = {
  urgent: 'danger',
  high: 'warning',
  normal: 'info',
  low: 'info',
}

/** 提前提醒的选项（分钟）。 */
export const REMIND_OPTIONS: [number, string][] = [
  [0, '到期时'],
  [15, '提前 15 分钟'],
  [30, '提前 30 分钟'],
  [60, '提前 1 小时'],
  [120, '提前 2 小时'],
  [24 * 60, '提前 1 天'],
]

/** "我的"页签下的快捷筛选。 */
export type MineFilter = 'open' | 'today' | 'overdue' | 'done'
export const MINE_FILTERS: [MineFilter, string][] = [
  ['open', '未完成'],
  ['today', '今天到期'],
  ['overdue', '已逾期'],
  ['done', '已完成'],
]

/** 快捷筛选对应的查询参数。 */
export function mineQuery(filter: MineFilter): {
  status?: TaskStatus
  due?: 'overdue' | 'today' | 'soon'
} {
  if (filter === 'done') return { status: 'done' }
  if (filter === 'today') return { status: 'open', due: 'today' }
  if (filter === 'overdue') return { status: 'open', due: 'overdue' }
  return { status: 'open' }
}

export type DueState = 'overdue' | 'soon' | 'normal' | null

export function dueState(task: Pick<Task, 'due_at' | 'status'>, now = new Date()): DueState {
  if (!task.due_at || task.status !== 'open') return null
  const due = new Date(task.due_at).getTime()
  if (due < now.getTime()) return 'overdue'
  if (due - now.getTime() < 24 * 3600 * 1000) return 'soon'
  return 'normal'
}

/** 截止时间的简短说法："今天 15:00"、"明天 09:30"、"10-09 18:00"。 */
export function dueText(value: string | null, now = new Date()): string {
  if (!value) return '无截止'
  const due = new Date(value)
  const day = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()
  const diff = Math.round((day(due) - day(now)) / (24 * 3600 * 1000))
  const time = due.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
  if (diff === 0) return `今天 ${time}`
  if (diff === 1) return `明天 ${time}`
  if (diff === -1) return `昨天 ${time}`
  const date = `${String(due.getMonth() + 1).padStart(2, '0')}-${String(due.getDate()).padStart(2, '0')}`
  return `${date} ${time}`
}

/** 个人待办有变化时通知菜单角标刷新。 */
export const TASKS_CHANGED = 'edp:tasks-changed'

export function tasksChanged(): void {
  window.dispatchEvent(new Event(TASKS_CHANGED))
}
