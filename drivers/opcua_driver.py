"""
OPC UA 驱动 (asyncua)
=====================
现场几千个点必须用订阅(Subscription), 不能轮询。
asyncua 2.1 起内置 auto_reconnect: 断线自动重连并且自动重建订阅,
所以"断线重连"这个硬指标不用自己写 supervisor 了。
"""

from __future__ import annotations

import asyncio
from typing import Any

from asyncua import Client, ua

from .base import ChangeCallback, Driver, Tag


class _SubHandler:
    """订阅回调。asyncua 在收到服务端推送时调用。"""

    def __init__(self, on_change: ChangeCallback, tag_by_nodeid: dict[str, Tag]):
        self._on_change = on_change
        self._tags = tag_by_nodeid

    def datachange_notification(self, node, val, data):
        nodeid = node.nodeid.to_string()
        tag = self._tags.get(nodeid)
        if tag is None:
            # 兜底: 按后缀匹配, 防止 namespace 写法不一致
            for key, t in self._tags.items():
                if key.split(";s=")[-1] == nodeid.split(";s=")[-1]:
                    tag = t
                    break
        if tag is not None:
            self._on_change(tag, val)

    def event_notification(self, event):
        # 事件/报警走 OPC UA A&C, asyncua 支持不完整, 暂不处理
        pass


class OpcUaDriver(Driver):
    name = "opcua"

    def __init__(
        self,
        url: str,
        namespace_uri: str | None = None,
        session_timeout: int = 600000,
        sampling_interval: int = 500,
    ):
        self.url = url
        self.namespace_uri = namespace_uri
        self.session_timeout = session_timeout
        self.sampling_interval = sampling_interval
        self.client: Client | None = None
        self._sub = None
        self._ns_index: int | None = None

    # ------------------------------------------------------------------

    async def connect(self) -> None:
        if self.client is not None:
            return

        self.client = Client(url=self.url, auto_reconnect=True)
        self.client.session_timeout = self.session_timeout
        await self.client.connect()

        if self.namespace_uri:
            self._ns_index = await self.client.get_namespace_index(self.namespace_uri)

    async def disconnect(self) -> None:
        if self._sub is not None:
            try:
                await self._sub.delete()
            except Exception:
                pass
            self._sub = None
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

    # ------------------------------------------------------------------

    def _resolve(self, tag: Tag):
        """把点表里的 addr 解析成 Node 对象。

        支持两种写法:
          "ns=2;s=Plant1.Line1.MillCurrent"   完整 NodeId 字符串(推荐, 现场通用)
          "Plant1.Line1.MillCurrent"           裸标识符, 需要配 namespace_uri
        """
        assert self.client is not None, "还没 connect"
        if tag.addr.startswith("ns="):
            return self.client.get_node(tag.addr)
        if self._ns_index is None:
            raise RuntimeError(
                f"点表 {tag.name} 用的是裸地址 {tag.addr}, 但没配 namespace_uri"
            )
        return self.client.get_node(ua.NodeId(tag.addr, self._ns_index))

    async def read(self, tag: Tag) -> Any:
        return await self._resolve(tag).read_value()

    async def read_many(self, tags: list[Tag]) -> dict[str, Any]:
        if not tags:
            return {}
        nodes = [self._resolve(t) for t in tags]
        values = await self.client.read_values(nodes)
        return {t.name: v for t, v in zip(tags, values)}

    async def write(self, tag: Tag, value: Any) -> None:
        await self._resolve(tag).write_value(value)

    async def subscribe(self, tags: list[Tag], on_change: ChangeCallback) -> None:
        assert self.client is not None, "还没 connect"
        by_nodeid = {self._resolve(t).nodeid.to_string(): t for t in tags}
        self._sub = await self.client.create_subscription(
            self.sampling_interval, _SubHandler(on_change, by_nodeid)
        )
        for tag in tags:
            await self._sub.subscribe_data_change(self._resolve(tag))

    def supports_subscription(self) -> bool:
        return True

    # ------------------------------------------------------------------

    async def browse(self) -> list[str]:
        """列出服务器上的点位(NodeId 字符串)。现场点表对不上时用它来对账。"""
        assert self.client is not None
        objects = self.client.nodes.objects
        found: list[str] = []

        async def walk(node, depth: int = 0):
            if depth > 4:
                return
            try:
                children = await node.get_children()
            except Exception:
                return
            for child in children:
                nid = child.nodeid.to_string()
                found.append(nid)
                try:
                    bname = await child.read_browse_name()
                except Exception:
                    continue
                if bname.Name in ("Server", "Types", "Views", "ObjectTypes", "VariableTypes"):
                    continue
                await walk(child, depth + 1)

        await walk(objects)
        return found
