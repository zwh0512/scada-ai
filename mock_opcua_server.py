"""
Mock OPC UA Server —— 本地模拟一台工业设备
=========================================
用途:
  没有真实 PLC 也能开发和调试 OPC UA 客户端, 不用等现场。
  模拟对象参考钢铁喷煤系统: 磨机电流 / 一次风温度 / 喷煤量 / 运行状态。

启动:
  python mock_opcua_server.py                 # 默认端口 4840
  python mock_opcua_server.py --port 4841     # 换端口

客户端连:
  opc.tcp://localhost:4840/freeopcua/server/

用 UaExpert 连的话 (免费, Unified Automation 出品):
  1. Servers -> 右键 -> Add Server -> Custom Discovery -> Double click to Add Server
  2. 输入上面那个 URL
  3. SecurityPolicy 选 None-None, 身份认证选 Anonymous
  4. 连接后第一次会弹证书窗口, 点 Trust Server Certificate
  5. 展开地址空间, 找到 Plant1 -> Line1 -> 就能看到 4 个点位

安全说明:
  本机 mock 只开 NoSecurity + 匿名, **仅用于本地开发**。
  现场生产必须是 Basic256Sha256 + 用户名密码, 证书进信任列表。

接单场景的改法 (3 处):
  1. 删掉 simulate_* 那几个函数 (不用假数据)
  2. 改成 asyncua 的 Server 端连真实 PLC (但现场通常不让你起 server)
  3. 其实真实做法: 这台 mock server 换成现场的 OPC UA 服务器地址,
     客户端代码一行都不用改
"""
import argparse
import asyncio
import math
import random
import sys
from datetime import datetime

from asyncua import Server, ua

# Windows 控制台默认 GBK, 强制 UTF-8, 否则中文输出会乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------- 配置

DEFAULT_PORT = 4840
NAMESPACE_URI = "http://example.org/scada"

# 点位定义: (NodeId 字符串, 浏览名, 类型, 初值, 单位)
# 格式跟阶段 2 的点表 YAML 对应, 这里只是先内置一份
POINTS = [
    ("Plant1.Line1.MillCurrent",  "MillCurrent",  "float", 40.0, "A"),
    ("Plant1.Line1.AirTemp",      "AirTemp",      "float", 180.0, "degC"),
    ("Plant1.Line1.CoalRate",     "CoalRate",     "float", 2.0, "t/h"),
    ("Plant1.Line1.RunState",     "RunState",     "bool",  True, ""),
    ("Plant1.Line2.MillCurrent",  "MillCurrent",  "float", 38.0, "A"),
    ("Plant1.Line2.AirTemp",      "AirTemp",      "float", 195.0, "degC"),
    ("Plant1.Line2.CoalRate",     "CoalRate",     "float", 1.8, "t/h"),
    ("Plant1.Line2.RunState",     "RunState",     "bool",  True, ""),
]


# ---------------------------------------------------------------- 仿真

def simulate_mill_current(base: float, tick: int) -> float:
    """磨机电流: 基值 + 正弦波动 + 随机噪声, 偶尔尖峰 (模拟堵料)"""
    wave = math.sin(tick / 8.0) * 2.5
    noise = random.uniform(-0.4, 0.4)
    spike = 12.0 if tick % 120 in (45, 46, 47) else 0.0
    return round(base + wave + noise + spike, 2)


def simulate_air_temp(base: float, tick: int) -> float:
    """一次风温度: 缓慢正弦漂移"""
    return round(base + math.sin(tick / 30.0) * 12.0, 2)


def simulate_coal_rate(base: float, tick: int) -> float:
    """喷煤量: 小幅随机游走, 夹在 0.5 ~ 4.5 之间"""
    drift = random.uniform(-0.08, 0.08)
    value = base + drift
    if not (0.5 <= value <= 4.5):
        value = base
    return round(value, 3)


# ---------------------------------------------------------------- 服务端

