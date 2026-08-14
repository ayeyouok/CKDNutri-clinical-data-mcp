"""只读工具用例：数据集完整性、get_labs、get_critical_values、get_lab_trend。"""

from __future__ import annotations
import os
os.environ.setdefault("A207_ENV", "test")  # N-SEC-1（2026-08-14）：测试进程显式声明测试环境（守卫 fail-closed 默认拒绝）

import re

from a207_policy import CallerUnknown, as_caller

from harness import check

from CKDNutri_clinical_data_mcp import core, his, store


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


@check("get_labs / 营养师·患儿伙伴·调度 被拒（N-CALLER-1：未知角色身份层即拒）")
def _labs_denied():
    for caller in ("nutritionist", "child_companion", "orchestrator", "unknown_agent"):
        try:
            with as_caller(caller):
                res = core.get_labs("P0013")
        except CallerUnknown:
            continue  # N-CALLER-1（2026-08-14）：白名单外身份在 get_caller 即拒（更早、更强）
        assert res["ok"] is False and res["error"] == "FORBIDDEN", (caller, res)


@check("get_labs / parent_assistant 可见完整化验数值（2026-08-13 用户决策：知情权）")
def _labs_parent():
    tok = _parent_token("P0013")
    with as_caller("parent_assistant"):
        res = core.get_labs("P0013", guardian_token=tok)
    assert res["ok"] is True, res
    data = res["data"]
    assert data["data_scope"] == "parent_full"
    assert data["panel_count"] >= 2
    assert "panels" in data, "家长应可见完整面板（含数值）"
    first = data["panels"][0]["results"][0]
    for key in ("analyte", "value", "unit", "status", "ref_low", "ref_high"):
        assert key in first, f"缺字段 {key}"


@check("get_labs / 危急例家长可见数值并标注危急")
def _parent_critical():
    tok = _parent_token("P0028")
    with as_caller("parent_assistant"):
        res = core.get_labs("P0028", guardian_token=tok)
    assert res["ok"] is True
    assert res["data"]["data_scope"] == "parent_full"
    crit = [r for p in res["data"]["panels"] for r in p["results"]
            if r["status"].startswith("critical")]
    assert crit, "危急例应有危急标注项"
    assert all("value" in r for r in crit), "危急项应带数值"


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


@check("get_critical_values 家长助手可见命中明细（2026-08-13 用户决策：知情权）")
def _crit_parent():
    tok = _parent_token("P0028")
    with as_caller("parent_assistant"):
        res = core.get_critical_values("P0028", guardian_token=tok)
    assert res["ok"] is True, res
    data = res["data"]
    assert data["data_scope"] == "parent_full"
    assert data["critical_count"] >= 1
    assert data["items"], "家长应可见危急明细"


@check("get_critical_values 家长助手缺 guardian_token 拒绝")
def _crit_parent_no_token():
    with as_caller("parent_assistant"):
        res = core.get_critical_values("P0028")
    assert res["ok"] is False and res["error"] == "GUARDIAN_UNVERIFIED", res


@check("get_critical_values 家长助手对平稳患者返回零命中")
def _crit_parent_none():
    tok = _parent_token("P0001")
    with as_caller("parent_assistant"):
        res = core.get_critical_values("P0001", guardian_token=tok)
    assert res["ok"] is True
    assert res["data"]["critical_count"] == 0
    assert res["data"]["items"] == []


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


@check("get_lab_trend 家长助手可见完整数值趋势（2026-08-13 用户决策：知情权）")
def _trend_parent():
    tok = _parent_token("P0013")
    with as_caller("parent_assistant"):
        res = core.get_lab_trend("P0013", "k_mmol_L", guardian_token=tok)
    assert res["ok"] is True, res
    data = res["data"]
    assert data["data_scope"] == "parent_full"
    assert data["point_count"] >= 2
    assert all("value" in p for p in data["points"]), "家长趋势应含数值"
    assert data["direction"] in ("up", "down", "flat"), data


