<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api } from '../../api'
import {
  AI_MODES,
  AMOUNT_VISIBILITIES,
  ASSIGNMENTS,
  AUTO_ADVANCE_RULES,
  INTENT_STAGES,
  type AutoAdvance,
  type LostReason,
  type OpportunitySettings,
  type Stage,
} from '../../opportunities'
import { useAuthStore } from '../../stores/auth'

/**
 * 商机设置（设计文档 §40.10，`opportunity:assign`）：阶段（名称、顺序、概率、停滞天数；赢单、输单不能删，
 * 有商机的阶段并到别的阶段后再删）、AI 转入、自动推进、新线索分配、输单原因分类、预计金额对谁可见。
 * 阶段的改动立刻生效；其余的点"保存"。
 */
type Rule = { on: boolean; code: string }
type RuleKey = keyof AutoAdvance

const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ saved: [settings: OpportunitySettings]; stagesChanged: [stages: Stage[]] }>()

const auth = useAuthStore()
const tab = ref('stages')
const stages = ref<Stage[]>([])
const form = reactive({
  ai_mode: 'auto' as OpportunitySettings['ai_mode'],
  min_stage: 2,
  follow_days: 3,
  assignment: 'owner' as OpportunitySettings['assignment'],
  assignment_group_id: '' as string,
  lost_reasons: [] as LostReason[],
  amount_visibility: 'all' as OpportunitySettings['amount_visibility'],
})
const rules = reactive<Record<RuleKey, Rule>>({
  first_followup: { on: true, code: 'contacted' },
  quote: { on: true, code: 'quoted' },
  contract_final: { on: true, code: 'negotiating' },
})
const groups = ref<Schemas['SkillGroupOut'][]>([])
const newStage = ref('')
const adding = ref(false)
const saving = ref(false)
const deleting = ref<{ stage: Stage; into: string } | null>(null)

const openStages = computed(() => stages.value.filter((s) => s.kind === 'open'))
const canPickGroup = computed(() => auth.can('routing:manage'))

async function load(): Promise<void> {
  const [settings, groupList] = await Promise.all([
    api.GET('/api/v1/opportunities/settings'),
    canPickGroup.value ? api.GET('/api/v1/skill-groups') : Promise.resolve(null),
  ])
  if (!settings.data) {
    ElMessage.error(errorMessage(settings.error))
    return
  }
  const s = settings.data.settings
  stages.value = settings.data.stages
  Object.assign(form, {
    ai_mode: s.ai_mode ?? 'auto',
    min_stage: s.min_stage ?? 2,
    follow_days: s.follow_days ?? 3,
    assignment: s.assignment ?? 'owner',
    assignment_group_id: s.assignment_group_id ?? '',
    lost_reasons: (s.lost_reasons ?? []).map((r) => ({ ...r })),
    amount_visibility: s.amount_visibility ?? 'all',
  })
  for (const [key, , fallback] of AUTO_ADVANCE_RULES) {
    const code = s.auto_advance?.[key] ?? null
    rules[key] = { on: code !== null, code: code ?? fallback }
  }
  groups.value = groupList?.data?.items ?? []
}

