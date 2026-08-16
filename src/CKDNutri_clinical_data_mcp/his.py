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

import json
import math
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from a207_policy import (
    CLINICIAN_ONLY_FIELDS,
    PARENT_ROLE,
    HIS_ALLOWED_FILTER_KEYS,
    HIS_COHORT,
    P1_PARENT_HIDDEN_FIELDS,
    PermissionDenied,
    atomic_write_json,
    enforce_read,
    enforce_write,
    get_caller,
    resolve_state_path,
    validate_patient_id,
    verify_guardian_token,
)

# v2.4：业务层数据访问统一走 repository（双后端），不再直连 JSON 文件。
# 延迟导入避免循环引用（repository 内部 import 本模块的筛选谓词）。
def _repo():
    from .repository import get_repository

    return get_repository()

DEFAULT_DATA_FILE = Path(__file__).resolve().parent / "data" / "patients.json"
DATA_FILE_ENV = "A207_HIS_DATA_FILE"
# S6 修复（2026-08-13）：patient_id 校验收敛到 a207_policy.validate_patient_id
# （单一事实源），不再本地维护第二份正则——防两处正则漂移（一处改一处漏）。

# F2（v2.3）：通用角色闸统一走 a207_policy.enforce_read / enforce_write 中枢（单一事实源），
# 不再本地维护 caller not in X 的白/黑名单。家长 guardian_token 闸由 _guard_guardian 独立负责（F1/F4）。
# 跨患者队列检索（list_patients）在中枢读权之上再叠加 M1 专属 cohort 限制（仅临床/风险身份）。
COHORT_CALLERS: frozenset[str] = HIS_COHORT
ALLOWED_FILTER_KEYS: frozenset[str] = HIS_ALLOWED_FILTER_KEYS

# list_patients 分页（2026-08-13 医生端量级防护）：默认 50 条/页，上限 200 钳制——
# HIS 患者成百上千时全量返回会灌爆 LLM 上下文并拖慢响应；LLM 应先按 ID 单查，
# 浏览/筛查走分页。超上限钳制而非报错（宁可多页也不让调用方因 page_size 过大崩）。
_LIST_DEFAULT_PAGE_SIZE = 50
_LIST_MAX_PAGE_SIZE = 200

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
# P1 一般（2026-08-15）：withheld 声明与实际剥离集合对齐——此前声明
# P1_PARENT_HIDDEN_FIELDS | {"z_score_height","stage_confirmed_by"} 漏掉
# CLINICIAN_ONLY_FIELDS 叶子、且可能含已被剔除放行的 nutrition_ceiling
# （声明"被隐藏"但实际对家长可见），声明与剥离行为漂移。
# 现直接引用实际剥离集合 _PARENT_HIDDEN_FIELDS（单一事实源，杜绝未来再漂移）。
_PARENT_WITHHELD_DOC = _PARENT_HIDDEN_FIELDS


def _strip_parent_sensitive(obj: Any) -> Any:
    """递归剥除家长不可见的敏感键（F3 单一事实源）。

    遍历 dict/list，凡是键名落在 _PARENT_HIDDEN_FIELDS 的逐项移除，确保未来新增的敏感字段
    也会自动从家长视图剥离，不再依赖手写白名单（杜绝双轨漂移）。

    2026-08-12：移除 removed 收集参数（死代码）——withheld 声明走中央集合
    _PARENT_WITHHELD_DOC（覆盖未挂载的大块字段如 biochemistry/food_diary_5d），
    动态 removed 反而无法覆盖"未挂载即隐藏"的字段，无消费者。
    """
    if isinstance(obj, dict):
        return {
            k: _strip_parent_sensitive(v)
            for k, v in obj.items()
            if k not in _PARENT_HIDDEN_FIELDS
        }
    if isinstance(obj, list):
        return [_strip_parent_sensitive(v) for v in obj]
    return obj

