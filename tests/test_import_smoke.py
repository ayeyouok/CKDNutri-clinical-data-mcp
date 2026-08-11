"""P1 冒烟自测：导入 server 不报错 + 代表性只读工具可调用。

运行：pytest tests/test_import_smoke.py  (或 python tests/test_import_smoke.py)
依赖：a207-policy 已随 pip install -e . 安装（本包 dependencies 已声明）。
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

# 身份由部署注入（P0-1）；测试显式注入，缺失即 fail-closed。
os.environ.setdefault("A207_CALLER", "doctor_assistant")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_server_imports():
    """导入 server 不可抛错（回归：合并时曾缺 .errors/.views/.reference/.his）。"""
    mod = importlib.import_module("CKDNutri_clinical_data_mcp.server")
    assert mod.mcp is not None


def test_list_known_patients():
    from CKDNutri_clinical_data_mcp import core

    r = core.list_known_patients()
    assert r.get("ok") is True
    assert r["data"]["count"] == 36


if __name__ == "__main__":
    test_server_imports()
    test_list_known_patients()
    print("P1 SMOKE OK")
