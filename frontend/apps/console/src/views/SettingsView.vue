<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import AgentsTab from '../components/settings/AgentsTab.vue'
import BillingTab from '../components/settings/BillingTab.vue'
import ChannelsTab from '../components/settings/ChannelsTab.vue'
import ConsoleTab from '../components/settings/ConsoleTab.vue'
import DataTab from '../components/settings/DataTab.vue'
import IntegrationTab from '../components/settings/IntegrationTab.vue'
import MailboxesTab from '../components/settings/MailboxesTab.vue'
import PrintersTab from '../components/settings/PrintersTab.vue'
import RetentionTab from '../components/settings/RetentionTab.vue'
import RoutingPoliciesTab from '../components/settings/RoutingPoliciesTab.vue'
import SkillGroupsTab from '../components/settings/SkillGroupsTab.vue'
import SupportTab from '../components/settings/SupportTab.vue'
import UsageTab from '../components/settings/UsageTab.vue'
import OrderSettingsTab from '../components/orders/OrderSettingsTab.vue'
import TodoTypesTab from '../components/todos/TodoTypesTab.vue'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const canRoute = computed(() => auth.can('routing:manage'))
const canManageTenant = computed(() => auth.can('tenant:manage'))
const canConfigTodos = computed(() => auth.can('todo:config'))
const canIntegrate = computed(() => auth.can('integration:manage'))
const canConfigOrders = computed(
  () => auth.can('order:config') && auth.me?.features?.orders !== false,
)
const canPrint = computed(() => auth.can('print:manage') && auth.me?.features?.orders !== false)
// 站内信等链接可以直接打开某个页签（/settings?tab=mail）。
const route = useRoute()
const tab = ref(typeof route.query.tab === 'string' ? route.query.tab : 'channels')
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
      <h2>设置</h2>
    </div>
    <el-tabs v-model="tab" data-testid="settings-tabs">
      <el-tab-pane label="接入渠道" name="channels" lazy>
        <ChannelsTab />
      </el-tab-pane>
      <el-tab-pane label="邮箱" name="mail" lazy>
        <MailboxesTab />
      </el-tab-pane>
      <el-tab-pane v-if="canRoute" label="技能组" name="groups" lazy>
        <SkillGroupsTab />
      </el-tab-pane>
      <el-tab-pane v-if="canRoute" label="路由策略" name="policies" lazy>
        <RoutingPoliciesTab />
      </el-tab-pane>
      <el-tab-pane v-if="canRoute" label="坐席" name="agents" lazy>
        <AgentsTab />
      </el-tab-pane>
      <el-tab-pane label="控制台" name="console" lazy>
        <ConsoleTab />
      </el-tab-pane>
      <el-tab-pane v-if="canConfigTodos" label="待办" name="todos" lazy>
        <TodoTypesTab />
      </el-tab-pane>
      <el-tab-pane v-if="canConfigOrders" label="订单" name="orders" lazy>
        <OrderSettingsTab />
      </el-tab-pane>
      <el-tab-pane v-if="canPrint" label="打印" name="print" lazy>
        <PrintersTab />
      </el-tab-pane>
      <el-tab-pane v-if="canIntegrate" label="企业系统对接" name="integration" lazy>
        <IntegrationTab />
      </el-tab-pane>
      <el-tab-pane label="用量" name="usage" lazy>
        <UsageTab />
      </el-tab-pane>
      <el-tab-pane label="套餐与账单" name="billing" lazy>
        <BillingTab />
      </el-tab-pane>
      <el-tab-pane label="数据保留" name="retention" lazy>
        <RetentionTab />
      </el-tab-pane>
      <el-tab-pane v-if="canManageTenant" label="数据与注销" name="data" lazy>
        <DataTab />
      </el-tab-pane>
      <el-tab-pane v-if="canManageTenant" label="平台访问授权" name="support" lazy>
        <SupportTab />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>
