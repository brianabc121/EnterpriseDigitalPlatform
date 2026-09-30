<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import {
  APPROVE_LABEL,
  CANDIDATE_KIND,
  CANDIDATE_KIND_TAG,
  CANDIDATE_STATUS,
  diffText,
} from '../../knowledge'
import { percent } from '../../reports'
import { useAuthStore } from '../../stores/auth'

/** 审核一条候选：编辑后通过、合并到已有知识，或驳回。证据对话在提炼时已脱敏。 */
const props = defineProps<{ candidateId: string | null }>()
const emit = defineEmits<{ close: []; reviewed: [] }>()

const auth = useAuthStore()
const canPublish = computed(() => auth.can('kb:publish'))
const detail = ref<Schemas['KbCandidateDetail'] | null>(null)
const loading = ref(false)
const acting = ref(false)
const form = reactive({ question: '', answer: '', category: '', agentOnly: false })

const open = computed({
  get: () => props.candidateId !== null,
  set: (value) => {
    if (!value) emit('close')
  },
})
const pending = computed(() => detail.value?.status === 'pending')
const kind = computed(() => detail.value?.kind ?? 'new')
const creates = computed(() => kind.value === 'new' || kind.value === 'gap')
const isPhrase = computed(() => kind.value === 'phrase')
const diff = computed(() =>
  kind.value === 'conflict' && detail.value?.target
    ? diffText(detail.value.target.content, form.answer)
    : [],
)

watch(
  () => props.candidateId,
  async (id) => {
    detail.value = null
    if (!id) return
    loading.value = true
    const { data, error } = await api.GET('/api/v1/kb/candidates/{candidate_id}', {
      params: { path: { candidate_id: id } },
    })
    loading.value = false
    if (!data) {
      ElMessage.error(errorMessage(error))
      return
    }
    detail.value = data
    Object.assign(form, {
      question: data.question,
      answer: data.answer ?? '',
      category: data.category,
      agentOnly: false,
    })
  },
)

async function run(request: () => Promise<{ data?: unknown; error?: unknown }>, done: string) {
  acting.value = true
  const { data, error } = await request()
  acting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(done)
  emit('reviewed')
}

async function approve(): Promise<void> {
  if (!detail.value) return
  if ((creates.value || isPhrase.value) && !form.answer.trim()) {
    ElMessage.warning(isPhrase.value ? '请填写话术内容' : '请填写答案')
    return
  }
  const id = detail.value.id
  await run(
    () =>
      api.POST('/api/v1/kb/candidates/{candidate_id}/approve', {
        params: { path: { candidate_id: id } },
        body: {
          question: form.question.trim() || null,
          answer: form.answer.trim() || null,
          category: form.category.trim(),
          visibility: creates.value ? (form.agentOnly ? 'agent' : 'public') : null,
        },
      }),
    kind.value === 'similar' ? '已并入原问答' : isPhrase.value ? '已加入共享话术' : '已发布',
  )
}

async function merge(item: Schemas['KbSearchHit']): Promise<void> {
  if (!detail.value) return
  const id = detail.value.id
  await run(
    () =>
      api.POST('/api/v1/kb/candidates/{candidate_id}/merge', {
        params: { path: { candidate_id: id } },
        body: { item_id: item.item_id },
      }),
    `已合并到「${item.title}」`,
  )
}

async function reject(): Promise<void> {
  if (!detail.value) return
  let reason: string
  try {
    const result = await ElMessageBox.prompt('驳回理由会用于改进提炼，请简单说明。', '驳回候选', {
      confirmButtonText: '驳回',
      cancelButtonText: '取消',
      inputPattern: /\S/,
      inputErrorMessage: '请填写理由',
    })
    reason = (result as { value: string }).value
  } catch {
    return
  }
  const id = detail.value.id
  await run(
    () =>
      api.POST('/api/v1/kb/candidates/{candidate_id}/reject', {
        params: { path: { candidate_id: id } },
        body: { reason },
      }),
    '已驳回',
  )
}
</script>

