<script setup lang="ts">
import type { ConsoleMenu, Permission, Schemas } from '@edp/api-client'
import { ArrowRight, Search } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, ref } from 'vue'

import { MENU } from '../../menu'
import { MENU_ICONS } from '../../menuIcons'
import {
  buildModules,
  matches,
  moduleState,
  toggleModule,
  type PermissionModule,
} from '../../permissionModules'
import { addRequired, compareWithRoles, lacksPermission, orderMenus } from '../../staffAccess'

/**
 * 分配权限（角色对话框和员工的"页面和权限"共用）：按业务模块分组，每个模块可以展开、收起和全选，
 * 显示勾了几项；权限按查看、操作、管理的先后排列，长名称分成标题和说明。
 * - 传了 menus（员工自定义时）：每个模块上方是它的页面，勾上页面自动勾上它需要的权限。
 * - 传了 baseline（角色给的权限）：标出比角色多给的、去掉的。
 * - 自己没有的权限不能勾。
 */

const props = withDefaults(
  defineProps<{
    permissions: Permission[]
    /** 传了（员工自定义时）就在每个模块上方列出它的页面。 */
    menus?: ConsoleMenu[]
    catalog: Schemas['PermissionInfo'][]
    canGrant: (code: Permission) => boolean
    baseline?: Permission[]
    features?: Readonly<Record<string, boolean>>
    /** 默认展开全部模块（角色对话框）；员工的自定义默认收起，只看每个模块的摘要。 */
    expanded?: boolean
  }>(),
  { menus: undefined, baseline: undefined, features: () => ({}), expanded: false },
)
/** 页面和权限一起变（勾上页面时会补上它需要的权限），所以一次发出完整的结果。 */
const emit = defineEmits<{
  change: [value: { permissions: Permission[]; menus: ConsoleMenu[] | undefined }]
}>()

function commit(next: { permissions?: Permission[]; menus?: ConsoleMenu[] }): void {
  emit('change', {
    permissions: next.permissions ?? props.permissions,
    menus: next.menus ?? props.menus,
  })
}

const modules = computed(() => buildModules(props.catalog))
const selected = computed(() => new Set(props.permissions))
const pagesMode = computed(() => props.menus !== undefined)
const shownPages = computed(
  () =>
    new Set(
      MENU.filter((item) => !item.feature || props.features[item.feature] !== false).map(
        (item) => item.name,
      ),
    ),
)
const menuOf = (name: ConsoleMenu) => MENU.find((item) => item.name === name)
const diff = computed(() =>
  props.baseline ? compareWithRoles(props.permissions, props.baseline) : null,
)

const keyword = ref('')
const open = ref(new Set<string>(props.expanded ? modules.value.map((m) => m.key) : []))
const searching = computed(() => keyword.value.trim() !== '')

/** 显示的模块：搜索时只留有匹配项的模块和匹配的权限、页面。 */
const visible = computed(() =>
  modules.value.flatMap((module) => {
    const pages = pagesMode.value ? module.pages.filter((name) => shownPages.value.has(name)) : []
    if (!searching.value) return [{ module, pages, items: module.items }]
    const word = keyword.value.trim().toLowerCase()
    const items = module.items.filter((entry) => matches(entry, word))
    const hitPages = pages.filter((name) => (menuOf(name)?.title ?? '').toLowerCase().includes(word))
    const hitModule = module.title.toLowerCase().includes(word)
    if (hitModule) return [{ module, pages, items: module.items }]
    return items.length || hitPages.length ? [{ module, pages: hitPages, items }] : []
  }),
)

const total = computed(() => modules.value.reduce((sum, m) => sum + m.items.length, 0))
const allOpen = computed(() => modules.value.every((m) => open.value.has(m.key)))

function isOpen(key: string): boolean {
  return searching.value || open.value.has(key)
}

