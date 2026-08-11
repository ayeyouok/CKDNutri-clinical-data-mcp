"""a207-lis-mcp 纯逻辑层（M2 检验数据）。

无 fastmcp 依赖，tests/ 可直接 import 断言。
权限依据需求文档 §7 权限矩阵：M2 LIS 医生助手/风险预警可读全量，
家长助手受限读，营养师/患儿伙伴/调度 Agent 禁止直读。
写权依据 MX-3：upsert_lab_result 仅 doctor_assistant。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from pydantic import ValidationError

from ._policy import (
    LIS_CRITICAL_CHANNEL,
    LIS_READ_FULL,
    LIS_READ_LIMITED,
    LIS_WRITE_ALLOWED,
    get_caller,
)
from .errors import fail, forbidden, invalid, no_data, not_found, patient_context
from .models import LabResultIn, PatientQuery, TrendQuery, UpsertRequest
from .his import (
    _guard_access,
    _guard_guardian,
    get_diagnosis,
    get_nutrition_ceiling,
    get_patient_profile,
    list_patients,
    verify_guardian_binding,
)
from .reference import ANALYTES, UNIT_ALIASES, critical_hits
from .store import append_record, find_patient, known_patient_ids, merged_panels
from .views import build_trend, decorate_panel, parent_view_items

# 权限数据来自 a207_policy（Plan A 单一事实源）；本包不再维护第二份。
READ_FULL = LIS_READ_FULL
READ_LIMITED = LIS_READ_LIMITED
CRITICAL_CHANNEL = LIS_CRITICAL_CHANNEL
WRITE_ALLOWED = LIS_WRITE_ALLOWED

REEVALUATE_HINT = "a207-risk-rules-mcp.evaluate_risk_rules"
NOTIFY_HINT = "a207-notify-mcp.notify_critical_value"


# --- 工具 1：get_labs -------------------------------------------------------

def get_labs(patient_id: str, guardian_token: str | None = None) -> dict[str, Any]:
    """取患者全部检验记录（按报告日期升序）。

    caller=parent_assistant 返回受限视图：仅最近一次 K/P/Alb/Hb 的趋势方向与
    是否在儿童参考区间内，危急项标注，不含任何原始数值。
    caller=parent_assistant 必须经 guardian_token 绑定核验（F4），缺 token 即拒绝。
    """
    caller = get_caller()
    try:
        query = PatientQuery(patient_id=patient_id, caller=caller)
    except ValidationError as exc:
        return invalid(exc)

    # F2：通用读权统一走中枢 a207_policy.enforce_read（单一事实源）
    denied = _guard_access("get_labs")
    if denied:
        return denied
    # F4：家长受限视图必须经监护人令牌绑定核验；缺 token / 不匹配即拒绝，不给降级视图
    denied = _guard_guardian(caller, patient_id, guardian_token, "get_labs")
    if denied:
        return denied

    patient = find_patient(query.patient_id)
    if patient is None:
        return not_found(query.patient_id)

    panels = merged_panels(query.patient_id)
    age, sex = float(patient["age_years"]), patient["sex"]

    if caller in READ_LIMITED:
        if not panels:
            return no_data(query.patient_id)
        latest = panels[-1]
        previous = panels[-2] if len(panels) >= 2 else None
        items = parent_view_items(latest, previous, age, sex)
        return {
            "ok": True,
            "data": {
                "patient_id": query.patient_id,
                "data_scope": "limited_parent",
                "report_date": latest["report_date"],
                "items": items,
                "has_critical": any(item["critical"] for item in items),
                "note": "受限视图不含化验原始数值。需要具体数值请联系主管医生。",
            },
        }

    return {
        "ok": True,
        "data": {
            "patient_id": query.patient_id,
            "data_scope": "full",
            "context": patient_context(patient),
            "panel_count": len(panels),
            "panels": [decorate_panel(p, age, sex) for p in panels],
        },
    }


# --- 工具 2：get_critical_values --------------------------------------------

def get_critical_values(
    patient_id: str
) -> dict[str, Any]:
    """危急值独立通道：扫描最近一次采样，命中项直连通知链路。"""
    caller = get_caller()
    try:
        query = PatientQuery(patient_id=patient_id, caller=caller)
    except ValidationError as exc:
        return invalid(exc)

    if caller not in CRITICAL_CHANNEL:
        return forbidden(caller, "get_critical_values")

    patient = find_patient(query.patient_id)
    if patient is None:
        return not_found(query.patient_id)

    panels = merged_panels(query.patient_id)
    if not panels:
        return no_data(query.patient_id)

    latest = panels[-1]
    hits = critical_hits(latest["values"])
    history = [
        {"report_date": p["report_date"], "hit_count": len(critical_hits(p["values"]))}
        for p in panels[:-1]
        if critical_hits(p["values"])
    ]
    return {
        "ok": True,
        "data": {
            "patient_id": query.patient_id,
            "report_date": latest["report_date"],
            "sample_id": latest["sample_id"],
            "scan_scope": "latest_panel",
            "critical_count": len(hits),
            "items": hits,
            "prior_panels_with_critical": history,
            "next_action": NOTIFY_HINT if hits else None,
        },
    }


# --- 工具 3：get_lab_trend --------------------------------------------------

def _within_window(panels: list[dict[str, Any]], window_days: int) -> list[dict[str, Any]]:
    if not panels:
        return panels
    end = date.fromisoformat(panels[-1]["report_date"])
    return [
        p
        for p in panels
        if (end - date.fromisoformat(p["report_date"])).days <= window_days
    ]


def get_lab_trend(
    patient_id: str,
    analyte: str,
    window_days: int | None = None,
) -> dict[str, Any]:
    """单指标时间序列 + 环比变化率 + 每 30 天斜率。家长助手无全量趋势权限。"""
    caller = get_caller()
    try:
        query = TrendQuery(
            patient_id=patient_id,
            caller=caller,
            analyte=analyte,
            window_days=window_days,
        )
    except ValidationError as exc:
        return invalid(exc)

    if caller not in READ_FULL:
        return forbidden(caller, "get_lab_trend")
    if query.analyte not in ANALYTES:
        return fail(
            "INVALID_ARGUMENT",
            f"analyte={query.analyte} 未知，可选：{', '.join(sorted(ANALYTES))}",
        )

    patient = find_patient(query.patient_id)
    if patient is None:
        return not_found(query.patient_id)

    panels = merged_panels(query.patient_id)
    if query.window_days is not None:
        panels = _within_window(panels, query.window_days)

    trend = build_trend(
        panels, query.analyte, float(patient["age_years"]), patient["sex"]
    )
    trend["patient_id"] = query.patient_id
    trend["window_days"] = query.window_days
    return {"ok": True, "data": trend}


# --- 工具 4：upsert_lab_result（写权仅 doctor_assistant）--------------------

def _normalize(lab: LabResultIn) -> tuple[dict[str, float], list[str]]:
    """把别名口径换算成契约单位，返回 (契约值, 换算说明)。"""
    raw = lab.model_dump(exclude_none=True)
    values: dict[str, float] = {k: float(v) for k, v in raw.items() if k in ANALYTES}
    conversions: list[str] = []
    for alias, (target, factor) in UNIT_ALIASES.items():
        if alias not in raw:
            continue
        converted = round(float(raw[alias]) * factor, 4)
        if target in values:
            conversions.append(f"{alias} 与 {target} 同时给出，以契约字段 {target} 为准")
            continue
        values[target] = converted
        conversions.append(f"{alias}={raw[alias]} 归一化为 {target}={converted}")
    return values, conversions


def upsert_lab_result(
    patient_id: str,
    lab: dict[str, Any] | None = None,
    write_mode: bool = True,
) -> dict[str, Any]:
    """新增一条采样。写权仅 doctor_assistant（MX-3），越权 403。

    write_mode=False 时只做校验与影响面预演，不落盘（回滚模式）。
    成功返回 recommend_reevaluate=true，提示编排层强制触发 M8 重评。
    """
    caller = get_caller()
    # F2：写权统一走中枢 a207_policy.enforce_write（含 MX-3 写工具白名单），较本地 WRITE_ALLOWED 更严
    denied = _guard_access("upsert_lab_result", write=True)
    if denied:
        return denied

    try:
        request = UpsertRequest(
            patient_id=patient_id, caller=caller, lab=lab, write_mode=write_mode
        )
    except ValidationError as exc:
        return invalid(exc)

    patient = find_patient(request.patient_id)
    if patient is None:
        return not_found(request.patient_id)

    values, conversions = _normalize(request.lab)
    if not values:
        return fail("INVALID_ARGUMENT", "lab 中未提供任何可识别的检验指标")

    existing = len(merged_panels(request.patient_id))
    sample_id = request.lab.sample_id or f"{request.patient_id}-U{existing + 1:02d}"
    record = {
        "patient_id": request.patient_id,
        "sample_id": sample_id,
        "report_date": request.lab.report_date.isoformat(),
        "specimen": request.lab.specimen or "静脉血",
        "values": values,
        "recorded_by": caller,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    store_size = append_record(record) if request.write_mode else None
    hits = critical_hits(values)
    return {
        "ok": True,
        "recommend_reevaluate": True,
        "data": {
            "patient_id": request.patient_id,
            "sample_id": sample_id,
            "report_date": record["report_date"],
            "persisted": bool(request.write_mode),
            "write_mode": request.write_mode,
            "store_record_count": store_size,
            "analytes_written": sorted(values),
            "unit_conversions": conversions,
            "critical_count": len(hits),
            "critical_items": hits,
            "next_action": REEVALUATE_HINT,
            "notify_action": NOTIFY_HINT if hits else None,
        },
    }


def list_known_patients() -> dict[str, Any]:
    """列出本 LIS 数据集覆盖的 patient_id（供跨包联调核对）。

    OD-012：受 M2 读权限矩阵约束（doctor/risk_warning 全量、parent 受限视图，其余角色禁止）；
    此前该 core 函数既无 caller 也无闸门，直接调用可绕过 server 层 gate。
    """
    caller = get_caller()
    denied = _guard_access("list_known_patients")
    if denied:
        return denied
    ids = known_patient_ids()
    return {"ok": True, "data": {"count": len(ids), "patient_ids": ids}}