@check("get_lab_trend 家长助手缺 guardian_token 拒绝")
def _trend_parent_no_token():
    with as_caller("parent_assistant"):
        res = core.get_lab_trend("P0013", "k_mmol_L")
    assert res["ok"] is False and res["error"] == "GUARDIAN_UNVERIFIED", res


@check("get_lab_trend 未知指标被拒")
def _trend_bad():
    res = core.get_lab_trend("P0013", "potassium")
    assert res["ok"] is False and res["error"] == "INVALID_ARGUMENT", res


@check("list_patients 分页语义（医生端量级防护，2026-08-13）")
def _list_patients_pagination():
    import math

    with as_caller("doctor_assistant"):
        r = his.list_patients()
    d = r["data"]
    # 默认页：50 条/页；36 患儿 < 50 → 一页取完 has_more=False
    assert d["count"] == d["total_matched"] == 36, (d["count"], d["total_matched"])
    assert d["page"] == 1 and d["page_size"] == 50 and d["has_more"] is False
    assert d["patient_ids"] == sorted(d["patient_ids"]), "应按 patient_id 升序（跨页稳定）"
    sample = d["patients"][0]
    assert {"patient_id", "age_years", "age_band", "sex", "ckd_stage",
            "dialysis", "primary_disease", "has_allergies"} <= set(sample), sample.keys()

    # 小页多页：page_size=10 → 4 页（36=10+10+10+6），页间无重叠
    r1 = his.list_patients(page_size=10)
    r2 = his.list_patients(page=4, page_size=10)
    d1, d2 = r1["data"], r2["data"]
    assert d1["count"] == 10 and d1["has_more"] is True
    assert d2["count"] == 6 and d2["has_more"] is False
    assert not set(d1["patient_ids"]) & set(d2["patient_ids"])

    # 越界页 → 空页（不报错）
    r3 = his.list_patients(page=99, page_size=10)
    assert r3["data"]["count"] == 0 and r3["data"]["has_more"] is False

    # 非法分页参数 → INVALID_FILTER
    for bad_page, bad_size in ((0, 10), (1.5, 10), ("1", 10), (1, 0), (1, True)):
        rr = his.list_patients(page=bad_page, page_size=bad_size)
        assert rr.get("error") == "INVALID_FILTER", (bad_page, bad_size, rr)

    # page_size 超上限钳制 200
    assert his.list_patients(page_size=9999)["data"]["page_size"] == 200

    # filter + 分页组合：G3a（6 例）→ 首尾页
    d5 = his.list_patients(filter={"ckd_stage": "G3a"}, page_size=5)["data"]
    assert d5["total_matched"] == 6 and d5["count"] == 5 and d5["has_more"] is True
    assert all(p["ckd_stage"] == "G3a" for p in d5["patients"])
    last = math.ceil(6 / 5)
    dl = his.list_patients(filter={"ckd_stage": "G3a"}, page=last, page_size=5)["data"]
    assert dl["has_more"] is False and dl["count"] == 1

    # 家长（非 cohort 角色）→ FORBIDDEN（越权回归）
    with as_caller("parent_assistant"):
        denied = his.list_patients()
    assert denied["ok"] is False and denied["error"] == "FORBIDDEN", denied


