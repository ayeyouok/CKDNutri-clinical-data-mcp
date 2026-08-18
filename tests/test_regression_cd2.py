# -*- coding: utf-8 -*-
"""十五审（2026-08-18）clinical-data-mcp 复审回归：P1-01~04 / P2-01~06。

用例覆盖（Fake Tablestore / 内存，不连真实 OTS）：
- P1-01 JSON 后端启动不校验 OTS；未知后端 SystemExit(1)
- P1-02 历史危急值按报告日 < 最新日过滤（同日多采样不计入历史）
- P1-03 确定性幂等键纳入 specimen_time（同日同值不同时刻 → 两条；同刻重试 → 幂等）
- P1-04 双单位并存：换算一致放行 / 矛盾拒绝 INVALID_INPUT
- P2-01 known_lis_patient_ids = 基线 ∪ 写库（Tablestore 双表 + LocalJson 语义）
- P2-02 未知 backend fail-fast（RuntimeError）
- P2-03 采样时年龄推算以数据集 as_of 为基准（快照日采样 = 快照年龄）
- P2-04 delta_pct 与 trend_code 同 epsilon 口径（近零前值 → None + flat）
- P2-05 age_band 非法年龄 fail-closed（-1/150/NaN/bool → ValueError）
- P2-06 令牌库逐条目 Schema 校验（损坏条目 → RuntimeError）
"""
import os
os.environ.setdefault("A207_ENV", "test")  # N-SEC-1（2026-08-14）：测试进程显式声明测试环境（守卫 fail-closed 默认拒绝）
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")  # 生产护栏（2026-08-15）：测试进程显式确认 json 后端为开发模式
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
_POLICY = Path(__file__).resolve().parents[1].parents[1] / "a207-policy" / "src"
for p in (_SRC, _POLICY):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
os.environ.setdefault("A207_CALLER", "doctor_assistant")

import harness  # noqa: E402,F401  必须最先导入：注入 Fake Tablestore 后才能 import core
from a207_policy import as_caller  # noqa: E402

from CKDNutri_clinical_data_mcp import core, his, repository as repo, views  # noqa: E402
from CKDNutri_clinical_data_mcp.reference import age_band  # noqa: E402

_TODAY = date.today().isoformat()


def test_p101_json_backend_main_skips_ots_check():
    """P1-01：A207_STORAGE_BACKEND=json 时 main() 不校验 OTS（本地开发闭环）；
    未知后端（jsno）SystemExit(1) fail-fast。"""
    from unittest import mock

    from CKDNutri_clinical_data_mcp import server

    old_backend = os.environ.get("A207_STORAGE_BACKEND")
    try:
        os.environ["A207_STORAGE_BACKEND"] = "json"
        os.environ["A207_ACCEPT_DEV_STORAGE"] = "1"
        with mock.patch.object(server.mcp, "run", lambda: None):
            server.main()  # JSON 模式不应抛 SystemExit（旧代码会撞 OTS 自检失败）
        os.environ["A207_STORAGE_BACKEND"] = "jsno"  # 拼写错误
        try:
            server.main()
        except SystemExit as exc:
            assert exc.code == 1, exc
        else:
            raise AssertionError("未知后端应 SystemExit(1)（fail-fast）")
    finally:
        if old_backend is None:
            os.environ.pop("A207_STORAGE_BACKEND", None)
        else:
            os.environ["A207_STORAGE_BACKEND"] = old_backend
        os.environ.pop("A207_ACCEPT_DEV_STORAGE", None)


def test_p102_critical_history_excludes_same_day():
    """P1-02：同日多采样时，当日危急值不再计入历史（prior_panels_with_critical
    仅含 report_date < 最新报告日的面板）。"""
    with as_caller("doctor_assistant"):
        a = core.upsert_lab_result(
            "P0004", {"report_date": _TODAY, "k_mmol_L": 7.0, "sample_id": "SD-K7"},
            write_mode=True)
        b = core.upsert_lab_result(
            "P0004", {"report_date": _TODAY, "k_mmol_L": 5.0, "sample_id": "SD-K5"},
            write_mode=True)
    assert a["ok"] is True, a
    assert b["ok"] is True, b
    r = core.get_critical_values("P0004")
    assert r["ok"] is True, r
    assert r["data"]["critical_count"] >= 1, r["data"]  # 晨危急仍被当前扫描命中
    assert all(h["report_date"] < _TODAY for h in r["data"]["prior_panels_with_critical"]), \
        r["data"]["prior_panels_with_critical"]


