<script setup lang="ts">
import type { Schemas } from '@edp/api-client'
import { Delete } from '@element-plus/icons-vue'
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { layoutStaffDiagram, type DiagramDirection } from '../../staffDiagram'
import { mergeDiagramCards } from '../../staffDiagramCards'

const props = defineProps<{ staff: Schemas['StaffOut'][]; company: string; canManage: boolean; focusId?: string | null; diagramNodes?: Schemas['StaffDiagramNodeOut'][]; adding?: boolean; deleteHint?: (member: Schemas['StaffOut']) => string | null }>()
const emit = defineEmits<{ 'add-branch': [parentId: string | null, direction: DiagramDirection]; 'delete-card': [nodeId: string]; 'edit-draft': [nodeId: string]; 'edit-staff': [member: Schemas['StaffOut']] }>()
const activeNode = ref<string | null>(null)
const sizes = reactive<Record<string, { width: number; height: number }>>({})
/** 最顶部的卡片（§39.5）：企业所有者的卡片，企业里的其他角色从这里延伸出去；没有时显示企业。 */
const owner = computed(() => props.staff.find((member) => member.is_owner) ?? null)
const diagram = computed(() => layoutStaffDiagram(mergeDiagramCards(props.staff, props.diagramNodes ?? [], owner.value?.id), sizes))
const drafts = computed(() => new Map((props.diagramNodes ?? []).filter((node) => !node.staff_id).map((node) => [node.id, node])))
const byId = computed(() => {
  const map = new Map(props.staff.map((member) => [member.id, member]))
  for (const node of props.diagramNodes ?? []) {
    const member = node.staff_id ? map.get(node.staff_id) : undefined
    if (member) map.set(node.id, member)
  }
  if (owner.value) map.set('company', owner.value)
  return map
})
function cardName(id: string): string {
  return byId.value.get(id)?.display_name ?? (id === 'company' ? props.company : '待完善员工')
}
/** 员工卡片不能删除时的提示（权限高于自己、自己的卡片），删除按钮置灰；待完善卡片都可以删除。 */
function deleteBlocked(id: string): string | null {
  const member = drafts.value.has(id) ? undefined : byId.value.get(id)
  return member && props.deleteHint ? props.deleteHint(member) : null
}
function editCard(id: string): void {
  if (!props.canManage) return
  if (drafts.value.has(id)) emit('edit-draft', id)
  else if (byId.value.has(id)) emit('edit-staff', byId.value.get(id)!)
}
function clickCard(id: string): void {
  if (window.matchMedia('(hover: none)').matches && activeNode.value !== id) {
    activeNode.value = id
    return
  }
  editCard(id)
}
const elements = new Map<string, HTMLElement>()
const nodeRefs = new Map<string, (element: unknown) => void>()
const observer = new ResizeObserver((entries) => {
  for (const entry of entries) {
    const id = (entry.target as HTMLElement).dataset.nodeId!
    const { width, height } = entry.target.getBoundingClientRect()
    if (sizes[id]?.width !== width || sizes[id]?.height !== height) sizes[id] = { width, height }
  }
})
function bindNode(id: string, element: unknown): void {
  const previous = elements.get(id)
  if (element === previous) return
  if (previous) observer.unobserve(previous)
  if (element instanceof HTMLElement) {
    elements.set(id, element)
    observer.observe(element)
  } else {
    elements.delete(id)
    delete sizes[id]
  }
}
function nodeRef(id: string): (element: unknown) => void {
  if (!nodeRefs.has(id)) nodeRefs.set(id, (element) => bindNode(id, element))
  return nodeRefs.get(id)!
}
watch(() => [props.focusId, props.staff], async () => {
  await nextTick()
  if (props.focusId) elements.get(props.focusId)?.scrollIntoView({ block: 'nearest', inline: 'center', behavior: 'smooth' })
})
onBeforeUnmount(() => observer.disconnect())
const directions: { key: DiagramDirection; label: string }[] = [
  { key: 'left', label: '左侧' }, { key: 'right', label: '右侧' }, { key: 'down', label: '下方' },
]
</script>

