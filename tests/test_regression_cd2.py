"""十九审（2026-08-19）clinical-data 回归：审查报告 P1-NEW-01/P1-NEW-02/P2-02/P2-03。

覆盖：
- P1-NEW-01：specimen_time 写入后落库 + 读回对称；同日多采样按真实采样时刻排序
  （latest/trend/critical 展示不再依赖 sample_id hash）；缺失 specimen_time 视为当天最早
- P1-NEW-02：同日同指标多次危急值全部保留（移除 analyte 去重，不丢危急历史证据）
- P2-02：migrate 冲突时校验源/目标一致性（_verify_row_consistent）
- P2-03：双单位换算容差重构（整数单位绝对下限 0.5 + 0.1% 相对；真实口径冲突拒绝、
  取整级差异放行）
- 确认：Tablestore known_lis_patient_ids = 基线 ∪ 写库（上轮 P2-01 已修，回归固化）

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

from CKDNutri_clinical_data_mcp import core


def _upsert(patient_id: str, lab: dict) -> dict:
    """upsert 辅助（写权 doctor_assistant，harness 默认身份）。"""
    return core.upsert_lab_result(patient_id, lab=lab)


def test_specimen_time_persisted():
    """P1-NEW-01：upsert 的 specimen_time 落库（Tablestore 端不再丢列）。"""
    r = _upsert("P0005", {
        "report_date": "2026-08-19", "specimen_time": "2026-08-19T08:00:00",
        "k_mmol_L": 4.2})
    assert r["ok"] is True, r
    recs = harness.fake_labs_store_records()
    assert any(rec.get("specimen_time") == "2026-08-19T08:00:00" for rec in recs), recs


def test_specimen_time_order_within_day():
    """P1-NEW-01：同日多采样按 specimen_time 排序——latest 是真实最新（14:00）。"""
    _upsert("P0008", {"report_date": "2026-08-19",
                      "specimen_time": "2026-08-19T08:00:00", "k_mmol_L": 7.0})
    _upsert("P0008", {"report_date": "2026-08-19",
                      "specimen_time": "2026-08-19T14:00:00", "k_mmol_L": 5.0})
    panels = harness.FAKE_REPO.get_panels("P0008")
    latest = panels[-1]
    assert latest["specimen_time"] == "2026-08-19T14:00:00", panels
    assert latest["values"]["k_mmol_L"] == 5.0, latest
    # get_critical_values 的 latest 展示 = 14:00 那条（08:00 K=7.0 危急、14:00 K=5.0 正常）
    cv = core.get_critical_values("P0008")
    assert cv["ok"] is True, cv
    assert cv["data"]["sample_id"] == latest["sample_id"], cv["data"]
    assert cv["data"]["critical_count"] == 1, cv["data"]  # 仅 08:00 的 K=7.0


def test_critical_same_day_no_dedup():
    """P1-NEW-02：同日同指标多次危急全部保留（移除 analyte 去重）。"""
    _upsert("P0009", {"report_date": "2026-08-19",
                      "specimen_time": "2026-08-19T08:00:00", "k_mmol_L": 7.0})
    _upsert("P0009", {"report_date": "2026-08-19",
                      "specimen_time": "2026-08-19T14:00:00", "k_mmol_L": 7.2})
    cv = core.get_critical_values("P0009")
    assert cv["ok"] is True, cv
    k_items = [h for h in cv["data"]["items"] if h["analyte"] == "k_mmol_L"]
    assert len(k_items) == 2, cv["data"]  # 08:00 与 14:00 两条危急都保留


def test_specimen_time_missing_sorted_first():
    """P1-NEW-01：缺失 specimen_time 的样本视为当天最早（保守，不抢占明确时刻）。"""
    _upsert("P0010", {"report_date": "2026-08-19", "k_mmol_L": 4.0})  # 无采样时刻
    _upsert("P0010", {"report_date": "2026-08-19",
                      "specimen_time": "2026-08-19T10:00:00", "k_mmol_L": 4.5})
    panels = harness.FAKE_REPO.get_panels("P0010")
    same_day = [p for p in panels if p["report_date"] == "2026-08-19"]
    assert same_day[0]["specimen_time"] is None, same_day
    assert same_day[-1]["specimen_time"] == "2026-08-19T10:00:00", same_day


def test_dual_unit_tolerance():
    """P2-03：双单位换算容差——取整级差异放行、真实口径冲突拒绝。"""
    from CKDNutri_clinical_data_mcp.core import _normalize
    from CKDNutri_clinical_data_mcp.models import LabResultIn

    # 整数单位（scr_umol_L）取整级差异：177 vs 2.0 mg/dL=176.8，差 0.2 < 0.5 下限 → 放行
    lab = LabResultIn(report_date="2026-08-19", scr_umol_L=177.0, scr_mg_dL=2.0)
    values, _ = _normalize(lab)
    assert values["scr_umol_L"] == 177.0, values
    # 真实口径冲突：3000 µmol/L vs 33.9 mg/dL=2996.76，差 3.24 > 3.0（0.1%×3000）→ 拒绝
    # （旧 1% 容差 30 会放行——正是本轮收紧点；33.9 在模型上限 33.93 内）
    lab2 = LabResultIn(report_date="2026-08-19", scr_umol_L=3000.0, scr_mg_dL=33.9)
    try:
        _normalize(lab2)
    except ValueError:
        pass
    else:
        raise AssertionError("3000 µmol/L vs 33.9 mg/dL（差 3.24 µmol/L）应被拒绝")
    # 小数单位（ca mmol/L）两位小数舍入放行、真实冲突拒绝
    lab3 = LabResultIn(report_date="2026-08-19", ca_mmol_L=2.4, ca_mg_dL=9.6)
    v3, _ = _normalize(lab3)
    assert v3["ca_mmol_L"] == 2.4, v3  # 9.6×0.2495=2.3952 差 0.0048 < 0.05
    lab4 = LabResultIn(report_date="2026-08-19", ca_mmol_L=2.4, ca_mg_dL=9.9)
    try:
        _normalize(lab4)
    except ValueError:
        pass
    else:
        raise AssertionError("2.4 vs 9.9 mg/dL（=2.47 mmol/L，差 0.07）应被拒绝")


def test_known_lis_patient_ids_union():
    """确认（回归固化）：Tablestore known_lis_patient_ids = 基线 ∪ 写库（P2-01 已修）。"""
    ids = harness.FAKE_REPO.known_lis_patient_ids()
    assert "P0001" in ids  # 基线患者
    _upsert("P0011", {"report_date": "2026-08-19", "k_mmol_L": 4.0})  # 写库新行
    ids2 = harness.FAKE_REPO.known_lis_patient_ids()
    assert "P0036" in ids2, "纯写库患者必须出现在 known_lis_patient_ids（双表并集）"


def test_verify_row_consistent():
    """P2-02：migrate 冲突一致性校验——值一致返回 None，被污染返回差异描述。"""
    from CKDNutri_clinical_data_mcp.migrate_tablestore import _verify_row_consistent

    src = {"sample_id": "S1", "report_date": "2026-08-01",
           "specimen": "静脉血", "values": {"k_mmol_L": 4.2}}
    assert _verify_row_consistent(src, dict(src)) is None  # 一致
    polluted = dict(src)
    polluted["values"] = {"k_mmol_L": 9.9}
    diff = _verify_row_consistent(src, polluted)
    assert diff is not None and "values" in diff, diff  # 被污染 → 报差异
    # Tablestore values 为 JSON 字符串列（反序列化后比较）
    ts_row = dict(src)
    ts_row["values"] = '{"k_mmol_L": 4.2}'
    assert _verify_row_consistent(src, ts_row) is None, ts_row


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"P1 CD2 REGRESSION OK（{len(fns)} 个用例）")
