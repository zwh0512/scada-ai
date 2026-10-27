"""
MCP Server —— 把现场数据接给大模型
==================================
这是整个项目的技术分水岭: 上位机采到的数据, 从这里流向 AI。

    大模型 / AI 客户端  <--MCP (stdio)-->  本进程  <--async-->  Collector
                                                            |
                                              Driver(OPC UA / Modbus / S7)
                                                            |
                                                        现场设备

三个设计要点:

1. **采集器常驻**
   本进程自己持有 OPC UA 订阅, 值一直在内存缓存里。
   工具调用是读内存, 毫秒级返回, 不受现场网络和设备负载影响 ——
   大模型问一次"磨机电流多少", 不该等一个 RTT 再等 PLC 响应。

2. **工具描述就是给大模型看的**
   每个工具的 description 必须写清"什么时候用、返回什么、注意什么"。
   大模型就是靠这段文字决定调哪个工具, 描述写糊了工具就等于没有。

3. **写操作有白名单闸门**
   只有点表里显式标了 writable: true 的点能写, 其余一律拒绝。
   把写权限交给大模型, 必须有这道闸。

【必须记住的坑】
stdio 传输时 **stdout 是 MCP 的 JSON-RPC 通道**, 任何 print() 都会把协议流冲坏,
客户端直接报解析错误。所以这个文件里所有日志一律走 stderr。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from drivers import AlarmEvent, Collector, PointTable, load_points
from drivers.alarm import HI, LO, NORMAL

# asyncua 默认会打 DEBUG 日志, 订阅推送时每个值都打一行, 直接把 stderr 刷爆。
# MCP 的 stderr 是给客户端看日志用的, 这里调到 WARNING 只留真正的告警。
logging.getLogger("asyncua").setLevel(logging.WARNING)
logging.getLogger("asyncua.uaprotocol").setLevel(logging.WARNING)

# =====================================================================
# 全局上下文 —— 工具函数通过它拿到采集器
# =====================================================================

_CTX: dict[str, Any] = {"table": None, "collector": None}


def _log(msg: str) -> None:
    """日志一律走 stderr。stdout 是 MCP 协议通道, 碰不得。"""
    print(msg, file=sys.stderr, flush=True)


def _ctx() -> tuple[PointTable, Collector]:
    table = _CTX["table"]
    collector = _CTX["collector"]
    if table is None or collector is None:
        raise RuntimeError("采集器还没启动, 工具调用过早")
    return table, collector


# =====================================================================
# MCP Server
# =====================================================================

server = MCPServer(
    name="scada-ai",
    version="0.1.0",
    instructions=(
        "工业现场上位机的实时数据与报警查询工具。数据来自常驻的现场订阅缓存, 单位是工程量。\n"
        "排查报警/故障的推荐顺序:\n"
        "  1) search_tags  按关键词找点位(名称/说明/地址都参与匹配)\n"
        "  2) list_tags    看某个连接源下完整有哪些点位及报警阈值\n"
        "  3) read_values  读实时值(不给名字就读全部)\n"
        "  4) get_alarms   看当前活动报警(scope=active)和历史 SOE(scope=history)\n"
        "  5) diagnose_tag 对单个点位做完整诊断: 当前值/阈值余量/变化趋势/该点报警史/同源其它点\n"
        "判断'为什么报警'时优先用 diagnose_tag, 它一次给全了趋势和历史。"
    ),
)

STATE_TEXT = {HI: "越上限", LO: "越下限", NORMAL: "正常"}
LEVEL_TEXT = {1: "低", 2: "中", 3: "高"}


# ---------------------------------------------------------------------
# 工具 1: 列点表
# ---------------------------------------------------------------------


@server.tool(
    description=(
        "列出点表里配置的 monitoring 点位: 名称、所属连接源(source)、协议地址(addr)、"
        "工程量单位、报警上下限和死区、是否可写。"
        "用于回答'这套系统监控了哪些点'或'某个连接源下面有哪些设备'。"
        "不传 source 返回全部点位; 传 source(如 opcua)只返回该连接源的点位。"
    )
)
async def list_tags(source: str = "") -> dict[str, Any]:
    """列点表配置。"""
    table, _ = _ctx()
    tags = table.points
    if source:
        tags = [t for t in tags if t.source == source]

    return {
        "count": len(tags),
        "sources": sorted(table.sources.keys()),
        "points": [
            {
                "name": t.name,
                "source": t.source,
                "addr": t.addr,
                "type": t.type,
                "unit": t.unit,
                "desc": t.desc,
                "alarm_hi": t.alarm_hi,
                "alarm_lo": t.alarm_lo,
                "deadband": t.deadband,
                "alarm_level": LEVEL_TEXT.get(t.alarm_level, str(t.alarm_level)),
                "writable": t.writable,
            }
            for t in tags
        ],
    }


# ---------------------------------------------------------------------
# 工具 2: 搜点位
# ---------------------------------------------------------------------


@server.tool(
    description=(
        "按关键字搜索点位。关键字会在点位名称、中文说明、协议地址、单位里做不区分大小写的包含匹配。"
        "用于'找磨机相关的点'、'找温度点'这类模糊定位。"
        "找不到就返回空列表, 建议再试 list_tags。"
    )
)
async def search_tags(keyword: str) -> dict[str, Any]:
    """按关键字搜点位。"""
    table, collector = _ctx()
    hits = collector.find_tags(keyword)
    return {
        "keyword": keyword,
        "count": len(hits),
        "points": [
            {
                "name": t.name,
                "source": t.source,
                "addr": t.addr,
                "unit": t.unit,
                "desc": t.desc,
                "alarm_hi": t.alarm_hi,
                "alarm_lo": t.alarm_lo,
                "current_value": _fmt_value(collector.values.get(t.name)),
            }
            for t in hits
        ],
    }


# ---------------------------------------------------------------------
# 工具 3: 读实时值
# ---------------------------------------------------------------------


@server.tool(
    description=(
        "读取实时值。tag_names 不传(或传空列表)就读全部点位; 传了就只读这几个。"
        "返回工程量(已做 scale/offset 换算)、单位、质量标记和数据年龄(age_ms, 距上次更新多久)。"
        "数据来自常驻订阅缓存, 是毫秒级读内存, 不是现场实时查询。"
        "age_ms 很大说明现场通信有问题, 要结合 get_alarms 看。"
    )
)
async def read_values(tag_names: list[str] | None = None) -> dict[str, Any]:
    """读实时值。"""
    table, collector = _ctx()

    if tag_names:
        wanted = [t for t in tag_names if t]
        tags = []
        missing = []
        for name in wanted:
            tag = table.get(name)
            if tag is None:
                missing.append(name)
            else:
                tags.append(tag)
    else:
        tags = list(table.points)
        missing = []

    values = []
    for tag in tags:
        lv = collector.values.get(tag.name)
        if lv is None:
            values.append(
                {
                    "name": tag.name,
                    "value": None,
                    "text": "无数据",
                    "unit": tag.unit,
                    "quality": "NO_DATA",
                    "age_ms": -1,
                }
            )
            continue
        values.append(
            {
                "name": tag.name,
                "value": _plain(lv.physical),
                "text": _fmt_value(lv),
                "unit": tag.unit,
                "source": tag.source,
                "desc": tag.desc,
                "quality": lv.quality,
                "age_ms": lv.age_ms,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(lv.ts)),
                "alarm_state": STATE_TEXT.get(
                    collector.engine.current_state(tag.name), "正常"
                ),
            }
        )

    return {
        "count": len(values),
        "values": values,
        "missing_tags": missing,
        "connected": collector.connected,
        "sources": _source_status(collector),
    }


# ---------------------------------------------------------------------
# 工具 4: 报警 / SOE
# ---------------------------------------------------------------------


@server.tool(
    description=(
        "查报警。scope='active' 返回当前仍在报警中的点位(已用死区判定, 不会抖); "
        "scope='history' 返回 SOE 顺序事件记录, 包含触发和恢复, 按时间倒序。"
        "limit 控制条数(默认 20)。"
        "排查问题时 active 和 history 都要看: active 看现在有什么问题, history 看之前发生过什么。"
    )
)
async def get_alarms(scope: str = "active", limit: int = 20) -> dict[str, Any]:
    """查报警和 SOE 历史。"""
    _, collector = _ctx()
    scope = (scope or "active").lower()

    if scope == "history":
        events = list(reversed(collector.engine.soe))[: max(1, limit)]
        items = [_alarm_dict(e) for e in events]
    else:
        items = [_alarm_dict(e) for e in collector.engine.active_alarms()][: max(1, limit)]

    return {
        "scope": scope,
        "count": len(items),
        "alarms": items,
        "total_soe": len(collector.engine.soe),
    }


# ---------------------------------------------------------------------
# 工具 5: 单点诊断 —— 最有用的一个
# ---------------------------------------------------------------------


@server.tool(
    description=(
        "对单个点位做完整诊断, 一次给全: 当前值和单位、报警状态、"
        "距上限/下限还有多少余量、最近的变化趋势(首值/末值/最小/最大/增量/最近序列)、"
        "该点位的报警历史(SOE)、以及同一连接源下其它点位的当前值快照。"
        "排查'为什么这个点报警了'优先用这个工具, 不用再分别查多个工具拼。"
    )
)
async def diagnose_tag(tag_name: str) -> dict[str, Any]:
    """单点诊断。"""
    table, collector = _ctx()

    tag = table.get(tag_name)
    if tag is None:
        hits = collector.find_tags(tag_name)
        return {
            "found": False,
            "requested": tag_name,
            "message": f"没有叫 '{tag_name}' 的点位",
            "did_you_mean": [t.name for t in hits[:10]],
        }

    lv = collector.values.get(tag.name)
    state = collector.engine.current_state(tag.name)

    # ---- 阈值余量 ----
    value = lv.physical if lv else None
    to_hi = None
    to_lo = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if tag.alarm_hi is not None:
            to_hi = round(tag.alarm_hi - value, 4)
        if tag.alarm_lo is not None:
            to_lo = round(value - tag.alarm_lo, 4)

    # ---- 趋势 ----
    trend = _trend(collector, tag.name)

    # ---- 该点位的 SOE ----
    soe = [_alarm_dict(e) for e in reversed(collector.engine.soe) if e.tag_name == tag.name][:10]

    # ---- 同源其它点 ----
    related = []
    for other in table.points:
        if other.name == tag.name or other.source != tag.source:
            continue
        olv = collector.values.get(other.name)
        related.append(
            {
                "name": other.name,
                "value": _plain(olv.physical) if olv else None,
                "text": _fmt_value(olv),
                "unit": other.unit,
                "alarm_state": STATE_TEXT.get(
                    collector.engine.current_state(other.name), "正常"
                ),
            }
        )

    result = {
        "found": True,
        "tag": {
            "name": tag.name,
            "source": tag.source,
            "addr": tag.addr,
            "unit": tag.unit,
            "desc": tag.desc,
            "alarm_hi": tag.alarm_hi,
            "alarm_lo": tag.alarm_lo,
            "deadband": tag.deadband,
            "alarm_level": LEVEL_TEXT.get(tag.alarm_level, str(tag.alarm_level)),
        },
        "current": {
            "value": _plain(value),
            "text": _fmt_value(lv),
            "quality": lv.quality if lv else "NO_DATA",
            "age_ms": lv.age_ms if lv else -1,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(lv.ts)) if lv else None,
        },
        "state": state,
        "state_text": STATE_TEXT.get(state, state),
        "margin": {"to_hi": to_hi, "to_lo": to_lo},
        "trend": trend,
        "soe": soe,
        "related": related,
        "assessment": _assess(tag, lv, state, to_hi, to_lo, trend),
    }
    return result


# ---------------------------------------------------------------------
# 工具 6: 写值 (有闸门)
# ---------------------------------------------------------------------


@server.tool(
    description=(
        "写一个点位的值。只对点表里标了 writable: true 的点位有效, "
        "其余一律返回错误 —— 这是防止大模型误写生产设备的安全闸门。"
        "调用前先用 list_tags 确认点位可写。"
    )
)
async def write_value(tag_name: str, value: float) -> dict[str, Any]:
    """写点位(受 writable 白名单限制)。"""
    _, collector = _ctx()
    try:
        await collector.write(tag_name, value)
    except KeyError as exc:
        return {"ok": False, "tag_name": tag_name, "error": str(exc)}
    except PermissionError as exc:
        return {
            "ok": False,
            "tag_name": tag_name,
            "error": str(exc),
            "hint": "点表里给该点位加 writable: true 才能写",
        }
    except Exception as exc:                        # noqa: BLE001
        return {"ok": False, "tag_name": tag_name, "error": f"{type(exc).__name__}: {exc}"}

    return {"ok": True, "tag_name": tag_name, "value": value}


# =====================================================================
# 辅助函数
# =====================================================================


def _plain(value: Any) -> Any:
    """把值转成 JSON 友好的类型。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 4)
    return value


