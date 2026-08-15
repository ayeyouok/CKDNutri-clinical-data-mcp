"""儿科检验参考区间、危急值阈值与单位归一化表。

单位口径锁死，与 contracts/pcp-schema.json 一致：
Scr umol/L；K/P/Ca/Na/Cl/BUN/HCO3 mmol/L；Alb/Hb g/L；iPTH pg/mL；UA umol/L。

参考区间按儿童年龄别给出（成人区间会把 5 岁患儿的正常肌酐误判为异常，
见需求文档 §10 第 2 条），部分指标区分性别。
"""

from __future__ import annotations

# --- 指标元数据 -------------------------------------------------------------

ANALYTES: dict[str, dict[str, str]] = {
    "scr_umol_L": {"label": "血肌酐", "unit": "umol/L", "worse": "high"},
    "bun_mmol_L": {"label": "血尿素氮", "unit": "mmol/L", "worse": "high"},
    "egfr_ml_min": {"label": "估算肾小球滤过率", "unit": "mL/min/1.73m2", "worse": "low"},
    "k_mmol_L": {"label": "血钾", "unit": "mmol/L", "worse": "both"},
    "na_mmol_L": {"label": "血钠", "unit": "mmol/L", "worse": "both"},
    "cl_mmol_L": {"label": "血氯", "unit": "mmol/L", "worse": "both"},
    "ca_mmol_L": {"label": "血钙", "unit": "mmol/L", "worse": "both"},
    "p_mmol_L": {"label": "血磷", "unit": "mmol/L", "worse": "high"},
    "hco3_mmol_L": {"label": "碳酸氢根", "unit": "mmol/L", "worse": "low"},
    "albumin_g_L": {"label": "血白蛋白", "unit": "g/L", "worse": "low"},
    "prealbumin_mg_L": {"label": "前白蛋白", "unit": "mg/L", "worse": "low"},
    "hb_g_L": {"label": "血红蛋白", "unit": "g/L", "worse": "low"},
    "ipth_pg_mL": {"label": "全段甲状旁腺激素", "unit": "pg/mL", "worse": "high"},
    "vitd25oh_nmol_L": {"label": "25-羟维生素D", "unit": "nmol/L", "worse": "low"},
    "ua_umol_L": {"label": "血尿酸", "unit": "umol/L", "worse": "high"},
    "urine_protein_g_24h": {"label": "尿蛋白定量", "unit": "g/24h", "worse": "high"},
    "upcr_mg_mmol": {"label": "尿蛋白肌酐比", "unit": "mg/mmol", "worse": "high"},
}

# --- 年龄分带 ---------------------------------------------------------------

# P1-1（2026-08-15）：双闭区间留缝（3.999,4.0）/(6.999,7.0)/(12.999,13.0)——真实年龄
# 如 3.9996 落缝被兜底成 adolescent，套用青少年参考区间致化验状态静默错判。
# 改**半开区间** [low, high)：上界即下一带下界，全实数轴无缝隙。
AGE_BANDS: tuple[tuple[float, float, str], ...] = (
    (0.0, 4.0, "toddler"),
    (4.0, 7.0, "preschool"),
    (7.0, 13.0, "school"),
    (13.0, 99.0, "adolescent"),
)


def age_band(age_years: float) -> str:
    """把年龄映射到儿科参考区间分带（半开区间，全轴无缝隙）。

    P1-1 修复（2026-08-15）：此前 `low <= age <= high` 双闭区间在整岁边界
    (3.999,4.0)/(6.999,7.0)/(12.999,13.0) 留缝，落缝年龄兜底 adolescent——
    真实年龄（如 3.9996 岁）被套青少年参考区间，化验状态静默错判。
    """
    for low, high, name in AGE_BANDS:
        if low <= age_years < high:
            return name
    return "adolescent"


# --- 参考区间 ---------------------------------------------------------------

# 年龄别（必要时叠加性别）区间：键为 band 或 band_sex
_REF_BY_AGE: dict[str, dict[str, tuple[float, float]]] = {
    "scr_umol_L": {
        "toddler": (15.0, 35.0),
        "preschool": (20.0, 42.0),
        "school": (27.0, 62.0),
        "adolescent_M": (45.0, 90.0),
        "adolescent_F": (40.0, 80.0),
    },
    "bun_mmol_L": {
        "toddler": (1.8, 6.0),
        "preschool": (1.8, 6.4),
        "school": (2.5, 6.8),
        "adolescent": (2.5, 7.1),
    },
    "p_mmol_L": {
        "toddler": (1.25, 2.10),
        "preschool": (1.20, 1.80),
        "school": (1.05, 1.75),
        "adolescent": (0.95, 1.65),
    },
    "hb_g_L": {
        "toddler": (110.0, 140.0),
        "preschool": (115.0, 145.0),
        "school": (115.0, 155.0),
        "adolescent_M": (130.0, 165.0),
        "adolescent_F": (120.0, 155.0),
    },
    "ua_umol_L": {
        "toddler": (100.0, 300.0),
        "preschool": (120.0, 320.0),
        "school": (140.0, 360.0),
        "adolescent_M": (200.0, 430.0),
        "adolescent_F": (150.0, 380.0),
    },
    "hco3_mmol_L": {
        "toddler": (20.0, 26.0),
        "preschool": (21.0, 27.0),
        "school": (22.0, 28.0),
        "adolescent": (22.0, 29.0),
    },
}

