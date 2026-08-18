"""P1 临床数据域 MCP 服务：患者档案(HIS) + 化验面板(LIS)。

合并自 M1 (a207-his-mcp) + M2 (a207-lis-mcp)。
"""
from __future__ import annotations

import json
import logging
import os

from typing import Any, Optional

from fastmcp import FastMCP

from a207_policy import translate_error

# BUG-55（2026-08-12）：函数拆分到 his.py 后，server 导入未同步——get_diagnosis /
# get_patient_profile / get_nutrition_ceiling / list_patients / verify_guardian_binding
# 实际定义在 .his，此前全从 .core 导入导致 server 整体 ImportError、P1 MCP 无法启动。
# 按真实模块归属拆分导入。
from .core import (
    get_critical_values,
    get_labs,
    get_lab_trend,
    list_known_patients,
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
    # B2 中心化（2026-08-15）：异常翻译收敛到 a207_policy.translate_error 单实现
    return translate_error(exc, domain="P1", logger=logger)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")  # C2（2026-08-15）：生产 stdout 可采集
    # P1-01（2026-08-18）：启动自检按后端条件分支——本地 JSON 开发模式
    # （A207_STORAGE_BACKEND=json）仅做开发模式护栏自检（A207_ACCEPT_DEV_STORAGE=1），
    # **不校验 OTS 连接参数**（此前无条件 Tablestore 自检，JSON 模式本地启动直接
    # SystemExit(1)，开发闭环失败）；生产/Tablestore 模式维持 OTS 连通性自检。
    from .repository import STORAGE_BACKEND_ENV, ensure_json_backend_allowed

    backend = os.environ.get(STORAGE_BACKEND_ENV, "tablestore").strip().lower()
    if backend not in ("tablestore", "json"):
        # P2-02（2026-08-18）：未知后端 fail-fast（与 get_repository 同口径）——
        # 此前任意非 "json" 值静默走 Tablestore，拼写错误（如 jsno）带病运行。
        logger.error("[selfcheck] FAIL A207_STORAGE_BACKEND=%r 非法（仅支持 "
                     "tablestore/json），服务未启动。", backend)
        raise SystemExit(1)
    if backend == "json":
        ensure_json_backend_allowed()  # 未显式确认 A207_ACCEPT_DEV_STORAGE=1 即抛（fail-closed）
        logger.info("[selfcheck] OK 本地 JSON 开发模式（A207_STORAGE_BACKEND=json），跳过 OTS 自检")
    else:
        # v3.0 启动自检（生产后端 = Tablestore）：验证 OTS 连通性。
        # P1-9 修复（2026-08-13）：**自检失败 fail-fast 退出（非零码）**——此前仅打日志
        # 继续 mcp.run()，部署后"服务活着但每个工具 INTERNAL_ERROR"（比启动失败更难发现，
        # 医疗数据读写全挂）。现在自检失败 → stderr 提示 + SystemExit(1)，魔搭/HAIP 部署
        # 立即暴露，不静默带病运行。
        try:
            from .repository import TablestoreRepository

            repo = TablestoreRepository()
            tables = repo._get_client().list_table()
            logger.info("[ots-selfcheck] OK 已连通表格存储，表=%s", sorted(tables))
            # D4（2026-08-18）：部署即初始化缺失表（幂等）——此前 ensure_tablestore_tables
            # 仅 migrate_tablestore.py 手动调用，main() 不建表：魔搭新实例部署后表不存在，
            # 读写全 INTERNAL_ERROR（P1_DATA）。ensure_tables 仅建缺失表（已存在跳过），
            # 不影响已建表/存量数据。
            from .repository import ensure_tablestore_tables

            ensure_tablestore_tables()
        except Exception as exc:  # noqa: BLE001 - 自检失败必须阻断启动（fail-fast）
            logger.error("[ots-selfcheck] FAIL 无法连接表格存储：%s: %s",
                         type(exc).__name__, str(exc)[:200])
            # N-LOG-1（2026-08-14）：冗余 stderr print 并入 logger.error（默认输出
            # 到 stderr，统一采集口径）——此前 logger.error 已打，print 仅重复。
            logger.error(
                "[ots-selfcheck] FAIL 无法连接表格存储（%s）。检查 A207_OTS_* "
                "环境变量与网络后重试；服务未启动。", type(exc).__name__)
            raise SystemExit(1) from exc
    mcp.run()


# ---- M1: 患者主索引 ----

@mcp.tool
def get_patient_profile_tool(patient_id: str, guardian_token: Optional[str] = None) -> dict[str, Any]:
    """查患儿完整档案（人口学+诊断），家庭助手需携带 guardian_token。"""
    try:
        return get_patient_profile(patient_id, guardian_token)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def get_diagnosis_tool(patient_id: str, guardian_token: Optional[str] = None) -> dict[str, Any]:
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
      sex / primary_disease / min_age_years / max_age_years 等键（不支持的键会
      返回错误提示）；不传 filter 时返回全部患者的第一页。
    - 分页：page 从 1 起；page_size 默认 50、最大 200（超限自动钳制）。
      返回 has_more=true 表示还有下一页；total_matched 为匹配总数。
    - 场景示例：查 G3a 期患儿 → filter={"ckd_stage": "G3a"}（CKD 分期为
      G2/G3a/G3b/G4/G5 细分档，无 "G3" 这一档，查询需用细分档）。
    """
    try:
        return list_patients(filter, page=page, page_size=page_size)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def list_known_patients_tool() -> dict[str, Any]:
    """列出本数据集覆盖的全部 patient_id（供跨包联调/编排核对）。

    仅临床/风险身份可用，家长不可枚举患者花名册。返回 {count, patient_ids}。
    """
    try:
        return list_known_patients()
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def get_nutrition_ceiling_tool(patient_id: str, guardian_token: Optional[str] = None) -> dict[str, Any]:
    """查营养上限设定。"""
    try:
        return get_nutrition_ceiling(patient_id, guardian_token)
    except Exception as exc:
        return _invalid(exc)


# ---- M2: 化验面板 ----

@mcp.tool
def get_labs_tool(patient_id: str, guardian_token: Optional[str] = None) -> dict[str, Any]:
    """查患儿化验面板（家长可见完整化验数值，知情权）。家长需携带 guardian_token 完成患儿绑定。"""
    try:
        return get_labs(patient_id, guardian_token)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def get_critical_values_tool(patient_id: str, guardian_token: Optional[str] = None) -> dict[str, Any]:
    """查最新报告日危急值明细（家长可见命中明细，知情权）。家长需携带 guardian_token 完成患儿绑定。"""
    try:
        return get_critical_values(patient_id, guardian_token)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def get_lab_trend_tool(patient_id: str, analyte: str, window_days: int = None,
                       guardian_token: Optional[str] = None) -> dict[str, Any]:
    """查指定指标的时间趋势（家长可见完整数值趋势，知情权）。家长需携带 guardian_token 完成患儿绑定。"""
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
