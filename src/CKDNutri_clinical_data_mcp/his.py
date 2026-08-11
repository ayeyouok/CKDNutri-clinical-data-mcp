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
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._policy import (
    CLINICIAN_ONLY_FIELDS,
    HIS_ALLOWED_FILTER_KEYS,
    HIS_COHORT,
    P1_PARENT_HIDDEN_FIELDS,
    PermissionDenied,
    atomic_write_json,
    enforce_read,
    enforce_write,
    get_caller,
    resolve_state_path,
)

DEFAULT_DATA_FILE = Path(__file__).resolve().parent / "data" / "patients.json"
DATA_FILE_ENV = "A207_HIS_DATA_FILE"
PATIENT_ID_PATTERN = re.compile(r"^P[0-9]{4,}$")

# F2（v2.3）：通用角色闸统一走 a207_policy.enforce_read / enforce_write 中枢（单一事实源），
# 不再本地维护 caller not in X 的白/黑名单。家长 guardian_token 闸由 _guard_guardian 独立负责（F1/F4）。
# 跨患者队列检索（list_patients）在中枢读权之上再叠加 M1 专属 cohort 限制（仅临床/风险身份）。
COHORT_CALLERS: frozenset[str] = HIS_COHORT
ALLOWED_FILTER_KEYS: frozenset[str] = HIS_ALLOWED_FILTER_KEYS

_MCP_NAME = "CKDNutri-clinical-data-mcp"


def _guard_access(tool: str, *, write: bool = False) -> dict[str, Any] | None:
    """通用角色闸：统一走中枢 a207_policy.enforce_read / enforce_write（单一事实源）。

    放行返回 None；越权抛 PermissionDenied，此处转成 P1 既有 FORBIDDEN 信封（契约不变）。
    家长 guardian_token 核验由 _guard_guardian 独立负责，本函数不处理。
    """
    try:
        if write:
            enforce_write(_MCP_NAME, tool)
        else:
            enforce_read(_MCP_NAME, tool)
    except PermissionDenied as exc:
        return _err("FORBIDDEN",
                    f"caller={exc.caller} 无权访问 {tool}：{exc.reason}")
    return None


# F3（v2.3）：家长脱敏基准统一到 a207_policy 单一事实源。
# 必剥集合 = 全系统医生专有叶子字段(CLINICIAN_ONLY_FIELDS) ∪ P1 专有聚合块(P1_PARENT_HIDDEN_FIELDS)。
# nutrition_ceiling 是 P1 对家长的刻意放行（家长须知晓医生设定的摄入上限），从必剥集合剔除。
_PARENT_HIDDEN_FIELDS = (CLINICIAN_ONLY_FIELDS | P1_PARENT_HIDDEN_FIELDS) - {"nutrition_ceiling"}
# 家长受限视图对外声明"被隐藏"的字段（结构块 + 与之相关的两个叶子级敏感键）。
_PARENT_WITHHELD_DOC = P1_PARENT_HIDDEN_FIELDS | {"z_score_height", "stage_confirmed_by"}


