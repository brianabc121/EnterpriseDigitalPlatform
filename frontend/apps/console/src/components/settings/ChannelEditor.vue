<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { api, widgetBase } from '../../api'

type Channel = Schemas['ChannelOut']

const props = defineProps<{ channel: Channel | null; policies: Schemas['RoutingPolicyOut'][] }>()
const emit = defineEmits<{ saved: [channel: Channel]; close: [] }>()

const open = computed({
  get: () => props.channel !== null,
  set: (value) => {
    if (!value) emit('close')
  },
})

const form = reactive({
  name: '',
  status: 'active' as 'active' | 'disabled',
  routing_policy_id: null as string | null,
  title: '',
  welcome_message: '',
  privacy_notice: '',
  allowed_origins: [] as string[],
  kf_welcome: '',
  handoff_threshold: null as number | null,
  relevance_threshold: null as number | null,
  max_turns: null as number | null,
  kb_space_ids: [] as string[],
})
const spaces = ref<Schemas['KbSpaceOut'][]>([])

async function loadSpaces(): Promise<void> {
  const { data } = await api.GET('/api/v1/kb/spaces')
  spaces.value = data?.items ?? []
}

onMounted(loadSpaces)
const isWeb = computed(() => props.channel?.type === 'web')
const isKf = computed(() => props.channel?.type === 'wecom_kf')
/** 邮件渠道（§10.8）：不经过 AI 接待；邮箱的服务器、授权码在"设置 → 邮箱"里改。 */
const isEmail = computed(() => props.channel?.type === 'email')
const secret = ref<string | null>(null)
const showSecret = ref(false)
const saving = ref(false)

watch(
  () => props.channel,
  (channel, previous) => {
    if (!channel) return
    // 保存后父组件会传回新的渠道对象；只有换了渠道时才重新隐藏密钥。
    if (channel.id !== previous?.id) showSecret.value = false
    Object.assign(form, {
      name: channel.name,
      status: channel.status === 'disabled' ? 'disabled' : 'active',
      routing_policy_id: channel.routing_policy_id,
      title: channel.widget.title,
      welcome_message: channel.widget.welcome_message ?? '',
      privacy_notice: channel.widget.privacy_notice ?? '',
      allowed_origins: [...(channel.widget.allowed_origins ?? [])],
      kf_welcome: channel.kf?.welcome_message ?? '',
      handoff_threshold: channel.ai?.handoff_threshold ?? null,
      relevance_threshold: channel.ai?.relevance_threshold ?? null,
      max_turns: channel.ai?.max_turns ?? null,
      kb_space_ids: [...(channel.kb_space_ids ?? [])],
    })
    secret.value = channel.identity_secret
  },
  { immediate: true },
)

const snippet = computed(() =>
  props.channel
    // eslint-disable-next-line no-useless-escape -- SFC 的 <script> 里不能直接出现结束标签
    ? `<script src="${widgetBase}/embed.js" data-key="${props.channel.public_key}" async><\/script>`
    : '',
)

const identitySnippet = computed(
  () => `// 网站后端（示例为 Node.js）：为当前登录用户签名，不要把密钥下发到浏览器
const crypto = require('crypto')
const timestamp = Math.floor(Date.now() / 1000)
const signature = crypto
  .createHmac('sha256', process.env.EDP_IDENTITY_SECRET)
  .update(\`\${user.id}:\${user.name ?? ''}:\${timestamp}\`)
  .digest('hex')

// 页面里，在加载 embed.js 之前：
window.EDPWidgetConfig = {
  user: { external_id: String(user.id), name: user.name, timestamp, signature },
}`,
)

async function copy(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('浏览器不允许自动复制，请手动选择复制')
  }
}

