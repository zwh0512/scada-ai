"""
MCP Server 端到端自测
=====================
起一个 mock OPC UA server, 再用 MCP 客户端把本项目的 MCP server 拉起来,
把每个工具都调一遍, 看返回对不对。

这个脚本的意义: **没有图形界面也能证明"MCP 这条路走通了"**。
跑通了就说明: 现场数据 -> MCP 工具 -> AI 客户端, 整条链路是通的。

跑法:
    .\\run.ps1 test_mcp.py

退出码 0 = 全过, 非 0 = 有问题(看输出里的 [FAIL])。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from drivers import load_points

HERE = Path(__file__).resolve().parent
PY = sys.executable

EXPECTED_TOOLS = [
    "list_tags",
    "search_tags",
    "read_values",
    "get_alarms",
    "diagnose_tag",
    "write_value",
]

_failures: list[str] = []
_step = 0

# 运行时填: 期望的点位数 / 连接源
EXP_POINTS = 8
EXP_SOURCES = ["opcua"]


def banner(text: str) -> None:
    print()
    print("=" * 60)
    print(text)
    print("=" * 60)


def check(label: str, ok: bool, detail: str = "") -> bool:
    global _step
    _step += 1
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


def wait_port(host: str, port: int, timeout: float = 20.0) -> bool:
    """等 mock server 的端口起来。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def wait_port_closed(host: str, port: int, timeout: float = 8.0) -> bool:
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


def start_server(script: str, port: int, *args: str) -> subprocess.Popen | None:
    """起 mock server。端口上已经有在跑的就直接复用, 不重复起。"""
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


async def run_checks(client: Client) -> None:
    # ---------------- 1. 工具清单 ----------------
    banner("[1/6] 列出 MCP 工具")
    listing = await client.list_tools()
    names = [t.name for t in listing.tools]
    for name in EXPECTED_TOOLS:
        check(f"工具 {name} 已注册", name in names, f"实际: {names}")

    # 工具描述是给大模型看的, 描述为空等于工具对 AI 不可用
    for t in listing.tools:
        desc = (t.description or "").strip()
        check(f"工具 {t.name} 有描述", len(desc) > 10, f"{len(desc)} 字")
        # 返回注解必须带泛型 (-> dict[str, Any]), 否则框架不生成 output_schema,
        # 客户端只能去解析文本, 而不是直接拿嵌套字典
        check(f"工具 {t.name} 有 output_schema", t.output_schema is not None)

    # ---------------- 2. list_tags ----------------
    banner("[2/6] list_tags —— 点表配置")
    data = payload(await client.call_tool("list_tags", {}))
    check(
        "返回了点位",
        isinstance(data, dict) and data.get("count") == EXP_POINTS,
        f"count={data.get('count') if isinstance(data, dict) else data} (期望 {EXP_POINTS})",
    )
    pts = data.get("points", []) if isinstance(data, dict) else []
    check("包含'磨机电流'", any(p["name"] == "磨机电流" for p in pts))
    mill = next((p for p in pts if p["name"] == "磨机电流"), None)
    check("磨机电流带报警阈值", bool(mill) and mill.get("alarm_hi") == 48.0, str(mill))
    check("磨机电流单位是 A", bool(mill) and mill.get("unit") == "A")
    if len(EXP_SOURCES) > 1:
        check(
            "点表同时挂了多种协议",
            sorted(data.get("sources", [])) == sorted(EXP_SOURCES),
            str(data.get("sources")),
        )

    # ---------------- 3. search_tags ----------------
    banner("[3/6] search_tags —— 关键字检索")
    data = payload(await client.call_tool("search_tags", {"keyword": "磨机"}))
    check("搜'磨机'有命中", isinstance(data, dict) and data.get("count", 0) >= 2, f"count={data.get('count') if isinstance(data, dict) else data}")
    data = payload(await client.call_tool("search_tags", {"keyword": "一次风"}))
    check("搜'一次风'有命中", isinstance(data, dict) and data.get("count", 0) >= 2)

    # ---------------- 4. read_values ----------------
    banner("[4/6] read_values —— 实时值")
    data = payload(await client.call_tool("read_values", {}))
    vals = data.get("values", []) if isinstance(data, dict) else []
    check("读到全部点位", len(vals) == EXP_POINTS, f"count={len(vals)} (期望 {EXP_POINTS})")
    got = [v for v in vals if v.get("value") is not None]
    check("有真实数值", len(got) >= EXP_POINTS - 2, f"{len(got)}/{EXP_POINTS} 有值")
    check("连接状态为已连接", bool(data.get("connected")))
    check(
        "每个连接源都正常",
        all(v == "OK" for v in (data.get("sources") or {}).values()),
        str(data.get("sources")),
    )

    mill_v = next((v for v in vals if v["name"] == "磨机电流"), None)
    check("磨机电流有值", bool(mill_v) and isinstance(mill_v.get("value"), (int, float)), str(mill_v))
    check("数据年龄正常(<5秒)", bool(mill_v) and 0 <= mill_v.get("age_ms", -1) < 5000, f"age_ms={mill_v.get('age_ms') if mill_v else None}")

    # 点名读
    data = payload(await client.call_tool("read_values", {"tag_names": ["磨机电流"]}))
    check("点名读只回 1 条", isinstance(data, dict) and data.get("count") == 1, f"count={data.get('count') if isinstance(data, dict) else data}")

    # structured_content: 客户端应该能直接拿到嵌套字典, 不用自己解析文本
    raw = await client.call_tool("read_values", {})
    check(
        "返回带 structured_content",
        isinstance(getattr(raw, "structured_content", None), dict),
        type(getattr(raw, "structured_content", None)).__name__,
    )

    # ---------------- 5. get_alarms ----------------
    banner("[5/6] get_alarms —— 报警与 SOE")
    data = payload(await client.call_tool("get_alarms", {"scope": "active"}))
    check("活动报警接口通", isinstance(data, dict) and "alarms" in data)
    data = payload(await client.call_tool("get_alarms", {"scope": "history"}))
    check("SOE 历史接口通", isinstance(data, dict) and "total_soe" in data, f"total_soe={data.get('total_soe') if isinstance(data, dict) else data}")

    # ---------------- 6. diagnose_tag ----------------
    banner("[6/6] diagnose_tag —— 单点诊断 + 写闸门")
    data = payload(await client.call_tool("diagnose_tag", {"tag_name": "磨机电流"}))
    check("找到点位", isinstance(data, dict) and data.get("found") is True)
    check("有当前值", bool(data.get("current", {}).get("value") is not None))
    check("有阈值余量", "to_hi" in data.get("margin", {}))
    check("有趋势判断", bool(data.get("trend", {}).get("direction")), str(data.get("trend", {}).get("direction")))
    check("有同源其它点快照", len(data.get("related", [])) >= 3, f"{len(data.get('related', []))} 个")
    check("有一句话评估", bool(data.get("assessment")), str(data.get("assessment"))[:60])

    # 防幻觉: 不存在的点必须说找不到, 不能瞎编
    data = payload(await client.call_tool("diagnose_tag", {"tag_name": "根本不存在的点位"}))
    check("不存在的点返回 found=False", isinstance(data, dict) and data.get("found") is False)

    # 写闸门: 默认点位不可写, 必须被拒
    data = payload(await client.call_tool("write_value", {"tag_name": "磨机电流", "value": 1.0}))
    check("不可写点位被拒绝", isinstance(data, dict) and data.get("ok") is False, str(data.get("error"))[:60] if isinstance(data, dict) else str(data))


