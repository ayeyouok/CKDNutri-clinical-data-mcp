"""a207-lis-mcp 的 pydantic 请求/响应模型。

入参一律白名单校验（extra="forbid"），拒绝未声明字段，防止脏字段写进检验库。
"""

from __future__ import annotations

import re
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator

PATIENT_ID_RE = re.compile(r"^P[0-9]{4,}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PatientQuery(_Strict):
    """只读工具的通用入参。

    BUG-38（2026-08-12）：移除遗留的 caller 字段——身份由部署注入的 A207_CALLER 读取
    （get_caller），权限校验由 _guard_access/enforce_* 完成，模型里传 caller 是 P0-1
    修复前的死字段（传了不用，还占 extra="forbid" 白名单位）。
    """

    patient_id: str

    @field_validator("patient_id")
    @classmethod
    def _check_pid(cls, v: str) -> str:
        if not PATIENT_ID_RE.match(v):
            raise ValueError("patient_id 需匹配 ^P[0-9]{4,}$（如 P0007）")
        return v


class TrendQuery(PatientQuery):
    analyte: str
    window_days: int | None = Field(default=None, ge=1, le=3650)


class LabResultIn(_Strict):
    """upsert_lab_result 的化验载荷。

    契约字段用 pcp-schema 单位；带 _mg_dL / _g_dL 后缀的别名字段会在
    core 入口归一化，防止 Scr、P、Ca 的口径错配。
    """

    report_date: date
    sample_id: str | None = None
    specimen: str | None = None

    # 契约单位字段
    scr_umol_L: float | None = Field(default=None, ge=0, le=3000)
    bun_mmol_L: float | None = Field(default=None, ge=0, le=80)
    egfr_ml_min: float | None = Field(default=None, ge=0, le=250)
    k_mmol_L: float | None = Field(default=None, ge=0, le=12)
    na_mmol_L: float | None = Field(default=None, ge=80, le=200)
    cl_mmol_L: float | None = Field(default=None, ge=50, le=160)
    ca_mmol_L: float | None = Field(default=None, ge=0, le=6)
    p_mmol_L: float | None = Field(default=None, ge=0, le=8)
    hco3_mmol_L: float | None = Field(default=None, ge=0, le=50)
    albumin_g_L: float | None = Field(default=None, ge=0, le=80)
    prealbumin_mg_L: float | None = Field(default=None, ge=0, le=800)
    hb_g_L: float | None = Field(default=None, ge=0, le=250)
    ipth_pg_mL: float | None = Field(default=None, ge=0, le=5000)
    vitd25oh_nmol_L: float | None = Field(default=None, ge=0, le=500)
    ua_umol_L: float | None = Field(default=None, ge=0, le=1500)
    urine_protein_g_24h: float | None = Field(default=None, ge=0, le=30)
    upcr_mg_mmol: float | None = Field(default=None, ge=0, le=3000)

    # 异口径别名，入口换算
    scr_mg_dL: float | None = Field(default=None, ge=0, le=40)
    bun_mg_dL: float | None = Field(default=None, ge=0, le=250)
    p_mg_dL: float | None = Field(default=None, ge=0, le=25)
    ca_mg_dL: float | None = Field(default=None, ge=0, le=25)
    hb_g_dL: float | None = Field(default=None, ge=0, le=25)
    albumin_g_dL: float | None = Field(default=None, ge=0, le=8)
    ua_mg_dL: float | None = Field(default=None, ge=0, le=30)


class UpsertRequest(_Strict):
    patient_id: str
    lab: LabResultIn
    write_mode: bool = True

    @field_validator("patient_id")
    @classmethod
    def _check_pid(cls, v: str) -> str:
        if not PATIENT_ID_RE.match(v):
            raise ValueError("patient_id 需匹配 ^P[0-9]{4,}$（如 P0007）")
        return v


class LabValueOut(BaseModel):
    analyte: str
    label: str
    value: float
    unit: str
    ref_low: float | None = None
    ref_high: float | None = None
    status: str
    status_label: str


class ParentItemOut(BaseModel):
    """家长受限视图条目：不含任何原始数值。"""

    analyte: str
    label: str
    status: str
    status_label: str
    trend: str
    trend_code: str
    in_reference_range: bool
    critical: bool
