"""
驱动工厂 —— 按点表的 source 字段把点位路由到对应驱动
======================================================
这就是"多协议统一"的落地处:
    点表里 source: opcua 的点 -> OpcUaDriver
    点表里 source: modbus 的点 -> ModbusDriver
上层拿到的永远是同一个 Driver 接口, 协议差异在这一层被吃掉。

加新协议只要三步:
    1. 写一个 drivers/xxx_driver.py, 实现 Driver 的 6 个方法
    2. 在本文件的 build_drivers() 里加一个分支
    3. 点表里写 source: xxx
上层一行都不用改。
"""

from __future__ import annotations

from .base import Driver
from .point_table import PointTable


def build_drivers(table: PointTable) -> dict[str, Driver]:
    """按点表里的 sources 配置建驱动。返回 {source名: 驱动实例}。"""
    drivers: dict[str, Driver] = {}

    for source, cfg in (table.sources or {}).items():
        kind = str(cfg.get("type", source)).lower()

        if kind == "opcua":
            from .opcua_driver import OpcUaDriver

            drivers[source] = OpcUaDriver(
                url=cfg["url"],
                namespace_uri=cfg.get("namespace_uri"),
                sampling_interval=int(cfg.get("sampling_interval", 500)),
            )

        elif kind == "modbus":
            from .modbus_driver import ModbusDriver

            drivers[source] = ModbusDriver(
                host=cfg["host"],
                port=int(cfg.get("port", 502)),
                unit_id=int(cfg.get("unit_id", 1)),
                word_order=str(cfg.get("word_order", "abcd")).lower(),
            )

        else:
            raise ValueError(
                f"未知协议 '{kind}' (source='{source}')。"
                f"已支持: opcua, modbus"
            )

    return drivers


def check_coverage(table: PointTable, drivers: dict[str, Driver]) -> list[str]:
    """检查点表里有没有哪个 source 没配驱动。返回缺失的 source 列表。"""
    used = set(table.by_source().keys())
    return sorted(used - set(drivers.keys()))
