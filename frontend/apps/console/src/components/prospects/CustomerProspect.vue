<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { api } from '../../api'
import {
  dueText,
  isoDate,
  LEVEL_LABEL,
  LEVEL_TAG,
  STATUS_LABEL,
  STATUS_TAG,
  type CustomerProspectInfo,
} from '../../prospects'
import ProspectCreateDialog from './ProspectCreateDialog.vue'
import ProspectDrawer from './ProspectDrawer.vue'

/** 客户资料里的意向客户（§35.5）：意向状态、等级和下次跟进，"转入意向客户"或"查看与跟进"。 */
const props = defineProps<{ customerId: string; customerName: string }>()

const info = ref<CustomerProspectInfo | null>(null)
const createOpen = ref(false)
const openId = ref<string | null>(null)
const today = isoDate(new Date())

async function load(): Promise<void> {
  const { data } = await api.GET('/api/v1/opportunities/customer/{customer_id}', {
    params: { path: { customer_id: props.customerId } },
  })
  info.value = data ?? null
}

onMounted(load)
</script>

<template>
  <section v-if="info" class="block" data-testid="customer-prospect">
    <h3>意向客户</h3>
    <template v-if="info.opportunity">
      <div class="line">
        <el-tag :type="STATUS_TAG[info.opportunity.status]" size="small" data-testid="customer-prospect-status">
          {{ STATUS_LABEL[info.opportunity.status] }}
        </el-tag>
        <el-tag :type="LEVEL_TAG[info.opportunity.level]" size="small" effect="plain">
          意向{{ LEVEL_LABEL[info.opportunity.level] }}
        </el-tag>
        <span
          v-if="info.opportunity.status === 'active' && info.opportunity.next_follow_at"
          class="due"
          :class="{ overdue: info.opportunity.overdue }"
        >
          下次跟进 {{ dueText(info.opportunity.next_follow_at, today) }}
        </span>
      </div>
      <div v-if="info.opportunity.owner_name" class="muted">跟进人 {{ info.opportunity.owner_name }}</div>
      <el-button
        size="small"
        class="action"
        data-testid="customer-prospect-open"
        @click="openId = info.opportunity.id"
      >
        {{ info.opportunity.status === 'active' ? '查看与跟进' : '查看' }}
      </el-button>
    </template>
    <template v-else>
      <p class="muted">还没有转入意向客户。没有成交、以后还想跟进的客户可以转入。</p>
      <el-button size="small" data-testid="customer-prospect-create" @click="createOpen = true">
        转入意向客户
      </el-button>
    </template>
    <!-- 用到时才渲染：客户资料常在抽屉里，里面不放隐藏的对话框和抽屉。 -->
    <ProspectCreateDialog
      v-if="createOpen"
      v-model="createOpen"
      :customer="{ id: props.customerId, name: props.customerName }"
      @created="load"
    />
    <ProspectDrawer v-if="openId" :prospect-id="openId" @close="openId = null" @changed="load" />
  </section>
</template>

<style scoped>
.block {
  border-bottom: 1px solid var(--el-border-color-lighter);
  padding: 8px 0;
}

h3 {
  font-size: 13px;
  font-weight: 600;
  margin: 4px 0 8px;
}

.line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}

.due.overdue {
  color: var(--el-color-danger);
}

.muted {
  margin: 4px 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.action {
  margin-top: 6px;
}
</style>
