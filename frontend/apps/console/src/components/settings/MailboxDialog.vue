<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api } from '../../api'
import {
  guessProvider,
  invalidIgnore,
  parseIgnore,
  SECURITY_LABEL,
  type MailAccount,
  type MailProvider,
  type MailSecurity,
} from '../../mail'

/**
 * 添加或修改邮箱：选择邮箱类型后自动带出收发信服务器（可以在高级设置里改），保存前测试连接。
 * 授权码加密保存，不会显示；修改时不填表示不变。
 */
const props = defineProps<{
  modelValue: boolean
  account: MailAccount | null
  providers: MailProvider[]
  allowInsecure: boolean
}>()
const emit = defineEmits<{
  'update:modelValue': [open: boolean]
  saved: [account: MailAccount]
}>()

const open = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})
const editing = computed(() => props.account !== null)

const form = reactive({
  address: '',
  provider: 'custom',
  secret: '',
  name: '',
  displayName: '',
  signature: '',
  ignore: '',
  username: '',
  imapHost: '',
  imapPort: 993,
  imapSecurity: 'ssl' as MailSecurity,
  smtpHost: '',
  smtpPort: 465,
  smtpSecurity: 'ssl' as MailSecurity,
})
/** 手工选过邮箱类型、改过渠道名称后，不再跟着地址自动变。 */
const touched = reactive({ provider: false, name: false })
const advanced = ref(false)
const testing = ref(false)
const saving = ref(false)
const result = ref<Schemas['MailTestOut'] | null>(null)

const provider = computed(() => props.providers.find((p) => p.key === form.provider) ?? null)
const isCustom = computed(() => form.provider === 'custom')
const securities = computed<MailSecurity[]>(() =>
  props.allowInsecure ? ['ssl', 'starttls', 'none'] : ['ssl', 'starttls'],
)

function applyPreset(key: string): void {
  const preset = props.providers.find((p) => p.key === key)
  if (!preset || key === 'custom') return
  Object.assign(form, {
    imapHost: preset.imap.host,
    imapPort: preset.imap.port,
    imapSecurity: preset.imap.security,
    smtpHost: preset.smtp.host,
    smtpPort: preset.smtp.port,
    smtpSecurity: preset.smtp.security,
  })
}

watch(open, (value) => {
  if (!value) return
  result.value = null
  const a = props.account
  touched.provider = touched.name = a !== null
  advanced.value = false
  if (a) {
    Object.assign(form, {
      address: a.address,
      provider: a.provider,
      secret: '',
      name: a.name,
      displayName: a.display_name,
      signature: a.signature ?? '',
      ignore: a.ignore_senders.join('\n'),
      username: a.username === a.address ? '' : a.username,
      imapHost: a.imap.host,
      imapPort: a.imap.port,
      imapSecurity: a.imap.security,
      smtpHost: a.smtp.host,
      smtpPort: a.smtp.port,
      smtpSecurity: a.smtp.security,
    })
  } else {
    Object.assign(form, {
      address: '',
      provider: 'custom',
      secret: '',
      name: '',
      displayName: '',
      signature: '',
      ignore: '',
      username: '',
      imapHost: '',
      imapPort: 993,
      imapSecurity: 'ssl',
      smtpHost: '',
      smtpPort: 465,
      smtpSecurity: 'ssl',
    })
  }
})

watch(
  () => form.address,
  (address) => {
    if (!touched.provider) {
      const key = guessProvider(props.providers, address)
      if (key !== form.provider) {
        form.provider = key
        applyPreset(key)
      }
    }
    if (!touched.name) form.name = address.trim()
  },
)

function chooseProvider(key: string): void {
  touched.provider = true
  applyPreset(key)
  result.value = null
}

function payload(): Schemas['MailAccountIn'] | null {
  const address = form.address.trim()
  if (!address.includes('@')) {
    ElMessage.warning('请填写邮箱地址')
    return null
  }
  if (!editing.value && !form.secret.trim()) {
    ElMessage.warning(`请填写${provider.value?.secret_label ?? '授权码'}`)
    return null
  }
  if (!form.imapHost.trim() || !form.smtpHost.trim()) {
    advanced.value = true
    ElMessage.warning('请填写收信和发信服务器')
    return null
  }
  const ignore = parseIgnore(form.ignore)
  const invalid = invalidIgnore(ignore)
  if (invalid.length) {
    ElMessage.warning(`忽略的发件人格式不正确：${invalid.join('、')}（填完整地址或 @域名）`)
    return null
  }
  return {
    name: form.name.trim() || address,
    address,
    display_name: form.displayName.trim(),
    provider: form.provider,
    username: form.username.trim() || null,
    secret: form.secret.trim() || null,
    imap: { host: form.imapHost.trim(), port: form.imapPort, security: form.imapSecurity },
    smtp: { host: form.smtpHost.trim(), port: form.smtpPort, security: form.smtpSecurity },
    signature: form.signature.trim() || null,
    ignore_senders: ignore,
  }
}

async function test(): Promise<void> {
  const body = payload()
  if (!body || testing.value) return
  testing.value = true
  result.value = null
  const { data, error } = await api.POST('/api/v1/mail/accounts/test', {
    body: { ...body, account_id: props.account?.id ?? null },
  })
  testing.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  result.value = data
}

