<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { reactive, ref, watch } from 'vue'

import { api } from '../../api'

/**
 * 仓库设置（设计文档 §25.13）：指定一名仓管，确认工人开的领料单和入库单；不指定时由最早创建的
 * 工人担任（小企业里仓管和工人可能是同一个人）。也可以设置为单据不需要确认（开单即生效）。
 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{ settings: Schemas['WarehouseSettingsOut'] | null }>()
const emit = defineEmits<{ saved: [settings: Schemas['WarehouseSettingsOut']] }>()

const staff = ref<Schemas['StaffOut'][]>([])
const saving = ref(false)
const form = reactive({ keeperId: '', confirmRequired: true })

watch(open, async (value) => {
  if (!value) return
  Object.assign(form, {
    keeperId: props.settings?.keeper_id ?? '',
    confirmRequired: props.settings?.confirm_required ?? true,
  })
  const { data } = await api.GET('/api/v1/staff')
  staff.value = data?.items.filter((s) => s.status === 'active') ?? []
})

async function save(): Promise<void> {
  saving.value = true
  const { data, error } = await api.PUT('/api/v1/warehouse/settings', {
    body: { keeper_id: form.keeperId || null, confirm_required: form.confirmRequired },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(data.effective_keeper_name ? `仓管：${data.effective_keeper_name}` : '已保存')
  open.value = false
  emit('saved', data)
}
</script>

<template>
  <el-dialog v-model="open" title="仓库设置" width="min(480px, 92vw)" append-to-body data-testid="keeper-dialog">
    <el-form label-position="top">
      <el-form-item label="仓管">
        <el-select
          v-model="form.keeperId"
          clearable
          filterable
          placeholder="不指定：由最早创建的工人担任"
          class="full"
          data-testid="keeper-select"
        >
          <el-option v-for="s in staff" :key="s.id" :label="s.display_name" :value="s.id">
            <span>{{ s.display_name }}</span>
            <span class="muted"> {{ s.username }}</span>
          </el-option>
        </el-select>
        <div class="muted hint">
          仓管确认领料单和入库单，也可以盘点和调整库存。
          <template v-if="!form.keeperId">
            {{
              settings?.fallback && settings.effective_keeper_name
                ? `现在由最早创建的工人${settings.effective_keeper_name}担任。`
                : '还没有工人时，由管理员确认。'
            }}
          </template>
        </div>
      </el-form-item>
      <el-form-item label="单据确认">
        <el-switch
          v-model="form.confirmRequired"
          active-text="要仓管确认后才修改库存"
          inactive-text="开单即生效"
          data-testid="keeper-confirm-required"
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button type="primary" :loading="saving" data-testid="keeper-save" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.full {
  width: 100%;
}

.hint {
  width: 100%;
  margin-top: 4px;
  line-height: 1.5;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