function toggleOpen(key: string): void {
  const next = new Set(open.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  open.value = next
}

function toggleAllOpen(): void {
  open.value = new Set(allOpen.value ? [] : modules.value.map((m) => m.key))
}

function state(module: PermissionModule) {
  return moduleState(module, selected.value, props.canGrant)
}

function moduleDiff(module: PermissionModule): { extra: number; revoked: number } {
  if (!diff.value) return { extra: 0, revoked: 0 }
  const codes = module.items.map((entry) => entry.code)
  return {
    extra: codes.filter((code) => diff.value?.extra.has(code)).length,
    revoked: codes.filter((code) => diff.value?.revoked.has(code)).length,
  }
}

function selectModule(module: PermissionModule, on: boolean): void {
  commit({ permissions: toggleModule(module, props.permissions, on, props.canGrant) })
}

function togglePermission(code: Permission, on: boolean): void {
  const rest = props.permissions.filter((entry) => entry !== code)
  commit({ permissions: on ? [...rest, code] : rest })
}

/** 勾上页面时补上它需要的权限；自己没有的权限不能给出，页面仍然勾上并标出缺少权限。 */
function togglePage(name: ConsoleMenu, on: boolean): void {
  const current = props.menus ?? []
  if (!on) {
    commit({ menus: current.filter((entry) => entry !== name) })
    return
  }
  const { permissions: next, refused } = addRequired([name], props.permissions, props.canGrant)
  if (refused.length) {
    ElMessage.warning(
      `${refused.map((entry) => entry.title).join('、')}需要的权限你自己没有，不能给出，页面不会显示`,
    )
  }
  commit({ permissions: next, menus: orderMenus([...current, name]) })
}

function pageChecked(name: ConsoleMenu): boolean {
  return (props.menus ?? []).includes(name)
}

function pageLacks(name: ConsoleMenu): boolean {
  const menu = menuOf(name)
  return !!menu && pageChecked(name) && lacksPermission(menu, props.permissions)
}

/** 收起时在模块标题后面列出勾上的页面。 */
function chosenPages(pages: ConsoleMenu[]): string {
  return pages
    .filter((name) => pageChecked(name))
    .map((name) => menuOf(name)?.title ?? name)
    .join('、')
}
</script>

<template>
  <div class="picker" data-testid="permission-picker">
    <div class="toolbar">
      <span class="summary">
        已选 <b>{{ permissions.length }}</b> / {{ total }} 项
        <span v-if="diff" class="diff" data-testid="access-diff"
          >和角色比：多给 {{ diff.extra.size }} 项，去掉 {{ diff.revoked.size }} 项</span
        >
      </span>
      <span class="tools">
        <el-input
          v-model="keyword"
          clearable
          size="small"
          placeholder="搜索权限或页面"
          :prefix-icon="Search"
          class="search"
          data-testid="permission-search"
        />
        <el-button link type="primary" data-testid="permission-expand" @click="toggleAllOpen">
          {{ allOpen ? '全部收起' : '全部展开' }}
        </el-button>
      </span>
    </div>

    <div class="modules">
      <section
        v-for="{ module, pages, items } in visible"
        :key="module.key"
        class="module"
        :class="{ open: isOpen(module.key) }"
        :data-testid="`perm-module-${module.key}`"
      >
        <div
          class="head"
          role="button"
          tabindex="0"
          :aria-expanded="isOpen(module.key)"
          @click="toggleOpen(module.key)"
          @keydown.enter.prevent="toggleOpen(module.key)"
          @keydown.space.prevent="toggleOpen(module.key)"
        >
          <span class="check" @click.stop>
            <el-checkbox
              :model-value="state(module).all"
              :indeterminate="state(module).some"
              :disabled="state(module).grantable === 0"
              :aria-label="`${module.title}：全选`"
              :data-testid="`perm-module-all-${module.key}`"
              @change="selectModule(module, $event === true)"
            />
          </span>
          <el-icon class="icon"><component :is="MENU_ICONS[module.icon]" /></el-icon>
          <span class="title">{{ module.title }}</span>
          <span v-if="pagesMode && !isOpen(module.key) && chosenPages(pages)" class="pages-summary">
            页面：{{ chosenPages(pages) }}
          </span>
          <span class="spacer" />
          <span v-if="moduleDiff(module).extra" class="mark extra">多给 {{ moduleDiff(module).extra }}</span>
          <span v-if="moduleDiff(module).revoked" class="mark revoked"
            >去掉 {{ moduleDiff(module).revoked }}</span
          >
          <span class="count" :class="{ zero: state(module).checked === 0 }"
            >{{ state(module).checked }}/{{ state(module).total }}</span
          >
          <el-icon class="chevron"><ArrowRight /></el-icon>
        </div>

        <div v-show="isOpen(module.key)" class="body">
          <div v-if="pages.length" class="pages">
            <span class="row-label">页面</span>
            <span v-for="name in pages" :key="name" class="page">
              <el-check-tag
                :checked="pageChecked(name)"
                :data-testid="`access-menu-${name}`"
                @change="togglePage(name, $event)"
              >
                {{ menuOf(name)?.title ?? name }}
              </el-check-tag>
              <span v-if="pageLacks(name)" class="lack">缺少权限，不会显示</span>
            </span>
          </div>
          <div v-if="items.length" class="items">
            <el-checkbox
              v-for="entry in items"
              :key="entry.code"
              :model-value="selected.has(entry.code)"
              :disabled="!canGrant(entry.code)"
              class="item"
              :data-testid="`perm-${entry.code}`"
              @change="togglePermission(entry.code, $event === true)"
            >
              <span class="item-title">
                {{ entry.title }}
                <span v-if="diff?.extra.has(entry.code)" class="mark extra">多给</span>
                <span v-if="diff?.revoked.has(entry.code)" class="mark revoked">去掉</span>
              </span>
              <span v-if="entry.hint" class="item-hint">{{ entry.hint }}</span>
            </el-checkbox>
          </div>
        </div>
      </section>
      <p v-if="visible.length === 0" class="empty">没有找到相关的权限或页面</p>
    </div>
  </div>
</template>

<style scoped>
.picker {
  width: 100%;
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}

.summary {
  color: var(--el-text-color-regular);
  font-size: 13px;
}

.summary b {
  color: var(--el-color-primary);
}

.diff {
  margin-left: 8px;
  color: var(--el-text-color-secondary);
}

.tools {
  display: flex;
  align-items: center;
  gap: 8px;
}

.search {
  width: 180px;
}

.modules {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.module {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-bg-color);
}

.module.open {
  border-color: var(--el-border-color);
}

.head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 40px;
  padding: 0 12px;
  cursor: pointer;
  user-select: none;
  line-height: 1.4;
}

