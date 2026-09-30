<script setup lang="ts">
import { errorMessage, type ConsoleMenu, type ConsoleProfile, type Schemas } from '@edp/api-client'
import { Check } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { api } from '../../api'
import { MENU } from '../../menu'
import { useAuthStore } from '../../stores/auth'

/**
 * 控制台（§25.15）：勾选除管理员以外每个岗位显示的菜单，没有调整的岗位用默认值。菜单不会超出权限
 * （勾选了、没有权限的也不显示），隐藏菜单也不改变权限。员工的岗位由角色决定。
 */
type Item = Schemas['ConsoleProfileMenus']

const auth = useAuthStore()
const items = ref<Item[]>([])
const chosen = reactive<Partial<Record<ConsoleProfile, ConsoleMenu[]>>>({})
const loading = ref(false)
const saving = ref(false)

const editable = computed(() => items.value.filter((item) => item.editable))
const rows = computed(() =>
  MENU.map((item) => ({
    name: item.name,
    title: item.title,
    off: !!item.feature && auth.me?.features?.[item.feature] === false,
  })),
)

function sameMenus(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((menu) => b.includes(menu))
}

function checked(profile: ConsoleProfile, menu: ConsoleMenu): boolean {
  return chosen[profile]?.includes(menu) ?? false
}

function toggle(profile: ConsoleProfile, menu: ConsoleMenu, on: boolean): void {
  const menus = chosen[profile] ?? []
  chosen[profile] = on ? [...menus, menu] : menus.filter((m) => m !== menu)
}

/** 和默认值不同（包括还没保存的修改）。 */
function customized(item: Item): boolean {
  return !sameMenus(chosen[item.profile] ?? [], item.defaults)
}

function reset(item: Item): void {
  chosen[item.profile] = [...item.defaults]
}

function apply(data: Schemas['ConsoleSettingsOut']): void {
  items.value = data.items
  for (const item of data.items) chosen[item.profile] = [...item.menus]
}

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/tenant/console')
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  apply(data)
}

async function save(): Promise<void> {
  const empty = editable.value.filter((item) => !chosen[item.profile]?.length)
  if (empty.length) {
    ElMessage.warning(`${empty.map((item) => item.label).join('、')}至少要显示一个菜单`)
    return
  }
  // 只提交调整过的岗位；和默认值相同的恢复默认（以后默认菜单更新时跟着更新）。
  const menus: Record<string, ConsoleMenu[]> = {}
  for (const item of editable.value) {
    if (customized(item)) menus[item.profile] = chosen[item.profile] ?? []
  }
  saving.value = true
  const { data, error } = await api.PUT('/api/v1/tenant/console', { body: { menus } })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  apply(data)
  ElMessage.success('已保存，员工重新打开控制台后生效')
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" data-testid="console-settings">
    <el-alert type="info" :closable="false" show-icon class="intro">
      <template #title>每个岗位的员工只看到勾选的菜单，控制台更简洁</template>
      员工的岗位由角色决定：主管、坐席（客服）、仓管、工人、知识管理员；自定义角色在“员工 → 角色”里选择岗位。
      一个员工有几个岗位时菜单合在一起。勾选了但没有相应权限的菜单也不显示；隐藏菜单不改变权限。
    </el-alert>

    <el-table :data="rows" border size="small" class="matrix" data-testid="console-matrix">
      <el-table-column label="菜单" min-width="150" fixed>
        <template #default="{ row }">
          {{ row.title }}
          <span v-if="row.off" class="muted">（套餐未包含）</span>
        </template>
      </el-table-column>
      <el-table-column label="管理员" width="90" align="center">
        <template #default>
          <el-icon class="fixed" title="管理员固定显示全部菜单"><Check /></el-icon>
        </template>
      </el-table-column>
      <el-table-column v-for="item in editable" :key="item.profile" min-width="110" align="center">
        <template #header>
          <div class="head">
            <span>{{ item.label }}</span>
            <el-button
              v-if="customized(item)"
              link
              type="primary"
              size="small"
              :data-testid="`console-reset-${item.profile}`"
              @click="reset(item)"
              >恢复默认</el-button
            >
          </div>
        </template>
        <template #default="{ row }">
          <el-checkbox
            :model-value="checked(item.profile, row.name)"
            :data-testid="`console-${item.profile}-${row.name}`"
            @change="(value: unknown) => toggle(item.profile, row.name, value === true)"
          />
        </template>
      </el-table-column>
    </el-table>

    <div class="actions">
      <el-button type="primary" :loading="saving" data-testid="console-save" @click="save">保存</el-button>
    </div>
  </div>
</template>

<style scoped>
.intro {
  margin-bottom: 12px;
  line-height: 1.6;
}

.matrix {
  max-width: 820px;
}

.head {
  display: flex;
  flex-direction: column;
  align-items: center;
  line-height: 1.4;
}

.fixed {
  color: var(--el-text-color-secondary);
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.actions {
  margin-top: 12px;
}
</style>
