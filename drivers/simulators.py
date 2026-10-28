"""
内置模拟设备 —— 没有 PLC 也能把全流程跑通
==========================================
OPC UA / Modbus / S7 都有现成的模拟器(独立的 mock server 进程),
但 **EtherNet/IP 没有**。所以这里在**驱动层**提供一个模拟的 AB 控制器,
模仿 pycomm3 `LogixDriver` 的接口。

这样做的价值:
  - 任何人 clone 下来, 一条命令就能看到四个协议同时在跑
  - 演示和联调不需要真的挂 PLC
  - 上层(MCP 工具 / 界面)的测试不需要真设备

用法:
    python app.py       --points points_all4.yaml --simulate-eip
    python mcp_server.py --points points_all4.yaml --simulate-eip

【边界说明】
这只覆盖 AB 侧的**数据层**(标签路径映射 + 数据类型转换 + 活的值),
**不覆盖 EtherNet/IP 网络层**。接真 PLC 时不要用模拟器。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from .base import Tag
from .point_table import PointTable


@dataclass
class SimTag:
    """模仿 pycomm3 的 Tag 对象(驱动只用到 .value 和 .error)。"""

    value: Any = None
    error: str | None = None


def _default_base(tag: Tag) -> Any:
    """按点位的单位给一个合理的基值, 免得模拟出来全是 0 看不出效果。"""
    if tag.type == "bool":
        return True
    unit = (tag.unit or "").lower()
    if tag.type in ("int", "int16", "uint16", "int32", "uint32"):
        return {"rpm": 1800, "h": 1234}.get(unit, 100)
    if unit == "a":
        return 42.0
    if unit == "t/h":
        return 2.5
    if unit == "degc":
        return 188.0
    if unit == "mpa":
        return 0.045
    return 1.0


class SimulatedLogix:
    """模拟的 AB(Logix) 控制器。

    行为对齐 pycomm3:
      - read(*tags) 返回 Tag 列表, 标签不存在时 value=None 且 error 有值
      - write((path, value)) 接受元组
    值随时间小幅波动, 看起来像活的。
    """

    def __init__(self, base_values: dict[str, Any] | None = None, vary: bool = True):
        self._base = dict(base_values or {})
        self._vary = vary
        self.opened = False
        self.requested: list[str] = []
        self.written: list[tuple[str, Any]] = []

    # ---------------- pycomm3 接口 ----------------

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.opened = False

    def read(self, *tags: str) -> list[SimTag]:
        self.requested.extend(tags)
        t = time.time()
        out: list[SimTag] = []
        for path in tags:
            base = self._base.get(path.split(".")[-1])
            if base is None:
                out.append(SimTag(None, "Tag doesn't exist"))
            else:
                out.append(SimTag(self._value(base, t)))
        return out

    def write(self, *tags_values: Any) -> list[SimTag]:
        out: list[SimTag] = []
        for item in tags_values:
            path, value = item
            self.written.append((path, value))
            self._base[path.split(".")[-1]] = value
            out.append(SimTag(value))
        return out

    # ---------------- 内部 ----------------

    @staticmethod
    def _live(base: Any, t: float) -> Any:
        """在基值附近小幅波动。"""
        if isinstance(base, bool):
            return base
        if isinstance(base, int):
            return int(base + abs(base) * 0.05 * math.sin(t / 6.0))
        return round(base * (1.0 + 0.08 * math.sin(t / 7.0 + 0.5)), 4)

    def _value(self, base: Any, t: float) -> Any:
        return self._live(base, t) if self._vary else base


def simulated_eip_overrides(table: PointTable) -> dict[str, Any]:
    """给点表里所有 eip 类型的连接源生成模拟器覆盖。

    返回 {source名: 构造函数}, 直接传给 Collector(driver_overrides=...)。
    基值从点表里推出来(按单位给合理默认值), 不用额外配置。
    """
    from .eip_driver import EipDriver

    overrides: dict[str, Any] = {}
    for name, cfg in (table.sources or {}).items():
        if str(cfg.get("type", name)).lower() != "eip":
            continue

        base_values: dict[str, Any] = {}
        for tag in table.points:
            if tag.source == name:
                base_values[tag.addr.strip()] = _default_base(tag)

        sim = SimulatedLogix(base_values)

        def factory(c: dict[str, Any], _sim: SimulatedLogix = sim) -> Any:
            return EipDriver(
                path=c.get("path", "127.0.0.1/1/0"),
                program=str(c.get("program", "MainProgram")),
                client=_sim,
            )

        overrides[name] = factory

    return overrides
