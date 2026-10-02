<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { api } from '../api'

type Provider = Schemas['LlmProviderOut']
type Protocol = Provider['protocol']

/** 判断模型（TypeSafe Jev，设计文档 §32）只能用于"意图判断"场景。 */
const JUDGE_SCENES = ['intent']
const JUDGE_DEFAULTS = { baseUrl: 'https://api.typesafe.ai/v1', chatModel: 'jev-latest' }

const providers = ref<Provider[]>([])
const routes = ref<Schemas['LlmRoutesOut'] | null>(null)
const routeForm = reactive<Record<string, string>>({})
const loading = ref(false)
const saving = ref(false)
const dialogOpen = ref(false)
const editing = ref<Provider | null>(null)
const results = reactive<Record<string, string>>({})

function emptyForm() {
  return {
    protocol: 'openai' as Protocol,
    name: '',
    baseUrl: '',
    apiKey: '',
    chatModel: '',
    fastModel: '',
    embedModel: '',
    embedDim: 1024,
    sendDimensions: false,
    rerankModel: '',
    priceInput: 0,
    priceOutput: 0,
    tools: false,
    jsonSchema: false,
    contextTokens: 0,
    batch: false,
    isDefault: false,
    enabled: true,
  }
}
const form = reactive(emptyForm())
const isJudge = computed(() => form.protocol === 'typesafe')

/** 新建时选判断模型：填上官方接口地址、模型名和价格（输入每千 tokens 约 0.03 分，输出不计费）。 */
function changeProtocol(value: string | number | boolean | undefined): void {
  if (value !== 'openai' && value !== 'typesafe') return
  form.protocol = value
  if (value !== 'typesafe') return
  if (!form.baseUrl) form.baseUrl = JUDGE_DEFAULTS.baseUrl
  if (!form.chatModel) form.chatModel = JUDGE_DEFAULTS.chatModel
  if (!form.priceInput) form.priceInput = 0.03
  form.priceOutput = 0
  form.isDefault = false
}

/** 按场景路由的可选供应商：判断模型只出现在"意图判断"里。 */
function routeOptions(scene: string): Provider[] {
  return providers.value.filter((p) => p.protocol !== 'typesafe' || JUDGE_SCENES.includes(scene))
}

async function load(): Promise<void> {
  loading.value = true
  const [list, routing] = await Promise.all([
    api.GET('/platform/v1/llm-providers'),
    api.GET('/platform/v1/settings/llm-routes'),
  ])
  loading.value = false
  if (!list.data) {
    ElMessage.error(errorMessage(list.error))
    return
  }
  providers.value = list.data.items
  routes.value = routing.data ?? null
  for (const scene of Object.keys(routing.data?.scenes ?? {})) {
    routeForm[scene] = routing.data?.routes[scene] ?? ''
  }
}

function openCreate(): void {
  editing.value = null
  Object.assign(form, emptyForm())
  dialogOpen.value = true
}

function openEdit(p: Provider): void {
  editing.value = p
  Object.assign(form, {
    protocol: p.protocol,
    name: p.name,
    baseUrl: p.base_url,
    apiKey: '',
    chatModel: p.chat_model,
    fastModel: p.fast_model,
    embedModel: p.embed_model,
    embedDim: p.embed_dim,
    sendDimensions: p.send_dimensions,
    rerankModel: p.rerank_model,
    priceInput: p.prices.input,
    priceOutput: p.prices.output,
    tools: p.capabilities.tools,
    jsonSchema: p.capabilities.json_schema,
    contextTokens: p.capabilities.context_tokens,
    batch: p.capabilities.batch,
    isDefault: p.is_default,
    enabled: p.enabled,
  })
  dialogOpen.value = true
}

