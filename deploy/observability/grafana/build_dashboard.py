"""生成 edp-overview.json（Grafana 看板）：python3 build_dashboard.py edp-overview.json。"""
import json, sys

DS = {"type": "prometheus", "uid": "${datasource}"}
T = 'tenant=~"$tenant"'
panels = []
y = 0
pid = 0


def next_id():
    global pid
    pid += 1
    return pid


def row(title):
    global y
    panels.append({"type": "row", "title": title, "id": next_id(), "collapsed": False,
                   "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": []})
    y += 1


_x = 0


def panel(title, targets, unit="short", kind="timeseries", w=8, h=8, desc="", stack=False, thresholds=None, min0=True):
    global y, _x
    if _x + w > 24:
        _x = 0
        y += h
    defaults = {"unit": unit}
    if min0:
        defaults["min"] = 0
    if kind == "timeseries":
        defaults["custom"] = {"lineWidth": 2, "fillOpacity": 10 if not stack else 60,
                              "stacking": {"mode": "normal" if stack else "none"}}
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute", "steps": thresholds}
        defaults["color"] = {"mode": "thresholds"}
    p = {
        "type": kind, "title": title, "id": next_id(), "datasource": DS,
        "description": desc,
        "gridPos": {"h": h, "w": w, "x": _x, "y": y},
        "targets": [{"expr": f"{e} or vector(0)" if kind == "stat" else e, "legendFormat": l,
                     "refId": chr(65 + i), "datasource": DS}
                    for i, (e, l) in enumerate(targets)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
    }
    if kind == "timeseries":
        p["options"] = {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                        "tooltip": {"mode": "multi", "sort": "desc"}}
    elif kind == "stat":
        p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "value", "graphMode": "area", "textMode": "auto"}
    panels.append(p)
    _x += w


def end_row():
    global y, _x
    y += 8
    _x = 0


TENANT_CODE = ' * on (tenant) group_left (code) max by (tenant, code) (edp_tenant_info)'

row("概览")
panel("排队中的会话", [(f"sum(edp_queue_length{{{T}}})", "排队")], kind="stat", w=4, h=5,
      thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 10}, {"color": "red", "value": 50}])
panel("在线坐席", [(f"sum(edp_agents_online{{{T}}})", "在线")], kind="stat", w=4, h=5)
panel("AI 接待中", [(f"sum(edp_ai_sessions{{{T}}})", "AI")], kind="stat", w=4, h=5)
panel("人工接待中", [(f"sum(edp_human_sessions{{{T}}})", "人工")], kind="stat", w=4, h=5)
panel("事件积压", [("sum(edp_event_backlog)", "积压")], kind="stat", w=4, h=5,
      thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 100}, {"color": "red", "value": 1000}])
panel("死信", [("max(edp_dead_letter_size)", "死信")], kind="stat", w=4, h=5,
      thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}])
y += 5
_x = 0

row("排队与坐席")
panel("排队长度（按租户）", [(f"max by (tenant) (edp_queue_length{{{T}}})" + TENANT_CODE, "{{code}}")])
panel("排队最久的会话已等待", [(f"max by (tenant) (edp_queue_oldest_wait_seconds{{{T}}})" + TENANT_CODE, "{{code}}")], unit="s")
panel("排队等待时长（分配时）", [
    (f"histogram_quantile(0.5, sum by (le) (rate(edp_queue_wait_seconds_bucket{{{T}}}[$__rate_interval])))", "P50"),
    (f"histogram_quantile(0.95, sum by (le) (rate(edp_queue_wait_seconds_bucket{{{T}}}[$__rate_interval])))", "P95"),
], unit="s")
end_row()
panel("在线坐席（按租户）", [(f"max by (tenant) (edp_agents_online{{{T}}})" + TENANT_CODE, "{{code}}")])
panel("坐席负载（人工接待 / 接待上限）", [
    (f"sum by (tenant) (edp_human_sessions{{{T}}}) / clamp_min(sum by (tenant) (edp_agent_capacity{{{T}}}), 1)" + TENANT_CODE, "{{code}}")
], unit="percentunit")
panel("排队超时转留言", [(f"sum by (tenant) (increase(edp_queue_timeouts_total{{{T}}}[1h]))" + TENANT_CODE, "{{code}}")],
      desc="最近 1 小时")
