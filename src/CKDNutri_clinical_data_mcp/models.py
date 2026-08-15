"""a207-lis-mcp 的 pydantic 请求/响应模型。

入参一律白名单校验（extra="forbid"），拒绝未声明字段，防止脏字段写进检验库。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    # P1-2（2026-08-15）：下限 ge=80 与 reference.CRITICAL_RULES 的 na<120 危急自相矛盾——
    # 真实危急低钠（如 78 mmol/L）被 pydantic 拒写，危急样本无法进入系统触发警示。
    # 改为 ge=50（生理上 <50 必为录入错误，覆盖全部危急低钠域）。
    na_mmol_L: float | None = Field(default=None, ge=50, le=200)
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

    # 异口径别名，入口换算（CD-B1 修复 2026-08-14：上限与契约 mmol/L 口径对齐——
    # 此前 p_mg_dL≤25 → 25×0.3229=8.07 > 契约 p_mmol_L≤8，别名放行但归一化拒绝的
    # "陷阱带"；scr/bun/ca/ua 同类越界一并修正。上限 = 契约上限 ÷ 换算系数）
    # P1 一般（2026-08-15）：两轮修正暴露两个方向的问题——第一轮**向下取整**产生
    # 镜像陷阱带（scr_mg_dL≤33 拒 33.5 mg/dL=2961umol/L 合规值）；第二轮**向上取整**
    # 放行超契约值（p_mg_dL≤24.8 → 8.008 mmol/L > 8）。现用**契约上限 ÷ 系数的
    # 精确两位小数**（验证：le×系数 ≤ 契约上限，le+0.01×系数 > 上限）。
    scr_mg_dL: float | None = Field(default=None, ge=0, le=33.93)   # 33.93×88.4=2999.41
    bun_mg_dL: float | None = Field(default=None, ge=0, le=224.08)  # 224.08×0.357=79.997
    p_mg_dL: float | None = Field(default=None, ge=0, le=24.77)     # 24.77×0.3229=7.998
    ca_mg_dL: float | None = Field(default=None, ge=0, le=24.04)    # 24.04×0.2495=5.998
    hb_g_dL: float | None = Field(default=None, ge=0, le=25)
    albumin_g_dL: float | None = Field(default=None, ge=0, le=8)
    ua_mg_dL: float | None = Field(default=None, ge=0, le=25.21)    # 25.21×59.48=1499.49

    @model_validator(mode="after")
    def _check_report_date(self):
        # BUG-67 后补（2026-08-12）：report_date 不得晚于当天——未来日期的化验结果
        # 无法来自真实检验，可能是伪造/时钟错误数据，写入会污染时间序列与趋势窗口。
        # C2（2026-08-15）："当天"统一用 UTC 业务日（date.today() 本地 naive，
        # 跨时区部署"未来日期"判断漂移——UTC+8 部署 00:00-08:00 会把本地今天的
        # 报告误判为未来）。
        if self.report_date > datetime.now(timezone.utc).date():
            raise ValueError(
                f"report_date {self.report_date.isoformat()} 晚于当前日期 "
                f"{datetime.now(timezone.utc).date().isoformat()}，疑似未来数据，拒绝写入")
        return self


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
