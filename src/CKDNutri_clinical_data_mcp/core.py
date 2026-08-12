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

from a207_policy import (
    LIS_CRITICAL_CHANNEL,
    LIS_READ_FULL,
    LIS_READ_LIMITED,
    get_caller,
)
from .errors import fail, forbidden, invalid, no_data, not_found, patient_context
from .models import LabResultIn, PatientQuery, TrendQuery, UpsertRequest
from .his import (
    COHORT_CALLERS,
    _guard_access,
    _guard_guardian,
)
from .reference import ANALYTES, UNIT_ALIASES, critical_hits
from .store import append_record, find_patient, known_patient_ids, merged_panels
from .views import build_trend, decorate_panel, parent_trend_direction, parent_view_items

# 权限数据来自 a207_policy（Plan A 单一事实源）；本包不再维护第二份。
READ_FULL = LIS_READ_FULL
READ_LIMITED = LIS_READ_LIMITED
CRITICAL_CHANNEL = LIS_CRITICAL_CHANNEL

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
        query = PatientQuery(patient_id=patient_id)
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
    patient_id: str,
    guardian_token: str | None = None,
) -> dict[str, Any]:
    """危急值独立通道：扫描最近一次采样，命中项直连通知链路。

    BUG-05 修复（2026-08-12 需求对齐）：需求 P1 工具表 get_critical_values 家庭=✔（有/无）。
    家长助手可查"是否存在危急值"（受限视图，不含任何数值/明细），但必须携带
    guardian_token 完成患儿绑定核验；临床/风险角色返回全量命中明细。
    """
    caller = get_caller()
    try:
        query = PatientQuery(patient_id=patient_id)
    except ValidationError as exc:
        return invalid(exc)

    # 读权统一走中枢 a207_policy.enforce_read（单一事实源），覆盖 MCP 粒度
    denied = _guard_access("get_critical_values")
    if denied:
        return denied
    # 家长受限视图必须经监护人令牌绑定核验（F4，同 get_labs）
    denied = _guard_guardian(caller, query.patient_id, guardian_token, "get_critical_values")
    if denied:
        return denied

    patient = find_patient(query.patient_id)
    if patient is None:
        return not_found(query.patient_id)

    panels = merged_panels(query.patient_id)
    if not panels:
        return no_data(query.patient_id)

    latest = panels[-1]
    hits = critical_hits(latest["values"])
    # BUG-05：家长仅可见"有/无"受限视图（需求 P1 家庭=✔ 有/无），不落 CRITICAL_CHANNEL
    if caller not in CRITICAL_CHANNEL:
        if caller != "parent_assistant":
            return forbidden(caller, "get_critical_values")
        return {
            "ok": True,
            "data": {
                "patient_id": query.patient_id,
                "data_scope": "limited_parent",
                "report_date": latest["report_date"],
                "has_critical": bool(hits),
                "note": "受限视图仅提示是否存在危急值（有/无），具体项目请咨询主管医生。",
            },
        }
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
    """过滤出距最新采样不超过 window_days 天的面板。

    时间口径约定（2026-08-12 明确）：
    - report_date 为**自然日（日历日）**，YYYY-MM-DD 纯日期、**无时区语义**——
      models.LabResultIn 强制 date 类型，date.fromisoformat 不涉及时区解析；
      纯日期不含时分秒，不存在时区偏移/跨日边界不一致问题。
    - 窗口终点 = **最新面板日期**（以数据时间线为基准），而非"今天"；
      如需"相对今天"的窗口，由调用方先行过滤后再传入。
    """
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
    guardian_token: str | None = None,
) -> dict[str, Any]:
    """单指标时间序列 + 环比变化率 + 每 30 天斜率。

    BUG-06 修复（2026-08-12 需求对齐）：需求 P1 工具表 get_lab_trend 家庭=✔（仅方向）。
    家长助手可查指定指标的**趋势方向**（受限视图，不含任何数值），但必须携带
    guardian_token 完成患儿绑定核验；临床/风险角色返回全量趋势。
    """
    caller = get_caller()
    try:
        query = TrendQuery(
            patient_id=patient_id,
            analyte=analyte,
            window_days=window_days,
        )
    except ValidationError as exc:
        return invalid(exc)

    # 读权统一走中枢 a207_policy.enforce_read（单一事实源），覆盖 MCP 粒度
    denied = _guard_access("get_lab_trend")
    if denied:
        return denied
    # 家长受限视图必须经监护人令牌绑定核验（F4）
    denied = _guard_guardian(caller, query.patient_id, guardian_token, "get_lab_trend")
    if denied:
        return denied
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

    # BUG-06：家长仅可见趋势方向受限视图（需求 P1 家庭=✔ 仅方向），不落 READ_FULL
    if caller not in READ_FULL:
        if caller != "parent_assistant":
            return forbidden(caller, "get_lab_trend")
        return {
            "ok": True,
            "data": {
                "patient_id": query.patient_id,
                "data_scope": "limited_parent",
                "analyte": query.analyte,
                "label": ANALYTES[query.analyte]["label"],
                "unit": ANALYTES[query.analyte]["unit"],
                "direction": parent_trend_direction(query.analyte, panels),
                "note": "受限视图仅提示趋势方向（↑/↓/→），不含具体数值，详情请咨询主管医生。",
            },
        }

    trend = build_trend(
        panels, query.analyte, float(patient["age_years"]), patient["sex"]
    )
    trend["patient_id"] = query.patient_id
    trend["window_days"] = query.window_days
    return {"ok": True, "data": trend}


