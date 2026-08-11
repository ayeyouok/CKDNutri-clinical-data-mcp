"""只读工具用例：数据集完整性、get_labs、get_critical_values、get_lab_trend。"""

from __future__ import annotations

import re

from a207_policy import as_caller

from harness import check, numeric_leak

from CKDNutri_clinical_data_mcp import core, his, store
from CKDNutri_clinical_data_mcp.reference import PARENT_VIEW_ANALYTES


def _parent_token(patient_id: str) -> str:
    """以医生助手身份为患儿签发监护人令牌（F4：家长受限视图须凭令牌核验）。"""
    with as_caller("doctor_assistant"):
        issued = his.issue_guardian_token(patient_id)
    assert issued["ok"], issued
    return issued["data"]["guardian_token"]


@check("数据集覆盖 >=30 例且 patient_id 合规")
def _dataset():
    listed = core.list_known_patients()
    assert listed["ok"] is True
    ids = listed["data"]["patient_ids"]
    assert len(ids) >= 30, f"病例数不足：{len(ids)}"
    bad = [i for i in ids if not re.match(r"^P[0-9]{4,}$", i)]
    assert not bad, f"不合规 id：{bad}"
    patients = store.load_dataset()["patients"]
    assert {p["ckd_stage"] for p in patients} == {"G2", "G3a", "G3b", "G4", "G5"}
    assert {p["dialysis"] for p in patients} == {
        "none",
        "hemodialysis",
        "peritoneal",
    }
    assert all(len(p["panels"]) >= 2 for p in patients), "存在不足 2 次采样的病例"


@check("数据集内部一致：分期与 eGFR 不矛盾")
def _stage_consistency():
    bands = {
        "G2": (60.0, 90.0),
        "G3a": (45.0, 60.0),
        "G3b": (30.0, 45.0),
        "G4": (15.0, 30.0),
        "G5": (0.0, 15.0),
    }
    bad = []
    for p in store.load_dataset()["patients"]:
        if p["dialysis"] != "none":
            continue
        low, high = bands[p["ckd_stage"]]
        for panel in p["panels"]:
            egfr = panel["values"]["egfr_ml_min"]
            if not (low <= egfr < high):
                bad.append((p["patient_id"], p["ckd_stage"], egfr))
    assert not bad, f"分期与 eGFR 矛盾：{bad[:5]}"


@check("get_labs / doctor_assistant 返回全量面板")
def _labs_doctor():
    with as_caller("doctor_assistant"):
        res = core.get_labs("P0013")
    assert res["ok"] is True, res
    data = res["data"]
    assert data["data_scope"] == "full"
    assert data["panel_count"] >= 2
    dates = [p["report_date"] for p in data["panels"]]
    assert dates == sorted(dates), "面板未按时间升序"
    first = data["panels"][0]["results"][0]
    for key in ("analyte", "value", "unit", "status", "ref_low", "ref_high"):
        assert key in first, f"缺字段 {key}"


@check("get_labs 单位口径与 PCP 契约一致")
def _units():
    with as_caller("doctor_assistant"):
        res = core.get_labs("P0013")
    units = {r["analyte"]: r["unit"] for r in res["data"]["panels"][-1]["results"]}
    expected = {
        "scr_umol_L": "umol/L",
        "k_mmol_L": "mmol/L",
        "p_mmol_L": "mmol/L",
        "ca_mmol_L": "mmol/L",
        "na_mmol_L": "mmol/L",
        "bun_mmol_L": "mmol/L",
        "albumin_g_L": "g/L",
        "hb_g_L": "g/L",
        "egfr_ml_min": "mL/min/1.73m2",
    }
    for analyte, unit in expected.items():
        assert units.get(analyte) == unit, f"{analyte} 单位错配：{units.get(analyte)}"


@check("get_labs / risk_warning 可读全量")
def _labs_risk():
    with as_caller("risk_warning"):
        assert core.get_labs("P0013")["ok"] is True


@check("get_labs / 营养师·患儿伙伴·调度 被拒")
def _labs_denied():
    for caller in ("nutritionist", "child_companion", "orchestrator", "unknown_agent"):
        with as_caller(caller):
            res = core.get_labs("P0013")
        assert res["ok"] is False and res["error"] == "FORBIDDEN", (caller, res)