def test_p103_specimen_time_distinguishes_real_redraws():
    """P1-03：无显式 sample_id 时幂等键纳入 specimen_time——同日同值不同采样时刻
    生成不同 sample_id（真实重抽不再被误吞）；同 specimen_time 同值重试仍幂等。"""
    with as_caller("doctor_assistant"):
        a = core.upsert_lab_result(
            "P0005", {"report_date": _TODAY, "k_mmol_L": 5.2,
                      "specimen_time": "2026-08-18T08:00:00+08:00"}, write_mode=True)
        b = core.upsert_lab_result(
            "P0005", {"report_date": _TODAY, "k_mmol_L": 5.2,
                      "specimen_time": "2026-08-18T14:00:00+08:00"}, write_mode=True)
    assert a["ok"] is True, a
    assert b["ok"] is True, b
    sa, sb = a["data"]["sample_id"], b["data"]["sample_id"]
    assert sa != sb, "不同采样时刻的同值采样必须生成不同 sample_id（修复前被当重复吞掉）"
    # 同 specimen_time 重试 → 幂等命中同一 id
    with as_caller("doctor_assistant"):
        c = core.upsert_lab_result(
            "P0005", {"report_date": _TODAY, "k_mmol_L": 5.2,
                      "specimen_time": "2026-08-18T08:00:00+08:00"}, write_mode=True)
    assert c["ok"] is True, c
    assert c["data"]["sample_id"] == sa, "同 specimen_time 重试应幂等命中同 id"


def test_p104_dual_unit_consistency():
    """P1-04：契约单位与别名单位并存必须换算一致——矛盾拒绝 INVALID_INPUT、
    一致放行（入口级，write_mode=False 不落盘）。"""
    with as_caller("doctor_assistant"):
        bad = core.upsert_lab_result(
            "P0006", {"report_date": _TODAY, "scr_umol_L": 200.0, "scr_mg_dL": 9.9},
            write_mode=False)
        good = core.upsert_lab_result(
            "P0006", {"report_date": _TODAY, "scr_umol_L": 176.8, "scr_mg_dL": 2.0},
            write_mode=False)
    assert bad["ok"] is False and bad["error"] == "INVALID_INPUT", bad
    assert good["ok"] is True, good
    assert any("换算一致" in c for c in good["data"]["unit_conversions"]), good


def test_p201_known_lis_patient_ids_union():
    """P2-01：known_lis_patient_ids = 基线 ∪ 写库（Tablestore 双表并集）——
    向 labs_store 注入基线外患者后必须出现在列表中。"""
    base = set(core.list_known_patients()["data"]["patient_ids"])
    assert len(base) >= 30
    new_pid = "P9901"
    assert new_pid not in base
    # 直接向 Fake Tablestore labs_store 注入一条新患者行（绕过 upsert 的存在性检查）
    client = harness.FAKE_REPO._client
    client.put_row(
        repo.TABLE_LABS_STORE,
        harness._FakeRow([("patient_id", new_pid), ("sample_id", "S-X1")],
                         {"report_date": _TODAY, "values": "{}", "source": "upsert"}),
        None)
    after = set(core.list_known_patients()["data"]["patient_ids"])
    assert new_pid in after, "写库患者必须计入 known_lis_patient_ids（修复前仅扫 TABLE_LABS）"


