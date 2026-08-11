"""a207-his-mcp 纯逻辑层（M1 患者主数据）。

设计约束：
- 本层为纯逻辑，不引入任何模型校验框架或 MCP 运行时，也不引用兄弟包，
  tests/test_tools.py 可在裸 Python 环境直接断言本模块。
- HIS 为只读主数据源，本模块不提供任何写入接口。
- 权限判定全部在函数入口完成，越权一律返回 FORBIDDEN，不返回降级数据。
- 受限视图不静默丢字段：被裁剪的内容在 withheld 中显式列出。
- 调用方身份由部署环境注入（a207_policy.get_caller），模型不得自证身份（P0-1）。
"""

from __future__ import annotations

import hmac
import json
import os
import re
from pathlib import Path
from typing import Any

from a207_policy import (
    HIS_ALLOWED_FILTER_KEYS,
    HIS_BLOCKED,
    HIS_COHORT,
    HIS_FULL_VIEW,
    HIS_LIMITED,
    HIS_READ,
    get_caller,
)

DEFAULT_DATA_FILE = Path(__file__).resolve().parent / "data" / "patients.json"
DATA_FILE_ENV = "A207_HIS_DATA_FILE"
PATIENT_ID_PATTERN = re.compile(r"^P[0-9]{4,}$")

# 放行集合唯一事实源在 a207_policy.matrix（P1-1）；此处仅保留本地别名，下文逻辑零改动。
# 与需求文档 §7 权限矩阵一致：M1 HIS 对 调度/医生/风险预警 为 R，营养师/家长助手为 RL，患儿伙伴禁止
FULL_VIEW_CALLERS: frozenset[str] = HIS_FULL_VIEW
LIMITED_CALLERS: frozenset[str] = HIS_LIMITED
READ_CALLERS: frozenset[str] = HIS_READ
# MX-2 游戏化隔离：患儿伙伴不得触达任何医疗数据
BLOCKED_CALLERS: frozenset[str] = HIS_BLOCKED
# 队列检索属于跨患者操作，家长助手仅能访问被绑定的单个患儿，故不在允许集合内
COHORT_CALLERS: frozenset[str] = HIS_COHORT
ALLOWED_FILTER_KEYS: frozenset[str] = HIS_ALLOWED_FILTER_KEYS

MX1_NOTE = "分期以主诊医生确诊结果为准，本接口不进行 eGFR 复算（MX-1 分期互斥）"

_CACHE: dict[str, Any] = {}


def _err(code: str, detail: str) -> dict[str, Any]:
    return {"ok": False, "error": code, "detail": detail}


def _data_path() -> Path:
    override = os.environ.get(DATA_FILE_ENV)
    return Path(override).resolve() if override else DEFAULT_DATA_FILE


def load_dataset(force_reload: bool = False) -> dict[str, Any]:
    """读取并缓存 patients.json。数据集缺失时抛错，不返回空集合掩盖问题。"""
    path = _data_path()
    key = str(path)
    if force_reload or _CACHE.get("path") != key:
        if not path.exists():
            raise FileNotFoundError(
                f"患者数据集不存在：{path}；先运行 src/a207_his_mcp/data/generate.py 生成")
        raw = json.loads(path.read_text(encoding="utf-8"))
        index = {p["patient_id"]: p for p in raw["patients"]}
        if len(index) != len(raw["patients"]):
            raise ValueError("患者数据集存在重复 patient_id，拒绝加载")
        _CACHE.clear()
        _CACHE.update({"path": key, "raw": raw, "index": index})
    return _CACHE["raw"]


def _index() -> dict[str, Any]:
    load_dataset()
    return _CACHE["index"]


def _guard_read(caller: str, tool: str) -> dict[str, Any] | None:
    if caller in BLOCKED_CALLERS:
        return _err("FORBIDDEN", f"caller={caller} not allowed for {tool}（MX-2 游戏化隔离）")
    if caller not in READ_CALLERS:
        return _err("FORBIDDEN", f"caller={caller} not allowed for {tool}（未登记的调用方）")
    return None


def _guard_guardian(caller: str, patient_id: str, guardian_token: str | None,
                    tool: str) -> dict[str, Any] | None:
    """家长助手每次读取前必须通过绑定核验，缺 token 即拒绝，不给降级视图。"""
    if caller != "parent_assistant":
        return None
    if not guardian_token:
        return _err("GUARDIAN_UNVERIFIED",
                    f"caller=parent_assistant 调用 {tool} 必须携带 guardian_token")
    if not _token_matches(patient_id, guardian_token):
        return _err("FORBIDDEN", f"guardian_token 与 patient_id={patient_id} 不匹配")
    return None


