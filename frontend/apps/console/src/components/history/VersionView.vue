<script setup lang="ts">
import { computed } from 'vue'

import { compareDocs, shownColumns, shownFields, type VersionDoc } from '../../history'

/**
 * 一个版本的完整内容（设计文档 §25.14）："显示更改"打开时和对比的版本比较：改过的字段标黄、原来的
 * 值划掉；新增的明细行标绿，删掉的行标红并划掉；改过的单元格标出原来的值。
 */
const props = defineProps<{
  doc: VersionDoc
  /** 对比的版本；为空表示没有可以对比的（最初的版本）。 */
  base: VersionDoc | null
  highlight: boolean
}>()

const changes = computed(() => compareDocs(props.highlight ? props.base : null, props.doc))
const fields = computed(() => shownFields(props.doc, changes.value))
const tables = computed(() =>
  props.doc.tables.map((table) => {
    const diff = changes.value.tables[table.key]
    const removed = diff?.removed ?? []
    return { table, diff, removed, columns: shownColumns(table, removed) }
  }),
)
</script>

<template>
  <div class="version-view" data-testid="history-document">
    <dl class="fields">
      <template v-for="f in fields" :key="f.key">
        <dt>{{ f.label }}</dt>
        <dd
          :class="{ changed: f.key in changes.fields }"
          data-testid="history-field"
          :data-label="f.label"
          :data-changed="f.key in changes.fields ? 'true' : 'false'"
        >
          <del v-if="f.key in changes.fields" class="old">{{ changes.fields[f.key] || '空' }}</del>
          <span class="new">{{ f.value || (f.key in changes.fields ? '空' : '—') }}</span>
        </dd>
      </template>
    </dl>
    <section v-for="{ table, diff, removed, columns } in tables" :key="table.key" class="table">
      <h5>{{ table.label }}</h5>
      <table class="grid" :data-testid="`history-table-${table.key}`">
        <thead>
          <tr>
            <th v-for="c in columns" :key="c.key">{{ c.label }}</th>
            <th class="mark"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!table.rows.length && !removed.length">
            <td :colspan="columns.length + 1" class="empty">没有内容</td>
          </tr>
          <tr
            v-for="row in table.rows"
            :key="row.key"
            :class="{ added: diff?.added.has(row.key) }"
            data-testid="history-row"
            :data-state="diff?.added.has(row.key) ? 'added' : diff?.changed[row.key] ? 'changed' : 'same'"
          >
            <td
              v-for="c in columns"
              :key="c.key"
              :class="{ changed: diff?.changed[row.key]?.[c.key] !== undefined }"
            >
              <del v-if="diff?.changed[row.key]?.[c.key] !== undefined" class="old">{{
                diff.changed[row.key]![c.key] || '空'
              }}</del>
              <span>{{ row.cells[c.key] ?? '' }}</span>
            </td>
            <td class="mark">
              <el-tag v-if="diff?.added.has(row.key)" size="small" type="success">新增</el-tag>
              <el-tag v-else-if="diff?.changed[row.key]" size="small" type="warning">修改</el-tag>
            </td>
          </tr>
          <tr v-for="row in removed" :key="`removed-${row.key}`" class="removed" data-testid="history-row" data-state="removed">
            <td v-for="c in columns" :key="c.key">
              <del>{{ row.cells[c.key] ?? '' }}</del>
            </td>
            <td class="mark"><el-tag size="small" type="danger">删除</el-tag></td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
.fields {
  display: grid;
  grid-template-columns: max-content 1fr max-content 1fr;
  gap: 6px 12px;
  margin: 0 0 16px;
  font-size: 13px;
}

.fields dt {
  color: var(--el-text-color-secondary);
  text-align: right;
}

.fields dd {
  margin: 0;
  overflow-wrap: anywhere;
}

.changed {
  background: #fef3c7;
  border-radius: 3px;
}

.fields dd.changed {
  padding: 0 4px;
}

.old {
  margin-right: 6px;
  color: var(--el-color-danger);
}

.table h5 {
  margin: 0 0 6px;
  font-size: 13px;
}

.table {
  margin-bottom: 16px;
}

.grid {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.grid th {
  padding: 6px 8px;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-secondary);
  font-weight: 500;
  text-align: left;
}

.grid td {
  padding: 6px 8px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.grid td .old {
  display: block;
  margin: 0;
  font-size: 12px;
}

.grid tr.added td {
  background: #dcfce7;
}

.grid tr.removed td {
  background: #fee2e2;
  color: var(--el-color-danger);
}

.mark {
  width: 56px;
  text-align: right;
}

.empty {
  color: var(--el-text-color-secondary);
  text-align: center;
}

@media (max-width: 640px) {
  .fields {
    grid-template-columns: max-content 1fr;
  }
}
</style>
