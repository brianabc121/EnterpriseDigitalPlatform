<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { api } from '../../api'
import { splitKeywords } from '../../workbench/routing'
import BusinessHoursEditor from './BusinessHoursEditor.vue'
import { asBusinessHours, summarize, type BusinessHours } from './hours'

type Policy = Schemas['RoutingPolicyOut']
/** 编辑中的意图路由：关键词用逗号或空格分隔。 */
type RouteRow = { intent: string; keywords: string; skill_group_id: string | null }

const MODES: Record<string, string> = { human_first: '人工优先', ai_first: 'AI 优先' }

const policies = ref<Policy[]>([])
const groups = ref<Schemas['SkillGroupOut'][]>([])
const channels = ref<Schemas['ChannelOut'][]>([])
const loading = ref(false)
const dialogOpen = ref(false)
const saving = ref(false)
const editing = ref<Policy | null>(null)
const form = reactive({
  name: '',
  mode: 'human_first' as Schemas['RoutingMode'],
  default_skill_group_id: null as string | null,
  owner_first: true,
  max_wait_minutes: 5,
  idle_close_minutes: 30,
  resume_window_minutes: 10,
  business_hours: null as BusinessHours | null,
  priority_tags: ['VIP'] as string[],
  urgent_first: true,
  ai_while_queued: false,
  intent_routes: [] as RouteRow[],
})

const groupName = computed(() => new Map(groups.value.map((g) => [g.id, g.name])))
const channelName = computed(() => new Map(channels.value.map((c) => [c.id, c.name])))

async function load(): Promise<void> {
  loading.value = true
  const [p, g, c] = await Promise.all([
    api.GET('/api/v1/routing-policies'),
    api.GET('/api/v1/skill-groups'),
    api.GET('/api/v1/channels'),
  ])
  loading.value = false
  if (!p.data) {
    ElMessage.error(errorMessage(p.error))
    return
  }
  policies.value = p.data.items
  groups.value = g.data?.items ?? []
  channels.value = c.data?.items ?? []
}

function openDialog(policy: Policy | null): void {
  editing.value = policy
  Object.assign(form, {
    name: policy?.name ?? '',
    mode: policy?.mode ?? 'human_first',
    default_skill_group_id: policy?.default_skill_group_id ?? null,
    owner_first: policy?.owner_first ?? true,
    max_wait_minutes: Math.round((policy?.max_wait_seconds ?? 300) / 60),
    idle_close_minutes: policy?.idle_close_minutes ?? 30,
    resume_window_minutes: policy?.resume_window_minutes ?? 10,
    business_hours: asBusinessHours(policy?.business_hours),
    priority_tags: [...(policy?.priority_tags ?? ['VIP'])],
    urgent_first: policy?.urgent_first ?? true,
    ai_while_queued: policy?.ai_while_queued ?? false,
    intent_routes: (policy?.intent_routes ?? []).map((r) => ({
      intent: r.intent,
      keywords: (r.keywords ?? []).join('，'),
      skill_group_id: r.skill_group_id,
    })),
  })
  dialogOpen.value = true
}

function addRoute(): void {
  form.intent_routes.push({ intent: '', keywords: '', skill_group_id: null })
}

async function save(): Promise<void> {
  const routes = form.intent_routes.filter((r) => r.intent.trim() || r.skill_group_id)
  if (routes.some((r) => !r.intent.trim() || !r.skill_group_id)) {
    ElMessage.warning('每条意图分配都要填写意图名称并选择技能组')
    return
  }
  const body = {
    name: form.name.trim(),
    mode: form.mode,
    default_skill_group_id: form.default_skill_group_id,
    owner_first: form.owner_first,
    max_wait_seconds: form.max_wait_minutes * 60,
    idle_close_minutes: form.idle_close_minutes,
    resume_window_minutes: form.resume_window_minutes,
    business_hours: form.business_hours,
    priority_tags: form.priority_tags.map((t) => t.trim()).filter(Boolean),
    urgent_first: form.urgent_first,
    ai_while_queued: form.ai_while_queued,
    intent_routes: routes.map((r) => ({
      intent: r.intent.trim(),
      keywords: splitKeywords(r.keywords),
      skill_group_id: r.skill_group_id!,
    })),
  }
  saving.value = true
  const { error } = editing.value
    ? await api.PATCH('/api/v1/routing-policies/{policy_id}', {
        params: { path: { policy_id: editing.value.id } },
        body,
      })
    : await api.POST('/api/v1/routing-policies', { body })
  saving.value = false
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已保存')
  dialogOpen.value = false
  await load()
}

