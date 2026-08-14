# -*- coding: utf-8 -*-
"""JSON → Tablestore 一次性历史数据迁移脚本（幂等，可重跑）。

v3.0（2026-08-13）：运行时 JSON 存储已删除（唯一后端 Tablestore），本脚本仅用于
把**历史** JSON 数据一次性搬到 Tablestore，完成后本地 JSON 即可弃用。

用法（先注入 A207_OTS_* 连接参数）：
    python -m CKDNutri_clinical_data_mcp.migrate_tablestore

迁移内容：
- patients.json（HIS 患者主索引 36 条）→ patients 表
- _labs_baseline.py 内联基线（LIS 化验面板）→ labs 表
- labs_store.json（旧运行时写库，若存在，upsert 记录）→ labs_store 表
- guardian_tokens **不再迁移**（令牌库 JSON 文件是 a207-policy 校验的事实源，与 Tablestore 无关）

幂等性：按主键 PutRow（覆盖语义），重复执行不产生重复行。
嵌套结构（biochemistry/food_diary_5d/nutrition_ceiling/allergies/...）序列化为
JSON 字符串列，与 repository.TablestoreRepository._deserialize_patient 对称。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from a207_policy import resolve_state_path

from .repository import (
    TABLE_LABS,
    TABLE_LABS_STORE,
    TABLE_PATIENTS,
    TablestoreRepository,
    ensure_tablestore_tables,
)

SRC = Path(__file__).resolve().parent

# 旧 JSON 写库文件名（v3.0 前 store.py 使用；仅迁移脚本读取）
LEGACY_LABS_STORE_FILENAME = "labs_store.json"


def _load_patients() -> list[dict[str, Any]]:
    path = SRC / "data" / "patients.json"
    if not path.exists():
        raise FileNotFoundError(f"患者主数据缺失：{path}")
    return json.loads(path.read_text(encoding="utf-8"))["patients"]


def _load_labs_baseline() -> list[dict[str, Any]]:
    from . import _labs_baseline

    return list(_labs_baseline.BASELINE.get("patients", []))


def _legacy_labs_store_path() -> Path:
    """定位 v3.0 前的 labs_store.json（A207_LIS_DATA_DIR override 优先）。"""
    override = os.environ.get("A207_LIS_DATA_DIR")
    if override:
        return Path(override) / LEGACY_LABS_STORE_FILENAME
    return resolve_state_path(LEGACY_LABS_STORE_FILENAME)


def _load_labs_store() -> list[dict[str, Any]]:
    """读取旧 JSON 写库（v3.0 前 store.load_store 语义）；文件缺失返回空列表。"""
    path = _legacy_labs_store_path()
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("records", []) if isinstance(payload, dict) else []
    return records if isinstance(records, list) else []


def migrate() -> dict[str, int]:
    """执行迁移，返回各表写入行数。"""
    ensure_tablestore_tables()
    repo = TablestoreRepository()

    counts: dict[str, int] = {}

    # 1) patients 主索引
    patients = _load_patients()
    for p in patients:
        attrs = repo._serialize_patient(p)
        repo._put_row(TABLE_PATIENTS, repo._pk_patient(p["patient_id"]), attrs)
    counts[TABLE_PATIENTS] = len(patients)
    print(f"[migrate] patients 表写入 {len(patients)} 条")

    # 2) labs 基线面板（每患儿 panels 展开为独立行）
    lab_rows = 0
    for patient in _load_labs_baseline():
        pid = patient["patient_id"]
        for panel in patient.get("panels", []):
            attrs = {
                "report_date": panel.get("report_date"),
                "specimen": panel.get("specimen"),
                "values": json.dumps(panel.get("values", {}), ensure_ascii=False),
                "source": "baseline",
                "recorded_by": None,
            }
            repo._put_row(TABLE_LABS, repo._pk_lab(pid, panel["sample_id"]), attrs)
            lab_rows += 1
    counts[TABLE_LABS] = lab_rows
    print(f"[migrate] labs 表写入 {lab_rows} 条")

    # 3) labs_store 旧写库（v3.0 前的 upsert 记录，一次性历史迁移）
    store_rows = 0
    store_skipped = 0
    for record in _load_labs_store():
        # CD-S2 修复（2026-08-14）：迁移**幂等可重跑**——append_lab_record 用
        # EXPECT_NOT_EXIST 条件写（B1 防并发覆盖），第二次运行同 sample_id 必抛
        # RuntimeError（部分迁移后无法重跑补齐、无事务）。已存在视为"已迁移"跳过
        # 并计数，不中断；patients/labs 基线本就 IGNORE 覆盖幂等。
        try:
            repo.append_lab_record(record)
            store_rows += 1
        except RuntimeError as exc:
            if "已存在" in str(exc) or "sample_id" in str(exc):
                store_skipped += 1
            else:
                raise
    counts[TABLE_LABS_STORE] = store_rows
    print(f"[migrate] labs_store 表写入 {store_rows} 条（历史 JSON 写库），"
          f"跳过已存在 {store_skipped} 条（幂等重跑）")

    # 4) guardian_tokens 不再迁移：令牌库 JSON 文件是 a207-policy 校验的事实源
    #    （his.issue_guardian_token 直接读写 JSON，与 Tablestore 无关）。
    print("[migrate] guardian_tokens 跳过迁移（JSON 文件为校验事实源）")

    return counts


if __name__ == "__main__":
    os.environ.setdefault("A207_CALLER", "doctor_assistant")
    try:
        migrate()
    except Exception as exc:  # noqa: BLE001
        print(f"迁移失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
