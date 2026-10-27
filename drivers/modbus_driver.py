"""
Modbus 驱动 (pymodbus)
======================
Modbus 没有订阅, 只能轮询 -> supports_subscription() 返回 False,
上层据此自动降级成轮询模式。这是抽象层要显式表达的事。

地址格式 (点表 addr 字段):
    "hr:0"     holding register 起始 0
    "ir:10"    input register 起始 10
    "co:3"     coil 3
类型决定怎么解码:
    int16   1 个寄存器, 有符号
    uint16  1 个寄存器, 无符号
    float32 2 个寄存器, **字节序/字序由 source 配置决定**

字节序是工业第一大坑:
    abcd = 大端字节 + 大端字 (默认)
    badc = 大端字节 + 小端字
    cdab = 大端字节 + 字交换
    dcba = 全小端
不同厂商仪表默认值不一样, 必须现场实测。

【踩过的坑 —— 必须用异步 client】
一开始用的是同步的 `ModbusTcpClient`, 在 asyncio 里调它会**阻塞整个事件循环**。
后果不是"慢一点", 而是: OPC UA 的订阅推送、其它协议的轮询、MCP 工具调用,
全部被这一个 Modbus 读卡住。现场一个从站超时 = 整个上位机界面冻住。
pymodbus 3.7+ 提供了 `AsyncModbusTcpClient`, 它的方法要 await:
    rr = await client.read_holding_registers(addr, count=2, device_id=1)
"""

from __future__ import annotations

import struct
from typing import Any

from pymodbus.client import AsyncModbusTcpClient

from .base import ChangeCallback, Driver, Tag

# 功能码前缀
AREA_HR = "hr"     # holding register
AREA_IR = "ir"     # input register
AREA_CO = "co"     # coil

_WORD_ORDER = {
    "abcd": (0, 1, 2, 3),
    "badc": (1, 0, 3, 2),
    "cdab": (2, 3, 0, 1),
    "dcba": (3, 2, 1, 0),
}

# 每个类型占几个寄存器
_REG_WIDTH = {
    "bool": 1,
    "int16": 1,
    "uint16": 1,
    "float32": 2,
}


