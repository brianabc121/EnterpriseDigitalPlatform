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
    "supervisor": "主管转人工",
    "product_not_found": "没有找到客户要的商品",
    "order_limit": "今天 AI 下单已达上限",
    "price_probe": "识别到套价，已用固定话术答复",
    "reply_blocked": "回复出现内部价格信息，已拦截",
    "purchase_intent": "客户有明确的购买意向",
}


def label(reason: str | None) -> str:
    return REASON_LABELS.get(reason or "", reason or "")
