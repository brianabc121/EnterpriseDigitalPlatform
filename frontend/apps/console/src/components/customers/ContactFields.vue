<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api } from '../../api'
import { useAuthStore } from '../../stores/auth'

/** 客户联系方式：手机号、邮箱默认掩码显示，有权限的员工可以查看明文（记操作日志）。 */
const props = defineProps<{ customer: Schemas['CustomerOut'] }>()
const emit = defineEmits<{ saved: [customer: Schemas['CustomerDetail']] }>()

const auth = useAuthStore()
const canReveal = computed(() => auth.can('customer:view_sensitive'))
const revealed = ref<Schemas['CustomerSensitive'] | null>(null)
const editing = ref(false)
const saving = ref(false)
const form = reactive({ phone: '', email: '', company: '' })

watch(
  () => props.customer.id,
  () => {
    revealed.value = null
    editing.value = false
  },
)

async function reveal(): Promise<void> {
  const { data, error } = await api.GET('/api/v1/customers/{customer_id}/sensitive', {
    params: { path: { customer_id: props.customer.id } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  revealed.value = data
}

function startEdit(): void {
  // 手机号、邮箱不回显明文：留空表示不修改。
  Object.assign(form, { phone: '', email: '', company: props.customer.company ?? '' })
  editing.value = true
}

async function save(): Promise<void> {
  const body: Schemas['CustomerUpdate'] = { company: form.company.trim() }
  if (form.phone.trim()) body.phone = form.phone.trim()
  if (form.email.trim()) body.email = form.email.trim()
  saving.value = true
  const { data, error } = await api.PATCH('/api/v1/customers/{customer_id}', {
    params: { path: { customer_id: props.customer.id } },
    body,
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  revealed.value = null
  editing.value = false
  ElMessage.success('已保存联系方式')
  emit('saved', data)
}

async function clear(field: 'phone' | 'email'): Promise<void> {
  const { data, error } = await api.PATCH('/api/v1/customers/{customer_id}', {
    params: { path: { customer_id: props.customer.id } },
    body: { [field]: '' },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  revealed.value = null
  emit('saved', data)
}
</script>

<template>
  <div class="contact" data-testid="customer-contact">
    <template v-if="!editing">
      <dl>
        <dt>手机号</dt>
        <dd data-testid="contact-phone">
          {{ revealed ? (revealed.phone ?? '—') : (customer.phone ?? '—') }}
          <el-button v-if="customer.phone" link size="small" @click="clear('phone')">清除</el-button>
        </dd>
        <dt>邮箱</dt>
        <dd data-testid="contact-email">
          {{ revealed ? (revealed.email ?? '—') : (customer.email ?? '—') }}
          <el-button v-if="customer.email" link size="small" @click="clear('email')">清除</el-button>
        </dd>
        <dt>公司</dt>
        <dd>{{ customer.company ?? '—' }}</dd>
      </dl>
      <div class="actions">
        <el-button
          v-if="canReveal && (customer.phone || customer.email) && !revealed"
          size="small"
          data-testid="reveal-contact"
          @click="reveal"
        >
          查看完整联系方式
        </el-button>
        <el-button size="small" data-testid="edit-contact" @click="startEdit">编辑</el-button>
      </div>
    </template>
    <el-form v-else label-position="top" size="small" @submit.prevent="save">
      <el-form-item label="手机号">
        <el-input
          v-model="form.phone"
          :placeholder="customer.phone ? `${customer.phone}（留空不修改）` : '手机号'"
          maxlength="32"
          data-testid="contact-phone-input"
        />
      </el-form-item>
      <el-form-item label="邮箱">
        <el-input
          v-model="form.email"
          :placeholder="customer.email ? `${customer.email}（留空不修改）` : '邮箱'"
          maxlength="254"
        />
      </el-form-item>
      <el-form-item label="公司">
        <el-input v-model="form.company" maxlength="128" />
      </el-form-item>
      <el-button size="small" @click="editing = false">取消</el-button>
      <el-button type="primary" size="small" :loading="saving" data-testid="save-contact" @click="save">
        保存
      </el-button>
    </el-form>
  </div>
</template>

<style scoped>
dl {
  display: grid;
  grid-template-columns: 56px 1fr;
  gap: 4px 8px;
  margin: 0 0 8px;
  font-size: 13px;
}

dt {
  color: var(--el-text-color-secondary);
}

dd {
  margin: 0;
  word-break: break-all;
}

.actions {
  display: flex;
  gap: 4px;
}
</style>