<template>
  <div class="tree-viewport" tabindex="0" aria-label="员工思维导图，可横向和纵向滚动">
    <div class="staff-tree" :style="{ width: `${diagram.width}px`, height: `${diagram.height}px` }">
      <svg class="connections" :width="diagram.width" :height="diagram.height" aria-hidden="true">
        <path v-for="edge in diagram.edges" :key="`${edge.from}-${edge.to}`" :d="edge.path" />
      </svg>
      <article v-for="node in diagram.nodes" :key="node.id"
        :ref="nodeRef(node.id)" :data-node-id="node.id"
        :class="['staff-node', { company: node.id === 'company' && !owner, root: node.id === 'company', draft: drafts.has(node.id), selected: activeNode === node.id, administrator: byId.get(node.id)?.roles.includes('tenant_admin'), disabled: byId.has(node.id) && byId.get(node.id)?.status !== 'active' }]"
        :style="{ left: `${node.x}px`, top: `${node.y}px` }"
        :data-testid="drafts.has(node.id) ? `staff-draft-${node.id}` : byId.has(node.id) ? `staff-node-${byId.get(node.id)?.username}` : 'staff-node-company'"
        :data-root="node.id === 'company' ? 'true' : undefined"
        :tabindex="canManage ? 0 : undefined" @click="clickCard(node.id)"
        @keydown.enter.self.prevent="editCard(node.id)" @keydown.space.self.prevent="editCard(node.id)">
        <template v-if="node.id === 'company' && !owner">
          <span class="node-caption">企业</span>
          <strong>{{ company }}</strong>
          <span>{{ staff.length }} 位员工</span>
        </template>
        <template v-else-if="drafts.has(node.id)">
          <strong class="draft-title">待完善员工</strong>
          <p class="draft-hint">点击卡片填写员工资料、角色和权限</p>
          <el-tag type="info" size="small">尚未创建账号</el-tag>
          <el-button v-if="canManage" link type="primary" class="draft-edit" @click.stop="editCard(node.id)">完善信息</el-button>
        </template>
        <template v-else-if="byId.has(node.id)">
          <div v-if="node.id === 'company'" class="root-company" data-testid="root-company">
            <strong>{{ company }}</strong>
            <span>{{ staff.length }} 位员工</span>
          </div>
          <slot :member="byId.get(node.id)!" />
        </template>
        <span v-else>员工卡片暂不可用，请刷新</span>
        <el-tooltip v-if="canManage && node.id !== 'company'" :disabled="!deleteBlocked(node.id)" :content="deleteBlocked(node.id) ?? ''" placement="top">
          <span class="delete-wrap">
            <button type="button"
              :class="['delete-card', { blocked: deleteBlocked(node.id) }]" :disabled="adding || !!deleteBlocked(node.id)" :data-testid="`delete-card-${node.id}`"
              :title="deleteBlocked(node.id) ? undefined : '删除卡片'" aria-label="删除卡片"
              @click.stop="emit('delete-card', node.id)"><Delete aria-hidden="true" /></button>
          </span>
        </el-tooltip>
        <div v-if="canManage" class="branch-actions">
          <button v-for="direction in directions" :key="direction.key" type="button"
            :class="['branch-plus', direction.key]" :disabled="adding"
            :data-testid="`branch-${node.id}-${direction.key}`"
            :aria-label="`在${cardName(node.id)}${direction.key === 'left' ? '左侧' : direction.key === 'right' ? '右侧' : '下方'}新增卡片`"
            @click.stop="emit('add-branch', node.id === 'company' ? null : node.id, direction.key)">＋</button>
        </div>
      </article>
    </div>
  </div>
</template>

