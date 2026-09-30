<script setup lang="ts">
import { Close } from '@element-plus/icons-vue'
import { onBeforeUnmount, watch } from 'vue'

/**
 * 单据页（设计文档 §25.14）：下单、开领料单、开入库单和单据详情共用的版式。电脑上从右侧打开
 * （宽 1200px），手机上全屏：顶部是单据名称、单号、状态和处理进度，中间是单据头、明细和合计，
 * 底部是固定的操作栏。Ctrl+S（⌘S）保存。
 *
 * 这里的样式（doc-*）也给放在单据页里的内容用：单据头的字段栅格、明细表（手机上变成卡片）、合计。
 */
export interface SheetStep {
  label: string
  state: 'done' | 'current' | 'todo' | 'error'
  note?: string
}

const open = defineModel<boolean>({ required: true })
const props = withDefaults(
  defineProps<{
    title: string
    /** 单号；新单据为 null 时显示"保存后生成"。 */
    no?: string | null
    showNo?: boolean
    status?: { label: string; type: 'primary' | 'success' | 'info' | 'warning' | 'danger' } | null
    steps?: SheetStep[]
    loading?: boolean
    testid?: string
    statusTestid?: string
  }>(),
  {
    no: null,
    showNo: true,
    status: null,
    steps: () => [],
    loading: false,
    testid: 'doc-sheet',
    statusTestid: 'doc-status',
  },
)
const emit = defineEmits<{ save: [] }>()

function onKey(event: KeyboardEvent): void {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
    event.preventDefault()
    emit('save')
  }
}

watch(
  open,
  (value) => {
    if (value) window.addEventListener('keydown', onKey)
    else window.removeEventListener('keydown', onKey)
  },
  { immediate: true },
)
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <el-drawer
    v-model="open"
    direction="rtl"
    size="min(1200px, 100%)"
    append-to-body
    :with-header="false"
    class="doc-sheet"
    :data-testid="props.testid"
  >
    <div class="sheet">
      <header class="sheet-head">
        <div class="sheet-title">
          <h3>{{ title }}</h3>
          <span v-if="showNo" class="sheet-no" data-testid="doc-no">{{ no ?? '单号保存后生成' }}</span>
          <el-tag v-if="status" :type="status.type" :data-testid="statusTestid">{{ status.label }}</el-tag>
        </div>
        <div class="sheet-actions">
          <slot name="actions" />
          <el-button text circle aria-label="关闭" data-testid="doc-close" @click="open = false">
            <el-icon><Close /></el-icon>
          </el-button>
        </div>
      </header>
      <ol v-if="steps.length" class="sheet-steps" data-testid="doc-steps">
        <li v-for="(step, i) in steps" :key="i" :class="step.state">
          <span class="dot">{{ step.state === 'done' ? '✓' : step.state === 'error' ? '!' : i + 1 }}</span>
          <span class="step-label">{{ step.label }}</span>
          <span v-if="step.note" class="step-note">{{ step.note }}</span>
        </li>
      </ol>
      <main v-loading="loading" class="sheet-body">
        <slot />
      </main>
      <footer v-if="$slots.footer" class="sheet-foot">
        <slot name="footer" />
      </footer>
    </div>
  </el-drawer>
</template>

<style scoped>
.sheet {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.sheet-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 20px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.sheet-title {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  min-width: 0;
}

.sheet-title h3 {
  margin: 0;
  font-size: 18px;
}

.sheet-no {
  color: var(--el-text-color-secondary);
  font-family: var(--el-font-family);
  font-size: 13px;
}

.sheet-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}

.sheet-steps {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 20px;
  margin: 0;
  padding: 10px 20px;
  list-style: none;
  background: var(--el-fill-color-lighter);
  font-size: 13px;
}

.sheet-steps li {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--el-text-color-secondary);
}

.dot {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: var(--el-fill-color-dark);
  color: #fff;
  font-size: 11px;
}

.done .dot {
  background: var(--el-color-success);
}

.current .dot {
  background: var(--el-color-primary);
}

.current .step-label {
  color: var(--el-color-primary);
  font-weight: 600;
}

.error .dot {
  background: var(--el-color-danger);
}

.error .step-label {
  color: var(--el-color-danger);
}

.step-note {
  font-size: 12px;
}

.sheet-body {
  flex: 1;
  overflow: auto;
  padding: 16px 20px 24px;
}

.sheet-foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px 16px;
  padding: 10px 20px;
  border-top: 1px solid var(--el-border-color-lighter);
  background: var(--el-bg-color);
  box-shadow: 0 -2px 8px rgb(0 0 0 / 4%);
}

@media (max-width: 640px) {
  .sheet-head,
  .sheet-body,
  .sheet-foot,
  .sheet-steps {
    padding-left: 12px;
    padding-right: 12px;
  }
}
</style>

