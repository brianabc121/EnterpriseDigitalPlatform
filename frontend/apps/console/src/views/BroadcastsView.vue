<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { api, formatDateTime } from '../api'
import { BROADCAST_STATUS } from '../wecom'

/**
 * 企业微信群发（设计 §10.4）：企业微信不允许经 API 直接给客户发消息，平台创建群发任务，
 * 员工（发给客户）或群主（发到客户群）在企业微信里确认后发出；发送结果定时回收。
 */
type Kind = 'single' | 'group'

const items = ref<Schemas['BroadcastOut'][]>([])
const options = ref<Schemas['BroadcastOptions'] | null>(null)
const loading = ref(false)
const saving = ref(false)
const dialog = ref(false)
const detail = ref<Schemas['BroadcastDetail'] | null>(null)
const busy = ref('')
const form = reactive({
  kind: 'single' as Kind,
  title: '',
  content: '',
  linkTitle: '',
  linkUrl: '',
  tags: [] as string[],
  owners: [] as string[],
  chats: [] as string[],
})

const KIND: Record<string, string> = { single: '发给客户', group: '发到客户群' }
const statusType = (status: string) =>
  status === 'created' ? 'primary' : status === 'failed' ? 'danger' : 'info'

const canSubmit = computed(() => {
  if (!form.title.trim() || !form.content.trim()) return false
  if (form.kind === 'single') return form.tags.length > 0 || form.owners.length > 0
  return form.chats.length > 0 || form.owners.length > 0
})

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/wecom/broadcasts')
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  items.value = data.items
}

async function open(): Promise<void> {
  Object.assign(form, {
    kind: 'single',
    title: '',
    content: '',
    linkTitle: '',
    linkUrl: '',
    tags: [],
    owners: [],
    chats: [],
  })
  dialog.value = true
  const { data, error } = await api.GET('/api/v1/wecom/broadcast-options')
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  options.value = data
}