<template>
  <el-drawer v-model="open" title="审核候选" size="640px">
    <div v-loading="loading" class="body" data-testid="candidate-drawer">
      <template v-if="detail">
        <div class="head">
          <el-tag :type="CANDIDATE_KIND_TAG[detail.kind]">{{ CANDIDATE_KIND[detail.kind] }}</el-tag>
          <el-tag v-if="!pending" type="info">{{ CANDIDATE_STATUS[detail.status] }}</el-tag>
          <span class="muted">
            出现 {{ detail.occurrences }} 次（近 7 天 {{ detail.recent }} 次）· 首次
            {{ formatDateTime(detail.first_seen_at) }}
          </span>
        </div>
        <el-alert
          v-if="detail.time_sensitive"
          type="warning"
          :closable="false"
          show-icon
          title="可能是活动、价格等会过期的信息，发布后记得设置有效期。"
          class="block"
        />

        <el-form label-position="top" :disabled="!pending || !canPublish">
          <el-form-item :label="isPhrase ? '话术标题' : '问题'">
            <el-input
              v-model="form.question"
              :maxlength="isPhrase ? 64 : 500"
              data-testid="candidate-question"
            />
          </el-form-item>
          <div v-if="detail.variants.length > 1" class="variants">
            <span class="muted">客户的其他问法：</span>
            <el-tag v-for="v in detail.variants.slice(1)" :key="v" size="small" type="info">{{
              v
            }}</el-tag>
          </div>
          <el-form-item
            :label="
              kind === 'gap'
                ? '答案（客服当时没有解答，请补充）'
                : isPhrase
                  ? '话术（通过后加入共享快捷话术，所有坐席可用）'
                  : '答案'
            "
          >
            <el-input
              v-model="form.answer"
              type="textarea"
              :rows="4"
              maxlength="50000"
              data-testid="candidate-answer"
            />
          </el-form-item>
          <el-form-item v-if="isPhrase" label="话术分类">
            <el-input v-model="form.category" maxlength="32" data-testid="candidate-category" />
          </el-form-item>
          <div v-if="creates" class="row">
            <el-form-item label="分类" class="grow">
              <el-input v-model="form.category" maxlength="64" />
            </el-form-item>
            <el-form-item label=" " class="grow">
              <el-checkbox v-model="form.agentOnly">仅坐席可见（AI 不用于回答客户）</el-checkbox>
            </el-form-item>
          </div>
        </el-form>

        <template v-if="detail.target">
          <h4>{{ kind === 'conflict' ? '与已有答案的差异' : '已有知识' }}</h4>
          <div class="target">
            <div class="target-title">
              {{ detail.target.title }} <span class="muted">v{{ detail.target.version }}</span>
            </div>
            <p v-if="kind === 'conflict'" class="diff" data-testid="candidate-diff">
              <span v-for="(part, i) in diff" :key="i" :class="part.kind">{{ part.text }}</span>
            </p>
            <p v-else class="text">{{ detail.target.content }}</p>
          </div>
        </template>

        <template v-if="pending && canPublish && detail.similar.length && !isPhrase">
          <h4>相似的已有知识</h4>
          <div v-for="hit in detail.similar" :key="hit.item_id" class="similar">
            <div class="similar-head">
              <span class="similar-title">{{ hit.title }}</span>
              <span class="muted">相关度 {{ percent(hit.score) }}</span>
              <el-button
                link
                type="primary"
                size="small"
                :disabled="acting"
                data-testid="merge-into"
                @click="merge(hit)"
              >
                合并到这条
              </el-button>
            </div>
            <p class="text">{{ hit.text }}</p>
          </div>
        </template>

        <h4>证据对话（已脱敏）</h4>
        <div class="evidence">
          <div v-for="(e, i) in detail.evidence" :key="i" class="dialog" data-testid="evidence">
            <div class="muted">{{ formatDateTime(e.seen_at) }}</div>
            <p v-for="(line, j) in e.lines" :key="j" class="line">
              <span class="role">{{ line.role }}：</span>{{ line.text }}
            </p>
          </div>
        </div>
        <p class="muted trace">
          提炼模型 {{ detail.model ?? '—' }} · 提示词 {{ detail.prompt_version ?? '—' }}
        </p>
        <p v-if="detail.review_note" class="muted">处理说明：{{ detail.review_note }}</p>
      </template>
    </div>
    <template v-if="detail && pending && canPublish" #footer>
      <el-button :disabled="acting" data-testid="reject-candidate" @click="reject">驳回</el-button>
      <el-button type="primary" :loading="acting" data-testid="approve-candidate" @click="approve">
        {{ APPROVE_LABEL[detail.kind] }}
      </el-button>
    </template>
  </el-drawer>
</template>

<style scoped>
.head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.block {
  margin-bottom: 12px;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.variants {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  margin: -8px 0 12px;
}

.row {
  display: flex;
  gap: 12px;
}

.grow {
  flex: 1;
}

h4 {
  margin: 16px 0 8px;
  font-size: 14px;
}

.target,
.similar,
.dialog {
  padding: 8px 12px;
  border-radius: 6px;
  background: var(--el-fill-color-lighter);
  margin-bottom: 8px;
}

.target-title,
.similar-title {
  font-weight: 500;
}

.similar-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.similar-title {
  flex: 1;
}

.text,
.diff,
.line {
  margin: 4px 0 0;
  font-size: 13px;
  white-space: pre-wrap;
  word-break: break-word;
}

.diff .removed {
  background: var(--el-color-danger-light-8);
  text-decoration: line-through;
}

.diff .added {
  background: var(--el-color-success-light-8);
}

.role {
  color: var(--el-text-color-secondary);
}

.trace {
  margin-top: 12px;
}
</style>
