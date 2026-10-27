"""
check_client.py —— OPC UA 客户端验证脚本
=======================================
用来验证 mock server 跑起来了, 同时是你理解 OPC UA 客户端的最小样本。

按顺序做了 5 件事, 每一件对应 OPC UA 的一个核心概念:
  1. connect      -> 连接与端点
  2. browse       -> 地址空间 (Information Model)
  3. read_value   -> 读
  4. write_value  -> 写
  5. subscribe    -> 订阅推送 (PubSub), 现场几千个点位必须用这个

先启动 mock server (另开一个终端):
  python mock_opcua_server.py

再跑本脚本:
  python check_client.py
"""
import asyncio
import sys
from datetime import datetime

from asyncua import Client, ua

# Windows 控制台默认 GBK, 强制 UTF-8, 否则中文输出会乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ENDPOINT = "opc.tcp://localhost:4840/freeopcua/server/"
NS_URI = "http://example.org/scada"

# 标准 NodeId: Server 的状态节点, 常用来确认连的是不是真服务器
NODEID_SERVER_STATUS = "i=2256"

# 要订阅的点位
WATCH = [
    "Plant1.Line1.MillCurrent",
    "Plant1.Line1.AirTemp",
    "Plant1.Line2.CoalRate",
]


def nid(idx: int, name: str) -> str:
    """NodeId 字符串标准写法是 ns=<命名空间>;s=<标识符>。

    别写成 "2:Plant1" —— 那是浏览路径(BrowsePath)的写法, 给 get_node 用会报
    "not enough values to unpack"。这个坑我替你踩过了。
    """
    return f"ns={idx};s={name}"


class SubHandler:
    """asyncua 订阅回调: 服务端一有变化就调这个"""

    def datachange_notification(self, node, val, data):
        name = node.nodeid.to_string()
        # 变化回调在服务端推送时触发, 这里只打印
        short = name.split("s=")[-1]
        print(f"    [推送] {short:32s} = {val}")

    def event_notification(self, event):
        print(f"    [事件] {event}")


async def step1_connect(client):
    print()
    print("[1/5] 连接服务器 ...")
    print(f"      OK - 已连上 {ENDPOINT}")
    status = await client.get_node(NODEID_SERVER_STATUS).read_value()
    print(f"      服务器状态: {status}")


async def step2_browse(client, idx):
    print()
    print("[2/5] 浏览地址空间 ...")
    # 2a. 按 NodeId 直接取节点 (最可靠, 不依赖浏览名)
    objects = client.nodes.objects
    plant = client.get_node(ua.NodeId("Plant1", idx))
    print(f"      根节点: {objects.nodeid.to_string()}")
    print(f"      找到:   {plant.nodeid.to_string()}  浏览名={await plant.read_browse_name()}")

    # 2b. 用浏览路径走一层 (2:Plant1 = 命名空间2, 浏览名 Plant1)
    plant2 = await objects.get_child(f"{idx}:Plant1")
    print(f"      路径访问 OK: {plant2.nodeid.to_string()}")

    # 2c. 列出 Plant1 下的全部子节点
    children = await plant.get_children()
    print(f"      Plant1 有 {len(children)} 个直接子节点:")
    for c in children:
        bname = await c.read_browse_name()
        print(f"        - {bname.Name:16s} {c.nodeid.to_string()}")

    # 2d. 再下一层, 看 Line1 里的点位
    line1 = await plant.get_child(f"{idx}:Line1")
    leaves = await line1.get_children()
    print(f"      Line1 下有 {len(leaves)} 个点位:")
    for c in leaves:
        bname = await c.read_browse_name()
        val = await c.read_value()
        print(f"        - {bname.Name:16s} = {val}")
    return idx


async def step3_read(client, idx):
    print()
    print("[3/5] 读单个点位 ...")
    node = client.get_node(nid(idx, WATCH[0]))
    val = await node.read_value()
    print(f"      {WATCH[0]} = {val} A")

    # 批量读: 一次请求读多个, 现场上千点时用这个, 别一个个 read
    nodes = [client.get_node(nid(idx, t)) for t in WATCH]
    values = await client.read_values(nodes)
    print(f"      批量读 {len(values)} 个点位:")
    for tag, v in zip(WATCH, values):
        print(f"        {tag:32s} = {v}")
    return val


async def step4_write(client, idx):
    print()
    print("[4/5] 写点位 (测写操作) ...")
    node = client.get_node(nid(idx, "Plant1.Line1.CoalRate"))
    old = await node.read_value()
    new = 3.77
    await node.write_value(new)
    check = await node.read_value()
    print(f"      写入前 = {old}, 写入后 = {check}")
    print("      OK - 写入生效" if abs(check - new) < 1e-6 else "      [FAIL] 写入没生效")
    # 改回去, 不污染仿真
    await node.write_value(old)


async def step5_subscribe(client, idx, seconds=8):
    print()
    print(f"[5/5] 订阅 {len(WATCH)} 个点位, 观察 {seconds} 秒 ...")
    print("      (订阅 = 服务端主动推送, 不需要客户端反复轮询)")
    print()

    handler = SubHandler()
    # 500ms 采样周期
    sub = await client.create_subscription(500, handler)
    for t in WATCH:
        await sub.subscribe_data_change(client.get_node(nid(idx, t)))

    start = datetime.now()
    while (datetime.now() - start).total_seconds() < seconds:
        await asyncio.sleep(0.5)

    await sub.delete()
    print()
    print("      订阅已删除")


async def main():
    print("=" * 60)
    print("  OPC UA 客户端验证")
    print("=" * 60)
    print(f"  端点: {ENDPOINT}")

    # 关键坑: Client.__aenter__ 里就 await self.connect(), 也就是说
    # 用 `async with Client(...)` 的写法, 一进 with 块就已经连上了,
    # 后面再改 session_timeout / 设证书全都已经太晚。
    # 所以这里手动 new + 配置 + connect。
    client = Client(url=ENDPOINT)

    # mock server 的会话上限是 10 分钟, 客户端默认请求 1 小时,
    # 对齐一下就不会刷 "Requested session timeout" 告警
    client.session_timeout = 600000

    # 不调用 set_security_string —— 那个方法要求 4 段式参数
    # (Policy,Mode,cert,key), 传 "Anonymous" 会直接报错。
    # mock server 只开 NoSecurity, 客户端默认就是无安全 + 匿名连接。
    # 现场要加密时改用:
    #   await client.set_security_string(
    #       "Basic256Sha256,SignAndEncrypt,client.pem,client_key.pem,server_cert.pem")
    #   await client.set_user("user")
    #   await client.set_password("pwd")

    try:
        await client.connect()
        idx = await client.get_namespace_index(NS_URI)
        print(f"      命名空间索引: {idx}")
        await step1_connect(client)
        await step2_browse(client, idx)
        await step3_read(client, idx)
        await step4_write(client, idx)
        await step5_subscribe(client, idx)
    finally:
        await client.disconnect()

    print()
    print("=" * 60)
    print("  全部通过")
    print("=" * 60)
    print()
    print("下一步:")
    print("  1. 用 UaExpert 连同一个地址, 自己拖点位看")
    print("  2. 把这个客户端的代码搬进 drivers/opcua_driver.py")
    print("  3. 加 Modbus / S7 驱动, 套同一个 Driver 抽象层")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ConnectionRefusedError:
        print()
        print("[FAIL] 连不上服务器 —— mock_opcua_server.py 是不是还没启动?")
        sys.exit(1)
    except Exception as exc:
        print()
        print(f"[FAIL] {type(exc).__name__}: {exc}")
        sys.exit(1)