def _source_status(collector: Collector) -> dict[str, str]:
    """每个连接源的状态。大模型要能一眼看出"哪台设备掉线了"。"""
    out: dict[str, str] = {}
    for name in collector.table.sources.keys():
        if name in collector.connected_sources:
            out[name] = "OK"
        else:
            out[name] = f"FAIL: {collector.failed_sources.get(name, '未连接')}"
    return out


def _fmt_value(lv: Any) -> str:
    """工程量的文字表示。bool 显示成 运行/停止, 数值带单位。"""
    if lv is None:
        return "无数据"
    v = lv.physical
    if isinstance(v, bool):
        return "运行" if v else "停止"
    if isinstance(v, (int, float)):
        unit = lv.tag.unit
        return f"{v:.2f} {unit}".strip()
    return str(v)


def _alarm_dict(event: AlarmEvent) -> dict[str, Any]:
    """SOE / 活动报警 -> 字典。"""
    return {
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event.ts)),
        "tag_name": event.tag_name,
        "addr": event.tag_addr,
        "state": event.state,
        "state_text": STATE_TEXT.get(event.state, event.state),
        "level": LEVEL_TEXT.get(event.level, str(event.level)),
        "value": _plain(event.value),
        "unit": event.unit,
        "desc": event.desc,
    }


