"""
S7 + AB(EtherNet/IP) 驱动 + 四协议同表采集 端到端自测
======================================================
阶段 2 验收脚本。验证 7 组:

    1. 能连上真正的 S7Comm 从站(snap7 Server 模拟的 PLC)
    2. 能读 DB1 / DB2, 工程量换算正确
    3. S7 类型解码全套: REAL / INT / UINT / BOOL
    4. S7 写往返(写完再读还是那个值)
    5. AB 标签路径映射 + 类型转换
    6. AB 写
    7. **四协议同表采集**: OPC UA + Modbus + S7 + AB 一个采集器

【AB 为什么要用假客户端】
EtherNet/IP 没有现成的模拟器(snap7 和 pymodbus 都有)。
所以这里用 pycomm3 API 的假实现注入, 测的是**标签路径映射和数据类型转换**
这两层逻辑 —— 网络层要接真 PLC 才能验。这点 README 里写清楚了, 不吹。

跑法:
    .\\run.ps1 test_s7_eip.py

退出码 0 = 全过。
"""

from __future__ import annotations

import asyncio
import logging
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from drivers import Collector, load_points
from drivers.eip_driver import EipDriver
from drivers.simulators import SimulatedLogix
from drivers.snap7_driver import Snap7Driver

# asyncua 断开时的 BadNoSubscription 是正常收尾噪音
logging.getLogger("asyncua").setLevel(logging.WARNING)

HERE = Path(__file__).resolve().parent
PY = sys.executable
POINTS = HERE / "points_all4.yaml"

_failures: list[str] = []


def banner(text: str) -> None:
    print()
    print("=" * 62)
    print(text)
    print("=" * 62)


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"{'[OK]  ' if ok else '[FAIL]'} {label}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        _failures.append(label)
    return ok


