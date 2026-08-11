"""P1 临床数据域 MCP 服务：患者档案(HIS) + 化验面板(LIS)。

合并自 M1 (a207-his-mcp) + M2 (a207-lis-mcp)。
"""
from __future__ import annotations

from typing import Any, Optional

from fastmcp import FastMCP

from a207_policy import CallerError

from .core import (
    get_critical_values,
    get_diagnosis,
    get_labs,
    get_lab_trend,
    get_nutrition_ceiling,
    get_patient_profile,
    list_known_patients,
    list_patients,
    upsert_lab_result,
    verify_guardian_binding,
)
from .his import issue_guardian_token

mcp = FastMCP("CKDNutri-clinical-data-mcp")


def _invalid(exc):
    if isinstance(exc, CallerError):
        raise
    return {"ok": False, "error": "INVALID_INPUT", "detail": str(exc)}


def main():
    mcp.run()


# ---- M1: 患者主索引 ----

@mcp.tool
def get_patient_profile_tool(patient_id: str, guardian_token: str = "") -> dict[str, Any]:
    """查患儿完整档案（人口学+诊断），家庭助手需 guard_token。"""
    try:
        return get_patient_profile(patient_id, guardian_token)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def get_diagnosis_tool(patient_id: str, guardian_token: str = "") -> dict[str, Any]:
    """查确诊信息（CKD 分期/原发病/透析状态）。家庭助手需携带 guardian_token。"""
    try:
        return get_diagnosis(patient_id, guardian_token)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def verify_guardian_binding_tool(patient_id: str, guardian_token: str) -> dict[str, Any]:
    """验证家长令牌与患儿绑定。"""
    try:
        return verify_guardian_binding(patient_id, guardian_token)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def issue_guardian_token_tool(patient_id: str) -> dict[str, Any]:
    """签发监护人令牌（仅 CKD 临床助手）。生成随机令牌供家长端绑定核验，返回一次性令牌。"""
    try:
        return issue_guardian_token(patient_id)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def list_patients_tool(filters: dict) -> dict[str, Any]:
    """按维度筛查患者队列。仅 CKD 临床助手。"""
    try:
        return list_patients(filters)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def get_nutrition_ceiling_tool(patient_id: str, guardian_token: str = "") -> dict[str, Any]:
    """查营养上限设定。"""
    try:
        return get_nutrition_ceiling(patient_id, guardian_token)
    except Exception as exc:
        return _invalid(exc)


# ---- M2: 化验面板 ----

@mcp.tool
def get_labs_tool(patient_id: str) -> dict[str, Any]:
    """查最新化验面板（按身份视图裁剪：临床=全量，家庭=趋势方向）。"""
    try:
        return get_labs(patient_id)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def get_critical_values_tool(patient_id: str) -> dict[str, Any]:
    """查是否有当前危急值。"""
    try:
        return get_critical_values(patient_id)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def get_lab_trend_tool(patient_id: str, metric: str) -> dict[str, Any]:
    """查指定指标最近 N 次趋势。"""
    try:
        return get_lab_trend(patient_id, metric)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def upsert_lab_result_tool(patient_id: str, lab: dict, write_mode: bool = True) -> dict[str, Any]:
    """写入化验数据。仅 CKD 临床助手。"""
    try:
        return upsert_lab_result(patient_id, lab, write_mode)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def list_known_patients_tool() -> dict[str, Any]:
    """LIS 花名册核对。"""
    try:
        return list_known_patients()
    except Exception as exc:
        return _invalid(exc)


if __name__ == "__main__":
    main()
