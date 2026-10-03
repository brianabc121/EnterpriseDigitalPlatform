<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { api } from '../../api'
import {
  amountText,
  dueText,
  isoDate,
  LEVEL_LABEL,
  LEVEL_TAG,
  type CustomerOpportunityInfo,
} from '../../opportunities'
import { useAuthStore } from '../../stores/auth'
import OpportunityCreateDialog from './OpportunityCreateDialog.vue'
import OpportunityDrawer from './OpportunityDrawer.vue'

/**
 * 客户资料里的商机卡片（设计文档 §40.8，客户页和工作台右侧共用）：阶段、预计金额、负责人、下次跟进，
 * "转入商机"或"查看与跟进"，点进详情。
 */
const props = defineProps<{ customerId: string; customerName: string }>()

const auth = useAuthStore()
const info = ref<CustomerOpportunityInfo | null>(null)
const createOpen = ref(false)
const openId = ref<string | null>(null)
const today = isoDate(new Date())

const brief = computed(() => info.value?.opportunity ?? null)
const canCreate = computed(() => auth.can('opportunity:manage'))
const stageType = computed(() => {
  if (!brief.value) return 'info'
  if (brief.value.status === 'suggested') return 'warning'
  if (brief.value.stage_kind === 'won') return 'success'
  return brief.value.stage_kind === 'lost' ? 'info' : 'primary'
})
const stageText = computed(() => {
  if (!brief.value) return ''
  return brief.value.status === 'suggested' ? `待确认 · ${brief.value.stage_name}` : brief.value.stage_name
})

async function load(): Promise<void> {
  const { data } = await api.GET('/api/v1/opportunities/customer/{customer_id}', {
    params: { path: { customer_id: props.customerId } },
  })
  info.value = data ?? null
}

onMounted(load)
</script>

<template>
  <section v-if="info" class="block" data-testid="customer-opportunity">
    <h3>商机</h3>
    <template v-if="brief">
      <div class="line">
        <el-tag :type="stageType" size="small" data-testid="customer-opportunity-status">
          {{ stageText }}
        </el-tag>
        <el-tag :type="LEVEL_TAG[brief.level]" size="small" effect="plain">
          意向{{ LEVEL_LABEL[brief.level] }}
        </el-tag>
        <span v-if="brief.amount" class="amount">{{ amountText(brief.amount) }}</span>
      </div>
      <div class="name">{{ brief.name }}</div>
      <div class="muted">
        <span v-if="brief.owner_name">负责人 {{ brief.owner_name }}</span>
        <span
          v-if="brief.status === 'active' && brief.next_follow_at"
          class="due"
          :class="{ overdue: brief.overdue }"
        >
          下次跟进 {{ dueText(brief.next_follow_at, today) }}
        </span>
      </div>
      <el-button size="small" class="action" data-testid="customer-opportunity-open" @click="openId = brief.id">
        {{ brief.status === 'active' ? '查看与跟进' : '查看' }}
      </el-button>
      <el-button
        v-if="canCreate && (brief.status === 'won' || brief.status === 'lost' || brief.status === 'dismissed')"
        size="small"
        class="action"
        data-testid="customer-opportunity-create"
        @click="createOpen = true"
      >
        再开一个商机
      </el-button>
    </template>
    <template v-else>
      <p class="muted">还没有商机。有意向、还没成交的客户可以转入商机，从新线索开始跟进。</p>
      <el-button
        v-if="canCreate"
        size="small"
        data-testid="customer-opportunity-create"
        @click="createOpen = true"
      >
        转入商机
      </el-button>
    </template>
    <!-- 用到时才渲染：客户资料常在抽屉里，里面不放隐藏的对话框和抽屉。 -->
    <OpportunityCreateDialog
      v-if="createOpen"
      v-model="createOpen"
      :customer="{ id: props.customerId, name: props.customerName }"
      @created="load"
    />
    <OpportunityDrawer v-if="openId" :opportunity-id="openId" @close="openId = null" @changed="load" />
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

.amount {
  font-weight: 600;
}

.name {
  margin-top: 4px;
  font-size: 13px;
}

.due.overdue {
  color: var(--el-color-danger);
}

.muted {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 4px 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.action {
  margin-top: 6px;
}

.action + .action {
  margin-left: 8px;
}
</style>
