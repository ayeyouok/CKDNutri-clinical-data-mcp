"""CKDNutri-clinical-data-mcp：儿童 CKD 临床数据域（P1，合并自 M1 HIS + M2 LIS）。

职责：
- HIS 患者主索引（get_patient_profile / get_diagnosis / list_patients / get_nutrition_ceiling）
- LIS 化验面板（get_labs / get_critical_values / get_lab_trend / upsert_lab_result）
- 家长受限视图（guaranteed by guardian_token 绑定核验，F1/F4）
- 监护人令牌签发与绑定（issue_guardian_token / verify_guardian_binding）

P0-1 修复（2026-08-12）：本文件此前缺失，导致 setuptools find_packages（非 namespace 模式）
在打包时漏收本包全部模块 —— 源码可直接运行（PEP 420 命名空间包），但 wheel 安装后
import 失败、P1 服务无法启动。补上顶层 __init__.py 以修复打包。

除统一策略包 a207-policy（身份注入 / 权限矩阵 / 状态路径）外，无对其他 a207-* 包的 import。
"""
from __future__ import annotations

from importlib import metadata as _metadata


def _pkg_version() -> str:
    """从安装元数据读取版本（P2-6：与 pyproject.toml 单一事实源对齐）。未安装时回退 "0.0.0"。

    2026-08-12 加固：本函数在模块 import 顶层执行（__version__ = _pkg_version()），
    任何未捕获异常都会让包加载失败、服务无法启动。除 PackageNotFoundError（未安装）、
    TypeError / AttributeError（异常 metadata 实现）外，损坏的 .dist-info / 特殊运行时
    还可能抛 ValueError 等——版本号仅审计展示，缺失回退 "0.0.0" 无功能影响，
    故通配兜底（fail-open：宁可无版本号，不可阻断加载）。
    """
    try:
        return _metadata.version("CKDNutri-clinical-data-mcp")
    except Exception:  # noqa: BLE001 - 通配兜底（见 docstring 理由）
        return "0.0.0"


__version__ = _pkg_version()

__all__ = ["__version__"]
