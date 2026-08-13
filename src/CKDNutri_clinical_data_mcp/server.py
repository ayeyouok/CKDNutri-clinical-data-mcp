"""P1 临床数据域 MCP 服务：患者档案(HIS) + 化验面板(LIS)。

合并自 M1 (a207-his-mcp) + M2 (a207-lis-mcp)。
"""
from __future__ import annotations

import json
import logging

from typing import Any

from fastmcp import FastMCP

from a207_policy import CallerError

# BUG-55（2026-08-12）：函数拆分到 his.py 后，server 导入未同步——get_diagnosis /
# get_patient_profile / get_nutrition_ceiling / list_patients / verify_guardian_binding
# 实际定义在 .his，此前全从 .core 导入导致 server 整体 ImportError、P1 MCP 无法启动。
# 按真实模块归属拆分导入。
from .core import (
    get_critical_values,
    get_labs,
    get_lab_trend,
    upsert_lab_result,
)
from .his import (
    get_diagnosis,
    get_nutrition_ceiling,
    get_patient_profile,
    issue_guardian_token,
    list_patients,
    verify_guardian_binding,
)

mcp = FastMCP("CKDNutri-clinical-data-mcp")

# B2（2026-08-12 五包审查）：异常分级归类统一到 care/assessment 口径——
# 未知系统异常（内部 Code Bug）归 INTERNAL_ERROR 且 detail 脱敏，完整堆栈仅服务端日志。
logger = logging.getLogger("CKDNutri-clinical-data-mcp")


def _invalid(exc):
    if isinstance(exc, CallerError):
        # BUG-54（2026-08-12）：越权/身份未解析统一返回 FORBIDDEN 信封，不再向上抛 500。
        # 2026-08-12（七审，care 同口径）：caller/action/reason 三重 or 保底。
        logger.warning("临床数据服务鉴权拒绝: exc=%s", exc)
        caller = getattr(exc, "caller", None) or "?"
        action = getattr(exc, "action", None) or "access"
        reason = getattr(exc, "reason", None) or str(exc) or "无明确原因"
        return {"ok": False, "error": "FORBIDDEN",
                "detail": f"caller={caller} 无权 {action}（{reason}）"}
    # BUG-67 + B2（2026-08-12）：内部数据错误（store/his 损坏文件 RuntimeError、
    # FileNotFoundError/OSError/JSONDecodeError）归 INTERNAL_ERROR 且 detail **脱敏**
    # （不裸暴露 str(exc) 中的服务端路径），完整异常仅留服务端日志。
    if isinstance(exc, (FileNotFoundError, OSError, json.JSONDecodeError, RuntimeError)):
        logger.warning("临床数据服务内部数据错误: %s", exc)
        return {"ok": False, "error": "INTERNAL_ERROR",
                "detail": "内部数据错误（error_code=P1_DATA），详情见服务端日志"}
    if isinstance(exc, ValueError):
        # core/his 层业务/参数校验异常——detail 对调用方有明确语义，保留
        logger.info("临床数据服务参数校验拦截: %s", exc)
        return {"ok": False, "error": "INVALID_INPUT", "detail": str(exc)}
    # 未知系统异常 = 内部 Code Bug——归 INTERNAL_ERROR（编排层不应重试/误判入参问题）
    logger.error("临床数据服务未预期异常（内部 bug，error_code=P1_UNKNOWN）", exc_info=exc)
    return {"ok": False, "error": "INTERNAL_ERROR",
            "detail": "临床数据服务内部错误（error_code=P1_UNKNOWN），请查服务端日志"}


def main():
    # v3.0 启动自检（唯一后端 = Tablestore）：验证 OTS 连通性（仅日志，不阻断启动）。
    # 用于魔搭/HAIP 部署后确认沙箱能否访问表格存储——看运行日志即可判断：
    #   [ots-selfcheck] OK ...        → 网络通，数据读写走 OTS
    #   [ots-selfcheck] FAIL ...      → 沙箱禁外网 / 凭据错误，按 error 排查
    try:
        from .repository import TablestoreRepository

        repo = TablestoreRepository()
        tables = repo._get_client().list_table()
        logger.info("[ots-selfcheck] OK 已连通表格存储，表=%s", sorted(tables))
    except Exception as exc:  # noqa: BLE001 - 自检失败不阻断启动，打日志供排障
        logger.error("[ots-selfcheck] FAIL 无法连接表格存储：%s: %s",
                     type(exc).__name__, str(exc)[:200])
    mcp.run()


# ---- M1: 患者主索引 ----

@mcp.tool
def get_patient_profile_tool(patient_id: str, guardian_token: str = "") -> dict[str, Any]:
    """查患儿完整档案（人口学+诊断），家庭助手需携带 guardian_token。"""
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
    """签发监护人令牌（仅 CKD 临床助手）。生成随机令牌供家长端绑定核验，返回一次性令牌。

    家长访问患儿数据（档案/化验/危急值/趋势）均须凭此令牌完成绑定核验；token 有效期
    30 天，过期后需医生重新签发。令牌须经安全渠道交付家长，勿写入日志。
    """
    try:
        return issue_guardian_token(patient_id)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def list_patients_tool(filter: dict = None, page: int = 1, page_size: int = 50) -> dict[str, Any]:
    """按维度筛查/浏览患者队列（分页，按 patient_id 升序）。仅 CKD 临床助手。

    用法指引（重要）：
    - **查询单个患者请直接用 get_patient_profile_tool(patient_id)**，不要用本工具
      全量拉取后再找——患者多时本工具分页返回，全量拉取会浪费大量时间与上下文。
    - 本工具用于**按条件筛查队列**：filter 支持 age_band / ckd_stage / dialysis /
      sex / primary_disease / min_age_years / max_age_years 等键（见 INVALID_FILTER
      报错提示）；不传 filter 时返回全部患者的第一页。
    - 分页：page 从 1 起；page_size 默认 50、最大 200（超限自动钳制）。
      返回 has_more=true 表示还有下一页；total_matched 为匹配总数。
    - 场景示例：查全部 G3 期患儿 → filter={"ckd_stage": "G3"}。
    """
    try:
        return list_patients(filter, page=page, page_size=page_size)
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
def get_labs_tool(patient_id: str, guardian_token: str = "") -> dict[str, Any]:
    """查最新化验面板（2026-08-13 起家长可见完整化验数值，知情权）。家长需携带 guardian_token 完成患儿绑定。"""
    try:
        return get_labs(patient_id, guardian_token)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def get_critical_values_tool(patient_id: str, guardian_token: str = "") -> dict[str, Any]:
    """查当前危急值明细（2026-08-13 起家长可见命中明细，知情权）。家长需携带 guardian_token 完成患儿绑定。"""
    try:
        return get_critical_values(patient_id, guardian_token)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def get_lab_trend_tool(patient_id: str, analyte: str, window_days: int = None,
                       guardian_token: str = "") -> dict[str, Any]:
    """查指定指标最近 N 次趋势（2026-08-13 起家长可见完整数值趋势，知情权）。家长需携带 guardian_token 完成患儿绑定。"""
    try:
        return get_lab_trend(patient_id, analyte, window_days, guardian_token)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def upsert_lab_result_tool(patient_id: str, lab: dict, write_mode: bool = True) -> dict[str, Any]:
    """写入化验数据。仅 CKD 临床助手。"""
    try:
        return upsert_lab_result(patient_id, lab, write_mode)
    except Exception as exc:
        return _invalid(exc)


if __name__ == "__main__":
    main()
