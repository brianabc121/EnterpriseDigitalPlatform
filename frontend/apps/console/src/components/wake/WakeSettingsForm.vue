<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { api, formatDateTime } from '../../api'
import { checkOverrides, groupChecks, WEEKDAYS, type WakeCheck, type WakeSettings } from '../../wake'

/**
 * AI 唤醒的设置（§33.8）：开关、每日巡检和知识库整理的时间、简报发给谁、升级天数，以及每个检查项的
 * 开关和数字。检查项旁边显示它读的数据表和最近一次实际检查的时间：数据没变时检查项会跳过（§33.9）。
 */
const emit = defineEmits<{ saved: [] }>()

const loading = ref(false)
const saving = ref(false)
const form = reactive<WakeSettings>({
  enabled: true,
  hourly: true,
  daily_time: '08:30',
  daily_workdays_only: true,
  brief_staff_ids: [],
  escalate_days: 2,
  kb_enabled: true,
  kb_weekday: 1,
  kb_time: '08:00',
  kb_on_policy_change: true,
  kb_hold_conflicts: false,
  checks: {},
})
const checks = ref<WakeCheck[]>([])
const staff = ref<Schemas['StaffOut'][]>([])
const groups = computed(() => groupChecks(checks.value))

async function load(): Promise<void> {
  loading.value = true
  const [settings, people] = await Promise.all([
    api.GET('/api/v1/wake/settings'),
    api.GET('/api/v1/staff'),
  ])
  loading.value = false
  if (!settings.data) {
    ElMessage.error(errorMessage(settings.error))
    return
  }
  Object.assign(form, settings.data.settings)
  checks.value = settings.data.checks
  staff.value = (people.data?.items ?? []).filter((m) => m.status === 'active')
}

