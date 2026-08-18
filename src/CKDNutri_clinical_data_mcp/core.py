"""a207-lis-mcp 纯逻辑层（M2 检验数据）。

无 fastmcp 依赖，tests/ 可直接 import 断言。
权限依据需求文档 §7 权限矩阵：M2 LIS 医生助手/风险预警可读全量，
家长助手受限读，营养师/患儿伙伴/调度 Agent 禁止直读。
写权依据 MX-3：upsert_lab_result 仅 doctor_assistant。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from a207_policy import (
    ConflictError,
    LIS_CRITICAL_CHANNEL,
    PARENT_ROLE,
    PARENT_EQUIVALENT_ROLES,
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
from .views import build_trend, decorate_panel

# v2.4：业务层数据访问统一走 repository（双后端），不再直连 store 文件层。
def _repo():
    from .repository import get_repository

    return get_repository()

# 权限数据来自 a207_policy（Plan A 单一事实源）；本包不再维护第二份。
READ_FULL = LIS_READ_FULL
READ_LIMITED = LIS_READ_LIMITED
CRITICAL_CHANNEL = LIS_CRITICAL_CHANNEL

# F-3（2026-08-15）：next_action 原指向"a207-risk-rules-mcp.evaluate_risk_rules"——该工具在 P4 工具面不存在（P4 实际暴露 assess_clinical_status_tool），LLM 依提示调用即 ToolNotFound（幽灵指引）。改为真实工具名（编排层可据此编排 DAG）。
REEVALUATE_HINT = "CKDNutri-assessment-mcp.assess_clinical_status_tool"
# F-3：原指向"a207-notify-mcp.notify_critical_value"——P3 工具面实际为 trigger_event_notification_tool（事件驱动通知），改为真实工具名。
NOTIFY_HINT = "CKDNutri-care-mcp.trigger_event_notification_tool"


# --- 工具 1：get_labs -------------------------------------------------------

def get_labs(patient_id: str, guardian_token: str | None = None) -> dict[str, Any]:
    """取患者全部检验记录（按报告日期升序）。

    2026-08-13（用户决策）：**化验原始数值对家长可见**（家长有知情权；现实中医院
    化验单家长本就可查阅）。caller=parent_assistant 返回完整面板（含数值/单位/参考
    区间/状态），data_scope="parent_full"；仍必须经 guardian_token 绑定核验（F4，
    防跨患者读取）。受限边界收敛到**临床判读**（PEW/生长曲线/医生备注在对应接口
    仍对家长隐藏）。
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
    # F4：家长读取必须经监护人令牌绑定核验；缺 token / 不匹配即拒绝，不给降级视图
    denied = _guard_guardian(caller, patient_id, guardian_token, "get_labs")
    if denied:
        return denied

    patient = _repo().get_patient(query.patient_id)
    if patient is None:
        return not_found(query.patient_id)

    panels = _repo().get_panels(query.patient_id)
    age, sex = float(patient["age_years"]), patient["sex"]

    # 空数据统一 NO_DATA（2026-08-12）：此前仅 READ_LIMITED 家长分支返回 no_data、
    # READ_FULL 医生返回 ok:True+panel_count:0——信封不对称。对齐 get_critical_values
    # 模式，在角色分支前统一返回：无检验记录 = NO_DATA 提示，非"合法空结果"。
    if not panels:
        return no_data(query.patient_id)

    if caller in READ_LIMITED:
        # 2026-08-13（用户决策）：家长可见完整化验数值（data_scope 标识 parent_full），
        # 与医生同构面板；临床判读不在此接口。
        return {
            "ok": True,
            "data": {
                "patient_id": query.patient_id,
                "data_scope": "parent_full",
                "context": patient_context(patient),
                "panel_count": len(panels),
                "panels": [decorate_panel(p, age, sex) for p in panels],
                "note": "家长视图含化验原始数值（知情权）；临床判读（PEW/生长曲线等）不在此接口。",
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

    patient = _repo().get_patient(query.patient_id)
    if patient is None:
        return not_found(query.patient_id)

    panels = _repo().get_panels(query.patient_id)
    if not panels:
        return no_data(query.patient_id)

    # P1-4（2026-08-18）：危急扫描从"仅最新一次采样"扩为"最新报告日全部采样"。
    # 此前 latest = panels[-1]（同日多采样取 sample_id 最大者），同日较早采样的危急
    # 值（如晨 K=7.0、午后复查 K=5.0 正常）被漏扫，通知链路静默不触发——医疗安全缺口。
    # 现取最新报告日的全部面板做危急扫描（按指标去重合并），不漏同日任一采样；
    # 不扫历史面板（不把旧危急误报为当前危急）。
    latest_date = panels[-1]["report_date"]
    same_day = [p for p in panels if p["report_date"] == latest_date]
    latest = panels[-1]  # 仅用于 report_date/sample_id 展示（最新采样）
    hits: list[dict[str, object]] = []
    _seen_analytes: set[str] = set()
    for _p in same_day:
        for _h in critical_hits(_p["values"]):
            if _h["analyte"] not in _seen_analytes:
                _seen_analytes.add(_h["analyte"])
                hits.append(_h)
    # 2026-08-13（用户决策）：危急值明细对家长可见（家长知情权；现实中医院会主动
    # 告知危急值）。家长分支仍强制 guardian_token（防跨患者读取），但不落
    # CRITICAL_CHANNEL 通知链路（通知是 doctor/risk_warning 的职责）。
    if caller not in CRITICAL_CHANNEL:
        if caller not in PARENT_EQUIVALENT_ROLES:
            return forbidden(caller, "get_critical_values")
        return {
            "ok": True,
            "data": {
                "patient_id": query.patient_id,
                "data_scope": "parent_full",
                "report_date": latest["report_date"],
                "sample_id": latest["sample_id"],
                "critical_count": len(hits),
                "items": hits,
                # M1（2026-08-16，第七轮审查）：家长分支 next_action 恒 None——此前
                # 下发 NOTIFY_HINT 与注释"家长不落通知链路"矛盾，LLM 依提示可能让家长
                # 重复触发通知（通知是 doctor/risk_warning 的职责）。
                "next_action": None,
                "note": "危急值明细对家长可见（知情权）；请及时联系主管医生处置。",
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
            "scan_scope": "latest_day",  # P1-4：最新报告日全部采样（非仅最新一次）
            # L（2026-08-16）：医生分支补 data_scope（家长分支已有 parent_full，信封对称）
            "data_scope": "full",
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
            "INVALID_INPUT",
            f"analyte={query.analyte} 未知，可选：{', '.join(sorted(ANALYTES))}",
        )

    patient = _repo().get_patient(query.patient_id)
    if patient is None:
        return not_found(query.patient_id)

    panels = _repo().get_panels(query.patient_id)
    if query.window_days is not None:
        panels = _within_window(panels, query.window_days)
    # 空数据统一 NO_DATA（对齐 get_critical_values L136 / get_labs）：角色分支之前、
    # 家长/医生双方同构——"无检验记录"或"窗口内无采样"不是"趋势平坦"，应明确提示。
    if not panels:
        return no_data(query.patient_id)

    # 2026-08-13（用户决策）：家长可见完整数值趋势（知情权）——与医生同构
    # build_trend 结果（含 point 数值/环比/斜率），data_scope="parent_full"。
    if caller not in READ_FULL:
        if caller not in PARENT_EQUIVALENT_ROLES:
            return forbidden(caller, "get_lab_trend")
        trend = build_trend(
            panels, query.analyte, float(patient["age_years"]), patient["sex"]
        )
        # P1-2（2026-08-18）：窗口内该指标无采样（point_count==0）≠ 趋势持平。
        # 此前 build_trend 对 latest is None 静默标 "flat"（误导为"测过且没变"），
        # 现明确返回 NO_DATA（无该指标数据），与空面板同口径，不伪造"持平"趋势。
        if trend["point_count"] == 0:
            return no_data(query.patient_id,
                           f"patient_id={query.patient_id} 在窗口内无 {query.analyte} 化验数据，无法生成趋势")
        trend["patient_id"] = query.patient_id
        trend["window_days"] = query.window_days
        trend["data_scope"] = "parent_full"
        trend["note"] = "家长视图含化验数值趋势（知情权）；临床判读（PEW/生长曲线等）不在此接口。"
        return {"ok": True, "data": trend}

    trend = build_trend(
        panels, query.analyte, float(patient["age_years"]), patient["sex"]
    )
    if trend["point_count"] == 0:
        return no_data(query.patient_id,
                       f"patient_id={query.patient_id} 在窗口内无 {query.analyte} 化验数据，无法生成趋势")
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

    patient = _repo().get_patient(request.patient_id)
    if patient is None:
        return not_found(request.patient_id)

    try:
        values, conversions = _normalize(request.lab)
    except ValueError as exc:
        # BUG-67 后补：别名换算后超出契约范围（如 scr_mg_dL=40 → scr_umol_L=3536 > 3000）
        # 由 _normalize 抛 ValueError，此处转 INVALID_INPUT 信封（回滚模式同路径）。
        return fail("INVALID_INPUT", str(exc))
    if not values:
        return fail("INVALID_INPUT", "lab 中未提供任何可识别的检验指标")
    # P1-3（2026-08-18）：幂等命中分支与正常分支共用同一 critical_hits 结果，
    # 保证两条路径信封结构一致（critical_items/next_action/notify_action 同步）。
    hits = critical_hits(values)

    # B1 修复（2026-08-13）：并发写入 sample_id 碰撞——「当前面板数+1」在并发下
    # 两个请求读到相同 existing → 相同 sample_id → append_lab_record 覆盖写静默丢一条。
    # 服务端改为生成碰撞免疫 ID（uuid4 hex8，UUID 前缀保留人读可辨性）；调用方显式
    # 传 sample_id 仍优先（去重责任上移的显式路径，由调用方保证唯一）。
    # S-2（2026-08-15）：无显式 sample_id 时生成**确定性幂等 ID**——此前 uuid4 每次
    # 调用不同，LIS/集成方超时重试同一化验（同日期同值）会落两行（条件写
    # EXPECT_NOT_EXIST 因 id 不同拦不住）。幂等键 = patient + report_date + 归一化
    # **指标名-值对**（值参与哈希：同值重试命中同 id 幂等返回；并发不同化验值
    # 生成不同 id 不碰撞——若只哈希键名，并发同指标集不同值会 20 个全撞一个 id）。
    if request.lab.sample_id:
        sample_id = request.lab.sample_id
    else:
        # A4-1（2026-08-16，十审）：幂等键**定精度归一化**——此前
        # `sorted(values.items())` 用 Python repr 直接哈希：① 60（int）与 60.0
        # （float）repr 不同（'60' vs '60.0'）；② 浮点计算噪声（0.1*3 =
        # 0.30000000000000004 vs 0.3）repr 不同。两者都会生成不同 sample_id →
        # EXPECT_NOT_EXIST 主键防撞被击穿 → LIS 重试同一化验写两条（正是 S-2
        # 确定性 id 本要防的故障）。统一 round 4 位（与 _normalize 别名换算同
        # 精度）+ json.dumps sort_keys（键序无关），幂等键对类型/噪声鲁棒。
        _norm_vals = {k: round(float(v), 4) for k, v in values.items()}
        id_key = (f"{request.patient_id}|{request.lab.report_date.isoformat()}|"
                  + json.dumps(_norm_vals, sort_keys=True, ensure_ascii=False))
        sample_id = f"{request.patient_id}-S{hashlib.sha1(id_key.encode('utf-8')).hexdigest()[:8]}"
    record = {
        "patient_id": request.patient_id,
        "sample_id": sample_id,
        "report_date": request.lab.report_date.isoformat(),
        "specimen": request.lab.specimen or "静脉血",
        "values": values,
        "recorded_by": caller,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    try:
        store_size = _repo().append_lab_record(record) if request.write_mode else None
    except ConflictError as exc:
        # 九审（2026-08-16）：repository 统一抛 ConflictError（替代字符串匹配）。
        # S-2：确定性 id 重试命中已存在（并发保护抛"sample_id 已存在"）→ 幂等成功，
        # 不落两行、不把重试当错误卡死（LIS 重试收到成功即停止）。
        if request.write_mode and not request.lab.sample_id:
            return {
                "ok": True,
                "data": {
                    "patient_id": request.patient_id,
                    "sample_id": sample_id,
                    "report_date": record["report_date"],
                    "persisted": True,
                    "write_mode": True,
                    "idempotent_hit": True,
                    # 九审（2026-08-16）：recommend_reevaluate 收进 data——此前与 ok
                    # 顶层并列（信封契约 {ok, data} 外泄业务标志），且幂等命中分支与
                    # 正常分支结构不一致（正常分支 data 内含 next_action 而本分支缺）。
                    "recommend_reevaluate": True,
                    "note": "同日期同指标集的化验已存在（确定性 sample_id 幂等命中），"
                            "未重复写入",
                    "store_record_count": None,
                    "analytes_written": sorted(values),
                    "unit_conversions": conversions,
                    "critical_count": len(hits),
                    # P1-3（2026-08-18）：幂等命中信封与正常分支对称——补 critical_items
                    # /next_action/notify_action，否则 LIS 超时重试命中同 id 时危急通知
                    # 链路被静默跳过（正常分支有、幂等分支缺，信封漂移）。
                    "critical_items": hits,
                    "next_action": REEVALUATE_HINT,
                    "notify_action": NOTIFY_HINT if hits else None,
                },
            }
        # L（2026-08-16，第七轮审查）+ 九审：**显式 sample_id** 冲突 → CONFLICT 信封
        # （repository 已抛 ConflictError，此处直接捕获；此前 RuntimeError 冒泡到
        # server 被 translate_error 归 INTERNAL_ERROR——调用方无法区分"服务端内部
        # 错误"与"业务冲突"；冲突是预期业务语义，应显式提示换 id）。
        return {
            "ok": False,
            "error": "CONFLICT",
            "detail": f"sample_id={sample_id} 已存在（并发冲突或重复提交），"
                      "拒绝覆盖写入；请更换 sample_id 或确认是否为重复请求",
        }
    hits = critical_hits(values)
    return {
        "ok": True,
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
            # 九审（2026-08-16）：recommend_reevaluate 收进 data（与幂等命中分支
            # 同口径，信封契约 {ok, data} 统一；此前顶层并列 = 契约漂移）。
            "recommend_reevaluate": True,
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
    ids = _repo().known_lis_patient_ids()
    return {"ok": True, "data": {"count": len(ids), "patient_ids": ids}}
