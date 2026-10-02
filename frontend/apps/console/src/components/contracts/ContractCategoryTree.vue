<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, ref } from 'vue'

import { api } from '../../api'
import { categoryOptions, categoryTree, type Category, type CategoryNode } from '../../contracts'

/**
 * 合同的多层级分类（§34.2）：选中后合同和模板按它筛选（包含下级分类）；有管理权限时可以新建、改名、
 * 移动、调整顺序和删除。数量包含下级分类。
 */
const props = defineProps<{
  categories: Category[]
  maxDepth: number
  selected: string | null
  canManage: boolean
  /** 显示合同数还是模板数。 */
  count: 'contracts' | 'templates'
}>()
const emit = defineEmits<{ select: [id: string | null]; changed: [] }>()

interface Node {
  key: string
  label: string
  count: number
  depth: number
  node?: CategoryNode
  children?: Node[]
}

const tree = computed(() => categoryTree(props.categories))
const total = computed(() =>
  tree.value.reduce((sum, n) => sum + (props.count === 'contracts' ? n.contracts : n.templates), 0),
)
const nodes = computed<Node[]>(() => {
  const convert = (list: CategoryNode[]): Node[] =>
    list.map((n) => ({
      key: n.id,
      label: n.label,
      count: props.count === 'contracts' ? n.contracts : n.templates,
      depth: n.depth,
      node: n,
      children: convert(n.children),
    }))
  return [{ key: 'all', label: '全部', count: total.value, depth: 0 }, ...convert(tree.value)]
})

const moving = ref<{ open: boolean; node: CategoryNode | null; parent: string[] }>({
  open: false,
  node: null,
  parent: [],
})
const moveOptions = computed(() => {
  const strip = (options: ReturnType<typeof categoryOptions>, skip: string): typeof options =>
    options
      .filter((o) => o.value !== skip)
      .map((o) => ({ ...o, ...(o.children ? { children: strip(o.children, skip) } : {}) }))
  return strip(categoryOptions(tree.value), moving.value.node?.id ?? '')
})

