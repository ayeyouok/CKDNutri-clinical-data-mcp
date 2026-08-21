"""child_assistant 患儿身份 P1 读白名单回归测试（2026-08-21）。

覆盖：
- child 调 get_patient_profile（绑定患儿 P0020）→ OK，RL 受限视图（不含化验/判读）
- child 调 get_labs / get_diagnosis（白名单外）→ FORBIDDEN（工具级读收口）
- child 调 get_patient_profile（非绑定患儿 P0001）→ FORBIDDEN（跨患儿绑定）

pytest + 直接运行双模式（CI 逐文件 `python tests/test_*.py`，不依赖 pytest）。
"""
import os

os.environ.setdefault("A207_ENV", "test")  # N-SEC-1：测试进程显式声明测试环境
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")  # 生产护栏确认
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
_POLICY = Path(__file__).resolve().parents[1].parents[1] / "a207-policy" / "src"
for p in (_SRC, _POLICY):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
os.environ.setdefault("A207_CALLER", "doctor_assistant")

import harness  # 注入 Fake Tablestore + 身份（必须先于 core import）

from a207_policy import as_caller  # noqa: E402

from CKDNutri_clinical_data_mcp import core  # noqa: E402
from CKDNutri_clinical_data_mcp import his  # noqa: E402

BOUND = "P0020"
OTHER = "P0001"


def test_child_profile_bound_ok():
    """child 读绑定患儿 P0020 档案 → OK 且为 RL 受限视图（不含化验/病历号/判读）。"""
    os.environ["A207_CHILD_PATIENT_ID"] = BOUND
    with as_caller("child_assistant"):
        r = his.get_patient_profile(BOUND)
    assert r["ok"] is True, r
    d = r["data"]
    assert d["data_scope"] == "limited_parent", "child 视图必须为家长级受限视图"
    assert "biochemistry" not in d, "child 不得看到化验块"
    assert "medical_record_no" not in d, "child 不得看到病历号"
    assert "withheld" in d, "受限视图须声明 withheld"


def test_child_profile_cross_patient_forbidden():
    """child 读非绑定患儿 P0001 档案 → FORBIDDEN（跨患儿越权）。"""
    os.environ["A207_CHILD_PATIENT_ID"] = BOUND
    with as_caller("child_assistant"):
        r = his.get_patient_profile(OTHER)
    assert r["ok"] is False and r["error"] == "FORBIDDEN", r


def test_child_labs_forbidden():
    """child 调 get_labs（P1 读白名单外）→ FORBIDDEN（工具级读收口）。"""
    os.environ["A207_CHILD_PATIENT_ID"] = BOUND
    with as_caller("child_assistant"):
        r = core.get_labs(BOUND)
    assert r["ok"] is False and r["error"] == "FORBIDDEN", r


def test_child_diagnosis_forbidden():
    """child 调 get_diagnosis（白名单外）→ FORBIDDEN。"""
    os.environ["A207_CHILD_PATIENT_ID"] = BOUND
    with as_caller("child_assistant"):
        r = his.get_diagnosis(BOUND)
    assert r["ok"] is False and r["error"] == "FORBIDDEN", r


def test_child_binding_env_missing_rejected():
    """child 未设置 A207_CHILD_PATIENT_ID → get_child_patient_id 抛 CallerUnknown。"""
    from a207_policy import CallerUnknown, get_child_patient_id

    os.environ.pop("A207_CHILD_PATIENT_ID", None)
    try:
        get_child_patient_id()
    except CallerUnknown:
        return
    raise AssertionError("未设置绑定 env 应抛 CallerUnknown")


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"CHILD P1 OK（{len(fns)} 个用例）")


if __name__ == "__main__":
    _run_all()