end_row()

row("AI 接待")
panel("AI 解决率（只有 AI 接待就结束 / AI 接待的会话）", [
    (f"sum by (tenant) (increase(edp_sessions_closed_total{{served_by=\"ai\",{T}}}[1h])) / clamp_min(sum by (tenant) (increase(edp_sessions_closed_total{{served_by=\"ai\",{T}}}[1h])) + sum by (tenant) (increase(edp_ai_handoffs_total{{{T}}}[1h])), 1)" + TENANT_CODE, "{{code}}")
], unit="percentunit", desc="最近 1 小时")
panel("转人工原因", [(f"sum by (reason) (increase(edp_ai_handoffs_total{{{T}}}[1h]))", "{{reason}}")], stack=True,
      desc="最近 1 小时")
panel("结束的会话（按接待方式）", [(f"sum by (served_by) (increase(edp_sessions_closed_total{{{T}}}[1h]))", "{{served_by}}")],
      stack=True, desc="最近 1 小时")
end_row()

row("大模型")
panel("调用量（按结果）", [(f"sum by (status) (rate(edp_llm_calls_total{{{T}}}[$__rate_interval]))", "{{status}}")],
      unit="reqps", stack=True)
panel("错误率（按租户）", [
    (f"sum by (tenant) (rate(edp_llm_calls_total{{status!=\"ok\",{T}}}[$__rate_interval])) / clamp_min(sum by (tenant) (rate(edp_llm_calls_total{{{T}}}[$__rate_interval])), 0.001)" + TENANT_CODE, "{{code}}")
], unit="percentunit")
panel("延迟 P95（按场景）", [
    ("histogram_quantile(0.95, sum by (le, scene) (rate(edp_llm_call_seconds_bucket[$__rate_interval])))", "{{scene}}")
], unit="s")
end_row()
panel("费用（元 / 小时，按租户）", [
    (f"sum by (tenant) (increase(edp_llm_cost_fen_total{{{T}}}[1h])) / 100" + TENANT_CODE, "{{code}}")
], unit="currencyCNY")
panel("tokens（按场景）", [(f"sum by (scene, kind) (rate(edp_llm_tokens_total{{{T}}}[$__rate_interval]))", "{{scene}} {{kind}}")])
panel("调用量（按场景）", [(f"sum by (scene) (rate(edp_llm_calls_total{{{T}}}[$__rate_interval]))", "{{scene}}")], unit="reqps")
end_row()

row("消息链路")
panel("消息量（按租户）", [(f"sum by (tenant) (rate(edp_messages_total{{{T}}}[$__rate_interval]))" + TENANT_CODE, "{{code}}")],
      unit="reqps")
panel("回调延迟（消息发出到平台收到）", [
    ("histogram_quantile(0.5, sum by (le) (rate(edp_webhook_delay_seconds_bucket{source=\"openim\"}[$__rate_interval])))", "P50"),
    ("histogram_quantile(0.95, sum by (le) (rate(edp_webhook_delay_seconds_bucket{source=\"openim\"}[$__rate_interval])))", "P95"),
], unit="s")
panel("对账补录（回调丢失的消息）", [(f"sum by (tenant) (increase(edp_reconcile_recovered_total{{{T}}}[1h]))" + TENANT_CODE, "{{code}}")],
      desc="最近 1 小时")
