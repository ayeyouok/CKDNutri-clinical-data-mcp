"""写入工具用例（upsert_lab_result）与通用铁律自查。"""

from __future__ import annotations

import ast
import json
import os
import re
import sys

from a207_policy import CallerUnknown, as_caller
from harness import PKG_ROOT, SRC, check

from CKDNutri_clinical_data_mcp import core, store
from CKDNutri_clinical_data_mcp.reference import ANALYTES, reference_interval

DENIED_WRITERS = ("parent_assistant", "nutritionist", "child_companion",
                  "risk_warning", "orchestrator", "unknown_agent")


@check("upsert_lab_result / doctor_assistant 写入成功且要求重评")
def _upsert_ok():
    with as_caller("doctor_assistant"):
        res = core.upsert_lab_result(
            "P0013", {"report_date": "2026-08-20", "k_mmol_L": 5.4, "p_mmol_L": 2.0, "hb_g_L": 108},
        )
    assert res["ok"] is True, res
    assert res["recommend_reevaluate"] is True, res
    data = res["data"]
    assert data["persisted"] is True
    assert data["store_record_count"] >= 1
    assert data["next_action"] == "a207-risk-rules-mcp.evaluate_risk_rules"
    assert store.store_path().exists(), "写库文件未落盘"
    payload = json.loads(store.store_path().read_text(encoding="utf-8"))
    assert payload["records"][-1]["patient_id"] == "P0013"
    assert payload["records"][-1]["recorded_by"] == "doctor_assistant"


@check("upsert 后 get_labs 能读到新采样且排在末位")
def _upsert_readback():
    with as_caller("doctor_assistant"):
        res = core.get_labs("P0013")
    last = res["data"]["panels"][-1]
    assert last["report_date"] == "2026-08-20", last
    assert last["source"] == "upsert", last
    analytes = {r["analyte"] for r in last["results"]}
    assert {"k_mmol_L", "p_mmol_L", "hb_g_L"} <= analytes, analytes


@check("upsert 后趋势序列纳入新点")
def _upsert_trend():
    trend = core.get_lab_trend("P0013", "k_mmol_L")["data"]
    assert trend["points"][-1]["report_date"] == "2026-08-20"
    assert trend["latest"] == 5.4, trend["latest"]


@check("upsert / write_mode=False 只校验不落盘")
def _upsert_dryrun():
    before = len(store.load_store())
    with as_caller("doctor_assistant"):
        res = core.upsert_lab_result(
            "P0014", {"report_date": "2026-08-21", "k_mmol_L": 4.8},
            write_mode=False,
        )
    assert res["ok"] is True and res["recommend_reevaluate"] is True, res
    assert res["data"]["persisted"] is False, res["data"]
    assert len(store.load_store()) == before, "回滚模式仍然写库了"


@check("upsert / 入口单位归一化 mg/dL -> 契约单位")
def _upsert_units():
    with as_caller("doctor_assistant"):
        res = core.upsert_lab_result(
            "P0015", {"report_date": "2026-08-22", "scr_mg_dL": 2.0, "ca_mg_dL": 9.0},
            write_mode=False,
        )
    assert res["ok"] is True, res
    written = res["data"]["analytes_written"]
    assert "scr_umol_L" in written and "ca_mmol_L" in written, written
    assert any("scr_mg_dL" in c for c in res["data"]["unit_conversions"])


@check("upsert / 契约字段与别名同时给出时以契约字段为准")
def _upsert_conflict():
    with as_caller("doctor_assistant"):
        res = core.upsert_lab_result(
            "P0015", {"report_date": "2026-08-22", "scr_umol_L": 200.0, "scr_mg_dL": 9.9},
            write_mode=False,
        )
    assert res["ok"] is True, res
    assert any("以契约字段" in c for c in res["data"]["unit_conversions"])


@check("upsert / 新值命中危急阈值时给出通知动作")
def _upsert_critical():
    with as_caller("doctor_assistant"):
        res = core.upsert_lab_result(
            "P0016", {"report_date": "2026-08-23", "k_mmol_L": 7.2},
            write_mode=False,
        )
    assert res["ok"] is True
    assert res["data"]["critical_count"] >= 1, res["data"]
    assert res["data"]["notify_action"] == "a207-notify-mcp.notify_critical_value"


@check("upsert / caller=parent_assistant 返回 FORBIDDEN")
def _upsert_parent():
    with as_caller("parent_assistant"):
        res = core.upsert_lab_result(
            "P0013", {"report_date": "2026-08-20", "k_mmol_L": 4.5},
        )
    assert res["ok"] is False, res
    assert res["error"] == "FORBIDDEN", res
    assert "parent_assistant" in res["detail"], res


@check("upsert / 写权仅医生助手，其余 caller 一律 FORBIDDEN")
def _upsert_others():
    for caller in DENIED_WRITERS:
        with as_caller(caller):
            res = core.upsert_lab_result(
                "P0013", {"report_date": "2026-08-20", "k_mmol_L": 4.5}
            )
        assert res["ok"] is False and res["error"] == "FORBIDDEN", (caller, res)


