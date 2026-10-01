<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { TextWithLinks } from '@edp/ui'
import { ElMessage } from 'element-plus'
import { computed, ref } from 'vue'

import { api } from '../../api'
import { showAddress, type EmailView, type WorkbenchMessage } from '../../workbench/messages'

/**
 * 一封邮件（设计文档 §10.8）：主题、发件人、收件人、新写的正文，引用的历史内容折叠起来。
 * "查看原邮件"在沙箱 iframe 里显示原样的 HTML（不执行脚本，不加载外部图片）。
 */
const props = defineProps<{ message: WorkbenchMessage; email: EmailView; replyable?: boolean }>()
const emit = defineEmits<{ reply: [message: WorkbenchMessage] }>()

const showQuoted = ref(false)
const originalOpen = ref(false)
const original = ref<Schemas['MailOriginalOut'] | null>(null)
const loading = ref(false)

const inbound = computed(() => props.message.senderType === 'customer')
const recipients = computed(() => props.email.to.map(showAddress).join('、'))
const copied = computed(() => props.email.cc.map(showAddress).join('、'))

async function openOriginal(): Promise<void> {
  if (!props.message.id || loading.value) return
  loading.value = true
  const { data, error } = await api.GET('/api/v1/mail/messages/{message_id}/original', {
    params: { path: { message_id: props.message.id } },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  original.value = data
  originalOpen.value = true
}
</script>

<template>
  <div class="email" data-testid="email-message">
    <div class="subject" data-testid="email-subject">{{ email.subject || '（无主题）' }}</div>
    <div class="meta">
      <div v-if="email.from" data-testid="email-from">发件人：{{ showAddress(email.from) }}</div>
      <div v-if="recipients">收件人：{{ recipients }}</div>
      <div v-if="copied">抄送：{{ copied }}</div>
    </div>
    <div class="text" data-testid="email-text"><TextWithLinks :text="email.text" /></div>
    <div v-if="email.quoted" class="quoted">
      <el-button link size="small" data-testid="email-quoted-toggle" @click="showQuoted = !showQuoted">
        {{ showQuoted ? '收起引用的内容' : '显示引用的内容' }}
      </el-button>
      <div v-if="showQuoted" class="quoted-text" data-testid="email-quoted">{{ email.quoted }}</div>
    </div>
    <div class="actions">
      <span v-if="email.attachments" class="hint">附件 {{ email.attachments }} 个，见下方</span>
      <el-button
        v-if="inbound && message.id"
        link
        size="small"
        :loading="loading"
        data-testid="email-original-button"
        @click="openOriginal"
      >
        查看原邮件
      </el-button>
      <a
        v-if="email.url"
        :href="email.url"
        target="_blank"
        rel="noopener"
        class="download"
        data-testid="email-download"
      >
        下载 .eml
      </a>
      <el-button
        v-if="replyable && inbound && message.id"
        link
        type="primary"
        size="small"
        data-testid="email-reply"
        @click="emit('reply', message)"
      >
        回复这封
      </el-button>
    </div>
    <el-dialog
      v-model="originalOpen"
      :title="original?.subject || '原邮件'"
      width="820px"
      append-to-body
      destroy-on-close
    >
      <iframe
        v-if="original"
        :srcdoc="original.html"
        sandbox="allow-popups allow-popups-to-escape-sandbox"
        referrerpolicy="no-referrer"
        class="frame"
        title="原邮件"
        data-testid="email-original"
      />
      <p class="note">为保护隐私和安全，原邮件里的外部图片不会加载，脚本不会执行。</p>
    </el-dialog>
  </div>
</template>

<style scoped>
.email {
  min-width: 240px;
  white-space: normal;
}

.subject {
  font-weight: 600;
  margin-bottom: 2px;
}

.meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  word-break: break-all;
}

.text {
  margin-top: 6px;
  white-space: pre-wrap;
}

.quoted {
  margin-top: 4px;
}

.quoted-text {
  margin-top: 2px;
  padding-left: 8px;
  border-left: 2px solid var(--el-border-color);
  font-size: 12px;
  white-space: pre-wrap;
  color: var(--el-text-color-secondary);
}

.actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-top: 6px;
  font-size: 12px;
}

.actions .el-button + .el-button {
  margin-left: 0;
}

.hint {
  color: var(--el-text-color-secondary);
}

.download {
  color: var(--el-color-primary);
  text-decoration: none;
}

.frame {
  width: 100%;
  height: 60vh;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  background: #fff;
}

.note {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
