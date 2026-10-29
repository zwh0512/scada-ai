"""
报警诊断 —— 让大模型用 MCP 工具分析现场报警
============================================
这是整个项目的最后一环:
    现场数据 -> 规则化判断 -> 大模型根因推理 -> 报警诊断报告

流程:
    1. 用 MCP 客户端连上 mcp_server.py (stdio 传输)
    2. list_tools() 拿到 6 个工具, 转成大模型的 function calling 格式
    3. 把问题交给 qwen-plus
    4. 大模型要调工具 -> 执行 -> 结果回传 -> 再问 (最多 N 轮)
    5. 大模型给出最终诊断报告

【关键设计】
大模型拿到的是**事实**(阈值余量、趋势、报警史、同源快照),
根因推理交给它。分工明确, 比让大模型自己算阈值靠谱得多。

【离线模式】
`--offline` 用规则模拟大模型的工具决策, 没有 key 也能把整条链路跑通,
用来验证"MCP 工具调用循环"本身是通的。

跑法:
    .\\run.ps1 llm_diagnose.py
    .\\run.ps1 llm_diagnose.py --question "给煤机实际量是不是偏低？" --model qwen-max
    .\\run.ps1 llm_diagnose.py --offline
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from drivers import load_points
from llm_client import BailianClient, LLMError, to_assistant_tool_calls
from mockctl import start_server, stop_all

HERE = Path(__file__).resolve().parent
PY = sys.executable

DEFAULT_QUESTION = "磨机电流现在什么情况？给煤量稳不稳？给一份现场诊断报告。"

SYSTEM_PROMPT = """你是钢铁厂喷煤线的现场运维专家助手。

现场数据通过 MCP 工具提供, 你**必须先调工具拿事实**, 不要凭记忆编数值。

工作方式:
1. search_tags / list_tags 找到相关点位
2. diagnose_tag 拿完整诊断(当前值、阈值余量、变化趋势、报警历史、同源快照)
3. get_alarms 看有没有相关报警记录
4. 在这些**事实**基础上做根因推理, 给出可执行的处置建议

必须遵守:
- diagnose_tag 返回的 assessment 是上位机用规则算好的判断, 那是**事实的一部分**, 不是你的结论
- trend.direction 很重要: "持续上升"和"刚跳上去"对应的根因完全不一样
- 数值单位看 unit 字段, 不要自己换算
- 工具没返回的数据不要编, 不确定就明说不确定
- 用中文回答, 结构清晰: 现状 / 判断依据 / 根因分析 / 处置建议
"""


# =====================================================================
# 离线模式: 规则模拟大模型
# =====================================================================


class OfflineLLM:
    """离线模式: 用规则模拟大模型的工具决策。

    目的: 没有 key 或没网络时, 也能把"MCP 工具调用循环"整条链路验通。
    """

    def __init__(self, tag_names: list[str]):
        self.tag_names = tag_names
        self.round = 0
        self.total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        self.round += 1

        if self.round == 1:
            question = messages[-1].get("content") or ""
            calls: list[dict[str, Any]] = []
            for name in self.tag_names:
                if name and name in question:
                    calls.append(
                        {
                            "id": f"call_{len(calls)}",
                            "name": "diagnose_tag",
                            "arguments": {"tag_name": name},
                        }
                    )
                    break
            if "报警" in question or not calls:
                calls.append(
                    {
                        "id": f"call_{len(calls)}",
                        "name": "get_alarms",
                        "arguments": {"scope": "active", "limit": 10},
                    }
                )
            return {"content": "", "tool_calls": calls, "raw": {}}

        # 第二轮: 根据工具结果拼一份结论
        facts: list[str] = []
        for m in reversed(messages):
            if m.get("role") != "tool":
                continue
            try:
                data = json.loads(m.get("content") or "{}")
            except json.JSONDecodeError:
                continue
            if data.get("found") and "assessment" in data:
                facts.append(data["assessment"])
                trend = data.get("trend") or {}
                if trend.get("direction"):
                    facts.append(f"趋势: {trend['direction']}")
            elif "alarms" in data:
                facts.append(f"活动报警 {data.get('count', 0)} 条")

        body = "; ".join(facts) if facts else "没有取到现场数据"
        return {
            "content": (
                "[离线模式 · 规则模拟输出]\n\n"
                f"现状与判断依据: {body}\n\n"
                "说明: 这是没有 API Key 时的规则模拟结果, 只用来验证"
                "MCP 工具调用链路是否通。接上真实大模型后, 这一段会由 qwen 生成。"
            ),
            "tool_calls": [],
            "raw": {},
        }


# =====================================================================
# 工具格式转换
# =====================================================================


def to_openai_tools(mcp_tools: list[Any]) -> list[dict[str, Any]]:
    """MCP 工具定义 -> OpenAI function calling 格式。"""
    out = []
    for t in mcp_tools:
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.input_schema
                    or {"type": "object", "properties": {}},
                },
            }
        )
    return out


def extract_payload(result: Any) -> Any:
    """从 MCP 调用结果里取出数据(优先 structured_content)。"""
    structured = getattr(result, "structured_content", None)
    if structured:
        return structured
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return None


# =====================================================================
# Agent 循环
# =====================================================================


SUMMARY_PROMPT = """现在不要再调任何工具。
直接基于上面已经拿到的工具返回结果, 给出**最终诊断报告**, 中文, 结构如下:

【现状】现场关键点位的当前值和状态
【判断依据】引用的具体数值(带单位)
【根因分析】你的推理
【处置建议】可执行的步骤

工具没返回的数据一律不要编造。"""


async def run_agent(
    client: Client,
    llm: Any,
    question: str,
    max_rounds: int = 8,
) -> tuple[str, list[dict[str, Any]]]:
    """问 -> 模型要工具 -> 执行 -> 回传 -> 再问, 直到模型给出结论。

    【为什么要强制总结】
    实测 qwen-plus 有时会先说一句"现在让我获取实时值"就停住,
    或者轮次用完了还没落结论。这里兜一层: 不带工具再问一次, 逼它输出报告。
    """
    tools = to_openai_tools(list((await client.list_tools()).tools))

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]

    trace: list[dict[str, Any]] = []
    answer = ""
    forced = False

    for rnd in range(max_rounds):
        resp = llm.chat(messages, tools=tools)
        content = resp.get("content") or ""
        calls = resp.get("tool_calls") or []
        print(f"   [第 {rnd + 1} 轮] 模型要求调 {len(calls)} 个工具, 自带正文 {len(content)} 字")

        if content.strip():
            answer = content
        if not calls:
            break

        messages.append(
            {
                "role": "assistant",
                "content": answer or None,
                "tool_calls": to_assistant_tool_calls(calls),
            }
        )

        for call in calls:
            name = call["name"]
            args = call.get("arguments") or {}
            print(f"   -> 调工具 {name}({json.dumps(args, ensure_ascii=False)})")
            try:
                result = await client.call_tool(name, args)
                payload = extract_payload(result)
            except Exception as exc:                # noqa: BLE001
                payload = {"error": f"{type(exc).__name__}: {exc}"}

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "name": name,
                    "content": json.dumps(payload, ensure_ascii=False, default=str),
                }
            )
            trace.append({"tool": name, "args": args, "result": payload})
    else:
        # for-else: 轮次用完了还没 break, 说明模型一直没落结论
        print("   [提示] 轮次用完, 模型还没落结论, 强制总结一次")
        forced = True

    # 兜底: 模型正文太短(常见于只说了"我去看一下数据"就停住)也强制总结
    if not forced and len(answer.strip()) < 50:
        print("   [提示] 模型输出过短, 强制总结一次")
        forced = True

    if forced:
        try:
            resp2 = llm.chat(
                messages + [{"role": "user", "content": SUMMARY_PROMPT}],
                tools=None,
            )
            answer = resp2.get("content") or answer
            print(f"   [OK] 强制总结完成, 正文 {len(answer)} 字")
        except Exception as exc:                # noqa: BLE001
            print(f"   [WARN] 强制总结失败: {exc}")

    return answer, trace


# =====================================================================


async def main() -> int:
    parser = argparse.ArgumentParser(description="大模型报警诊断")
    parser.add_argument("--points", default="points_all4.yaml", help="点表路径")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="要问的问题")
    parser.add_argument("--model", default="qwen-plus", help="百炼模型名")
    parser.add_argument("--offline", action="store_true", help="离线模式(规则模拟大模型)")
    parser.add_argument("--no-mocks", action="store_true", help="不自动起模拟设备(用已在跑的现场)")
    parser.add_argument("--max-rounds", type=int, default=8, help="最多几轮工具调用")
    args = parser.parse_args()

    points_path = Path(args.points)
    if not points_path.is_absolute():
        points_path = HERE / points_path

    table = load_points(points_path)
    tag_names = [t.name for t in table.points]

    # ---- 模拟设备 ----
    procs = []
    if not args.no_mocks:
        print("启动模拟设备 ...")
        for script, port, extra in (
            ("mock_opcua_server.py", 4840, ()),
            ("mock_modbus_server.py", 5020, ("--port", "5020")),
            ("mock_s7_server.py", 10200, ("--port", "10200")),
        ):
            p = start_server(script, port, *extra)
            if p:
                procs.append(p)

    try:
        # ---- 大模型 ----
        if args.offline:
            llm: Any = OfflineLLM(tag_names)
            print("[INFO] 离线模式: 用规则模拟大模型\n")
        else:
            llm = BailianClient(model=args.model)
            if not llm.available:
                print("[FAIL] 没有找到 API Key。")
                print("       设环境变量 DASHSCOPE_API_KEY, 或在项目根目录放一个 .env:")
                print("         DASHSCOPE_API_KEY=sk-xxxxxxxx")
                print("       或者先用 --offline 跑通链路。")
                return 1
            print(f"[INFO] 模型: {args.model}\n")

        # ---- MCP server (stdio) ----
        server_args = [str(HERE / "mcp_server.py"), "--points", str(points_path)]
        if "eip" in table.sources:
            server_args.append("--simulate-eip")

        params = StdioServerParameters(
            command=PY,
            args=server_args,
            cwd=str(HERE),
            env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )

        print("=" * 62)
        print("  大模型报警诊断")
        print("=" * 62)
        print(f"点表: {points_path.name} ({len(table.points)} 个点位, {len(table.sources)} 个连接源)")
        print(f"问题: {args.question}")
        print()

        async with Client(params) as client:
            tools = list((await client.list_tools()).tools)
            print(f"[OK] MCP server 已连接, {len(tools)} 个工具可用")
            await asyncio.sleep(3.0)          # 等现场数据推上来
            print()

            print("-" * 62)
            print("  诊断过程")
            print("-" * 62)
            answer, trace = await run_agent(client, llm, args.question, args.max_rounds)

        print()
        print("-" * 62)
        print("  工具调用轨迹")
        print("-" * 62)
        for i, item in enumerate(trace, 1):
            print(f"{i}. {item['tool']}  参数={json.dumps(item['args'], ensure_ascii=False)}")
            res = item["result"]
            if isinstance(res, dict):
                if "assessment" in res:
                    print(f"   -> {res['assessment']}")
                elif "count" in res:
                    print(f"   -> 返回 {res['count']} 条")
                else:
                    print(f"   -> {json.dumps(res, ensure_ascii=False)[:160]}")
            else:
                print(f"   -> {str(res)[:160]}")

        print()
        print("=" * 62)
        print("  诊断报告")
        print("=" * 62)
        print(answer)
        print()

        usage = getattr(llm, "total_usage", None)
        if usage and usage.get("total_tokens"):
            print(f"[token] {usage}")

        return 0
    finally:
        stop_all(procs)


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
    except LLMError as exc:
        print(f"[FAIL] {exc}")
        sys.exit(1)