@check("B2 / 家长视图键集合 ⊆ 白名单（deny-list 防新增敏感字段静默泄露）")
def _b2_parent_allowlist():
    """契约测试：家长受限视图返回的 JSON 键必须落在显式白名单内。

    若未来新增医生专有字段而开发者忘记加入 CLINICIAN_ONLY_FIELDS，黑名单脱敏会
    把它原样透给家长视图——本测试在 CI 卡死该回归（红），倒逼字段显式声明。
    """
    from a207_policy import CLINICIAN_ONLY_FIELDS, P1_PARENT_HIDDEN_FIELDS

    # 家长视图允许出现的顶层键（白名单）。与 _strip_parent_sensitive 的黑名单互补：
    # 黑名单保证"已知敏感键必剥"，白名单保证"未声明的键必拒"。
    PARENT_TOP_ALLOWED = {
        "patient_id", "data_scope", "as_of", "units", "demographics", "diagnosis",
        "allergies", "diet_restrictions", "nutrition_ceiling", "withheld",
        "withheld_reason", "ckd_stage", "ckd_stage_numeric", "primary_disease",
        "dialysis", "dialysis_mode", "dialysis_label", "diagnosed_on",
        "note", "age_years", "age_band", "age_band_label", "sex",
        "height_cm", "weight_kg", "bmi", "edema",
    }
    tok = _parent_token("P0013")
    with as_caller("parent_assistant"):
        prof = his.get_patient_profile("P0013", guardian_token=tok)
        diag = his.get_diagnosis("P0013", guardian_token=tok)
    assert prof["ok"] is True and diag["ok"] is True, (prof, diag)

    # ① 家长档案视图顶层键 ⊆ 白名单
    leaked_top = set(prof["data"]) - PARENT_TOP_ALLOWED
    assert not leaked_top, f"家长档案视图出现未声明顶层键（可能为新敏感字段泄露）：{leaked_top}"

    # ② 递归收集家长视图全部叶子键，断言不含任何 CLINICIAN_ONLY_FIELDS 键
    def _leaves(node):
        found = set()
        if isinstance(node, dict):
            found.update(node.keys())
            for v in node.values():
                found |= _leaves(v)
        elif isinstance(node, list):
            for v in node:
                found |= _leaves(v)
        return found

    leaves = _leaves(prof["data"]) | _leaves(diag["data"])
    # nutrition_ceiling 是家长刻意放行字段（家长须知晓医生设定上限），从断言集合剔除
    must_not_see = (CLINICIAN_ONLY_FIELDS | P1_PARENT_HIDDEN_FIELDS) - {"nutrition_ceiling"}
    leaked = leaves & must_not_see
    assert not leaked, (
        f"家长视图泄露医生专有字段（CLINICIAN_ONLY_FIELDS/P1_PARENT_HIDDEN_FIELDS）："
        f"{sorted(leaked)}。新增敏感字段必须显式加入 a207_policy 脱敏集合（单一事实源）。")

    # ③ 核心叶子逐一断言（显式点名，防集合误配）
    for key in ("scr_umol_L", "egfr_ml_min", "z_score_height", "stage_confirmed_by",
                "food_diary_5d", "biochemistry", "medical_record_no", "dialysis_detail"):
        assert key not in leaves, f"家长视图泄露字段 {key}"


@check("S6 / 畸形 patient_id 在所有 HIS 工具入口被拒（INVALID_ARGUMENT）")
def _s6_patient_id_validation():
    """S6 修复回归：畸形 patient_id 不得进入存储/查询层。

    此前 get_patient_profile / get_diagnosis / verify_guardian_binding /
    get_nutrition_ceiling 未校验，畸形 id 直插 repository；现统一走
    a207_policy.validate_patient_id（^P[0-9]{4,}$）。
    """
    bad_ids = ("", "  ", "P12", "P123", "p0007", "P0007X", "X0001", "1234",
               "P-001", None, 12345)
    with as_caller("doctor_assistant"):
        for bad in bad_ids:
            for tool in (his.get_patient_profile, his.get_diagnosis,
                         his.get_nutrition_ceiling):
                res = tool(bad)
                assert res.get("error") == "INVALID_ARGUMENT", (tool.__name__, bad, res)
            res = his.verify_guardian_binding(bad, "tok")
            assert res.get("error") == "INVALID_ARGUMENT", ("verify", bad, res)
            res = his.issue_guardian_token(bad)
            assert res.get("error") == "INVALID_ARGUMENT", ("issue", bad, res)
    # 合法 id 不受影响
    with as_caller("doctor_assistant"):
        assert his.get_patient_profile("P0013")["ok"] is True
        assert his.verify_guardian_binding("P0013", "tok")["ok"] is True
        assert his.issue_guardian_token("P0013")["ok"] is True
