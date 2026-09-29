"""转人工原因的中文名称：AI 判定、交接摘要、非工作时间的留言共用。"""

REASON_LABELS = {
    "visitor_request": "访客点击转人工",
    "customer_request": "客户要求人工",
    "sensitive": "敏感诉求",
    "vip": "VIP 客户",
    "model_request": "AI 判断需要人工",
    "score": "AI 把握不足",
    "guardrail": "回复未通过安全检查",
    "ai_unavailable": "AI 暂时不可用",
    "quota": "AI 额度已用完",
    "disabled": "AI 接待已关闭",
    "not_configured": "AI 接待未配置",
}


def label(reason: str | None) -> str:
    return REASON_LABELS.get(reason or "", reason or "")
