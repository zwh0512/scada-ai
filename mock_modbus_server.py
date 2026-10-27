"""
模拟 Modbus TCP 设备 —— 喷煤线给煤控制器
=========================================
用来把 Modbus 驱动跑通。没有真 PLC 的时候, 这是唯一能验证
"多协议抽象不是口号"的办法 —— 点表里同时挂 OPC UA 和 Modbus,
上层一行都不用改。

模拟一台 Modbus TCP 从站 (默认 unit_id=1), 寄存器布局:

holding register (hr):
    0-1   给煤机设定量   float32   2.50  t/h    (可写, 静态)
    2-3   给煤机实际量   float32   live   t/h
    4-5   一次风温度     float32   live   degC
    6     实际转速       int16     live   rpm
    7     运行小时数     uint16    1234   h
    8-9   磨机电流       float32   live   A

input register (ir):
    0-1   一次风压力     float32   live   MPa
    2     设备状态码     uint16    0

coil (co):
    0     启动
    1     停止

【实现要点 —— pymodbus 3.16 的模拟器】
1. SimData 描述一段连续寄存器; `count` 是**重复次数**, 不是寄存器个数。
   要 N 个不同寄存器就写 `count=1` + 长度为 N 的 values 列表。
   (这个语义坑过我一次: count=2 + 2 个值 = 4 个寄存器, 列表被复制了两遍)
2. SimDevice.action 是个 async 回调, 每次寄存器被读写都会调用,
   传入的 registers 就是**实时内存列表**, 就地改就能改到设备里的值。
   用它做出"活的"设备 —— 每个信号是不同周期的正弦波。
3. float32 用 DataType.FLOAT32 会按**大端 (abcd)** 打包,
   所以驱动配 word_order=abcd 就能对上。

跑法:
    python mock_modbus_server.py
    python mock_modbus_server.py --port 5020 --unit-id 1
"""

from __future__ import annotations

import argparse
import asyncio
import math
import struct
import time

from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

# 寄存器布局: (起始寄存器, 寄存器个数)
HR_SETPOINT = 0      # 0-1  给煤机设定量 float32 (静态, 可写)
HR_ACTUAL = 2        # 2-3  给煤机实际量 float32
HR_AIR_TEMP = 4      # 4-5  一次风温度   float32
HR_SPEED = 6         # 6    实际转速     int16
HR_HOURS = 7         # 7    运行小时数   uint16
HR_MILL_CURRENT = 8  # 8-9  磨机电流     float32
HR_COUNT = 10

IR_AIR_PRESS = 0     # 0-1  一次风压力   float32
IR_STATUS = 2        # 2    设备状态码   uint16
IR_COUNT = 3

COIL_START = 0       # 0  启动
COIL_STOP = 1        # 1  停止


def _f32_to_regs(value: float) -> list[int]:
    """float32 -> 2 个寄存器, 大端 (abcd)。"""
    b = struct.pack(">f", float(value))
    return [int.from_bytes(b[0:2], "big"), int.from_bytes(b[2:4], "big")]


def _put_f32(registers: list[int], offset: int, value: float) -> None:
    """把 float32 写进实时寄存器的第 offset 个位置。"""
    hi, lo = _f32_to_regs(value)
    registers[offset] = hi
    registers[offset + 1] = lo


def _live(t: float) -> dict[str, float]:
    """按时间生成"活的"过程数据。

    每个信号用不同周期的正弦波, 免得所有点位一起涨落 —— 那样太假,
    也看不出诊断工具的趋势判断到底准不准。
    """
    return {
        "coal_rate": 1.85 + 0.35 * math.sin(t / 7.0),
        "air_temp": 187.5 + 8.0 * math.sin(t / 11.0 + 1.0),
        "mill_current": 42.5 + 3.5 * math.sin(t / 5.0 + 0.6),
        "speed": 1850 + 45 * math.sin(t / 6.0 + 0.2),
        "air_press": 0.045 + 0.004 * math.sin(t / 9.0 + 2.0),
    }


async def _sim_action(func_code, start_address, address, count, registers, values):
    """SimDevice.action 回调: 每次寄存器被读写都调一次。

    func_code: 3=读holding 4=读input 5=写coil 6=写holding 15=写多coil 16=写多holding
    registers: 该功能区的实时寄存器列表, 就地改就等于改设备里的值
    values:    写请求带过来的值, 读请求是 None
    """
    v = _live(time.time())

    if func_code in (3, 6, 16):
        # holding register: 只刷新"活"的信号, 设定量不动, 否则写进去马上被覆盖
        _put_f32(registers, HR_ACTUAL, v["coal_rate"])
        _put_f32(registers, HR_AIR_TEMP, v["air_temp"])
        registers[HR_SPEED] = int(v["speed"]) & 0xFFFF
        _put_f32(registers, HR_MILL_CURRENT, v["mill_current"])
    elif func_code == 4:
        # input register
        _put_f32(registers, IR_AIR_PRESS, v["air_press"])

    return None


def build_device(unit_id: int) -> SimDevice:
    """按上面的布局造一台设备。"""
    hr_values = [0] * HR_COUNT
    hr_values[HR_SETPOINT:HR_SETPOINT + 2] = _f32_to_regs(2.50)
    hr_values[HR_HOURS] = 1234

    ir_values = [0] * IR_COUNT
    ir_values[IR_STATUS] = 0

    # tuple 形式: (coils, discrete inputs, holding registers, input registers)
    return SimDevice(
        id=unit_id,
        simdata=(
            [SimData(address=0, count=1, values=[False] * 16, datatype=DataType.BITS)],
            [SimData(address=0, count=1, values=[False] * 16, datatype=DataType.BITS)],
            [SimData(address=0, count=1, values=hr_values, datatype=DataType.REGISTERS)],
            [SimData(address=0, count=1, values=ir_values, datatype=DataType.REGISTERS)],
        ),
        action=_sim_action,
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description="模拟 Modbus TCP 给煤控制器")
    parser.add_argument("--port", type=int, default=5020, help="TCP 端口")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--unit-id", type=int, default=1, help="从站地址")
    args = parser.parse_args()

    device = build_device(args.unit_id)
    server = ModbusTcpServer(device, address=(args.host, args.port))

    print("=" * 58)
    print("  模拟 Modbus TCP 设备 (给煤控制器)")
    print("=" * 58)
    print(f"  地址: {args.host}:{args.port}   unit_id: {args.unit_id}")
    print()
    print("  holding register (hr):")
    print(f"    {HR_SETPOINT}-1  给煤机设定量  float32  2.50 t/h   (可写)")
    print(f"    {HR_ACTUAL}-3    给煤机实际量  float32  live t/h")
    print(f"    {HR_AIR_TEMP}-5  一次风温度    float32  live degC")
    print(f"    {HR_SPEED}      实际转速      int16    live rpm")
    print(f"    {HR_HOURS}      运行小时数    uint16   1234 h")
    print(f"    {HR_MILL_CURRENT}-9  磨机电流     float32  live A")
    print("  input register (ir):")
    print(f"    {IR_AIR_PRESS}-1  一次风压力    float32  live MPa")
    print(f"    {IR_STATUS}      设备状态码    uint16   0")
    print("  coil (co):")
    print(f"    {COIL_START}  启动")
    print(f"    {COIL_STOP}   停止")
    print()
    print("  float32 按大端(abcd)存放, 驱动配 word_order=abcd 可对上")
    print("  Ctrl+C 退出")
    print()

    try:
        await server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭 ...")
        await server.shutdown()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
