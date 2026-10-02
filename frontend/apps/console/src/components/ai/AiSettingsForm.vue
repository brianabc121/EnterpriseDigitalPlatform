<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { api } from '../../api'

/**
 * AI 接待设置：启用、名称与语气、转人工的阈值和关键词、意图判断（§32）；显示平台配置状态和本月额度。
 */
const settings = ref<Schemas['AiSettingsOut'] | null>(null)
const saving = ref(false)
const form = reactive({
  enabled: false,
  bot_name: '',
  persona: '',
  handoff_threshold: 0.6,
  relevance_threshold: 0.55,
  max_turns: 8,
  handoff_keywords: [] as string[],
  sensitive_keywords: [] as string[],
  extraction_enabled: true,
  auto_merge_similar: false,
  rewrite_enabled: true,
  answer_cache: true,
  tools_enabled: false,
  segment_replies: true,
  intent_enabled: true,
  intent_in_reply: true,
  /** 0 表示不转。 */
  intent_handoff_stage: 0 as 0 | 3 | 4,
  custom_intents: [] as { name: string; description: string }[],
})

/** 平台的意图判断：判断模型，或用大模型判断。 */
const intentSource = computed(() => {
  const s = settings.value
  if (!s || s.intent_source === 'none') return ''
  const model = s.intent_model ? `（${s.intent_model}）` : ''
  return s.intent_source === 'judge' ? `判断模型${model}` : `大模型${model}，按大模型计费`
})

const quotaUsage = computed(() => {
  const s = settings.value
  if (!s || s.monthly_quota === null) return null
  return Math.min(100, Math.round((s.used_this_month / Math.max(1, s.monthly_quota)) * 100))
})

function fill(data: Schemas['AiSettingsOut']): void {
  settings.value = data
  Object.assign(form, {
    enabled: data.enabled,
    bot_name: data.bot_name,
    persona: data.persona ?? '',
    handoff_threshold: data.handoff_threshold,
    relevance_threshold: data.relevance_threshold,
    max_turns: data.max_turns,
    handoff_keywords: [...data.handoff_keywords],
    sensitive_keywords: [...data.sensitive_keywords],
    extraction_enabled: data.extraction_enabled,
    auto_merge_similar: data.auto_merge_similar,
    rewrite_enabled: data.rewrite_enabled,
    answer_cache: data.answer_cache,
    tools_enabled: data.tools_enabled,
    segment_replies: data.segment_replies,
    intent_enabled: data.intent_enabled,
    intent_in_reply: data.intent_in_reply,
    intent_handoff_stage: data.intent_handoff_stage === 3 || data.intent_handoff_stage === 4
      ? data.intent_handoff_stage
      : 0,
    custom_intents: data.custom_intents.map((item) => ({
      name: item.name,
      description: item.description ?? '',
    })),
  })
}

async function load(): Promise<void> {
  const { data, error } = await api.GET('/api/v1/ai/settings')
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  fill(data)
}