def test_p202_unknown_backend_fail_fast():
    """P2-02：A207_STORAGE_BACKEND 非白名单值 → RuntimeError（不再静默走 Tablestore）。"""
    old_backend = os.environ.get("A207_STORAGE_BACKEND")
    harness._REPO_PATCH.stop()  # 撤掉 Fake 注入，测真实 get_repository
    try:
        os.environ["A207_STORAGE_BACKEND"] = "jsno"
        try:
            repo.get_repository()
        except RuntimeError:
            pass
        else:
            raise AssertionError("未知后端 jsno 应 RuntimeError（fail-fast）")
        # 白名单值正常解析（tablestore 缺 OTS 参数会抛连接错误——非 RuntimeError 白名单问题）
        os.environ["A207_STORAGE_BACKEND"] = "tablestore"
        try:
            repo.get_repository()
        except RuntimeError as exc:
            assert "非法" not in str(exc), exc  # 白名单值不得命中"非法后端"分支
    finally:
        harness._REPO_PATCH.start()
        if old_backend is None:
            os.environ.pop("A207_STORAGE_BACKEND", None)
        else:
            os.environ["A207_STORAGE_BACKEND"] = old_backend


def test_p203_eff_age_anchored_to_as_of():
    """P2-03：采样时年龄以数据集 as_of 为基准——基准日采样 = 快照年龄（不再被
    (today - as_of) 漂移低估）；晚于基准日 10 天的采样年龄自快照向前生长。"""
    as_of = views._dataset_as_of()
    assert as_of is not None, "数据集应含 as_of"
    p1 = views.decorate_panel(
        {"sample_id": "S-ASOF", "report_date": as_of.isoformat(),
         "values": {"hb_g_L": 120.0}}, age=8.0, sex="F")
    assert p1["age_at_report"] == 8.0, p1["age_at_report"]  # 修复前为 8 - (today-as_of)/365.25
    # 晚于基准日但未到未来：年龄自快照向前生长
    after10 = as_of + timedelta(days=10)
    if after10 < date.today():  # as_of 距今 <10 天时该分支降级（数据构造边界），跳过
        p2 = views.decorate_panel(
            {"sample_id": "S-UP", "report_date": after10.isoformat(),
             "values": {"hb_g_L": 120.0}}, age=8.0, sex="F")
        assert 8.0 < p2["age_at_report"] < 8.1, p2["age_at_report"]


def test_p204_delta_pct_epsilon_aligned():
    """P2-04：前值近零（< _EPSILON）时 delta_pct=None 且 direction=flat——
    不再输出天文数字百分比与方向判 flat 的矛盾状态。"""
    trend = views.build_trend(
        [
            {"sample_id": "S1", "report_date": "2026-07-01", "values": {"k_mmol_L": 1e-12}},
            {"sample_id": "S2", "report_date": "2026-08-01", "values": {"k_mmol_L": 2e-12}},
        ],
        "k_mmol_L", age=8.0, sex="F")
    assert trend["delta_pct"] is None, trend["delta_pct"]
    assert trend["direction"] == "flat", trend


def test_p205_age_band_invalid_fail_closed():
    """P2-05：age_band 对非法年龄 fail-closed——-1/150/NaN/bool 一律 ValueError
    （不再静默兜底 adolescent）。"""
    for bad in (-1.0, 150.0, float("nan"), True, "8"):
        try:
            age_band(bad)
        except ValueError:
            continue
        raise AssertionError(f"age_band({bad!r}) 未拒绝（修复前静默 adolescent）")
    assert age_band(8.0) == "school"
    assert age_band(13.0) == "adolescent"


def test_p206_token_store_entry_schema():
    """P2-06：令牌库逐条目 Schema 校验——损坏条目（token 非 str / expires_at 垃圾）
    拒绝加载（fail-closed），不再静默载入后行为不透明。"""
    import json

    p = his._guardian_store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(json.dumps({"P0001": {"token": 123}}), encoding="utf-8")
        try:
            his._load_guardian_tokens()
        except RuntimeError:
            pass
        else:
            raise AssertionError("token 非 str 条目未被拒绝")
        p.write_text(json.dumps(
            {"P0001": {"token": "abc", "expires_at": "not-a-date"}}), encoding="utf-8")
        try:
            his._load_guardian_tokens()
        except RuntimeError:
            pass
        else:
            raise AssertionError("expires_at 垃圾串条目未被拒绝")
    finally:
        try:
            os.remove(p)
        except OSError:
            pass
