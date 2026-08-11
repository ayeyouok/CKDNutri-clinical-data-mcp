"""自测脚手架：环境隔离、身份注入、断言收集、数值泄露探测。

导入本模块即完成三件事：
1. 注入测试身份 A207_CALLER（方案 A / P0-1：caller 由部署环境注入，模型不可自证）；
2. 校验默认数据目录能解析到基线 labs.json；
3. 把数据目录切到临时副本，使写操作不污染仓库产物。
必须在 import core 之前先 import 本模块。

用例若需其他身份，显式传 caller 形参覆盖（等价于换一个部署实例）。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

PKG_ROOT = Path(__file__).resolve().parents[1]
SRC = PKG_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# P0-1：模拟部署侧注入身份。缺失即 fail-closed，故测试必须显式注入。
os.environ.setdefault("A207_CALLER", "doctor_assistant")

from CKDNutri_clinical_data_mcp import store  # noqa: E402

# 基线数据随包内联（_labs_baseline.BASELINE），任意部署环境自包含加载，无需 labs.json 文件；
# 写操作隔离到临时目录（A207_LIS_DATA_DIR），不污染仓库产物。
ds = store.load_dataset()
assert ds.get("patients"), "内联基线数据缺失：load_dataset 返回空"
TMP_DIR = Path(tempfile.mkdtemp(prefix="a207-clinical-test-"))
os.environ["A207_LIS_DATA_DIR"] = str(TMP_DIR)
os.environ["A207_GUARDIAN_TOKEN_DIR"] = str(TMP_DIR)
store.load_dataset(refresh=True)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str):
    """把一个断言函数注册为用例，立即执行并记录结果。"""

    def wrapper(fn):
        try:
            fn()
            RESULTS.append((name, True, ""))
        except AssertionError as exc:
            RESULTS.append((name, False, str(exc) or "断言失败"))
        except Exception:  # noqa: BLE001
            RESULTS.append((name, False, traceback.format_exc(limit=2)))
        return fn

    return wrapper


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
    """打印结果并返回进程退出码。"""
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, m) for n, ok, m in RESULTS if not ok]
    for name, ok, msg in RESULTS:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            print(f"       {msg.strip()}")
    print(f"\n合计 {len(RESULTS)} 项：通过 {passed}，失败 {len(failed)}")
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    return 1 if failed else 0