def _trend(collector: Collector, name: str) -> dict[str, Any]:
    """算一个点位的变化趋势。

    关键在 delta 和方向: 报警分析里"当前值超限"只是现象,
    "是慢慢爬上去的"和"刚跳上去的"对应的根因完全不一样。
    """
    hist = list(collector.history.get(name, []))
    if len(hist) < 2:
        return {
            "samples": len(hist),
            "direction": "数据不足",
            "series": [],
        }

    vals = [v for _, v in hist]
    first, last = vals[0], vals[-1]
    delta = last - first
    span = hist[-1][0] - hist[0][0]

    if abs(delta) < 1e-6:
        direction = "基本不变"
    elif abs(delta) < max(abs(last), 1.0) * 0.02:
        direction = "小幅波动"
    else:
        direction = "持续上升" if delta > 0 else "持续下降"

    # 单位时间变化率
    rate = round(delta / span, 5) if span > 0.2 else None

    return {
        "samples": len(hist),
        "window_seconds": round(span, 1),
        "first": round(first, 4),
        "last": round(last, 4),
        "min": round(min(vals), 4),
        "max": round(max(vals), 4),
        "delta": round(delta, 4),
        "rate_per_sec": rate,
        "direction": direction,
        "series": [round(v, 4) for v in vals[-20:]],
    }


