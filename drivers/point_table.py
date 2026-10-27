"""
点表加载 —— 工业上位机的核心思想
================================
组态软件的灵魂是 "画面 + 点表": 画面不写死任何地址, 全部由点表驱动。
用代码表达这件事, 就是让 YAML 点表同时驱动采集、显示、报警三件事。

一个 YAML 同时装下连接信息(sources)和点位清单(points):
    sources  -> 建哪些驱动、连到哪
    points   -> 点位从哪来、什么类型、什么量程、报警阈值
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .base import Tag


@dataclass
class PointTable:
    sources: dict[str, dict[str, Any]]
    points: list[Tag]

    def by_source(self) -> dict[str, list[Tag]]:
        """按协议把点位分组, 驱动层要靠这个决定谁负责读哪些点。"""
        groups: dict[str, list[Tag]] = {}
        for tag in self.points:
            groups.setdefault(tag.source, []).append(tag)
        return groups

    def get(self, name: str) -> Tag | None:
        for tag in self.points:
            if tag.name == name:
                return tag
        return None


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def load_points(path: str | Path) -> PointTable:
    """从 YAML 读点表。

    路径可以是:
      - points.yaml            (相对当前工作目录)
      - 项目根下的 points.yaml (自动向上找)
    """
    p = Path(path)
    if not p.is_absolute() and not p.exists():
        # 从本文件往上找项目根
        here = Path(__file__).resolve().parent.parent
        candidate = here / path
        if candidate.exists():
            p = candidate

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    sources = raw.get("sources") or {}
    points: list[Tag] = []

    for item in raw.get("points") or []:
        if "name" not in item or "addr" not in item:
            raise ValueError(f"点表条目缺少 name/addr: {item}")
        points.append(
            Tag(
                name=str(item["name"]),
                source=str(item.get("source", "opcua")),
                addr=str(item["addr"]),
                type=str(item.get("type", "float")),
                scale=float(item.get("scale", 1.0)),
                offset=float(item.get("offset", 0.0)),
                deadband=float(item.get("deadband", 0.0)),
                unit=str(item.get("unit", "")),
                desc=str(item.get("desc", "")),
                alarm_hi=_opt_float(item.get("alarm_hi")),
                alarm_lo=_opt_float(item.get("alarm_lo")),
                alarm_level=int(item.get("alarm_level", 1)),
                writable=bool(item.get("writable", False)),
            )
        )

    # 点位名必须全局唯一。
    # 值缓存、报警状态、SOE、MCP 工具全都是按名字索引的,
    # 重名会导致两个不同协议的同名点互相覆盖 —— 界面上看着"有值",
    # 实际读到的是另一个设备的数。这种错不报错最难查, 加载时就拦掉。
    seen: dict[str, str] = {}
    for tag in points:
        if tag.name in seen:
            raise ValueError(
                f"点位名重复: '{tag.name}' "
                f"(地址 {seen[tag.name]} 和 {tag.addr} 撞名了)。"
                f"点位名在整个点表里必须唯一。"
            )
        seen[tag.name] = tag.addr

    return PointTable(sources=sources, points=points)
