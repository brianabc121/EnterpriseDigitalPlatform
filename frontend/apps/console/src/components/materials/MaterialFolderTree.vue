<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, ref } from 'vue'

import { api } from '../../api'
import { folderOptions, folderPath, folderTree, type FolderNode, type MaterialFolder } from '../../materials'

/**
 * 企业资料的多层级文件夹（§36.2）："全部"、"未归档"（没有放进文件夹的）和文件夹；选中文件夹时显示它和
 * 下级文件夹里的资料。有 material:manage 时可以新建、改名、移动、调整顺序和删除空文件夹。
 */
const props = defineProps<{
  folders: MaterialFolder[]
  maxDepth: number
  unfiled: number
  /** null 为全部，"unfiled" 为未归档，其余是文件夹 id。 */
  selected: string | null
  canManage: boolean
}>()
const emit = defineEmits<{ select: [id: string | null]; changed: [] }>()

interface Node {
  key: string
  label: string
  count: number
  depth: number
  node?: FolderNode
  children?: Node[]
}

const tree = computed(() => folderTree(props.folders))
const nodes = computed<Node[]>(() => {
  const convert = (list: FolderNode[]): Node[] =>
    list.map((n) => ({
      key: n.id,
      label: n.label,
      count: n.count,
      depth: n.depth,
      node: n,
      children: convert(n.children),
    }))
  const total = tree.value.reduce((sum, n) => sum + n.count, 0) + props.unfiled
  return [
    { key: 'all', label: '全部', count: total, depth: 0 },
    { key: 'unfiled', label: '未归档', count: props.unfiled, depth: 0 },
    ...convert(tree.value),
  ]
})

const moving = ref<{ open: boolean; node: FolderNode | null; parent: string[] }>({
  open: false,
  node: null,
  parent: [],
})
const moveOptions = computed(() => folderOptions(tree.value, moving.value.node?.id ?? ''))

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

function done(error: unknown, message: string): boolean {
  if (error) {
    ElMessage.error(errorMessage(error))
    return false
  }
  ElMessage.success(message)
  emit('changed')
  return true
}

async function create(parent: FolderNode | null): Promise<void> {
  const name = await ask(parent ? `在「${parent.label}」下新建文件夹` : '新建文件夹')
  if (!name) return
  const { error } = await api.POST('/api/v1/materials/folders', {
    body: { name, parent_id: parent?.id ?? null },
  })
  done(error, '已新建')
}

function siblings(node: FolderNode): FolderNode[] {
  const find = (list: FolderNode[]): FolderNode[] | null => {
    if (list.some((n) => n.id === node.id)) return list
    for (const n of list) {
      const found = find(n.children)
      if (found) return found
    }
    return null
  }
  return find(tree.value) ?? []
}

async function swap(node: FolderNode, step: -1 | 1): Promise<void> {
  const list = siblings(node)
  const index = list.findIndex((n) => n.id === node.id)
  const other = list[index + step]
  if (!other) return
  // 顺序按列表的位置重新编号，交换这两个。
  const order = list.map((n) => n.id)
  order[index] = other.id
  order[index + step] = node.id
  for (const [i, id] of order.entries()) {
    const current = props.folders.find((f) => f.id === id)
    if (current && current.sort !== i + 1) {
      const { error } = await api.PATCH('/api/v1/materials/folders/{material_folder_id}', {
        params: { path: { material_folder_id: id } },
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

async function command(action: string, node: FolderNode): Promise<void> {
  if (action === 'child') return create(node)
  if (action === 'up') return swap(node, -1)
  if (action === 'down') return swap(node, 1)
  if (action === 'move') {
    const parent = props.folders.find((f) => f.id === node.id)?.parent_id
    moving.value = { open: true, node, parent: folderPath(props.folders, parent) }
    return
  }
  if (action === 'rename') {
    const name = await ask('改名', node.label)
    if (!name || name === node.label) return
    const { error } = await api.PATCH('/api/v1/materials/folders/{material_folder_id}', {
      params: { path: { material_folder_id: node.id } },
      body: { name },
    })
    done(error, '已改名')
    return
  }
  if (action === 'delete') {
    try {
      await ElMessageBox.confirm(`删除文件夹「${node.label}」？只能删除空的文件夹。`, '删除', {
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        type: 'warning',
      })
    } catch {
      return
    }
    const { error } = await api.DELETE('/api/v1/materials/folders/{material_folder_id}', {
      params: { path: { material_folder_id: node.id } },
    })
    if (done(error, '已删除') && props.selected === node.id) emit('select', null)
  }
}

async function move(): Promise<void> {
  const node = moving.value.node
  if (!node) return
  const { error } = await api.PATCH('/api/v1/materials/folders/{material_folder_id}', {
    params: { path: { material_folder_id: node.id } },
    body: { parent_id: moving.value.parent.at(-1) ?? null },
  })
  if (done(error, '已移动')) moving.value.open = false
}
</script>

<template>
  <div class="tree" data-testid="material-folder-tree">
    <div class="head">
      <span>文件夹</span>
      <el-button
        v-if="canManage"
        link
        type="primary"
        size="small"
        data-testid="material-folder-create"
        @click="create(null)"
      >
        新建文件夹
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
        <span class="node" :data-testid="`material-folder-${data.label}`">
          <span class="label">{{ data.label }}</span>
          <span class="count">{{ data.count }}</span>
          <el-dropdown
            v-if="canManage && data.node"
            trigger="click"
            @command="(action: string) => command(action, data.node)"
          >
            <el-button
              link
              size="small"
              class="more"
              :data-testid="`material-folder-more-${data.label}`"
              @click.stop
            >
              ···
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item v-if="data.depth < maxDepth" command="child">新建下级文件夹</el-dropdown-item>
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
    <p v-if="!folders.length" class="empty">
      {{ canManage ? '还没有文件夹，可以按用途新建，例如"产品视频""公司介绍"。' : '还没有文件夹。' }}
    </p>

    <el-dialog v-model="moving.open" title="移到…" width="420px" append-to-body>
      <p class="muted">把「{{ moving.node?.label }}」和它的下级文件夹移到：</p>
      <div data-testid="material-folder-move-to" class="wide">
        <el-cascader
          v-model="moving.parent"
          :options="moveOptions"
          :props="{ checkStrictly: true }"
          clearable
          placeholder="第一级（不选）"
          class="wide"
        />
      </div>
      <template #footer>
        <el-button @click="moving.open = false">取消</el-button>
        <el-button type="primary" data-testid="material-folder-move-save" @click="move">移动</el-button>
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
  margin: 0;
  padding: 8px 12px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.muted {
  margin: 0 0 8px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.wide,
.wide :deep(.el-cascader) {
  width: 100%;
}
</style>
