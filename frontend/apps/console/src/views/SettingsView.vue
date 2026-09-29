<script setup lang="ts">
import { computed, ref } from 'vue'

import AgentsTab from '../components/settings/AgentsTab.vue'
import BillingTab from '../components/settings/BillingTab.vue'
import ChannelsTab from '../components/settings/ChannelsTab.vue'
import DataTab from '../components/settings/DataTab.vue'
import RoutingPoliciesTab from '../components/settings/RoutingPoliciesTab.vue'
import SkillGroupsTab from '../components/settings/SkillGroupsTab.vue'
import SupportTab from '../components/settings/SupportTab.vue'
import UsageTab from '../components/settings/UsageTab.vue'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const canRoute = computed(() => auth.can('routing:manage'))
const canManageTenant = computed(() => auth.can('tenant:manage'))
const tab = ref('channels')
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
      <el-tab-pane v-if="canRoute" label="技能组" name="groups" lazy>
        <SkillGroupsTab />
      </el-tab-pane>
      <el-tab-pane v-if="canRoute" label="路由策略" name="policies" lazy>
        <RoutingPoliciesTab />
      </el-tab-pane>
      <el-tab-pane v-if="canRoute" label="坐席" name="agents" lazy>
        <AgentsTab />
      </el-tab-pane>
      <el-tab-pane label="用量" name="usage" lazy>
        <UsageTab />
      </el-tab-pane>
      <el-tab-pane label="套餐与账单" name="billing" lazy>
        <BillingTab />
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