class ModbusDriver(Driver):
    name = "modbus"

    def __init__(
        self,
        host: str,
        port: int = 502,
        unit_id: int = 1,
        word_order: str = "abcd",
        timeout: float = 3.0,
    ):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        if word_order not in _WORD_ORDER:
            raise ValueError(f"word_order 只能是 {list(_WORD_ORDER)}")
        self.word_order = word_order
        self.client: AsyncModbusTcpClient | None = None

    # ------------------------------------------------------------------

    async def connect(self) -> None:
        if self.client is not None:
            return
        self.client = AsyncModbusTcpClient(
            self.host, port=self.port, timeout=self.timeout
        )
        ok = await self.client.connect()
        if not ok:
            self.client = None
            raise ConnectionError(f"Modbus TCP 连不上 {self.host}:{self.port}")

    async def disconnect(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            except Exception:                        # noqa: BLE001
                pass
            self.client = None

    # ------------------------------------------------------------------

    @staticmethod
    def _parse_addr(addr: str) -> tuple[str, int, int]:
        """'hr:0:2' -> ('hr', 0, 2)"""
        parts = addr.split(":")
        area = parts[0].strip().lower()
        start = int(parts[1])
        count = int(parts[2]) if len(parts) > 2 else 1
        return area, start, count

    def _decode(self, tag: Tag, regs: list[int]) -> Any:
        if tag.type == "bool":
            return bool(regs[0])
        if tag.type in ("int16", "uint16"):
            v = regs[0]
            if tag.type == "int16" and v > 32767:
                v -= 65536
            return v
        if tag.type == "float32":
            raw = b"".join(r.to_bytes(2, "big") for r in regs[:2])
            order = _WORD_ORDER[self.word_order]
            swapped = bytes(raw[i] for i in order)
            return struct.unpack(">f", swapped)[0]
        return regs[0]

    def _encode(self, tag: Tag, value: Any) -> list[int]:
        """float32 -> 2 个寄存器。编码是解码的逆过程。"""
        if tag.type != "float32":
            return [int(value)]
        canonical = struct.pack(">f", float(value))
        order = _WORD_ORDER[self.word_order]
        out_bytes = bytes(canonical[i] for i in order)
        return [
            int.from_bytes(out_bytes[0:2], "big"),
            int.from_bytes(out_bytes[2:4], "big"),
        ]

    # ------------------------------------------------------------------

    async def read(self, tag: Tag) -> Any:
        result = await self.read_many([tag])
        return result.get(tag.name)

    async def read_many(self, tags: list[Tag]) -> dict[str, Any]:
        if not tags:
            return {}
        assert self.client is not None, "还没 connect"

        out: dict[str, Any] = {}

        # 按区域分组
        groups: dict[str, list[Tag]] = {}
        for t in tags:
            area, _, _ = self._parse_addr(t.addr)
            groups.setdefault(area, []).append(t)

        for area, group in groups.items():
            if area == AREA_CO:
                for t in group:
                    _, addr, _ = self._parse_addr(t.addr)
                    rr = await self.client.read_coils(addr, count=1, device_id=self.unit_id)
                    out[t.name] = bool(rr.bits[0]) if not rr.isError() else None
                continue

            # 连续的地址合并成一次请求 —— 上千个点时一个一个读会把网络打满
            for start, count, batch in self._merge(group):
                if area == AREA_HR:
                    rr = await self.client.read_holding_registers(
                        start, count=count, device_id=self.unit_id
                    )
                else:
                    rr = await self.client.read_input_registers(
                        start, count=count, device_id=self.unit_id
                    )
                if rr.isError():
                    for t in batch:
                        out[t.name] = None
                    continue
                regs = list(rr.registers)
                for t in batch:
                    _, tag_start, _ = self._parse_addr(t.addr)
                    offset = tag_start - start
                    width = _REG_WIDTH.get(t.type, 1)
                    chunk = regs[offset:offset + width]
                    out[t.name] = self._decode(t, chunk) if chunk else None

        return out

    @staticmethod
    def _merge(group: list[Tag]) -> list[tuple[int, int, list[Tag]]]:
        """把地址连续的同类点合并成一次批量读。

        返回 [(起始地址, 寄存器数, [Tag...]), ...]
        连续性判断按"每个点的实际占位宽度"算, float32 占 2 个寄存器。
        """
        items = []
        for t in group:
            _, start, _ = ModbusDriver._parse_addr(t.addr)
            items.append((start, _REG_WIDTH.get(t.type, 1), t))
        items.sort(key=lambda x: x[0])

        merged: list[tuple[int, int, list[Tag]]] = []
        for start, width, tag in items:
            if merged:
                m_start, m_count, m_tags = merged[-1]
                if start <= m_start + m_count:      # 与上一个连续或重叠
                    new_count = max(m_count, start + width - m_start)
                    merged[-1] = (m_start, new_count, m_tags + [tag])
                    continue
            merged.append((start, width, [tag]))
        return merged

    async def write(self, tag: Tag, value: Any) -> None:
        assert self.client is not None, "还没 connect"
        area, start, count = self._parse_addr(tag.addr)
        if area == AREA_CO:
            await self.client.write_coil(start, bool(value), device_id=self.unit_id)
            return
        if area == AREA_HR:
            regs = self._encode(tag, value)
            await self.client.write_registers(start, regs, device_id=self.unit_id)
        else:
            raise ValueError("input register 是只读的, 不能写")

    async def subscribe(self, tags: list[Tag], on_change: ChangeCallback) -> None:
        raise NotImplementedError("Modbus 没有订阅机制, 上层应该改用轮询")

    def supports_subscription(self) -> bool:
        return False
