"""
scada-ai 驱动层
================
上层只认 Driver 接口, 不知道底下是 OPC UA / Modbus / S7 / EtherNet-IP。
"""

from .base import Driver, Tag
from .point_table import PointTable, load_points
from .alarm import AlarmEngine, AlarmEvent, NORMAL, HI, LO
from .factory import build_drivers, check_coverage
from .collector import Collector, LiveValue

__all__ = [
    "Driver",
    "Tag",
    "PointTable",
    "load_points",
    "AlarmEngine",
    "AlarmEvent",
    "NORMAL",
    "HI",
    "LO",
    "build_drivers",
    "check_coverage",
    "Collector",
    "LiveValue",
]