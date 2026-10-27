"""
采集器 —— 协议无关的采集核心
============================
把"连接 -> 订阅/轮询 -> 值缓存 -> 报警判断"这一整套从界面里抽出来,
让 PySide6 上位机和 MCP Server 共用同一份逻辑。

以前这段逻辑写死在 app.py 的 Collector 里, 现在两个前端各调一次:

    app.py        : 采集器跑在 QThread 的 asyncio 循环里, 通过 Qt 信号推给 UI
    mcp_server.py : 采集器跑在 MCP 的 asyncio 循环里, 工具直接读缓存

关键点: 采集器**只管采集**, 不管怎么展示、怎么被问。
上层拿到的是一个"活的现场"对象:
    coll.values     当前值缓存   {点位名: LiveValue}
    coll.engine     报警引擎
    coll.soe        SOE 顺序事件记录(只追加)
    coll.errors     连接/配置错误
    coll.connected  是否所有驱动都连上了

订阅能力由驱动自己声明(Driver.supports_subscription()),
不支持订阅的协议(Modbus / S7)自动降级成轮询, 上层完全无感。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from .alarm import AlarmEngine, AlarmEvent
from .base import Driver, Tag
from .factory import build_drivers, check_coverage
from .point_table import PointTable


@dataclass
class LiveValue:
    """一个点位的最新值。

    raw      协议层原始值(寄存器里的整数)
    physical 工程量(经过 scale/offset 换算), 上位机和 LLM 都看这个
    ts       更新时间戳
    quality  GOOD / STALE / BAD —— OPC UA 的 DataValue 里本来就有 Quality,
             这里自己定义一套, 保证跨协议语义一致
    """

    tag: Tag
    raw: Any = None
    physical: Any = None
    ts: float = 0.0
    quality: str = "GOOD"

    @property
    def age_ms(self) -> int:
        """距离上次更新过了多少毫秒。-1 表示从没收到过值。"""
        if not self.ts:
            return -1
        return int((time.time() - self.ts) * 1000)


class Collector:
    """协议无关的采集器。

    用法:
        coll = Collector(table, on_value=cb, on_alarm=cb, on_status=cb)
        await coll.start()
        ...
        await coll.stop()

    start() 之后 coll.connected 为 True 才说明现场真的连上了。
    连接失败不会抛异常, 而是写进 coll.errors —— 上层决定怎么提示。
    """

    def __init__(
        self,
        table: PointTable,
        on_value: Callable[[Tag, Any], None] | None = None,
        on_alarm: Callable[[AlarmEvent], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        poll_period: float = 0.5,
        history_size: int = 300,
    ):
        self.table = table
        self.engine = AlarmEngine(on_event=on_alarm)
        self.values: dict[str, LiveValue] = {}
        self.drivers: dict[str, Driver] = {}
        self.errors: list[str] = []
        self.connected = False
        # 每个连接源各自的状态。现场一台设备坏了, 不该整个上位机跟着白屏。
        self.connected_sources: set[str] = set()
        self.failed_sources: dict[str, str] = {}

        # 每个点位的历史序列 (时间戳, 工程量)。给 AI 做趋势判断用:
        # 光看当前值说不了"是突然跳上去的还是慢慢爬上去的", 这在报警分析里是决定性的。
        self.history: dict[str, deque[tuple[float, Any]]] = {
            t.name: deque(maxlen=history_size) for t in table.points
        }

        self._on_value = on_value
        self._on_status = on_status
        self._poll_period = poll_period
        self._stop_event: asyncio.Event | None = None
        self._poll_tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """连接所有驱动并开始采集。失败写 errors, 不抛异常。"""
        self.engine.register(self.table.points)
        self.drivers = build_drivers(self.table)

        missing = check_coverage(self.table, self.drivers)
        if missing:
            msg = f"点表里的 source 没有驱动: {missing}"
            self.errors.append(msg)
            self._status(f"[FAIL] {msg}")
            return

        for name, drv in self.drivers.items():
            try:
                await drv.connect()
                self.connected_sources.add(name)
                self._status(f"[OK] {name} 已连接")
            except Exception as exc:                 # noqa: BLE001
                # 连接失败只废掉这一个源, 其它源照常跑。
                # 现场常见: 一台仪表掉线, 不该让整个画面全空。
                msg = f"{name} 连接失败: {exc}"
                self.errors.append(msg)
                self.failed_sources[name] = str(exc)
                self._status(f"[FAIL] {msg}")

        if not self.connected_sources:
            self._status("[FAIL] 所有连接源都没连上")
            return

        self.connected = True
        self._stop_event = asyncio.Event()

        for name, drv in self.drivers.items():
            if name not in self.connected_sources:
                continue
            tags = self.table.by_source().get(name, [])
            if not tags:
                continue

            if drv.supports_subscription():
                try:
                    await drv.subscribe(tags, self._on_change)
                    self._status(f"[OK] {name} 已订阅 {len(tags)} 个点位")
                    continue
                except Exception as exc:             # noqa: BLE001
                    # 订阅失败不能要命, 退轮询继续跑 —— 现场这种"订阅建不起来"
                    # 的情况非常多, 掉到轮询至少还能看到数据
                    self._status(f"[WARN] {name} 订阅失败, 退回轮询: {exc}")
            else:
                self._status(f"[OK] {name} 不支持订阅, 改轮询")

            self._poll_tasks.append(asyncio.create_task(self._poll(drv, tags)))

    async def stop(self) -> None:
        """停止采集并断开所有驱动。可重复调用。"""
        self._status("[INFO] 正在断开 ...")

        if self._stop_event is not None:
            self._stop_event.set()

        for task in self._poll_tasks:
            task.cancel()
        for task in self._poll_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:                        # noqa: BLE001
                pass
        self._poll_tasks.clear()

        for drv in self.drivers.values():
            try:
                await drv.disconnect()
            except Exception:                        # noqa: BLE001
                pass

        self.connected = False
        self.connected_sources.clear()
        self._status("[INFO] 已断开")

    # ------------------------------------------------------------------
    # 写操作
    # ------------------------------------------------------------------

    async def write(self, name: str, value: Any) -> None:
        """写点位。只有点表里标了 writable: true 的点才允许写。

        这道闸门是故意的: 现场把写权限交给大模型, 必须显式白名单。
        """
        tag = self.table.get(name)
        if tag is None:
            raise KeyError(f"点位不存在: {name}")
        if not tag.writable:
            raise PermissionError(
                f"点位 '{name}' 在点表里没标 writable: true, 拒绝写入。"
                f"要开放写权限, 在 points.yaml 里给它加上 writable: true。"
            )
        drv = self.drivers.get(tag.source)
        if drv is None:
            raise KeyError(f"没有 {tag.source} 驱动")
        await drv.write(tag, value)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, LiveValue]:
        """当前值缓存的拷贝。"""
        return dict(self.values)

    def find_tags(self, keyword: str) -> list[Tag]:
        """按关键字搜点位: 名称 / 说明 / 地址 任意一处命中就算。"""
        kw = (keyword or "").strip().lower()
        if not kw:
            return list(self.table.points)
        out = []
        for tag in self.table.points:
            hay = f"{tag.name} {tag.desc} {tag.addr} {tag.unit}".lower()
            if kw in hay:
                out.append(tag)
        return out

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _status(self, msg: str) -> None:
        if self._on_status:
            self._on_status(msg)

    def _on_change(self, tag: Tag, raw: Any) -> None:
        """值变化回调。订阅推送和轮询结果都走这里, 只有这一条路径。"""
        physical = tag.to_physical(raw)
        now = time.time()
        self.values[tag.name] = LiveValue(
            tag=tag, raw=raw, physical=physical, ts=now, quality="GOOD"
        )
        hist = self.history.get(tag.name)
        if hist is not None and isinstance(physical, (int, float)) and not isinstance(physical, bool):
            hist.append((now, float(physical)))
        if self._on_value:
            self._on_value(tag, physical)
        # 报警判断用原始值, 引擎内部自己换算成工程量
        self.engine.check(tag, raw)

    async def _poll(self, drv: Driver, tags: list[Tag]) -> None:
        """轮询循环, 给不支持订阅的协议用(Modbus / S7)。

        用 wait_for 等 stop_event 而不是 sleep, 停止信号才能立刻生效。
        """
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                values = await drv.read_many(tags)
                for tag in tags:
                    raw = values.get(tag.name)
                    if raw is not None:
                        self._on_change(tag, raw)
            except Exception as exc:                 # noqa: BLE001
                self._status(f"[WARN] 轮询异常: {exc}")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_period)
            except asyncio.TimeoutError:
                pass
