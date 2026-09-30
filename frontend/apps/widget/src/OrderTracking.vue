<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { money, shortTime } from './orders'
import { fetchTracking, type Tracking } from './visitor'

/**
 * 订单跟踪页（设计文档 §25.6）：凭跟踪链接查看，不需要登录，在微信里打开同样可用。显示进度、商品和
 * 金额、收款方式与收款状态、物流、掩码后的收货信息和客户可见的动态，以及"联系客服"入口。
 */
const props = defineProps<{ token: string }>()

const order = ref<Tracking | null>(null)
const error = ref<string | null>(null)
const loading = ref(true)

onMounted(async () => {
  document.title = '订单进度'
  if (!props.token) {
    error.value = '订单链接已失效，请联系客服'
    loading.value = false
    return
  }
  try {
    order.value = await fetchTracking(props.token)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '订单链接已失效，请联系客服'
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="widget tracking" data-testid="order-tracking">
    <header class="header">
      <span class="title">订单进度</span>
    </header>
    <p v-if="loading" class="empty">正在加载…</p>
    <p v-else-if="error" class="error" role="alert" data-testid="tracking-error">{{ error }}</p>
    <div v-else-if="order" class="body">
      <section class="card">
        <div class="head">
          <span class="no" data-testid="tracking-no">订单 {{ order.no }}</span>
          <span class="status" data-testid="tracking-status">{{ order.status_label }}</span>
        </div>
        <ol v-if="order.status !== 'cancelled'" class="steps" data-testid="tracking-steps">
          <li v-for="step in order.steps" :key="step.key" :class="{ done: step.done }">
            <span class="dot" />
            <span class="label">{{ step.label }}</span>
            <span v-if="step.at" class="time">{{ shortTime(step.at) }}</span>
          </li>
        </ol>
      </section>

      <section class="card">
        <h4>商品</h4>
        <div v-for="(item, i) in order.items" :key="i" class="item" data-testid="tracking-item">
          <img v-if="item.image_url" :src="item.image_url" alt="" class="thumb" />
          <div class="item-text">
            <div>{{ item.name }} <span class="muted">{{ item.spec }}</span></div>
            <div class="muted">{{ money(item.unit_price) }} × {{ item.quantity }}</div>
          </div>
          <span class="amount">{{ money(item.amount) }}</span>
        </div>
        <dl class="totals">
          <template v-if="Number(order.discount) > 0">
            <dt>商品金额</dt>
            <dd>{{ money(order.items_amount) }}</dd>
            <dt>优惠</dt>
            <dd>-{{ money(order.discount) }}</dd>
          </template>
          <dt>合计</dt>
          <dd class="total" data-testid="tracking-total">{{ money(order.total) }}</dd>
        </dl>
      </section>

      <section class="card">
        <h4>收款</h4>
        <dl>
          <dt>收款方式</dt>
          <dd>{{ order.payment_method ?? '客服确认后告知' }}</dd>
          <dt>收款状态</dt>
          <dd data-testid="tracking-payment">{{ order.payment_status }}</dd>
          <template v-if="Number(order.paid_amount) > 0">
            <dt>已付</dt>
            <dd>{{ money(order.paid_amount) }}</dd>
          </template>
          <template v-if="Number(order.outstanding) > 0 && order.status !== 'cancelled'">
            <dt>待付</dt>
            <dd>{{ money(order.outstanding) }}</dd>
          </template>
        </dl>
      </section>

      <section v-if="order.shipping_company || Object.keys(order.receiver).length" class="card">
        <h4>配送</h4>
        <dl>
          <template v-if="order.shipping_company">
            <dt>物流</dt>
            <dd data-testid="tracking-shipping">{{ order.shipping_company }} {{ order.tracking_no }}</dd>
          </template>
          <template v-if="order.receiver.name">
            <dt>收货人</dt>
            <dd>{{ order.receiver.name }} {{ order.receiver.phone ?? '' }}</dd>
          </template>
          <template v-if="order.receiver.address">
            <dt>收货地址</dt>
            <dd>{{ order.receiver.address }}</dd>
          </template>
          <template v-if="order.customer_note">
            <dt>您的要求</dt>
            <dd>{{ order.customer_note }}</dd>
          </template>
        </dl>
      </section>

      <section v-if="order.events.length" class="card">
        <h4>动态</h4>
        <ol class="events">
          <li v-for="(event, i) in [...order.events].reverse()" :key="i" data-testid="tracking-event">
            <span class="time">{{ shortTime(event.created_at) }}</span>
            <span>{{ event.text }}</span>
          </li>
        </ol>
      </section>

      <a v-if="order.contact_url" :href="order.contact_url" class="contact" data-testid="tracking-contact"
        >联系客服</a
      >
    </div>
  </div>
</template>

<style scoped>

.body {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
}

.card {
  background: #fff;
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}

.head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.no {
  font-weight: 600;
}

.status {
  color: var(--primary);
  font-weight: 600;
}

.steps {
  display: flex;
  list-style: none;
  padding: 0;
  margin: 12px 0 0;
}

.steps li {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  font-size: 12px;
  color: var(--muted);
  position: relative;
}

.steps li::before {
  content: '';
  position: absolute;
  top: 5px;
  left: -50%;
  width: 100%;
  height: 2px;
  background: var(--border);
}

.steps li:first-child::before {
  display: none;
}

.steps li.done {
  color: #111827;
}

.steps li.done::before {
  background: var(--primary);
}

.dot {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--border);
  z-index: 1;
}

.done .dot {
  background: var(--primary);
}

.label {
  margin-top: 4px;
}

.time {
  color: var(--muted);
  font-size: 11px;
}

h4 {
  margin: 0 0 8px;
  font-size: 14px;
}

.item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
  border-bottom: 1px solid #f3f4f6;
}

.thumb {
  width: 44px;
  height: 44px;
  object-fit: cover;
  border-radius: 4px;
}

.item-text {
  flex: 1;
  font-size: 13px;
}

.amount {
  font-size: 13px;
}

dl {
  display: grid;
  grid-template-columns: 72px 1fr;
  gap: 4px 8px;
  margin: 8px 0 0;
  font-size: 13px;
}

dt {
  color: var(--muted);
}

dd {
  margin: 0;
  word-break: break-all;
}

.totals dd {
  text-align: right;
}

.total {
  font-weight: 600;
}

.muted {
  color: var(--muted);
  font-size: 12px;
}

.events {
  list-style: none;
  padding: 0;
  margin: 0;
  font-size: 13px;
}

.events li {
  display: flex;
  gap: 8px;
  padding: 4px 0;
}

.contact {
  display: block;
  text-align: center;
  padding: 10px;
  border-radius: 8px;
  background: var(--primary);
  color: #fff;
  text-decoration: none;
}

.empty,
.error {
  padding: 24px 16px;
  text-align: center;
}
</style>