# --- 监护人令牌状态库（修复 F1：取代可被 patient_id 直接伪造的 "guardian_<id>" 常量）---
# 令牌由服务端随机生成（secrets），按 patient_id 持久化到可写状态目录；
# 仅临床角色 doctor_assistant 可签发，家长经 verify_guardian_binding 提交完成绑定核验。
GUARDIAN_TOKEN_STORE = "guardian_tokens.json"
GUARDIAN_TOKEN_BYTES = 32
GUARDIAN_ISSUERS: frozenset[str] = frozenset({"doctor_assistant"})
GUARDIAN_TOKEN_DIR_ENV = "A207_GUARDIAN_TOKEN_DIR"
# 2026-08-12 修复（P2-6）：监护人令牌加有效期，过期后绑定核验失败（家长需医生重新签发）。
# 令牌是家长端身份凭证，泄露后长期有效风险大；30 天为合理轮换周期。
GUARDIAN_TOKEN_TTL_DAYS = 30


def _guardian_store_path() -> Path:
    base = os.environ.get(GUARDIAN_TOKEN_DIR_ENV)
    return resolve_state_path(GUARDIAN_TOKEN_STORE, base=base)


def _load_guardian_tokens() -> dict[str, Any]:
    p = _guardian_store_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # BUG-67 后补（2026-08-12）：令牌库损坏禁止静默返回 {}——否则所有家长绑定
        # 失效（"降级为无绑定"），且 issue_guardian_token 会用新令牌覆盖旧数据；
        # 抛 RuntimeError（server._invalid 归 INTERNAL_ERROR），运维可恢复备份。
        raise RuntimeError(
            f"监护人令牌库 {GUARDIAN_TOKEN_STORE} JSON 损坏，拒绝加载"
            f"（防止绑定状态静默失效），请检查磁盘/恢复备份: {exc}") from exc
    except OSError as exc:
        raise RuntimeError(
            f"监护人令牌库 {GUARDIAN_TOKEN_STORE} 读取失败: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            f"监护人令牌库 {GUARDIAN_TOKEN_STORE} 数据类型错误：期望 dict，"
            f"实际为 {type(data).__name__}，拒绝加载")
    return data


def _save_guardian_tokens(tokens: dict[str, Any]) -> None:
    atomic_write_json(_guardian_store_path(), tokens)


# 令牌库并发保护（2026-08-12）：issue_guardian_token 的 load→改→save RMW 序列在
# 单进程多线程并发签发下会 Lost Update（两请求各读旧库、后写覆盖前写），持锁串行化。
# P1-2 修复（2026-08-13）：_TOKEN_LOCK 仅护**单进程内**多线程；多实例部署（魔搭多副本）
# 并发签发仍会跨进程 Lost Update（各读旧库、后写冲掉先写的绑定，家长被锁在数据外）。
# 增加**跨进程文件锁**（与数据同目录的 .lock 文件，fcntl/msvcrt），RMW 全程持锁。
_TOKEN_LOCK = threading.Lock()


def _guardian_lock_path():
    return _guardian_store_path().with_suffix(".json.lock")


class _CrossProcessTokenLock:
    """跨进程互斥（文件锁）：Windows msvcrt / POSIX fcntl 双平台。"""

    def __enter__(self):
        import os

        # #12（2026-08-15）：open 成功后加锁失败也要关闭句柄——此前 lock 抛异常
        # 时 _fh 泄漏（句柄不释放，重试多次会耗尽文件描述符）。
        self._fh = open(_guardian_lock_path(), "a+", encoding="utf-8")
        try:
            try:
                import msvcrt  # Windows

                try:
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
                except OSError as exc:
                    # D3（2026-08-15）：msvcrt LK_LOCK 阻塞重试约 10s 后抛 OSError——
                    # 此前原样上传被 server 归 INTERNAL_ERROR（虚假 500，误导为数据
                    # 错误）；转带语义的 RuntimeError（资源忙，提示重试）。
                    raise RuntimeError(
                        "跨进程令牌锁获取超时（另一签发/核验进程持锁超过 10s），"
                        "通常是异常进程占用，请稍后重试或检查 .lock 文件") from exc
            except ImportError:
                import fcntl  # POSIX

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except BaseException:
            self._fh.close()
            raise
        return self

    def __exit__(self, *exc):
        try:
            import msvcrt

            msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except ImportError:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()
        return False


