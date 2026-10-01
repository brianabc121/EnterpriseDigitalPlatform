<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import AssistantSettingsTab from '../components/assistant/AssistantSettingsTab.vue'
import BotsTab from '../components/assistant/BotsTab.vue'
import ChatPanel from '../components/assistant/ChatPanel.vue'
import GroupsTab from '../components/assistant/GroupsTab.vue'
import IdentitiesTab from '../components/assistant/IdentitiesTab.vue'
import MyBindingPanel from '../components/assistant/MyBindingPanel.vue'
import { useAuthStore } from '../stores/auth'

/**
 * AI 公司助理（设计文档 §27.3、§27.4）：每个员工都能在这里对话、绑定自己的 IM 账号；
 * 有设置权限的人另外管理助理设置、机器人、群组和绑定。
 */
const auth = useAuthStore()
const canManage = computed(() => auth.can('settings:manage'))
const route = useRoute()
const tab = ref(typeof route.query.tab === 'string' ? route.query.tab : 'chat')
watch(
  () => route.query.tab,
  (value) => {
    if (typeof value === 'string') tab.value = value
  },
)
</script>

<template>
  <div>
    <div class="page-header">
      <h2>AI 助理</h2>
    </div>
    <el-tabs v-model="tab" data-testid="assistant-tabs">
      <el-tab-pane label="对话" name="chat" lazy>
        <ChatPanel />
      </el-tab-pane>
      <el-tab-pane label="我的绑定" name="binding" lazy>
        <MyBindingPanel />
      </el-tab-pane>
      <el-tab-pane v-if="canManage" label="设置" name="settings" lazy>
        <AssistantSettingsTab />
      </el-tab-pane>
      <el-tab-pane v-if="canManage" label="机器人" name="bots" lazy>
        <BotsTab />
      </el-tab-pane>
      <el-tab-pane v-if="canManage" label="群组" name="groups" lazy>
        <GroupsTab />
      </el-tab-pane>
      <el-tab-pane v-if="canManage" label="绑定管理" name="identities" lazy>
        <IdentitiesTab />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>
