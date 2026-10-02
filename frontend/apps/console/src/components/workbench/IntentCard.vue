<script setup lang="ts">
import { computed, ref } from 'vue'

import { meter, percentText, STAGES, stageChanges, stageTone } from '../../intent'
import { useAuthStore } from '../../stores/auth'
import { useWorkbenchStore } from '../../stores/workbench'

/**
 * 意图判断（设计文档 §32.5）：一行显示下单意向（五格进度条和阶段）、真实意图、客户在意的和情绪；
 * 点"详情"看下单意向的分布、这次会话的变化、可能的意图和依据。意向明确、准备下单时可以直接
 * "生成订单"（右栏的 AI 预填）。只给坐席看。
 */
const props = defineProps<{ sessionId: string }>()
const emit = defineEmits<{ order: [] }>()

const wb = useWorkbenchStore()
const auth = useAuthStore()
const expanded = ref(false)

const intent = computed(() => wb.intents[props.sessionId] ?? null)
const tone = computed(() => stageTone(intent.value?.stage))
const segments = computed(() => (intent.value ? meter(intent.value.stage) : []))
const canOrder = computed(
  () =>
    !!intent.value &&
    intent.value.stage >= 3 &&
    auth.can('order:create') &&
    auth.me?.features?.orders !== false,
)
const concerns = computed(() => intent.value?.concerns.map((c) => c.label).join('、') ?? '')
/** 有些着急、生气激动时才显示情绪。 */
const emotion = computed(() =>
  intent.value && (intent.value.emotion ?? 0) >= 0.5 ? intent.value.emotion_label : null,
)

function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

const changes = computed(() => (intent.value ? stageChanges(intent.value.history, clock) : []))
/** 依据：判断覆盖到的那条客户消息（在当前加载的消息里时显示原文）。 */
const evidence = computed(() => {
  const id = intent.value?.message_id
  const found = id ? wb.activeMessages.find((m) => m.id === id) : undefined
  return found?.text ?? null
})
const source = computed(() => {
  const value = intent.value
  if (!value) return ''
  const kind = value.source === 'llm' ? '大模型判断' : '判断模型'
  return value.model ? `${kind}（${value.model}）` : kind
})
</script>

<template>
  <div v-if="intent" class="intent" :class="tone" data-testid="intent-card">
    <div class="line">
      <span class="label">下单意向</span>
      <span class="meter" aria-hidden="true">
        <i v-for="(on, index) in segments" :key="index" :class="{ on }" />
      </span>
      <strong class="stage" data-testid="intent-stage">{{ intent.stage_label }}</strong>
      <span class="muted">{{ percentText(intent.stage_probability) }}</span>
      <span v-if="intent.intent_label" class="field" data-testid="intent-real">
        <span class="label">真实意图</span>{{ intent.intent_label }}
      </span>
      <span v-if="concerns" class="field" data-testid="intent-concerns">
        <span class="label">在意</span>{{ concerns }}
      </span>
      <span v-if="emotion" class="field mood" data-testid="intent-emotion">{{ emotion }}</span>
      <span class="spacer" />
      <el-button
        v-if="canOrder"
        size="small"
        type="primary"
        link
        data-testid="intent-order"
        @click="emit('order')"
      >
        生成订单
      </el-button>
      <el-button size="small" link data-testid="intent-toggle" @click="expanded = !expanded">
        {{ expanded ? '收起' : '详情' }}
      </el-button>
    </div>

    <div v-if="expanded" class="detail" data-testid="intent-detail">
      <section>
        <div class="title">下单意向的分布</div>
        <div
          v-for="(p, index) in intent.distribution"
          :key="index"
          class="bar-row"
          data-testid="intent-distribution"
        >
          <span class="bar-label" :class="{ current: index === intent.stage }">{{
            STAGES[index]
          }}</span>
          <span class="bar"><span class="fill" :style="{ width: percentText(p) }" /></span>
          <span class="bar-value">{{ percentText(p) }}</span>
        </div>
      </section>
      <section>
        <div class="title">这次会话的变化</div>
        <div class="changes" data-testid="intent-history">
          <template v-for="(change, index) in changes" :key="index">
            <span v-if="index" class="arrow">→</span>
            <span class="change">{{ change.at }} {{ change.label }}</span>
          </template>
        </div>
        <template v-if="intent.intents.length">
          <div class="title">可能的意图</div>
          <div class="chips">
            <span v-for="option in intent.intents" :key="option.code" class="chip">
              {{ option.label }} <small>{{ percentText(option.probability) }}</small>
            </span>
          </div>
        </template>
        <div class="meta">
          <template v-if="intent.human_probability != null">
            要人工的可能 {{ percentText(intent.human_probability) }} ·
          </template>
          {{ source }} · {{ clock(intent.judged_at) }} 判断
          <template v-if="intent.pending"> · 有新消息正在判断</template>
        </div>
        <div v-if="evidence" class="meta evidence">依据：客户说「{{ evidence }}」</div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.intent {
  --tone: var(--el-color-info);
  --tone-light: var(--el-color-info-light-9);
  padding: 6px 16px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  background: var(--tone-light);
  font-size: 13px;
}

.intent.primary {
  --tone: var(--el-color-primary);
  --tone-light: var(--el-color-primary-light-9);
}

.intent.warning {
  --tone: var(--el-color-warning);
  --tone-light: var(--el-color-warning-light-9);
}

.intent.danger {
  --tone: var(--el-color-danger);
  --tone-light: var(--el-color-danger-light-9);
}

.line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 10px;
  min-height: 24px;
}

.label {
  margin-right: 4px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.meter {
  display: inline-flex;
  gap: 2px;
}

.meter i {
  width: 10px;
  height: 8px;
  border-radius: 2px;
  background: var(--el-border-color);
}

.meter i.on {
  background: var(--tone);
}

.stage {
  color: var(--tone);
}

.field {
  white-space: nowrap;
}

.mood {
  color: var(--el-color-danger);
}

.spacer {
  flex: 1;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.detail {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 8px 24px;
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px dashed var(--el-border-color);
}

.title {
  margin: 2px 0 4px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.bar-row {
  display: grid;
  grid-template-columns: 76px 1fr 36px;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  line-height: 18px;
}

.bar-label {
  color: var(--el-text-color-regular);
}

.bar-label.current {
  color: var(--tone);
  font-weight: 600;
}

.bar {
  height: 6px;
  border-radius: 3px;
  background: var(--el-fill-color-dark);
  overflow: hidden;
}

.fill {
  display: block;
  height: 100%;
  background: var(--tone);
}

.bar-value {
  color: var(--el-text-color-secondary);
  text-align: right;
}

.changes,
.chips {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  margin-bottom: 6px;
  font-size: 12px;
}

.arrow {
  color: var(--el-text-color-placeholder);
}

.change,
.chip {
  padding: 0 6px;
  border-radius: 4px;
  background: var(--el-bg-color);
  line-height: 20px;
}

.chip small {
  color: var(--el-text-color-secondary);
}

.meta {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 18px;
}

.evidence {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
