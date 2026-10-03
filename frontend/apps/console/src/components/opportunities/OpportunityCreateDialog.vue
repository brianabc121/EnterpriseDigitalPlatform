<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api } from '../../api'
import {
  LEVEL_LABEL,
  LEVELS,
  STATUS_LABEL,
  type CustomerOpportunityInfo,
  type Opportunity,
  type OpportunityLevel,
  type Stage,
} from '../../opportunities'
import { useAuthStore } from '../../stores/auth'

/**
 * 新建商机（设计文档 §40.6）：从客户资料打开时客户是固定的，从商机页面打开时先搜索客户（也可以当场新建
 * 客户）；名称、阶段、等级、预计金额、预计成交日、负责人、想要什么、顾虑、下次跟进。负责人默认是客户的
 * 归属坐席（没有时是自己），改成别人需要分配商机的权限。
 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{ customer?: { id: string; name: string } | null }>()
const emit = defineEmits<{ created: [opportunity: Opportunity] }>()

const auth = useAuthStore()
const canAssign = computed(() => auth.can('opportunity:assign') && auth.can('staff:read'))
const canCreateCustomer = computed(() => !props.customer && auth.can('customer:create'))

const form = reactive({
  customerId: '',
  name: '',
  stageId: '',
  level: 'medium' as OpportunityLevel,
  amount: null as number | null,
  expectedCloseAt: '',
  interest: '',
  concerns: '',
  nextFollowAt: '',
  ownerId: '',
})
const newCustomer = reactive({ on: false, name: '', phone: '', company: '' })
const saving = ref(false)
const followDays = ref(3)
const stages = ref<Stage[]>([])
const info = ref<CustomerOpportunityInfo | null>(null)
const customers = ref<Schemas['CustomerOut'][]>([])
const searching = ref(false)
const staff = ref<Schemas['StaffOut'][]>([])

const openStages = computed(() => stages.value.filter((s) => s.kind === 'open'))
const listed = computed(() => {
  const status = info.value?.opportunity?.status
  return status === 'active' || status === 'suggested' ? status : null
})

async function searchCustomers(q: string): Promise<void> {
  searching.value = true
  const { data } = await api.GET('/api/v1/customers', {
    params: { query: { q: q.trim() || undefined, limit: 20, offset: 0 } },
  })
  searching.value = false
  customers.value = data?.items ?? []
}

async function loadInfo(customerId: string): Promise<void> {
  info.value = null
  if (!customerId) return
  const { data } = await api.GET('/api/v1/opportunities/customer/{customer_id}', {
    params: { path: { customer_id: customerId } },
  })
  info.value = data ?? null
}

async function reset(): Promise<void> {
  Object.assign(form, {
    customerId: props.customer?.id ?? '',
    name: '',
    stageId: '',
    level: 'medium',
    amount: null,
    expectedCloseAt: '',
    interest: '',
    concerns: '',
    nextFollowAt: '',
    ownerId: '',
  })
  Object.assign(newCustomer, { on: false, name: '', phone: '', company: '' })
  info.value = null
  const [settings] = await Promise.all([
    api.GET('/api/v1/opportunities/settings'),
    props.customer ? loadInfo(props.customer.id) : searchCustomers(''),
    canAssign.value && staff.value.length === 0
      ? api.GET('/api/v1/staff').then(({ data }) => {
          staff.value = data?.items.filter((s) => s.status === 'active') ?? []
        })
      : Promise.resolve(),
  ])
  followDays.value = settings.data?.settings.follow_days ?? 3
  stages.value = settings.data?.stages ?? []
  form.stageId = openStages.value[0]?.id ?? ''
}

/** 当场新建客户：称呼必填，手机号和公司可以不填。 */
async function createCustomer(): Promise<string | null> {
  if (!newCustomer.name.trim()) {
    ElMessage.warning('请填写客户的称呼')
    return null
  }
  const { data, error } = await api.POST('/api/v1/customers', {
    body: {
      display_name: newCustomer.name.trim(),
      phone: newCustomer.phone.trim() || null,
      company: newCustomer.company.trim() || null,
    },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return null
  }
  return data.id
}

async function save(): Promise<void> {
  saving.value = true
  const customerId = newCustomer.on ? await createCustomer() : form.customerId
  if (!customerId) {
    saving.value = false
    if (!newCustomer.on) ElMessage.warning('请选择客户')
    return
  }
  const { data, error } = await api.POST('/api/v1/opportunities', {
    body: {
      customer_id: customerId,
      name: form.name.trim() || null,
      stage_id: form.stageId || null,
      level: form.level,
      interest: form.interest.trim() || null,
      concerns: form.concerns.trim() || null,
      amount: form.amount === null ? null : form.amount.toFixed(2),
      expected_close_at: form.expectedCloseAt || null,
      next_follow_at: form.nextFollowAt || null,
      owner_id: form.ownerId || null,
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已转入商机')
  open.value = false
  emit('created', data)
}

function recentDeal(value: string): string {
  return new Date(value).toLocaleDateString('zh-CN')
}

watch(
  open,
  (value) => {
    if (value) void reset()
  },
  { immediate: true },
)
watch(
  () => form.customerId,
  (id) => {
    if (!props.customer) void loadInfo(id)
  },
)
</script>

<template>
  <el-dialog v-model="open" title="新建商机" width="560px" append-to-body data-testid="opp-create">
    <el-form label-width="96px" @submit.prevent="save">
      <el-form-item label="客户" required>
        <span v-if="props.customer" data-testid="opp-create-customer">{{ props.customer.name }}</span>
        <template v-else>
          <el-select
            v-if="!newCustomer.on"
            v-model="form.customerId"
            filterable
            remote
            :remote-method="searchCustomers"
            :loading="searching"
            placeholder="搜索客户名称、公司或手机号"
            class="grow"
            data-testid="opp-customer-select"
          >
            <el-option
              v-for="c in customers"
              :key="c.id"
              :label="c.company ? `${c.display_name}（${c.company}）` : c.display_name"
              :value="c.id"
            />
          </el-select>
          <el-input
            v-else
            v-model="newCustomer.name"
            placeholder="客户的称呼"
            class="grow"
            data-testid="opp-new-customer-name"
          />
          <el-button
            v-if="canCreateCustomer"
            link
            type="primary"
            class="toggle"
            data-testid="opp-new-customer"
            @click="newCustomer.on = !newCustomer.on"
          >
            {{ newCustomer.on ? '选已有的客户' : '当场新建客户' }}
          </el-button>
        </template>
      </el-form-item>
      <template v-if="newCustomer.on">
        <el-form-item label="手机号">
          <el-input v-model="newCustomer.phone" maxlength="20" data-testid="opp-new-customer-phone" />
        </el-form-item>
        <el-form-item label="公司">
          <el-input v-model="newCustomer.company" maxlength="100" data-testid="opp-new-customer-company" />
        </el-form-item>
      </template>
      <el-alert
        v-if="listed"
        type="warning"
        :closable="false"
        show-icon
        class="tip"
        :title="`这个客户已经有进行中的商机（${STATUS_LABEL[listed]}），一个客户同时只跟一条`"
        data-testid="opp-already"
      />
      <el-alert
        v-else-if="info?.recent_deal_at"
        type="info"
        :closable="false"
        show-icon
        class="tip"
        :title="`这个客户 ${recentDeal(info.recent_deal_at)} 刚下过单，老客户有新的需求也可以再开商机`"
        data-testid="opp-recent-deal"
      />
      <el-form-item label="商机名称">
        <el-input
          v-model="form.name"
          maxlength="128"
          placeholder="不填时按想要什么或客户称呼"
          data-testid="opp-name"
        />
      </el-form-item>
      <el-form-item label="阶段">
        <el-select v-model="form.stageId" data-testid="opp-stage">
          <el-option v-for="s in openStages" :key="s.id" :label="s.name" :value="s.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="意向等级">
        <el-radio-group v-model="form.level" data-testid="opp-level">
          <el-radio-button v-for="level in LEVELS" :key="level" :value="level">
            {{ LEVEL_LABEL[level] }}
          </el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="预计金额">
        <el-input-number
          v-model="form.amount"
          :min="0"
          :precision="2"
          :controls="false"
          placeholder="元"
          class="amount"
          data-testid="opp-amount"
        />
        <el-date-picker
          v-model="form.expectedCloseAt"
          type="date"
          value-format="YYYY-MM-DD"
          placeholder="预计成交日"
          class="close"
          data-testid="opp-close"
        />
      </el-form-item>
      <el-form-item label="想要什么">
        <el-input
          v-model="form.interest"
          type="textarea"
          :rows="2"
          maxlength="1000"
          placeholder="商品、规格、数量、用途、预算"
          data-testid="opp-interest"
        />
      </el-form-item>
      <el-form-item label="顾虑">
        <el-input
          v-model="form.concerns"
          type="textarea"
          :rows="2"
          maxlength="1000"
          placeholder="为什么还没下单：价格、交期、还在比较……"
          data-testid="opp-concerns"
        />
      </el-form-item>
      <el-form-item label="下次跟进">
        <el-date-picker
          v-model="form.nextFollowAt"
          type="date"
          value-format="YYYY-MM-DD"
          :placeholder="`不填时 ${followDays} 天后`"
          data-testid="opp-next-date"
        />
      </el-form-item>
      <el-form-item v-if="canAssign" label="负责人">
        <el-select v-model="form.ownerId" clearable placeholder="默认客户的归属坐席" data-testid="opp-owner">
          <el-option v-for="s in staff" :key="s.id" :label="s.display_name" :value="s.id" />
        </el-select>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button
        type="primary"
        :loading="saving"
        :disabled="!!listed"
        data-testid="opp-create-save"
        @click="save"
      >
        转入
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.tip {
  margin: -6px 0 12px;
}

.grow {
  flex: 1;
}

.toggle {
  margin-left: 8px;
}

.amount {
  width: 160px;
}

.close {
  margin-left: 8px;
}
</style>
