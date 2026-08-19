"""写入工具用例（upsert_lab_result）与通用铁律自查。"""

from __future__ import annotations

import ast
import os

os.environ.setdefault("A207_ENV", "test")  # N-SEC-1（2026-08-14）：测试进程显式声明测试环境（守卫 fail-closed 默认拒绝）
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")  # 生产护栏（2026-08-15）：测试进程显式确认 json 后端为开发模式
import re
from datetime import date

from a207_policy import CallerUnknown, as_caller
from harness import PKG_ROOT, SRC, check, fake_labs_store_count, fake_labs_store_records

from CKDNutri_clinical_data_mcp import core
from CKDNutri_clinical_data_mcp.reference import ANALYTES, reference_interval

DENIED_WRITERS = ("parent_assistant", "nutritionist", "child_companion",
                  "risk_warning", "orchestrator", "unknown_agent")

# BUG-67 后补（2026-08-12）：report_date 校验拒绝未来日期后，测试夹具改用动态"今天"——
# 基线数据最新 2026-08-01，今天(>=08-12) 仍排在末位，测试语义不变；避免硬编码未来日期被校验拦截。
_TODAY = date.today().isoformat()


@check("upsert_lab_result / doctor_assistant 写入成功且要求重评")
def _upsert_ok():
    with as_caller("doctor_assistant"):
        res = core.upsert_lab_result(
            "P0013", {"report_date": _TODAY, "k_mmol_L": 5.4, "p_mmol_L": 2.0, "hb_g_L": 108},
        )
    assert res["ok"] is True, res
    # 九审（2026-08-16）：recommend_reevaluate 收进 data（信封契约 {ok, data} 统一）
    assert res["data"]["recommend_reevaluate"] is True, res
    data = res["data"]
    assert data["persisted"] is True
    assert data["store_record_count"] >= 1
    assert data["next_action"] == "CKDNutri-assessment-mcp.assess_clinical_status_tool"  # F-3：真实工具名
    # v3.0：写库落在 Tablestore labs_store（Fake 客户端），不再有 JSON 文件
    records = fake_labs_store_records()
    assert records, "Tablestore labs_store 无写入"
    assert records[-1].get("source") == "upsert"
    assert records[-1].get("recorded_by") == "doctor_assistant"


@check("upsert 后 get_labs 能读到新采样且排在末位")
def _upsert_readback():
    with as_caller("doctor_assistant"):
        res = core.get_labs("P0013")
    last = res["data"]["panels"][-1]
    assert last["report_date"] == _TODAY, last
    assert last["source"] == "upsert", last
    analytes = {r["analyte"] for r in last["results"]}
    assert {"k_mmol_L", "p_mmol_L", "hb_g_L"} <= analytes, analytes


@check("upsert 后趋势序列纳入新点")
def _upsert_trend():
    trend = core.get_lab_trend("P0013", "k_mmol_L")["data"]
    assert trend["points"][-1]["report_date"] == _TODAY
    assert trend["latest"] == 5.4, trend["latest"]


@check("upsert / write_mode=False 只校验不落盘")
def _upsert_dryrun():
    before = fake_labs_store_count()
    with as_caller("doctor_assistant"):
        res = core.upsert_lab_result(
            "P0014", {"report_date": _TODAY, "k_mmol_L": 4.8},
            write_mode=False,
        )
    assert res["ok"] is True and res["data"]["recommend_reevaluate"] is True, res
    assert res["data"]["persisted"] is False, res["data"]
    assert fake_labs_store_count() == before, "回滚模式仍然写库了"


@check("upsert / 入口单位归一化 mg/dL -> 契约单位")
def _upsert_units():
    with as_caller("doctor_assistant"):
        res = core.upsert_lab_result(
            "P0015", {"report_date": _TODAY, "scr_mg_dL": 2.0, "ca_mg_dL": 9.0},
            write_mode=False,
        )
    assert res["ok"] is True, res
    written = res["data"]["analytes_written"]
    assert "scr_umol_L" in written and "ca_mmol_L" in written, written
    assert any("scr_mg_dL" in c for c in res["data"]["unit_conversions"])