# 年龄无关区间
_REF_FLAT: dict[str, tuple[float, float]] = {
    "egfr_ml_min": (90.0, 140.0),
    "k_mmol_L": (3.5, 5.1),
    "na_mmol_L": (135.0, 145.0),
    "cl_mmol_L": (98.0, 107.0),
    "ca_mmol_L": (2.20, 2.70),
    "albumin_g_L": (35.0, 50.0),
    "prealbumin_mg_L": (200.0, 400.0),
    "ipth_pg_mL": (15.0, 65.0),
    # BUG-44 说明（2026-08-12）：50-75 nmol/L 属"不足"带（缺乏 <50），区间下限取 50 为
    # 保守口径；临床判读时 50-75 应提示补充维生素 D，75 以上视为充足。
    "vitd25oh_nmol_L": (50.0, 250.0),
    "urine_protein_g_24h": (0.0, 0.15),
    "upcr_mg_mmol": (0.0, 20.0),
}


def reference_interval(
    analyte: str, age_years: float, sex: str
) -> tuple[float, float] | None:
    """返回 (下限, 上限)；无区间定义时返回 None。"""
    table = _REF_BY_AGE.get(analyte)
    if table is not None:
        band = age_band(age_years)
        return table.get(f"{band}_{sex}") or table.get(band)
    return _REF_FLAT.get(analyte)


# --- 危急值 -----------------------------------------------------------------

# BUG-51 说明（2026-08-12）：本表的"危急值"与 P4 assessment 风险引擎的"L1 等级"
# 是**两套不同粒度**，非矛盾：
#   - 本表 CRITICAL_RULES：需**立即处置**的危急值（如 K>6.5、Hb<60 g/L）。
#   - P4 rules.json 的 L1：**最高风险等级**（如 R-02 K>5.5 高钾血症即 L1，R-03 K>6.5
#     危急高钾也 L1）——5.5-6.5 的高钾属"需尽快处理"的风险事件，但未到危急值。
# 因此同一数值（如 K=6.0）：P1 家长侧显示"偏高/非危急"，P4 医生侧判 L1 风险——
# 两域语义不同，编排层不应将 has_critical 与 risk_level 直接互相推导。
# 家长视图已用 status_label（偏高/偏低/在区间内）提示，危急项另有 critical 标注。

# (指标, 比较符, 阈值, 规则名, 中文说明)
CRITICAL_RULES: tuple[tuple[str, str, float, str, str], ...] = (
    ("k_mmol_L", ">", 6.5, "CRIT-K-HIGH", "血钾危急升高，有致命心律失常风险"),
    ("k_mmol_L", "<", 2.8, "CRIT-K-LOW", "血钾危急降低"),
    ("hb_g_L", "<", 60.0, "CRIT-HB-LOW", "重度贫血，需评估输血指征"),
    ("ca_mmol_L", "<", 1.60, "CRIT-CA-LOW", "血钙危急降低，有抽搐风险"),
    ("ca_mmol_L", ">", 3.50, "CRIT-CA-HIGH", "血钙危急升高"),
    ("na_mmol_L", "<", 120.0, "CRIT-NA-LOW", "血钠危急降低"),
    ("na_mmol_L", ">", 160.0, "CRIT-NA-HIGH", "血钠危急升高"),
    ("p_mmol_L", ">", 3.20, "CRIT-P-HIGH", "血磷危急升高"),
    ("hco3_mmol_L", "<", 12.0, "CRIT-HCO3-LOW", "重度代谢性酸中毒"),
)


def critical_hits(values: dict[str, float | None]) -> list[dict[str, object]]:
    """扫描一组化验值，返回命中的危急值条目。"""
    hits: list[dict[str, object]] = []
    for analyte, op, threshold, rule, note in CRITICAL_RULES:
        value = values.get(analyte)
        if value is None:
            continue
        breached = value > threshold if op == ">" else value < threshold
        if breached:
            meta = ANALYTES[analyte]
            hits.append(
                {
                    "analyte": analyte,
                    "label": meta["label"],
                    "value": value,
                    "unit": meta["unit"],
                    "operator": op,
                    "threshold": threshold,
                    "rule": rule,
                    "note": note,
                    "severity": "critical",
                }
            )
    return hits


def classify(analyte: str, value: float, age_years: float, sex: str) -> str:
    """判定状态：critical_high / high / in_range / low / critical_low / unknown。"""
    for name, op, threshold, _rule, _note in CRITICAL_RULES:
        if name != analyte:
            continue
        if op == ">" and value > threshold:
            return "critical_high"
        if op == "<" and value < threshold:
            return "critical_low"
    interval = reference_interval(analyte, age_years, sex)
    if interval is None:
        return "unknown"
    low, high = interval
    if value < low:
        return "low"
    if value > high:
        return "high"
    return "in_range"


STATUS_LABEL: dict[str, str] = {
    "critical_high": "危急升高",
    "high": "偏高",
    "in_range": "在参考区间内",
    "low": "偏低",
    "critical_low": "危急降低",
    "unknown": "无儿童参考区间",
}

# --- 入口单位归一化 ---------------------------------------------------------

# 外部系统常见的异口径字段 -> (契约字段, 乘以该系数)
UNIT_ALIASES: dict[str, tuple[str, float]] = {
    "scr_mg_dL": ("scr_umol_L", 88.4),
    "bun_mg_dL": ("bun_mmol_L", 0.357),
    "p_mg_dL": ("p_mmol_L", 0.3229),
    "ca_mg_dL": ("ca_mmol_L", 0.2495),
    "hb_g_dL": ("hb_g_L", 10.0),
    "albumin_g_dL": ("albumin_g_L", 10.0),
    "ua_mg_dL": ("ua_umol_L", 59.48),
}
