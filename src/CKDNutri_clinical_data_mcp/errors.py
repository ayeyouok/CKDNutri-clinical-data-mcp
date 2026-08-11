"""统一错误出参构造。

所有工具失败时返回同一形状：{"ok": false, "error": <机器码>, "detail": <人读说明>}，
上层编排可按 error 分支处理，不需要解析 detail 文案。
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError


def fail(error: str, detail: str) -> dict[str, Any]:
    return {"ok": False, "error": error, "detail": detail}


def forbidden(caller: str, tool: str) -> dict[str, Any]:
    return fail("FORBIDDEN", f"caller={caller} not allowed for {tool}")


def not_found(patient_id: str) -> dict[str, Any]:
    return fail("NOT_FOUND", f"patient_id={patient_id} 不在本 LIS 数据集内")


def no_data(patient_id: str) -> dict[str, Any]:
    return fail("NO_DATA", f"patient_id={patient_id} 暂无检验记录")


def invalid(exc: ValidationError | ValueError) -> dict[str, Any]:
    """把 pydantic 校验错误压成单条可读说明。"""
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        loc = ".".join(str(x) for x in first.get("loc", ())) or "input"
        return fail("INVALID_ARGUMENT", f"{loc}: {first.get('msg')}")
    return fail("INVALID_ARGUMENT", str(exc))


def patient_context(patient: dict[str, Any]) -> dict[str, Any]:
    """检验解读所需的最小临床上下文（年龄别参考区间与分期相关判读用）。"""
    return {
        "age_years": patient["age_years"],
        "sex": patient["sex"],
        "ckd_stage": patient["ckd_stage"],
        "dialysis": patient["dialysis"],
        "primary_dx": patient["primary_dx"],
    }
