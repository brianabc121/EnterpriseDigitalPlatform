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
import { inWecom, isMobile } from './wecom'

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
  wecom: () => import('./views/WecomView.vue'),
  broadcasts: () => import('./views/BroadcastsView.vue'),
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
    path: '/signup',
    name: 'signup',
    component: () => import('./views/SignupView.vue'),
    meta: { public: true, title: '免费试用' },
  },
  {
    // 企业微信扫码登录、企业微信内网页授权（免登）、员工自行绑定后跳回这里。
    path: '/wecom/login',
    name: 'wecom-login',
    component: () => import('./views/WecomLoginView.vue'),
    meta: { public: true, title: '企业微信登录' },
  },
  {
    // 企业微信聊天工具栏里的侧边栏（独立的移动端布局）。
    path: '/wecom/sidebar',
    name: 'wecom-sidebar',
    component: () => import('./views/WecomSidebarView.vue'),
    meta: { public: true, title: '客户助手' },
  },
  {
    // 企业微信手机端的坐席工作台（应用消息提醒点进来后免登进入）。
    path: '/m',
    name: 'mobile-workbench',
    component: () => import('./views/MobileWorkbenchView.vue'),
    meta: { permission: 'workbench:use', title: '工作台' },
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
    if (to.name === 'wecom-login' || to.name === 'wecom-sidebar') return true
    return to.name === 'login' && auth.isAuthenticated
      ? firstAccessiblePath(auth.permissions)
      : true
  }
  if (!auth.isAuthenticated) return { name: 'login', query: { redirect: to.fullPath } }
  if (to.meta.permission && !auth.can(to.meta.permission)) return { name: 'forbidden' }
  // 企业微信手机端打开工作台时进入手机版。
  if (to.name === 'workbench' && inWecom() && isMobile()) {
    return { name: 'mobile-workbench', query: to.query }
  }
  return true
})

router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} · EDP 控制台` : 'EDP 控制台'
})

onUnauthorized(() => {
  useAuthStore().clear()
  void router.push({ name: 'login', query: { redirect: router.currentRoute.value.fullPath } })
})
