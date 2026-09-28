<script setup lang="ts">
import { computed } from 'vue'

import { visibleMenus } from '../menu'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const modules = computed(() => visibleMenus(auth.permissions).filter((m) => m.name !== 'dashboard'))
</script>

<template>
  <div>
    <div class="page-header">
      <h2>你好，{{ auth.me?.display_name }}</h2>
    </div>
    <el-descriptions :column="2" border>
      <el-descriptions-item label="企业">{{ auth.me?.tenant.name }}（{{ auth.me?.tenant.code }}）</el-descriptions-item>
      <el-descriptions-item label="角色">
        <el-tag v-for="role in auth.me?.roles" :key="role" class="role">{{ role }}</el-tag>
      </el-descriptions-item>
    </el-descriptions>
    <h3 class="section">可用模块</h3>
    <el-space wrap>
      <router-link v-for="item in modules" :key="item.name" :to="item.path">
        <el-card shadow="hover" class="module">{{ item.title }}</el-card>
      </router-link>
    </el-space>
  </div>
</template>

<style scoped>
.role + .role {
  margin-left: 6px;
}

.section {
  margin: 24px 0 12px;
  font-size: 15px;
}

.module {
  width: 160px;
  text-align: center;
}

a {
  text-decoration: none;
}
</style>
