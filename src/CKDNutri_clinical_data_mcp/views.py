"""检验结果的呈现层：区间标注、趋势数学。

本模块不做权限判定，只负责把原始数值转成可展示结构。
（2026-08-13：家长受限视图专用函数 parent_view_items / parent_trend_direction 已删除——
用户决策：化验原始数值对家长可见（知情权），权限边界收敛到 core 的 data_scope 标识。）
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ._business import business_today  # 2026-08-19（审查 ④）：统一业务日期单一事实源
from .reference import (
    ANALYTES,
    STATUS_LABEL,
    classify,
    reference_interval,
)

# 变化幅度低于该比例视为持平，避免检测误差被读成趋势
FLAT_TOLERANCE = 0.05
# BUG-67 后补（2026-08-12）：前值接近 0 时按持平处理（防除零抖动），见 trend_code
_EPSILON = 1e-10

# P2-03（2026-08-18）：数据集年龄基准日缓存——patients.json 顶层 as_of 是 age_years
# 快照日（生成器 BASE_DATE），采样时年龄推算的参考基准从运行时 today 改为 as_of。
_AS_OF_CACHE: date | None = None
_AS_OF_LOADED = False

TREND_ARROW = {"up": "↑", "down": "↓", "flat": "→"}


def _dataset_as_of() -> date | None:
    """年龄基准日（数据集 as_of，date 对象）；不可用时返回 None（调用方回退 today）。

    P2-03（2026-08-18）：age_years 是生成基准日（as_of，如 2026-08-01）的快照年龄，
    若推算继续用运行时 today 扣减，会把"基准日至今"的天数一并扣掉（部署越久误差
    越大，age_at_report 被系统性低估，跨年龄带分类漂移）。基准不可用（如 Tablestore
    环境无 patients.json）时回退 today（既有语义，误差退化为原行为）。
    """
    global _AS_OF_CACHE, _AS_OF_LOADED
    if not _AS_OF_LOADED:
        _AS_OF_LOADED = True
        try:
            from . import his as _his
            raw = _his.load_dataset().get("as_of")
            _AS_OF_CACHE = date.fromisoformat(str(raw)) if raw else None
        except Exception:  # noqa: BLE001 - 基准不可用回退 today（见 docstring）
            _AS_OF_CACHE = None
    return _AS_OF_CACHE


def _eff_age_at(age: float, report_date: Any) -> tuple[float, date | None, str | None]:
    """按采样日期推算采样时年龄（N-AGE-1：decorate_panel/build_trend 共用，杜绝双轨）。

    返回 (eff_age, rd_date, warn)：
    - report_date 缺失/非法 → (age, None, None)：回退当前年龄，不告警；
    - report_date 在未来 → (age, rd_date, "future")：N-AGE-2——不推算（推算会
      yrs_since<0 → eff_age 虚增超过当前年龄，参考区间被套到更老龄段失真），
      回退当前年龄并单独告警；
    - 正常 → (max(0.0, age - yrs_since), rd_date, None)：采样时年龄。
    """
    if not report_date:
        return age, None, None
    try:
        rd_date = date.fromisoformat(str(report_date)[:10])
    except ValueError:
        return age, None, None
    # L（2026-08-16，第七轮审查）：未来日期判断统一 UTC 业务日（date.today() 本地
    # naive 与全局 UTC 口径脱节，跨时区部署漂移）。
    # 2026-08-19（审查 ④）：统一走 business_today()（全项目唯一"今天"定义）。
    today = business_today()
    if rd_date > today:
        return age, rd_date, "future"
    # P2-03（2026-08-18）：基准日从运行时 today 改为数据集 as_of（age_years 快照日）——
    # yrs_since 可正可负：基线采样（rd <= as_of）年龄自快照回退；upsert 采样
    # （as_of < rd <= today，晚于基线属正常）年龄自快照向前生长。此前 (today - rd)
    # 会把"基准日至今"的漂移一并扣掉，部署越久 age_at_report 低估越严重。
    ref = _dataset_as_of() or today
    yrs_since = (ref - rd_date).days / 365.25
    return max(0.0, age - yrs_since), rd_date, None


def decorate_panel(panel: dict[str, Any], age: float, sex: str) -> dict[str, Any]:
    """给一次采样的每个指标补上单位、儿童参考区间与状态判定。

    CD-B4 修复（2026-08-14）：参考区间按**采样时年龄**判定——此前用患者当前年龄，
    历史采样（如 5 岁采的样、现在 8 岁）会套用 8 岁参考区间，跨年面板年龄别区间
    失真。采样时年龄 ≈ 当前年龄 −（今天 − report_date）年；日期缺失/非法回退当前
    年龄。结果附 age_at_report 便于审计。
    N-AGE-1/2（2026-08-14）：eff_age 推算抽到 _eff_age_at 共用（与 build_trend
    单实现，杜绝双轨）；未来日期不推算并单独告警。
    """
    eff_age, rd_date, warn = _eff_age_at(age, panel.get("report_date"))
    age_note = None
    if warn == "future":
        age_note = (f"report_date {rd_date.isoformat()} 在未来（数据异常），"
                    f"参考区间按当前年龄 {age:.1f} 岁判定（未做采样时年龄推算）")
    elif rd_date is not None and abs(eff_age - age) > 0.05:
        age_note = (f"按采样日期 {rd_date.isoformat()} 推算采样时年龄 "
                    f"{eff_age:.1f} 岁（当前 {age:.1f} 岁），参考区间按采样时年龄判定")
    results = []
    for analyte, value in panel.get("values", {}).items():
        meta = ANALYTES.get(analyte)
        if meta is None or value is None:
            continue
        interval = reference_interval(analyte, eff_age, sex)
        status = classify(analyte, float(value), eff_age, sex)
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
    out = {
        "sample_id": panel.get("sample_id"),
        "report_date": panel.get("report_date"),
        "specimen": panel.get("specimen"),
        "source": panel.get("source", "baseline"),
        "age_at_report": round(eff_age, 2),
        "results": results,
    }
    if age_note:
        out["age_note"] = age_note
    return out


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
    numer = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    return round(numer / denom * 30.0, 4)


def build_trend(
    panels: list[dict[str, Any]], analyte: str, age: float, sex: str
) -> dict[str, Any]:
    """指定指标的时间序列 + 环比变化率 + 斜率。

    N-AGE-1 修复（2026-08-14）：每个历史点按**采样时年龄**（report_date 推算）判
    status/参考区间——此前统一用患者当前年龄，跨生日患儿（如 5 岁采样、现 8 岁）
    趋势把历史点套 8 岁区间，与 decorate_panel（CD-B4 采样时年龄）双轨失真。
    实现抽 _eff_age_at 与 decorate_panel 共用，杜绝双轨。points 附 age_at_report；
    趋势级参考带用**最新采样点**的采样时年龄（与最新 status 一致）。
    """
    meta = ANALYTES[analyte]
    points = []
    for panel in panels:
        value = panel.get("values", {}).get(analyte)
        if value is None:
            continue
        point_age, _, _ = _eff_age_at(age, panel.get("report_date"))
        status = classify(analyte, float(value), point_age, sex)
        points.append(
            {
                "report_date": panel["report_date"],
                "value": float(value),
                "age_at_report": round(point_age, 2),
                "status": status,
                "status_label": STATUS_LABEL[status],
            }
        )

    latest = points[-1]["value"] if points else None
    previous = points[-2]["value"] if len(points) >= 2 else None
    delta_abs = round(latest - previous, 4) if previous is not None else None
    # P2-04（2026-08-18）：delta_pct 与 trend_code 同口径 epsilon——此前仅判
    # `previous != 0`（精确判零），previous≈0（如 1e-12）时 delta_pct 输出天文数字
    # （+1e14%）而 direction 判 flat，内部数据状态矛盾（报告层展示 delta_pct 会
    # 误导）。abs(previous) < _EPSILON 一律 delta_pct=None（同 trend_code 持平口径）。
    delta_pct = (
        round((latest - previous) / abs(previous) * 100.0, 2)
        if previous is not None and abs(previous) >= _EPSILON
        else None
    )
    # N-AGE-1：趋势级参考带用最新采样点的采样时年龄（与最新 status 口径一致）
    ref_age = points[-1]["age_at_report"] if points else age
    interval = reference_interval(analyte, ref_age, sex)
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
