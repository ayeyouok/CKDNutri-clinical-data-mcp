"""CKDNutri-clinical-data-mcp (P1 HIS) 数据构造器。

把单例患儿的各个子结构（人体测量、营养上限、透析方案、饮食日记、生化序列）
拆成独立函数，供 generate.py 组装。全部依赖传入的 random.Random 实例，
不使用全局随机源，保证同一 seed 下结果可复现。
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from random import Random
from typing import Any

try:
    from . import reference as ref
except ImportError:  # 直接以脚本方式运行 generate.py 时走这里
    import reference as ref  # type: ignore[no-redef]


def _rr(rng: Random, lo: float, hi: float, nd: int = 1) -> float:
    """区间均匀抽样并按 nd 位小数取整。"""
    return round(rng.uniform(lo, hi), nd)


def _interp(table: dict[int, tuple[float, float]], age: float, sex_idx: int) -> float:
    """按年龄在整数年龄参照表之间做线性插值。"""
    lo_age = max(1, min(17, int(math.floor(age))))
    hi_age = min(18, lo_age + 1)
    frac = min(1.0, max(0.0, age - lo_age))
    lo = table[lo_age][sex_idx]
    hi = table[hi_age][sex_idx]
    return lo + (hi - lo) * frac


def build_anthropometry(rng: Random, band: str, sex: str, stage: str,
                        edema: bool) -> dict[str, Any]:
    """生成年龄/身高/体重/身高 Z 评分。CKD 分期越晚，身高落后越明显。"""
    lo_age, hi_age, _ = ref.AGE_BANDS[band]
    age = round(rng.uniform(lo_age, hi_age), 1)
    sex_idx = 0 if sex == "M" else 1
    ref_h = _interp(ref.HEIGHT_REF_CM, age, sex_idx)
    ref_w = _interp(ref.WEIGHT_REF_KG, age, sex_idx)

    deficit = rng.uniform(*ref.STAGE_HEIGHT_DEFICIT[stage])
    height = round(ref_h * (1.0 - deficit), 1)
    # 身高 SD 近似取参照中位数的 4.3%
    z_height = round((height - ref_h) / (0.043 * ref_h), 2)

    # 体重先随身高等比缩放，再叠加体型与水肿因素
    bmi_factor = rng.uniform(0.86, 1.04)
    if edema:
        bmi_factor += rng.uniform(0.06, 0.14)
    weight = round(ref_w * (height / ref_h) ** 2 * bmi_factor, 1)
    bmi = round(weight / (height / 100.0) ** 2, 1)
    bsa = round(math.sqrt(height * weight / 3600.0), 2)
    return {
        "age_years": age, "sex": sex, "height_cm": height, "weight_kg": weight,
        "bmi": bmi, "bsa_m2": bsa, "z_score_height": z_height, "edema": edema,
    }


def build_nutrition_ceiling(rng: Random, band: str, sex: str, stage: str,
                            dialysis: str, weight: float, set_on: date) -> dict[str, Any]:
    """医生设定的个体化摄入上限。per_day 由 per_kg × 实际体重推导。"""
    energy_per_kg = _rr(rng, *ref.ENERGY_KCAL_PER_KG[(band, sex)], nd=0)
    if dialysis == "PD":
        # 腹透液葡萄糖倒灌提供额外能量，口服目标相应下调
        energy_per_kg = round(energy_per_kg * rng.uniform(0.88, 0.94))
    energy_per_day = int(round(energy_per_kg * weight / 10.0) * 10)

    protein_per_kg = ref.PROTEIN_DRI_G_PER_KG[band] * ref.STAGE_PROTEIN_FACTOR[stage]
    protein_per_kg += rng.uniform(*ref.DIALYSIS_PROTEIN_ADD[dialysis])
    protein_per_kg = round(protein_per_kg, 2)
    protein_per_day = round(protein_per_kg * weight, 1)

    k_factor = ref.POTASSIUM_DIALYSIS_OVERRIDE.get(dialysis) or ref.POTASSIUM_MG_PER_KCAL[stage]
    p_factor = ref.PHOSPHORUS_DIALYSIS_OVERRIDE.get(dialysis) or ref.PHOSPHORUS_MG_PER_KCAL[stage]
    k_limit = int(round(max(800.0, energy_per_day * k_factor) / 50.0) * 50) if k_factor else None
    p_limit = int(round(max(300.0, energy_per_day * p_factor) / 50.0) * 50) if p_factor else None

    # 钠上限以年龄段适宜摄入量为基线按分期下调，透析或容量负荷再收紧一档
    na_base = ref.SODIUM_AI_MG_BY_BAND[band] * ref.SODIUM_RESTRICTION_FACTOR[stage]
    if dialysis != "none":
        na_base *= 0.90
    na_limit = int(round(min(2300.0, na_base) / 50.0) * 50)

    if dialysis == "HD":
        fluid = int(round((500.0 + 10.0 * weight) / 50.0) * 50)
    elif dialysis == "PD":
        fluid = int(round(min(2000.0, 800.0 + 15.0 * weight) / 50.0) * 50)
    else:
        fluid = None

    return {
        "energy_kcal_per_kg": energy_per_kg,
        "protein_g_per_kg": protein_per_kg,
        "energy_kcal_per_day": energy_per_day,
        "protein_g_per_day": protein_per_day,
        "potassium_mg_per_day": k_limit,
        "phosphorus_mg_per_day": p_limit,
        "sodium_mg_per_day": na_limit,
        "fluid_ml_per_day": fluid,
        "set_by": f"doc-{rng.randint(8801, 8899)}",
        "set_on": set_on.isoformat(),
        "basis": "PRNT 2020 推荐范围经主诊医生个体化调整",
    }


def build_dialysis_detail(rng: Random, dialysis: str, weight: float, bsa: float,
                          started_on: date) -> dict[str, Any] | None:
    """透析方案明细。PD 输出腹透液葡萄糖负荷，供 M5 估算葡萄糖倒灌。"""
    if dialysis == "none":
        return None
    if dialysis == "HD":
        return {
            "modality": "HD",
            "sessions_per_week": 3,
            "hours_per_session": _rr(rng, 3.5, 4.0),
            "dry_weight_kg": round(weight - rng.uniform(0.2, 0.8), 1),
            "vascular_access": rng.choice(["带隧道带涤纶套中心静脉导管", "自体动静脉内瘘"]),
            "started_on": started_on.isoformat(),
        }
    exchanges = rng.randint(4, 5)
    fill_per_m2 = rng.choice([900, 1000, 1100])
    glucose_pct = rng.choice([1.5, 1.5, 2.5])
    daily_volume_ml = round(fill_per_m2 * bsa * exchanges)
    glucose_g = round(daily_volume_ml / 1000.0 * glucose_pct * 10.0, 1)
    return {
        "modality": "PD",
        "regimen": rng.choice(["APD 夜间自动腹膜透析", "CAPD 持续不卧床腹膜透析"]),
        "exchanges_per_day": exchanges,
        "dwell_hours": _rr(rng, 3.0, 5.0),
        "fill_volume_ml_per_m2": fill_per_m2,
        "dialysate_glucose_pct": glucose_pct,
        "dialysate_glucose_g_per_day": glucose_g,
        "daily_dialysate_volume_ml": daily_volume_ml,
        "started_on": started_on.isoformat(),
        "note": "腹透液葡萄糖吸收量需计入总能量，由 a207-nutrition-calc-mcp 估算",
    }


def _pick(rng: Random, category: str, avoid: set[str], meal: str = "") -> str:
    """从该类别中挑一种未被过敏回避的食物。整类被回避时返回空串，由调用方跳过。"""
    narrowed = ref.MEAL_CATEGORY_POOL.get((meal, category))
    candidates = narrowed if narrowed else tuple(
        n for n, (cat, _k, _g) in ref.FOODS.items() if cat == category)
    pool = sorted(n for n in candidates if n not in avoid)
    if not pool:
        return ""
    return rng.choice(pool)


def build_food_diary(rng: Random, band: str, allergen_foods: set[str],
                     end_date: date) -> list[dict[str, Any]]:
    """连续 5 天饮食日记。强制混入高钾与低钾食物，供风险规则与替换建议演示。"""
    scale = ref.PORTION_SCALE[band]
    start = end_date - timedelta(days=4)
    forced_snack = {
        0: _pick(rng, "fruit", allergen_foods | {"苹果", "梨", "西瓜"}),  # 高钾水果
        1: "苹果",
    }
    entries: list[dict[str, Any]] = []
    for day in range(5):
        day_date = (start + timedelta(days=day)).isoformat()
        for meal, slots in ref.MEAL_SLOTS.items():
            for category, count in slots.items():
                for _ in range(count):
                    if meal == "snack" and day in forced_snack:
                        food = forced_snack[day]
                    else:
                        food = _pick(rng, category, allergen_foods, meal)
                    if not food or food in allergen_foods:
                        continue
                    base_g = ref.FOODS[food][2]
                    grams = round(base_g * scale * rng.uniform(0.85, 1.15), 1)
                    entries.append({
                        "date": day_date,
                        "meal": meal,
                        "meal_label": ref.MEAL_LABEL_ZH[meal],
                        "food": food,
                        "portion_g": grams,
                    })
    return entries


# M2（2026-08-16，第七轮审查）：LAB_RANGES 用**短名**（potassium/hemoglobin...），
# ANALYTES 用**契约长键**（k_mmol_L/hb_g_L...，位于主包 reference）——worse 方向
# 经此映射查主包 ANALYTES 单一事实源（data.reference 无 ANALYTES）。
_SHORT_TO_ANALYTE: dict[str, str] = {
    "potassium": "k_mmol_L", "phosphorus": "p_mmol_L", "calcium": "ca_mmol_L",
    "albumin": "albumin_g_L", "hemoglobin": "hb_g_L", "pth": "ipth_pg_mL",
    "bun": "bun_mmol_L", "bicarbonate": "hco3_mmol_L", "uric_acid": "ua_umol_L",
}

# 延迟导入主包 ANALYTES（避免 data 子模块与主包循环；_lab_value 内 lazy）
_MAIN_ANALYTES: dict | None = None


def _worse_of(short_name: str) -> str:
    """短名 → 主包 ANALYTES 的 worse 方向（high/low/both），查不到默认 high。"""
    global _MAIN_ANALYTES
    if _MAIN_ANALYTES is None:
        from CKDNutri_clinical_data_mcp.reference import ANALYTES as _A
        _MAIN_ANALYTES = _A
    return _MAIN_ANALYTES.get(_SHORT_TO_ANALYTE.get(short_name, ""), {}).get("worse", "high")


def _lab_value(rng: Random, stage: str, dialysis: str, analyte: str,
               drift: float, nd: int) -> float:
    lo, hi = ref.LAB_RANGES[stage][analyte]
    shift = ref.DIALYSIS_LAB_SHIFT.get(dialysis, {}).get(analyte, 0.0)
    span = hi - lo
    # M2（2026-08-16，第七轮审查）：drift 方向按 ANALYTES.worse 修正——此前固定
    # "drift 大（病情进展）→ 值偏高"，对 worse="low" 的指标（Hb/白蛋白/前白蛋白/
    # hco3/egfr/vitD——**越低越坏**）方向反转，生成"病情进展但指标虚假改善"的
    # 时间序列（mock 数据误导演示/测试）。按 worse 取方向：
    #   high → drift 大 → 值高（恶化）；low → drift 大 → 值低（恶化）；
    #   both → 不随 drift 偏（高/低都坏，方向无歧义）。
    worse = _worse_of(analyte)
    direction = 1.0 if worse == "low" else (-1.0 if worse == "high" else 0.0)
    value = rng.uniform(lo, hi) + shift + direction * span * (1.0 - drift) * 0.55
    return round(max(value, lo * 0.75), nd)


def build_biochemistry(rng: Random, stage: str, dialysis: str, height_cm: float,
                       glomerular: bool, latest_date: date,
                       pd_glucose_g: float | None) -> list[dict[str, Any]]:
    """2-3 次跨月度生化采样，时间升序。Scr 由 eGFR 经 Schwartz 公式反推，保持内部自洽。"""
    n_visits = 3 if stage in ("G3b", "G4", "G5") else 2
    if rng.random() < 0.25:
        n_visits = 2 if n_visits == 3 else 3

    egfr_lo, egfr_hi = (ref.DIALYSIS_EGFR_RANGE if dialysis != "none"
                        else ref.STAGE_EGFR_RANGE[stage])
    latest_egfr = _rr(rng, egfr_lo, egfr_hi)

    records: list[dict[str, Any]] = []
    cursor = latest_date
    dates: list[date] = [cursor]
    for _ in range(n_visits - 1):
        cursor = cursor - timedelta(days=rng.randint(28, 45))
        dates.append(cursor)
    dates.reverse()

    for idx, sample_date in enumerate(dates):
        steps_before_latest = n_visits - 1 - idx
        # 越早的采样 eGFR 越高，模拟缓慢进展
        egfr = round(latest_egfr * (1.0 + rng.uniform(0.03, 0.10) * steps_before_latest), 1)
        egfr = min(egfr, egfr_hi * 1.15)
        # Schwartz 床旁公式（μmol/L 口径）：eGFR = 36.5 × 身高cm / Scr
        scr = round(36.5 * height_cm / egfr, 1)
        drift = 1.0 - 0.06 * steps_before_latest
        albumin = _lab_value(rng, stage, dialysis, "albumin", drift, 1)
        if glomerular:
            albumin = round(max(20.0, albumin - rng.uniform(4.0, 11.0)), 1)
        record: dict[str, Any] = {
            "sample_date": sample_date.isoformat(),
            "scr_umol_L": scr,
            "egfr_ml_min_1_73m2": egfr,
            "bun_mmol_L": _lab_value(rng, stage, dialysis, "bun", drift, 1),
            "potassium_mmol_L": _lab_value(rng, stage, dialysis, "potassium", drift, 2),
            "phosphorus_mmol_L": _lab_value(rng, stage, dialysis, "phosphorus", drift, 2),
            "calcium_mmol_L": _lab_value(rng, stage, dialysis, "calcium", drift, 2),
            "sodium_mmol_L": _rr(rng, 133.0, 142.0, 0),
            "bicarbonate_mmol_L": _lab_value(rng, stage, dialysis, "bicarbonate", drift, 1),
            "albumin_g_L": albumin,
            "hemoglobin_g_L": round(_lab_value(rng, stage, dialysis, "hemoglobin", drift, 0)),
            "pth_pg_mL": round(_lab_value(rng, stage, dialysis, "pth", drift, 0)),
            "uric_acid_umol_L": round(_lab_value(rng, stage, dialysis, "uric_acid", drift, 0)),
            "vitamin_d_25oh_nmol_L": _rr(rng, 28.0, 72.0),
            "urine_pcr_mg_mmol": _rr(rng, 220.0, 780.0) if glomerular else _rr(rng, 15.0, 190.0),
        }
        if pd_glucose_g is not None:
            record["pd_dialysate_glucose_g_per_day"] = round(
                pd_glucose_g * rng.uniform(0.92, 1.08), 1)
            record["note"] = "腹透葡萄糖倒灌，评估能量摄入时须扣减"
        records.append(record)
    return records