async def build_server(endpoint: str):
    """建地址空间, 返回 (server, {nodeid字符串: node对象}, 命名空间索引)"""
    server = Server()
    await server.init()
    server.set_endpoint(endpoint)
    server.set_server_name("MockScadaServer")

    # 本地 mock 只开放 NoSecurity + 匿名, 省掉证书配置, 也避免启动告警
    # 现场绝不能这么配
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity])

    idx = await server.register_namespace(NAMESPACE_URI)
    nodes = {}

    # 注意: 浏览名必须显式指定命名空间, 否则落在 namespace 0,
    # 客户端用 "2:Plant1" 这种路径就找不到 (会报 BadNoMatch)。
    # NodeId 和 QualifiedName 是两个独立的东西, 这里都要给对。

    # Plant1 -> Line1 / Line2 两层结构, 模拟真实设备层级
    plant = await server.nodes.objects.add_object(
        ua.NodeId("Plant1", idx), ua.QualifiedName("Plant1", idx)
    )

    for line in ("Line1", "Line2"):
        line_obj = await plant.add_object(
            ua.NodeId(f"Plant1.{line}", idx), ua.QualifiedName(line, idx)
        )
        for nodeid_str, bname, dtype, init, unit in POINTS:
            if not nodeid_str.startswith(f"Plant1.{line}."):
                continue
            node = await line_obj.add_variable(
                ua.NodeId(nodeid_str, idx), ua.QualifiedName(bname, idx), init
            )
            await node.set_writable()      # 允许客户端写入, 方便测写操作
            nodes[nodeid_str] = node

    # 一个只读的时间戳变量, 用来测订阅
    ts_node = await plant.add_variable(
        ua.NodeId("Plant1.ServerTime", idx),
        ua.QualifiedName("ServerTime", idx),
        datetime.now().isoformat(),
    )
    nodes["Plant1.ServerTime"] = ts_node

    return server, nodes, idx


async def main() -> None:
    parser = argparse.ArgumentParser(description="Mock OPC UA Server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口, 默认 4840")
    parser.add_argument("--interval", type=float, default=1.0, help="仿真步长秒, 默认 1.0")
    args = parser.parse_args()

    endpoint = f"opc.tcp://0.0.0.0:{args.port}/freeopcua/server/"

    print("=" * 60)
    print("  Mock OPC UA Server")
    print("=" * 60)
    print()
    print(f"  端点地址 : {endpoint}")
    print(f"  命名空间 : {NAMESPACE_URI}")
    print(f"  点位数量 : {len(POINTS) + 1}")
    print(f"  仿真步长 : {args.interval}s")
    print(f"  安全策略 : NoSecurity + 匿名  (仅限本地开发)")
    print()
    print("  客户端连这个地址:")
    print(f"    {endpoint.replace('0.0.0.0', 'localhost')}")
    print()
    print("  按 Ctrl+C 停止")
    print()

    server, nodes, idx = await build_server(endpoint)
    tick = 0

    async with server:
        print("[server] 已启动, 开始仿真...")
        print()
        print("  " + "-" * 56)
        print("  时间              L1电流     L1风温     L2电流     L2风温")
        print("  " + "-" * 56)

        try:
            while True:
                now = datetime.now().strftime("%H:%M:%S")

                base_map = {
                    "Line1": {"mill": 40.0, "temp": 180.0, "coal": 2.0},
                    "Line2": {"mill": 38.0, "temp": 195.0, "coal": 1.8},
                }
                snapshot = {}
                for line, base in base_map.items():
                    mill = simulate_mill_current(base["mill"], tick)
                    temp = simulate_air_temp(base["temp"], tick)
                    coal = simulate_coal_rate(base["coal"], tick)
                    snapshot[line] = (mill, temp, coal)

                    await nodes[f"Plant1.{line}.MillCurrent"].write_value(mill)
                    await nodes[f"Plant1.{line}.AirTemp"].write_value(temp)
                    await nodes[f"Plant1.{line}.CoalRate"].write_value(coal)

                await nodes["Plant1.ServerTime"].write_value(datetime.now().isoformat())

                l1m, l1t, _ = snapshot["Line1"]
                l2m, l2t, _ = snapshot["Line2"]
                print(f"  {now}     {l1m:7.2f} A {l1t:7.2f} C {l2m:7.2f} A {l2t:7.2f} C")

                tick += 1
                await asyncio.sleep(args.interval)

        except (KeyboardInterrupt, asyncio.CancelledError):
            print()
            print("[server] 收到停止信号, 关闭...")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
