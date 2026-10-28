"""
S7 驱动 (python-snap7)
======================
西门子 S7Comm 协议。现场最常见的读法是 **读 DB 块**:
    DB1.DBD0    给煤机设定量  (REAL)
    DB1.DBW10   转速          (INT)

点表 addr 格式:
    "db:1:0"      DB1 字节偏移 0
    "db:1:0:2"    DB1 字节偏移 0, 第 4 段按类型解释
    "mb:0:10"     位存储区 M, 字节 10
    "eb:0:0"      输入区 E, 字节 0
    "ab:0:0"      输出区 A, 字节 0

第 4 段的含义按类型走:
    bool   -> 位号 (0-7), 默认 0,  即 DBX<byte>.<bit>
    string -> 字符串最大长度, 默认 20
其它类型不用第 4 段, 长度按类型自动算。

【字节序 —— 和 Modbus 正好相反】
S7 数据区是**大端**。同一个 float32:
    S7 的 DB 里   42 C7 00 00
    Modbus 里可能 00 00 C7 42
所以 S7 这边**不用配字节序, 配了反而错**。
Modbus 要现场实测字节序, S7 不用 —— 这是两者的关键差别。

【为什么用 AsyncClient 而不是 Client】
python-snap7 的 Client 是同步的, 在 asyncio 里直接调会**阻塞整个事件循环**
(和 Modbus 那个坑一模一样: 一个协议的同步调用拖死所有协议)。
3.3.0 提供了 AsyncClient, 不用自己包线程。
"""

from __future__ import annotations

import struct
from typing import Any

from snap7 import AsyncClient
from snap7.type import Area

from .base import ChangeCallback, Driver, Tag

# 点表 area 前缀 -> snap7 区域
#   db = 数据块   mb = 位存储区(M)   eb = 输入区(E)   ab = 输出区(A)
_AREAS: dict[str, Area] = {
    "db": Area.DB,
    "mb": Area.MK,
    "eb": Area.PE,
    "ab": Area.PA,
}

# 类型 -> 字节宽度
_TYPE_WIDTH: dict[str, int] = {
    "bool": 1,
    "int16": 2,
    "uint16": 2,
    "int32": 4,
    "uint32": 4,
    "float32": 4,
    "float64": 8,
}

# S7 STRING: 前 2 字节是 [最大长度, 当前长度], 之后是字符
_STRING_HEADER = 2


def _decode_bool(raw: bytes, bit: int) -> bool:
    return bool(raw[0] & (1 << bit))


def _decode_string(raw: bytes) -> str:
    """S7 STRING 解码。raw[0]=最大长度, raw[1]=当前长度, 之后是字符。"""
    if len(raw) < _STRING_HEADER:
        return ""
    cur = raw[1]
    data = raw[_STRING_HEADER:_STRING_HEADER + cur]
    return data.decode("latin-1", errors="replace").rstrip("\x00")


def _encode_string(value: Any, max_len: int) -> bytes:
    """S7 STRING 编码: [最大长度, 当前长度, 字符...]"""
    text = str(value)
    if len(text) > max_len:
        text = text[:max_len]
    body = text.encode("latin-1", errors="replace")
    return bytes([max_len, len(body)]) + body


