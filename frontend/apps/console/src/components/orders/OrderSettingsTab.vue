<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'

import { api } from '../../api'
import { PAYMENT_METHODS } from '../../orders'

/**
 * 设置 → 订单（设计文档 §25.9）：编号前缀、提交时的必填项、启用的收款方式和定金规则、是否有发货环节、
 * AI 告知建议零售价与 AI 下单、折扣上限、通知模板、跟踪链接的有效期、客户删除请求的处理方式。
 * 订单的处理人按"订单审核"待办类型的分派规则确定（在"设置 → 待办"里修改）。
 */
type Settings = Schemas['OrderSettings']
const form = ref<Settings | null>(null)
const saving = ref(false)

type RequiredField = NonNullable<Settings['required_fields']>[number]
const REQUIRED: [RequiredField, string][] = [
  ['receiver_name', '收货人'],
  ['receiver_phone', '联系电话'],
  ['receiver_address', '收货地址'],
  ['expected_at', '期望时间'],
]
const TEMPLATES: [keyof Settings, string, string][] = [
  ['confirm_template', '确认通知', '{no} 订单号、{summary} 商品、{total} 合计、{payment} 收款方式、{link} 跟踪链接'],
  ['ship_template', '发货通知', '{company} 物流公司、{tracking_no} 单号，另可用 {no}、{link}'],
  ['complete_template', '完成通知', '{no} 订单号'],
  ['cancel_template', '取消通知', '{reason} 取消原因，另可用 {no}'],
  ['update_template', '修改通知', '{summary}、{total}、{link}'],
]

onMounted(async () => {
  const { data, error } = await api.GET('/api/v1/admin/order-settings')
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  form.value = data
})

async function save(): Promise<void> {
  if (!form.value) return
  if (!form.value.payment_methods?.length) {
    ElMessage.warning('至少启用一种收款方式')
    return
  }
  saving.value = true
  const { data, error } = await api.PUT('/api/v1/admin/order-settings', { body: form.value })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  form.value = data
  ElMessage.success('已保存')
}
</script>

<template>
  <div v-if="form" class="settings" data-testid="order-settings">
    <el-form label-width="150px">
      <h4>编号与必填项</h4>
      <el-form-item label="订单号前缀">
        <el-input v-model="form.prefix" maxlength="6" class="short" data-testid="order-prefix" />
        <span class="muted">1 到 6 个大写字母，例如 SO20260930-0007</span>
      </el-form-item>
      <el-form-item label="提交时必须有">
        <el-checkbox-group v-model="form.required_fields">
          <el-checkbox v-for="[value, label] in REQUIRED" :key="value" :value="value">{{ label }}</el-checkbox>
        </el-checkbox-group>
      </el-form-item>

      <h4>收款与履约</h4>
      <el-form-item label="启用的收款方式">
        <el-checkbox-group v-model="form.payment_methods" data-testid="order-methods">
          <el-checkbox v-for="[value, label] in PAYMENT_METHODS" :key="value" :value="value">{{ label }}</el-checkbox>
        </el-checkbox-group>
      </el-form-item>
      <el-form-item label="预付定金的尾款">
        <el-radio-group v-model="form.deposit_balance">
          <el-radio value="before_ship">发货前收清</el-radio>
          <el-radio value="on_delivery">货到时收取</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="有发货环节">
        <el-switch v-model="form.shipping_enabled" />
        <span class="muted">服务类订单可以关闭：处理中直接完成</span>
      </el-form-item>
      <el-form-item label="优惠上限">
        <el-input-number v-model="form.discount_limit" :min="0" :max="100" />
        <span class="muted">% （相对建议零售价）；超过时需要有审批权限的主管操作</span>
      </el-form-item>
      <el-form-item label="每行数量上限">
        <el-input-number v-model="form.max_quantity" :min="1" :max="100000" />
      </el-form-item>

      <h4>AI</h4>
      <el-form-item label="AI 告知建议零售价">
        <el-switch v-model="form.ai_price_enabled" data-testid="order-ai-price" />
        <span class="muted">AI 永远不会告诉客户成本价</span>
      </el-form-item>
      <el-form-item label="AI 下单">
        <el-radio-group v-model="form.ai_order_mode" data-testid="order-ai-mode">
          <el-radio value="off">关闭（客户要购买时转人工）</el-radio>
          <el-radio value="collect">采集信息，客户确认后提交审核</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="每位客户每天">
        <el-input-number v-model="form.ai_daily_limit" :min="1" :max="20" />
        <span class="muted">个 AI 订单以内</span>
      </el-form-item>
      <el-form-item label="跟进未完成的订单">
        <el-switch v-model="form.draft_followup" />
        <span class="muted">客户中途离开时，AI 采集的草稿</span>
        <el-input-number
          v-model="form.draft_followup_minutes"
          :disabled="!form.draft_followup"
          :min="10"
          :max="1440"
          size="small"
          class="minutes"
        />
        <span class="muted">分钟没有更新就生成一条跟进待办</span>
      </el-form-item>
      <el-form-item label="提交后的答复">
        <el-input v-model="form.promise_text" type="textarea" :rows="2" maxlength="500" />
        <span class="muted">{no} 为订单号</span>
      </el-form-item>

      <h4>通知客户</h4>
      <el-form-item v-for="[key, label, hint] in TEMPLATES" :key="key" :label="label">
        <el-input v-model="(form[key] as string)" type="textarea" :rows="2" maxlength="1000" />
        <span class="muted">可用：{{ hint }}</span>
      </el-form-item>

      <h4>跟踪链接与个人信息</h4>
      <el-form-item label="跟踪链接保留">
        <el-input-number v-model="form.tracking_days" :min="7" :max="3650" />
        <span class="muted">天（订单完成或取消后）</span>
      </el-form-item>
      <el-form-item label="客户申请删除个人信息">
        <el-radio-group v-model="form.erase_mode">
          <el-radio value="anonymize">清空订单里的个人信息（保留商品、金额和收款用于统计）</el-radio>
          <el-radio value="delete">整单删除</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item>
        <p class="muted">订单的处理人按"订单审核"待办类型的分派规则确定，在"设置 → 待办"里修改。</p>
      </el-form-item>
      <el-form-item>
        <el-button type="primary" :loading="saving" data-testid="order-settings-save" @click="save">保存</el-button>
      </el-form-item>
    </el-form>
  </div>
</template>

<style scoped>
.settings {
  max-width: 820px;
}

h4 {
  margin: 20px 0 8px;
  font-size: 14px;
}

.short {
  width: 120px;
  margin-right: 8px;
}

.muted {
  margin-left: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.minutes {
  margin-left: 8px;
}
</style>