async function save(): Promise<void> {
  saving.value = true
  const body = {
    name: form.name.trim(),
    base_url: form.baseUrl.trim(),
    chat_model: form.chatModel.trim(),
    fast_model: form.fastModel.trim(),
    embed_model: form.embedModel.trim(),
    embed_dim: form.embedDim,
    send_dimensions: form.sendDimensions,
    rerank_model: form.rerankModel.trim(),
    prices: { input: form.priceInput, output: form.priceOutput },
    capabilities: {
      tools: form.tools,
      json_schema: form.jsonSchema,
      context_tokens: form.contextTokens,
      batch: form.batch,
    },
    is_default: form.isDefault,
    enabled: form.enabled,
  }
  // 接口类型创建后不能修改。
  const result = editing.value
    ? await api.PATCH('/platform/v1/llm-providers/{provider_id}', {
        params: { path: { provider_id: editing.value.id } },
        body: { ...body, api_key: form.apiKey ? form.apiKey : undefined },
      })
    : await api.POST('/platform/v1/llm-providers', {
        body: { ...body, protocol: form.protocol, api_key: form.apiKey },
      })
  saving.value = false
  if (!result.data) {
    ElMessage.error(errorMessage(result.error))
    return
  }
  ElMessage.success('已保存，几秒内所有服务生效')
  dialogOpen.value = false
  await load()
}

async function test(p: Provider): Promise<void> {
  results[p.id] = '检查中…'
  const { data, error } = await api.POST('/platform/v1/llm-providers/{provider_id}/test', {
    params: { path: { provider_id: p.id } },
  })
  if (!data) {
    results[p.id] = errorMessage(error)
    return
  }
  const kind = p.protocol === 'typesafe' ? '判断' : '对话'
  const chat = data.chat.ok
    ? `${kind}正常（${data.chat.latency_ms} ms${data.chat.model ? `，${data.chat.model}` : ''}）`
    : `${kind}失败：${data.chat.error}`
  const embed = data.embed
    ? data.embed.ok
      ? `，向量正常（${data.embed.latency_ms} ms）`
      : `，向量失败：${data.embed.error}`
    : ''
  results[p.id] = chat + embed
}

