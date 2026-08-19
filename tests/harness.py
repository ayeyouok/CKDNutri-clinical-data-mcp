"""自测脚手架：环境隔离、身份注入、Fake Tablestore、断言收集、数值泄露探测。

导入本模块即完成三件事：
1. 注入测试身份 A207_CALLER（方案 A / P0-1：caller 由部署环境注入，模型不可自证）；
2. 校验默认数据目录能解析到基线 labs 数据（内联基线，自包含）；
3. **注入内存 Fake Tablestore 客户端**（v3.0：唯一后端 = Tablestore，测试不连真实 OTS，
   用内存实现 get_row/put_row/get_range 等最小接口，种子数据=patients.json 全量 + labs 基线，
   等价于"迁移完成后的 Tablestore"），并把令牌库切到临时目录（guardian_tokens 走 JSON 事实源）。
必须在 import core 之前先 import 本模块。

用例若需其他身份，显式传 caller 形参覆盖（等价于换一个部署实例）。
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("A207_ENV", "test")  # N-SEC-1（2026-08-14）：测试进程显式声明测试环境（守卫 fail-closed 默认拒绝）
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")  # 生产护栏（2026-08-15）：测试进程显式确认 json 后端为开发模式
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any
from unittest import mock

sys.stdout.reconfigure(encoding="utf-8")

PKG_ROOT = Path(__file__).resolve().parents[1]
SRC = PKG_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# P0-1：模拟部署侧注入身份。缺失即 fail-closed，故测试必须显式注入。
os.environ.setdefault("A207_CALLER", "doctor_assistant")

from CKDNutri_clinical_data_mcp import repository as _repo_mod
from CKDNutri_clinical_data_mcp import store

# 基线数据随包内联（_labs_baseline.BASELINE），任意部署环境自包含加载；
# 令牌库（guardian_tokens.json）隔离到临时目录，不污染仓库产物。
ds = store.load_dataset()
assert ds.get("patients"), "内联基线数据缺失：load_dataset 返回空"
TMP_DIR = Path(tempfile.mkdtemp(prefix="a207-clinical-test-"))
os.environ["A207_GUARDIAN_TOKEN_DIR"] = str(TMP_DIR)


# ---- 内存 Fake Tablestore 客户端（v3.0 单后端测试注入）----------------------
# LOW-6（2026-08-15）：Fake 异常继承真实 SDK 类——repository 改为 `except OTSClientError`
# 直接捕获（不再按 __name__ 字符串比对），若 Fake 仅继承 Exception 则捕获不到。
# SDK 缺失（CI 精简环境）时回退 Exception，测试不硬依赖 SDK。
try:
    from tablestore import OTSClientError as _RealOTSClientError
except ImportError:  # pragma: no cover - 仅无 SDK 环境
    _RealOTSClientError = Exception


class OTSClientError(_RealOTSClientError):
    """模拟 tablestore.OTSClientError（继承真实 SDK 类，repository 可 isinstance 捕获）。"""


class _FakeRow:
    """模拟 tablestore.Row：primary_key 为 [(名,值)]，attribute_columns 为 [(名,值,ts)]。"""

    def __init__(self, primary_key: list[tuple[str, str]], attrs: dict):
        self.primary_key = [(n, v) for n, v in primary_key]
        self.attribute_columns = [(n, v, 0) for n, v in attrs.items()]


class FakeOtsClient:
    """内存版 OTS 客户端（get_row/put_row/get_range/list_table 最小接口）。

    种子数据 = patients.json 全量（patients 表）+ _labs_baseline 基线（labs 表），
    等价于"迁移完成后的 Tablestore"；labs_store 从空开始（upsert 写入验证用）。
    """

    def __init__(self) -> None:
        self.tables: dict[str, dict[tuple, dict]] = {
            name: {} for name in (_repo_mod.TABLE_PATIENTS,
                                  _repo_mod.TABLE_LABS,
                                  _repo_mod.TABLE_LABS_STORE)
        }
        self._seed()

    @staticmethod
    def _pk_names(table: str) -> list[str]:
        return (["patient_id"] if table == _repo_mod.TABLE_PATIENTS
                else ["patient_id", "sample_id"])

    def _seed(self) -> None:
        from CKDNutri_clinical_data_mcp import _labs_baseline
        from CKDNutri_clinical_data_mcp import his as _his

        # patients 表种子 = HIS 患者档案（patients.json 全量，与 migrate_tablestore 同源）——
        # 此前误用 store.load_dataset()（LIS 化验基线，仅 7 键 primary_dx/panels），
        # 导致 Fake 下 list_patients/get_patient_profile 缺 age_band/primary_disease 等档案键。
        for p in _his.load_dataset().get("patients", []):
            attrs = _repo_mod.TablestoreRepository._serialize_patient(p)
            self.put_row(_repo_mod.TABLE_PATIENTS,
                         _FakeRow([("patient_id", p["patient_id"])], attrs), None)
        for patient in _labs_baseline.BASELINE.get("patients", []):
            pid = patient["patient_id"]
            for panel in patient.get("panels", []):
                attrs = {
                    "report_date": panel.get("report_date"),
                    "specimen": panel.get("specimen"),
                    "values": json.dumps(panel.get("values", {}), ensure_ascii=False),
                    "source": "baseline",
                    "recorded_by": None,
                }
                self.put_row(_repo_mod.TABLE_LABS,
                             _FakeRow([("patient_id", pid),
                                       ("sample_id", panel["sample_id"])], attrs), None)

    def list_table(self) -> list[str]:
        return list(self.tables)

    def get_row(self, table: str, pk: list[tuple[str, str]]):
        key = tuple(v for _, v in pk)
        attrs = self.tables[table].get(key)
        return None, (_FakeRow(pk, attrs) if attrs is not None else None), None

    def put_row(self, table: str, row, condition) -> None:
        key = tuple(v for _, v in row.primary_key)
        cols = row.attribute_columns
        # SDK 的 Row（_put_row 构造）attribute_columns 为 (name, value) 二元组；
        # get_row 返回的 row 为 (name, value, ts) 三元组。兼容两种。
        if cols and len(cols[0]) == 2:
            attrs = dict(cols)
        else:
            attrs = {n: v for n, v, _ in cols}
        # B1 修复（2026-08-13）：模拟 SDK 条件写语义——EXPECT_NOT_EXIST 条件不满足
        # （主键已存在）抛 OTSClientError，与真实 Tablestore 行为一致，保证并发压测
        # 能真实捕捉"覆盖写"问题（此前 Fake 无条件覆盖，掩盖并发丢数据）。
        if condition is not None:
            try:
                expectation = getattr(condition, "row_existence_expectation", None)
            except Exception:  # noqa: BLE001 - Fake 容错
                expectation = None
            if expectation is not None and str(expectation) == "EXPECT_NOT_EXIST":
                if key in self.tables[table]:
                    raise OTSClientError("Condition check failed: row already exists")
        self.tables[table][key] = attrs

    def delete_row(self, table: str, row, condition) -> None:
        key = tuple(v for _, v in row.primary_key)
        self.tables[table].pop(key, None)

    def get_range(self, table: str, direction: str, start, end, limit: int = 200):
        names = self._pk_names(table)
        rows = [_FakeRow(list(zip(names, key, strict=True)), attrs)
                for key, attrs in sorted(self.tables[table].items())]
        # #6（2026-08-15）：模拟主键前缀范围——storage._range_all 的 prefix 参数
        # 经 start/end 的 INF_MIN/INF_MAX 表达"该前缀段"；Fake 此前忽略边界返回
        # 全表，带 prefix 的调用（get_panels/append_lab_record）会读到所有患者数据
        # （跨患者串数据）。按 start/end 中非 INF 的列值过滤。
        from tablestore import INF_MAX, INF_MIN

        def _is_inf(v):
            return v is INF_MIN or v is INF_MAX or v == INF_MIN or v == INF_MAX

        def _within(pk_row):
            for (col, lo), (_, hi) in zip(start, end, strict=True):
                if not _is_inf(lo) and pk_row.get(col) < lo:
                    return False
                if not _is_inf(hi) and pk_row.get(col) > hi:
                    return False
            return True

        rows = [r for r in rows if _within(dict(r.primary_key))]
        return None, None, rows, None


# 唯一后端注入：patch repository.get_repository → 带 Fake 客户端的 TablestoreRepository。
# core/his 的 _repo() 每次调用 `from .repository import get_repository`，patch 模块属性即生效。
FAKE_REPO = _repo_mod.TablestoreRepository(client=FakeOtsClient())
_REPO_PATCH = mock.patch.object(_repo_mod, "get_repository", return_value=FAKE_REPO)
_REPO_PATCH.start()


def fake_labs_store_count() -> int:
    """测试断言用：当前 labs_store（写库）行数。"""
    return len(FAKE_REPO._client.tables[_repo_mod.TABLE_LABS_STORE])


def fake_labs_store_records() -> list[dict]:
    """测试断言用：labs_store（写库）全部属性行。"""
    return [dict(attrs) for attrs in FAKE_REPO._client.tables[_repo_mod.TABLE_LABS_STORE].values()]


RESULTS: list[tuple[str, bool, str]] = []
# 十二审（2026-08-17）：@check 注册的用例函数（直跑 report() 统一执行）。
# 此前 check 在 **import 时立即执行** fn()——pytest 收集 test_* 后再次执行造成
# 双执行（有状态副作用用例如 upsert 第二次失败）。改为**延迟执行**：check 只
# 注册 + 注册 test_ 前缀（pytest 收集），直跑 report() 统一执行。各只跑一次。
CHECK_FNS: list[tuple[str, Any]] = []


def check(name: str):
    """把一个断言函数注册为用例（延迟执行——直跑 report() / pytest test_* 各跑一次）。

    十二审（2026-08-17）：以 test_ 前缀注册到**调用模块**全局——pytest 即可收集
    @check 用例（CI 假绿阻断修复：此前 50 例只在直跑时执行，pytest/CI 不收集）。
    """

    def wrapper(fn):
        CHECK_FNS.append((name, fn))
        frame = sys._getframe(1)
        _test_name = f"test_{fn.__name__.lstrip('_')}"
        frame.f_globals[_test_name] = fn
        return fn

    return wrapper


def _run_checks() -> None:
    """直跑模式：统一执行全部 @check 用例，填充 RESULTS。"""
    for name, fn in CHECK_FNS:
        try:
            fn()
            RESULTS.append((name, True, ""))
        except AssertionError as exc:
            RESULTS.append((name, False, str(exc) or "断言失败"))
        except Exception:  # noqa: BLE001
            RESULTS.append((name, False, traceback.format_exc(limit=2)))


def numeric_leak(node) -> bool:
    """递归探测结构中是否存在任何数值（bool 不计入）。"""
    if isinstance(node, bool):
        return False
    if isinstance(node, (int, float)):
        return True
    if isinstance(node, dict):
        return any(numeric_leak(v) for v in node.values())
    if isinstance(node, (list, tuple)):
        return any(numeric_leak(v) for v in node)
    return False


def report() -> int:
    """打印结果并返回进程退出码（直跑模式：先统一执行全部 @check）。"""
    _run_checks()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, m) for n, ok, m in RESULTS if not ok]
    for name, ok, msg in RESULTS:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            print(f"       {msg.strip()}")
    print(f"\n合计 {len(RESULTS)} 项：通过 {passed}，失败 {len(failed)}")
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    return 1 if failed else 0