class Snap7Driver(Driver):
    name = "s7"

    def __init__(
        self,
        host: str,
        port: int = 102,
        rack: int = 0,
        slot: int = 1,
        timeout: float = 3.0,
    ):
        self.host = host
        self.port = port
        self.rack = rack
        self.slot = slot
        self.timeout = timeout
        self.client: AsyncClient | None = None

    # ------------------------------------------------------------------

    async def connect(self) -> None:
        if self.client is not None:
            return
        self.client = AsyncClient()
        await self.client.connect(self.host, self.rack, self.slot, self.port)

    async def disconnect(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:                        # noqa: BLE001
                pass
            self.client = None

    # ------------------------------------------------------------------
    # 地址解析
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_addr(addr: str) -> tuple[str, int, int, int]:
        """'db:1:0:3' -> ('db', 1, 0, 3)

        返回 (区前缀, DB号, 字节偏移, 附加参数)
        附加参数: bool 类型是位号, string 类型是最大长度, 其它类型是 0
        """
        parts = str(addr).split(":")
        area = parts[0].strip().lower()
        if area not in _AREAS:
            raise ValueError(
                f"未知存储区 '{area}', 可用: {list(_AREAS)}"
            )
        db_number = int(parts[1]) if len(parts) > 1 else 0
        offset = int(parts[2]) if len(parts) > 2 else 0
        extra = int(parts[3]) if len(parts) > 3 else 0
        return area, db_number, offset, extra

    def _width(self, tag: Tag, extra: int) -> int:
        """这个点占多少字节。"""
        if tag.type == "string":
            return _STRING_HEADER + (extra or 20)
        width = _TYPE_WIDTH.get(tag.type)
        if width is None:
            raise ValueError(f"S7 驱动不支持的类型 '{tag.type}'")
        return width

    # ------------------------------------------------------------------

    async def read(self, tag: Tag) -> Any:
        result = await self.read_many([tag])
        return result.get(tag.name)

    async def read_many(self, tags: list[Tag]) -> dict[str, Any]:
        if not tags:
            return {}
        assert self.client is not None, "还没 connect"

        out: dict[str, Any] = {}

        # 按 (区, DB号) 分组
        groups: dict[tuple[str, int], list[Tag]] = {}
        for t in tags:
            area, db, _, _ = self._parse_addr(t.addr)
            groups.setdefault((area, db), []).append(t)

        for (area_key, db_number), group in groups.items():
            area = _AREAS[area_key]
            # 连续地址合并成一次读
            for start, size, batch in self._merge(group):
                raw = await self.client.read_area(area, db_number, start, size)
                for t in batch:
                    _, _, offset, extra = self._parse_addr(t.addr)
                    chunk = bytes(raw[offset - start: offset - start + self._width(t, extra)])
                    out[t.name] = self._decode(t, chunk, extra)

        return out

    @staticmethod
    def _merge(group: list[Tag]) -> list[tuple[int, int, list[Tag]]]:
        """把偏移连续的同类点合并成一次批量读。

        返回 [(起始偏移, 字节数, [Tag...]), ...]
        """
        items: list[tuple[int, int, Tag]] = []
        for t in group:
            _, _, offset, extra = Snap7Driver._parse_addr(t.addr)
            width = (
                _STRING_HEADER + (extra or 20)
                if t.type == "string"
                else _TYPE_WIDTH.get(t.type, 1)
            )
            items.append((offset, width, t))
        items.sort(key=lambda x: x[0])

        merged: list[tuple[int, int, list[Tag]]] = []
        for offset, width, tag in items:
            if merged:
                m_start, m_size, m_tags = merged[-1]
                if offset <= m_start + m_size:        # 连续或重叠
                    merged[-1] = (m_start, max(m_size, offset + width - m_start), m_tags + [tag])
                    continue
            merged.append((offset, width, [tag]))
        return merged

    def _decode(self, tag: Tag, raw: bytes, extra: int) -> Any:
        if not raw:
            return None
        t = tag.type
        if t == "bool":
            return _decode_bool(raw, extra)
        if t == "int16":
            return struct.unpack_from(">h", raw, 0)[0]
        if t == "uint16":
            return struct.unpack_from(">H", raw, 0)[0]
        if t == "int32":
            return struct.unpack_from(">i", raw, 0)[0]
        if t == "uint32":
            return struct.unpack_from(">I", raw, 0)[0]
        if t == "float32":
            return struct.unpack_from(">f", raw, 0)[0]
        if t == "float64":
            return struct.unpack_from(">d", raw, 0)[0]
        if t == "string":
            return _decode_string(raw)
        return None

    def _encode(self, tag: Tag, value: Any, extra: int) -> bytes:
        t = tag.type
        if t == "bool":
            return bytes([1 if value else 0])
        if t == "int16":
            return struct.pack(">h", int(value))
        if t == "uint16":
            return struct.pack(">H", int(value))
        if t == "int32":
            return struct.pack(">i", int(value))
        if t == "uint32":
            return struct.pack(">I", int(value))
        if t == "float32":
            return struct.pack(">f", float(value))
        if t == "float64":
            return struct.pack(">d", float(value))
        if t == "string":
            return _encode_string(value, extra or 20)
        raise ValueError(f"S7 驱动不支持的类型 '{t}'")

    async def write(self, tag: Tag, value: Any) -> None:
        assert self.client is not None, "还没 connect"
        area_key, db_number, offset, extra = self._parse_addr(tag.addr)
        area = _AREAS[area_key]
        data = self._encode(tag, value, extra)
        await self.client.write_area(area, db_number, offset, data)

    async def subscribe(self, tags: list[Tag], on_change: ChangeCallback) -> None:
        raise NotImplementedError("S7 没有订阅机制, 上层应该改用轮询")

    def supports_subscription(self) -> bool:
        return False
