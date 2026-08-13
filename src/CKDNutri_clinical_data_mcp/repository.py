# -*- coding: utf-8 -*-
"""P1 临床数据域 DAO 抽象层（v2.4：JSON ↔ Tablestore 双后端）。

设计目标：
- 数据访问契约与存储实现解耦：业务层（his.py / core.py）只面向本层接口编程，
  存储后端可切换（本地 JSON 文件 ↔ 阿里云表格存储 Tablestore），业务逻辑零改动。
- 本层只做「数据存取」，不做权限/脱敏/业务计算——那些留在 his.py / core.py。

后端切换（环境变量）：
- A207_STORAGE_BACKEND=json（默认）：本地 JSON 文件（现状，零回归）
- A207_STORAGE_BACKEND=tablestore：阿里云表格存储（需配 A207_OTS_* 连接参数）

Tablestore 连接参数（由部署环境注入，与 A207_CALLER 同模式，不入代码）：
- A207_OTS_ENDPOINT       形如 https://xxx.cn-hangzhou.ots.aliyuncs.com
- A207_OTS_INSTANCE_NAME  实例名
- A207_OTS_ACCESS_KEY_ID   RAM 子账号 AK
- A207_OTS_ACCESS_KEY_SECRET  AK Secret

Tablestore 表结构（v2.4 设计，详见 A207_工具粒度架构评审_20260812.md §5）：
- patients       主键 patient_id；属性列 = 档案字段（嵌套结构 JSON 序列化进 bio/food_diary/ceiling 列）
- labs           主键 patient_id + sample_id；属性列 = report_date/specimen/values(JSON)/source/recorded_by
- labs_store     写库追加（与 labs 同构，source=upsert）；get_panels 时按 sample_id 合并覆盖基线
- guardian_tokens 主键 patient_id；属性列 = token/issued_at/expires_at/issued_by
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Protocol, runtime_checkable

from . import his as _his
from . import store as _store

logger = logging.getLogger("CKDNutri-clinical-data-mcp.repository")

# Tablestore 连接参数的环境变量名（与 a207-policy 的 env 注入约定一致）
OTS_ENDPOINT_ENV = "A207_OTS_ENDPOINT"
OTS_INSTANCE_ENV = "A207_OTS_INSTANCE_NAME"
OTS_AK_ID_ENV = "A207_OTS_ACCESS_KEY_ID"
OTS_AK_SECRET_ENV = "A207_OTS_ACCESS_KEY_SECRET"
STORAGE_BACKEND_ENV = "A207_STORAGE_BACKEND"

# Tablestore 表名（单一事实源）
TABLE_PATIENTS = "patients"
TABLE_LABS = "labs"
TABLE_LABS_STORE = "labs_store"
TABLE_GUARDIAN_TOKENS = "guardian_tokens"

# patients 表中嵌套结构序列化列名
_PAT_NESTED_COLS = ("biochemistry", "food_diary_5d", "nutrition_ceiling",
                    "allergies", "diet_restrictions", "dialysis_detail")


@runtime_checkable
class ClinicalDataRepository(Protocol):
    """P1 数据访问契约（HIS 患者主数据 + LIS 化验 + 监护人令牌库）。"""

    # ---- 数据集元数据 ----
    def get_dataset_meta(self) -> dict[str, Any]:
        """返回数据集级元数据（as_of / units / schema_version / patient_count）。"""
        ...

    # ---- HIS 患者主数据 ----
    def get_patient(self, patient_id: str) -> dict[str, Any] | None:
        """按主索引取患者完整档案（含 nutrition_ceiling 等嵌套结构）。"""
        ...

    def list_patients(self, criteria: dict[str, Any]) -> list[dict[str, Any]]:
        """按维度筛选患者队列（age_band / ckd_stage / dialysis 等）。"""
        ...

    def all_patient_ids(self) -> list[str]:
        """枚举 HIS 主索引全部 patient_id（供 list_patients 总数/联调）。"""
        ...

    # ---- LIS 化验数据 ----
    def get_panels(self, patient_id: str) -> list[dict[str, Any]]:
        """基线 + 写库合并后的化验面板（按报告日期升序）。"""
        ...

    def append_lab_record(self, record: dict[str, Any]) -> int:
        """追加一条化验记录到写库，返回写库总条数。"""
        ...

    def known_lis_patient_ids(self) -> list[str]:
        """枚举 LIS 数据集覆盖的 patient_id（供跨包联调核对，不暴露给 LLM）。"""
        ...

    # ---- 监护人令牌库 ----
    def load_guardian_tokens(self) -> dict[str, Any]:
        """读取监护人令牌库（损坏时抛 RuntimeError，拒绝静默降级）。"""
        ...

    def save_guardian_tokens(self, tokens: dict[str, Any]) -> None:
        """原子写监护人令牌库。"""
        ...


class LocalJsonRepository:
    """本地 JSON 文件后端（现状实现，零行为变更）。

    复用 store.py / his.py 的既有读取与原子写逻辑，避免双份实现漂移。
    """

    def get_dataset_meta(self) -> dict[str, Any]:
        ds = _his.load_dataset()
        return {
            "as_of": ds.get("as_of"),
            "units": ds.get("units"),
            "schema_version": ds.get("schema_version"),
            "patient_count": len(ds.get("patients", [])),
        }

    def get_patient(self, patient_id: str) -> dict[str, Any] | None:
        # 用公共 load_dataset 自行查找，不依赖 his 私有 _lookup 缓存
        for p in _his.load_dataset().get("patients", []):
            if p.get("patient_id") == patient_id:
                return p
        return None

    def list_patients(self, criteria: dict[str, Any]) -> list[dict[str, Any]]:
        # 直读数据集自行筛选（不复用 his.list_patients——业务层改走本接口后
        # 会递归；筛选谓词复用 his._match 单一事实源，与业务层行为一致）。
        from .his import _match

        criteria = criteria or {}
        matched = []
        for p in _his.load_dataset().get("patients", []):
            if all(_match(p, k, v) for k, v in criteria.items()):
                matched.append({
                    "patient_id": p["patient_id"],
                    "age_years": p["age_years"],
                    "age_band": p["age_band"],
                    "sex": p["sex"],
                    "ckd_stage": p["ckd_stage"],
                    "dialysis": p["dialysis"],
                    "primary_disease": p["primary_disease"],
                    "has_allergies": bool(p.get("allergies")),
                })
        return matched

    def all_patient_ids(self) -> list[str]:
        return [p["patient_id"] for p in _his.load_dataset().get("patients", [])]

    def get_panels(self, patient_id: str) -> list[dict[str, Any]]:
        return _store.merged_panels(patient_id)

    def append_lab_record(self, record: dict[str, Any]) -> int:
        return _store.append_record(record)

    def known_lis_patient_ids(self) -> list[str]:
        return _store.known_patient_ids()

    def load_guardian_tokens(self) -> dict[str, Any]:
        return _his._load_guardian_tokens()

    def save_guardian_tokens(self, tokens: dict[str, Any]) -> None:
        _his._save_guardian_tokens(tokens)


class TablestoreRepository:
    """阿里云表格存储后端（v2.4 已实现，连接参数由 A207_OTS_* 环境变量注入）。

    设计要点：
    - 行级写用 PutRow（幂等覆盖），天然支持多写者，替代 JSON 文件锁。
    - patients 嵌套结构（biochemistry/food_diary_5d/nutrition_ceiling/...）序列化为
      JSON 字符串列，保持与 patients.json 数据结构完全一致，业务层零改动。
    - labs 与 labs_store 分开存：get_panels 时按 sample_id 合并（写库优先），
      与 store.merged_panels 语义一致。
    - 二级索引：36 患儿量级 list_patients 用全表 GetRange + 内存筛选即可，
      未来数据量上来再加 ckd_stage/age 二级索引（见 ensure_tables 注释）。
    - 连接参数缺失时 fail-fast 抛错，避免静默回退造成数据双写错乱。
    """

    def __init__(self) -> None:
        self.endpoint = os.environ.get(OTS_ENDPOINT_ENV)
        self.instance = os.environ.get(OTS_INSTANCE_ENV)
        self.ak_id = os.environ.get(OTS_AK_ID_ENV)
        self.ak_secret = os.environ.get(OTS_AK_SECRET_ENV)
        missing = [name for name, val in (
            (OTS_ENDPOINT_ENV, self.endpoint),
            (OTS_INSTANCE_ENV, self.instance),
            (OTS_AK_ID_ENV, self.ak_id),
            (OTS_AK_SECRET_ENV, self.ak_secret),
        ) if not val]
        if missing:
            raise RuntimeError(
                f"Tablestore 后端缺少连接参数：{', '.join(missing)}。"
                f"请设置 A207_STORAGE_BACKEND=tablestore 时一并注入 A207_OTS_* 环境变量。")
        self._client = None  # 惰性建连（_get_client() 首次调用时）

    def _get_client(self):
        """惰性建连（单例）：业务层每请求调用 get_repository()，避免重复握手。"""
        if self._client is None:
            import tablestore  # 延迟导入：JSON 后端无需依赖 SDK

            self._client = tablestore.OTSClient(
                self.endpoint, self.ak_id, self.ak_secret, self.instance)
        return self._client

    # ---- 基础读写 ----

    @staticmethod
    def _pk_patient(patient_id: str) -> list[tuple[str, str]]:
        return [("patient_id", patient_id)]

    @staticmethod
    def _pk_lab(patient_id: str, sample_id: str) -> list[tuple[str, str]]:
        return [("patient_id", patient_id), ("sample_id", sample_id)]

    def _get_row(self, table: str, pk: list[tuple[str, str]]) -> dict[str, Any] | None:
        try:
            _, row, _ = self._get_client().get_row(table, pk)
        except Exception as exc:
            # 五审（2026-08-13）🔴：存储层故障必须 fail-closed 抛错（→ INTERNAL_ERROR），
            # **不得静默返回 None**——此前宽 except 把网络抖动/超时/鉴权失败全部吞掉，
            # 上层 get_patient 等会误判"患者不存在"（NOT_FOUND），医疗数据可信度受损
            # （医生在 Tablestore 抖动时看到"查无此人"而非"系统故障"）。
            # 行不存在（row is None）是 SDK 的正常返回，不抛异常。
            logger.error("Tablestore get_row 失败: table=%s pk=%s exc=%s", table, pk, exc)
            raise RuntimeError(
                f"Tablestore 读取失败（table={table}），详情见服务端日志") from exc
        if row is None:
            return None
        # attribute_columns 为 (name, value, timestamp) 三元组，仅取 name/value
        return {name: value for name, value, _ in row.attribute_columns}

    def _put_row(self, table: str, pk: list[tuple[str, str]],
                 attrs: dict[str, Any]) -> None:
        from tablestore import Condition, Row, RowExistenceExpectation

        condition = Condition(RowExistenceExpectation.IGNORE)
        # SDK 不支持 None 属性值：过滤掉（缺列等价于 JSON 里缺键，读取侧容错）
        clean = {k: v for k, v in attrs.items() if v is not None}
        row = Row(pk, list(clean.items()))
        self._get_client().put_row(table, row, condition)

    def _range_all(self, table: str) -> list[dict[str, Any]]:
        """全表 GetRange（主键升序）。返回 [{primary_key_dict, attrs_dict}]。"""
        from tablestore import INF_MIN, INF_MAX, RowExistenceExpectation

        start = [("patient_id", INF_MIN), ("sample_id", INF_MIN)] \
            if table in (TABLE_LABS, TABLE_LABS_STORE) else [("patient_id", INF_MIN)]
        end = [("patient_id", INF_MAX), ("sample_id", INF_MAX)] \
            if table in (TABLE_LABS, TABLE_LABS_STORE) else [("patient_id", INF_MAX)]
        rows: list[dict[str, Any]] = []
        next_start = start
        while next_start is not None:
            consumed, next_start, row_list, _ = self._get_client().get_range(
                table, "FORWARD", next_start, end, limit=200)
            for row in row_list:  # 6.4.8 返回 Row 对象（.primary_key/.attribute_columns）
                pk_dict = {}
                for k, v in row.primary_key:
                    pk_dict[k] = v.decode() if isinstance(v, bytes) else v
                # attribute_columns 为 (name, value, timestamp) 三元组，仅取 name/value
                attrs_dict = {name: value for name, value, _ in row.attribute_columns}
                rows.append({"pk": pk_dict, "attrs": attrs_dict})
        return rows

    # ---- 数据集元数据 ----

    def get_dataset_meta(self) -> dict[str, Any]:
        """数据集级元数据（as_of/units/schema_version）。

        units 是静态 schema 配置（单位表）、as_of 是基线快照日期——属于「配置」
        而非「运行时数据」，与存储后端解耦：Tablestore 端同样从随包发布的
        patients.json 读取（若文件缺失则返回空 units + as_of=None）。
        """
        try:
            ds = _his.load_dataset()
        except FileNotFoundError:
            return {"as_of": None, "units": {}, "schema_version": None,
                    "patient_count": len(self.all_patient_ids())}
        return {
            "as_of": ds.get("as_of"),
            "units": ds.get("units"),
            "schema_version": ds.get("schema_version"),
            "patient_count": len(self.all_patient_ids()),
        }

    # ---- HIS 患者主数据 ----

    def get_patient(self, patient_id: str) -> dict[str, Any] | None:
        row = self._get_row(TABLE_PATIENTS, self._pk_patient(patient_id))
        if row is None:
            return None
        patient = self._deserialize_patient(row)
        patient["patient_id"] = patient_id  # 主键列不入属性，读回时补回
        return patient

    @staticmethod
    def _deserialize_patient(attrs: dict[str, Any]) -> dict[str, Any]:
        """把 Tablestore 行还原为 patients.json 结构（嵌套列反序列化）。"""
        patient: dict[str, Any] = {}
        for key, value in attrs.items():
            if key in _PAT_NESTED_COLS and isinstance(value, str):
                try:
                    patient[key] = json.loads(value)
                except json.JSONDecodeError:
                    patient[key] = value
            else:
                patient[key] = value
        return patient

    @staticmethod
    def _serialize_patient(patient: dict[str, Any]) -> dict[str, Any]:
        """把 patients.json 结构编码为 Tablestore 属性列（嵌套结构 JSON 化）。

        排除主键列（patient_id 已作为主键，不能重复作为属性列）。
        """
        attrs: dict[str, Any] = {}
        for key, value in patient.items():
            if key == "patient_id":
                continue  # 主键列，不入属性
            if key in _PAT_NESTED_COLS and not isinstance(value, str):
                attrs[key] = json.dumps(value, ensure_ascii=False)
            else:
                attrs[key] = value
        return attrs

    def list_patients(self, criteria: dict[str, Any]) -> list[dict[str, Any]]:
        # 36 患儿量级：全表 GetRange + 内存筛选（未来数据量上来改二级索引）
        from .his import _match  # 复用 his 的筛选谓词（单一事实源）

        criteria = criteria or {}
        rows = self._range_all(TABLE_PATIENTS)
        matched = []
        for item in rows:
            patient = self._deserialize_patient(item["attrs"])
            patient["patient_id"] = item["pk"]["patient_id"]  # 补回主键列
            if all(_match(patient, k, v) for k, v in criteria.items()):
                matched.append({
                    "patient_id": patient["patient_id"],
                    "age_years": patient["age_years"],
                    "age_band": patient["age_band"],
                    "sex": patient["sex"],
                    "ckd_stage": patient["ckd_stage"],
                    "dialysis": patient["dialysis"],
                    "primary_disease": patient["primary_disease"],
                    "has_allergies": bool(patient.get("allergies")),
                })
        return matched

    def all_patient_ids(self) -> list[str]:
        rows = self._range_all(TABLE_PATIENTS)
        return [item["pk"]["patient_id"] for item in rows]

    # ---- LIS 化验数据 ----

    def get_panels(self, patient_id: str) -> list[dict[str, Any]]:
        """基线 + 写库合并（同 sample_id 写库优先），按报告日期升序。

        与 store.merged_panels 语义一致：基线 labs + 写库 labs_store 按 sample_id
        合并覆盖，再按 (report_date, sample_id) 排序。基线行不带 source 键
        （与 LocalJson 端 merged_panels 行为一致），写库行带 source="upsert"。
        """
        panels: dict[str, dict[str, Any]] = {}
        for table in (TABLE_LABS, TABLE_LABS_STORE):
            for item in self._range_all(table):
                if item["pk"].get("patient_id") != patient_id:
                    continue
                attrs = dict(item["attrs"])
                sid = item["pk"]["sample_id"]
                if sid is None or attrs.get("report_date") is None:
                    continue
                values = attrs.get("values")
                if isinstance(values, str):
                    try:
                        values = json.loads(values)
                    except json.JSONDecodeError:
                        values = {}
                if not isinstance(values, dict):
                    continue
                src = attrs.get("source")
                row: dict[str, Any] = {
                    "sample_id": sid,
                    "report_date": attrs["report_date"],
                    "specimen": attrs.get("specimen"),
                    "values": values,
                    "recorded_by": attrs.get("recorded_by"),
                }
                # 仅写库行（TABLE_LABS_STORE）带 source=upsert；基线行不带
                # （与 LocalJson merged_panels 行为对齐：基线无 source 键）。
                if table == TABLE_LABS_STORE and src:
                    row["source"] = src
                panels[sid] = row
        return sorted(panels.values(), key=lambda p: (p["report_date"], p["sample_id"]))

    def append_lab_record(self, record: dict[str, Any]) -> int:
        sid = record.get("sample_id")
        if sid is None:
            raise ValueError("sample_id 必填")
        attrs = {
            "report_date": record.get("report_date"),
            "specimen": record.get("specimen"),
            "values": json.dumps(record.get("values", {}), ensure_ascii=False),
            "source": "upsert",
            "recorded_by": record.get("recorded_by"),
        }
        self._put_row(TABLE_LABS_STORE,
                      self._pk_lab(record["patient_id"], sid), attrs)
        # 返回写库总条数（与 store.append_record 语义一致）
        return len([i for i in self._range_all(TABLE_LABS_STORE)
                    if i["pk"].get("patient_id") == record["patient_id"]])

    def known_lis_patient_ids(self) -> list[str]:
        rows = self._range_all(TABLE_LABS)
        return sorted({item["pk"]["patient_id"] for item in rows})

    # ---- 监护人令牌库 ----

    def load_guardian_tokens(self) -> dict[str, Any]:
        rows = self._range_all(TABLE_GUARDIAN_TOKENS)
        tokens: dict[str, Any] = {}
        for item in rows:
            pid = item["pk"]["patient_id"]
            attrs = dict(item["attrs"])
            token = attrs.get("token")
            if token is None:
                continue
            tokens[pid] = {
                "token": token,
                "issued_at": attrs.get("issued_at"),
                "expires_at": attrs.get("expires_at"),
                "issued_by": attrs.get("issued_by"),
            }
        return tokens

    def save_guardian_tokens(self, tokens: dict[str, Any]) -> None:
        from tablestore import Condition, Row, RowExistenceExpectation

        # 全量覆盖语义（与 JSON 原子写一致）：先清空再批量写
        for item in self._range_all(TABLE_GUARDIAN_TOKENS):
            row = Row([("patient_id", item["pk"]["patient_id"])])
            self._get_client().delete_row(
                TABLE_GUARDIAN_TOKENS, row,
                Condition(RowExistenceExpectation.IGNORE))
        for patient_id, payload in tokens.items():
            attrs = {k: v for k, v in payload.items() if v is not None}
            self._put_row(TABLE_GUARDIAN_TOKENS,
                          [("patient_id", patient_id)], attrs)


def ensure_tablestore_tables() -> None:
    """创建/校验 Tablestore 表（幂等，仅建缺失表）。

    需先设置 A207_OTS_* 环境变量。36 患儿量级暂不建二级索引
    （list_patients 全表 GetRange 内存筛选足够）；数据量增长后可为
    patients 表 ckd_stage/age_band 建二级索引。
    """
    from tablestore import (CapacityUnit, Condition, OTSClient,
                            ReservedThroughput, RowExistenceExpectation,
                            TableMeta, TableOptions)

    endpoint = os.environ[OTS_ENDPOINT_ENV]
    instance = os.environ[OTS_INSTANCE_ENV]
    ak = os.environ[OTS_AK_ID_ENV]
    sk = os.environ[OTS_AK_SECRET_ENV]
    client = OTSClient(endpoint, ak, sk, instance)
    existing = set(client.list_table())

    def _create(table_name: str, pk_schema: list[tuple[str, str]]) -> None:
        if table_name in existing:
            return
        meta = TableMeta(table_name, pk_schema)
        options = TableOptions(time_to_live=-1, max_version=1)
        throughput = ReservedThroughput(capacity_unit=CapacityUnit(0, 0))  # 按量计费
        client.create_table(meta, options, throughput)
        print(f"[ensure] 已创建表 {table_name}")

    _create(TABLE_PATIENTS, [("patient_id", "STRING")])
    _create(TABLE_LABS, [("patient_id", "STRING"), ("sample_id", "STRING")])
    _create(TABLE_LABS_STORE, [("patient_id", "STRING"), ("sample_id", "STRING")])
    _create(TABLE_GUARDIAN_TOKENS, [("patient_id", "STRING")])
    print(f"[ensure] Tablestore 表就绪：{sorted(existing | {TABLE_PATIENTS, TABLE_LABS, TABLE_LABS_STORE, TABLE_GUARDIAN_TOKENS})}")


def get_repository() -> ClinicalDataRepository:
    """按环境变量选择存储后端（默认本地 JSON，零回归）。"""
    backend = os.environ.get(STORAGE_BACKEND_ENV, "json").strip().lower()
    if backend == "tablestore":
        return TablestoreRepository()
    return LocalJsonRepository()


__all__ = [
    "ClinicalDataRepository",
    "LocalJsonRepository",
    "TablestoreRepository",
    "ensure_tablestore_tables",
    "get_repository",
    "TABLE_PATIENTS",
    "TABLE_LABS",
    "TABLE_LABS_STORE",
    "TABLE_GUARDIAN_TOKENS",
]