@check("get_labs / parent_assistant 受限视图不含任何原始数值")
def _labs_parent():
    tok = _parent_token("P0013")
    with as_caller("parent_assistant"):
        res = core.get_labs("P0013", guardian_token=tok)
    assert res["ok"] is True, res
    data = res["data"]
    assert data["data_scope"] == "limited_parent"
    assert "panels" not in data, "受限视图不应给出全量面板"
    assert not numeric_leak(data), "受限视图泄露了数值"
    assert {i["analyte"] for i in data["items"]} == set(PARENT_VIEW_ANALYTES)
    for item in data["items"]:
        assert "value" not in item, "受限视图条目仍带 value 字段"
        assert item["trend"] in ("↑", "↓", "→"), item["trend"]
        assert isinstance(item["in_reference_range"], bool)


@check("get_labs / 受限视图对危急例标红但仍不给数值")
def _parent_critical():
    tok = _parent_token("P0028")
    with as_caller("parent_assistant"):
        res = core.get_labs("P0028", guardian_token=tok)
    assert res["ok"] is True
    assert res["data"]["has_critical"] is True, res["data"]
    assert not numeric_leak(res["data"]), "危急场景下受限视图泄露了数值"


@check("get_labs / 未知 patient_id 返回 NOT_FOUND")
def _labs_404():
    with as_caller("doctor_assistant"):
        res = core.get_labs("P9999")
    assert res["ok"] is False and res["error"] == "NOT_FOUND", res


@check("get_labs / 非法 patient_id 格式被拒")
def _labs_badid():
    with as_caller("doctor_assistant"):
        res = core.get_labs("abc")
    assert res["ok"] is False and res["error"] == "INVALID_ARGUMENT", res


@check("get_critical_values 命中高钾危急例 P0028")
def _crit_k():
    res = core.get_critical_values("P0028")
    assert res["ok"] is True, res
    assert res["data"]["critical_count"] >= 1, res["data"]
    rules = {i["rule"] for i in res["data"]["items"]}
    assert "CRIT-K-HIGH" in rules, rules
    hit = next(i for i in res["data"]["items"] if i["rule"] == "CRIT-K-HIGH")
    assert hit["value"] > 6.5 and hit["unit"] == "mmol/L", hit
    assert res["data"]["next_action"] == "a207-notify-mcp.notify_critical_value"


@check("get_critical_values 命中重度贫血 P0031 与低钙 P0023")
def _crit_others():
    hb = core.get_critical_values("P0031")
    assert "CRIT-HB-LOW" in {i["rule"] for i in hb["data"]["items"]}, hb["data"]
    ca = core.get_critical_values("P0023")
    assert "CRIT-CA-LOW" in {i["rule"] for i in ca["data"]["items"]}, ca["data"]


@check("get_critical_values 对平稳患者返回零命中")
def _crit_none():
    res = core.get_critical_values("P0001")
    assert res["ok"] is True and res["data"]["critical_count"] == 0, res["data"]
    assert res["data"]["next_action"] is None


@check("get_critical_values 家长助手无危急值通道权限")
def _crit_denied():
    with as_caller("parent_assistant"):
        res = core.get_critical_values("P0028")
    assert res["ok"] is False and res["error"] == "FORBIDDEN", res


@check("get_lab_trend 返回时序、环比与斜率")
def _trend():
    res = core.get_lab_trend("P0031", "scr_umol_L")
    assert res["ok"] is True, res
    data = res["data"]
    assert data["point_count"] >= 2
    assert data["unit"] == "umol/L"
    assert data["delta_pct"] is not None and data["slope_per_30d"] is not None
    assert data["direction"] in ("up", "down", "flat")
    dates = [p["report_date"] for p in data["points"]]
    assert dates == sorted(dates)


@check("get_lab_trend 斜率方向与实际序列一致")
def _trend_slope_sign():
    res = core.get_lab_trend("P0031", "hb_g_L")
    points = [p["value"] for p in res["data"]["points"]]
    slope = res["data"]["slope_per_30d"]
    assert (points[-1] - points[0] < 0) == (slope < 0), (points, slope)


@check("get_lab_trend window_days 生效")
def _trend_window():
    full = core.get_lab_trend("P0031", "hb_g_L")["data"]["point_count"]
    narrow = core.get_lab_trend("P0031", "hb_g_L", window_days=60)["data"]["point_count"]
    assert 1 <= narrow <= full, (full, narrow)


@check("get_lab_trend 家长助手无全量趋势权限")
def _trend_parent():
    with as_caller("parent_assistant"):
        res = core.get_lab_trend("P0013", "k_mmol_L")
    assert res["ok"] is False and res["error"] == "FORBIDDEN", res


@check("get_lab_trend 未知指标被拒")
def _trend_bad():
    res = core.get_lab_trend("P0013", "potassium")
    assert res["ok"] is False and res["error"] == "INVALID_ARGUMENT", res
