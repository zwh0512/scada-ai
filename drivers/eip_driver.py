"""
AB (罗克韦尔) EtherNet/IP 驱动 (pycomm3)
========================================
Micro800 / CompactLogix / ControlLogix 通用。

点表 addr 就是**控制器里的标签名**, 两种写法都支持:
    "CoalRate"                        -> 自动补成 Program:<program>.CoalRate
    "Program:MainProgram.CoalRate"   -> 显式完整路径, 原样使用
    "Motor.Speed"                     -> UDT 成员访问, 原样使用

【为什么全部用 asyncio.to_thread 包起来】
pycomm3 **没有异步版本**, 它的 open/read/write 都是阻塞的网络调用。
直接在协程里调, 会**阻塞整个事件循环** —— 和 Modbus 那个坑一模一样:
一个协议的同步调用会把 OPC UA 的订阅推送、其它协议的轮询全卡住。
所以这里的规矩是: **同步协议库一律 `await asyncio.to_thread(...)`**。

【为什么不做订阅】
EtherNet/IP 的 CIP 有"订阅"服务, 但 pycomm3 没实现, 现场上位机
也都是轮询。`supports_subscription()` 返回 False, 上层自动降级轮询。
"""

from __future__ import annotations

import asyncio
from typing import Any

from .base import ChangeCallback, Driver, Tag

try:
    from pycomm3 import LogixDriver
except ImportError:                                  # pragma: no cover
    LogixDriver = None                               # type: ignore[assignment]


class EipDriver(Driver):
    name = "eip"

    def __init__(
        self,
        path: str,
        program: str = "MainProgram",
        timeout: float = 10.0,
        client: Any | None = None,
    ):
        """
        path    CIP 路径, 常见写法:
                  "192.168.1.10"        ControlLogix 槽位 0 / CompactLogix
                  "192.168.1.10/1"     ControlLogix 槽位 1
        program 程序名, 点表里只写标签名时用它补全路径
        client  可注入的 pycomm3 客户端(测试用), 传了就直接用
        """
        self.path = path
        self.program = program
        self.timeout = timeout
        self.client: Any | None = client
        self._connected = False

    # ------------------------------------------------------------------

    async def connect(self) -> None:
        if self._connected:
            return
        if self.client is None:
            if LogixDriver is None:
                raise RuntimeError("没装 pycomm3, 用 uv pip install pycomm3")
            self.client = LogixDriver(self.path, timeout=self.timeout)
        # 阻塞调用, 必须挪到线程里
        await asyncio.to_thread(self.client.open)
        self._connected = True

    async def disconnect(self) -> None:
        if self.client is not None:
            try:
                await asyncio.to_thread(self.client.close)
            except Exception:                        # noqa: BLE001
                pass
            self.client = None
        self._connected = False

    # ------------------------------------------------------------------

    def _path(self, tag: Tag) -> str:
        """点表里的标签名 -> CIP 完整路径。

        带 ":" 的是显式路径, 原样用; 纯标签名才补程序前缀。
        """
        addr = tag.addr.strip()
        if ":" in addr:
            return addr
        return f"Program:{self.program}.{addr}"

    def _coerce(self, tag: Tag, value: Any) -> Any:
        """按点表声明的类型做一次转换。

        pycomm3 返回的是控制器里的原生类型, 但点表是工程量口径,
        这里统一一下, 上层和 MCP 工具看到的类型才一致。
        """
        if value is None:
            return None
        try:
            if tag.type in ("float", "float32"):
                return float(value)
            if tag.type in ("int", "int16", "int32", "uint", "uint16", "uint32"):
                return int(value)
            if tag.type == "bool":
                return bool(value)
            if tag.type == "string":
                return str(value)
        except (TypeError, ValueError):
            return value
        return value

    async def read(self, tag: Tag) -> Any:
        result = await self.read_many([tag])
        return result.get(tag.name)

    async def read_many(self, tags: list[Tag]) -> dict[str, Any]:
        if not tags:
            return {}
        assert self.client is not None, "还没 connect"

        paths = [self._path(t) for t in tags]
        # 阻塞调用, 挪到线程里
        results = await asyncio.to_thread(self.client.read, *paths)

        # read() 单个标签返回 Tag, 多个返回 list
        if not isinstance(results, (list, tuple)):
            results = [results]

        out: dict[str, Any] = {}
        for tag, res in zip(tags, results):
            value = getattr(res, "value", None)
            error = getattr(res, "error", None)
            out[tag.name] = None if error else self._coerce(tag, value)
        return out

    async def write(self, tag: Tag, value: Any) -> None:
        assert self.client is not None, "还没 connect"
        path = self._path(tag)
        await asyncio.to_thread(self.client.write, (path, value))

    async def subscribe(self, tags: list[Tag], on_change: ChangeCallback) -> None:
        raise NotImplementedError("EtherNet/IP 轮询模式, 上层应该改用轮询")

    def supports_subscription(self) -> bool:
        return False
