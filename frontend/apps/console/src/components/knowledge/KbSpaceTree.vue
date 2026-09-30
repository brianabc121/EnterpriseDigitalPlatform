<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed } from 'vue'

import { api } from '../../api'
import { categoryTree, type CategoryNode } from '../../knowledge'
import { useKbSpacesStore } from '../../stores/kbSpaces'

/** 知识库左侧的空间与分类树：选择后筛选知识；有管理权限时可以新建、改名、删除。 */
export interface Selection {
  space_id?: string
  category_id?: string
  unassigned?: boolean
}

interface Node {
  key: string
  label: string
  count?: number
  kind: 'all' | 'unassigned' | 'space' | 'category'
  id?: string
  spaceId?: string
  depth?: number
  children?: Node[]
}

const props = defineProps<{ canManage: boolean; selected: string }>()
const emit = defineEmits<{ select: [key: string, selection: Selection]; changed: [] }>()
const store = useKbSpacesStore()

const nodes = computed<Node[]>(() => {
  const convert = (spaceId: string, list: CategoryNode[], counts: Map<string, number>): Node[] =>
    list.map((c) => ({
      key: `category:${c.id}`,
      label: c.name,
      count: counts.get(c.id),
      kind: 'category' as const,
      id: c.id,
      spaceId,
      depth: c.depth,
      children: convert(spaceId, c.children, counts),
    }))
  return [
    { key: 'all', label: '全部知识', kind: 'all' },
    ...store.spaces.map((s) => ({
      key: `space:${s.id}`,
      label: s.name,
      count: s.items,
      kind: 'space' as const,
      id: s.id,
      children: convert(s.id, categoryTree(s.categories), new Map(s.categories.map((c) => [c.id, c.items]))),
    })),
    { key: 'unassigned', label: '未归入空间', count: store.unassigned, kind: 'unassigned' },
  ]
})

function select(node: Node): void {
  const selection: Selection =
    node.kind === 'space'
      ? { space_id: node.id }
      : node.kind === 'category'
        ? { category_id: node.id }
        : node.kind === 'unassigned'
          ? { unassigned: true }
          : {}
  emit('select', node.key, selection)
}

async function ask(title: string, value = ''): Promise<string | null> {
  try {
    const result = await ElMessageBox.prompt('', title, {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      inputValue: value,
      inputPattern: /\S/,
      inputErrorMessage: '请填写名称',
    })
    return (result as { value: string }).value.trim().slice(0, 64)
  } catch {
    return null
  }
}

async function done(error: unknown, message: string): Promise<void> {
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(message)
  await store.load()
  emit('changed')
}

async function createSpace(): Promise<void> {
  const name = await ask('新建知识空间')
  if (!name) return
  const { error } = await api.POST('/api/v1/kb/spaces', { body: { name } })
  await done(error, '已新建')
}

async function command(action: string, node: Node): Promise<void> {
  if (!node.id) return
  if (action === 'child') {
    const name = await ask('新建分类')
    if (!name) return
    const { error } = await api.POST('/api/v1/kb/categories', {
      body: {
        space_id: node.kind === 'space' ? node.id : node.spaceId!,
        parent_id: node.kind === 'category' ? node.id : null,
        name,
      },
    })
    await done(error, '已新建')
  } else if (action === 'rename') {
    const name = await ask('改名', node.label)
    if (!name || name === node.label) return
    const { error } =
      node.kind === 'space'
        ? await api.PATCH('/api/v1/kb/spaces/{space_id}', {
            params: { path: { space_id: node.id } },
            body: { name },
          })
        : await api.PATCH('/api/v1/kb/categories/{category_id}', {
            params: { path: { category_id: node.id } },
            body: { name },
          })
    await done(error, '已改名')
  } else if (action === 'delete') {
    const what =
      node.kind === 'space'
        ? `删除知识空间「${node.label}」？其中的知识会保留（移出空间），分类一并删除。`
        : `删除分类「${node.label}」及其下级分类？其中的知识保留在空间里。`
    try {
      await ElMessageBox.confirm(what, '删除', {
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        type: 'warning',
      })
    } catch {
      return
    }
    const { error } =
      node.kind === 'space'
        ? await api.DELETE('/api/v1/kb/spaces/{space_id}', { params: { path: { space_id: node.id } } })
        : await api.DELETE('/api/v1/kb/categories/{category_id}', {
            params: { path: { category_id: node.id } },
          })
    if (!error && props.selected === node.key) emit('select', 'all', {})
    await done(error, '已删除')
  }
}
</script>

<template>
  <div class="tree" data-testid="kb-space-tree">
    <div class="head">
      <span>知识空间</span>
      <el-button v-if="canManage" link type="primary" size="small" data-testid="kb-space-create" @click="createSpace">
        新建空间
      </el-button>
    </div>
    <el-tree
      :data="nodes"
      node-key="key"
      :current-node-key="selected"
      :props="{ label: 'label', children: 'children' }"
      default-expand-all
      highlight-current
      :expand-on-click-node="false"
      @node-click="select"
    >
      <template #default="{ data }">
        <span class="node" :data-testid="`kb-node-${data.label}`">
          <span class="label">{{ data.label }}</span>
          <span v-if="data.count !== undefined" class="count">{{ data.count }}</span>
          <el-dropdown
            v-if="canManage && (data.kind === 'space' || data.kind === 'category')"
            trigger="click"
            @command="(action: string) => command(action, data)"
          >
            <el-button link size="small" class="more" @click.stop>···</el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item v-if="data.kind === 'space' || (data.depth ?? 0) < 3" command="child">
                  新建{{ data.kind === 'space' ? '分类' : '子分类' }}
                </el-dropdown-item>
                <el-dropdown-item command="rename">改名</el-dropdown-item>
                <el-dropdown-item command="delete" divided>删除</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </span>
      </template>
    </el-tree>
  </div>
</template>

<style scoped>
.tree {
  width: 220px;
  flex-shrink: 0;
  border-right: 1px solid var(--el-border-color-lighter);
  padding-right: 8px;
}

.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.node {
  display: flex;
  align-items: center;
  gap: 4px;
  flex: 1;
  min-width: 0;
  font-size: 13px;
}

.label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.count {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.more {
  margin-left: auto;
  padding: 0 4px;
}
</style>