async function save(): Promise<void> {
  const body = payload()
  if (!body || saving.value) return
  saving.value = true
  const { data, error } = props.account
    ? await api.PUT('/api/v1/mail/accounts/{account_id}', {
        params: { path: { account_id: props.account.id } },
        body,
      })
    : await api.POST('/api/v1/mail/accounts', { body })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(props.account ? '已保存' : '邮箱已接入，新邮件会进入客服排队')
  emit('saved', data)
  open.value = false
}
</script>

<template>
  <el-dialog
    v-model="open"
    :title="editing ? '修改邮箱' : '添加邮箱'"
    width="620px"
    destroy-on-close
    data-testid="mailbox-dialog"
  >
    <el-form label-width="96px" @submit.prevent>
      <el-form-item label="邮箱地址" required>
        <el-input
          v-model="form.address"
          placeholder="例如 service@163.com"
          maxlength="254"
          data-testid="mailbox-address-input"
        />
      </el-form-item>
      <el-form-item label="邮箱类型">
        <el-select
          v-model="form.provider"
          data-testid="mailbox-provider"
          @change="chooseProvider"
        >
          <el-option v-for="p in providers" :key="p.key" :label="p.name" :value="p.key" />
        </el-select>
      </el-form-item>
      <el-form-item :label="provider?.secret_label ?? '授权码'" :required="!editing">
        <el-input
          v-model="form.secret"
          type="password"
          show-password
          autocomplete="new-password"
          maxlength="256"
          :placeholder="editing ? '不修改请留空' : ''"
          data-testid="mailbox-secret"
        />
      </el-form-item>
      <el-alert
        v-if="provider?.help"
        type="info"
        :closable="false"
        class="help"
        data-testid="mailbox-help"
        :title="provider.help"
      />
      <el-form-item label="渠道名称">
        <el-input
          v-model="form.name"
          maxlength="64"
          placeholder="例如：售后邮箱"
          data-testid="mailbox-name"
          @input="touched.name = true"
        />
      </el-form-item>
      <el-form-item label="发件人名称">
        <el-input
          v-model="form.displayName"
          maxlength="64"
          placeholder="客户看到的发件人，例如：某某公司客服"
          data-testid="mailbox-display-name"
        />
      </el-form-item>
      <el-form-item label="签名">
        <el-input
          v-model="form.signature"
          type="textarea"
          :rows="3"
          maxlength="2000"
          placeholder="附在每封回复的末尾"
          data-testid="mailbox-signature"
        />
      </el-form-item>
      <el-form-item label="忽略发件人">
        <el-input
          v-model="form.ignore"
          type="textarea"
          :rows="2"
          placeholder="不导入这些发件人的邮件：一行一个，完整地址或 @域名"
          data-testid="mailbox-ignore"
        />
      </el-form-item>
      <el-form-item>
        <el-checkbox v-model="advanced" data-testid="mailbox-advanced">
          高级设置（服务器、端口、登录名）
        </el-checkbox>
      </el-form-item>
      <template v-if="advanced || isCustom">
        <el-form-item label="登录名">
          <el-input
            v-model="form.username"
            maxlength="254"
            placeholder="默认是邮箱地址"
            data-testid="mailbox-username"
          />
        </el-form-item>
        <el-form-item label="收信 IMAP" required>
          <div class="server">
            <el-input
              v-model="form.imapHost"
              placeholder="imap.example.com"
              data-testid="mailbox-imap-host"
            />
            <el-input-number
              v-model="form.imapPort"
              :min="1"
              :max="65535"
              :controls="false"
              class="port"
              data-testid="mailbox-imap-port"
            />
            <el-select v-model="form.imapSecurity" class="security">
              <el-option v-for="s in securities" :key="s" :label="SECURITY_LABEL[s]" :value="s" />
            </el-select>
          </div>
        </el-form-item>
        <el-form-item label="发信 SMTP" required>
          <div class="server">
            <el-input
              v-model="form.smtpHost"
              placeholder="smtp.example.com"
              data-testid="mailbox-smtp-host"
            />
            <el-input-number
              v-model="form.smtpPort"
              :min="1"
              :max="65535"
              :controls="false"
              class="port"
              data-testid="mailbox-smtp-port"
            />
            <el-select v-model="form.smtpSecurity" class="security">
              <el-option v-for="s in securities" :key="s" :label="SECURITY_LABEL[s]" :value="s" />
            </el-select>
          </div>
        </el-form-item>
      </template>
    </el-form>
    <div v-if="result" class="result" data-testid="mailbox-test-result">
      <div :class="result.imap_error ? 'bad' : 'good'">
        收信：{{ result.imap_error ?? `正常（收件箱 ${result.inbox ?? 0} 封邮件）` }}
      </div>
      <div :class="result.smtp_error ? 'bad' : 'good'">
        发信：{{ result.smtp_error ?? '正常' }}
      </div>
    </div>
    <template #footer>
      <el-button :loading="testing" data-testid="mailbox-test" @click="test">测试连接</el-button>
      <el-button type="primary" :loading="saving" data-testid="mailbox-save" @click="save">
        {{ editing ? '保存' : '测试并保存' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.help {
  margin: -6px 0 14px 96px;
  width: auto;
}

.server {
  display: flex;
  gap: 6px;
  width: 100%;
}

.port {
  width: 90px;
  flex-shrink: 0;
}

.security {
  width: 150px;
  flex-shrink: 0;
}

.result {
  margin-top: 4px;
  padding: 8px 12px;
  border-radius: 4px;
  font-size: 13px;
  line-height: 1.6;
  background: var(--el-fill-color-lighter);
}

.good {
  color: var(--el-color-success);
}

.bad {
  color: var(--el-color-danger);
}
</style>
