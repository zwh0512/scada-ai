"""
曲线数据自检 —— 确认 pyqtgraph 里的每条曲线数据是否正常
=======================================================
排查目标: 截图里 Y 轴被撑到 1000, 且有一条水平线从负 x 开始,
怀疑是 setData 的空数据初始化把坐标轴搞坏了。

做法: 起一个真实的 mock server, 订阅 12 秒, 打印每条曲线的
      x 范围 / y 范围 / 数据点数, 看是否与实际值吻合。
"""

import asyncio
import os
import sys
from collections import deque
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import pyqtgraph as pg

from drivers import build_drivers, load_points

HERE = Path(__file__).resolve().parent


async def main() -> int:
    app = QApplication(sys.argv)

    table = load_points(HERE / "points.yaml")
    drivers = build_drivers(table)
    drv = drivers["opcua"]
    await drv.connect()

    trend_tags = [t for t in table.points if t.type == "float"][:4]

    widget = pg.PlotWidget()
    widget.show()
    buffers = {t.name: deque(maxlen=240) for t in trend_tags}
    curves = {}
    for tag in trend_tags:
        c = widget.plot([], [], name=f"{tag.name}({tag.unit})")
        curves[tag.name] = c

    def on_change(tag, raw):
        if tag.name not in buffers:
            return
        buffers[tag.name].append(tag.to_physical(raw))
        buf = buffers[tag.name]
        curves[tag.name].setData(list(range(len(buf))), list(buf))

    await drv.subscribe(trend_tags, on_change)
    await asyncio.sleep(12)

    print()
    print("=" * 62)
    print("  曲线数据自检")
    print("=" * 62)
    print()
    ok = True
    for tag in trend_tags:
        c = curves[tag.name]
        x = c.getData()[0]
        y = c.getData()[1]
        buf = buffers[tag.name]
        xs = list(x) if x is not None else []
        ys = list(y) if y is not None else []

        print(f"{tag.name} ({tag.unit})")
        print(f"   buffer 长度 = {len(buf)}")
        print(f"   x: n={len(xs)}  首={xs[0] if xs else None}  末={xs[-1] if xs else None}  范围=[{min(xs) if xs else None}, {max(xs) if xs else None}]")
        print(f"   y: n={len(ys)}  首={ys[0] if ys else None}  末={ys[-1] if ys else None}  范围=[{min(ys) if ys else None}, {max(ys) if ys else None}]")

        # 检查 x 是否连续递增且从 0 开始
        if xs and (xs[0] != 0 or xs != list(range(len(xs)))):
            print(f"   [FAIL] x 序列异常: {xs[:5]} ...")
            ok = False
        else:
            print(f"   [PASS] x 序列连续")

        # 检查 y 是否在合理工程量范围内
        if ys:
            span = max(ys) - min(ys)
            if span > 1000:
                print(f"   [FAIL] y 跨度 {span:.1f} 太大, 可能有脏数据")
                ok = False
            else:
                print(f"   [PASS] y 跨度 {span:.2f} 正常")
        print()

    print("=" * 62)
    print("  全部正常" if ok else "  存在异常")
    print("=" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
