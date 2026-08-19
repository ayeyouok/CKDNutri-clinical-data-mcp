"""CKDNutri-clinical-data-mcp（P1，由 a207-his-mcp + a207-lis-mcp 合并而来）自测入口。

直接 import core 断言，不依赖 fastmcp、不依赖 pytest。
运行：python tests/test_tools.py

用例分布：
- cases_read.py  只读工具（数据集完整性 / get_labs / get_critical_values / get_lab_trend）
- cases_write.py 写入工具（upsert_lab_result）与铁律自查
写操作跑在临时数据目录，不污染仓库内的 labs.json。
"""

from __future__ import annotations

import os

os.environ.setdefault("A207_ENV", "test")  # N-SEC-1（2026-08-14）：测试进程显式声明测试环境（守卫 fail-closed 默认拒绝）
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")  # 生产护栏（2026-08-15）：测试进程显式确认 json 后端为开发模式

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 十二审（2026-08-17）：pytest 收集（CI 假绿阻断修复）——@check 用例在 import 时
# 已以 test_ 前缀注册到 cases_read/cases_write 模块全局，但 pytest 只收集
# test_*.py 文件（cases_*.py 不匹配默认 python_files 模式）。把两模块的 test_*
# 成员转发到本模块，pytest 收集 test_tools.py 时一并收集全部 @check 用例。
# 直跑模式（python test_tools.py → main() → harness.report()）不受影响。
import types as _types

import cases_read
import cases_write
import harness

for _mod in (cases_read, cases_write):
    for _n, _o in vars(_mod).items():
        if _n.startswith("test_") and isinstance(_o, _types.FunctionType):
            globals()[_n] = _o


def main() -> int:
    return harness.report()


if __name__ == "__main__":
    raise SystemExit(main())
