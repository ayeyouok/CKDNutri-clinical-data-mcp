# -*- coding: utf-8 -*-
"""JSON → Tablestore 数据迁移脚本（幂等，可重跑）。

用法（先注入 A207_OTS_* 连接参数）：
    python -m CKDNutri_clinical_data_mcp.migrate_tablestore

迁移内容：
- patients.json（HIS 患者主索引 36 条）→ patients 表
- _labs_baseline.py 内联基线（LIS 化验面板）→ labs 表
- 写库 labs_store.json（若存在，upsert 记录）→ labs_store 表
- guardian_tokens 状态库（若存在）→ guardian_tokens 表

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

from . import store as _store
from .repository import (
    TABLE_GUARDIAN_TOKENS,
    TABLE_LABS,
    TABLE_LABS_STORE,
    TABLE_PATIENTS,
    TablestoreRepository,
    ensure_tablestore_tables,
)

SRC = Path(__file__).resolve().parent


def _load_patients() -> list[dict[str, Any]]:
    path = SRC / "data" / "patients.json"
    if not path.exists():
        raise FileNotFoundError(f"患者主数据缺失：{path}")
    return json.loads(path.read_text(encoding="utf-8"))["patients"]


def _load_labs_baseline() -> list[dict[str, Any]]:
    from . import _labs_baseline

    return list(_labs_baseline.BASELINE.get("patients", []))


def _load_labs_store() -> list[dict[str, Any]]:
    return _store.load_store()


def _load_guardian_tokens() -> dict[str, Any]:
    from .his import _load_guardian_tokens

    return _load_guardian_tokens()


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

    # 3) labs_store 写库（upsert 记录，若存在）
    store_rows = 0
    for record in _load_labs_store():
        repo.append_lab_record(record)
        store_rows += 1
    counts[TABLE_LABS_STORE] = store_rows
    print(f"[migrate] labs_store 表写入 {store_rows} 条")

    # 4) guardian_tokens 状态库
    # 五审（2026-08-13）说明：令牌校验（a207_policy.verify_guardian_token）与签发
    # （his.issue_guardian_token）当前仍以 **JSON 文件（guardian_tokens.json）为事实源**
    # ——a207-policy 是跨包共享层，令牌读写未接 Tablestore 后端。此处迁移到 Tablestore
    # 仅为"未来 a207-policy 支持 Tablestore 后端时开箱即用"预留，当前无读路径；
    # 多实例部署的令牌一致性靠 A207_GUARDIAN_TOKEN_DIR 指向共享持久目录保证。
    tokens = _load_guardian_tokens()
    repo.save_guardian_tokens(tokens)
    counts[TABLE_GUARDIAN_TOKENS] = len(tokens)
    print(f"[migrate] guardian_tokens 表写入 {len(tokens)} 条")

    return counts


if __name__ == "__main__":
    os.environ.setdefault("A207_CALLER", "doctor_assistant")
    try:
        migrate()
    except Exception as exc:  # noqa: BLE001
        print(f"迁移失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
