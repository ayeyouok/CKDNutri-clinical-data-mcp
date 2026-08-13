"""检验**静态基线数据**访问（v3.0：仅剩只读数据源，JSON 写库已删除）。

- 只读基线：随包内联 `_labs_baseline.BASELINE`（或 fallback data/labs.json）
- v3.0（2026-08-13）：用户决策删除 JSON 双后端——本模块原有的 labs_store.json
  **写库**（load_store/append_record/merged_panels/store_path）随 LocalJsonRepository
  一并移除；运行时化验写入统一走 repository → Tablestore `labs_store` 表。
  本模块仅保留静态基线访问（load_dataset/find_patient/known_patient_ids），
  供 repository.get_dataset_meta（units 静态配置）、迁移脚本与测试使用。
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_DATASET_CACHE: dict[str, Any] | None = None

try:
    from ._labs_baseline import BASELINE as _EMBEDDED_BASELINE
except Exception:  # pragma: no cover - 兜底，正常运行时模块必存在
    _EMBEDDED_BASELINE = None

BASE_FILENAME = "labs.json"


def data_dir() -> Path:
    """定位数据目录。"""
    env = os.environ.get("A207_LIS_DATA_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    candidates = (
        here.parent / "data",              # 包内 data（CKDNutri_clinical_data_mcp/data）
        here.parents[2] / "data",          # 包根 data（本仓库布局）
    )
    for path in candidates:
        if (path / BASE_FILENAME).exists():
            return path
    return candidates[-1]


def base_path() -> Path:
    return data_dir() / BASE_FILENAME


def load_dataset(refresh: bool = False) -> dict[str, Any]:
    """载入只读基线数据集（进程内缓存，返回深拷贝副本）。

    优先使用随包内联的基线模块（_labs_baseline.py），保证在任意部署环境
    （本地 / 云沙箱 / 容器）都能自包含加载，无需外部 data/labs.json。
    仅当内联模块缺失时，才回退到文件读取（兼容源码开发态）。

    2026-08-12（数据污染加固）：返回 copy.deepcopy，调用方拥有独立副本，
    避免对返回数据的原地修改污染全局缓存（36 患儿 112 面板量级开销可忽略）。
    """
    global _DATASET_CACHE
    with _LOCK:
        if _DATASET_CACHE is None or refresh:
            if _EMBEDDED_BASELINE is not None:
                _DATASET_CACHE = _EMBEDDED_BASELINE
            else:
                path = base_path()
                if not path.exists():
                    raise FileNotFoundError(
                        f"检验基线数据缺失：{path}。"
                        f"请确认 a207-lis-mcp 安装包内含内联基线数据（从当前源码重新构建发布），"
                        f"或在运行环境设置环境变量 A207_LIS_DATA_DIR 指向含 labs.json 的目录。"
                    )
                _DATASET_CACHE = json.loads(path.read_text(encoding="utf-8"))
        return copy.deepcopy(_DATASET_CACHE)


def find_patient(patient_id: str) -> dict[str, Any] | None:
    """按 id 取患者的基线记录。"""
    for patient in load_dataset().get("patients", []):
        if patient.get("patient_id") == patient_id:
            return patient
    return None


def known_patient_ids() -> list[str]:
    return [p["patient_id"] for p in load_dataset().get("patients", [])]