# =====================================================================


async def main() -> int:
    global EXP_POINTS, EXP_SOURCES

    parser = argparse.ArgumentParser(description="MCP 端到端自测")
    parser.add_argument("--points", default="points.yaml", help="点表路径")
    parser.add_argument(
        "--with-modbus",
        action="store_true",
        help="同时起 mock Modbus server(配合 points_mixed.yaml 用)",
    )
    args = parser.parse_args()

    points_path = Path(args.points)
    if not points_path.is_absolute():
        points_path = HERE / points_path
    table = load_points(points_path)
    EXP_POINTS = len(table.points)
    EXP_SOURCES = list(table.sources.keys())

    banner(f"启动 mock server (点表 {points_path.name}, {EXP_POINTS} 个点位, 源 {EXP_SOURCES})")
    servers: list[subprocess.Popen] = []

    mock = start_server("mock_opcua_server.py", 4840)
    if mock:
        servers.append(mock)
        print(f"[OK]   mock opcua pid={mock.pid}")

    if args.with_modbus:
        mock_mb = start_server("mock_modbus_server.py", 5020, "--port", "5020")
        if mock_mb:
            servers.append(mock_mb)
            print(f"[OK]   mock modbus pid={mock_mb.pid}")

    try:
        if not wait_port("127.0.0.1", 4840, timeout=25.0):
            print("[FAIL] mock OPC UA 端口没起来")
            return 1
        print("[OK]   端口 4840 已就绪")
        if args.with_modbus:
            if not wait_port("127.0.0.1", 5020, timeout=25.0):
                print("[FAIL] mock Modbus 端口没起来")
                return 1
            print("[OK]   端口 5020 已就绪")

        params = StdioServerParameters(
            command=PY,
            args=[str(HERE / "mcp_server.py"), "--points", str(points_path)],
            cwd=str(HERE),
            env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )

        banner("连接 MCP server")
        async with Client(params) as client:
            info = client.server_info
            print(f"[OK]   已连接, server={info.name if info else '?'}")

            # 等订阅把第一批值推上来(订阅间隔 500ms, mock 1 秒刷一次)
            print("[INFO] 等待现场数据推送 ...")
            await asyncio.sleep(4.0)

            await run_checks(client)
    finally:
        for p in servers:
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

    print(f"[OK]   全部 {len(EXPECTED_TOOLS)} 个工具 + 端到端链路验证通过")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
