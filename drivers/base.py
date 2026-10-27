"""
Driver 抽象层 —— 多协议上位机的地基
===================================
上层(报警 / MCP / LLM / UI)只认这一个接口, 完全不知道底下是 S7 / Modbus / OPC UA。

这就是西门子那篇 ETFA 论文里 "MCP Tool Server 包装 OPC UA 执行接口" 的泛化版:
协议差异全部被 Driver 层吃掉, 交出去的是统一语义。

设计原则:
  1. 全异步(async), 因为现场几千个点必须并发读写
  2. 订阅能力显式建模: OPC UA 有订阅, S7/Modbus 只能轮询 -> 由调用方决定
  3. Tag 里带报警阈值和死区, 让报警引擎跟协议解耦
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

# 值变化的回调: (tag, 原始值)
ChangeCallback = Callable[["Tag", Any], None]


@dataclass
class Tag:
    """一个点位。字段跟点表 YAML 一一对应。

    scale / offset 是工程量换算, 现场仪表很常见:
      仪表寄存器是 250, 实际是 25.0 degC -> scale=0.1
    deadband 是报警死区, 防止值在阈值附近抖动导致报警反复跳。
    """

    name: str
    source: str                       # opcua / modbus / s7 / eip
    addr: str                         # 按协议不同, 含义不同(见各驱动)
    type: str = "float"               # float / int / bool
    scale: float = 1.0
    offset: float = 0.0
    deadband: float = 0.0
    unit: str = ""
    desc: str = ""
    alarm_hi: float | None = None
    alarm_lo: float | None = None
    alarm_level: int = 1              # 1=低 2=中 3=高
    writable: bool = False

    def to_physical(self, raw: Any) -> Any:
        """原始值 -> 工程量。bool 不做换算。"""
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return raw
        return raw * self.scale + self.offset


class Driver(ABC):
    """所有协议的统一接口。

    约定:
      - connect() 可能被反复调用(重连), 实现要幂等
      - read_many() 优先于逐个 read(), 上千点时必须批量
      - subscribe() 回调在协议库的线程/事件循环里触发, 调用方要自己保证线程安全
    """

    name: str = "base"

    @abstractmethod
    async def connect(self) -> None:
        """建立连接。失败要抛异常, 由上层决定重试策略。"""

    @abstractmethod
    async def disconnect(self) -> None:
        """断开连接, 释放资源。要能重复调用。"""

    @abstractmethod
    async def read(self, tag: Tag) -> Any:
        """读单个点位, 返回原始值(未换算)。"""

    @abstractmethod
    async def read_many(self, tags: list[Tag]) -> dict[str, Any]:
        """批量读, 返回 {tag.name: 原始值}。上万个点时必须走这个。"""

    @abstractmethod
    async def write(self, tag: Tag, value: Any) -> None:
        """写点位。"""

    @abstractmethod
    async def subscribe(self, tags: list[Tag], on_change: ChangeCallback) -> None:
        """订阅一批点位, 值变化时调用 on_change(tag, raw_value)。"""

    @abstractmethod
    def supports_subscription(self) -> bool:
        """这个协议能不能订阅。False 的驱动要由上层改用轮询。"""

    # ---- 可选能力 ----

    async def browse(self) -> list[str]:
        """返回服务器上可用的点位地址(OPC UA 专有)。基类不支持。"""
        raise NotImplementedError(f"{self.name} 驱动不支持 browse")
