"""
SOE / 报警链路端到端自测
=========================
为什么这个要单独测:
    诊断时大模型会调 get_alarms(active) 和 get_alarms(history)。
    这两个工具要是返回**空数组**, 模型就拿不到任何历史依据,
    根因分析直接变成瞎猜。所以必须验证"里面真的有数据", 而不是"接口能调通"。

怎么制造报警:
    **不注入假值** —— mock OPC UA 的数据是仿真出来的, 它自己就会越限:
        Line2 一次风温度 = 195 + sin(tick/30)*12  ->  峰值 207 > 上限 205
        Line1 磨机电流   = 40 + sin(tick/8)*2.5 + 堵料尖峰12 ->  ~54 > 上限 48
    所以这里只是等它自然发生, 走的是和现场一模一样的路径:
    订阅推值 -> 报警引擎判定 -> 死区判定 -> 写 SOE -> MCP 工具返回。

跑法:
    .\\run.ps1 test_alarm_soe.py

耗时: 约 60~110 秒(取决于等多久仿真数据越限)。

退出码 0 = 全过, 非 0 = 有问题(看输出里的 [FAIL])。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from mockctl import start_server, stop_all, wait_port

HERE = Path(__file__).resolve().parent
PY = sys.executable

_failures: list[str] = []


def banner(text: str) -> None:
    print()
    print("=" * 62)
    print(text)
    print("=" * 62)


def check(label: str, ok: bool, detail: str = "") -> bool:
    tag = "[OK]  " if ok else "[FAIL]"
    line = f"{tag} {label}"
    if detail:
        line += f"  -> {detail}"
    print(line)
    if not ok:
        _failures.append(label)
    return ok


def payload(result: Any) -> Any:
    """从 MCP 调用结果里取出结构化数据。"""
    structured = getattr(result, "structured_content", None)
    if structured:
        return structured
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return text
    return None


# =====================================================================


async def wait_for_active(client: Client, timeout: float) -> dict[str, Any] | None:
    """轮询 get_alarms(active), 等第一个活动报警出现。"""
    deadline = time.time() + timeout
    tick = 0
    while time.time() < deadline:
        data = payload(await client.call_tool("get_alarms", {"scope": "active"}))
        if isinstance(data, dict) and data.get("count", 0) > 0:
            alarms = data.get("alarms") or []
            return alarms[0] if alarms else None
        tick += 1
        if tick % 10 == 0:
            print(f"       ... 等待越限, 已过 {tick} 秒")
        await asyncio.sleep(1.0)
    return None


async def wait_for_cleared(client: Client, tag_name: str, timeout: float) -> bool:
    """等指定点位的活动报警消失(恢复)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = payload(await client.call_tool("get_alarms", {"scope": "active"}))
        if isinstance(data, dict):
            names = [a.get("tag_name") for a in (data.get("alarms") or [])]
            if tag_name not in names:
                return True
        await asyncio.sleep(1.0)
    return False


# =====================================================================


async def run_checks(client: Client) -> None:
    # ---------------- 1. 等报警 ----------------
    banner("[1/3] 等一次真实越限(mock 数据自然波动, 不注入假值)")
    alarm = await wait_for_active(client, timeout=90.0)
    if not check("活动报警出现了", alarm is not None):
        print("       说明: 90 秒内仿真数据没越限。")
        print("             正常情况 Line2 一次风温度约 47 秒就该越 205 上限。")
        return

    tag_name = alarm.get("tag_name", "")
    print(f"       触发: {tag_name} {alarm.get('state')} "
          f"{alarm.get('value')} {alarm.get('unit')} "
          f"(等级 {alarm.get('level')}, 阈值 {alarm.get('threshold')})")

    # ---------------- 2. 活动报警内容 ----------------
    banner("[2/3] 活动报警记录字段")
    check("有 tag_name", bool(tag_name), str(tag_name))
    check("有状态(状态机字段)", bool(alarm.get("state")), str(alarm.get("state")))
    check("有触发值", alarm.get("value") is not None, str(alarm.get("value")))
    check("有单位", bool(alarm.get("unit")), str(alarm.get("unit")))
    check("有报警等级", alarm.get("level") is not None, str(alarm.get("level")))
    check("有触发时间", bool(alarm.get("time")), str(alarm.get("time")))

    # ---------------- 3. 等恢复, 验 SOE 历史 ----------------
    banner("[3/3] 等死区恢复, 验 SOE 历史")
    cleared = await wait_for_cleared(client, tag_name, timeout=45.0)
    check("报警已恢复(死区判定生效)", cleared)

    data = payload(await client.call_tool("get_alarms", {"scope": "history", "limit": 50}))
    check("history 接口返回字典", isinstance(data, dict))
    total = data.get("total_soe", 0) if isinstance(data, dict) else 0
    check("SOE 总条数 > 0", total > 0, f"total_soe={total}")

    events = (data.get("alarms") or []) if isinstance(data, dict) else []
    check("返回了 SOE 明细", len(events) > 0, f"{len(events)} 条")

    if events:
        first = events[0]
        print("       最近 5 条 SOE:")
        for e in events[:5]:
            print(f"         {e.get('time')}  {e.get('tag_name'):<16} "
                  f"{e.get('state'):<8} {e.get('value')}")

        mine = [e for e in events if e.get("tag_name") == tag_name]
        check("SOE 里有刚才那个点位的记录", len(mine) > 0, f"{len(mine)} 条")
        states = {e.get("state") for e in mine}
        check("有触发态(HI/LO)", bool(states & {"HI", "LO"}), str(states))
        check("有恢复态(NORMAL)", "NORMAL" in states, str(states))
        check("SOE 带时间戳", all(e.get("time") for e in mine))

    # 单点诊断里也带该点的报警史
    data = payload(await client.call_tool("diagnose_tag", {"tag_name": tag_name}))
    soe = data.get("soe") if isinstance(data, dict) else None
    check("diagnose_tag 里带该点 SOE", isinstance(soe, list) and len(soe) > 0,
          f"{len(soe) if isinstance(soe, list) else soe} 条")


# =====================================================================


async def main() -> int:
    parser = argparse.ArgumentParser(description="SOE / 报警链路自测")
    parser.add_argument("--points", default="points.yaml", help="点表路径")
    args = parser.parse_args()

    points_path = Path(args.points)
    if not points_path.is_absolute():
        points_path = HERE / points_path

    banner("启动 mock OPC UA server")
    servers: list[subprocess.Popen] = []

    mock = start_server("mock_opcua_server.py", 4840)
    if mock:
        servers.append(mock)
        print(f"[OK]   mock opcua pid={mock.pid}")

    try:
        if not wait_port("127.0.0.1", 4840, timeout=25.0):
            print("[FAIL] mock OPC UA 端口没起来")
            return 1
        print("[OK]   端口 4840 已就绪")

        params = StdioServerParameters(
            command=PY,
            args=[str(HERE / "mcp_server.py"), "--points", str(points_path)],
            cwd=str(HERE),
            env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )

        banner("连接 MCP server")
        async with Client(params) as client:
            print("[OK]   已连接")
            print("[INFO] 等订阅推第一批值 ...")
            await asyncio.sleep(3.0)
            await run_checks(client)
    finally:
        stop_all(servers)

    banner("结果")
    if _failures:
        print(f"[FAIL] {len(_failures)} 项不通过:")
        for f in _failures:
            print(f"       - {f}")
        return 1

    print("[OK]   报警触发 / 死区恢复 / SOE 历史 全链路验证通过")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