async function save(): Promise<void> {
  saving.value = true
  const { data, error } = await api.PUT('/api/v1/wake/settings', {
    body: { ...form, checks: checkOverrides(checks.value) },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  Object.assign(form, data.settings)
  checks.value = data.checks
  ElMessage.success('已保存')
  emit('saved')
}

function tables(check: WakeCheck): string {
  return check.domains.join('、')
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="wake-settings" data-testid="wake-settings">
    <el-form label-width="150px" class="form">
      <el-form-item label="开启 AI 唤醒">
        <el-switch v-model="form.enabled" data-testid="wake-enabled" />
        <span class="hint">关掉后不再巡检、不再整理知识库</span>
      </el-form-item>
      <el-form-item label="每小时检查">
        <el-switch v-model="form.hourly" :disabled="!form.enabled" data-testid="wake-hourly" />
        <span class="hint">工作时间内每小时检查需要及时处理的问题（工作时间取默认路由策略）</span>
      </el-form-item>
      <el-form-item label="每日巡检">
        <el-time-select
          v-model="form.daily_time"
          start="06:00"
          end="22:00"
          step="00:15"
          format="HH:mm"
          :clearable="false"
          class="time"
          :disabled="!form.enabled"
          data-testid="wake-daily-time"
        />
        <el-radio-group v-model="form.daily_workdays_only" :disabled="!form.enabled" class="gap">
          <el-radio :value="true">只在工作日</el-radio>
          <el-radio :value="false">每天</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="简报还发给">
        <el-select
          v-model="form.brief_staff_ids"
          multiple
          filterable
          placeholder="租户管理员都会收到"
          class="wide"
          :disabled="!form.enabled"
          data-testid="wake-brief-staff"
        >
          <el-option v-for="m in staff" :key="m.id" :label="m.display_name" :value="m.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="升级给管理员">
        <span class="inline">问题超过</span>
        <el-input-number
          v-model="form.escalate_days"
          :min="1"
          :max="30"
          size="small"
          :disabled="!form.enabled"
          data-testid="wake-escalate-days"
        />
        <span class="inline">天没处理</span>
      </el-form-item>

      <el-divider content-position="left">知识库整理</el-divider>
      <el-form-item label="定期整理">
        <el-switch v-model="form.kb_enabled" :disabled="!form.enabled" data-testid="wake-kb-enabled" />
        <el-select v-model="form.kb_weekday" size="small" class="weekday gap" :disabled="!form.kb_enabled">
          <el-option v-for="[value, label] in WEEKDAYS" :key="value" :label="`每${label}`" :value="value" />
        </el-select>
        <el-time-select
          v-model="form.kb_time"
          start="06:00"
          end="22:00"
          step="00:30"
          format="HH:mm"
          :clearable="false"
          size="small"
          class="time"
          :disabled="!form.kb_enabled"
        />
        <span class="hint">对照现行的规章制度检查知识：冲突、缺失、重复</span>
      </el-form-item>
      <el-form-item label="制度变化后整理">
        <el-switch v-model="form.kb_on_policy_change" :disabled="!form.enabled" />
        <span class="hint">规章制度新增、修改、下线后 10 分钟自动整理</span>
      </el-form-item>
      <el-form-item label="暂停冲突的知识">
        <el-switch v-model="form.kb_hold_conflicts" data-testid="wake-kb-hold" />
        <span class="hint">和现行制度冲突、还没处理的知识先不用于 AI 回复客户（坐席仍然能看到）</span>
      </el-form-item>
    </el-form>

    <h4 class="checks-title">检查项</h4>
    <p class="hint block">
      每个检查项是一段按口径的查询。检查前先比对增量更新索引：检查项读的数据表都没有新的变化、口径没改、
      也没到时限时直接跳过，不再查询这些表。
    </p>
    <div v-for="[label, items] in groups" :key="label" class="group">
      <div class="group-title">{{ label }}</div>
      <div
        v-for="check in items"
        :key="check.code"
        class="check"
        :class="{ off: !check.enabled || !check.available }"
        :data-testid="`wake-check-${check.code}`"
      >
        <el-switch
          v-model="check.enabled"
          size="small"
          :disabled="!check.available"
          :data-testid="`wake-check-switch-${check.code}`"
        />
        <div class="check-main">
          <div class="check-head">
            <span class="check-title">{{ check.title }}</span>
            <el-tag v-if="check.hourly" size="small" effect="plain">每小时</el-tag>
            <el-tag v-if="!check.available" size="small" type="info">套餐不包含</el-tag>
          </div>
          <div class="check-desc">{{ check.description }}</div>
          <div v-if="check.params.length" class="params">
            <span v-for="param in check.params" :key="param.name" class="param">
              {{ param.label }}
              <el-input-number
                v-model="param.value"
                :min="param.minimum"
                :max="param.maximum"
                size="small"
                controls-position="right"
                :disabled="!check.enabled"
                :data-testid="`wake-param-${check.code}-${param.name}`"
              />
              {{ param.unit }}
            </span>
          </div>
          <div class="check-meta">
            读：{{ tables(check) }}
            <template v-if="check.changed_at"> · 最近变化 {{ formatDateTime(check.changed_at) }}</template>
            <template v-if="check.checked_at"> · 最近检查 {{ formatDateTime(check.checked_at) }}</template>
          </div>
        </div>
      </div>
    </div>

    <div class="footer">
      <el-button type="primary" :loading="saving" data-testid="wake-settings-save" @click="save">
        保存设置
      </el-button>
    </div>
  </div>
</template>

<style scoped>
.form {
  max-width: 760px;
}

.hint {
  margin-left: 10px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.hint.block {
  display: block;
  margin: 0 0 10px;
}

.time {
  width: 110px;
}

.weekday {
  width: 100px;
  margin-right: 8px;
}

.gap {
  margin-left: 12px;
}

.wide {
  width: 360px;
}

.inline {
  margin: 0 8px;
  font-size: 13px;
}

.checks-title {
  margin: 20px 0 4px;
  font-size: 15px;
}

.group {
  margin-bottom: 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-bg-color);
}

.group-title {
  padding: 8px 14px;
  font-size: 13px;
  font-weight: 500;
  background: var(--el-fill-color-lighter);
  border-radius: 8px 8px 0 0;
}

.check {
  display: flex;
  gap: 12px;
  padding: 10px 14px;
  border-top: 1px solid var(--el-border-color-lighter);
}

.check.off .check-title,
.check.off .check-desc {
  color: var(--el-text-color-secondary);
}

.check-main {
  flex: 1;
  min-width: 0;
}

.check-head {
  display: flex;
  align-items: center;
  gap: 6px;
}

.check-title {
  font-weight: 500;
}

.check-desc,
.check-meta {
  margin-top: 2px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.params {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  margin-top: 6px;
  font-size: 13px;
}

.param :deep(.el-input-number) {
  width: 110px;
  margin: 0 4px;
}

.footer {
  margin-top: 16px;
}
</style>
