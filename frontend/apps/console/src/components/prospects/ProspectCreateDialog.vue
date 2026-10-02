<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api } from '../../api'
import {
  LEVEL_LABEL,
  LEVELS,
  STATUS_LABEL,
  type CustomerProspectInfo,
  type Prospect,
  type ProspectLevel,
} from '../../prospects'
import { useAuthStore } from '../../stores/auth'

/**
 * 转入意向客户（§35.3）：从客户资料打开时客户是固定的，从意向客户列表打开时先搜索客户。跟进人默认
 * 是客户的归属坐席（没有时是自己），改成别人需要分配客户的权限。
 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{ customer?: { id: string; name: string } | null }>()
const emit = defineEmits<{ created: [prospect: Prospect] }>()

const auth = useAuthStore()
const canAssign = computed(() => auth.can('customer:assign') && auth.can('staff:read'))

const form = reactive({
  customerId: '',
  level: 'medium' as ProspectLevel,
  interest: '',
  concerns: '',
  nextFollowAt: '',
  followerId: '',
})
const saving = ref(false)
const followDays = ref(3)
const info = ref<CustomerProspectInfo | null>(null)
const customers = ref<Schemas['CustomerOut'][]>([])
const searching = ref(false)
const staff = ref<Schemas['StaffOut'][]>([])

const listed = computed(() => {
  const status = info.value?.prospect?.status
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
  const { data } = await api.GET('/api/v1/prospects/customer/{customer_id}', {
    params: { path: { customer_id: customerId } },
  })
  info.value = data ?? null
}

async function reset(): Promise<void> {
  Object.assign(form, {
    customerId: props.customer?.id ?? '',
    level: 'medium',
    interest: '',
    concerns: '',
    nextFollowAt: '',
    followerId: '',
  })
  info.value = null
  const [settings] = await Promise.all([
    api.GET('/api/v1/prospects/settings'),
    props.customer ? loadInfo(props.customer.id) : searchCustomers(''),
    canAssign.value && staff.value.length === 0
      ? api.GET('/api/v1/staff').then(({ data }) => {
          staff.value = data?.items.filter((s) => s.status === 'active') ?? []
        })
      : Promise.resolve(),
  ])
  followDays.value = settings.data?.settings.follow_days ?? 3
}

async function save(): Promise<void> {
  if (!form.customerId) {
    ElMessage.warning('请选择客户')
    return
  }
  saving.value = true
  const { data, error } = await api.POST('/api/v1/prospects', {
    body: {
      customer_id: form.customerId,
      level: form.level,
      interest: form.interest.trim() || null,
      concerns: form.concerns.trim() || null,
      next_follow_at: form.nextFollowAt || null,
      follower_id: form.followerId || null,
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已转入意向客户')
  open.value = false
  emit('created', data)
}

function recentDeal(value: string): string {
  return new Date(value).toLocaleDateString('zh-CN')
}

watch(open, (value) => {
  if (value) void reset()
})
watch(
  () => form.customerId,
  (id) => {
    if (!props.customer) void loadInfo(id)
  },
)
</script>

<template>
  <el-dialog v-model="open" title="转入意向客户" width="520px" data-testid="prospect-create">
    <el-form label-width="96px" @submit.prevent="save">
      <el-form-item label="客户" required>
        <span v-if="props.customer" data-testid="prospect-create-customer">{{ props.customer.name }}</span>
        <el-select
          v-else
          v-model="form.customerId"
          filterable
          remote
          :remote-method="searchCustomers"
          :loading="searching"
          placeholder="搜索客户名称、公司或手机号"
          data-testid="prospect-customer-select"
        >
          <el-option
            v-for="c in customers"
            :key="c.id"
            :label="c.company ? `${c.display_name}（${c.company}）` : c.display_name"
            :value="c.id"
          />
        </el-select>
      </el-form-item>
      <el-alert
        v-if="listed"
        type="warning"
        :closable="false"
        show-icon
        class="tip"
        :title="`这个客户已经在意向客户里了（${STATUS_LABEL[listed]}）`"
        data-testid="prospect-already"
      />
      <el-alert
        v-else-if="info?.recent_deal_at"
        type="info"
        :closable="false"
        show-icon
        class="tip"
        :title="`这个客户 ${recentDeal(info.recent_deal_at)} 刚下过单，老客户有新的需求也可以转入`"
        data-testid="prospect-recent-deal"
      />
      <el-form-item label="意向等级">
        <el-radio-group v-model="form.level" data-testid="prospect-level">
          <el-radio-button v-for="level in LEVELS" :key="level" :value="level">
            {{ LEVEL_LABEL[level] }}
          </el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="想要什么">
        <el-input
          v-model="form.interest"
          type="textarea"
          :rows="2"
          maxlength="1000"
          placeholder="商品、规格、数量、用途、预算"
          data-testid="prospect-interest"
        />
      </el-form-item>
      <el-form-item label="顾虑">
        <el-input
          v-model="form.concerns"
          type="textarea"
          :rows="2"
          maxlength="1000"
          placeholder="为什么还没下单：价格、交期、还在比较……"
          data-testid="prospect-concerns"
        />
      </el-form-item>
      <el-form-item label="下次跟进">
        <el-date-picker
          v-model="form.nextFollowAt"
          type="date"
          value-format="YYYY-MM-DD"
          :placeholder="`不填时 ${followDays} 天后`"
          data-testid="prospect-next-date"
        />
      </el-form-item>
      <el-form-item v-if="canAssign" label="跟进人">
        <el-select
          v-model="form.followerId"
          clearable
          placeholder="默认客户的归属坐席"
          data-testid="prospect-follower"
        >
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
        data-testid="prospect-create-save"
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
</style>