<style>
/* 单据页里的内容共用的样式（不加 scoped，放进单据页的内容都可以用）。 */
.doc-sheet .el-drawer__body {
  padding: 0;
}

.doc-section {
  margin-bottom: 20px;
}

.doc-section-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 10px;
}

.doc-section-head h4 {
  margin: 0;
  padding-left: 8px;
  border-left: 3px solid var(--el-color-primary);
  font-size: 14px;
}

.doc-fields {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px 20px;
}

.doc-field {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.doc-field.wide {
  grid-column: span 2;
}

.doc-field.full {
  grid-column: 1 / -1;
}

.doc-field > label {
  flex-shrink: 0;
  width: 72px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  text-align: right;
}

.doc-field > label.required::before {
  content: '*';
  margin-right: 2px;
  color: var(--el-color-danger);
}

.doc-field > .value {
  min-width: 0;
  font-size: 14px;
  overflow-wrap: anywhere;
}

.doc-field > .el-input,
.doc-field > .el-select,
.doc-field > .el-date-editor,
.doc-field > .el-autocomplete {
  flex: 1;
  min-width: 0;
}

.doc-grid {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.doc-grid th {
  padding: 8px;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-secondary);
  font-weight: 500;
  text-align: left;
  white-space: nowrap;
}

.doc-grid td {
  padding: 6px 8px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  vertical-align: middle;
}

.doc-grid .num {
  text-align: right;
  white-space: nowrap;
}

/* 明细的列宽（电脑上）：名称列占剩下的宽度，其他列固定；手机上明细是卡片，不用列宽。 */
@media (min-width: 641px) {
  .doc-grid {
    table-layout: fixed;
  }
}

.doc-grid .c-seq {
  width: 40px;
}

.doc-grid .c-unit {
  width: 64px;
}

.doc-grid .c-num {
  width: 96px;
}

.doc-grid .c-qty {
  width: 132px;
}

.doc-grid .c-money {
  width: 112px;
}

.doc-grid .c-price {
  width: 136px;
}

.doc-grid .c-change {
  width: 150px;
}

.doc-grid .c-ops {
  width: 56px;
}

.doc-grid .seq {
  width: 36px;
  color: var(--el-text-color-secondary);
  text-align: center;
}

.doc-grid .ops {
  width: 48px;
  text-align: center;
}

.doc-grid .item-name {
  font-weight: 500;
}

.doc-grid .item-sub {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.doc-grid .entry td {
  background: var(--el-fill-color-lighter);
}

.doc-grid tfoot td {
  padding: 8px;
  border-bottom: none;
  background: var(--el-fill-color-light);
  font-weight: 600;
}

.doc-grid .warn {
  color: var(--el-color-danger);
  font-weight: 600;
}

.doc-grid .el-input-number {
  width: 112px;
}

.doc-grid .price-input {
  width: 116px;
}

.doc-muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.doc-totals {
  display: grid;
  grid-template-columns: auto auto;
  justify-content: end;
  gap: 8px 16px;
  align-items: center;
  font-size: 14px;
}

.doc-totals .label {
  color: var(--el-text-color-secondary);
  text-align: right;
}

.doc-totals .grand {
  color: var(--el-color-danger);
  font-size: 20px;
  font-weight: 700;
}

.doc-foot-summary {
  color: var(--el-text-color-regular);
  font-size: 13px;
}

.doc-foot-summary b {
  color: var(--el-color-danger);
  font-size: 16px;
}

.doc-foot-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-left: auto;
}

@media (max-width: 900px) {
  .doc-fields {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 640px) {
  .doc-fields {
    grid-template-columns: 1fr;
  }

  .doc-field.wide {
    grid-column: auto;
  }

  .doc-field > label {
    width: 64px;
  }

  /* 手机上明细变成卡片：第一行名称，下面是各列（带列名）。 */
  .doc-grid thead,
  .doc-grid colgroup {
    display: none;
  }

  .doc-grid tbody tr {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 12px;
    padding: 10px 0;
    border-bottom: 1px solid var(--el-border-color-lighter);
  }

  .doc-grid tbody td {
    padding: 0;
    border: none;
  }

  .doc-grid tbody td.seq {
    display: none;
  }

  .doc-grid tbody td.main {
    flex: 1 1 calc(100% - 60px);
  }

  .doc-grid tbody td.ops {
    width: auto;
  }

  .doc-grid tbody td[data-label]::before {
    content: attr(data-label) ' ';
    color: var(--el-text-color-secondary);
    font-size: 12px;
  }

  .doc-grid tbody td.num {
    text-align: left;
  }

  .doc-grid .entry {
    padding: 10px 0;
  }

  .doc-grid .entry td {
    flex: 1 1 100%;
    background: none;
  }

  .doc-grid tfoot tr {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 12px;
  }

  .doc-grid tfoot td {
    padding: 6px 0;
    background: none;
  }

  .doc-totals {
    justify-content: stretch;
  }
}
</style>