<style scoped>
.tree-viewport { overflow: auto; max-height: 75vh; padding: 12px; border: 1px solid var(--el-border-color-lighter); border-radius: 12px; background: var(--el-fill-color-extra-light); }
.staff-tree { position: relative; margin: 0 auto; }
.connections { position: absolute; inset: 0; pointer-events: none; }
.connections path { fill: none; stroke: var(--el-border-color-darker); stroke-width: 2; stroke-linejoin: round; }
.staff-node { position: absolute; width: 260px; box-sizing: border-box; padding: 18px; background: var(--el-bg-color); border: 1px solid var(--el-border-color-lighter); border-top: 4px solid #08b6b0; border-radius: 18px; box-shadow: 0 6px 18px #00000008; }
.staff-node.administrator { border-top-color: #1647ce; border-radius: 8px; }
.staff-node.disabled { border-top-color: var(--el-text-color-placeholder); }
.staff-node.company { display: flex; flex-direction: column; gap: 8px; text-align: center; background: #1260ec; color: white; border: 0; border-radius: 10px; padding: 20px 16px; }
.company strong { font-size: 20px; overflow-wrap: anywhere; }
.company span { font-size: 13px; }
/* 企业所有者的卡片在最顶部：上方蓝色的一栏写着企业名称和员工数。 */
.staff-node.root:not(.company) { border-top: 0; box-shadow: 0 8px 24px #1647ce1f; }
.root-company { display: flex; flex-direction: column; gap: 4px; margin: -18px -18px 14px; padding: 14px 18px 12px; border-radius: 7px 7px 0 0; background: #1260ec; color: white; text-align: center; }
.root-company strong { font-size: 18px; line-height: 1.4; overflow-wrap: anywhere; }
.root-company span { font-size: 12px; opacity: .85; }
.node-caption { opacity: .8; }
.staff-node { cursor: default; }
.staff-node:not(.company) { cursor: pointer; }
.staff-node:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 4px; }
.staff-node:not(.company) :deep(.staff-name) { padding-right: 30px; }
.delete-wrap { position: absolute; top: 18px; right: 14px; display: flex; z-index: 3; }
.delete-card { display: flex; align-items: center; justify-content: center; width: 28px; height: 28px; padding: 5px; border: 0; border-radius: 6px; background: transparent; color: var(--el-color-danger); cursor: pointer; }
.delete-card:not(:disabled):hover { background: var(--el-color-danger-light-9); }
.delete-card:focus-visible { outline: 2px solid var(--el-color-danger); outline-offset: 2px; }
.delete-card:disabled { opacity: .5; cursor: wait; }
/* 权限高于自己的员工、自己的卡片：和置灰的"重置密码"一样，悬停时外层显示提示。 */
.delete-card.blocked { color: var(--el-color-danger-light-5); opacity: 1; cursor: not-allowed; }
.delete-card svg { width: 18px; height: 18px; }
.staff-node.draft { border: 1px dashed var(--el-color-primary-light-5); border-top: 4px solid var(--el-color-primary-light-5); background: var(--el-color-primary-light-9); min-height: 140px; }
.draft-title { display: block; padding-right: 30px; font-size: 16px; line-height: 28px; }
.draft-hint { font-size: 12px; color: var(--el-text-color-secondary); line-height: 1.6; }
.draft-edit { display: block; margin: 10px 0 0; }
.branch-actions { position: absolute; inset: 0; pointer-events: none; }
.branch-plus { position: absolute; width: 28px; height: 28px; border-radius: 50%; border: 1px solid var(--el-color-primary); background: var(--el-bg-color); color: var(--el-color-primary); font-size: 21px; line-height: 24px; padding: 0; cursor: pointer; opacity: 0; pointer-events: none; box-shadow: 0 2px 6px #00000014; transition: opacity .12s; z-index: 2; }
.branch-plus.left { left: -14px; top: calc(50% - 14px); }
.branch-plus.right { right: -14px; top: calc(50% - 14px); }
.branch-plus.down { bottom: -14px; left: calc(50% - 14px); }
.staff-node:hover .branch-plus, .staff-node:focus-within .branch-plus, .staff-node.selected .branch-plus { opacity: 1; pointer-events: auto; }
.branch-plus:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 3px; }
.branch-plus:disabled { cursor: wait; }
@media (hover: none) { .branch-plus { width: 36px; height: 36px; line-height: 32px; } .branch-plus.left { left: -18px; top: calc(50% - 18px); } .branch-plus.right { right: -18px; top: calc(50% - 18px); } .branch-plus.down { bottom: -18px; left: calc(50% - 18px); } }
</style>
