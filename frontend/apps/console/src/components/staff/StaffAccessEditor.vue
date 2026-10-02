<script setup lang="ts">
import { errorMessage, type ConsoleMenu, type Permission, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, ref, watch } from 'vue'

import { api } from '../../api'
import { MENU } from '../../menu'
import { accessFromRoles, choosableMenus, previewMenus, type AccessForm } from '../../staffAccess'
import { useAuthStore } from '../../stores/auth'
import PermissionPicker from './PermissionPicker.vue'

/**
 * 员工的"页面和权限"（§31）：按角色，或者自定义看到的页面、登录后打开的页面和功能权限。自定义时
 * 从角色给的页面和权限开始，按业务模块勾选（PermissionPicker）；只能给出自己有的权限。
 */

const form = defineModel<AccessForm>({ required: true })
const props = defineProps<{ roleCodes: string[]; catalog: Schemas['PermissionInfo'][] }>()

const auth = useAuthStore()
/** 选中的角色给的页面和权限（/api/v1/staff/access-defaults）。 */
const defaults = ref<Schemas['StaffAccessDefaults'] | null>(null)
let request = 0

async function loadDefaults(): Promise<void> {
  const id = ++request
  if (props.roleCodes.length === 0) {
    defaults.value = null
    return
  }
  const { data, error } = await api.GET('/api/v1/staff/access-defaults', {
    params: { query: { role_codes: props.roleCodes } },
  })
  if (id !== request) return
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  defaults.value = data
  if (!data.adjustable && form.value.mode === 'custom') {
    // 选了租户管理员：不能单独调整。
    form.value = { ...form.value, mode: 'role' }
  } else if (form.value.mode === 'custom' && form.value.menus.length === 0) {
    // 角色的页面和权限还没取到时就选了"自定义"：取到后填入。
    form.value = accessFromRoles(data)
  }
}

watch(() => [...props.roleCodes].sort().join(','), loadDefaults, { immediate: true })

const adjustable = computed(() => defaults.value?.adjustable !== false)
const features = computed(() => auth.me?.features ?? {})
const title = (name: ConsoleMenu): string => MENU.find((item) => item.name === name)?.title ?? name
const canGrant = (code: Permission): boolean => auth.can(code)

/** 预览：这个员工会看到的页面和登录后打开的页面。 */
const shown = computed(() =>
  form.value.mode === 'custom'
    ? previewMenus(form.value, features.value).map((item) => item.name)
    : (defaults.value?.menus ?? []),
)
const landing = computed(() => {
  const home = form.value.mode === 'custom' ? form.value.home : ''
  const name = home && shown.value.includes(home) ? home : shown.value[0]
  return name ? title(name) : ''
})
/** 登录后打开：从勾上的页面里选。 */
const landingOptions = computed(() => {
  const chosen = new Set(form.value.menus)
  return choosableMenus(features.value).filter((item) => chosen.has(item.name))
})

/** 选择器一次给出页面和权限（勾上页面会补上它需要的权限）；去掉了登录后打开的页面时清空它。 */
function onPicker(value: { permissions: Permission[]; menus: ConsoleMenu[] | undefined }): void {
  const menus = value.menus ?? form.value.menus
  const home = form.value.home && menus.includes(form.value.home) ? form.value.home : ''
  form.value = { ...form.value, permissions: value.permissions, menus, home }
}

function setMode(mode: string | number | boolean | undefined): void {
  if (mode !== 'role' && mode !== 'custom') return
  if (mode === 'custom' && form.value.menus.length === 0 && defaults.value) {
    form.value = accessFromRoles(defaults.value)
    return
  }
  form.value = { ...form.value, mode }
}

function onHome(value: unknown): void {
  form.value = { ...form.value, home: (value as ConsoleMenu | undefined) ?? '' }
}
</script>

<template>
  <div class="access" data-testid="staff-access">
    <div class="mode">
      <el-radio-group :model-value="form.mode" data-testid="access-mode" @update:model-value="setMode">
        <el-radio-button value="role" data-testid="access-mode-role">按角色</el-radio-button>
        <el-radio-button value="custom" :disabled="!adjustable" data-testid="access-mode-custom">
          自定义
        </el-radio-button>
      </el-radio-group>
      <span class="hint">
        <template v-if="!adjustable">租户管理员看到全部页面、拥有全部权限，不能单独调整。</template>
        <template v-else-if="form.mode === 'role'">页面和权限跟着角色走，角色以后改了也跟着变。</template>
        <template v-else>从角色给的页面和权限开始，按模块勾选；只记录和角色的差别。</template>
      </span>
    </div>

    <dl class="preview" data-testid="access-preview">
      <dt>会看到的页面</dt>
      <dd data-testid="access-preview-pages">
        <template v-if="shown.length">
          <span v-for="name in shown" :key="name" class="page">{{ title(name) }}</span>
        </template>
        <span v-else class="none">看不到任何页面</span>
      </dd>
      <dt>登录后打开</dt>
      <dd>
        <!-- 自定义时在这里选；按角色时打开第一个页面。 -->
        <el-select
          v-if="form.mode === 'custom'"
          :model-value="form.home"
          clearable
          size="small"
          :placeholder="`第一个页面（${landing || '无'}）`"
          class="home"
          data-testid="access-home"
          @update:model-value="onHome"
        >
          <el-option
            v-for="item in landingOptions"
            :key="item.name"
            :label="item.title"
            :value="item.name"
          />
        </el-select>
        <span v-else data-testid="access-preview-home">{{ landing || '—' }}</span>
      </dd>
    </dl>

    <template v-if="form.mode === 'custom'">
      <PermissionPicker
        :permissions="form.permissions"
        :menus="form.menus"
        :catalog="catalog"
        :can-grant="canGrant"
        :baseline="(defaults?.permissions ?? []) as Permission[]"
        :features="features"
        @change="onPicker"
      />
    </template>
  </div>
</template>

<style scoped>
.access {
  width: 100%;
}

.mode {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
}

.hint {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.5;
}

.preview {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 6px 12px;
  margin: 10px 0 0;
  padding: 10px 12px;
  border-radius: 6px;
  background: var(--el-fill-color-light);
  font-size: 13px;
  line-height: 22px;
}

.preview dt {
  color: var(--el-text-color-secondary);
  white-space: nowrap;
}

.preview dd {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 6px;
  margin: 0;
  color: var(--el-text-color-primary);
}

.page {
  padding: 0 8px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  background: var(--el-bg-color);
  line-height: 20px;
}

.none {
  color: var(--el-color-danger);
}

.home {
  width: 200px;
}

.access :deep(.picker) {
  margin-top: 12px;
}
</style>