def _token_matches(patient_id: str, guardian_token: str) -> bool:
    expected = f"guardian_{patient_id}"
    return hmac.compare_digest(expected, guardian_token)


def _lookup(patient_id: str) -> dict[str, Any] | None:
    if not isinstance(patient_id, str) or not PATIENT_ID_PATTERN.match(patient_id):
        return None
    return _index().get(patient_id)


def _scope_of(caller: str) -> str:
    if caller == "parent_assistant":
        return "limited_parent"
    if caller == "nutritionist":
        return "limited_nutritionist"
    return "full"


def _diagnosis_block(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "ckd_stage": p["ckd_stage"],
        "ckd_stage_numeric": p["ckd_stage_numeric"],
        "primary_disease": p["primary_disease"],
        "dialysis": p["dialysis"],
        "dialysis_mode": p["dialysis_mode"],
        "dialysis_label": p["dialysis_label"],
        "diagnosed_on": p["diagnosed_on"],
        "stage_confirmed_by": p["stage_confirmed_by"],
    }


def _demographics_block(p: dict[str, Any], scope: str) -> dict[str, Any]:
    block = {
        "age_years": p["age_years"],
        "age_band": p["age_band"],
        "age_band_label": p["age_band_label"],
        "sex": p["sex"],
        "height_cm": p["height_cm"],
        "weight_kg": p["weight_kg"],
        "bmi": p["bmi"],
        "edema": p["edema"],
    }
    if scope != "limited_parent":
        block["bsa_m2"] = p["bsa_m2"]
        block["z_score_height"] = p["z_score_height"]
    return block


def get_patient_profile(patient_id: str,
                        guardian_token: str | None = None) -> dict[str, Any]:
    """人口学 + 诊断 + 过敏史 + 医生营养上限。视图随 caller 收窄。"""
    caller = get_caller()
    denied = _guard_read(caller, "get_patient_profile")
    if denied:
        return denied
    denied = _guard_guardian(caller, patient_id, guardian_token, "get_patient_profile")
    if denied:
        return denied
    p = _lookup(patient_id)
    if p is None:
        return _err("NOT_FOUND", f"patient_id={patient_id} 不在患者主数据中")

    scope = _scope_of(caller)
    data: dict[str, Any] = {
        "patient_id": p["patient_id"],
        "data_scope": scope,
        "as_of": load_dataset()["as_of"],
        "units": load_dataset()["units"],
        "demographics": _demographics_block(p, scope),
        "diagnosis": _diagnosis_block(p),
        "allergies": list(p["allergies"]),
        "diet_restrictions": list(p["diet_restrictions"]),
        "nutrition_ceiling": dict(p["nutrition_ceiling"]),
        "withheld": [],
    }
    if scope == "full":
        data["medical_record_no"] = p["medical_record_no"]
        data["dialysis_detail"] = p["dialysis_detail"]
        data["food_diary_5d"] = p["food_diary_5d"]
        data["biochemistry"] = p["biochemistry"]
    elif scope == "limited_nutritionist":
        data["dialysis_detail"] = p["dialysis_detail"]
        data["food_diary_5d"] = p["food_diary_5d"]
        data["withheld"] = ["biochemistry", "medical_record_no"]
        data["withheld_reason"] = "营养师无 M2 LIS 读权，生化数据不经 M1 旁路下发"
    else:
        data["withheld"] = ["biochemistry", "food_diary_5d", "dialysis_detail",
                            "medical_record_no", "z_score_height"]
        data["withheld_reason"] = "家长受限视图仅含诊断、过敏与医生设定上限"
    return {"ok": True, "data": data}


def get_diagnosis(patient_id: str,
                  guardian_token: str | None = None) -> dict[str, Any]:
    """仅返回确诊分期与透析方式。营养师/家长读此接口而非复算分期。"""
    caller = get_caller()
    denied = _guard_read(caller, "get_diagnosis")
    if denied:
        return denied
    denied = _guard_guardian(caller, patient_id, guardian_token, "get_diagnosis")
    if denied:
        return denied
    p = _lookup(patient_id)
    if p is None:
        return _err("NOT_FOUND", f"patient_id={patient_id} 不在患者主数据中")
    return {
        "ok": True,
        "data": {
            "patient_id": p["patient_id"],
            "data_scope": _scope_of(caller),
            **_diagnosis_block(p),
            "diet_restrictions": list(p["diet_restrictions"]),
            "note": MX1_NOTE,
        },
    }


