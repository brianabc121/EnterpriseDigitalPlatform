<script setup lang="ts">
import { computed } from 'vue'

import { contractBlocks, type Span } from '../../contracts'

/**
 * 合同正文的排版预览（与导出的 Word 同一套规则）：填好的填写项高亮，没填的标红。打印时只打印
 * 这一块（打印样式去掉高亮）。
 */
const props = defineProps<{ body: string; values: Record<string, string> }>()
const blocks = computed(() => contractBlocks(props.body, props.values))

function spanClass(span: Span): Record<string, boolean> {
  return {
    bold: span.bold === true,
    'field-value': span.field === 'value',
    'field-missing': span.field === 'missing',
  }
}

function spanText(span: Span): string {
  return span.field === 'missing' ? `＿＿＿（${span.text}）` : span.text
}
</script>

<template>
  <article class="contract-paper" data-testid="contract-preview">
    <template v-for="(block, i) in blocks" :key="i">
      <h1 v-if="block.kind === 'title'">
        <span v-for="(span, j) in block.spans" :key="j" :class="spanClass(span)">{{ spanText(span) }}</span>
      </h1>
      <h3 v-else-if="block.kind === 'heading'" :class="`level-${block.level ?? 1}`">
        <span v-for="(span, j) in block.spans" :key="j" :class="spanClass(span)">{{ spanText(span) }}</span>
      </h3>
      <table v-else-if="block.kind === 'table'">
        <tbody>
          <tr v-for="(row, r) in block.rows" :key="r" :class="{ head: block.header && r === 0 }">
            <td v-for="(cell, c) in row" :key="c">
              <span v-for="(span, j) in cell" :key="j" :class="spanClass(span)">{{ spanText(span) }}</span>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else :class="{ bullet: block.kind === 'bullet' }">
        <template v-if="block.kind === 'bullet'">• </template>
        <span v-for="(span, j) in block.spans" :key="j" :class="spanClass(span)">{{ spanText(span) }}</span>
      </p>
    </template>
  </article>
</template>

<style scoped>
.contract-paper {
  max-width: 760px;
  margin: 0 auto;
  padding: 32px 40px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  font-family: SimSun, 'Songti SC', serif;
  font-size: 14px;
  line-height: 1.9;
  color: var(--el-text-color-primary);
}

h1 {
  margin: 0 0 20px;
  font-size: 22px;
  text-align: center;
  letter-spacing: 2px;
}

h3 {
  margin: 14px 0 6px;
  font-size: 15px;
}

h3.level-2 {
  font-size: 14px;
}

p {
  margin: 0 0 4px;
  text-align: justify;
}

p.bullet {
  padding-left: 1.2em;
  text-indent: -1.2em;
}

table {
  width: 100%;
  margin: 8px 0 12px;
  border-collapse: collapse;
  font-size: 13px;
}

td {
  padding: 4px 8px;
  border: 1px solid #333;
  text-align: center;
}

tr.head td {
  font-weight: 600;
}

.bold {
  font-weight: 600;
}

.field-value {
  padding: 0 2px;
  background: var(--el-color-primary-light-9);
  border-bottom: 1px solid var(--el-color-primary-light-5);
}

.field-missing {
  color: var(--el-color-danger);
  background: var(--el-color-danger-light-9);
}

/* 打印：只打印合同正文，不标颜色。 */
@media print {
  .contract-paper {
    max-width: none;
    padding: 0;
    border: none;
  }

  .field-value,
  .field-missing {
    color: inherit;
    background: none;
    border: none;
  }
}
</style>