async function makeDefault(policy: Policy): Promise<void> {
  const { error } = await api.PATCH('/api/v1/routing-policies/{policy_id}', {
    params: { path: { policy_id: policy.id } },
    body: { is_default: true },
  })
  if (error) ElMessage.error(errorMessage(error))
  else ElMessage.success(`已将"${policy.name}"设为默认策略`)
  await load()
}

async function remove(policy: Policy): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `使用这套策略的渠道会改用默认策略。确定删除"${policy.name}"吗？`,
      '删除路由策略',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  const { error } = await api.DELETE('/api/v1/routing-policies/{policy_id}', {
    params: { path: { policy_id: policy.id } },
  })
  if (error) ElMessage.error(errorMessage(error))
  await load()
}

onMounted(load)
</script>

<template>
  <div>
    <div class="toolbar">
      <span class="hint"
        >渠道没有指定策略时使用默认策略。可以在"接入渠道 → 设置"里为渠道选择策略。</span
      >
      <el-button type="primary" data-testid="new-policy" @click="openDialog(null)"
        >新建策略</el-button
      >
    </div>
    <el-table v-loading="loading" :data="policies" data-testid="policies-table">
      <el-table-column label="名称" min-width="140">
        <template #default="{ row }">
          {{ row.name }}
          <el-tag v-if="row.is_default" size="small" type="success" class="tag">默认</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="接待方式" width="100">
        <template #default="{ row }">{{ MODES[row.mode] ?? row.mode }}</template>
      </el-table-column>
      <el-table-column label="分配规则" min-width="200">
        <template #default="{ row }">
          <div>
            技能组：{{
              row.default_skill_group_id ? groupName.get(row.default_skill_group_id) : '不限'
            }}
          </div>
          <div class="muted">
            {{ row.owner_first ? '归属坐席优先' : '不优先归属坐席' }} · 排队
            {{ Math.round(row.max_wait_seconds / 60) }} 分钟转留言 · 空闲
            {{ row.idle_close_minutes }} 分钟结束 ·
            {{ row.resume_window_minutes ? `${row.resume_window_minutes} 分钟内续接` : '不续接' }}
          </div>
          <div class="muted" data-testid="policy-priority">
            优先：{{ row.priority_tags.length ? row.priority_tags.join('、') : '不按标签' }}
            {{ row.urgent_first ? '· 投诉优先' : '' }}
            {{ row.ai_while_queued ? '· 排队时 AI 继续回答' : '' }}
          </div>
          <div v-if="row.intent_routes.length" class="muted" data-testid="policy-intents">
            意图：{{
              row.intent_routes
                .map(
                  (r: Schemas['IntentRoute']) =>
                    `${r.intent} → ${groupName.get(r.skill_group_id) ?? '?'}`,
                )
                .join('；')
            }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="工作时间" min-width="200">
        <template #default="{ row }">{{ summarize(asBusinessHours(row.business_hours)) }}</template>
      </el-table-column>
      <el-table-column label="使用的渠道" min-width="110">
        <template #default="{ row }">
          <span v-if="row.channel_ids.length">
            {{ row.channel_ids.map((id: string) => channelName.get(id) ?? '').join('、') }}
          </span>
          <span v-else class="muted">{{ row.is_default ? '未指定策略的渠道' : '—' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="" width="180">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="openDialog(row)">编辑</el-button>
          <el-button v-if="!row.is_default" link size="small" @click="makeDefault(row)">
            设为默认
          </el-button>
          <el-button v-if="!row.is_default" link type="danger" size="small" @click="remove(row)">
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog
      v-model="dialogOpen"
      :title="editing ? `编辑策略 · ${editing.name}` : '新建路由策略'"
      width="640px"
    >
      <el-form label-width="120px" data-testid="policy-form">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" maxlength="64" data-testid="policy-name" />
        </el-form-item>
        <el-form-item label="接待方式">
          <el-radio-group v-model="form.mode">
            <el-radio value="human_first">人工优先</el-radio>
            <el-radio value="ai_first">AI 优先（启用 AI 接待后生效）</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="分配到技能组">
          <el-select
            v-model="form.default_skill_group_id"
            clearable
            placeholder="不限（全部在线坐席）"
          >
            <el-option v-for="g in groups" :key="g.id" :label="g.name" :value="g.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="归属坐席优先">
          <el-switch v-model="form.owner_first" />
          <span class="field-hint">客户有归属坐席且其在线时，优先分给他</span>
        </el-form-item>
        <el-form-item label="排队超时">
          <el-input-number v-model="form.max_wait_minutes" :min="1" :max="1440" />
          <span class="field-hint">分钟后仍无人接待，转为留言</span>
        </el-form-item>
        <el-form-item label="空闲结束">
          <el-input-number v-model="form.idle_close_minutes" :min="1" :max="1440" />
          <span class="field-hint">分钟没有新消息，自动结束会话</span>
        </el-form-item>
        <el-form-item label="会话续接">
          <el-input-number v-model="form.resume_window_minutes" :min="0" :max="1440" />
          <span class="field-hint">分钟内再来咨询，优先分给上次的坐席（0 表示关闭）</span>
        </el-form-item>
        <el-form-item label="工作时间">
          <BusinessHoursEditor v-model="form.business_hours" />
        </el-form-item>
        <el-divider content-position="left">排队优先级</el-divider>
        <el-form-item label="VIP 标签">
          <el-input-tag
            v-model="form.priority_tags"
            :max="20"
            placeholder="输入客户标签，回车添加"
            data-testid="policy-priority-tags"
          />
          <div class="field-hint block">带这些标签的客户排在最前面</div>
        </el-form-item>
        <el-form-item label="投诉优先">
          <el-switch v-model="form.urgent_first" data-testid="policy-urgent-first" />
          <span class="field-hint">投诉、退款等敏感诉求或情绪激动的客户排在普通客户前面</span>
        </el-form-item>
        <el-form-item label="排队时 AI 回答">
          <el-switch v-model="form.ai_while_queued" data-testid="policy-ai-while-queued" />
          <span class="field-hint">排队期间 AI 继续回答客户的其他问题（需要启用 AI 接待）</span>
        </el-form-item>
        <el-divider content-position="left">按意图分配</el-divider>
        <p class="field-hint section-hint">
          AI 识别出意图，或客户的话里出现关键词时，分配到对应技能组；都不匹配时按上面的技能组分配。
        </p>
        <div
          v-for="(route, i) in form.intent_routes"
          :key="i"
          class="route"
          data-testid="intent-route"
        >
          <el-input
            v-model="route.intent"
            maxlength="32"
            placeholder="意图，如 售后"
            class="route-intent"
            data-testid="intent-name"
          />
          <el-input
            v-model="route.keywords"
            placeholder="关键词，用逗号分隔（可不填）"
            class="route-keywords"
            data-testid="intent-keywords"
          />
          <el-select
            v-model="route.skill_group_id"
            placeholder="技能组"
            class="route-group"
            data-testid="intent-group"
          >
            <el-option v-for="g in groups" :key="g.id" :label="g.name" :value="g.id" />
          </el-select>
          <el-button link type="danger" @click="form.intent_routes.splice(i, 1)">删除</el-button>
        </div>
        <el-button
          size="small"
          :disabled="!groups.length || form.intent_routes.length >= 20"
          data-testid="add-intent-route"
          @click="addRoute"
        >
          添加意图
        </el-button>
        <span v-if="!groups.length" class="field-hint">先在"技能组"里建好技能组</span>
      </el-form>
      <template #footer>
        <el-button @click="dialogOpen = false">取消</el-button>
        <el-button
          type="primary"
          :loading="saving"
          :disabled="!form.name.trim()"
          data-testid="save-policy"
          @click="save"
        >
          保存
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.hint,
.muted,
.field-hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.field-hint {
  margin-left: 10px;
}

.tag {
  margin-left: 6px;
}

.block {
  display: block;
  margin-left: 0;
}

.section-hint {
  margin: 0 0 8px;
}

.route {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}

.route-intent {
  width: 120px;
}

.route-keywords {
  flex: 1;
}

.route-group {
  width: 140px;
}
</style>
