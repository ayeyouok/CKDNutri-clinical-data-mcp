"""a207-his-mcp 虚拟患儿数据集生成脚本。

用法：python src/a207_his_mcp/data/generate.py [输出路径]

确定性生成：固定 SEED，同一份代码任意机器跑出的 patients.json 完全一致。
覆盖矩阵：CKD 分期 G2/G3a/G3b/G4/G5 × 透析 非透析/HD/PD × 年龄带 幼儿/学龄前/学龄/青少年。
数据为虚构演示样本，不对应任何真实患者。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from random import Random
from typing import Any

try:
    from . import builders, reference as ref
except ImportError:  # 以脚本方式直接运行
    import builders  # type: ignore[no-redef]
    import reference as ref  # type: ignore[no-redef]

SEED = 20260808
BASE_DATE = date(2026, 8, 1)
# conformance-allow: C4 构建期一次性数据生成脚本，不在生产运行时执行；默认写安装目录是
# 刻意行为（产出随包分发的只读基线 patients.json）。已额外支持 A207_DATA_DIR 外置输出，
# 供只读文件系统/打包环境使用。运行时读路径见 core.DEFAULT_DATA_FILE（只读不写）。
_DATA_DIR = os.environ.get("A207_DATA_DIR")
DEFAULT_OUTPUT = Path(_DATA_DIR).resolve() / "patients.json" if _DATA_DIR else Path(__file__).resolve().parent / "patients.json"
DIET_RESTRICTION_BY_STAGE: dict[str, list[str]] = {
    "G2": [], "G3a": ["限磷"], "G3b": ["限磷", "限钠"],
    "G4": ["低钾", "限磷", "限钠"], "G5": ["低钾", "限磷", "限钠"],
}


def _allergies(rng: Random) -> list[str]:
    """约三分之一患儿带食物过敏史，其中少数为双过敏原。"""
    roll = rng.random()
    if roll >= 0.32:
        return []
    names = sorted(ref.ALLERGEN_FOODS)
    count = 2 if roll < 0.08 else 1
    return sorted(rng.sample(names, count))


def _diet_restrictions(stage: str, dialysis: str) -> list[str]:
    items = list(DIET_RESTRICTION_BY_STAGE[stage])
    if dialysis == "HD" and "限液" not in items:
        items.append("限液")
    if dialysis == "PD" and "限磷" not in items:
        items.append("限磷")
    return items


def build_patient(index: int, stage: str, dialysis: str, band: str) -> dict[str, Any]:
    """按 (分期, 透析, 年龄带) 组合生成单个患儿完整档案。"""
    rng = Random(SEED + index * 7919)
    patient_id = f"P{index + 1:04d}"
    sex = "M" if rng.random() < 0.56 else "F"
    disease = rng.choice(ref.PRIMARY_DISEASE_BY_BAND[band])
    glomerular = any(k in disease for k in ref.GLOMERULAR_KEYS)
    edema = glomerular and rng.random() < 0.35

    anthro = builders.build_anthropometry(rng, band, sex, stage, edema)
    allergies = _allergies(rng)
    allergen_foods: set[str] = set()
    for name in allergies:
        allergen_foods.update(ref.ALLERGEN_FOODS[name])

    diagnosed_on = BASE_DATE - timedelta(days=rng.randint(180, 1500))
    ceiling_set_on = BASE_DATE - timedelta(days=rng.randint(14, 120))
    dialysis_started = BASE_DATE - timedelta(days=rng.randint(90, 900))

    dialysis_detail = builders.build_dialysis_detail(
        rng, dialysis, anthro["weight_kg"], anthro["bsa_m2"], dialysis_started)
    pd_glucose = (dialysis_detail or {}).get("dialysate_glucose_g_per_day")

    diary_end = BASE_DATE - timedelta(days=rng.randint(1, 4))
    lab_latest = BASE_DATE - timedelta(days=rng.randint(3, 20))

    return {
        "patient_id": patient_id,
        "medical_record_no": f"MRN{2600000 + index * 37:07d}",
        "age_band": band,
        "age_band_label": ref.AGE_BANDS[band][2],
        "age_years": anthro["age_years"],
        "sex": sex,
        "height_cm": anthro["height_cm"],
        "weight_kg": anthro["weight_kg"],
        "bmi": anthro["bmi"],
        "bsa_m2": anthro["bsa_m2"],
        "z_score_height": anthro["z_score_height"],
        "edema": anthro["edema"],
        "ckd_stage": stage,
        "ckd_stage_numeric": ref.STAGE_TO_NUMERIC[stage],
        "primary_disease": disease,
        "dialysis": dialysis,
        "dialysis_mode": ref.DIALYSIS_TO_PCP[dialysis],
        "dialysis_label": ref.DIALYSIS_LABEL_ZH[dialysis],
        "dialysis_detail": dialysis_detail,
        "diagnosed_on": diagnosed_on.isoformat(),
        "stage_confirmed_by": f"doc-{rng.randint(8801, 8899)}@{diagnosed_on.isoformat()}",
        "allergies": allergies,
        "diet_restrictions": _diet_restrictions(stage, dialysis),
        "nutrition_ceiling": builders.build_nutrition_ceiling(
            rng, band, sex, stage, dialysis, anthro["weight_kg"], ceiling_set_on),
        "food_diary_5d": builders.build_food_diary(rng, band, allergen_foods, diary_end),
        "biochemistry": builders.build_biochemistry(
            rng, stage, dialysis, anthro["height_cm"], glomerular, lab_latest, pd_glucose),
    }


def build_dataset() -> dict[str, Any]:
    patients = [build_patient(i, stage, dialysis, band)
                for i, (stage, dialysis, band) in enumerate(ref.COHORT)]
    coverage: dict[str, dict[str, int]] = {"ckd_stage": {}, "dialysis": {}, "age_band": {}}
    for p in patients:
        for key, field in (("ckd_stage", "ckd_stage"), ("dialysis", "dialysis"),
                           ("age_band", "age_band")):
            coverage[key][p[field]] = coverage[key].get(p[field], 0) + 1
    return {
        "schema_version": "1.0.0",
        "dataset": "a207-his-mcp mock patient master data",
        "generated_by": "src/a207_his_mcp/data/generate.py",
        "seed": SEED,
        "as_of": BASE_DATE.isoformat(),
        "patient_count": len(patients),
        "coverage": coverage,
        "units": ref.UNITS,
        "disclaimer": "全部为程序化生成的虚构演示数据，不对应真实患者，不得用于临床决策",
        "patients": patients,
    }


def main(argv: list[str]) -> int:
    output = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_OUTPUT
    dataset = build_dataset()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")
    diary_entries = sum(len(p["food_diary_5d"]) for p in dataset["patients"])
    lab_records = sum(len(p["biochemistry"]) for p in dataset["patients"])
    print(f"written: {output}")
    print(f"patients: {dataset['patient_count']}")
    print(f"coverage: {json.dumps(dataset['coverage'], ensure_ascii=False)}")
    print(f"diary_entries: {diary_entries}  lab_records: {lab_records}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
