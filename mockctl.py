"""
mock server 控制工具 —— 测试脚本和诊断程序共用
=============================================
项目里有三个模拟设备进程(mock_opcua_server / mock_modbus_server / mock_s7_server),
测试脚本和 llm_diagnose.py 都要起它们、然后收掉。逻辑放一处, 免得抄四遍。

【一个坑】
起 mock 前必须**等端口真正释放**。上一轮的 server 还在退出中时, 端口照样能连上,
新起的 server 绑不上去, 测试就会连到那个"正在死"的 server —— 偶发失败就是这么来的。
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable


def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """端口现在能不能连上。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_port(host: str, port: int, timeout: float = 20.0) -> bool:
    """等端口起来。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(host, port, timeout=0.3):
            return True
        time.sleep(0.3)
    return False


def wait_port_closed(host: str, port: int, timeout: float = 8.0) -> bool:
    """等端口真正释放(上一个进程可能还在退出中)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_open(host, port, timeout=0.3):
            return True
        time.sleep(0.3)
    return False


def start_server(script: str, port: int, *args: str) -> subprocess.Popen | None:
    """起一个 mock server。

    端口上已经有在跑的(比如你自己在终端起了 mock)就直接复用, 不重复起。
    返回 None 表示复用了现成的。
    """
    if port_open("127.0.0.1", port):
        wait_port_closed("127.0.0.1", port, timeout=8.0)
        if port_open("127.0.0.1", port):
            print(f"      (端口 {port} 被占用, 复用现成的 server)")
            return None

    proc = subprocess.Popen(
        [PY, str(HERE / script), *args],
        cwd=str(HERE),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        if port_open("127.0.0.1", port, timeout=0.2):
            return proc
        time.sleep(0.25)

    proc.terminate()
    raise RuntimeError(f"mock server {script} 启动失败, 端口 {port} 没起来")


def stop_all(procs: list[subprocess.Popen] | None) -> None:
    """收掉所有起的进程。"""
    for p in procs or []:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
