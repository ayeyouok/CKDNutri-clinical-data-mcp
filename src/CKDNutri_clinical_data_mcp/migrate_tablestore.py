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
import logging
import os
from pathlib import Path
from typing import Any

from a207_policy import ConflictError, resolve_state_path

from .repository import (
    TABLE_LABS,
    TABLE_LABS_STORE,
    TABLE_PATIENTS,
    TablestoreRepository,
    ensure_tablestore_tables,
)

logger = logging.getLogger("CKDNutri-clinical-data-mcp.migrate")

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


def _verify_row_consistent(record: dict[str, Any], row: dict[str, Any]) -> str | None:
    """迁移冲突时校验已存在行与源 JSON 记录是否一致。

    审查 P2-02（2026-08-19）：此前冲突直接按"已迁移"跳过、不验证内容——若线上行
    被修改/污染（如 K=4.2 → K=9.9），迁移静默认为"已完成"，源与目标不一致无人知晓。
    对比核心业务字段（report_date / specimen / values）；values 在 Tablestore 为
    JSON 字符串列，反序列化后比较。一致返回 None，不一致返回差异描述。
    """
    diffs: list[str] = []
    for key in ("report_date", "specimen"):
        src = record.get(key)
        tgt = row.get(key)
        if src != tgt:
            diffs.append(f"{key}: 源={src!r} vs 目标={tgt!r}")
    src_vals = record.get("values")
    tgt_vals = row.get("values")
    if isinstance(tgt_vals, str):
        try:
            tgt_vals = json.loads(tgt_vals)
        except json.JSONDecodeError:
            diffs.append("values: 目标行 JSON 损坏")
            tgt_vals = None
    if src_vals != tgt_vals:
        diffs.append(f"values: 源={src_vals!r} vs 目标={tgt_vals!r}")
    return "；".join(diffs) if diffs else None


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
    logger.info("[migrate] patients 表写入 %s 条", len(patients))

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
    logger.info("[migrate] labs 表写入 %s 条", lab_rows)

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
        except ConflictError as exc:
            # 十二审（2026-08-17）：**捕获 ConflictError**——append_lab_record 自
            # 十审起改抛 ConflictError（跨包错误码统一，非 RuntimeError 子类），
            # 此前 except RuntimeError 匹配不到 → 二次运行第一条即抛 ConflictError
            # 冒泡中断迁移，"可重跑补齐"承诺失效（幂等重跑测试场景）。
            if "已存在" in str(exc) or "sample_id" in str(exc):
                # 审查 P2-02（2026-08-19）：跳过前校验已存在行与源记录**内容一致**——
                # 此前仅按错误串判断"已迁移"即跳过，线上行若被修改/污染（K=4.2→9.9）
                # 迁移静默认为"已完成"（源与目标不一致无人知晓）。读回目标行对比：
                # 一致 → skip（幂等重跑）；不一致 → fail-closed 中断并提示人工核对。
                row = repo._get_row(TABLE_LABS_STORE,
                                    repo._pk_lab(record.get("patient_id"), record.get("sample_id")))
                conflict = _verify_row_consistent(record, row) if row is not None else "目标行不存在"
                if conflict is None:
                    store_skipped += 1
                else:
                    # 冲突是预期数据问题，切断原 ConflictError 链（B904：from None）
                    raise RuntimeError(
                        f"[migrate] 迁移冲突且目标行与源不一致（sample_id="
                        f"{record.get('sample_id')}）：{conflict}——请人工核对后处理") from None
            else:
                raise
    counts[TABLE_LABS_STORE] = store_rows
    logger.info("[migrate] labs_store 表写入 %s 条（历史 JSON 写库），跳过已存在 %s 条（幂等重跑）",
                store_rows, store_skipped)

    # 4) guardian_tokens 不再迁移：令牌库 JSON 文件是 a207-policy 校验的事实源
    #    （his.issue_guardian_token 直接读写 JSON，与 Tablestore 无关）。
    logger.info("[migrate] guardian_tokens 跳过迁移（JSON 文件为校验事实源）")

    return counts


if __name__ == "__main__":
    # N-LOG-1（2026-08-14）：统一 logging（生产 stdout 可采集），不再裸 print
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # S-1（2026-08-15）：禁止静默默认医生身份——此前 setdefault 让未配置身份的
    # 部署一跑即以最高权限 doctor_assistant 运行（P0-1：身份必须部署注入、模型不可
    # 自证；迁移脚本同样适用）。改为显式要求：未注入 A207_CALLER 即 fail-closed
    # 拒绝启动，并提示运维显式指定（迁移需全权限，缺省不可静默升级）。
    if not os.environ.get("A207_CALLER"):
        logger.error(
            "[migrate] A207_CALLER 未设置——迁移脚本需要写入权限身份（通常为 "
            "doctor_assistant），请显式注入后重试：\n"
            "  A207_CALLER=doctor_assistant A207_ENV=production python -m "
            "CKDNutri_clinical_data_mcp.migrate_tablestore\n"
            "不静默默认医生身份（P0-1：身份必须由部署显式注入）")
        raise SystemExit(2)
    logger.warning("[migrate] 以 A207_CALLER=%s 身份执行迁移——确认该部署配置为最高权限身份",
                   os.environ.get("A207_CALLER"))
    try:
        migrate()
    except Exception as exc:  # noqa: BLE001
        logger.error("[migrate] 迁移失败：%s: %s", type(exc).__name__, exc)
        raise SystemExit(1) from None