function select(node: Node): void {
  emit('select', node.key === 'all' ? null : node.key)
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

async function done(error: unknown, message: string): Promise<boolean> {
  if (error) {
    ElMessage.error(errorMessage(error))
    return false
  }
  ElMessage.success(message)
  emit('changed')
  return true
}

async function create(parent: CategoryNode | null): Promise<void> {
  const name = await ask(parent ? `在「${parent.label}」下新建分类` : '新建分类')
  if (!name) return
  const { error } = await api.POST('/api/v1/contracts/categories', {
    body: { name, parent_id: parent?.id ?? null },
  })
  await done(error, '已新建')
}

async function addDefaults(): Promise<void> {
  const { error } = await api.POST('/api/v1/contracts/categories/defaults')
  await done(error, '已添加常用分类')
}

function siblings(node: CategoryNode): CategoryNode[] {
  const parent = props.categories.find((c) => c.id === node.id)?.parent_id ?? null
  const find = (list: CategoryNode[]): CategoryNode[] | null => {
    if (list.some((n) => n.id === node.id)) return list
    for (const n of list) {
      const found = find(n.children)
      if (found) return found
    }
    return null
  }
  return parent === null ? tree.value : (find(tree.value) ?? [])
}

async function swap(node: CategoryNode, step: -1 | 1): Promise<void> {
  const list = siblings(node)
  const index = list.findIndex((n) => n.id === node.id)
  const other = list[index + step]
  if (!other) return
  // 顺序按列表的位置重新编号，交换这两个。
  const order = list.map((n) => n.id)
  order[index] = other.id
  order[index + step] = node.id
  for (const [i, id] of order.entries()) {
    const current = props.categories.find((c) => c.id === id)
    if (current && current.sort !== i + 1) {
      const { error } = await api.PATCH('/api/v1/contracts/categories/{contract_category_id}', {
        params: { path: { contract_category_id: id } },
        body: { sort: i + 1 },
      })
      if (error) {
        ElMessage.error(errorMessage(error))
        return
      }
    }
  }
  emit('changed')
}

async function command(action: string, node: CategoryNode): Promise<void> {
  if (action === 'child') return create(node)
  if (action === 'up') return swap(node, -1)
  if (action === 'down') return swap(node, 1)
  if (action === 'move') {
    const parent = props.categories.find((c) => c.id === node.id)?.parent_id
    const path: string[] = []
    let current = parent ? props.categories.find((c) => c.id === parent) : undefined
    while (current) {
      path.unshift(current.id)
      current = current.parent_id ? props.categories.find((c) => c.id === current?.parent_id) : undefined
    }
    moving.value = { open: true, node, parent: path }
    return
  }
  if (action === 'rename') {
    const name = await ask('改名', node.label)
    if (!name || name === node.label) return
    const { error } = await api.PATCH('/api/v1/contracts/categories/{contract_category_id}', {
      params: { path: { contract_category_id: node.id } },
      body: { name },
    })
    await done(error, '已改名')
    return
  }
  if (action === 'delete') {
    try {
      await ElMessageBox.confirm(`删除分类「${node.label}」？只能删除空的分类。`, '删除', {
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        type: 'warning',
      })
    } catch {
      return
    }
    const { error } = await api.DELETE('/api/v1/contracts/categories/{contract_category_id}', {
      params: { path: { contract_category_id: node.id } },
    })
    if ((await done(error, '已删除')) && props.selected === node.id) emit('select', null)
  }
}

async function move(): Promise<void> {
  const node = moving.value.node
  if (!node) return
  const parent = moving.value.parent.at(-1) ?? null
  const { error } = await api.PATCH('/api/v1/contracts/categories/{contract_category_id}', {
    params: { path: { contract_category_id: node.id } },
    body: { parent_id: parent },
  })
  if (await done(error, '已移动')) moving.value.open = false
}
</script>

<template>
  <div class="tree" data-testid="contract-category-tree">
    <div class="head">
      <span>合同分类</span>
      <el-button v-if="canManage" link type="primary" size="small" data-testid="contract-category-create" @click="create(null)">
        新建分类
      </el-button>
    </div>
    <el-tree
      :data="nodes"
      node-key="key"
      :current-node-key="selected ?? 'all'"
      :props="{ label: 'label', children: 'children' }"
      default-expand-all
      highlight-current
      :expand-on-click-node="false"
      @node-click="select"
    >
      <template #default="{ data }">
        <span class="node" :data-testid="`contract-category-${data.label}`">
          <span class="label">{{ data.label }}</span>
          <span class="count">{{ data.count }}</span>
          <el-dropdown
            v-if="canManage && data.node"
            trigger="click"
            @command="(action: string) => command(action, data.node)"
          >
            <el-button link size="small" class="more" :data-testid="`contract-category-more-${data.label}`" @click.stop>
              ···
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item v-if="data.depth < maxDepth" command="child">新建下级分类</el-dropdown-item>
                <el-dropdown-item command="rename">改名</el-dropdown-item>
                <el-dropdown-item command="move">移到…</el-dropdown-item>
                <el-dropdown-item command="up">上移</el-dropdown-item>
                <el-dropdown-item command="down">下移</el-dropdown-item>
                <el-dropdown-item command="delete" divided>删除</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </span>
      </template>
    </el-tree>
    <div v-if="!categories.length" class="empty">
      <p>还没有分类。</p>
      <el-button v-if="canManage" size="small" data-testid="contract-category-defaults" @click="addDefaults">
        添加常用分类
      </el-button>
    </div>

    <el-dialog v-model="moving.open" title="移到…" width="420px" append-to-body>
      <p class="muted">把「{{ moving.node?.label }}」和它的下级分类移到：</p>
      <el-cascader
        v-model="moving.parent"
        :options="moveOptions"
        :props="{ checkStrictly: true }"
        clearable
        placeholder="第一级（不选）"
        class="wide"
        data-testid="contract-category-move-to"
      />
      <template #footer>
        <el-button @click="moving.open = false">取消</el-button>
        <el-button type="primary" data-testid="contract-category-move-save" @click="move">移动</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.tree {
  padding: 8px 4px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-bg-color);
}

.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 8px 6px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.node {
  display: flex;
  flex: 1;
  align-items: center;
  gap: 6px;
  min-width: 0;
  padding-right: 4px;
  font-size: 13px;
}

.label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.count {
  margin-left: auto;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.more {
  padding: 0 2px;
}

.empty {
  padding: 8px 12px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.empty p {
  margin: 0 0 6px;
}

.muted {
  margin: 0 0 8px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.wide {
  width: 100%;
}
</style>