async function save(): Promise<void> {
  if (!form.bot_name.trim()) {
    ElMessage.warning('请填写智能客服的名称')
    return
  }
  const customIntents = form.custom_intents
    .map((item) => ({ name: item.name.trim(), description: item.description.trim() }))
    .filter((item) => item.name)
  saving.value = true
  const { data, error } = await api.PUT('/api/v1/ai/settings', {
    body: {
      ...form,
      bot_name: form.bot_name.trim(),
      persona: form.persona.trim() || null,
      intent_handoff_stage: form.intent_handoff_stage || null,
      custom_intents: customIntents,
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  fill(data)
  ElMessage.success('已保存')
}

onMounted(load)
</script>

<template>
  <div v-if="settings" class="settings" data-testid="ai-settings">
    <el-alert
      v-if="!settings.llm_configured"
      type="warning"
      :closable="false"
      show-icon
      title="平台还没有配置大模型，AI 接待和坐席助手暂不可用。请联系平台运营。"
      class="block"
    />
    <el-alert
      v-else-if="!settings.embeddings_configured"
      type="info"
      :closable="false"
      show-icon
      title="平台没有配置向量模型，知识检索只按关键词匹配。"
      class="block"
    />

    <el-form label-width="140px" class="form">
      <el-form-item label="AI 接待">
        <el-switch v-model="form.enabled" data-testid="ai-enabled" />
        <span class="help">
          启用后，路由策略为「AI 优先」的渠道由 AI 先接待，无法解决时自动转人工。
        </span>
      </el-form-item>
      <el-form-item label="名称">
        <el-input v-model="form.bot_name" maxlength="32" class="short" data-testid="ai-bot-name" />
        <span class="help">访客看到的智能客服名称</span>
      </el-form-item>
      <el-form-item label="语气与风格">
        <el-input
          v-model="form.persona"
          type="textarea"
          :rows="2"
          maxlength="500"
          placeholder="例如：称呼客户为“您”，语气亲切，回答简洁。"
        />
      </el-form-item>
      <el-form-item label="转人工灵敏度">
        <el-slider
          v-model="form.handoff_threshold"
          :min="0.2"
          :max="1.2"
          :step="0.1"
          show-stops
          class="slider"
        />
        <span class="help">
          {{
            form.handoff_threshold.toFixed(1)
          }}：知识不足、把握低、情绪负面、重复提问等信号的加权得分达到这个值时转人工，越小越容易转人工。
        </span>
      </el-form-item>
      <el-form-item label="知识相关度阈值">
        <el-slider
          v-model="form.relevance_threshold"
          :min="0.3"
          :max="0.9"
          :step="0.05"
          class="slider"
        />
        <span class="help"
          >{{ form.relevance_threshold.toFixed(2) }}：最相关的知识低于这个值视为知识缺失</span
        >
      </el-form-item>
      <el-form-item label="最多接待轮数">
        <el-input-number v-model="form.max_turns" :min="1" :max="50" />
        <span class="help">超过后计入转人工信号</span>
      </el-form-item>
      <el-form-item label="转人工关键词">
        <el-input-tag
          v-model="form.handoff_keywords"
          :max="50"
          placeholder="客户说这些词时立即转人工，回车添加"
          data-testid="ai-handoff-keywords"
        />
        <span class="help">已内置：转人工、人工客服、找客服等</span>
      </el-form-item>
      <el-form-item label="敏感词">
        <el-input-tag
          v-model="form.sensitive_keywords"
          :max="200"
          placeholder="客户提到时转人工，AI 回复中出现时不发送"
        />
        <span class="help">已内置：投诉、退款、赔偿、律师、12315 等</span>
      </el-form-item>
      <el-divider content-position="left">回答增强</el-divider>
      <el-form-item label="改写问题">
        <el-switch v-model="form.rewrite_enabled" data-testid="ai-rewrite" />
        <span class="help">检索前结合上文补全"它""这个"等指代，一句话问了几件事时拆开分别检索</span>
      </el-form-item>
      <el-form-item label="答案缓存">
        <el-switch v-model="form.answer_cache" data-testid="ai-cache" />
        <span class="help">
          意思相同的问题直接用之前的回答，更快也更省；知识、设置或提示词变化后自动失效
        </span>
      </el-form-item>
      <el-form-item label="工具调用">
        <el-switch
          v-model="form.tools_enabled"
          :disabled="!settings.tools_supported && !form.tools_enabled"
          data-testid="ai-tools"
        />
        <span class="help">
          允许 AI 再次检索知识、查看客户档案（不含联系方式）、登记客户留下的线索（坐席确认后写入档案）、
          主动转人工、登记留言。
          <template v-if="!settings.tools_supported">当前使用的模型不支持工具调用。</template>
        </span>
      </el-form-item>
      <el-form-item label="分段发送">
        <el-switch v-model="form.segment_replies" data-testid="ai-segments" />
        <span class="help">网页渠道里较长的回答分成几条发送，发送前访客会看到"正在输入"</span>
      </el-form-item>
      <el-divider content-position="left">意图判断</el-divider>
      <el-form-item label="判断客户意图">
        <el-switch v-model="form.intent_enabled" data-testid="ai-intent-enabled" />
        <span class="help">
          按客户的消息判断有没有下单意向（5 级）、真实意图、在意什么和情绪，在坐席工作台显示。
          <template v-if="intentSource">当前用{{ intentSource }}。</template>
          <span v-else class="warn" data-testid="ai-intent-unconfigured">
            平台还没有配置判断模型，暂时不能判断，请联系平台运营。
          </span>
        </span>
      </el-form-item>
      <el-form-item label="AI 回复参考">
        <el-switch
          v-model="form.intent_in_reply"
          :disabled="!form.intent_enabled"
          data-testid="ai-intent-reply"
        />
        <span class="help">
          AI 接待回复时按客户的意向和在意的点调整回答重点；客户换种说法要人工时也能认出来并转人工
        </span>
      </el-form-item>
      <el-form-item label="高意向客户转人工">
        <el-select
          v-model="form.intent_handoff_stage"
          :disabled="!form.intent_enabled"
          class="short"
          data-testid="ai-intent-handoff"
        >
          <el-option :value="0" label="不转" />
          <el-option :value="3" label="意向明确时" />
          <el-option :value="4" label="准备下单时" />
        </el-select>
        <span class="help">到了这个阶段就转给人工客服跟进成交，交接摘要里写明意向</span>
      </el-form-item>
      <el-form-item label="自定义意图">
        <div class="customs" data-testid="ai-custom-intents">
          <div v-for="(item, index) in form.custom_intents" :key="index" class="custom">
            <el-input
              v-model="item.name"
              maxlength="16"
              placeholder="名称，如：定制尺寸"
              class="custom-name"
              data-testid="ai-custom-intent-name"
            />
            <el-input
              v-model="item.description"
              maxlength="60"
              placeholder="说明，帮助判断，如：客户想按自己的尺寸定做"
              class="custom-description"
              data-testid="ai-custom-intent-description"
            />
            <el-button link type="danger" @click="form.custom_intents.splice(index, 1)">
              删除
            </el-button>
          </div>
          <el-button
            size="small"
            :disabled="form.custom_intents.length >= 10"
            data-testid="ai-custom-intent-add"
            @click="form.custom_intents.push({ name: '', description: '' })"
          >
            添加意图
          </el-button>
        </div>
        <span class="help">
          内置：了解商品、询价比价、购买下单、库存发货、查订单、改订单、售后、投诉、发票手续、合作代理、
          要人工、闲聊其他；可以再加 10 个本行业的
        </span>
      </el-form-item>
      <el-divider content-position="left">知识沉淀</el-divider>
      <el-form-item label="自动提炼">
        <el-switch v-model="form.extraction_enabled" data-testid="ai-extraction" />
        <span class="help">
          每小时从已结束的会话里提炼问答和没有解答的问题（先脱敏），客户评价满意的会话还会挑选坐席的
          优秀回复作为话术候选，在"知识库 → 审核台"审核
        </span>
      </el-form-item>
      <el-form-item label="自动合并相似问法">
        <el-switch v-model="form.auto_merge_similar" />
        <span class="help">
          同一个问法出现 3 次以上、与已有问答高度相似且答案一致时，直接并入原问答，不经审核
        </span>
      </el-form-item>
      <el-form-item label="本月 AI 回复">
        <div class="quota" data-testid="ai-quota">
          <span>
            {{ settings.used_this_month.toLocaleString('zh-CN') }}
            /
            {{
              settings.monthly_quota === null
                ? '不限'
                : settings.monthly_quota.toLocaleString('zh-CN')
            }}
          </span>
          <el-progress
            v-if="quotaUsage !== null"
            :percentage="quotaUsage"
            :status="quotaUsage >= 100 ? 'exception' : quotaUsage >= 80 ? 'warning' : undefined"
            class="progress"
          />
        </div>
        <span class="help">额度由套餐决定；用完后新会话直接转人工</span>
      </el-form-item>
      <el-form-item>
        <el-button type="primary" :loading="saving" data-testid="ai-save" @click="save"
          >保存</el-button
        >
      </el-form-item>
    </el-form>
  </div>
</template>

<style scoped>
.block {
  margin-bottom: 16px;
}

.form {
  max-width: 820px;
}

.short {
  width: 200px;
}

.slider {
  width: 280px;
  margin-right: 16px;
}

.help {
  margin-left: 12px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.5;
}

.form :deep(.el-form-item__content) {
  flex-wrap: wrap;
  row-gap: 4px;
}

.warn {
  color: var(--el-color-warning);
}

.customs {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
  width: 100%;
}

.custom {
  display: flex;
  gap: 8px;
  width: 100%;
}

.custom-name {
  width: 160px;
}

.custom-description {
  flex: 1;
  min-width: 0;
}

.quota {
  display: flex;
  align-items: center;
  gap: 12px;
  font-variant-numeric: tabular-nums;
}

.progress {
  width: 200px;
}
</style>
