import type { Schemas } from '@edp/api-client'

/** 首页的数字（DashboardView 统一取数后传给各岗位的一块；没有显示相应菜单时为空）。 */
export interface HomeData {
  live: Schemas['Realtime'] | null
  todos: Schemas['TodoCounts'] | null
  orders: Schemas['OrderCounts'] | null
  warehouse: Schemas['WarehouseCounts'] | null
  receivables: Schemas['ReceivableSummary'] | null
  opportunities: Schemas['OpportunityStats'] | null
}
