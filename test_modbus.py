"""
Modbus 驱动 + 混合协议采集 端到端自测
====================================
这是"多协议抽象不是口号"的证明脚本。验证 7 件事:

    1. 能连上真正的 Modbus TCP 从站
    2. 能读 holding / input register, 工程量换算正确
    3. float32 字节序 4 种顺序的对比结果 —— 配错就是垃圾数字
    4. coil 能读能写
    5. 写 holding register 能往返 (写完再读还是那个值)
    6. **一张点表同时挂 OPC UA 和 Modbus, 采集器自动路由, 上层零改动**
    7. Modbus 没有订阅 -> 采集器自动降级成轮询

跑法:
    .\\run.ps1 test_modbus.py

退出码 0 = 全过。
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path

from mockctl import start_server, stop_all, wait_port
from drivers import Collector, load_points
from drivers.modbus_driver import ModbusDriver

# asyncua 断开时的 BadNoSubscription 是正常收尾噪音
logging.getLogger("asyncua").setLevel(logging.WARNING)

HERE = Path(__file__).resolve().parent
PY = sys.executable
POINTS = HERE / "points_mixed.yaml"

_failures: list[str] = []
_step = 0


def banner(text: str) -> None:
    print()
    print("=" * 62)
    print(text)
    print("=" * 62)


def check(label: str, ok: bool, detail: str = "") -> bool:
    global _step
    _step += 1
    print(f"{'[OK]  ' if ok else '[FAIL]'} {label}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        _failures.append(label)
    return ok


# =====================================================================
# 1-5: 直接打驱动
# =====================================================================


async def test_driver() -> None:
    banner("[1/7] 驱动连接")
    drv = ModbusDriver(host="127.0.0.1", port=5020, unit_id=1, word_order="abcd")
    try:
        await drv.connect()
        check("连上 Modbus TCP 从站", True, f"127.0.0.1:5020 unit_id=1")
    except Exception as exc:                        # noqa: BLE001
        check("连上 Modbus TCP 从站", False, str(exc))
        return

    banner("[2/7] 读 holding / input register")
    table = load_points(POINTS)
    mb_tags = [t for t in table.points if t.source == "modbus"]
    values = await drv.read_many(mb_tags)

    for tag in mb_tags:
        v = values.get(tag.name)
        print(f"      {tag.name:<14s} {tag.addr:<10s} = {v!s:<14s} {tag.unit}")

    setpoint = values.get("给煤机设定量")
    check("holding float32 读到 2.5", isinstance(setpoint, float) and abs(setpoint - 2.5) < 0.01, str(setpoint))
    actual = values.get("给煤机实际量")
    check("holding float32 实际量在合理区间", isinstance(actual, float) and 1.0 < actual < 2.6, str(actual))
    press = values.get("一次风压力")
    check("input register float32 读到", isinstance(press, float) and 0.03 < press < 0.06, str(press))
    speed = values.get("给煤机转速")
    check("int16 转速读到", isinstance(speed, int) and 1700 < speed < 2000, str(speed))
    hours = values.get("运行小时数")
    check("uint16 小时数 = 1234", hours == 1234, str(hours))
    status = values.get("设备状态码")
    check("input register 状态码 = 0", status == 0, str(status))

    banner("[3/7] float32 字节序 —— 配错就是垃圾数字")
    print("      同一批寄存器, 用 4 种字节序各读一遍:")
    for order in ("abcd", "badc", "cdab", "dcba"):
        d = ModbusDriver(host="127.0.0.1", port=5020, unit_id=1, word_order=order)
        await d.connect()
        tag = next(t for t in mb_tags if t.name == "给煤机设定量")
        v = await d.read(tag)
        mark = "  <- 正确" if order == "abcd" else ""
        print(f"        {order}  读出 {v!r}{mark}")
        if order == "abcd":
            check("abcd 读出正确值 2.5", isinstance(v, float) and abs(v - 2.5) < 0.01, str(v))
        else:
            check(f"{order} 读出乱码(证明字节序必须配对)", not (isinstance(v, float) and abs(v - 2.5) < 0.01), str(v))
        await d.disconnect()

    banner("[4/7] coil 读写")
    coil_tag = next(t for t in mb_tags if t.name == "启动信号")
    before = await drv.read(coil_tag)
    check("coil 初值为 False", before is False, str(before))
    await drv.write(coil_tag, True)
    after = await drv.read(coil_tag)
    check("写 coil=True 后读到 True", after is True, str(after))
    await drv.write(coil_tag, False)
    check("写回 False", (await drv.read(coil_tag)) is False)

    banner("[5/7] 写 holding register 往返")
    sp = next(t for t in mb_tags if t.name == "给煤机设定量")
    await drv.write(sp, 3.25)
    back = await drv.read(sp)
    check("写 3.25 再读还是 3.25", isinstance(back, float) and abs(back - 3.25) < 0.001, str(back))
    await drv.write(sp, 2.50)
    check("写回 2.50", abs((await drv.read(sp)) - 2.50) < 0.001)

    await drv.disconnect()


# =====================================================================
# 6-7: 走采集器(混合协议)
# =====================================================================


async def test_collector() -> None:
    banner("[6/7] 混合协议采集 —— 一张点表挂两种协议")
    table = load_points(POINTS)
    print(f"      点表: {len(table.points)} 个点位, 连接源: {list(table.sources.keys())}")
    by_src = table.by_source()
    for src, tags in by_src.items():
        print(f"        {src}: {len(tags)} 个")

    collector = Collector(table)
    await collector.start()

    check("两个连接源都连上了", collector.connected and len(collector.connected_sources) == 2, str(sorted(collector.connected_sources)))
    if collector.errors:
        for e in collector.errors:
            print(f"        [错误] {e}")

    # 等两个协议都推/轮询出值
    await asyncio.sleep(4.0)

    snap = collector.snapshot()
    print(f"      采集到 {len(snap)}/{len(table.points)} 个点位")
    for tag in table.points:
        lv = snap.get(tag.name)
        v = lv.physical if lv else None
        print(f"        {tag.source:<7s} {tag.name:<14s} = {v!s:<14s} {tag.unit}")

    opc_vals = [t for t in table.points if t.source == "opcua"]
    mb_vals = [t for t in table.points if t.source == "modbus"]
    got_opc = [t for t in opc_vals if snap.get(t.name) and snap[t.name].physical is not None]
    got_mb = [t for t in mb_vals if snap.get(t.name) and snap[t.name].physical is not None]

    check("OPC UA 侧全部有值", len(got_opc) == len(opc_vals), f"{len(got_opc)}/{len(opc_vals)}")
    check("Modbus 侧全部有值", len(got_mb) == len(mb_vals), f"{len(got_mb)}/{len(mb_vals)}")
    check("两种协议的数据在同一个缓存里", len(snap) >= len(opc_vals) + len(mb_vals), f"缓存 {len(snap)} 个")

    banner("[7/7] Modbus 无订阅 -> 自动降级轮询")
    mdrv = collector.drivers.get("modbus")
    check("Modbus 驱动声明不支持订阅", mdrv is not None and mdrv.supports_subscription() is False)
    odrv = collector.drivers.get("opcua")
    check("OPC UA 驱动声明支持订阅", odrv is not None and odrv.supports_subscription() is True)

    # 轮询的证据: Modbus 侧的值时间戳在持续刷新
    ages1 = {t.name: collector.values[t.name].age_ms for t in mb_vals if t.name in collector.values}
    await asyncio.sleep(2.5)
    ages2 = {t.name: collector.values[t.name].age_ms for t in mb_vals if t.name in collector.values}
    refreshed = [n for n in ages1 if ages2.get(n, 10**9) < ages1.get(n, 0)]
    check("Modbus 侧数据在持续刷新(轮询生效)", len(refreshed) >= 3, f"{len(refreshed)}/{len(ages1)} 个点位时间戳更新了")

    await collector.stop()


# =====================================================================


async def main() -> int:
    banner("启动 mock server (OPC UA + Modbus)")
    procs: list[subprocess.Popen] = []

    mock_ua = start_server("mock_opcua_server.py", 4840)
    if mock_ua:
        procs.append(mock_ua)
        print(f"[OK]   mock opcua pid={mock_ua.pid}")

    mock_mb = start_server("mock_modbus_server.py", 5020, "--port", "5020")
    if mock_mb:
        procs.append(mock_mb)
        print(f"[OK]   mock modbus pid={mock_mb.pid}")

    try:
        check("mock OPC UA 端口 4840 就绪", wait_port("127.0.0.1", 4840, 25.0))
        check("mock Modbus 端口 5020 就绪", wait_port("127.0.0.1", 5020, 25.0))

        await test_driver()
        await test_collector()
    finally:
        stop_all(procs)

    banner("结果")
    if _failures:
        print(f"[FAIL] {len(_failures)} 项不通过:")
        for f in _failures:
            print(f"       - {f}")
        return 1
    print("[OK]   Modbus 驱动 + 混合协议采集全部验证通过")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