# --- 工具 4：upsert_lab_result（写权仅 doctor_assistant）--------------------

def _contract_bounds(analyte: str) -> tuple[float | None, float | None]:
    """从 LabResultIn 模型字段提取契约数值边界 (ge, le)。

    单一事实源在 models.py（Field ge/le），此处动态读取避免重复硬编码——归一化后的
    契约值必须落在模型定义的合理范围内，防止别名输入绕过契约上限（BUG-67 后补）。
    """
    field = LabResultIn.model_fields.get(analyte)
    if field is None:
        return (None, None)
    ge = le = None
    for md in field.metadata:
        if hasattr(md, "ge"):
            ge = md.ge
        if hasattr(md, "le"):
            le = md.le
    return (ge, le)


def _normalize(lab: LabResultIn) -> tuple[dict[str, float], list[str]]:
    """把别名口径换算成契约单位，返回 (契约值, 换算说明)。

    BUG-67 后补（2026-08-12）：别名换算后的契约值须落在模型契约范围（ge/le）内——
    models 只校验输入字段自身范围（如 scr_mg_dL ≤ 40），换算后 scr_umol_L 可能超 3000
    上限（40mg/dL × 88.4 = 3536）绕过校验写入脏数据；此处二次校验，超界抛 ValueError。
    """
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
        ge, le = _contract_bounds(target)
        if (le is not None and converted > le) or (ge is not None and converted < ge):
            raise ValueError(
                f"{alias}={raw[alias]} 归一化为 {target}={converted}，"
                f"超出契约范围 [{ge}, {le}]，拒绝写入")
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
            patient_id=patient_id, lab=lab, write_mode=write_mode
        )
    except ValidationError as exc:
        return invalid(exc)

    patient = find_patient(request.patient_id)
    if patient is None:
        return not_found(request.patient_id)

    try:
        values, conversions = _normalize(request.lab)
    except ValueError as exc:
        # BUG-67 后补：别名换算后超出契约范围（如 scr_mg_dL=40 → scr_umol_L=3536 > 3000）
        # 由 _normalize 抛 ValueError，此处转 INVALID_ARGUMENT 信封（回滚模式同路径）。
        return fail("INVALID_ARGUMENT", str(exc))
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

    OD-012 + 2026-08-12 修复（P1-1）：受 M2 读权限矩阵约束（doctor/risk_warning 全量、
    parent 受限视图，其余角色禁止）；**跨患者队列枚举仅限临床/风险身份**（与 list_patients
    的 COHORT_CALLERS 一致）——此前仅做读权检查，家长（RL）可枚举全部 patient_id（PII）。
    """
    caller = get_caller()
    denied = _guard_access("list_known_patients")
    if denied:
        return denied
    # P1-1 修复：家长仅能访问被绑定单个患儿，不得枚举全量患者花名册
    if caller not in COHORT_CALLERS:
        return forbidden(caller, "list_known_patients")
    ids = known_patient_ids()
    return {"ok": True, "data": {"count": len(ids), "patient_ids": ids}}