def issue_guardian_token(patient_id: str) -> dict[str, Any]:
    """签发监护人令牌（仅 doctor_assistant）。

    生成服务端随机令牌（不可由 patient_id 推导），按 patient_id 持久化到状态库。
    家长端经安全渠道获取后，调用 verify_guardian_binding 提交以完成绑定核验。
    重复调用将轮换（覆盖）该患儿令牌。

    P0-1（2026-08-12 修复）：**禁止入参覆盖身份**——此前提供 issuer 参数且
    caller = issuer or get_caller()，任意调用方可传 issuer="doctor_assistant"
    自证身份绕过签发限制。现强制 get_caller()（部署注入），并补 _guard_access
    写权中枢校验（enforce_write + 矩阵 R/W 回查，与 care 双轨制清理同口径）。
    """
    caller = get_caller()
    denied = _guard_access("issue_guardian_token", write=True)
    if denied:
        return denied
    if caller not in GUARDIAN_ISSUERS:
        return _err("FORBIDDEN",
                    f"caller={caller} 无权签发监护人令牌（仅 {sorted(GUARDIAN_ISSUERS)}）")
    # S6 修复：统一走 a207_policy.validate_patient_id（此前本地 PATIENT_ID_PATTERN）
    try:
        patient_id = validate_patient_id(patient_id)
    except ValueError as exc:
        return _err("INVALID_INPUT", str(exc))
    if _repo().get_patient(patient_id) is None:
        return _err("NOT_FOUND", f"patient_id={patient_id} 不在患者主数据中")
    token = secrets.token_urlsafe(GUARDIAN_TOKEN_BYTES)
    # P1-2 修复：进程内 _TOKEN_LOCK + 跨进程文件锁双重保护（多实例并发签发不丢绑定）
    with _TOKEN_LOCK, _CrossProcessTokenLock():
        # v3.0（2026-08-13）：guardian_tokens 不落 Tablestore——家长绑定校验
        # （a207_policy.verify_guardian_token，跨包共享层）只读 JSON 文件，
        # 因此令牌库以 JSON 为**唯一事实源**，由本模块直接读写（不再经 repository）。
        # 多实例部署的一致性靠 A207_GUARDIAN_TOKEN_DIR 指向共享持久目录 + 文件锁保证。
        tokens = _load_guardian_tokens()
        now = datetime.now(timezone.utc)
        # P1 一般（2026-08-15）：签发时顺带清理过期令牌——过期项核验已 fail-closed
        # 拒绝（不会误放行），但只增不删会无限累积（30 天 TTL，长期运行文件膨胀）。
        # 持锁写回路径清理最安全（无并发读写竞争），写回即自然移除过期项。
        for pid, t in list(tokens.items()):
            if isinstance(t, dict) and _token_expired(t.get("expires_at"), now):
                del tokens[pid]
        # B1（2026-08-15）：签发限流——同一患儿 60s 内重复签发拒绝。此前无限制，
        # prompt 注入/误调用可反复轮换令牌（家长端持有的旧令牌立即失效，被锁在
        # 系统外最长 30 天）；签发是低频操作（医生主动签发），60s 冷却不伤正常流程。
        prev = tokens.get(patient_id)
        if isinstance(prev, dict):
            prev_issued = prev.get("issued_at")
            if prev_issued:
                try:
                    prev_dt = datetime.fromisoformat(str(prev_issued))
                    if prev_dt.tzinfo is None:
                        prev_dt = prev_dt.replace(tzinfo=timezone.utc)
                    if (now - prev_dt).total_seconds() < 60:
                        # B1（2026-08-15）：冷却期内**幂等返回现有令牌**（不轮换）——
                        # 拒绝会让正常流程（重复签发/编排重试）报错卡死；返回现有令牌
                        # 同时挫败 prompt 注入的"高频轮换锁死家长"意图（令牌不变，
                        # 家长持有的旧令牌依然有效，无需重新下发）。
                        _save_guardian_tokens(tokens)
                        return {
                            "ok": True,
                            "data": {
                                "patient_id": patient_id,
                                "guardian_token": prev.get("token", ""),
                                "issued_at": prev_issued,
                                "expires_at": prev.get("expires_at"),
                                "issued_by": prev.get("issued_by"),
                                "idempotent_cooling": True,
                                "note": f"该患儿令牌于 {prev_issued} 签发（<60s 冷却期），"
                                        "幂等返回现有令牌（未轮换）；如需强制轮换请稍后再试",
                            },
                        }
                except (ValueError, TypeError):
                    pass  # issued_at 损坏不拦（令牌仍可正常轮换）
        tokens[patient_id] = {
            "token": token,
            "issued_at": now.isoformat(timespec="seconds"),
            "expires_at": (now + timedelta(days=GUARDIAN_TOKEN_TTL_DAYS)).isoformat(timespec="seconds"),
            "issued_by": caller,
        }
        _save_guardian_tokens(tokens)
    return {
        "ok": True,
        "data": {
            "patient_id": patient_id,
            "guardian_token": token,
            "issued_at": tokens[patient_id]["issued_at"],
            "expires_at": tokens[patient_id]["expires_at"],
            "issued_by": caller,
            "note": "令牌须经安全渠道交付家长端，切勿写入日志或明文下发；"
                    f"有效期 {GUARDIAN_TOKEN_TTL_DAYS} 天，过期需重新签发。",
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
    """读取并缓存 patients.json。数据集缺失时抛错，不返回空集合掩盖问题。

    2026-08-12（并发加固）：缓存更新由原位 _CACHE.clear()+update() 改为**整体替换**
    _CACHE = {...}（模块级引用赋值原子）——并发读不再可能看到 clear 后的中间态
    （此前 force_reload 与 _index() 并发会命中 _CACHE["index"] KeyError）。
    """
    global _CACHE
    path = _data_path()
    key = str(path)
    if force_reload or _CACHE.get("path") != key:
        if not path.exists():
            raise FileNotFoundError(
                f"患者数据集不存在：{path}；先运行 src/CKDNutri_clinical_data_mcp/data/generate.py 生成")
        raw = json.loads(path.read_text(encoding="utf-8"))
        index = {p["patient_id"]: p for p in raw["patients"]}
        if len(index) != len(raw["patients"]):
            raise ValueError("患者数据集存在重复 patient_id，拒绝加载")
        _CACHE = {"path": key, "raw": raw, "index": index}
    return _CACHE["raw"]


def _guard_guardian(caller: str, patient_id: str, guardian_token: str | None,
                    tool: str) -> dict[str, Any] | None:
    """家长助手每次读取前必须通过绑定核验，缺 token 即拒绝，不给降级视图。"""
    if caller != PARENT_ROLE:
        return None
    if not guardian_token:
        return _err("GUARDIAN_UNVERIFIED",
                    f"caller=parent_assistant 调用 {tool} 必须携带 guardian_token")
    if not _token_matches(patient_id, guardian_token):
        # BUG-43（2026-08-12）：与 P2 错误消息统一——令牌过期（30 天 TTL）是常见拒绝原因，
        # 仅提示"不匹配"会让家长误以为输错 token，而不是过期需重新签发。
        return _err("FORBIDDEN", f"guardian_token 与 patient_id={patient_id} 不匹配或已过期")
    return None


def _token_matches(patient_id: str, guardian_token: str) -> bool:
    """恒定时间比对服务端持久化的随机令牌（修复 F1）。

    BUG-36（2026-08-12）：校验逻辑统一收敛到 a207_policy.verify_guardian_token
    （含 expires_at 过期校验 + 恒定时间比对 + 旧令牌兼容），消除与 P2 nutrition 的
    副本漂移——此前 P2 各维护一份副本且缺过期校验，令牌轮换后旧令牌在 P2 仍有效。
    """
    return verify_guardian_token(patient_id, guardian_token)


def _token_expired(expires_at: Any, now: datetime) -> bool:
    """判定令牌是否过期（与 a207_policy.gate.verify_guardian_token 同口径）。

    P1 一般（2026-08-15）：签发路径清理过期令牌用——naive/aware 兼容、
    无法解析一律视为过期（fail-closed）。
    """
    try:
        parsed = datetime.fromisoformat(str(expires_at or ""))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return now > parsed
    except (ValueError, TypeError):
        return True


def _scope_of(caller: str) -> str:
    # BUG-31（2026-08-12）：删除退役角色 nutritionist 分支（v2.3 阶段 0 已退役，
    # CALLERS 仅剩 3 个身份）；家长受限视图、其余（doctor/risk）全量。
    if caller == PARENT_ROLE:
        return "limited_parent"
    return "full"


def _diagnosis_block(p: dict[str, Any]) -> dict[str, Any]:
    # D4（2026-08-15）：硬索引 KeyError 防御——此前 p["ckd_stage"] 直取，种子数据
    # 缺键（手工编辑/旧版 JSON）时 get_diagnosis 整段 500；.get 兜底为"未知"标记。
    def _g(key: str, fallback: Any = None) -> Any:
        return p.get(key, fallback)

    return {
        "ckd_stage": _g("ckd_stage"),
        "ckd_stage_numeric": _g("ckd_stage_numeric"),
        "primary_disease": _g("primary_disease"),
        "dialysis": _g("dialysis"),
        "dialysis_mode": _g("dialysis_mode"),
        "dialysis_label": _g("dialysis_label"),
        "diagnosed_on": _g("diagnosed_on"),
        "stage_confirmed_by": _g("stage_confirmed_by"),
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
        # F-6（2026-08-15）：报告"基本信息"渲染 ckd_stage（content 契约要求
        # demographics 含 ckd_stage/dialysis_mode）——此前 ckd_stage 仅在 diagnosis
        # 块，报告层取 demographics 渲染为 None。分期非敏感（diagnosis 块家长可见），
        # 补到 demographics 无泄露风险。
        "ckd_stage": p.get("ckd_stage"),
        "ckd_stage_numeric": p.get("ckd_stage_numeric"),
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
    # S6 修复：畸形 patient_id 不进存储层（此前未校验，直插 repository）
    try:
        patient_id = validate_patient_id(patient_id)
    except ValueError as exc:
        return _err("INVALID_INPUT", str(exc))
    denied = _guard_guardian(caller, patient_id, guardian_token, "get_patient_profile")
    if denied:
        return denied
    p = _repo().get_patient(patient_id)
    if p is None:
        return _err("NOT_FOUND", f"patient_id={patient_id} 不在患者主数据中")

    scope = _scope_of(caller)
    meta = _repo().get_dataset_meta()
    data: dict[str, Any] = {
        "patient_id": p["patient_id"],
        "data_scope": scope,
        "as_of": meta["as_of"],
        # units 为基础名元数据（数据 schema 展示用，键如 "scr"/"potassium"，与 values
        # 的契约键 "scr_umol_L"/"k_mmol_L" 不同空间）；单位解析唯一事实源 =
        # reference.ANALYTES（契约键 → label/unit），勿按 values 键在 units 中查找。
        "units": meta["units"],
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
    else:
        # 家长受限视图（F3）：以 a207_policy 单一事实源递归剥敏感键，withheld 声明引用中央集合
        # BUG-31：删除退役角色 limited_nutritionist 分支（nutritionist 已退役）
        data = _strip_parent_sensitive(data)
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
    # S6 修复：畸形 patient_id 不进存储层
    try:
        patient_id = validate_patient_id(patient_id)
    except ValueError as exc:
        return _err("INVALID_INPUT", str(exc))
    denied = _guard_guardian(caller, patient_id, guardian_token, "get_diagnosis")
    if denied:
        return denied
    p = _repo().get_patient(patient_id)
    if p is None:
        return _err("NOT_FOUND", f"patient_id={patient_id} 不在患者主数据中")
    payload: dict[str, Any] = {
        "patient_id": p["patient_id"],
        "data_scope": _scope_of(caller),
        **_diagnosis_block(p),
        "diet_restrictions": list(p["diet_restrictions"]),
        "note": MX1_NOTE,
    }
    # F3：家长视图递归剥敏感键（如 stage_confirmed_by），与 get_patient_profile 统一基准；
    # 2026-08-12（二次审查）：补 withheld 显式声明——设计约束"受限视图不静默丢字段：
    # 被裁剪的内容在 withheld 中显式列出"。用中央集合 _PARENT_WITHHELD_DOC（与
    # get_patient_profile 同源，防硬编码清单漂移）。
    if _scope_of(caller) == "limited_parent":
        payload = _strip_parent_sensitive(payload)
        payload["withheld"] = sorted(_PARENT_WITHHELD_DOC)
    return {"ok": True, "data": payload}


def verify_guardian_binding(patient_id: str, guardian_token: str) -> dict[str, Any]:
    """家长身份核验。患者不存在与 token 不符统一返回 bound=false，避免枚举患者。

    2026-08-12（二次审查）：补齐 _guard_access 读权中枢校验——此前本接口是模块内
    唯一未走 a207_policy.enforce_read 的 Tool 入口（其余 get_patient_profile/
    get_diagnosis/list_patients/get_nutrition_ceiling/issue_guardian_token 均显式调用），
    矩阵若对该 Tool 收紧将无法生效。家长矩阵为 ACCESS_READ，补闸后仍可正常调用。
    """
    # P0-1：身份必须由部署环境注入，未注入即 fail-closed；本接口的放行范围维持原状不收紧。
    caller = get_caller()
    denied = _guard_access("verify_guardian_binding")
    if denied:
        return denied
    # S6 修复：畸形 patient_id 不进存储层（此前未校验，直插 repository）
    try:
        patient_id = validate_patient_id(patient_id)
    except ValueError as exc:
        return _err("INVALID_INPUT", str(exc))
    if not isinstance(guardian_token, str) or not guardian_token:
        return _err("INVALID_INPUT", "guardian_token 不能为空")
    p = _repo().get_patient(patient_id)
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


def _validate_filter(criteria: dict[str, Any]) -> str | None:
    """统一校验 list_patients 筛选值（fail-closed）。非法返回错误 detail，合法返回 None。

    2026-08-12（二次审查）拦截三类隐患：
    - has_allergies 字符串：bool("false")=True 误判 → 仅接受 bool 或 "true"/"false"；
    - min/max_age_years 传 bool（float(True)=1.0 绕过类型拦截）或 NaN/inf
      （比较语义异常）→ 拒绝 bool、要求 math.isfinite；
    - 枚举值（age_band/ckd_stage/...）含不可哈希元素（如 dict）→ set(values) TypeError
      → 要求元素为标量。
    """
    for k, v in criteria.items():
        if k in ("age_band", "ckd_stage", "dialysis", "sex", "primary_disease"):
            items = v if isinstance(v, (list, tuple, set)) else [v]
            for it in items:
                # P1 一般（2026-08-15）：枚举键仅接受字符串——此前只拦不可哈希元素，
                # int/float（如 sex=1、age_band=123）放行后 _match 永不命中，静默
                # 返回空队列，调用方误以为"该枚举无匹配患者"（实际是查询写错）。
                if not isinstance(it, str):
                    return f"{k} 的值必须为字符串或字符串列表，收到 {it!r}"
        elif k == "has_allergies":
            if isinstance(v, str):
                if v.strip().lower() not in ("true", "false"):
                    return f"has_allergies 必须为布尔值或 'true'/'false'，收到 {v!r}"
            elif not isinstance(v, bool):
                return f"has_allergies 必须为布尔值或 'true'/'false'，收到 {v!r}"
        elif k in ("min_age_years", "max_age_years"):
            if isinstance(v, bool):
                return f"{k} 不能为布尔值，收到 {v!r}"
            try:
                val = float(v)
            except (TypeError, ValueError):
                return f"{k} 必须是数字，收到 {v!r}"
            if not math.isfinite(val):
                return f"{k} 必须是有限数值，收到 {v!r}"
    # P1 一般（2026-08-15）：min>max 年龄不校验会静默返回空队列（查询写错被当成
    # "无匹配患者"）——交叉校验两界大小。
    if "min_age_years" in criteria and "max_age_years" in criteria:
        lo = float(criteria["min_age_years"])
        hi = float(criteria["max_age_years"])
        if lo > hi:
            return f"min_age_years（{lo}）不能大于 max_age_years（{hi}）"
    return None


def _match(p: dict[str, Any], key: str, want: Any) -> bool:
    if key in ("age_band", "ckd_stage", "dialysis", "sex", "primary_disease"):
        values = want if isinstance(want, (list, tuple, set)) else [want]
        try:
            return p[key] in set(values)
        except TypeError:
            # 防御纵深：入口 _validate_filter 已拦不可哈希元素，此处双保险（fail-open 不匹配）
            return False
    if key == "has_allergies":
        # 入口 _validate_filter 已把字符串规范化为 bool，此处仅防御非 bool 输入
        want = want.strip().lower() == "true" if isinstance(want, str) else want
        return bool(p["allergies"]) is bool(want)
    if key == "min_age_years":
        return p["age_years"] >= float(want)
    if key == "max_age_years":
        return p["age_years"] <= float(want)
    return False


def list_patients(filter: dict[str, Any] | None = None,
                  page: int = 1, page_size: int = _LIST_DEFAULT_PAGE_SIZE) -> dict[str, Any]:
    """按 age_band / ckd_stage / dialysis 等条件筛选队列（分页，按 patient_id 升序）。

    未知筛选键直接报错，不静默忽略。分页参数：page 从 1 起；page_size 默认 50、
    上限 200（超限钳制）。count 为本页条数，total_matched 为匹配总数。
    """
    caller = get_caller()
    denied = _guard_access("list_patients")
    if denied:
        return denied
    # 跨患者队列检索在中枢读权之上再叠加 M1 cohort 限制（家长仅能访问被绑定单个患儿）
    if caller not in COHORT_CALLERS:
        return _err("FORBIDDEN", f"caller={caller} not allowed for list_patients（队列检索仅限临床/风险身份）")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        return _err("INVALID_FILTER", "page 必须为 ≥1 的整数")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size < 1:
        return _err("INVALID_FILTER", "page_size 必须为 ≥1 的整数")
    page_size = min(page_size, _LIST_MAX_PAGE_SIZE)
    criteria = filter or {}
    if not isinstance(criteria, dict):
        return _err("INVALID_FILTER", "filter 必须是对象")
    unknown = sorted(set(criteria) - ALLOWED_FILTER_KEYS)
    if unknown:
        return _err("INVALID_FILTER",
                    f"不支持的筛选键 {unknown}；可用键 {sorted(ALLOWED_FILTER_KEYS)}")
    # 筛选值统一校验（2026-08-12 二次审查）：bool/不可哈希/NaN-inf 拦截 → INVALID_FILTER
    err = _validate_filter(criteria)
    if err:
        return _err("INVALID_FILTER", err)

    # v2.4：数据读取走 repository（双后端），筛选谓词在 repo 内部复用本模块 _match
    repo = _repo()
    rows = repo.list_patients(criteria)
    # 分页前显式排序（Tablestore GetRange 天然按主键序，LocalJson 需排序保证跨页稳定）
    rows = sorted(rows, key=lambda r: r["patient_id"])
    total = len(rows)
    start = (page - 1) * page_size
    page_rows = rows[start:start + page_size]
    meta = repo.get_dataset_meta()
    return {
        "ok": True,
        "data": {
            "count": len(page_rows),
            "total_matched": total,
            "total_in_dataset": meta["patient_count"],
            "page": page,
            "page_size": page_size,
            "has_more": start + page_size < total,
            "applied_filter": dict(criteria),
            "patient_ids": [r["patient_id"] for r in page_rows],
            "patients": page_rows,
        },
    }


def get_nutrition_ceiling(patient_id: str,
                          guardian_token: str | None = None) -> dict[str, Any]:
    """医生设定的个体化摄入上限，供营养计算与食谱生成作为硬约束。"""
    caller = get_caller()
    denied = _guard_access("get_nutrition_ceiling")
    if denied:
        return denied
    # S6 修复：畸形 patient_id 不进存储层
    try:
        patient_id = validate_patient_id(patient_id)
    except ValueError as exc:
        return _err("INVALID_INPUT", str(exc))
    denied = _guard_guardian(caller, patient_id, guardian_token, "get_nutrition_ceiling")
    if denied:
        return denied
    p = _repo().get_patient(patient_id)
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
