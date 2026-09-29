/** AI 接待与知识库页面用到的名称和小工具（与后端 app/modules/ai、app/modules/kb 一致）。 */
import type { Schemas } from '@edp/api-client'

export const KB_STATUS: Record<string, string> = {
  draft: '草稿',
  published: '已发布',
  archived: '已下架',
}

export const KB_STATUS_TAG: Record<string, 'info' | 'success' | 'warning'> = {
  draft: 'info',
  published: 'success',
  archived: 'warning',
}

export const KB_KIND: Record<string, string> = { faq: '问答', doc: '文档' }

export const KB_VISIBILITY: Record<string, string> = {
  public: '对客（AI 与坐席）',
  agent: '仅坐席',
  admin: '仅管理员',
}

export const KB_SOURCE: Record<string, string> = {
  manual: '手工录入',
  import: '批量导入',
  extracted: '对话提炼',
}

/** 软信号：命中的信号加权求和，达到阈值转人工（后端 decision.WEIGHTS）。 */
export const SIGNAL_LABEL: Record<string, string> = {
  low_relevance: '知识相关度低',
  low_confidence: 'AI 把握低',
  negative: '负面情绪',
  repeated: '重复提问',
  negation: '否定回答',
  too_many_turns: '轮数过多',
}

export const GUARD_LABEL: Record<string, string> = {
  empty: '回复为空',
  too_long: '回复过长',
  promise: '资料外的承诺',
  sensitive: '包含敏感词',
  bad_output: '模型输出格式错误',
}

/** 命中的软信号名称。 */
export function activeSignals(signals: Record<string, unknown>): string[] {
  return Object.keys(SIGNAL_LABEL)
    .filter((name) => signals[name] === true)
    .map((name) => SIGNAL_LABEL[name]!)
}

type EvalCase = Schemas['EvalCase']

/**
 * 评测样例的文本格式：每行一个问题，竖线后写期望——"转人工"，或回复中应包含的关键词（逗号或顿号分隔）。
 * 没有竖线表示期望 AI 直接回答、不检查关键词。空行和 # 开头的行忽略。
 */
export function parseEvalCases(text: string): { cases: EvalCase[]; errors: string[] } {
  const cases: EvalCase[] = []
  const errors: string[] = []
  text.split('\n').forEach((raw, index) => {
    const line = raw.trim()
    if (!line || line.startsWith('#')) return
    const [question = '', expectation = ''] = line.split(/[|｜]/, 2).map((part) => part.trim())
    if (!question) {
      errors.push(`第 ${index + 1} 行：缺少问题`)
      return
    }
    if (expectation === '转人工') {
      cases.push({ question, expect_handoff: true, expect_keywords: [] })
      return
    }
    const keywords = expectation
      .split(/[,，、]/)
      .map((k) => k.trim())
      .filter(Boolean)
    if (keywords.some((k) => k.length > 32)) {
      errors.push(`第 ${index + 1} 行：关键词不能超过 32 个字`)
      return
    }
    cases.push({ question, expect_handoff: false, expect_keywords: keywords.slice(0, 10) })
  })
  return { cases, errors }
}

export const EVAL_SAMPLE = [
  '# 每行一个问题；竖线后写期望：转人工，或回复里应包含的关键词（逗号分隔）',
  '快递几天能到 | 天',
  '我要投诉 | 转人工',
].join('\n')

/** FAQ 批量导入的 CSV 模板（后端 kb.service.parse_faq_csv）。 */
export const FAQ_CSV_TEMPLATE = [
  '标准问,答案,相似问,分类',
  '订单发货后多久能到？,一般 2 到 3 天送达，偏远地区 5 到 7 天。,快递几天能到|多久能收到货,物流',
].join('\n')
