<script setup lang="ts">
import { computed } from 'vue'

import { markdownBlocks, type MarkdownSpan } from './markdown'
import TextWithLinks from './TextWithLinks.vue'

/** 按块显示简单 Markdown（文字资料），不使用 v-html；文字里的 http(s) 链接可以点击。 */
const props = defineProps<{ source: string }>()
const blocks = computed(() => markdownBlocks(props.source))

function key(spans: MarkdownSpan[], index: number): string {
  return `${index}:${spans.map((s) => s.text).join('')}`
}
</script>

<template>
  <div class="markdown" data-testid="markdown-view">
    <template v-for="(block, i) in blocks" :key="i">
      <component :is="`h${block.level + 1}`" v-if="block.kind === 'heading'" class="heading">
        <template v-for="(span, j) in block.spans" :key="key([span], j)">
          <strong v-if="span.bold"><TextWithLinks :text="span.text" /></strong>
          <TextWithLinks v-else :text="span.text" />
        </template>
      </component>
      <p v-else-if="block.kind === 'paragraph'">
        <template v-for="(span, j) in block.spans" :key="key([span], j)">
          <strong v-if="span.bold"><TextWithLinks :text="span.text" /></strong>
          <TextWithLinks v-else :text="span.text" />
        </template>
      </p>
      <blockquote v-else-if="block.kind === 'quote'">
        <template v-for="(span, j) in block.spans" :key="key([span], j)">
          <strong v-if="span.bold"><TextWithLinks :text="span.text" /></strong>
          <TextWithLinks v-else :text="span.text" />
        </template>
      </blockquote>
      <component :is="block.ordered ? 'ol' : 'ul'" v-else-if="block.kind === 'list'">
        <li v-for="(item, j) in block.items" :key="key(item, j)">
          <template v-for="(span, k) in item" :key="key([span], k)">
            <strong v-if="span.bold"><TextWithLinks :text="span.text" /></strong>
            <TextWithLinks v-else :text="span.text" />
          </template>
        </li>
      </component>
      <table v-else-if="block.kind === 'table'">
        <tbody>
          <tr v-for="(row, r) in block.rows" :key="r" :class="{ head: block.header && r === 0 }">
            <td v-for="(cell, c) in row" :key="c">
              <template v-for="(span, k) in cell" :key="key([span], k)">
                <strong v-if="span.bold"><TextWithLinks :text="span.text" /></strong>
                <TextWithLinks v-else :text="span.text" />
              </template>
            </td>
          </tr>
        </tbody>
      </table>
      <hr v-else-if="block.kind === 'rule'" />
    </template>
  </div>
</template>

<style scoped>
.markdown {
  font-size: 14px;
  line-height: 1.75;
  word-break: break-word;
}

.heading {
  margin: 14px 0 6px;
  line-height: 1.4;
}

.markdown > :first-child {
  margin-top: 0;
}

h2.heading {
  font-size: 20px;
}

h3.heading {
  font-size: 17px;
}

h4.heading {
  font-size: 15px;
}

p {
  margin: 0 0 8px;
}

blockquote {
  margin: 0 0 8px;
  padding: 4px 12px;
  color: var(--el-text-color-secondary, #666);
  border-left: 3px solid var(--el-border-color, #ddd);
}

ul,
ol {
  margin: 0 0 8px;
  padding-left: 22px;
}

table {
  width: 100%;
  margin: 0 0 10px;
  border-collapse: collapse;
}

td {
  padding: 6px 8px;
  border: 1px solid var(--el-border-color, #ddd);
}

tr.head td {
  font-weight: 600;
  background: var(--el-fill-color-light, #f5f7fa);
}

hr {
  margin: 12px 0;
  border: 0;
  border-top: 1px solid var(--el-border-color, #ddd);
}
</style>
