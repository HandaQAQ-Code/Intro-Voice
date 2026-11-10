"""手机麦克风：环形缓冲、处理、HTTP/WebSocket 端到端。

运行：runtime\\python.exe tests\\test_mobile_mic.py
"""
from __future__ import annotations

import base64
import json
import os
import queue
import socket
import struct
import sys
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mobile_mic as mm  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  PASS  {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAILED += 1
        print(f"  FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def tone(freq: float, seconds: float, amp: float = 0.25, rate: int = 48000) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def pcm16(samples: np.ndarray) -> bytes:
    return np.clip(samples * 32768, -32768, 32767).astype("<i2").tobytes()


def ws_connect(host: str, port: int, path: str):
    sock = socket.create_connection((host, port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    request = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
               f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
    sock.sendall(request.encode())
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    head = data.split(b"\r\n\r\n")[0].decode("latin-1")
    return sock, head


def send_frame(sock, opcode: int, payload: bytes) -> None:
    mask = os.urandom(4)
    header = bytes([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header += bytes([0x80 | length])
    elif length < 65536:
        header += bytes([0x80 | 126]) + struct.pack(">H", length)
    else:
        header += bytes([0x80 | 127]) + struct.pack(">Q", length)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(header + mask + masked)


def recv_exact(sock, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data


def recv_frame(sock):
    b1, b2 = recv_exact(sock, 2)
    opcode = b1 & 0x0F
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", recv_exact(sock, 8))[0]
    return opcode, recv_exact(sock, length)


def test_processing() -> None:
    print("== 缓冲与处理 ==")
    mic = mm.MobileMic(48000)
    mic.configure(gain_db=0.0, hpf_hz=20.0, gate_db=-80.0, jitter_ms=20.0)
    sig = tone(440, 0.5)
    mic.push_pcm16(pcm16(sig[:480]))          # 只给 10 ms，未填满 20 ms 抖动缓冲
    check("写入后包计数", mic.packets == 1 and mic.bytes > 0, f"{mic.bytes} bytes")
    out = mic.read(960)
    check("抖动未满时补静音", np.allclose(out, 0))
    # 再灌 0.5 秒，抖动缓冲满足后可读到非零
    mic.push_pcm16(pcm16(tone(440, 0.5)))
    out = np.concatenate([mic.read(960) for _ in range(10)])
    rms = float(np.sqrt(np.mean(out ** 2)))
    check("读取到有效音频", rms > 0.1, f"rms={rms:.3f}")

    # 增益 +6 dB 约 2 倍
    mic2 = mm.MobileMic(48000)
    mic2.configure(gain_db=6.0, hpf_hz=20.0, gate_db=-80.0, jitter_ms=20.0)
    mic2.push_pcm16(pcm16(tone(440, 0.4, amp=0.1)))
    mic2.push_pcm16(pcm16(tone(440, 0.4, amp=0.1)))
    got = np.concatenate([mic2.read(960) for _ in range(6)])
    rms = float(np.sqrt(np.mean(got ** 2)))
    check("增益 +6 dB", 0.13 < rms < 0.16, f"rms={rms:.3f}（0.0707×2≈0.141）")

    # 高通：直流输入应被压掉
    mic3 = mm.MobileMic(48000)
    mic3.configure(gain_db=0.0, hpf_hz=120.0, gate_db=-80.0, jitter_ms=20.0)
    mic3.push_pcm16(pcm16(np.full(4800, 0.2, dtype=np.float32)))
    mic3.push_pcm16(pcm16(np.full(4800, 0.2, dtype=np.float32)))
    out = np.concatenate([mic3.read(960) for _ in range(6)])
    check("高通压掉直流", float(np.mean(np.abs(out))) < 0.02,
          f"mean={float(np.mean(np.abs(out))):.4f}")

    # 噪声门：低于阈值应衰减
    mic4 = mm.MobileMic(48000)
    mic4.configure(gain_db=0.0, hpf_hz=20.0, gate_db=-30.0, jitter_ms=20.0)
    quiet = tone(440, 0.4, amp=0.005)
    for _ in range(3):
        mic4.push_pcm16(pcm16(quiet))
    # 门是慢关（~90 ms），先冲掉关闭过程，再看稳态
    tail = np.concatenate([mic4.read(960) for _ in range(24)])[-5760:]
    rms = float(np.sqrt(np.mean(tail ** 2)))
    check("噪声门衰减", rms < 0.0005, f"rms={rms:.6f}")


def test_server() -> None:
    print("== HTTP / WebSocket ==")
    mic = mm.MobileMic(48000)
    mic.configure(gain_db=0.0, hpf_hz=20.0, gate_db=-80.0, jitter_ms=40.0)
    server = mm.MicServer(mic, port=0)
    status = server.start()
    try:
        check("服务启动", status["running"] and status["port"] > 0,
              f"port={status['port']}")
        check("PIN 长度", len(status["pin"]) == 6, status["pin"])
        check("生成深链", status["deepLink"].startswith("introvoice://mic?"), status["deepLink"])

        with urllib.request.urlopen(f"http://127.0.0.1:{status['port']}/health", timeout=5) as resp:
            payload = json.loads(resp.read().decode())
        check("健康检查", resp.status == 200 and payload["ok"] is True)

        with urllib.request.urlopen(f"http://127.0.0.1:{status['port']}/", timeout=5) as resp:
            html = resp.read().decode("utf-8")
        check("安装页含 PIN", status["pin"] in html and "Intro Voice" in html)

        bad, head = ws_connect("127.0.0.1", status["port"], "/ws?t=AAAAAA")
        check("错误 PIN 被拒绝", "403" in head.splitlines()[0], head.splitlines()[0])
        bad.close()

        sock, head = ws_connect("127.0.0.1", status["port"], f"/ws?t={status['pin']}")
        check("握手 101", "101" in head.splitlines()[0], head.splitlines()[0])
        send_frame(sock, 0x1, json.dumps({"type": "hello", "label": "测试手机", "sr": 48000}).encode())
        opcode, payload = recv_frame(sock)
        welcome = json.loads(payload.decode())
        check("收到 welcome", opcode == 0x1 and welcome.get("type") == "welcome", str(welcome))
        check("客户端标签", mic.client_label == "测试手机", mic.client_label)

        frames = 60
        for _ in range(frames):
            send_frame(sock, 0x2, pcm16(tone(440, 0.02, amp=0.2)))
        deadline = time.time() + 3
        while mic.packets < frames and time.time() < deadline:
            time.sleep(0.02)
        check("收到 PCM 帧", mic.packets >= frames, f"packets={mic.packets}")

        audio = np.concatenate([mic.read(960) for _ in range(10)])
        check("服务端音频非静音", float(np.sqrt(np.mean(audio ** 2))) > 0.1,
              f"rms={float(np.sqrt(np.mean(audio ** 2))):.3f}")
        check("电平统计", mic.level_db > -30, f"{mic.level_db} dB")
        check("连接数", mic.clients == 1, str(mic.clients))

        send_frame(sock, 0x8, b"")
        sock.close()
        time.sleep(0.3)
        check("断开后连接数归零", mic.clients == 0, str(mic.clients))
    finally:
        server.stop()
        check("服务停止", not server.status()["running"])


def test_virtual_input() -> None:
    print("== 虚拟输入 ==")
    import mixer_service as ms
    from types import SimpleNamespace

    mic = mm.MobileMic(48000)
    mic.configure(gain_db=0.0, hpf_hz=20.0, gate_db=-80.0, jitter_ms=20.0)
    mic.push_pcm16(pcm16(tone(440, 1.0, amp=0.2)))
    mic.push_pcm16(pcm16(tone(440, 1.0, amp=0.2)))
    engine = SimpleNamespace(stop_event=threading.Event(), mobile=mic, drops=0)
    pending: queue.Queue = queue.Queue(maxsize=10)
    source = ms._VirtualInput(engine, pending, 960, 48000)
    with source:
        time.sleep(0.16)
    check("按块喂入", pending.qsize() >= 2, f"queued={pending.qsize()}")
    _, block = pending.get_nowait()
    check("块形状", block.shape == (960, 1), str(block.shape))
    check("块非静音", float(np.sqrt(np.mean(block ** 2))) > 0.05,
          f"rms={float(np.sqrt(np.mean(block ** 2))):.3f}")


def test_lan_ip() -> None:
    ip = mm.lan_ip()
    check("lan_ip 非空", bool(ip), ip)
    check("lan_ip 不是回环", not ip.startswith("127."), ip)
    # 私网/合法 IPv4 形态
    import re
    check("lan_ip 是合法 IPv4", bool(re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", ip)), ip)
    # lan_ip 必须是纯 socket 实现：不再调用 ipconfig 等外部命令（避免 0x800700e8）
    check("无 ipconfig 依赖", not hasattr(mm, "_default_gateways"))
    check("无 _usable_ip 残留", not hasattr(mm, "_usable_ip"))


def main() -> int:
    test_processing()
    test_server()
    test_virtual_input()
    test_lan_ip()
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    print(f"mobile mic tests @ {ROOT}")
    code = main()
    print(f"\n结果：{PASSED} 通过 / {FAILED} 失败")
    sys.exit(code)
