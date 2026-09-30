<script setup lang="ts">
import { computed } from 'vue'

import { WEEKDAYS, type BusinessHours } from './hours'

const model = defineModel<BusinessHours | null>({ required: true })

const allDay = computed({
  get: () => model.value === null,
  set: (value: boolean) => {
    model.value = value
      ? null
      : {
          tz: 'Asia/Shanghai',
          days: Object.fromEntries(['1', '2', '3', '4', '5'].map((d) => [d, [['09:00', '18:00']]])),
        }
  },
})

function ranges(day: string): string[][] {
  return model.value?.days[day] ?? []
}

function setRanges(day: string, value: string[][]): void {
  if (!model.value) return
  const days = { ...model.value.days }
  if (value.length) days[day] = value
  else delete days[day]
  model.value = { ...model.value, days }
}

function toggle(day: string, on: boolean): void {
  setRanges(day, on ? [['09:00', '18:00']] : [])
}

function update(day: string, index: number, position: 0 | 1, time: string): void {
  setRanges(
    day,
    ranges(day).map((range, i) => {
      if (i !== index) return range
      const next = [...range]
      next[position] = time
      return next
    }),
  )
}

function add(day: string): void {
  setRanges(day, [...ranges(day), ['13:00', '18:00']])
}

function remove(day: string, index: number): void {
  setRanges(
    day,
    ranges(day).filter((_, i) => i !== index),
  )
}
</script>

<template>
  <div class="hours" data-testid="business-hours">
    <el-radio-group v-model="allDay">
      <el-radio :value="true">全天服务</el-radio>
      <el-radio :value="false">按工作时间</el-radio>
    </el-radio-group>
    <template v-if="model">
      <div v-for="[day, name] in WEEKDAYS" :key="day" class="day">
        <el-checkbox
          :model-value="ranges(day).length > 0"
          :data-testid="`weekday-${day}`"
          @update:model-value="(on: boolean | string | number) => toggle(day, Boolean(on))"
        >
          {{ name }}
        </el-checkbox>
        <div class="ranges">
          <span v-if="ranges(day).length === 0" class="rest">休息</span>
          <div v-for="(range, i) in ranges(day)" :key="i" class="range">
            <el-time-select
              :model-value="range[0]"
              start="00:00"
              step="00:30"
              end="23:30"
              size="small"
              :clearable="false"
              @update:model-value="(t: string) => update(day, i, 0, t)"
            />
            <span>至</span>
            <el-time-select
              :model-value="range[1]"
              start="00:30"
              step="00:30"
              end="24:00"
              size="small"
              :clearable="false"
              @update:model-value="(t: string) => update(day, i, 1, t)"
            />
            <el-button link size="small" @click="remove(day, i)">删除</el-button>
          </div>
          <el-button
            v-if="ranges(day).length > 0"
            link
            type="primary"
            size="small"
            @click="add(day)"
          >
            增加时段
          </el-button>
        </div>
      </div>
      <p class="tip">时区：{{ model.tz }}。非工作时间来访的客户会看到提示并转为留言。</p>
    </template>
  </div>
</template>

<style scoped>
.hours {
  width: 100%;
}

.day {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 4px 0;
}

.day .el-checkbox {
  width: 64px;
}

.ranges {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.range {
  display: flex;
  align-items: center;
  gap: 6px;
}

.range .el-select {
  width: 100px;
}

.rest,
.tip {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.rest {
  line-height: 32px;
}

.tip {
  margin: 6px 0 0;
}
</style>
