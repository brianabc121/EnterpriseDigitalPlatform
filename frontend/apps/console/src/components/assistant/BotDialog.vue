<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api } from '../../api'
import { compactFields, PROVIDER_NAME, type Bot, type Provider, type ProviderSpec } from '../../assistant'

/**
 * 添加或修改机器人：按平台列出要填的配置和密钥，说明要在平台后台配置什么；
 * 保存时平台校验凭证（Telegram 还会自动登记回调地址）。修改时不填密钥表示不变。
 */
const props = defineProps<{ bot: Bot | null; providers: ProviderSpec[]; webhookBase: string }>()
const visible = defineModel<boolean>({ required: true })
const emit = defineEmits<{ saved: [bot: Bot] }>()

const saving = ref(false)
const form = reactive({
  provider: 'telegram' as Provider,
  name: '',
  config: {} as Record<string, string>,
  secrets: {} as Record<string, string>,
})

const editing = computed(() => props.bot !== null)
const spec = computed(() => props.providers.find((p) => p.provider === form.provider) ?? null)

watch(visible, (open) => {
  if (!open) return
  form.provider = props.bot?.provider ?? 'telegram'
  form.name = props.bot?.name ?? ''
  form.config = Object.fromEntries(
    Object.entries(props.bot?.config ?? {}).map(([k, v]) => [k, String(v ?? '')]),
  )
  form.secrets = {}
})

watch(
  () => form.provider,
  (provider) => {
    if (!editing.value && !form.name) form.name = `${PROVIDER_NAME[provider]}助理`
  },
)

function hasSecret(key: string): boolean {
  return props.bot?.secret_keys.includes(key) ?? false
}

async function save(): Promise<void> {
  if (!spec.value || saving.value) return
  const name = form.name.trim()
  if (!name) {
    ElMessage.warning('请填写名称')
    return
  }
  const secrets = compactFields(form.secrets)
  const missing = spec.value.secret_fields.filter((f) => f.required && !secrets[f.key] && !hasSecret(f.key))
  if (missing.length) {
    ElMessage.warning(`请填写 ${missing.map((f) => f.label).join('、')}`)
    return
  }
  const body: Schemas['BotIn'] = {
    provider: form.provider,
    name,
    config: compactFields(form.config),
    secrets,
  }
  saving.value = true
  const { data, error } = props.bot
    ? await api.PUT('/api/v1/assistant/bots/{bot_id}', { params: { path: { bot_id: props.bot.id } }, body })
    : await api.POST('/api/v1/assistant/bots', { body })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(props.bot ? '已保存' : '机器人已添加，把回调地址配置到平台后台即可使用')
  emit('saved', data)
  visible.value = false
}
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="editing ? '修改机器人' : '添加机器人'"
    width="640px"
    destroy-on-close
    data-testid="bot-dialog"
  >
    <el-form label-width="150px">
      <el-form-item label="平台">
        <el-radio-group v-model="form.provider" :disabled="editing" data-testid="bot-provider">
          <el-radio-button v-for="p in providers" :key="p.provider" :value="p.provider">{{ p.name }}</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="名称" required>
        <el-input v-model="form.name" maxlength="64" data-testid="bot-name" />
      </el-form-item>
      <template v-if="spec">
        <el-form-item v-for="f in spec.config_fields" :key="f.key" :label="f.label" :required="f.required">
          <el-input v-model="form.config[f.key]" :placeholder="f.help" :data-testid="`bot-config-${f.key}`" />
        </el-form-item>
        <el-form-item v-for="f in spec.secret_fields" :key="f.key" :label="f.label" :required="f.required && !hasSecret(f.key)">
          <el-input
            v-model="form.secrets[f.key]"
            type="password"
            show-password
            :placeholder="hasSecret(f.key) ? '已保存，不填表示不变' : f.help"
            :data-testid="`bot-secret-${f.key}`"
          />
        </el-form-item>
        <el-form-item label="回调地址">
          <div class="hint">
            <template v-if="bot">{{ bot.webhook_url }}</template>
            <template v-else>保存后生成（{{ webhookBase }}…），把它填到平台后台的消息接收地址里。</template>
          </div>
        </el-form-item>
        <el-form-item label="在平台后台">
          <ul class="notes">
            <li v-for="note in spec.notes" :key="note">{{ note }}</li>
            <li>群消息：{{ spec.groups }}。{{ spec.notify ? '可以主动给员工发通知。' : '不能主动发消息。' }}</li>
          </ul>
        </el-form-item>
      </template>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" data-testid="bot-save" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.hint {
  font-size: 12px;
  line-height: 1.6;
  word-break: break-all;
  color: var(--el-text-color-secondary);
}

.notes {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.8;
  color: var(--el-text-color-secondary);
}
</style>
