"""检验数据的读取与 mock 持久层。

- 只读基线：data/labs.json（由 data/generate.py 确定性生成）
- 写库：data/labs_store.json（upsert_lab_result 追加，M1 HIS 只读主数据不受影响）

数据目录解析顺序：环境变量 A207_LIS_DATA_DIR > 包内 data/ > 仓库根 data/。
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

from a207_policy import atomic_write_json, resolve_state_path

_LOCK = threading.Lock()
_DATASET_CACHE: dict[str, Any] | None = None

try:
    from ._labs_baseline import BASELINE as _EMBEDDED_BASELINE
except Exception:  # pragma: no cover - 兜底，正常运行时模块必存在
    _EMBEDDED_BASELINE = None

BASE_FILENAME = "labs.json"
STORE_FILENAME = "labs_store.json"


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


def store_path() -> Path:
    """可写写库路径（P1-3 修复：不再写安装目录）。

    - 若设 A207_LIS_DATA_DIR（开发/测试态），落到该目录，与基线同处，便于联调；
    - 否则经 a207_policy.resolve_state_path 落到 A207_DATA_DIR 或系统临时目录，
      保证在只读安装目录/容器/云沙箱下仍可写。
    """
    override = os.environ.get("A207_LIS_DATA_DIR")
    if override:
        return Path(override) / STORE_FILENAME
    return resolve_state_path(STORE_FILENAME)


def load_dataset(refresh: bool = False) -> dict[str, Any]:
    """载入只读基线数据集（进程内缓存，返回深拷贝副本）。

    优先使用随包内联的基线模块（_labs_baseline.py），保证在任意部署环境
    （本地 / 云沙箱 / 容器）都能自包含加载，无需外部 data/labs.json。
    仅当内联模块缺失时，才回退到文件读取（兼容源码开发态）。

    2026-08-12（数据污染加固）：此前直接返回 _DATASET_CACHE 引用（= _EMBEDDED_BASELINE，
    即模块级全局 BASELINE）——下游对返回数据的任何原地修改（如 get_lab_trend 对 panels
    排序、merged_panels 修改 panel dict）都会污染全局缓存，引发跨请求数据错乱。
    现改为返回 copy.deepcopy：36 患儿 112 面板量级开销可忽略，调用方拥有独立副本。
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


def load_store() -> list[dict[str, Any]]:
    """载入写库中追加的采样记录。"""
    path = store_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # BUG-67（2026-08-12）：损坏文件禁止静默返回 []——否则 append_record 会
        # load_store→[]→append→atomic_write 用"仅新记录"覆盖整个写库，历史采样永久丢失。
        # 抛 RuntimeError（server._invalid 归 INTERNAL_ERROR），运维可发现并恢复备份。
        raise RuntimeError(
            f"检验写库 {STORE_FILENAME} JSON 损坏，拒绝加载（防止静默清空），"
            f"请检查磁盘/恢复备份: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"检验写库 {STORE_FILENAME} 数据类型错误：期望 dict，"
            f"实际为 {type(payload).__name__}，拒绝加载（防止静默清空）")
    records = payload.get("records", [])
    return records if isinstance(records, list) else []


def append_record(record: dict[str, Any]) -> int:
    """把一条采样追加进写库，返回写库总条数。

    P1-5 修复（2026-08-12）：改用 a207_policy.atomic_write_json 原子写（OD-014 标准），
    此前用 path.write_text 直接覆盖写——写一半被杀/磁盘满会留下截断文件，且
    load_store 对损坏 JSON 静默返回 [] 后再次覆盖，会静默丢失全部检验数据。
    """
    with _LOCK:
        records = load_store()
        records.append(record)
        payload = {
            "dataset": "a207-lis-mcp/labs_store",
            "schema_version": "1.0.0",
            "record_count": len(records),
            "records": records,
        }
        atomic_write_json(store_path(), payload)
        return len(records)


def find_patient(patient_id: str) -> dict[str, Any] | None:
    """按 id 取患者的基线记录。"""
    for patient in load_dataset().get("patients", []):
        if patient.get("patient_id") == patient_id:
            return patient
    return None


def merged_panels(patient_id: str) -> list[dict[str, Any]]:
    """基线 + 写库合并，按报告日期升序；同一 sample_id 以写库为准。"""
    patient = find_patient(patient_id)
    panels: list[dict[str, Any]] = list(patient["panels"]) if patient else []
    by_sample = {p["sample_id"]: dict(p) for p in panels}
    for record in load_store():
        if record.get("patient_id") != patient_id:
            continue
        # S7（2026-08-12 五包审查）：写库记录 .get() 防御——早期版本/手工写入的记录
        # 可能缺 sample_id/report_date/values 键，硬索引会 KeyError（且被 server _invalid
        # 误归 INVALID_INPUT）；缺关键字段的脏记录跳过合并（不影响基线展示）。
        sid = record.get("sample_id")
        values = record.get("values")
        if sid is None or record.get("report_date") is None or not isinstance(values, dict):
            continue
        by_sample[sid] = {
            "sample_id": sid,
            "report_date": record["report_date"],
            "specimen": record.get("specimen"),
            "values": values,
            "source": "upsert",
            "recorded_by": record.get("recorded_by"),
        }
    return sorted(by_sample.values(), key=lambda p: (p["report_date"], p["sample_id"]))


def known_patient_ids() -> list[str]:
    return [p["patient_id"] for p in load_dataset().get("patients", [])]