def wait_port(host: str, port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def wait_port_closed(host: str, port: int, timeout: float = 8.0) -> bool:
    """等端口真正释放 —— 上一轮的 mock 可能还在退出中。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                time.sleep(0.3)
        except OSError:
            return True
    return False


def start_server(script: str, port: int, *args: str) -> subprocess.Popen | None:
    if wait_port("127.0.0.1", port, timeout=0.5):
        wait_port_closed("127.0.0.1", port, timeout=8.0)
        if wait_port("127.0.0.1", port, timeout=0.5):
            print(f"      (端口 {port} 被占用, 复用现成的 server)")
            return None

    proc = subprocess.Popen(
        [PY, str(HERE / script), *args],
        cwd=str(HERE),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(80):
        if wait_port("127.0.0.1", port, timeout=0.2):
            return proc
        time.sleep(0.25)
    proc.terminate()
    raise RuntimeError(f"mock server {script} 启动失败, 端口 {port} 没起来")


# =====================================================================
# AB 的模拟控制器 —— 用项目内置的 drivers.simulators
# =====================================================================


def make_fake() -> SimulatedLogix:
    """固定基值、不波动, 方便断言精确值。"""
    return SimulatedLogix(
        {
            "AB给煤量设定": 3.2,
            "AB给煤机转速": 1780,
            "AB磨机电流": 41.6,
            "AB一次风温度": 188.3,
            "AB运行标志": True,
        },
        vary=False,
    )


# =====================================================================


async def test_s7() -> None:
    banner("[1/4] S7 驱动: 连接 + 读 DB")
    drv = Snap7Driver(host="127.0.0.1", port=10200, rack=0, slot=1)
    try:
        await drv.connect()
        check("连上 S7 从站", True, "127.0.0.1:10200 rack0/slot1")
    except Exception as exc:                        # noqa: BLE001
        check("连上 S7 从站", False, str(exc))
        return

    table = load_points(POINTS)
    s7_tags = [t for t in table.points if t.source == "s7"]
    values = await drv.read_many(s7_tags)

    for tag in s7_tags:
        v = values.get(tag.name)
        print(f"      {tag.name:<16s} {tag.addr:<12s} = {v!s:<14s} {tag.unit}")

    banner("[2/4] S7 类型解码")
    check("REAL 解出给煤量设定 2.5",
          abs((values.get("S7给煤量设定") or 0) - 2.5) < 0.01, str(values.get("S7给煤量设定")))
    actual = values.get("S7给煤量实际")
    check("REAL 实际量在合理区间", isinstance(actual, float) and 1.0 < actual < 2.6, str(actual))
    temp = values.get("S7一次风温度")
    check("REAL 温度在合理区间", isinstance(temp, float) and 170 < temp < 205, str(temp))
    speed = values.get("S7给煤机转速")
    check("INT 转速解出", isinstance(speed, int) and 1700 < speed < 2000, str(speed))
    hours = values.get("S7累计运行小时")
    check("UINT 运行小时 = 1234", hours == 1234, str(hours))
    running = values.get("S7运行标志")
    check("BOOL 运行标志 = True", running is True, str(running))
    db2 = values.get("S7二线磨机电流")
    check("DB2 也读到了", isinstance(db2, float) and 30 < db2 < 50, str(db2))

    banner("[3/4] S7 写往返")
    sp = next(t for t in s7_tags if t.name == "S7给煤量设定")
    await drv.write(sp, 3.75)
    back = await drv.read(sp)
    check("写 3.75 再读还是 3.75", isinstance(back, float) and abs(back - 3.75) < 0.001, str(back))
    await drv.write(sp, 2.50)
    check("写回 2.50", abs((await drv.read(sp)) - 2.50) < 0.001)

    await drv.disconnect()


async def test_eip(fake: SimulatedLogix) -> None:
    banner("[4/4] AB(EtherNet/IP) 驱动")
    drv = EipDriver(path="127.0.0.1/1/0", program="MainProgram", client=fake)
    await drv.connect()
    check("已连接(注入的假客户端)", fake.opened)

    table = load_points(POINTS)
    eip_tags = [t for t in table.points if t.source == "eip"]
    values = await drv.read_many(eip_tags)

    for tag in eip_tags:
        v = values.get(tag.name)
        print(f"      {tag.name:<16s} {tag.addr:<18s} = {v!s:<14s} {tag.unit}")

    banner("[5/6] AB 标签路径映射")
    check("请求的路径补上了 Program:MainProgram 前缀",
          all(p.startswith("Program:MainProgram.") for p in fake.requested),
          str(fake.requested[:2]))
    check("float 转换正确", abs((values.get("AB给煤量设定") or 0) - 3.2) < 0.001, str(values.get("AB给煤量设定")))
    check("int 转换正确", values.get("AB给煤机转速") == 1780, str(values.get("AB给煤机转速")))
    check("bool 转换正确", values.get("AB运行标志") is True, str(values.get("AB运行标志")))

    banner("[6/6] AB 写")
    sp = next(t for t in eip_tags if t.name == "AB给煤量设定")
    await drv.write(sp, 2.9)
    check("写进假控制器了", fake.written and fake.written[-1][1] == 2.9, str(fake.written[-1:]))
    check("写完再读是 2.9", abs((await drv.read(sp)) - 2.9) < 0.001)

    await drv.disconnect()

    # 内置模拟器的"活的值": 没人推它, 值自己会变
    live = SimulatedLogix({"探针": 10.0}, vary=True)
    v1 = live.read("探针")[0].value
    await asyncio.sleep(1.2)
    v2 = live.read("探针")[0].value
    check("内置模拟器的值会随时间变化", v1 != v2, f"{v1} -> {v2}")


async def test_four(table, fake: SimulatedLogix) -> None:
    banner("[7/7] 四协议同表采集 —— 一个采集器四个协议")
    print(f"      点表: {len(table.points)} 个点位, 连接源: {list(table.sources.keys())}")
    for src, tags in table.by_source().items():
        print(f"        {src}: {len(tags)} 个")

    def eip_factory(cfg):
        return EipDriver(
            path=cfg["path"],
            program=str(cfg.get("program", "MainProgram")),
            client=fake,
        )

    collector = Collector(table, driver_overrides={"eip": eip_factory})
    await collector.start()

    check(
        "四个连接源都连上了",
        collector.connected and len(collector.connected_sources) == 4,
        str(sorted(collector.connected_sources)),
    )
    if collector.errors:
        for e in collector.errors:
            print(f"        [错误] {e}")

    await asyncio.sleep(4.0)

    snap = collector.snapshot()
    print(f"      采集到 {len(snap)}/{len(table.points)} 个点位")
    for tag in table.points:
        lv = snap.get(tag.name)
        print(f"        {tag.source:<7s} {tag.name:<16s} = {(lv.physical if lv else None)!s:<14s} {tag.unit}")

    by_src = table.by_source()
    for src, tags in by_src.items():
        got = [t for t in tags if snap.get(t.name) and snap[t.name].physical is not None]
        check(f"{src} 侧全部有值", len(got) == len(tags), f"{len(got)}/{len(tags)}")

    check("四种协议的数据在同一个缓存里", len(snap) == len(table.points), f"缓存 {len(snap)} 个")

    # 轮询生效: Modbus / S7 / AB 三个无订阅的源, 数据时间戳要持续刷新
    poll_tags = [t for t in table.points if t.source in ("modbus", "s7", "eip")]
    ages1 = {t.name: collector.values[t.name].age_ms for t in poll_tags if t.name in collector.values}
    await asyncio.sleep(2.5)
    ages2 = {t.name: collector.values[t.name].age_ms for t in poll_tags if t.name in collector.values}
    refreshed = [n for n in ages1 if ages2.get(n, 10**9) < ages1.get(n, 0)]
    check("轮询型协议数据在持续刷新", len(refreshed) >= len(ages1) * 0.8, f"{len(refreshed)}/{len(ages1)}")

    await collector.stop()


# =====================================================================


async def main() -> int:
    banner("启动 mock server (OPC UA + Modbus + S7)")
    procs: list[subprocess.Popen] = []

    p_ua = start_server("mock_opcua_server.py", 4840)
    if p_ua:
        procs.append(p_ua)
        print(f"[OK]   mock opcua pid={p_ua.pid}")

    p_mb = start_server("mock_modbus_server.py", 5020, "--port", "5020")
    if p_mb:
        procs.append(p_mb)
        print(f"[OK]   mock modbus pid={p_mb.pid}")

    p_s7 = start_server("mock_s7_server.py", 10200, "--port", "10200")
    if p_s7:
        procs.append(p_s7)
        print(f"[OK]   mock s7 pid={p_s7.pid}")

    try:
        check("mock OPC UA 端口 4840 就绪", wait_port("127.0.0.1", 4840, 25.0))
        check("mock Modbus 端口 5020 就绪", wait_port("127.0.0.1", 5020, 25.0))
        check("mock S7 端口 10200 就绪", wait_port("127.0.0.1", 10200, 25.0))

        table = load_points(POINTS)
        fake = make_fake()

        await test_s7()
        await test_eip(fake)
        await test_four(table, fake)
    finally:
        for p in procs:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    banner("结果")
    if _failures:
        print(f"[FAIL] {len(_failures)} 项不通过:")
        for f in _failures:
            print(f"       - {f}")
        return 1
    print("[OK]   S7 + AB 驱动 + 四协议同表采集全部验证通过")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
