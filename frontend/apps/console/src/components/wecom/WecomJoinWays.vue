<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'

import { api, formatDateTime } from '../../api'

/**
 * 客户群活码（设计 §10.4）："加入群聊"二维码，客户扫码进入指定的客户群，群满后可以自动建新群。
 * 放在活动海报、网页、公众号文章里；进群人数按二维码的 state 统计。
 */
const ways = ref<Schemas['JoinWayOut'][]>([])
const groups = ref<Schemas['GroupChatOut'][]>([])
const loading = ref(false)
const saving = ref(false)
const dialog = ref(false)
const form = reactive({
  name: '',
  chatIds: [] as string[],
  autoCreate: true,
  baseName: '',
  baseId: 1,
})

async function load(): Promise<void> {
  loading.value = true
  const [list, options] = await Promise.all([
    api.GET('/api/v1/admin/integrations/wecom/join-ways'),
    api.GET('/api/v1/wecom/broadcast-options'),
  ])
  loading.value = false
  if (!list.data) {
    ElMessage.error(errorMessage(list.error))
    return
  }
  ways.value = list.data.items
  groups.value = options.data?.group_chats ?? []
}

function open(): void {
  Object.assign(form, { name: '', chatIds: [], autoCreate: true, baseName: '', baseId: 1 })
  dialog.value = true
}

async function create(): Promise<void> {
  if (!form.name.trim() || !form.chatIds.length) {
    ElMessage.warning('请填写名称并选择客户群')
    return
  }
  saving.value = true
  const { data, error } = await api.POST('/api/v1/admin/integrations/wecom/join-ways', {
    body: {
      name: form.name.trim(),
      chat_ids: form.chatIds,
      auto_create_room: form.autoCreate,
      room_base_name: form.autoCreate ? form.baseName.trim() || null : null,
      room_base_id: form.autoCreate ? form.baseId : null,
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  dialog.value = false
  ElMessage.success('二维码已生成')
  await load()
}

async function remove(way: Schemas['JoinWayOut']): Promise<void> {
  try {
    await ElMessageBox.confirm(`删除后「${way.name}」的二维码不能再扫码进群。`, '删除二维码', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  const { error } = await api.DELETE('/api/v1/admin/integrations/wecom/join-ways/{way_id}', {
    params: { path: { way_id: way.id } },
  })
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  await load()
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" data-testid="wecom-join-ways">
    <div class="toolbar">
      <span class="muted">扫码进入指定的客户群（最多 5 个），群满后可以自动建新群</span>
      <el-button type="primary" size="small" data-testid="join-way-new" @click="open">
        新建二维码
      </el-button>
    </div>
    <el-table :data="ways" size="small" empty-text="还没有客户群二维码" data-testid="join-way-table">
      <el-table-column label="二维码" width="96">
        <template #default="{ row }">
          <el-image
            v-if="row.qr_code"
            :src="row.qr_code"
            :preview-src-list="[row.qr_code]"
            preview-teleported
            class="qr"
            fit="contain"
          />
        </template>
      </el-table-column>
      <el-table-column prop="name" label="名称" min-width="140" />
      <el-table-column label="客户群" min-width="200">
        <template #default="{ row }">{{ row.group_names.join('、') }}</template>
      </el-table-column>
      <el-table-column label="群满后" min-width="160">
        <template #default="{ row }">
          <template v-if="row.auto_create_room">
            自动建群「{{ row.room_base_name }}{{ row.room_base_id }}」起
          </template>
          <span v-else class="muted">不自动建群</span>
        </template>
      </el-table-column>
      <el-table-column label="进群客户" width="90">
        <template #default="{ row }">
          <span data-testid="join-way-joined">{{ row.joined }}</span>
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="80">
        <template #default="{ row }">
          <el-button link type="danger" size="small" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialog" title="新建客户群二维码" width="480px">
      <el-form label-width="96px" @submit.prevent="create">
        <el-form-item label="名称">
          <el-input
            v-model="form.name"
            maxlength="30"
            placeholder="例如：国庆活动"
            data-testid="join-way-name"
          />
        </el-form-item>
        <el-form-item label="客户群">
          <el-select
            v-model="form.chatIds"
            multiple
            filterable
            :multiple-limit="5"
            placeholder="选择客户群"
            no-data-text="还没有同步到客户群"
            data-testid="join-way-groups"
          >
            <el-option
              v-for="g in groups"
              :key="g.chat_id"
              :label="`${g.name || g.chat_id}（${g.member_count} 人）`"
              :value="g.chat_id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="群满后建群">
          <el-switch v-model="form.autoCreate" />
        </el-form-item>
        <template v-if="form.autoCreate">
          <el-form-item label="群名前缀">
            <el-input
              v-model="form.baseName"
              maxlength="40"
              placeholder="例如：VIP 客户群"
              data-testid="join-way-base"
            />
          </el-form-item>
          <el-form-item label="起始序号">
            <el-input-number v-model="form.baseId" :min="1" :max="100000" />
          </el-form-item>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" data-testid="join-way-save" @click="create">
          生成二维码
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.qr {
  width: 64px;
  height: 64px;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