async function save(): Promise<void> {
  if (form.assignment === 'round_robin' && !form.assignment_group_id) {
    ElMessage.warning('轮流分配要选一个技能组')
    return
  }
  if (form.lost_reasons.some((r) => !r.name.trim())) {
    ElMessage.warning('输单原因的名称不能为空')
    return
  }
  saving.value = true
  const { data, error } = await api.PUT('/api/v1/opportunities/settings', {
    body: {
      ai_mode: form.ai_mode,
      min_stage: form.min_stage,
      follow_days: form.follow_days,
      auto_advance: {
        first_followup: rules.first_followup.on ? rules.first_followup.code : null,
        quote: rules.quote.on ? rules.quote.code : null,
        contract_final: rules.contract_final.on ? rules.contract_final.code : null,
      },
      assignment: form.assignment,
      assignment_group_id: form.assignment_group_id || null,
      lost_reasons: form.lost_reasons.map((r) => ({ code: r.code, name: r.name.trim() })),
      amount_visibility: form.amount_visibility,
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已保存商机设置')
  open.value = false
  emit('saved', data.settings)
}

// ---- 阶段：改动立刻生效 ----

async function reloadStages(): Promise<void> {
  const { data } = await api.GET('/api/v1/opportunities/stages')
  if (data) {
    stages.value = data
    emit('stagesChanged', data)
  }
}

async function patch(
  stage: Stage,
  body: Partial<Schemas['StageUpdate']>,
): Promise<void> {
  const { data, error } = await api.PATCH('/api/v1/opportunities/stages/{stage_id}', {
    params: { path: { stage_id: stage.id } },
    body: { clear_stale_days: false, ...body },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    await reloadStages()
    return
  }
  Object.assign(stage, data)
  emit('stagesChanged', stages.value)
}

function rename(stage: Stage, name: string): void {
  if (!name.trim()) {
    void reloadStages()
    return
  }
  void patch(stage, { name: name.trim() })
}

function setProbability(stage: Stage, value: number | undefined): void {
  if (value === undefined) {
    void reloadStages()
    return
  }
  void patch(stage, { probability: value })
}

function setStale(stage: Stage, value: number | undefined | null): void {
  void patch(stage, value === undefined || value === null ? { clear_stale_days: true } : { stale_days: value })
}

async function move(stage: Stage, direction: -1 | 1): Promise<void> {
  const ids = openStages.value.map((s) => s.id)
  const index = ids.indexOf(stage.id)
  const target = index + direction
  if (index === -1 || target < 0 || target >= ids.length) return
  ;[ids[index], ids[target]] = [ids[target] as string, ids[index] as string]
  const { data, error } = await api.PUT('/api/v1/opportunities/stages/order', { body: { ids } })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  stages.value = data
  emit('stagesChanged', data)
}

async function addStage(): Promise<void> {
  const name = newStage.value.trim()
  if (!name) {
    ElMessage.warning('请填写阶段的名称')
    return
  }
  adding.value = true
  const last = openStages.value[openStages.value.length - 1]
  const { data, error } = await api.POST('/api/v1/opportunities/stages', {
    body: { name, probability: 50, stale_days: 7, after_id: last?.id ?? null },
  })
  adding.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  newStage.value = ''
  ElMessage.success(`已添加阶段「${data.name}」`)
  await reloadStages()
}

function askDelete(stage: Stage): void {
  const other = openStages.value.find((s) => s.id !== stage.id)
  deleting.value = { stage, into: other?.id ?? '' }
}

async function confirmDelete(): Promise<void> {
  const current = deleting.value
  if (!current) return
  const { error, response } = await api.DELETE('/api/v1/opportunities/stages/{stage_id}', {
    params: { path: { stage_id: current.stage.id }, query: { merge_into: current.into || undefined } },
  })
  if (!response.ok) {
    ElMessage.error(errorMessage(error))
    return
  }
  deleting.value = null
  ElMessage.success(`已删除阶段「${current.stage.name}」`)
  await reloadStages()
}

// ---- 输单原因 ----

function addReason(): void {
  if (form.lost_reasons.length >= 10) {
    ElMessage.warning('最多 10 个输单原因')
    return
  }
  let n = form.lost_reasons.length + 1
  while (form.lost_reasons.some((r) => r.code === `custom_${n}`)) n += 1
  form.lost_reasons.push({ code: `custom_${n}`, name: '' })
}

watch(open, (value) => {
  if (value) void load()
})
</script>

<template>
  <el-dialog v-model="open" title="商机设置" width="680px" append-to-body data-testid="opp-settings">
    <el-tabs v-model="tab">
      <el-tab-pane label="阶段" name="stages">
        <p class="tip">
          进行中的阶段可以改名、排序、增删，改动立刻生效；赢单、输单各一个，不能删。停滞天数：在一个阶段超过这么
          多天没有动态就标"停滞"，AI 唤醒会提醒。
        </p>
        <table class="stages" data-testid="opp-stages-table">
          <thead>
            <tr>
              <th>阶段</th>
              <th>成交概率</th>
              <th>停滞天数</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(stage, i) in stages" :key="stage.id" :data-testid="`opp-stage-row-${stage.code}`">
              <td>
                <span class="dot" :style="{ background: stage.color ?? 'var(--el-border-color)' }" />
                <el-input
                  v-model="stage.name"
                  size="small"
                  maxlength="32"
                  class="name"
                  :data-testid="`opp-stage-name-${stage.code}`"
                  @change="(v: string) => rename(stage, v)"
                />
              </td>
              <td>
                <el-input-number
                  v-model="stage.probability"
                  size="small"
                  :min="0"
                  :max="100"
                  :controls="false"
                  :disabled="stage.kind !== 'open'"
                  class="num"
                  :data-testid="`opp-stage-probability-${stage.code}`"
                  @change="(v: number | undefined) => setProbability(stage, v)"
                />
                <span class="unit">%</span>
              </td>
              <td>
                <template v-if="stage.kind === 'open'">
                  <el-input-number
                    v-model="stage.stale_days"
                    size="small"
                    :min="1"
                    :max="365"
                    :controls="false"
                    placeholder="不算"
                    class="num"
                    :data-testid="`opp-stage-stale-${stage.code}`"
                    @change="(v: number | undefined | null) => setStale(stage, v)"
                  />
                  <span class="unit">天</span>
                </template>
                <span v-else class="muted">—</span>
              </td>
              <td class="ops">
                <template v-if="stage.kind === 'open'">
                  <el-button link size="small" :disabled="i === 0" @click="move(stage, -1)">上移</el-button>
                  <el-button link size="small" :disabled="i >= openStages.length - 1" @click="move(stage, 1)">
                    下移
                  </el-button>
                  <el-button
                    link
                    type="danger"
                    size="small"
                    :disabled="openStages.length <= 1"
                    :data-testid="`opp-stage-delete-${stage.code}`"
                    @click="askDelete(stage)"
                  >
                    删除
                  </el-button>
                </template>
                <span v-else class="muted">{{ stage.kind === 'won' ? '赢单' : '输单' }}</span>
              </td>
            </tr>
          </tbody>
        </table>
        <div class="add">
          <el-input
            v-model="newStage"
            size="small"
            maxlength="32"
            placeholder="新阶段的名称，加在最后一个进行中的阶段之后"
            class="add-input"
            data-testid="opp-stage-new"
            @keyup.enter="addStage"
          />
          <el-button size="small" :loading="adding" data-testid="opp-stage-add" @click="addStage">
            添加阶段
          </el-button>
        </div>
      </el-tab-pane>

      <el-tab-pane label="AI 转入" name="ai">
        <el-form label-width="120px">
          <el-form-item label="AI 转入">
            <el-radio-group v-model="form.ai_mode" class="modes" data-testid="opp-ai-mode">
              <el-radio v-for="[value, label, hint] in AI_MODES" :key="value" :value="value">
                {{ label }}<span class="hint">{{ hint }}</span>
              </el-radio>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="最低意向">
            <el-select v-model="form.min_stage" :disabled="form.ai_mode === 'off'" data-testid="opp-min-stage">
              <el-option v-for="[value, label] in INTENT_STAGES" :key="value" :label="label" :value="value" />
            </el-select>
            <div class="tip">会话结束后，意图判断的最高意向达到这一级、之后没有下单的客户由 AI 转入新线索</div>
          </el-form-item>
          <el-form-item label="默认跟进">
            <el-input-number v-model="form.follow_days" :min="1" :max="60" data-testid="opp-follow-days" />
            <span class="unit">天后</span>
          </el-form-item>
        </el-form>
      </el-tab-pane>

      <el-tab-pane label="推进与分配" name="flow">
        <el-form label-width="120px">
          <el-form-item label="自动推进">
            <div class="rules">
              <div v-for="[key, label] in AUTO_ADVANCE_RULES" :key="key" class="rule">
                <el-switch v-model="rules[key].on" :data-testid="`opp-auto-${key}`" />
                <span class="rule-label">{{ label }}</span>
                <span class="arrow">→</span>
                <el-select
                  v-model="rules[key].code"
                  size="small"
                  :disabled="!rules[key].on"
                  class="rule-stage"
                  :data-testid="`opp-auto-stage-${key}`"
                >
                  <el-option v-for="s in openStages" :key="s.code" :label="s.name" :value="s.code" />
                </el-select>
              </div>
              <div class="rule muted">订单确认、合同签署 → 赢单（总是开着）</div>
              <div class="tip">只往前不往后：员工手动改过阶段后，自动推进只在目标阶段更靠后时生效</div>
            </div>
          </el-form-item>
          <el-form-item label="新线索分给谁">
            <el-radio-group v-model="form.assignment" class="modes" data-testid="opp-assignment">
              <el-radio v-for="[value, label, hint] in ASSIGNMENTS" :key="value" :value="value">
                {{ label }}<span class="hint">{{ hint }}</span>
              </el-radio>
            </el-radio-group>
          </el-form-item>
          <el-form-item v-if="form.assignment === 'round_robin'" label="技能组">
            <el-select
              v-if="canPickGroup"
              v-model="form.assignment_group_id"
              placeholder="选一个技能组"
              data-testid="opp-assignment-group"
            >
              <el-option v-for="g in groups" :key="g.id" :label="`${g.name}（${g.members.length} 人）`" :value="g.id" />
            </el-select>
            <span v-else class="muted">需要路由管理的权限才能选技能组</span>
          </el-form-item>
        </el-form>
      </el-tab-pane>

      <el-tab-pane label="输单原因与金额" name="lost">
        <el-form label-width="120px">
          <el-form-item label="输单原因">
            <div class="reasons" data-testid="opp-lost-reasons">
              <div v-for="(reason, i) in form.lost_reasons" :key="reason.code" class="reason">
                <el-input v-model="reason.name" size="small" maxlength="16" class="reason-name" />
                <el-button
                  link
                  type="danger"
                  size="small"
                  :disabled="form.lost_reasons.length <= 1"
                  @click="form.lost_reasons.splice(i, 1)"
                >
                  去掉
                </el-button>
              </div>
              <el-button link type="primary" size="small" data-testid="opp-lost-reason-add" @click="addReason">
                添加一个原因
              </el-button>
              <div class="tip">输单时必须选一个原因，报表按原因统计；最多 10 个</div>
            </div>
          </el-form-item>
          <el-form-item label="预计金额可见">
            <el-radio-group v-model="form.amount_visibility" class="modes" data-testid="opp-amount-visibility">
              <el-radio v-for="[value, label] in AMOUNT_VISIBILITIES" :key="value" :value="value">
                {{ label }}
              </el-radio>
            </el-radio-group>
          </el-form-item>
        </el-form>
      </el-tab-pane>
    </el-tabs>
    <template #footer>
      <el-button @click="open = false">关闭</el-button>
      <el-button type="primary" :loading="saving" data-testid="opp-settings-save" @click="save">保存</el-button>
    </template>

    <el-dialog
      :model-value="!!deleting"
      title="删除阶段"
      width="420px"
      append-to-body
      data-testid="opp-stage-delete-dialog"
      @close="deleting = null"
    >
      <template v-if="deleting">
        <p>删除阶段「{{ deleting.stage.name }}」。这个阶段里的商机并到：</p>
        <el-select v-model="deleting.into" data-testid="opp-stage-merge-into">
          <el-option
            v-for="s in openStages.filter((s) => s.id !== deleting?.stage.id)"
            :key="s.id"
            :label="s.name"
            :value="s.id"
          />
        </el-select>
      </template>
      <template #footer>
        <el-button @click="deleting = null">取消</el-button>
        <el-button type="danger" data-testid="opp-stage-delete-confirm" @click="confirmDelete">删除</el-button>
      </template>
    </el-dialog>
  </el-dialog>
</template>

<style scoped>
.tip {
  width: 100%;
  margin: 0 0 10px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.stages {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.stages th {
  padding: 4px 6px;
  font-weight: normal;
  color: var(--el-text-color-secondary);
  text-align: left;
}

.stages td {
  padding: 4px 6px;
  vertical-align: middle;
}

.dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  margin-right: 6px;
  border-radius: 50%;
}

.name {
  width: calc(100% - 16px);
}

.num {
  width: 72px;
}

.unit {
  margin-left: 6px;
  color: var(--el-text-color-secondary);
}

.ops {
  white-space: nowrap;
}

.add {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}

.add-input {
  flex: 1;
}

.modes {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
}

.hint {
  margin-left: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.rules {
  width: 100%;
}

.rule {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  font-size: 13px;
}

.rule-label {
  min-width: 200px;
}

.arrow {
  color: var(--el-text-color-secondary);
}

.rule-stage {
  width: 140px;
}

.reasons {
  width: 100%;
}

.reason {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.reason-name {
  width: 200px;
}

.muted {
  color: var(--el-text-color-secondary);
}
</style>