end_row()
panel("事件积压（按分区）", [("sum by (partition) (edp_event_backlog)", "分区 {{partition}}")], stack=True)
panel("事件延迟（发布到开始处理）P95", [
    ("histogram_quantile(0.95, sum by (le, type) (rate(edp_event_delay_seconds_bucket[$__rate_interval])))", "{{type}}")
], unit="s")
panel("事件处理结果", [(f"sum by (type, outcome) (rate(edp_events_processed_total{{{T}}}[$__rate_interval]))", "{{type}} {{outcome}}")],
      unit="reqps")
end_row()
panel("IM 发件箱待执行", [(f"sum by (tenant) (edp_im_ops_pending{{{T}}})" + TENANT_CODE, "{{code}}")], stack=True)
panel("IM 发件箱最久未执行", [("max(edp_im_ops_oldest_pending_seconds)", "最久")], unit="s")
panel("IM 操作结果", [(f"sum by (op, outcome) (rate(edp_im_ops_total{{{T}}}[$__rate_interval]))", "{{op}} {{outcome}}")],
      unit="reqps")
end_row()

row("企业微信")
panel("接口错误（按错误码）", [("sum by (errcode) (increase(edp_wecom_api_errors_total[1h]))", "{{errcode}}")], stack=True,
      desc="最近 1 小时；-1 表示网络或服务端故障")
panel("接口错误（按接口）", [("sum by (api) (increase(edp_wecom_api_errors_total[1h]))", "{{api}}")], stack=True)
panel("授权已取消的企业", [(f"sum by (tenant) (edp_wecom_auth_invalid{{{T}}})" + TENANT_CODE, "{{code}}")])
end_row()

row("API")
panel("请求量（按状态）", [("sum by (status) (rate(edp_http_requests_total[$__rate_interval]))", "{{status}}")],
      unit="reqps", stack=True)
panel("延迟 P95（最慢的 10 个路由）", [
    ("topk(10, histogram_quantile(0.95, sum by (le, route) (rate(edp_http_request_duration_seconds_bucket[$__rate_interval]))))", "{{route}}")
], unit="s")
panel("请求量（按租户）", [(f"sum by (tenant) (rate(edp_tenant_http_requests_total{{{T}}}[$__rate_interval]))" + TENANT_CODE, "{{code}}")],
      unit="reqps")
end_row()
panel("被限流的请求", [(f"sum by (tenant, rule) (increase(edp_rate_limited_total{{{T}}}[1h]))" + TENANT_CODE, "{{code}} {{rule}}")],
      desc="最近 1 小时")
panel("调度任务失败", [("sum by (task) (increase(edp_job_runs_total{outcome=\"error\"}[1h]))", "{{task}}")],
      desc="最近 1 小时")
panel("调度任务距上次成功", [("time() - max by (task) (edp_job_last_success_timestamp_seconds)", "{{task}}")], unit="s")
end_row()

dashboard = {
    "title": "EDP 平台总览",
    "uid": "edp-overview",
    "description": "排队、坐席、AI、大模型、消息链路、企业微信、API 与调度任务（设计文档 §19.3）。",
    "tags": ["edp"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 1,
    "editable": True,
    "refresh": "30s",
    "time": {"from": "now-6h", "to": "now"},
    "annotations": {"list": []},
    "templating": {"list": [
        {"name": "datasource", "label": "数据源", "type": "datasource", "query": "prometheus",
         "current": {}, "hide": 0, "refresh": 1, "options": []},
        {"name": "tenant", "label": "租户", "type": "query", "datasource": DS,
         "query": {"query": "query_result(edp_tenant_info)", "refId": "tenants"},
         "definition": "query_result(edp_tenant_info)",
         "regex": '/code="(?<text>[^"]+)".*tenant="(?<value>[^"]+)"/',
         "multi": True, "includeAll": True, "allValue": ".*", "refresh": 2, "sort": 1,
         "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "hide": 0, "options": []},
    ]},
    "panels": panels,
}
json.dump(dashboard, open(sys.argv[1], "w"), ensure_ascii=False, indent=2)
print(len(panels), "panels")