@check("upsert / 越权时不产生任何写入副作用")
def _upsert_no_side_effect():
    before = len(store.load_store())
    for caller in DENIED_WRITERS:
        with as_caller(caller):
            core.upsert_lab_result(
                "P0013", {"report_date": "2026-08-25", "k_mmol_L": 4.5}
            )
    assert len(store.load_store()) == before, "越权调用仍然改动了写库"


@check("upsert / 未知字段被白名单拦截")
def _upsert_extra():
    with as_caller("doctor_assistant"):
        res = core.upsert_lab_result(
            "P0013", {"report_date": "2026-08-20", "k_mmol_L": 4.5, "cholesterol": 5.0},
            write_mode=False,
        )
    assert res["ok"] is False and res["error"] == "INVALID_ARGUMENT", res


@check("upsert / 越界数值被拒")
def _upsert_range():
    with as_caller("doctor_assistant"):
        res = core.upsert_lab_result(
            "P0013", {"report_date": "2026-08-20", "k_mmol_L": 99},
            write_mode=False,
        )
    assert res["ok"] is False and res["error"] == "INVALID_ARGUMENT", res


@check("upsert / 空指标载荷被拒")
def _upsert_empty():
    with as_caller("doctor_assistant"):
        res = core.upsert_lab_result(
            "P0013", {"report_date": "2026-08-20"}, write_mode=False
        )
    assert res["ok"] is False and res["error"] == "INVALID_ARGUMENT", res


@check("upsert / 未知患者返回 NOT_FOUND")
def _upsert_404():
    with as_caller("doctor_assistant"):
        res = core.upsert_lab_result(
            "P8888", {"report_date": "2026-08-20", "k_mmol_L": 4.5},
        write_mode=False,
    )
    assert res["ok"] is False and res["error"] == "NOT_FOUND", res


@check("P0-1 fail-closed：身份未注入时任何工具一律拒绝")
def _caller_fail_closed():
    saved = os.environ.pop("A207_CALLER", None)
    try:
        for fn, args in (
            (core.get_labs, ("P0013",)),
            (core.get_critical_values, ("P0013",)),
            (core.get_lab_trend, ("P0013", "k_mmol_L")),
            (core.upsert_lab_result, ("P0013",)),
        ):
            try:
                fn(*args)
            except CallerUnknown:
                continue
            raise AssertionError(f"{fn.__name__} 在无身份注入时未 fail-closed")
    finally:
        if saved is not None:
            os.environ["A207_CALLER"] = saved


@check("P0-1 身份来源唯一：server 工具签名不得暴露 caller 形参")
def _server_no_caller_param():
    tree = ast.parse((SRC / "CKDNutri_clinical_data_mcp" / "server.py").read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        a = node.args
        names = [x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)]
        if "caller" in names:
            offenders.append(node.name)
    assert not offenders, f"server 工具层残留 caller 形参：{offenders}"


@check("零跨包引用：未 import 任何其他 a207-* 包")
def _no_cross_import():
    # Plan A：a207_policy 是统一策略共享包（非领域 MCP），允许引用。
    pattern = re.compile(r"(?:^|\s)(?:import|from)\s+a207_(?!lis_mcp|policy)", re.M)
    offenders = [
        str(p.relative_to(PKG_ROOT))
        for p in PKG_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts
        and pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"存在跨包引用：{offenders}"


@check("纯逻辑层不 import fastmcp（无 fastmcp 亦可单测）")
def _no_fastmcp():
    pattern = re.compile(r"(?:^|\s)(?:import|from)\s+fastmcp\b", re.M)
    pure = ("core.py", "models.py", "views.py", "store.py", "reference.py",
            "errors.py", "__init__.py")
    offenders = [
        name
        for name in pure
        if (SRC / "CKDNutri_clinical_data_mcp" / name).exists()
        and pattern.search((SRC / "CKDNutri_clinical_data_mcp" / name).read_text(encoding="utf-8"))
    ]
    assert not offenders, f"纯逻辑层引入了 fastmcp：{offenders}"
    assert "fastmcp" not in sys.modules, "测试进程意外加载了 fastmcp"


@check("每个指标都有儿童参考区间")
def _reference_complete():
    missing = [a for a in ANALYTES if reference_interval(a, 9.0, "M") is None]
    assert not missing, f"缺少儿童参考区间：{missing}"


@check("参考区间随年龄分带生效（血磷幼儿高于青少年）")
def _reference_age_aware():
    toddler = reference_interval("p_mmol_L", 2.0, "M")
    teen = reference_interval("p_mmol_L", 15.0, "M")
    assert toddler[1] > teen[1], (toddler, teen)
    boy = reference_interval("hb_g_L", 15.0, "M")
    girl = reference_interval("hb_g_L", 15.0, "F")
    assert boy != girl, (boy, girl)
