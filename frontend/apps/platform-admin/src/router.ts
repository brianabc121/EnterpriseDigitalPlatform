import { createRouter, createWebHistory } from 'vue-router'

import { onUnauthorized } from './api'
import AdminLayout from './layouts/AdminLayout.vue'
import { useAuthStore } from './stores/auth'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('./views/LoginView.vue'),
      meta: { public: true },
    },
    {
      path: '/',
      component: AdminLayout,
      children: [
        { path: '', name: 'tenants', component: () => import('./views/TenantsView.vue') },
        {
          path: 'tenants/:id',
          name: 'tenant',
          component: () => import('./views/TenantDetailView.vue'),
          props: true,
        },
        { path: 'plans', name: 'plans', component: () => import('./views/PlansView.vue') },
        {
          path: 'invoices',
          name: 'invoices',
          component: () => import('./views/InvoicesView.vue'),
        },
        {
          path: 'channels',
          name: 'channels',
          component: () => import('./views/ChannelsView.vue'),
        },
        {
          path: 'providers',
          name: 'providers',
          component: () => import('./views/ProvidersView.vue'),
        },
        {
          path: 'prompts',
          name: 'prompts',
          component: () => import('./views/PromptsView.vue'),
        },
        {
          path: 'llm-usage',
          name: 'llm-usage',
          component: () => import('./views/LlmUsageView.vue'),
        },
        {
          path: 'content',
          name: 'content',
          component: () => import('./views/ContentPolicyView.vue'),
        },
        { path: 'health', name: 'health', component: () => import('./views/HealthView.vue') },
        { path: 'ops', name: 'ops', component: () => import('./views/OpsView.vue') },
        { path: 'audit', name: 'audit', component: () => import('./views/AuditView.vue') },
        {
          path: 'deletions',
          name: 'deletions',
          component: () => import('./views/DeletionsView.vue'),
        },
        {
          path: 'settings',
          name: 'settings',
          component: () => import('./views/SettingsView.vue'),
        },
        {
          path: 'security',
          name: 'security',
          component: () => import('./views/SecurityView.vue'),
        },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.meta.public) return true
  if (!auth.isAuthenticated) return { name: 'login' }
  // 平台要求二次验证而账号还没有设置：先完成设置。
  if (auth.needsMfaSetup && to.name !== 'security') return { name: 'security' }
  return true
})

onUnauthorized(() => {
  useAuthStore().logout()
  void router.push({ name: 'login' })
})