.head:hover,
.head:focus-visible {
  background: var(--el-fill-color-light);
  outline: none;
}

.module.open .head {
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.check {
  display: inline-flex;
}

.icon {
  color: var(--el-text-color-secondary);
}

.title {
  font-weight: 600;
  color: var(--el-text-color-primary);
  white-space: nowrap;
}

.pages-summary {
  min-width: 0;
  overflow: hidden;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.spacer {
  flex: 1;
}

.count {
  min-width: 36px;
  color: var(--el-color-primary);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  text-align: right;
}

.count.zero {
  color: var(--el-text-color-placeholder);
}

.chevron {
  color: var(--el-text-color-placeholder);
  transition: transform 0.2s;
}

.module.open .chevron {
  transform: rotate(90deg);
}

.body {
  padding: 10px 12px 12px;
}

.pages {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 8px;
  margin-bottom: 10px;
  padding-bottom: 10px;
  border-bottom: 1px dashed var(--el-border-color-lighter);
}

.row-label {
  margin-right: 4px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.page {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.lack {
  color: var(--el-color-danger);
  font-size: 12px;
}

.items {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
  gap: 6px 16px;
}

/* 勾选框默认是一行 32px 高；这里标题和说明分两行。 */
.item {
  height: auto;
  margin-right: 0;
  align-items: flex-start;
  white-space: normal;
}

.item :deep(.el-checkbox__input) {
  margin-top: 3px;
}

.item :deep(.el-checkbox__label) {
  display: flex;
  flex-direction: column;
  line-height: 1.45;
}

.item-title {
  color: var(--el-text-color-primary);
  font-size: 13px;
}

.item.is-disabled .item-title {
  color: var(--el-text-color-placeholder);
}

.item-hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.mark {
  margin-left: 4px;
  padding: 0 4px;
  border-radius: 3px;
  font-size: 12px;
  white-space: nowrap;
}

.mark.extra {
  color: var(--el-color-success);
  background: var(--el-color-success-light-9);
}

.mark.revoked {
  color: var(--el-color-warning-dark-2);
  background: var(--el-color-warning-light-9);
}

.empty {
  margin: 16px 0;
  color: var(--el-text-color-secondary);
  text-align: center;
}

@media (max-width: 560px) {
  .pages-summary {
    display: none;
  }

  .search {
    width: 140px;
  }
}
</style>
