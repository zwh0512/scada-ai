"""
模拟 S7 PLC (python-snap7 Server)
=================================
模拟一台 S7-1200, 用来把 S7 驱动跑通。**值是活的** —— 每个信号
是不同周期的正弦波, 跟真实喷煤线的过程量一个调调。

DB1 (给煤控制器):
    0-3    给煤机设定量  REAL   2.50 t/h    (静态, 可写)
    4-7    给煤机实际量  REAL   live t/h
    8-11   一次风温度    REAL   live degC
    12-13  给煤机转速    INT    live rpm
    14-15  累计运行小时  UINT   1234 h
    16     运行标志      BOOL   1
    17-20  给煤机电流    REAL   live A

DB2 (二线):
    0-3    二线磨机电流  REAL   live A

【实现要点】
1. `Server.register_area(SrvArea.DB, 1, bytearray)` 注册数据块,
   **传进去的 bytearray 就是 PLC 的内存**, 客户端写进去的值会落到这块内存,
   反过来我改这块内存, 客户端立刻能读到 —— 这就是"活的"设备的原理。
2. `Server.start(tcp_port)` 的参数才是真正的监听端口。
   只设 `Parameter.LocalPort` 不管用, 会被 start() 的默认值覆盖 —— 踩过。
3. snap7 的 Server 是阻塞式 C 实现, start() 之后自己开线程跑,
   主线程负责更新数据就行。

跑法:
    python mock_s7_server.py
    python mock_s7_server.py --port 10200
"""

from __future__ import annotations

import argparse
import math
import struct
import time

from snap7 import Server
from snap7.type import Parameter, SrvArea

# ---- DB1 布局 (字节偏移) ----
DB1_SETPOINT = 0      # 0-3   REAL  设定量 (静态, 可写)
DB1_ACTUAL = 4        # 4-7   REAL  实际量
DB1_AIR_TEMP = 8      # 8-11  REAL  一次风温度
DB1_SPEED = 12        # 12-13 INT   转速
DB1_HOURS = 14        # 14-15 UINT  运行小时
DB1_RUNNING = 16      # 16    BOOL  运行标志
DB1_CURRENT = 17      # 17-20 REAL  给煤机电流

DB1_SIZE = 32

# ---- DB2 布局 ----
DB2_MILL_CURRENT = 0  # 0-3   REAL  二线磨机电流
DB2_SIZE = 8


def _live(t: float) -> dict[str, float]:
    """按时间生成"活的"过程数据。不同信号用不同周期, 免得一起涨落。"""
    return {
        "actual": 1.85 + 0.35 * math.sin(t / 7.0),
        "air_temp": 187.5 + 8.0 * math.sin(t / 11.0 + 1.0),
        "speed": 1850 + 45 * math.sin(t / 6.0 + 0.2),
        "current": 42.5 + 3.5 * math.sin(t / 5.0 + 0.6),
        "mill_current": 39.5 + 3.0 * math.sin(t / 8.0 + 2.4),
    }


def _refresh(db1: bytearray, db2: bytearray) -> None:
    """刷新活信号。设定量是静态的, 不动 —— 否则客户端写进去马上被覆盖。"""
    v = _live(time.time())
    struct.pack_into(">f", db1, DB1_ACTUAL, v["actual"])
    struct.pack_into(">f", db1, DB1_AIR_TEMP, v["air_temp"])
    struct.pack_into(">h", db1, DB1_SPEED, int(v["speed"]))
    struct.pack_into(">f", db1, DB1_CURRENT, v["current"])
    struct.pack_into(">f", db2, DB2_MILL_CURRENT, v["mill_current"])


def main() -> int:
    parser = argparse.ArgumentParser(description="模拟 S7 PLC")
    parser.add_argument("--port", type=int, default=10200, help="TCP 端口")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--interval", type=float, default=0.5, help="刷新周期(秒)")
    args = parser.parse_args()

    db1 = bytearray(DB1_SIZE)
    db2 = bytearray(DB2_SIZE)

    # 初始值
    struct.pack_into(">f", db1, DB1_SETPOINT, 2.50)
    struct.pack_into(">H", db1, DB1_HOURS, 1234)
    db1[DB1_RUNNING] = 1
    _refresh(db1, db2)

    server = Server()
    server.set_param(Parameter.LocalPort, args.port)
    server.set_param(Parameter.RemotePort, args.port)
    server.register_area(SrvArea.DB, 1, db1)
    server.register_area(SrvArea.DB, 2, db2)

    rc = server.start(args.port)
    if rc != 0:
        print(f"[FAIL] 服务端启动失败, 返回码 {rc}")
        return 1

    print("=" * 58)
    print("  模拟 S7 PLC (S7-1200)")
    print("=" * 58)
    print(f"  端口: {args.port}   机架/槽位: 0/1")
    print()
    print("  DB1 (给煤控制器):")
    print(f"    DB1.DBD{DB1_SETPOINT:<3d}  给煤机设定量  REAL   2.50 t/h   (可写)")
    print(f"    DB1.DBD{DB1_ACTUAL:<3d}  给煤机实际量  REAL   live t/h")
    print(f"    DB1.DBD{DB1_AIR_TEMP:<3d}  一次风温度    REAL   live degC")
    print(f"    DB1.DBW{DB1_SPEED:<3d}  给煤机转速    INT    live rpm")
    print(f"    DB1.DBW{DB1_HOURS:<3d}  累计运行小时  UINT   1234 h")
    print(f"    DB1.DBX{DB1_RUNNING:<3d}.0  运行标志      BOOL   1")
    print(f"    DB1.DBD{DB1_CURRENT:<3d}  给煤机电流    REAL   live A")
    print("  DB2 (二线):")
    print(f"    DB2.DBD{DB2_MILL_CURRENT:<3d}  二线磨机电流  REAL   live A")
    print()
    print("  S7 数据区是大端, 驱动不用配字节序")
    print("  Ctrl+C 退出")
    print()

    try:
        while True:
            _refresh(db1, db2)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n正在关闭 ...")
    finally:
        server.stop()
        server.destroy()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