async function remove(p: Provider): Promise<void> {
  const confirmed = await ElMessageBox.confirm(
    `删除「${p.name}」：指定了它的租户改用默认供应商，按场景路由里引用它的场景一并去掉。`,
    '删除供应商',
    { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
  ).catch(() => false)
  if (!confirmed) return
  const { error } = await api.DELETE('/platform/v1/llm-providers/{provider_id}', {
    params: { path: { provider_id: p.id } },
  })
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  await load()
}

async function saveRoutes(): Promise<void> {
  const body: Record<string, string> = {}
  for (const [scene, id] of Object.entries(routeForm)) if (id) body[scene] = id
  const { data, error } = await api.PUT('/platform/v1/settings/llm-routes', {
    body: { routes: body },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  routes.value = data
  ElMessage.success('已保存')
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-header">
      <h2>模型供应商</h2>
      <el-button type="primary" data-testid="provider-create" @click="openCreate">添加供应商</el-button>
    </div>
    <p class="sub">
      OpenAI 兼容接口（DeepSeek、通义千问、智谱、豆包、Kimi、自部署 vLLM 等）。默认供应商用于没有单独指定的租户；
      没有配置任何供应商时使用环境变量里的配置。更换向量模型后需要执行 kb-reindex 重建知识库向量。
    </p>
    <el-table v-loading="loading" :data="providers" data-testid="provider-table" empty-text="暂无供应商">
      <el-table-column label="名称" min-width="140">
        <template #default="{ row }">
          <strong>{{ row.name }}</strong>
          <el-tag disable-transitions v-if="row.is_default" size="small" type="success" class="tag">默认</el-tag>
          <el-tag
            v-if="row.protocol === 'typesafe'"
            disable-transitions
            size="small"
            type="warning"
            class="tag"
            data-testid="provider-judge-tag"
            >判断模型</el-tag
          >
          <el-tag disable-transitions v-if="!row.enabled" size="small" type="info" class="tag">停用</el-tag>
          <div class="sub">{{ row.base_url }}</div>
        </template>
      </el-table-column>
      <el-table-column label="模型" min-width="200">
        <template #default="{ row }">
          <div v-if="row.protocol === 'typesafe'" class="sub">判断：{{ row.chat_model }}（只用于意图判断）</div>
          <div v-else class="sub">对话：{{ row.chat_model }}</div>
          <div v-if="row.fast_model" class="sub">轻量：{{ row.fast_model }}</div>
          <div v-if="row.embed_model" class="sub">向量：{{ row.embed_model }}（{{ row.embed_dim }} 维）</div>
          <div v-if="row.rerank_model" class="sub">重排序：{{ row.rerank_model }}</div>
        </template>
      </el-table-column>
      <el-table-column label="能力" min-width="150">
        <template #default="{ row }">
          <div class="caps" data-testid="provider-caps">
            <el-tag v-if="row.capabilities.tools" size="small" disable-transitions>工具调用</el-tag>
            <el-tag v-if="row.capabilities.json_schema" size="small" disable-transitions>结构化输出</el-tag>
            <el-tag v-if="row.capabilities.batch" size="small" disable-transitions>批量</el-tag>
            <el-tag v-if="row.rerank_model" size="small" disable-transitions>重排序</el-tag>
          </div>
          <div v-if="row.capabilities.context_tokens" class="sub">
            上下文 {{ Math.round(row.capabilities.context_tokens / 1000) }}K
          </div>
          <div class="sub">¥{{ (row.prices.input / 100).toFixed(4) }} / ¥{{ (row.prices.output / 100).toFixed(4) }} 每千 tokens</div>
        </template>
      </el-table-column>
      <el-table-column label="密钥" width="110">
        <template #default="{ row }">{{ row.api_key_set ? `****${row.api_key_hint}` : '未设置' }}</template>
      </el-table-column>
      <el-table-column label="指定租户" width="90" prop="tenants" />
      <el-table-column label="操作" min-width="260">
        <template #default="{ row }">
          <el-button link type="primary" data-testid="provider-test" @click="test(row)">检查连通</el-button>
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" @click="remove(row)">删除</el-button>
          <div v-if="results[row.id]" class="sub" data-testid="provider-test-result">{{ results[row.id] }}</div>
        </template>
      </el-table-column>
    </el-table>

    <h3>按场景路由</h3>
    <p class="sub">
      没有指定的场景用默认供应商；给租户单独指定了供应商时以租户的为准。意图判断可以选判断模型（Jev），也可以选
      OpenAI 兼容的供应商（用它的轻量模型）；没有指定时用环境变量配置的判断模型，都没有时不判断。
    </p>
    <el-form v-if="routes" label-width="150px" class="routes">
      <el-form-item v-for="(label, scene) in routes.scenes" :key="scene" :label="label">
        <el-select
          v-model="routeForm[scene]"
          :placeholder="scene === 'intent' ? '环境变量的判断模型（没有时不判断）' : '默认供应商'"
          clearable
          :data-testid="`route-${scene}`"
        >
          <el-option v-for="p in routeOptions(String(scene))" :key="p.id" :label="p.name" :value="p.id" />
        </el-select>
      </el-form-item>
      <el-form-item>
        <el-button type="primary" @click="saveRoutes">保存路由</el-button>
      </el-form-item>
    </el-form>

    <el-dialog v-model="dialogOpen" :title="editing ? `编辑 · ${editing.name}` : '添加供应商'" width="600px">
      <el-form label-width="120px">
        <el-form-item label="接口类型">
          <el-radio-group
            :model-value="form.protocol"
            :disabled="!!editing"
            data-testid="provider-protocol"
            @update:model-value="changeProtocol"
          >
            <el-radio-button value="openai">OpenAI 兼容</el-radio-button>
            <el-radio-button value="typesafe" data-testid="provider-protocol-typesafe">判断模型（Jev）</el-radio-button>
          </el-radio-group>
          <div class="sub hint">
            <template v-if="isJudge">
              TypeSafe 的判断模型只回答事先定义的问题、给出概率，按输入计费；只能用于"意图判断"，创建后不能改类型。
            </template>
            <template v-else>对话、向量、重排序（DeepSeek、通义千问、智谱、豆包、Kimi、自部署 vLLM 等）。</template>
          </div>
        </el-form-item>
        <el-form-item label="名称" required><el-input v-model="form.name" data-testid="provider-name" /></el-form-item>
        <el-form-item label="接口地址" required>
          <el-input
            v-model="form.baseUrl"
            :placeholder="isJudge ? JUDGE_DEFAULTS.baseUrl : 'https://api.deepseek.com/v1'"
            data-testid="provider-url"
          />
        </el-form-item>
        <el-form-item label="API Key">
          <el-input
            v-model="form.apiKey"
            type="password"
            show-password
            :placeholder="editing ? '不填表示不修改' : ''"
            data-testid="provider-key"
          />
        </el-form-item>
        <el-form-item :label="isJudge ? '判断模型' : '对话模型'" required>
          <el-input
            v-model="form.chatModel"
            :placeholder="isJudge ? 'jev-latest，建议固定版本如 jev-1.13.0' : ''"
            data-testid="provider-chat-model"
          />
        </el-form-item>
        <template v-if="!isJudge">
        <el-form-item label="轻量模型"><el-input v-model="form.fastModel" placeholder="摘要、分类等，可不填" /></el-form-item>
        <el-form-item label="向量模型">
          <el-input v-model="form.embedModel" placeholder="默认供应商的向量模型用于知识库检索，可不填" />
        </el-form-item>
        <el-form-item v-if="form.embedModel" label="向量维度">
          <el-input-number v-model="form.embedDim" :min="1" :max="8192" />
          <el-checkbox v-model="form.sendDimensions" class="gap">请求时传 dimensions</el-checkbox>
        </el-form-item>
        <el-form-item label="重排序模型">
          <el-input v-model="form.rerankModel" placeholder="检索结果重排序（/rerank），可不填" data-testid="provider-rerank" />
        </el-form-item>
        <el-form-item label="能力">
          <el-checkbox v-model="form.tools" data-testid="provider-cap-tools">工具调用</el-checkbox>
          <el-checkbox v-model="form.jsonSchema">结构化输出</el-checkbox>
          <el-checkbox v-model="form.batch">批量接口</el-checkbox>
        </el-form-item>
        <el-form-item label="上下文长度">
          <el-input-number v-model="form.contextTokens" :min="0" :step="1000" />
          <span class="sub gap">tokens，0 表示未知</span>
        </el-form-item>
        </template>
        <el-form-item label="价格（分/千 tokens）">
          输入
          <el-input-number
            v-model="form.priceInput"
            :min="0"
            :precision="3"
            :step="0.1"
            size="small"
            data-testid="provider-price-input"
          />
          输出
          <el-input-number
            v-model="form.priceOutput"
            :min="0"
            :precision="3"
            :step="0.1"
            size="small"
            data-testid="provider-price-output"
          />
        </el-form-item>
        <el-form-item v-if="!isJudge" label="设为默认"><el-switch v-model="form.isDefault" /></el-form-item>
        <el-form-item label="启用"><el-switch v-model="form.enabled" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogOpen = false">取消</el-button>
        <el-button type="primary" :loading="saving" data-testid="provider-save" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.page-header h2 {
  margin: 0;
  font-size: 18px;
}

h3 {
  margin: 24px 0 8px;
  font-size: 15px;
}

.tag {
  margin-left: 6px;
}

.gap {
  margin-left: 12px;
}

.routes {
  max-width: 560px;
}

.caps {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.hint {
  width: 100%;
  line-height: 1.5;
}
</style>