@check("upsert / 契约字段与别名同时给出：换算一致放行，矛盾拒绝（P1-04）")
def _upsert_conflict():
    # P1-04（2026-08-18）：双单位并存必须换算一致——此前直接以契约字段为准，
    # 矛盾值（如 scr_umol_L=200 与 scr_mg_dL=9.9→875.16）静默写错；现矛盾 → INVALID_INPUT。
    with as_caller("doctor_assistant"):
        bad = core.upsert_lab_result(
            "P0015", {"report_date": _TODAY, "scr_umol_L": 200.0, "scr_mg_dL": 9.9},
            write_mode=False,
        )
    assert bad["ok"] is False and bad["error"] == "INVALID_INPUT", bad
    # 一致（2.0 mg/dL = 176.8 umol/L）→ 放行并标注换算一致
    with as_caller("doctor_assistant"):
        good = core.upsert_lab_result(
            "P0015", {"report_date": _TODAY, "scr_umol_L": 176.8, "scr_mg_dL": 2.0},
            write_mode=False,
        )
    assert good["ok"] is True, good
    assert any("换算一致" in c for c in good["data"]["unit_conversions"]), good


@check("upsert / 新值命中危急阈值时给出通知动作")
def _upsert_critical():
    with as_caller("doctor_assistant"):
        res = core.upsert_lab_result(
            "P0016", {"report_date": _TODAY, "k_mmol_L": 7.2},
            write_mode=False,
        )
    assert res["ok"] is True
    assert res["data"]["critical_count"] >= 1, res["data"]
    assert res["data"]["notify_action"] == "CKDNutri-care-mcp.trigger_event_notification_tool"  # F-3：真实工具名


@check("upsert / caller=parent_assistant 返回 FORBIDDEN")
def _upsert_parent():
    with as_caller("parent_assistant"):
        res = core.upsert_lab_result(
            "P0013", {"report_date": _TODAY, "k_mmol_L": 4.5},
        )
    assert res["ok"] is False, res
    assert res["error"] == "FORBIDDEN", res
    assert "parent_assistant" in res["detail"], res


@check("upsert / 写权仅医生助手，其余 caller 一律拒绝（白名单内 FORBIDDEN / 白名单外 CallerUnknown）")
def _upsert_others():
    for caller in DENIED_WRITERS:
        try:
            with as_caller(caller):
                res = core.upsert_lab_result(
                    "P0013", {"report_date": _TODAY, "k_mmol_L": 4.5}
                )
        except CallerUnknown:
            continue  # N-CALLER-1（2026-08-14）：白名单外身份（nutritionist 等）身份层即拒
        assert res["ok"] is False and res["error"] == "FORBIDDEN", (caller, res)


@check("upsert / 越权时不产生任何写入副作用")
def _upsert_no_side_effect():
    before = fake_labs_store_count()
    for caller in DENIED_WRITERS:
        try:
            with as_caller(caller):
                core.upsert_lab_result(
                    "P0013", {"report_date": _TODAY, "k_mmol_L": 4.5}
                )
        except CallerUnknown:
            continue  # N-CALLER-1：白名单外身份被拒，同样无副作用
    assert fake_labs_store_count() == before, "越权调用仍然改动了写库"


@check("upsert / 未知字段被白名单拦截")
def _upsert_extra():
    with as_caller("doctor_assistant"):
        res = core.upsert_lab_result(
            "P0013", {"report_date": _TODAY, "k_mmol_L": 4.5, "cholesterol": 5.0},
            write_mode=False,
        )
    assert res["ok"] is False and res["error"] == "INVALID_INPUT", res


@check("upsert / 越界数值被拒")
def _upsert_range():
    with as_caller("doctor_assistant"):
        res = core.upsert_lab_result(
            "P0013", {"report_date": _TODAY, "k_mmol_L": 99},
            write_mode=False,
        )
    assert res["ok"] is False and res["error"] == "INVALID_INPUT", res