async function create(): Promise<void> {
  if (!canSubmit.value) {
    ElMessage.warning('请填写名称、内容，并选择发送对象')
    return
  }
  const link =
    form.linkTitle.trim() && form.linkUrl.trim()
      ? { title: form.linkTitle.trim(), url: form.linkUrl.trim() }
      : null
  saving.value = true
  const { data, error } = await api.POST('/api/v1/wecom/broadcasts', {
    body: {
      kind: form.kind,
      title: form.title.trim(),
      content: form.content.trim(),
      link,
      audience:
        form.kind === 'single'
          ? { tags: form.tags.length ? form.tags : null, owner_ids: form.owners.length ? form.owners : null }
          : { chat_ids: form.chats.length ? form.chats : null, owner_ids: form.owners.length ? form.owners : null },
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  dialog.value = false
  if (data.status === 'failed') ElMessage.error(`群发任务创建失败：${data.error ?? ''}`)
  else ElMessage.success(`已创建群发任务（${data.target_count} 个对象），等待员工在企业微信里确认发送`)
  await load()
}

async function show(item: Schemas['BroadcastOut']): Promise<void> {
  const { data, error } = await api.GET('/api/v1/wecom/broadcasts/{broadcast_id}', {
    params: { path: { broadcast_id: item.id } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  detail.value = data
}

async function act(action: 'refresh' | 'remind' | 'cancel'): Promise<void> {
  const current = detail.value
  if (!current) return
  if (action === 'cancel') {
    try {
      await ElMessageBox.confirm('停止后，还没确认发送的员工不能再发送。', '停止群发', {
        type: 'warning',
        confirmButtonText: '停止',
        cancelButtonText: '取消',
      })
    } catch {
      return
    }
  }
  busy.value = action
  const path = { params: { path: { broadcast_id: current.id } } }
  const result =
    action === 'refresh'
      ? await api.POST('/api/v1/wecom/broadcasts/{broadcast_id}/refresh', path)
      : action === 'cancel'
        ? await api.POST('/api/v1/wecom/broadcasts/{broadcast_id}/cancel', path)
        : await api.POST('/api/v1/wecom/broadcasts/{broadcast_id}/remind', path)
  busy.value = ''
  if (result.error) {
    ElMessage.error(errorMessage(result.error))
    return
  }
  if (action === 'remind') ElMessage.success('已提醒还没确认的员工')
  else if (result.data) detail.value = result.data as Schemas['BroadcastDetail']
  await load()
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" data-testid="broadcasts-page">
    <div class="page-header">
      <h2>群发</h2>
      <el-button type="primary" data-testid="broadcast-new" @click="open">新建群发</el-button>
    </div>
    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="企业微信不允许平台直接给客户发消息：群发任务由员工（或群主）在企业微信里确认后发出，每位客户每天最多收到一条企业群发。"
      class="tip"
    />
    <el-table :data="items" empty-text="还没有群发任务" data-testid="broadcast-table">
      <el-table-column prop="title" label="名称" min-width="140" />
      <el-table-column label="类型" width="110">
        <template #default="{ row }">{{ KIND[row.kind] }}</template>
      </el-table-column>
      <el-table-column label="对象" width="80" prop="target_count" />
      <el-table-column label="状态" width="150">
        <template #default="{ row }">
          <el-tag size="small" :type="statusType(row.status)">
            {{ BROADCAST_STATUS[row.status] ?? row.status }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="已发送" width="90">
        <template #default="{ row }">{{ row.stats.sent ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="创建人" width="110">
        <template #default="{ row }">{{ row.created_by_name ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="创建时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="80">
        <template #default="{ row }">
          <el-button link type="primary" data-testid="broadcast-detail" @click="show(row)">
            详情
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialog" title="新建群发" width="560px">
      <el-form label-width="96px" @submit.prevent="create">
        <el-form-item label="类型">
          <el-radio-group v-model="form.kind" data-testid="broadcast-kind">
            <el-radio value="single">发给客户</el-radio>
            <el-radio value="group">发到客户群</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="名称">
          <el-input
            v-model="form.title"
            maxlength="64"
            placeholder="仅平台内显示"
            data-testid="broadcast-title"
          />
        </el-form-item>
        <el-form-item label="内容">
          <el-input
            v-model="form.content"
            type="textarea"
            :rows="4"
            maxlength="4000"
            show-word-limit
            data-testid="broadcast-content"
          />
        </el-form-item>
        <el-form-item label="附带链接">
          <div class="link">
            <el-input v-model="form.linkTitle" maxlength="64" placeholder="标题（可选）" />
            <el-input v-model="form.linkUrl" placeholder="https://" />
          </div>
        </el-form-item>
        <template v-if="form.kind === 'single'">
          <el-form-item label="客户标签">
            <el-select
              v-model="form.tags"
              multiple
              filterable
              placeholder="带任一标签的客户"
              data-testid="broadcast-tags"
            >
              <el-option v-for="t in options?.tags ?? []" :key="t" :label="t" :value="t" />
            </el-select>
          </el-form-item>
        </template>
        <el-form-item v-else label="客户群">
          <el-select
            v-model="form.chats"
            multiple
            filterable
            placeholder="选择客户群"
            data-testid="broadcast-chats"
          >
            <el-option
              v-for="g in options?.group_chats ?? []"
              :key="g.chat_id"
              :label="`${g.name || g.chat_id}（群主 ${g.owner_name ?? g.owner_userid}）`"
              :value="g.chat_id"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="form.kind === 'single' ? '归属坐席' : '群主'">
          <el-select v-model="form.owners" multiple filterable placeholder="不限">
            <el-option v-for="o in options?.owners ?? []" :key="o.id" :label="o.name" :value="o.id" />
          </el-select>
        </el-form-item>
        <p class="muted">
          {{
            form.kind === 'single'
              ? '每位客户由归属坐席（没有时由最早添加他的员工）在企业微信里确认发送。'
              : '每个客户群由群主在企业微信里确认发送。'
          }}
        </p>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" data-testid="broadcast-save" @click="create">
          创建群发任务
        </el-button>
      </template>
    </el-dialog>

    <el-drawer
      :model-value="!!detail"
      :title="detail?.title"
      size="560px"
      data-testid="broadcast-drawer"
      @close="detail = null"
    >
      <template v-if="detail">
        <p class="content">{{ detail.content }}</p>
        <div class="stats" data-testid="broadcast-stats">
          <span>对象 <b>{{ detail.target_count }}</b></span>
          <span>已发送 <b>{{ detail.stats.sent ?? 0 }}</b></span>
          <span>发送失败 <b>{{ detail.stats.failed ?? 0 }}</b></span>
          <span>
            员工已确认 <b>{{ detail.stats.members_confirmed ?? 0 }}</b> /
            {{ detail.stats.members_total ?? 0 }}
          </span>
        </div>
        <div class="actions">
          <el-button size="small" :loading="busy === 'refresh'" data-testid="broadcast-refresh" @click="act('refresh')">
            刷新结果
          </el-button>
          <template v-if="detail.status === 'created'">
            <el-button size="small" :loading="busy === 'remind'" @click="act('remind')">提醒发送</el-button>
            <el-button size="small" type="danger" plain :loading="busy === 'cancel'" @click="act('cancel')">
              停止群发
            </el-button>
          </template>
        </div>
        <el-alert v-if="detail.error" type="error" :title="detail.error" :closable="false" />
        <el-table :data="detail.members" size="small" empty-text="还没有回收到结果">
          <el-table-column label="员工" min-width="120">
            <template #default="{ row }">{{ row.name ?? row.userid }}</template>
          </el-table-column>
          <el-table-column label="确认发送" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="row.confirmed ? 'success' : 'info'">
                {{ row.confirmed ? '已发送' : '未确认' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="送达" width="70" prop="sent" />
          <el-table-column label="失败" width="70" prop="failed" />
          <el-table-column label="未发" width="70" prop="unsent" />
        </el-table>
        <p v-if="detail.fail_list.length" class="muted">
          {{ detail.fail_list.length }} 位客户企业微信没有接受（可能已不是好友）。
        </p>
        <p class="muted">
          最近回收：{{ detail.polled_at ? formatDateTime(detail.polled_at) : '还没有回收' }}
        </p>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.tip {
  margin-bottom: 12px;
}

.link {
  display: flex;
  gap: 8px;
  width: 100%;
}

.content {
  white-space: pre-wrap;
  margin-top: 0;
}

.stats {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 24px;
  margin-bottom: 12px;
  color: var(--el-text-color-secondary);
}

.stats b {
  color: var(--el-text-color-primary);
  font-size: 16px;
}

.actions {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