def _strip_parent_sensitive(obj: Any, removed: set[str]) -> Any:
    """递归剥除家长不可见的敏感键（F3 单一事实源）。

    遍历 dict/list，凡是键名落在 _PARENT_HIDDEN_FIELDS 的逐项移除并记录，确保未来新增的敏感字段
    也会自动从家长视图剥离，不再依赖手写白名单（杜绝双轨漂移）。
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k in _PARENT_HIDDEN_FIELDS:
                removed.add(k)
            else:
                out[k] = _strip_parent_sensitive(v, removed)
        return out
    if isinstance(obj, list):
        return [_strip_parent_sensitive(v, removed) for v in obj]
    return obj

# --- 监护人令牌状态库（修复 F1：取代可被 patient_id 直接伪造的 "guardian_<id>" 常量）---
# 令牌由服务端随机生成（secrets），按 patient_id 持久化到可写状态目录；
# 仅临床角色 doctor_assistant 可签发，家长经 verify_guardian_binding 提交完成绑定核验。
GUARDIAN_TOKEN_STORE = "guardian_tokens.json"
GUARDIAN_TOKEN_BYTES = 32
GUARDIAN_ISSUERS: frozenset[str] = frozenset({"doctor_assistant"})
GUARDIAN_TOKEN_DIR_ENV = "A207_GUARDIAN_TOKEN_DIR"


def _guardian_store_path() -> Path:
    base = os.environ.get(GUARDIAN_TOKEN_DIR_ENV)
    return resolve_state_path(GUARDIAN_TOKEN_STORE, base=base)


def _load_guardian_tokens() -> dict[str, Any]:
    p = _guardian_store_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_guardian_tokens(tokens: dict[str, Any]) -> None:
    atomic_write_json(_guardian_store_path(), tokens)


def issue_guardian_token(patient_id: str, issuer: str | None = None) -> dict[str, Any]:
    """签发监护人令牌（仅 doctor_assistant）。

    生成服务端随机令牌（不可由 patient_id 推导），按 patient_id 持久化到状态库。
    家长端经安全渠道获取后，调用 verify_guardian_binding 提交以完成绑定核验。
    重复调用将轮换（覆盖）该患儿令牌。
    """
    caller = issuer or get_caller()
    if caller not in GUARDIAN_ISSUERS:
        return _err("FORBIDDEN",
                    f"caller={caller} 无权签发监护人令牌（仅 {sorted(GUARDIAN_ISSUERS)}）")
    if not isinstance(patient_id, str) or not PATIENT_ID_PATTERN.match(patient_id):
        return _err("INVALID_ARGUMENT", f"patient_id={patient_id} 格式不合法")
    if _lookup(patient_id) is None:
        return _err("NOT_FOUND", f"patient_id={patient_id} 不在患者主数据中")
    token = secrets.token_urlsafe(GUARDIAN_TOKEN_BYTES)
    tokens = _load_guardian_tokens()
    tokens[patient_id] = {
        "token": token,
        "issued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "issued_by": caller,
    }
    _save_guardian_tokens(tokens)
    return {
        "ok": True,
        "data": {
            "patient_id": patient_id,
            "guardian_token": token,
            "issued_at": tokens[patient_id]["issued_at"],
            "issued_by": caller,
            "note": "令牌须经安全渠道交付家长端，切勿写入日志或明文下发",
        },
    }


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
    """恒定时间比对服务端持久化的随机令牌（修复 F1）。

    旧实现 expected = "guardian_" + patient_id 可被任意知道 patient_id 者伪造。
    新实现从状态库取该患儿的随机令牌做 hmac.compare_digest；患儿无令牌（或不存在）
    时以空串参与比对，统一走恒定时间路径，避免「患儿是否存在」的枚举时序差。
    """
    entry = _load_guardian_tokens().get(patient_id)
    stored = entry.get("token", "") if isinstance(entry, dict) else ""
    return hmac.compare_digest(stored, guardian_token or "")


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
    denied = _guard_access("get_patient_profile")
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
        # 退役角色分支（营养评估已并入 P2）：保留逻辑，脱敏基准同家长视图引用中央集合
        data["dialysis_detail"] = p["dialysis_detail"]
        data["food_diary_5d"] = p["food_diary_5d"]
        data["withheld"] = sorted(P1_PARENT_HIDDEN_FIELDS & {"biochemistry", "medical_record_no"})
        data["withheld_reason"] = "营养师无 M2 LIS 读权，生化数据不经 M1 旁路下发"
    else:
        # 家长受限视图（F3）：以 a207_policy 单一事实源递归剥敏感键，withheld 声明引用中央集合
        removed: set[str] = set()
        data = _strip_parent_sensitive(data, removed)
        data["withheld"] = sorted(_PARENT_WITHHELD_DOC)
        data["withheld_reason"] = (
            "家长受限视图仅含诊断、过敏与医生设定上限；脱敏基准见 a207_policy "
            "CLINICIAN_ONLY_FIELDS + P1_PARENT_HIDDEN_FIELDS")
    return {"ok": True, "data": data}


def get_diagnosis(patient_id: str,
                  guardian_token: str | None = None) -> dict[str, Any]:
    """仅返回确诊分期与透析方式。营养师/家长读此接口而非复算分期。"""
    caller = get_caller()
    denied = _guard_access("get_diagnosis")
    if denied:
        return denied
    denied = _guard_guardian(caller, patient_id, guardian_token, "get_diagnosis")
    if denied:
        return denied
    p = _lookup(patient_id)
    if p is None:
        return _err("NOT_FOUND", f"patient_id={patient_id} 不在患者主数据中")
    payload: dict[str, Any] = {
        "patient_id": p["patient_id"],
        "data_scope": _scope_of(caller),
        **_diagnosis_block(p),
        "diet_restrictions": list(p["diet_restrictions"]),
        "note": MX1_NOTE,
    }
    # F3：家长视图递归剥敏感键（如 stage_confirmed_by），与 get_patient_profile 统一基准
    if _scope_of(caller) == "limited_parent":
        removed: set[str] = set()
        payload = _strip_parent_sensitive(payload, removed)
    return {"ok": True, "data": payload}


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
    denied = _guard_access("list_patients")
    if denied:
        return denied
    # 跨患者队列检索在中枢读权之上再叠加 M1 cohort 限制（家长仅能访问被绑定单个患儿）
    if caller not in COHORT_CALLERS:
        return _err("FORBIDDEN", f"caller={caller} not allowed for list_patients（队列检索仅限临床/风险身份）")
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
    denied = _guard_access("get_nutrition_ceiling")
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
