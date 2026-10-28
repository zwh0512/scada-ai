"""
PySide6 最小上位机 —— 能跑、能录屏、能放简历
=============================================
左边是点表(带实时值和报警状态), 右边上是实时曲线, 右下是报警/SOE 列表。

这个例子最重要的不是界面, 是**线程模型**:

    铁律: 任何通信 / 数据库 / LLM 调用都不许阻塞 UI 线程。

    做法: 采集跑在独立 QThread 里(线程内自己跑 asyncio 事件循环),
          通过 Qt 信号把值推回 UI 线程。跨线程信号默认是队列连接, 天然线程安全。

    千万别: 在槽函数里直接 while True 或者 time.sleep —— 界面会直接冻死,
            这是 90% "上位机卡死" 的真实原因。

界面用 PySide6 (LGPL), 不用 PyQt6 (GPL, 闭源商用要买授权)。
曲线用 pyqtgraph, 不用 matplotlib —— 上万点刷新差两个数量级。

启动:
    python app.py
    python app.py --points points.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import deque
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QThread,
    QTimer,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHeaderView,
    QMainWindow,
    QSplitter,
    QStatusBar,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import pyqtgraph as pg

from drivers import AlarmEvent, Collector, load_points
from drivers.alarm import HI, LO, NORMAL
from drivers.base import Tag
from drivers.simulators import simulated_eip_overrides

# 曲线保留的历史点数
TREND_LENGTH = 240
# 最多画几条曲线
TREND_LIMIT = 4

STATE_COLORS = {
    NORMAL: QColor("#1a1a1a"),
    HI: QColor("#d93025"),
    LO: QColor("#1a73e8"),
}


# =====================================================================
# 采集线程 —— 所有网络IO都在这里, UI 线程碰都不碰
# =====================================================================

class CollectorBridge(QObject):
    """Qt 侧适配器: 把协议无关的 Collector 接到 Qt 信号上。

    采集逻辑全部在 drivers/collector.py 里(和 MCP Server 共用同一份),
    这里只干一件事: 把回调翻译成 Qt 信号, 让 UI 线程去消费。
    信号是它跟 UI 之间的唯一通道。
    """

    value_changed = Signal(str, object)      # 点位名, 工程量
    alarm_raised = Signal(object)            # AlarmEvent
    status_changed = Signal(str)
    connection_changed = Signal(bool)

    def __init__(self, table, driver_overrides=None):
        super().__init__()
        self.table = table
        self.driver_overrides = driver_overrides
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None

    @Slot()
    def stop(self) -> None:
        """请求采集线程优雅退出。

        注意: QThread.quit() 只管 Qt 自己的事件循环, 管不了我们线程里跑的
        asyncio 循环。所以必须用 asyncio.Event 从外部叫醒它。
        call_soon_threadsafe 是跨线程安全的唯一正确做法。
        """
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)

    @Slot()
    def run(self) -> None:
        """线程入口。asyncio.run 会在这条线程里建一个独立的事件循环。"""
        try:
            asyncio.run(self._main())
        except Exception as exc:                     # noqa: BLE001
            self.status_changed.emit(f"采集线程异常退出: {type(exc).__name__}: {exc}")
            self.connection_changed.emit(False)

    # ------------------------------------------------------------------

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()

        collector = Collector(
            self.table,
            on_value=lambda tag, physical: self.value_changed.emit(tag.name, physical),
            on_alarm=self.alarm_raised.emit,
            on_status=self.status_changed.emit,
            driver_overrides=self.driver_overrides,
        )

        await collector.start()
        if collector.errors:
            self.connection_changed.emit(False)
            return

        self.connection_changed.emit(True)

        # 等停止信号(QThread.quit() 对 asyncio 循环无效, 必须走 Event)
        await self._stop_event.wait()

        await collector.stop()
        self.connection_changed.emit(False)


class CollectorThread(QThread):
    """把采集跑在独立线程。

    这里覆写 run() 而不是用 started 信号, 原因:
      QThread 默认会在新线程里跑一个 Qt 事件循环。如果我们在 started 信号里
      调 asyncio.run(), 那个 asyncio 循环会把线程占死, Qt 事件循环根本没机会跑,
      于是 QThread.quit() 完全无效, 退时必崩 "QThread: Destroyed while thread
      is still running"。
      覆写 run() 之后, 线程里只有我们自己的 asyncio 循环, 停机只要一个
      asyncio.Event, 干净利落。
    """

    def __init__(self, table, driver_overrides=None, parent=None):
        super().__init__(parent)
        self.collector = CollectorBridge(table, driver_overrides)

    def run(self) -> None:
        self.collector.run()


# =====================================================================
# 点表模型 —— Model/View, 几千行表格也不卡
# =====================================================================

class PointTableModel(QAbstractTableModel):
    COLUMNS = ["点位名称", "地址", "当前值", "单位", "上限", "下限", "状态"]
    HEADERS = ["点位名称", "地址", "当前值", "单位", "报警上限", "报警下限", "状态"]

    def __init__(self, tags: list[Tag]):
        super().__init__()
        self.tags = tags
        self.values: dict[str, object] = {}
        self.states: dict[str, str] = {t.name: NORMAL for t in tags}
        self._row_of = {t.name: i for i, t in enumerate(tags)}

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.tags)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return self.HEADERS[section]
        return str(section + 1)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        tag = self.tags[index.row()]
        col = index.column()
        state = self.states.get(tag.name, NORMAL)

        if role == Qt.DisplayRole:
            if col == 0:
                return tag.name
            if col == 1:
                return tag.addr
            if col == 2:
                v = self.values.get(tag.name)
                if v is None:
                    return "--"
                if isinstance(v, bool):
                    return "运行" if v else "停止"
                if isinstance(v, (int, float)):
                    return f"{v:.2f}"
                return str(v)
            if col == 3:
                return tag.unit
            if col == 4:
                return "" if tag.alarm_hi is None else f"{tag.alarm_hi:g}"
            if col == 5:
                return "" if tag.alarm_lo is None else f"{tag.alarm_lo:g}"
            if col == 6:
                return {NORMAL: "正常", HI: "越上限", LO: "越下限"}.get(state, state)

        if role == Qt.ForegroundRole and col == 6:
            return STATE_COLORS.get(state)

        if role == Qt.ForegroundRole and col == 0 and state != NORMAL:
            return STATE_COLORS.get(state)

        if role == Qt.TextAlignmentRole and col == 2:
            return int(Qt.AlignRight | Qt.AlignVCenter)

        if role == Qt.ToolTipRole:
            return f"{tag.name}\n{tag.addr}\n{tag.desc}\n报警等级 {tag.alarm_level}  死区 {tag.deadband}"

        return None

    # ---------------- 数据更新 ----------------

    def set_value(self, name: str, value) -> None:
        if name not in self._row_of:
            return
        self.values[name] = value
        row = self._row_of[name]
        idx = self.index(row, 2)
        self.dataChanged.emit(idx, idx)

    def set_state(self, name: str, state: str) -> None:
        if name not in self._row_of:
            return
        self.states[name] = state
        row = self._row_of[name]
        idx = self.index(row, 6)
        self.dataChanged.emit(idx, idx)


# =====================================================================
# 主窗口
# =====================================================================

class MainWindow(QMainWindow):
    def __init__(self, table, driver_overrides=None):
        super().__init__()
        self.table = table
        self.driver_overrides = driver_overrides
        self.setWindowTitle("上位机原型 —— 上位机 + AI 融合")
        self.resize(1280, 780)

        self.model = PointTableModel(table.points)
        self.trend_tags = [t for t in table.points if t.type == "float"][:TREND_LIMIT]
        self.buffers: dict[str, deque] = {t.name: deque(maxlen=TREND_LENGTH) for t in self.trend_tags}
        self.curves: dict[str, object] = {}

        self._build_ui()
        self._start_collector()

    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 6)

        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter)

        # --- 左: 点表 ---
        self.point_view = QTableView()
        self.point_view.setModel(self.model)
        self.point_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.point_view.setAlternatingRowColors(True)
        self.point_view.verticalHeader().setDefaultSectionSize(24)
        self.point_view.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.point_view.setColumnWidth(0, 150)
        self.point_view.setColumnWidth(2, 90)
        splitter.addWidget(self.point_view)

        # --- 右: 上下分 ---
        right = QSplitter(Qt.Vertical)

        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget()
        self.plot.setLabel("left", "工程量")
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.addLegend(offset=(10, 10))
        for tag in self.trend_tags:
            # 初始不要调 setData([], [])。pyqtgraph 在空数据集上做自动范围计算
            # 会留下垃圾坐标轴(实测 y 轴被撑到 1000, 还多出一条负 x 的水平线)。
            curve = self.plot.plot([], [], name=f"{tag.name}({tag.unit})")
            self.curves[tag.name] = curve
        right.addWidget(self.plot)

        # --- 右下: 报警 / SOE ---
        self.alarm_table = QTableWidget(0, 5)
        self.alarm_table.setHorizontalHeaderLabels(["时间", "点位", "状态", "数值", "说明"])
        self.alarm_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.alarm_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.alarm_table.setAlternatingRowColors(True)
        right.addWidget(self.alarm_table)

        right.setSizes([420, 240])
        splitter.addWidget(right)
        splitter.setSizes([520, 740])

        self.setCentralWidget(central)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("正在启动采集线程 ...")

        self.setStyleSheet(
            """
            QMainWindow { background: #fafafa; }
            QTableView, QTableWidget {
                background: white;
                gridline-color: #e0e0e0;
                font-family: "Microsoft YaHei UI";
                font-size: 12px;
            }
            QHeaderView::section {
                background: #eceff1;
                padding: 4px;
                border: none;
                border-right: 1px solid #e0e0e0;
                border-bottom: 1px solid #e0e0e0;
                font-weight: bold;
            }
            QStatusBar { background: #eceff1; }
            """
        )

    # ------------------------------------------------------------------

    def _start_collector(self) -> None:
        """把采集放到独立线程。UI 线程从此只负责画。"""
        self.thread = CollectorThread(self.table, self.driver_overrides)
        self.collector = self.thread.collector

        # 跨线程信号 -> 队列连接, 自动切回 UI 线程
        self.collector.value_changed.connect(self._on_value)
        self.collector.alarm_raised.connect(self._on_alarm)
        self.collector.status_changed.connect(self._on_status)
        self.collector.connection_changed.connect(self._on_connection)

        self.thread.start()

    # ---------------- 信号处理(都在 UI 线程) ----------------

    @Slot(str, object)
    def _on_value(self, name: str, value) -> None:
        self.model.set_value(name, value)

        buf = self.buffers.get(name)
        if buf is not None:
            buf.append(value)
            curve = self.curves.get(name)
            if curve is not None:
                # 只传 y, 让 pyqtgraph 自己生成 x = 0..n-1。
                # 手动传 x 数组在空数据初始化后容易留下脏坐标轴。
                curve.setData(y=list(buf))

    @Slot(object)
    def _on_alarm(self, event: AlarmEvent) -> None:
        # 状态回写到点表
        self.model.set_state(event.tag_name, event.state)

        # SOE 记录, 新的插最上面
        row = self.alarm_table.rowCount()
        self.alarm_table.insertRow(0)
        text = {HI: "越上限", LO: "越下限", NORMAL: "已恢复"}.get(event.state, event.state)
        cells = [
            event.time_text,
            event.tag_name,
            text,
            f"{event.value:.2f}" if isinstance(event.value, (int, float)) else str(event.value),
            event.desc or event.tag_addr,
        ]
        for col, val in enumerate(cells):
            item = QTableWidgetItem(str(val))
            if col == 2:
                item.setForeground(
                    STATE_COLORS.get(NORMAL if event.state == NORMAL else event.state, QColor("#000000"))
                )
                item.setForeground(QColor("#0b8043") if event.state == NORMAL else STATE_COLORS.get(event.state, QColor("#000")))
            self.alarm_table.setItem(0, col, item)

        if self.alarm_table.rowCount() > 200:
            self.alarm_table.removeRow(self.alarm_table.rowCount() - 1)

    @Slot(str)
    def _on_status(self, msg: str) -> None:
        self.status.showMessage(msg, 8000)

    @Slot(bool)
    def _on_connection(self, ok: bool) -> None:
        self.status.showMessage("已连接" if ok else "未连接", 0)

    # ------------------------------------------------------------------

    def closeEvent(self, event):
        # 先叫醒采集线程里的 asyncio 循环, 再等线程自己退出。
        # 顺序反了就会出现 "QThread: Destroyed while thread is still running"。
        try:
            self.collector.stop()
            self.thread.wait(5000)
        except Exception:
            pass
        super().closeEvent(event)


# =====================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="上位机原型")
    parser.add_argument("--points", default="points.yaml", help="点表路径")
    parser.add_argument("--shot", default=None, help="截图输出路径: 跑几秒后截图并退出")
    parser.add_argument("--shot-seconds", type=float, default=10.0, help="截图前等待秒数")
    parser.add_argument(
        "--alarm-demo",
        action="store_true",
        help="演示模式: 把所有上限压到 0, 让报警立刻触发, 用来录 Demo 视频/自测报警面板",
    )
    parser.add_argument(
        "--simulate-eip",
        action="store_true",
        help="用内置模拟器替代真实 AB 控制器(EtherNet/IP 没有现成的模拟器)",
    )
    args = parser.parse_args()

    here = Path(__file__).resolve().parent
    points_path = Path(args.points)
    if not points_path.is_absolute():
        points_path = here / points_path

    table = load_points(points_path)

    overrides = simulated_eip_overrides(table) if args.simulate_eip else None
    if args.simulate_eip:
        print(f"[模拟模式] AB 控制器已替换为内置模拟器: {sorted(overrides or {})}")

    if args.alarm_demo:
        # 演示模式: 上限压到 0, 任何正数都立刻越限, 报警面板马上有内容
        for tag in table.points:
            if tag.alarm_hi is not None:
                tag.alarm_hi = 0.0
        print("[演示模式] 所有报警上限已压到 0")

    print(f"点表: {points_path}")
    print(f"  连接源: {list(table.sources.keys())}")
    print(f"  点位数: {len(table.points)}")
    for src, tags in table.by_source().items():
        print(f"    {src}: {len(tags)} 个")

    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei UI", 9))

    win = MainWindow(table, overrides)
    win.show()

    if args.shot:
        # 截图模式: 跑一会儿抓一张图存盘, 然后退出。
        # 用来做无人值守的回归验证, 也方便直接贴简历。
        def do_shot() -> None:
            out = Path(args.shot)
            out.parent.mkdir(parents=True, exist_ok=True)
            ok = win.grab().save(str(out))
            print(f"截图: {'已保存' if ok else '失败'} -> {out}")
            win.close()          # 触发 closeEvent, 干净地收线程
            app.quit()

        QTimer.singleShot(int(args.shot_seconds * 1000), do_shot)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
