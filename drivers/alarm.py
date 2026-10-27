"""
报警引擎 —— ISA-18.2 简化模型
==============================
这是你 5 年上位机经验最值钱、也最容易被 AI 替代不了的部分。

核心概念(只保留工程上真用得上的):
  NORMAL  正常
  HI      越上限
  LO      越下限
  死区    报警触发要越过阈值, 恢复要回到 阈值 - 死区, 防止值在阈值附近抖动反复跳报警
  SOE     顺序事件记录, 只追加不修改, 是追溯事故的唯一依据

真实工程里的报警系统还有: 搁置(shelving)、抑制(suppression)、报警风暴收敛、
事件确认/复位、报警优先级。这里先做最小可用版本, 后面接 MCP 时再扩。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .base import Tag

NORMAL = "NORMAL"
HI = "HI"
LO = "LO"

# 报警等级中文名, 直接上界面
LEVEL_TEXT = {1: "低", 2: "中", 3: "高"}


@dataclass
class AlarmEvent:
    """一条 SOE 记录。产生即冻结, 不再修改。"""

    ts: float
    tag_name: str
    tag_addr: str
    state: str          # HI / LO / NORMAL(恢复)
    level: int
    value: Any
    unit: str = ""
    desc: str = ""

    @property
    def time_text(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.ts))


@dataclass
class _State:
    tag: Tag
    state: str = NORMAL
    value: Any = None
    since: float = 0.0
    history: list[AlarmEvent] = field(default_factory=list)


class AlarmEngine:
    """按 Tag 的阈值判断越限, 维护状态机, 输出 SOE。

    用法:
        engine = AlarmEngine(on_event=my_callback)
        engine.check(tag, raw_value)     # 每个值变化时调一次
    """

    def __init__(self, on_event: Callable[[AlarmEvent], None] | None = None):
        self._states: dict[str, _State] = {}
        self._on_event = on_event
        self.soe: list[AlarmEvent] = []       # 顺序事件记录

    def register(self, tags: list[Tag]) -> None:
        for tag in tags:
            self._states.setdefault(tag.name, _State(tag=tag))

    # ------------------------------------------------------------------

    def check(self, tag: Tag, raw_value: Any) -> AlarmEvent | None:
        """检查一个点位的值。返回本次产生的事件(没有则 None)。"""
        state = self._states.get(tag.name)
        if state is None:
            state = _State(tag=tag)
            self._states[tag.name] = state
        state.value = raw_value

        # bool / 非数值不做越限判断
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            return None

        value = tag.to_physical(raw_value)
        hi = tag.alarm_hi
        lo = tag.alarm_lo
        db = tag.deadband

        if state.state == NORMAL:
            if hi is not None and value > hi:
                return self._fire(state, HI, value)
            if lo is not None and value < lo:
                return self._fire(state, LO, value)
            return None

        # 已报警: 要回到 阈值 ∓ 死区 才算恢复
        if state.state == HI and hi is not None and value < hi - db:
            return self._fire(state, NORMAL, value)
        if state.state == LO and lo is not None and value > lo + db:
            return self._fire(state, NORMAL, value)
        return None

    def current_state(self, tag_name: str) -> str:
        state = self._states.get(tag_name)
        return state.state if state else NORMAL

    def active_alarms(self) -> list[AlarmEvent]:
        """当前还处于报警中的点位。"""
        out = []
        for st in self._states.values():
            if st.state != NORMAL:
                out.append(
                    AlarmEvent(
                        ts=st.since,
                        tag_name=st.tag.name,
                        tag_addr=st.tag.addr,
                        state=st.state,
                        level=st.tag.alarm_level,
                        # st.value 存的是原始值, 这里要跟 SOE 事件一样换成工程量,
                        # 否则 scale != 1 的点位会出现"报警值和界面值对不上"
                        value=st.tag.to_physical(st.value),
                        unit=st.tag.unit,
                        desc=st.tag.desc,
                    )
                )
        return out

    # ------------------------------------------------------------------

    def _fire(self, state: _State, new_state: str, value: Any) -> AlarmEvent:
        tag = state.tag
        state.state = new_state
        if new_state == NORMAL:
            state.since = 0.0
        else:
            state.since = time.time()

        event = AlarmEvent(
            ts=time.time(),
            tag_name=tag.name,
            tag_addr=tag.addr,
            state=new_state,
            level=tag.alarm_level,
            value=value,
            unit=tag.unit,
            desc=tag.desc,
        )
        state.history.append(event)
        self.soe.append(event)

        if self._on_event:
            self._on_event(event)
        return event
