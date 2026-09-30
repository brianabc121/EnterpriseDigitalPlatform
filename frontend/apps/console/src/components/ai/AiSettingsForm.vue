<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { api } from '../../api'

/** AI 接待设置：启用、名称与语气、转人工的阈值和关键词；显示平台配置状态和本月额度。 */
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
  saving.value = true
  const { data, error } = await api.PUT('/api/v1/ai/settings', {
    body: { ...form, bot_name: form.bot_name.trim(), persona: form.persona.trim() || null },
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
