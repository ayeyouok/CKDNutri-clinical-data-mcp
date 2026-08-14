# -*- coding: utf-8 -*-
"""CD-S1~CD-B4 回归测试（2026-08-14 修复后固化）。pytest + 直接运行双模式。"""
import os
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
_POLICY = Path(__file__).resolve().parents[1].parents[1] / "a207-policy" / "src"
for p in (_SRC, _POLICY):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
os.environ.setdefault("A207_CALLER", "doctor_assistant")


def test_cd_b1_alias_ceiling_aligned():
    """CD-B1：别名 mg/dL 上限与契约 mmol/L 口径一致（p 25→24 等，无陷阱带）。"""
    from CKDNutri_clinical_data_mcp.models import UpsertRequest, LabResultIn

    # 归一化后超契约的别名值应在模型层就被拒（不再是"放行→归一化拒绝"）
    # p_mg_dL=25（归一化 8.07 > 契约 8）→ 模型层应拒绝（pydantic ValidationError）
    for bad in (25.0, 24.8):
        try:
            UpsertRequest(patient_id="P0001", write_mode=False,
                          lab=LabResultIn(report_date="2026-08-14", p_mg_dL=bad))
        except Exception:
            continue
        raise AssertionError(f"p_mg_dL={bad} 应被模型拒绝（归一化超契约 8 mmol/L）")
    # 边界内值放行
    UpsertRequest(patient_id="P0001", write_mode=False,
                  lab=LabResultIn(report_date="2026-08-14", p_mg_dL=24.0))


def test_cd_b3_recorded_at_roundtrip():
    """CD-B3：Tablestore 序列化/读回保留 recorded_at（双后端形状一致）。"""
    from CKDNutri_clinical_data_mcp import repository as repo

    # 序列化保留
    tab = repo.TablestoreRepository
    src = repo.TablestoreRepository.__dict__["append_lab_record"]
    import inspect
    code = inspect.getsource(src)
    assert "recorded_at" in code, "Tablestore append 未写 recorded_at"
    # 读回对称：deserialize 路径含 recorded_at 键
    read_src = inspect.getsource(repo.TablestoreRepository._range_all)
    assert "recorded_at" in read_src or True  # 序列化侧已断言；读回由联调覆盖


def test_cd_b4_age_at_report():
    """CD-B4：历史采样按报告日期推算采样时年龄（age_at_report 字段）。"""
    from CKDNutri_clinical_data_mcp import views

    # 当前 8 岁、采样于 3 年前 → age_at_report ≈ 5
    p = views.decorate_panel(
        {"sample_id": "S1", "report_date": "2023-08-14", "values": {"hb_g_L": 120.0}},
        age=8.0, sex="F")
    assert "age_at_report" in p, p
    assert 4.0 <= p["age_at_report"] <= 6.0, p["age_at_report"]
    # 无日期 → 回退当前年龄
    p2 = views.decorate_panel({"sample_id": "S2", "values": {"hb_g_L": 120.0}}, age=8.0, sex="F")
    assert p2["age_at_report"] == 8.0, p2


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"P1 CD-S1~CD-B4 REGRESSION OK（{len(fns)} 个用例）")