def _assess(tag, lv, state: str, to_hi, to_lo, trend: dict) -> str:
    """给一句确定性的判断。

    这不是 AI 的活儿 —— AI 要的是"事实", 判断规则由上位机给。
    规则化判断 + 大模型自由推理, 比让大模型自己算阈值余量靠谱得多。
    """
    if lv is None:
        return f"点位 '{tag.name}' 还没有收到数据, 现场可能没连上"

    value = lv.physical
    if isinstance(value, bool):
        return f"{tag.name} 当前为 {'运行' if value else '停止'}"

    parts = [f"当前 {value:.2f} {tag.unit}".strip()]

    if state == HI:
        over = abs(to_hi) if to_hi is not None else 0.0
        parts.append(f"已越上限 {tag.alarm_hi} (超出 {over:.2f} {tag.unit})".strip())
    elif state == LO:
        under = abs(to_lo) if to_lo is not None else 0.0
        parts.append(f"已越下限 {tag.alarm_lo} (低于 {under:.2f} {tag.unit})".strip())
    else:
        if to_hi is not None and to_hi <= 5:
            parts.append(f"距上限 {to_hi:.2f} {tag.unit}, 余量偏小".strip())
        elif to_lo is not None and to_lo <= 5:
            parts.append(f"距下限 {to_lo:.2f} {tag.unit}, 余量偏小".strip())
        else:
            parts.append("状态正常")

    if trend.get("direction") not in (None, "数据不足", "基本不变"):
        parts.append(f"近期{trend['direction']}({trend.get('window_seconds')} 秒内 {trend.get('delta')})")

    return "; ".join(parts)


# =====================================================================
# 启动
# =====================================================================


async def main() -> int:
    parser = argparse.ArgumentParser(description="scada-ai MCP Server")
    parser.add_argument("--points", default="points.yaml", help="点表路径")
    args = parser.parse_args()

    here = Path(__file__).resolve().parent
    points_path = Path(args.points)
    if not points_path.is_absolute():
        points_path = here / points_path

    table = load_points(points_path)
    _log(f"[INFO] 点表: {points_path} ({len(table.points)} 个点位)")

    collector = Collector(
        table,
        on_status=lambda m: _log(m),
        on_alarm=lambda e: _log(
            f"[ALARM] {e.tag_name} {STATE_TEXT.get(e.state, e.state)} {e.value} {e.unit}"
        ),
    )
    _CTX["table"] = table
    _CTX["collector"] = collector

    await collector.start()
    for err in collector.errors:
        # 一个源掉线不该让 MCP 整个不可用, 其余源照常供数
        _log(f"[WARN] {err} (其它连接源继续工作)")
    if not collector.connected:
        _log("[FAIL] 所有现场连接源都没连上, MCP server 不启动")
        return 1

    _log(
        f"[OK] MCP server 就绪, 已连接: {sorted(collector.connected_sources)}, 监听 stdio"
    )

    try:
        await server.run_stdio_async()
    finally:
        await collector.stop()

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
