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
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.meta.public) return true
  return auth.isAuthenticated ? true : { name: 'login' }
})

onUnauthorized(() => {
  useAuthStore().logout()
  void router.push({ name: 'login' })
})
