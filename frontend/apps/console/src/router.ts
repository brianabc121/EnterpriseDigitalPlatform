import type { Permission } from '@edp/api-client'
import {
  createRouter,
  createWebHistory,
  type RouteComponent,
  type RouteRecordRaw,
} from 'vue-router'

import { onUnauthorized } from './api'
import MainLayout from './layouts/MainLayout.vue'
import { MENU, firstAccessiblePath } from './menu'
import { useAuthStore } from './stores/auth'

declare module 'vue-router' {
  interface RouteMeta {
    public?: boolean
    permission?: Permission
    title?: string
  }
}

type LazyView = () => Promise<RouteComponent>

const Placeholder: LazyView = () => import('./views/PlaceholderView.vue')
const VIEWS: Record<string, LazyView> = {
  dashboard: () => import('./views/DashboardView.vue'),
  workbench: () => import('./views/WorkbenchView.vue'),
  sessions: () => import('./views/SessionsView.vue'),
  tickets: () => import('./views/TicketsView.vue'),
  customers: () => import('./views/CustomersView.vue'),
  knowledge: () => import('./views/KnowledgeView.vue'),
  ai: () => import('./views/AiView.vue'),
  staff: () => import('./views/StaffView.vue'),
  reports: () => import('./views/ReportsView.vue'),
  settings: () => import('./views/SettingsView.vue'),
}

const pages: RouteRecordRaw[] = MENU.map((item) => ({
  path: item.path.slice(1),
  name: item.name,
  component: VIEWS[item.name] ?? Placeholder,
  meta: { permission: item.permission, title: item.title },
}))

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('./views/LoginView.vue'),
    meta: { public: true, title: '登录' },
  },
  {
    path: '/',
    component: MainLayout,
    children: [
      ...pages,
      {
        path: 'forbidden',
        name: 'forbidden',
        component: () => import('./views/ForbiddenView.vue'),
        meta: { title: '无权限' },
      },
    ],
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('./views/NotFoundView.vue'),
    meta: { public: true, title: '页面不存在' },
  },
]

export const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  await auth.restore()
  if (to.meta.public) {
    return to.name === 'login' && auth.isAuthenticated
      ? firstAccessiblePath(auth.permissions)
      : true
  }
  if (!auth.isAuthenticated) return { name: 'login', query: { redirect: to.fullPath } }
  if (to.meta.permission && !auth.can(to.meta.permission)) return { name: 'forbidden' }
  return true
})

router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} · EDP 控制台` : 'EDP 控制台'
})

onUnauthorized(() => {
  useAuthStore().clear()
  void router.push({ name: 'login', query: { redirect: router.currentRoute.value.fullPath } })
})
