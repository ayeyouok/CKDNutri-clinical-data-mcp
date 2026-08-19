"""统一业务日期（2026-08-19，审查 ④）：全项目唯一"今天"定义。

生产业务规定使用 UTC 业务日（对齐 recorded_at/created_at 全局 UTC 口径）——
本函数是**唯一**允许计算"今天"的地方：禁止各模块自行
`datetime.now(timezone.utc).date()` / `date.today()`（本地 naive 与 UTC 口径
混排会造成跨时区部署的日期判定漂移）。未来若医院业务改为本地时区
（如 Asia/Tokyo），只需修改本函数一处。
"""
from datetime import date, datetime, timezone


def business_today() -> date:
    """返回统一业务日期（UTC 业务日，不含时分秒）。"""
    return datetime.now(timezone.utc).date()
