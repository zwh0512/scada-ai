"""
管线自测 —— 不开界面, 验证驱动层 + 报警引擎
=============================================
跑法:
    .\\run.ps1 test_pipeline.py

mock OPC UA server **脚本自己会起**, 不用另开终端。
(如果 4840 端口已经有 server 在跑, 就直接用现成的, 不会重复起)

验证 5 件事:
    1. 点表能加载, source 能路由到驱动
    2. 能连上 mock server
    3. 能读到值, 且工程量换算正确
    4. 订阅能推送到回调
    5. 报警引擎能触发 + 能恢复(死区正确)
"""

from __future__ import annotations

import asyncio
import logging
import socket
import subprocess
import sys
import time
from pathlib import Path

from drivers import AlarmEngine, build_drivers, check_coverage, load_points
from drivers.alarm import HI, LO, NORMAL

# asyncua 断开时会打一条 BadNoSubscription 的 ServiceFault, 是正常收尾噪音
logging.getLogger("asyncua").setLevel(logging.WARNING)

HERE = Path(__file__).resolve().parent
POINTS = HERE / "points.yaml"

PASS = "[PASS]"
FAIL = "[FAIL]"


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _wait_port_closed(host: str, port: int, timeout: float = 8.0) -> bool:
    """等端口真正释放。

    上一轮测试的 mock server 可能还在退出中 —— 这时候端口照样能连上,
    新起的 server 绑不上去, 测试就会连到那个"正在死"的 server, 偶发失败。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                time.sleep(0.3)
        except OSError:
            return True
    return False


def _ensure_mock() -> subprocess.Popen | None:
    """端口没开就自己起一个 mock server。已经有人在跑就直接用。"""
    if _port_open("127.0.0.1", 4840):
        # 端口上有东西 —— 可能是上一轮残留、正在退出的 server,
        # 先等它真正释放, 免得新 server 绑不上
        _wait_port_closed("127.0.0.1", 4840, timeout=8.0)
        if _port_open("127.0.0.1", 4840):
            print("      (4840 已有 server 在跑, 直接用)")
            return None

    proc = subprocess.Popen(
        [sys.executable, str(HERE / "mock_opcua_server.py")],
        cwd=str(HERE),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(80):
        if _port_open("127.0.0.1", 4840):
            print(f"      (已自动启动 mock server pid={proc.pid})")
            return proc
        time.sleep(0.25)
    print("      (mock server 启动超时)")
    proc.terminate()
    return None


async def run() -> int:
    mock = _ensure_mock()
    try:
        return await _run_inner()
    finally:
        if mock is not None:
            mock.terminate()
            try:
                mock.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mock.kill()


async def _run_inner() -> int:
    failures = 0

    # ---------- 1. 点表 ----------
    print("=" * 58)
    print("  管线自测")
    print("=" * 58)
    print()
    print("[1/5] 加载点表 ...")
    table = load_points(POINTS)
    groups = table.by_source()
    print(f"      连接源: {list(table.sources.keys())}")
    print(f"      点位数: {len(table.points)}  分组: { {k: len(v) for k, v in groups.items()} }")
    if len(table.points) != 8:
        print(f"      {FAIL} 点位数不对, 期望 8")
        failures += 1
    else:
        print(f"      {PASS}")

    # ---------- 2. 工厂 ----------
    print()
    print("[2/5] 建驱动 + 覆盖检查 ...")
    drivers = build_drivers(table)
    missing = check_coverage(table, drivers)
    print(f"      驱动: { {k: type(v).__name__ for k, v in drivers.items()} }")
    if missing:
        print(f"      {FAIL} 缺驱动: {missing}")
        failures += 1
    else:
        print(f"      {PASS}")

    # ---------- 3. 连接 + 读 ----------
    print()
    print("[3/5] 连接 + 读值 ...")
    drv = drivers["opcua"]
    try:
        await drv.connect()
        print(f"      {PASS} 已连接 {drv.url}")
    except Exception as exc:                          # noqa: BLE001
        print(f"      {FAIL} 连接失败: {exc}")
        print("      (mock_opcua_server.py 起了吗?)")
        return 1

    values = await drv.read_many(table.points)
    for tag in table.points:
        raw = values.get(tag.name)
        phys = tag.to_physical(raw) if raw is not None else None
        print(f"      {tag.name:<16s} raw={raw!s:<10s} 工程量={phys!s:<12s} {tag.unit}")
    if values.get("磨机电流") is None:
        print(f"      {FAIL} 没读到值")
        failures += 1
    else:
        print(f"      {PASS}")

    # ---------- 4. 订阅 ----------
    print()
    print("[4/5] 订阅推送 (观察 3 秒) ...")
    received: list[tuple[str, object]] = []

    def on_change(tag, raw):
        received.append((tag.name, raw))

    sub_tags = [t for t in table.points if t.type == "float"][:3]
    await drv.subscribe(sub_tags, on_change)
    await asyncio.sleep(3)
    print(f"      收到 {len(received)} 条推送")
    for name, raw in received[:5]:
        print(f"        {name} = {raw}")
    if len(received) == 0:
        print(f"      {FAIL} 订阅没收到任何推送")
        failures += 1
    else:
        print(f"      {PASS}")

    # ---------- 5. 报警引擎 ----------
    print()
    print("[5/5] 报警引擎 (越限 + 死区恢复) ...")
    events = []
    engine = AlarmEngine(on_event=events.append)
    engine.register(table.points)

    tag = table.get("磨机电流")
    print(f"      点位: {tag.name}  上限={tag.alarm_hi}  下限={tag.alarm_lo}  死区={tag.deadband}")

    # 正常值 -> 不报警
    engine.check(tag, 35.0)
    if engine.current_state(tag.name) != NORMAL:
        print(f"      {FAIL} 正常值却报警了")
        failures += 1
    else:
        print(f"      {PASS} 35.0A 正常, 不报警")

    # 越上限 -> 报警
    engine.check(tag, 55.0)
    if engine.current_state(tag.name) != HI:
        print(f"      {FAIL} 越上限没报警")
        failures += 1
    else:
        print(f"      {PASS} 55.0A 触发越上限, 等级={tag.alarm_level}")

    # 落在阈值和死区之间 -> 不该恢复(防抖)
    engine.check(tag, tag.alarm_hi - tag.deadband * 0.5)
    if engine.current_state(tag.name) != HI:
        print(f"      {FAIL} 死区内就恢复了, 死区没起作用")
        failures += 1
    else:
        print(f"      {PASS} 死区内保持报警(不抖)")

    # 回到阈值-死区以下 -> 恢复
    engine.check(tag, tag.alarm_hi - tag.deadband - 1)
    if engine.current_state(tag.name) != NORMAL:
        print(f"      {FAIL} 该恢复没恢复")
        failures += 1
    else:
        print(f"      {PASS} 越过死区, 报警恢复")

    print(f"      SOE 记录 {len(engine.soe)} 条:")
    for e in engine.soe:
        print(f"        {e.time_text} {e.tag_name} {e.state} = {e.value}")

    await drv.disconnect()

    print()
    print("=" * 58)
    if failures:
        print(f"  {failures} 项未通过")
    else:
        print("  全部通过")
    print("=" * 58)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