def verify_guardian_binding(patient_id: str, guardian_token: str) -> dict[str, Any] :
    """家长身份核验。患者不存在与 token 不符统一返回 bound=false，避免枚举患者。"""
    # P0-1：身份必须由部署环境注入，未注入即 fail-closed；本接口的放行范围维持原状不收紧。
    caller = get_caller()
    if not isinstance(guardian_token, str) or not guardian_token:
        return _err("INVALID_ARGUMENT", "guardian_token 不能为空")
    p = _lookup(patient_id)
    bound = p is not None and _token_matches(patient_id, guardian_token)
    return {
        "ok": True,
        "data": {
            "bound": bound,
            "patient_id": patient_id,
            "data_scope": "limited_parent" if bound else "none",
            "reason": "绑定核验通过" if bound else "患儿不存在或监护令牌不匹配",
        },
    }


def _match(p: dict[str, Any], key: str, want: Any) -> bool:
    if key in ("age_band", "ckd_stage", "dialysis", "sex", "primary_disease"):
        values = want if isinstance(want, (list, tuple, set)) else [want]
        return p[key] in set(values)
    if key == "has_allergies":
        return bool(p["allergies"]) is bool(want)
    if key == "min_age_years":
        return p["age_years"] >= float(want)
    if key == "max_age_years":
        return p["age_years"] <= float(want)
    return False


def list_patients(filter: dict[str, Any] | None = None) -> dict[str, Any]:
    """按 age_band / ckd_stage / dialysis 等条件筛选队列。未知筛选键直接报错，不静默忽略。"""
    caller = get_caller()
    if caller in BLOCKED_CALLERS or caller not in COHORT_CALLERS:
        return _err("FORBIDDEN", f"caller={caller} not allowed for list_patients")
    criteria = filter or {}
    if not isinstance(criteria, dict):
        return _err("INVALID_FILTER", "filter 必须是对象")
    unknown = sorted(set(criteria) - ALLOWED_FILTER_KEYS)
    if unknown:
        return _err("INVALID_FILTER",
                    f"不支持的筛选键 {unknown}；可用键 {sorted(ALLOWED_FILTER_KEYS)}")

    rows = []
    for p in load_dataset()["patients"]:
        if all(_match(p, k, v) for k, v in criteria.items()):
            rows.append({
                "patient_id": p["patient_id"],
                "age_years": p["age_years"],
                "age_band": p["age_band"],
                "sex": p["sex"],
                "ckd_stage": p["ckd_stage"],
                "dialysis": p["dialysis"],
                "primary_disease": p["primary_disease"],
                "has_allergies": bool(p["allergies"]),
            })
    return {
        "ok": True,
        "data": {
            "count": len(rows),
            "total_in_dataset": len(load_dataset()["patients"]),
            "applied_filter": dict(criteria),
            "patient_ids": [r["patient_id"] for r in rows],
            "patients": rows,
        },
    }


def get_nutrition_ceiling(patient_id: str,
                          guardian_token: str | None = None) -> dict[str, Any]:
    """医生设定的个体化摄入上限，供营养计算与食谱生成作为硬约束。"""
    caller = get_caller()
    denied = _guard_read(caller, "get_nutrition_ceiling")
    if denied:
        return denied
    denied = _guard_guardian(caller, patient_id, guardian_token, "get_nutrition_ceiling")
    if denied:
        return denied
    p = _lookup(patient_id)
    if p is None:
        return _err("NOT_FOUND", f"patient_id={patient_id} 不在患者主数据中")
    return {
        "ok": True,
        "data": {
            "patient_id": p["patient_id"],
            "data_scope": _scope_of(caller),
            "weight_kg": p["weight_kg"],
            "ckd_stage": p["ckd_stage"],
            "dialysis": p["dialysis"],
            "nutrition_ceiling": dict(p["nutrition_ceiling"]),
            "allergies": list(p["allergies"]),
            "diet_restrictions": list(p["diet_restrictions"]),
            "units": {"energy": "kcal", "protein": "g", "potassium": "mg",
                      "phosphorus": "mg", "sodium": "mg", "fluid": "mL"},
            "note": "上限为医生设定的不可突破值；目标量由 a207-nutrition-calc-mcp 计算后不得超过本上限",
        },
    }
