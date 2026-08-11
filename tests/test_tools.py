"""CKDNutri-clinical-data-mcp（P1，由 a207-his-mcp + a207-lis-mcp 合并而来）自测入口。

直接 import core 断言，不依赖 fastmcp、不依赖 pytest。
运行：python tests/test_tools.py

用例分布：
- cases_read.py  只读工具（数据集完整性 / get_labs / get_critical_values / get_lab_trend）
- cases_write.py 写入工具（upsert_lab_result）与铁律自查
写操作跑在临时数据目录，不污染仓库内的 labs.json。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402  必须最先导入：切换数据目录后才能 import core

import cases_read  # noqa: E402,F401
import cases_write  # noqa: E402,F401


def main() -> int:
    return harness.report()


if __name__ == "__main__":
    raise SystemExit(main())
