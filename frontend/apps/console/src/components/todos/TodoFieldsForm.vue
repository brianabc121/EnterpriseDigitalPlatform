<script setup lang="ts">
import type { FieldSpec } from '../../todos'

/**
 * 按待办类型的字段定义生成表单。敏感字段已经保存过时显示"已填写"，留空表示不修改。
 */
const props = defineProps<{
  specs: readonly FieldSpec[]
  saved?: ReadonlySet<string>
  disabled?: boolean
}>()
const values = defineModel<Record<string, string>>({ required: true })

function placeholder(spec: FieldSpec): string {
  if (spec.sensitive && props.saved?.has(spec.key)) return '已填写（保密），留空表示不修改'
  switch (spec.type) {
    case 'phone':
      return '手机号'
    case 'email':
      return '邮箱地址'
    case 'number':
      return '数字'
    case 'file':
      return '文件链接'
    default:
      return ''
  }
}
</script>

<template>
  <div class="fields" data-testid="todo-fields">
    <el-form-item
      v-for="spec in specs"
      :key="spec.key"
      :label="spec.label"
      :required="spec.required && !(spec.sensitive && saved?.has(spec.key))"
    >
      <el-select
        v-if="spec.type === 'option'"
        v-model="values[spec.key]"
        clearable
        :disabled="disabled"
        :data-testid="`field-${spec.key}`"
      >
        <el-option v-for="option in spec.options ?? []" :key="option" :label="option" :value="option" />
      </el-select>
      <el-date-picker
        v-else-if="spec.type === 'date'"
        v-model="values[spec.key]"
        type="date"
        value-format="YYYY-MM-DD"
        :disabled="disabled"
        :data-testid="`field-${spec.key}`"
      />
      <el-input
        v-else
        v-model="values[spec.key]"
        :type="spec.type === 'address' ? 'textarea' : 'text'"
        :rows="2"
        :maxlength="spec.type === 'file' ? 1000 : 500"
        :placeholder="placeholder(spec)"
        :disabled="disabled"
        :data-testid="`field-${spec.key}`"
      >
        <template v-if="spec.sensitive" #suffix>
          <el-tooltip content="敏感信息，加密保存，默认掩码显示" placement="top">
            <span class="lock">🔒</span>
          </el-tooltip>
        </template>
      </el-input>
    </el-form-item>
  </div>
</template>

<style scoped>
.lock {
  font-size: 12px;
}
</style>