@check("upsert / NaN/Inf 化验值被拒且无写入副作用（S4：NaN 穿透范围校验的回归防线）")
def _upsert_nan():
    before = fake_labs_store_count()
    for bad in (float("nan"), float("inf"), float("-inf")):
        with as_caller("doctor_assistant"):
            res = core.upsert_lab_result(
                "P0013", {"report_date": _TODAY, "k_mmol_L": bad},
                write_mode=False,
            )
        assert res["ok"] is False and res["error"] == "INVALID_INPUT", (bad, res)
    assert fake_labs_store_count() == before, "NaN/Inf 写入产生了副作用"


@check("upsert / 空指标载荷被拒")
def _upsert_empty():
    with as_caller("doctor_assistant"):
        res = core.upsert_lab_result(
            "P0013", {"report_date": _TODAY}, write_mode=False
        )
    assert res["ok"] is False and res["error"] == "INVALID_INPUT", res


@check("upsert / 未知患者返回 NOT_FOUND")
def _upsert_404():
    with as_caller("doctor_assistant"):
        res = core.upsert_lab_result(
            "P8888", {"report_date": _TODAY, "k_mmol_L": 4.5},
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
    pattern = re.compile(r"(?:^|\s)(?:import|from)\s+a207_(?!lis_mcp|policy)", re.MULTILINE)
    offenders = [
        str(p.relative_to(PKG_ROOT))
        for p in PKG_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts
        and pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"存在跨包引用：{offenders}"


@check("纯逻辑层不 import fastmcp（无 fastmcp 亦可单测）")
def _no_fastmcp():
    pattern = re.compile(r"(?:^|\s)(?:import|from)\s+fastmcp\b", re.MULTILINE)
    pure = ("core.py", "models.py", "views.py", "store.py", "reference.py",
            "errors.py", "__init__.py")
    offenders = [
        name
        for name in pure
        if (SRC / "CKDNutri_clinical_data_mcp" / name).exists()
        and pattern.search((SRC / "CKDNutri_clinical_data_mcp" / name).read_text(encoding="utf-8"))
    ]
    # 十二审（2026-08-17）：移除 sys.modules 进程级断言——pytest 收集阶段
    # test_import_smoke 会 import server（fastmcp 依赖）进 sys.modules，该断言在
    # pytest 下必然误报（测试隔离问题，非产品缺陷）。真实契约是"纯逻辑层源码
    # 不 import fastmcp"（上方断言已覆盖）；server 层（唯一允许 fastmcp 的层）
    # 由 test_import_smoke 单独验证。
    assert not offenders, f"纯逻辑层引入了 fastmcp：{offenders}"


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


@check("B1 / 服务端生成 sample_id 为确定性幂等哈希（S-2 起取代 uuid）")
def _b1_uuid_sample_id():
    with as_caller("doctor_assistant"):
        res = core.upsert_lab_result(
            "P0013", {"report_date": _TODAY, "k_mmol_L": 4.8},
        )
    assert res["ok"] is True, res
    sid = res["data"]["sample_id"]
    # S-2（2026-08-15）：无显式 sample_id 时生成**确定性幂等哈希**（pid-S + 哈希
    # 截断）——同日期同指标名-值 → 同 id（LIS 超时重试幂等，不落两行）；此前 uuid4
    # 每次不同，重试同一化验落两行。格式：pid-S + 16 位十六进制（2026-08-19 审查 ②：
    # SHA-1[:8]=32bit 升级 SHA-256[:16]=64bit，长期 LIS 数据降低碰撞风险）。
    assert sid.startswith("P0013-S"), sid
    tail = sid[len("P0013-S"):]
    assert len(tail) == 16 and all(c in "0123456789abcdef" for c in tail), sid
    # 同日期同值重试 → **同 id**（幂等，S-2 新增语义）
    with as_caller("doctor_assistant"):
        res1b = core.upsert_lab_result(
            "P0013", {"report_date": _TODAY, "k_mmol_L": 4.8},
        )
    assert res1b["data"]["sample_id"] == sid, "同值重试应幂等命中同 id"
    assert res1b["data"].get("idempotent_hit") is True, "重试应标记幂等命中"
    # P1-3（2026-08-17）：幂等命中信封与正常分支对称——补 critical_items/next_action
    # /notify_action，否则 LIS 超时重试命中同 id 时危急通知链路被静默跳过。
    assert res1b["data"].get("next_action") == "CKDNutri-assessment-mcp.assess_clinical_status_tool", res1b["data"]
    assert "critical_items" in res1b["data"], res1b["data"]
    assert "notify_action" in res1b["data"], res1b["data"]
    # 不同指标集 → 不同 id（碰撞免疫）
    with as_caller("doctor_assistant"):
        res2 = core.upsert_lab_result(
            "P0013", {"report_date": _TODAY, "na_mmol_L": 140.0},
        )
    assert res2["ok"] is True, res2
    assert res2["data"]["sample_id"] != sid, "不同指标集应生成不同 sample_id"


@check("B1 / 显式 sample_id 重复提交被拒（不覆盖，防并发丢数据）")
def _b1_dup_sample_id_rejected():
    from CKDNutri_clinical_data_mcp.repository import get_repository

    with as_caller("doctor_assistant"):
        res = core.upsert_lab_result(
            "P0013",
            {"report_date": _TODAY, "k_mmol_L": 5.1, "sample_id": "P0013-U-test1"},
        )
    assert res["ok"] is True, res
    # 同 sample_id 再次写入：条件写（EXPECT_NOT_EXIST）必须失败，不得覆盖
    before = fake_labs_store_count()
    # L（2026-08-16，第七轮审查）：显式 sample_id 冲突返回 **CONFLICT 信封**（业务
    # 冲突语义化，不再抛 RuntimeError 冒泡 INTERNAL_ERROR）——测试同时接受两者。
    try:
        with as_caller("doctor_assistant"):
            res2 = core.upsert_lab_result(
                "P0013",
                {"report_date": _TODAY, "k_mmol_L": 9.9, "sample_id": "P0013-U-test1"},
            )
        assert res2["ok"] is False and res2["error"] == "CONFLICT", res2
    except RuntimeError as exc:
        assert "已存在" in str(exc), exc
    assert fake_labs_store_count() == before, "重复提交产生了写入副作用"
    # 原始记录未被覆盖：k 仍是 5.1（走 repository 原始面板，非 decorated results）

    for p in get_repository().get_panels("P0013"):
        if p["sample_id"] == "P0013-U-test1":
            assert p["values"].get("k_mmol_L") == 5.1, p["values"]
            break
    else:
        raise AssertionError("原记录丢失")


@check("B1 / 同患儿 20 并发 upsert 无覆盖丢数据（线程并发压测）")
def _b1_concurrent_upsert():
    import threading

    from CKDNutri_clinical_data_mcp.repository import get_repository

    # harness 顶部已注入 A207_CALLER=doctor_assistant（进程级），并发线程共享同一身份
    pid = "P0013"
    n = 20
    barrier = threading.Barrier(n)
    results: list[dict] = []
    lock = threading.Lock()
    errors: list[str] = []

    def _worker(idx: int):
        try:
            barrier.wait()  # 同时起跑，最大化碰撞窗口
            res = core.upsert_lab_result(
                pid,
                {"report_date": _TODAY, "k_mmol_L": round(4.0 + idx * 0.1, 1),
                 "hb_g_L": 100 + idx},
            )
            assert res["ok"] is True, res
            with lock:
                results.append(res)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发写入出现 {len(errors)} 个错误: {errors[:3]}"
    sids = {r["data"]["sample_id"] for r in results}
    assert len(sids) == n, f"并发写入 sample_id 碰撞：{n} 次写入仅 {len(sids)} 个唯一 ID"
    # 全部并发写入的 k 值都可在面板中找到（无一条被覆盖丢失）
    panels = get_repository().get_panels(pid)
    want_k = {round(4.0 + i * 0.1, 1) for i in range(n)}
    got_k = {p["values"].get("k_mmol_L") for p in panels
             if p.get("source") == "upsert" and p["values"].get("k_mmol_L") is not None}
    missing = want_k - got_k
    assert not missing, f"并发写入有 {len(missing)} 条被覆盖丢失: {sorted(missing)}"


@check("LOW-4 / append_lab_record 计数用主键前缀范围（不全表扫其他患者）")
def _low4_append_prefix_scan():
    """LOW-4（2026-08-15）：写库计数改前缀 GetRange——_range_all 必须带
    prefix={"patient_id": 该患者}，只扫该患者行（此前全表 GetRange 内存过滤）。"""
    from unittest import mock

    import harness

    calls: list[tuple] = []

    def spy(table, pk_cols, prefix=None):
        calls.append((table, pk_cols, prefix))
        return []

    rec = {"patient_id": "P0013", "sample_id": "low4-prefix-1",
           "report_date": _TODAY, "values": {"k_mmol_L": 4.5}}
    with mock.patch.object(harness.FAKE_REPO, "_range_all", spy):
        n = harness.FAKE_REPO.append_lab_record(rec)
    assert calls, "append_lab_record 未走 _range_all 计数"
    table, pk_cols, prefix = calls[-1]
    assert prefix == {"patient_id": "P0013"}, (table, pk_cols, prefix)
    assert pk_cols == ["patient_id", "sample_id"], pk_cols
    assert n == 0, n  # spy 返回空 → 计数 0（行为验证：返回值来自前缀结果）


@check("P1-2 / 窗口内该指标无采样 → NO_DATA（不静默标 flat）")
def _p1_2_trend_no_data():
    """P1-2（2026-08-17）：指定指标在窗口内无任何采样时返回 NO_DATA——
    此前 build_trend 对 latest is None 静默标 "flat"（误导为"测过且没变"）。"""
    today = date.today().isoformat()
    with as_caller("doctor_assistant"):
        up = core.upsert_lab_result(
            "P0016",
            {"report_date": today, "k_mmol_L": 4.8, "sample_id": "p1-2-no-data-1"},
        )
    assert up["ok"] is True, up
    # 今日面板仅含 k，na 不在其中且窗口(1天)只覆盖今日 → 该指标无数据 → NO_DATA
    res = core.get_lab_trend("P0016", "na_mmol_L", window_days=1)
    assert res["ok"] is False and res["error"] == "NO_DATA", res
    # 对照：窗口内有数据的指标正常返回趋势
    res2 = core.get_lab_trend("P0016", "k_mmol_L", window_days=1)
    assert res2["ok"] is True and res2["data"]["point_count"] >= 1, res2


@check("P1-4 / 同日多采样：较早采样的危急不被最新正常采样掩盖")
def _p1_4_same_day_critical():
    """P1-4（2026-08-17）：同日多次采样时危急扫描覆盖最新报告日全部采样——
    此前只扫 panels[-1]（sample_id 最大者），晨 K=7.2 危急、午后复查 K=5.0 正常
    时危急被静默掩盖（通知链路不触发）。现取同日全部面板并集，危急不丢。"""
    today = date.today().isoformat()
    with as_caller("doctor_assistant"):
        a = core.upsert_lab_result(
            "P0017",
            {"report_date": today, "k_mmol_L": 7.2, "sample_id": "p1-4-0001"},
        )
        b = core.upsert_lab_result(
            "P0017",
            {"report_date": today, "k_mmol_L": 5.0, "sample_id": "p1-4-0002"},
        )
        res = core.get_critical_values("P0017")
    assert a["ok"] is True and b["ok"] is True, (a, b)
    assert res["ok"] is True, res
    assert res["data"]["scan_scope"] == "latest_day", res["data"]
    # 较早采样的危急仍在（修复前只扫 latest=0002 正常样本 → critical_count=0）
    assert res["data"]["critical_count"] >= 1, res["data"]
    analytes = {i["analyte"] for i in res["data"]["items"]}
    assert "k_mmol_L" in analytes, analytes
