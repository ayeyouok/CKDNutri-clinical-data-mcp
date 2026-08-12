"""检验结果的呈现层：区间标注、家长受限视图、趋势数学。

本模块不做权限判定，只负责把原始数值转成可展示结构。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .reference import (
    ANALYTES,
    PARENT_VIEW_ANALYTES,
    STATUS_LABEL,
    classify,
    reference_interval,
)

# 变化幅度低于该比例视为持平，避免检测误差被读成趋势
FLAT_TOLERANCE = 0.05
# BUG-67 后补（2026-08-12）：前值接近 0 时按持平处理（防除零抖动），见 trend_code
_EPSILON = 1e-10

TREND_ARROW = {"up": "↑", "down": "↓", "flat": "→"}


def decorate_panel(panel: dict[str, Any], age: float, sex: str) -> dict[str, Any]:
    """给一次采样的每个指标补上单位、儿童参考区间与状态判定。"""
    results = []
    for analyte, value in panel.get("values", {}).items():
        meta = ANALYTES.get(analyte)
        if meta is None or value is None:
            continue
        interval = reference_interval(analyte, age, sex)
        status = classify(analyte, float(value), age, sex)
        results.append(
            {
                "analyte": analyte,
                "label": meta["label"],
                "value": float(value),
                "unit": meta["unit"],
                "ref_low": interval[0] if interval else None,
                "ref_high": interval[1] if interval else None,
                "status": status,
                "status_label": STATUS_LABEL[status],
            }
        )
    return {
        "sample_id": panel.get("sample_id"),
        "report_date": panel.get("report_date"),
        "specimen": panel.get("specimen"),
        "source": panel.get("source", "baseline"),
        "results": results,
    }


def trend_code(current: float, previous: float | None, analyte: str) -> str:
    """相对上一次采样的方向。previous 缺失或接近 0 时按持平处理。

    BUG-67 后补（2026-08-12）：previous==0 精确比较改为 abs(previous) < EPSILON——
    前值极小时（如肌酐 0.0001）abs(previous) 作分母数值不稳定，且零点附近无趋势意义。
    """
    if previous is None or abs(previous) < _EPSILON:
        return "flat"
    change = (current - previous) / abs(previous)
    if change > FLAT_TOLERANCE:
        return "up"
    if change < -FLAT_TOLERANCE:
        return "down"
    return "flat"


def parent_view_items(
    latest: dict[str, Any],
    previous: dict[str, Any] | None,
    age: float,
    sex: str,
) -> list[dict[str, Any]]:
    """家长受限视图：只给 K/P/Alb/Hb 的方向与区间内外，不返回任何原始数值。"""
    latest_values = latest.get("values", {})
    prev_values = (previous or {}).get("values", {})
    items: list[dict[str, Any]] = []
    for analyte in PARENT_VIEW_ANALYTES:
        value = latest_values.get(analyte)
        if value is None:
            continue
        status = classify(analyte, float(value), age, sex)
        prev = prev_values.get(analyte)
        code = trend_code(float(value), float(prev) if prev is not None else None, analyte)
        items.append(
            {
                "analyte": analyte,
                "label": ANALYTES[analyte]["label"],
                "status": status,
                "status_label": STATUS_LABEL[status],
                "trend": TREND_ARROW[code],
                "trend_code": code,
                "in_reference_range": status == "in_range",
                "critical": status.startswith("critical"),
            }
        )
    return items


def parent_trend_direction(analyte: str, panels: list[dict[str, Any]]) -> str:
    """家长受限趋势视图：仅返回最近两次采样的方向码（up/down/flat），不含任何数值。

    BUG-06 修复配套（需求 P1：get_lab_trend 家庭=✔ 仅方向）。
    """
    points = [p for p in panels if p.get("values", {}).get(analyte) is not None]
    if not points:
        return "flat"
    latest = float(points[-1]["values"][analyte])
    previous = float(points[-2]["values"][analyte]) if len(points) >= 2 else None
    return trend_code(latest, previous, analyte)


def _days_between(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def linear_slope_per_30d(points: list[dict[str, Any]]) -> float | None:
    """对时间序列做最小二乘拟合，返回每 30 天的变化量。"""
    if len(points) < 2:
        return None
    base = points[0]["report_date"]
    xs = [float(_days_between(base, p["report_date"])) for p in points]
    ys = [float(p["value"]) for p in points]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return None
    numer = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return round(numer / denom * 30.0, 4)


def build_trend(
    panels: list[dict[str, Any]], analyte: str, age: float, sex: str
) -> dict[str, Any]:
    """指定指标的时间序列 + 环比变化率 + 斜率。"""
    meta = ANALYTES[analyte]
    points = []
    for panel in panels:
        value = panel.get("values", {}).get(analyte)
        if value is None:
            continue
        status = classify(analyte, float(value), age, sex)
        points.append(
            {
                "report_date": panel["report_date"],
                "value": float(value),
                "status": status,
                "status_label": STATUS_LABEL[status],
            }
        )

    latest = points[-1]["value"] if points else None
    previous = points[-2]["value"] if len(points) >= 2 else None
    delta_abs = round(latest - previous, 4) if previous is not None else None
    delta_pct = (
        round((latest - previous) / abs(previous) * 100.0, 2)
        if previous not in (None, 0)
        else None
    )
    interval = reference_interval(analyte, age, sex)
    return {
        "analyte": analyte,
        "label": meta["label"],
        "unit": meta["unit"],
        "worse_direction": meta["worse"],
        "ref_low": interval[0] if interval else None,
        "ref_high": interval[1] if interval else None,
        "point_count": len(points),
        "points": points,
        "latest": latest,
        "previous": previous,
        "delta_abs": delta_abs,
        "delta_pct": delta_pct,
        "slope_per_30d": linear_slope_per_30d(points),
        "direction": trend_code(latest, previous, analyte) if latest is not None else "flat",
    }