async function save(): Promise<void> {
  if (!props.channel) return
  saving.value = true
  const { data, error } = await api.PATCH('/api/v1/channels/{channel_id}', {
    params: { path: { channel_id: props.channel.id } },
    body: {
      name: form.name,
      status: form.status,
      routing_policy_id: form.routing_policy_id,
      ai: {
        handoff_threshold: form.handoff_threshold,
        relevance_threshold: form.relevance_threshold,
        max_turns: form.max_turns,
      },
      kb_space_ids: form.kb_space_ids,
      ...(isWeb.value
        ? {
            widget: {
              title: form.title,
              welcome_message: form.welcome_message || null,
              privacy_notice: form.privacy_notice || null,
              allowed_origins: form.allowed_origins,
            },
          }
        : {}),
      ...(isKf.value ? { kf: { welcome_message: form.kf_welcome.trim() || null } } : {}),
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已保存')
  emit('saved', data)
}

async function rotate(): Promise<void> {
  if (!props.channel) return
  if (secret.value) {
    try {
      await ElMessageBox.confirm(
        '更换后旧密钥立即失效，网站后端需要同时换成新密钥，否则实名访客会无法接入。',
        '更换签名密钥',
        { confirmButtonText: '更换', cancelButtonText: '取消', type: 'warning' },
      )
    } catch {
      return
    }
  }
  const { data, error } = await api.POST('/api/v1/channels/{channel_id}/identity-secret', {
    params: { path: { channel_id: props.channel.id } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  secret.value = data.identity_secret
  showSecret.value = true
  ElMessage.success('已生成新的签名密钥')
  emit('saved', data)
}
</script>

<template>
  <el-drawer v-model="open" :title="`渠道设置 · ${channel?.name ?? ''}`" size="560px">
    <el-form v-if="channel" label-position="top" data-testid="channel-editor">
      <h4>基本信息</h4>
      <el-form-item label="名称">
        <el-input v-model="form.name" maxlength="64" data-testid="channel-name" />
      </el-form-item>
      <el-form-item label="状态">
        <el-radio-group v-model="form.status">
          <el-radio value="active">启用</el-radio>
          <el-radio value="disabled">{{
            isEmail ? '停用（不再收信，也不能回复）' : '停用（访客无法发起新对话）'
          }}</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="路由策略">
        <el-select v-model="form.routing_policy_id" clearable placeholder="租户默认策略">
          <el-option v-for="p in policies" :key="p.id" :label="p.name" :value="p.id" />
        </el-select>
      </el-form-item>

      <template v-if="isEmail">
        <h4>邮件</h4>
        <p class="hint" data-testid="email-channel-hint">
          客户的邮件不经过 AI 接待，直接交给客服；邮箱地址、授权码和签名在「设置 → 邮箱」里修改。
        </p>
      </template>
      <h4 v-else>AI 接待</h4>
      <p v-if="!isEmail" class="hint">
        不填表示使用"AI 接待"设置里的值。不同渠道的客户群体不同，可以单独调整。
      </p>
      <div v-if="!isEmail" class="ai-row">
        <el-form-item label="转人工灵敏度">
          <el-input-number
            v-model="form.handoff_threshold"
            :min="0.1"
            :max="2"
            :step="0.1"
            :precision="1"
            placeholder="默认"
            data-testid="channel-handoff"
          />
        </el-form-item>
        <el-form-item label="知识相关度阈值">
          <el-input-number
            v-model="form.relevance_threshold"
            :min="0"
            :max="1"
            :step="0.05"
            :precision="2"
            placeholder="默认"
            data-testid="channel-relevance"
          />
        </el-form-item>
        <el-form-item label="最多接待轮数">
          <el-input-number v-model="form.max_turns" :min="1" :max="50" placeholder="默认" />
        </el-form-item>
      </div>
      <el-form-item label="知识范围">
        <el-select
          v-model="form.kb_space_ids"
          multiple
          clearable
          placeholder="全部知识"
          data-testid="channel-spaces"
        >
          <el-option v-for="s in spaces" :key="s.id" :label="s.name" :value="s.id" />
        </el-select>
        <p class="hint">只用这些知识空间的知识回答这个渠道的客户（例如售前渠道只用产品知识）</p>
      </el-form-item>

      <template v-if="isKf">
        <h4>微信客服</h4>
        <el-form-item label="欢迎语">
          <el-input
            v-model="form.kf_welcome"
            type="textarea"
            :rows="3"
            maxlength="500"
            placeholder="客户进入会话时自动发送（不占 48 小时内 5 条的额度）；留空表示不发送"
            data-testid="kf-welcome-message"
          />
        </el-form-item>
        <el-button type="primary" :loading="saving" data-testid="save-channel" @click="save">
          保存
        </el-button>
      </template>
      <template v-else-if="!isWeb">
        <el-button type="primary" :loading="saving" data-testid="save-channel" @click="save">
          保存
        </el-button>
      </template>

      <template v-if="isWeb">
        <h4>聊天窗口</h4>
        <el-form-item label="窗口标题">
          <el-input v-model="form.title" maxlength="32" data-testid="widget-title-input" />
        </el-form-item>
        <el-form-item label="欢迎语">
          <el-input
            v-model="form.welcome_message"
            type="textarea"
            :rows="2"
            maxlength="500"
            placeholder="访客打开窗口时看到"
            data-testid="welcome-input"
          />
        </el-form-item>
        <el-form-item label="隐私提示">
          <el-input
            v-model="form.privacy_notice"
            type="textarea"
            :rows="2"
            maxlength="1000"
            placeholder="访客发送第一条消息前展示，例如对话内容的用途"
            data-testid="privacy-input"
          />
        </el-form-item>
        <el-form-item label="允许嵌入的网站">
          <el-input-tag
            v-model="form.allowed_origins"
            placeholder="输入 https://www.example.com 后回车；留空表示不限制"
            data-testid="origins-input"
          />
        </el-form-item>
        <el-button type="primary" :loading="saving" data-testid="save-channel" @click="save">
          保存
        </el-button>

        <h4>嵌入代码</h4>
        <p class="hint">
          把下面的代码放到网站页面的 &lt;/body&gt; 之前，页面右下角会出现"在线客服"按钮。
        </p>
        <pre class="code" data-testid="embed-snippet">{{ snippet }}</pre>
        <el-button size="small" @click="copy(snippet)">复制嵌入代码</el-button>

        <h4>实名访客</h4>
        <p class="hint">
          网站用户登录后，由网站后端用签名密钥为用户签名，客服即可看到用户在网站上的身份，
          同一用户换设备也会接续同一个对话。
        </p>
        <div v-if="secret" class="secret">
          <code data-testid="identity-secret">{{ showSecret ? secret : '•'.repeat(24) }}</code>
          <el-button link type="primary" @click="showSecret = !showSecret">
            {{ showSecret ? '隐藏' : '显示' }}
          </el-button>
          <el-button link type="primary" @click="copy(secret)">复制</el-button>
        </div>
        <el-button size="small" data-testid="rotate-secret" @click="rotate">
          {{ secret ? '更换签名密钥' : '启用实名访客（生成签名密钥）' }}
        </el-button>
        <template v-if="secret">
          <pre class="code">{{ identitySnippet }}</pre>
          <el-button size="small" @click="copy(identitySnippet)">复制示例代码</el-button>
        </template>
      </template>
    </el-form>
  </el-drawer>
</template>

<style scoped>
h4 {
  margin: 20px 0 8px;
  font-size: 14px;
}

h4:first-child {
  margin-top: 0;
}

.hint {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
}

.code {
  margin: 0 0 8px;
  padding: 10px 12px;
  background: var(--el-fill-color-light);
  border-radius: 4px;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}

.ai-row {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
}

.secret {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-bottom: 8px;
}

.secret code {
  font-size: 12px;
  word-break: break-all;
}
</style>
