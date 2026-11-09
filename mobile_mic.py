"""手机麦克风：局域网 HTTP + WebSocket 服务。

手机 App 采集 48 kHz / 单声道 / 16-bit PCM，按 20 ms（960 样本）一帧推到
``ws://<电脑IP>:<端口>/ws?t=<PIN>``。这里负责：

* 起一个轻量 HTTP 服务（安装页、APK 下载、二维码链接）；
* 校验 PIN、完成 WebSocket 握手、解析帧；
* 把 PCM 写进 2 秒环形缓冲，做增益 / 一阶高通 / 噪声门；
* 给音频引擎提供带抖动缓冲的 ``read``，网络抖动时补静音而不是卡住回调。

不依赖第三方库：WebSocket 握手与帧解析都在本文件内实现。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEFAULT_PORT = 8765
MAX_CLIENTS = 3
PIN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def new_pin(length: int = 6) -> str:
    import secrets
    return "".join(secrets.choice(PIN_ALPHABET) for _ in range(length))


def _resample(samples: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or len(samples) == 0:
        return samples
    try:
        import soxr
        return soxr.resample(samples, src, dst).astype(np.float32)
    except Exception:
        # 兜底：线性插值（只在非 48 kHz 手机上走到）
        count = max(1, int(round(len(samples) * dst / src)))
        xp = np.arange(len(samples), dtype=np.float64)
        x = np.linspace(0, len(samples) - 1, count)
        return np.interp(x, xp, samples).astype(np.float32)


# ---------------------------------------------------------------------------
# 手机音频状态
# ---------------------------------------------------------------------------
class MobileMic:
    """环形缓冲 + 轻量处理，音频线程与控制线程共享。"""

    def __init__(self, rate: int = 48000, seconds: float = 2.0):
        self.rate = rate
        self.capacity = int(rate * seconds)
        self._buf = np.zeros(self.capacity, dtype=np.float32)
        self._read = 0
        self._write = 0
        self._count = 0
        self._lock = threading.Lock()

        self.gain_db = 0.0
        self.hpf_hz = 80.0
        self.gate_db = -62.0
        self.jitter_ms = 80.0

        self._hp_state = 0.0
        self._gate_gain = 1.0

        self.packets = 0
        self.bytes = 0
        self.underruns = 0
        self.overflows = 0
        self.level_db = -90.0
        self.last_seen = 0.0
        self.clients = 0
        self.client_label = ""
        self.client_rate = rate

    # -- 控制 -------------------------------------------------------------
    def configure(self, *, gainDb=None, hpfHz=None, gateDb=None, jitterMs=None,
                  gain_db=None, hpf_hz=None, gate_db=None, jitter_ms=None) -> None:
        """同时接受 camelCase（服务端设置）与 snake_case（内部/测试）两种写法。"""
        gain_db = gain_db if gain_db is not None else gainDb
        hpf_hz = hpf_hz if hpf_hz is not None else hpfHz
        gate_db = gate_db if gate_db is not None else gateDb
        jitter_ms = jitter_ms if jitter_ms is not None else jitterMs
        if gain_db is not None:
            self.gain_db = float(np.clip(gain_db, -24.0, 24.0))
        if hpf_hz is not None:
            self.hpf_hz = float(np.clip(hpf_hz, 20.0, 300.0))
        if gate_db is not None:
            self.gate_db = float(np.clip(gate_db, -80.0, -20.0))
        if jitter_ms is not None:
            self.jitter_ms = float(np.clip(jitter_ms, 20.0, 300.0))

    def reset(self) -> None:
        with self._lock:
            self._buf.fill(0)
            self._read = self._write = self._count = 0
        self._hp_state = 0.0
        self._gate_gain = 1.0

    # -- 手机 -> 缓冲 -----------------------------------------------------
    def push_pcm16(self, payload: bytes, sample_rate: int | None = None) -> None:
        if len(payload) < 2:
            return
        usable = len(payload) - (len(payload) % 2)
        pcm = np.frombuffer(payload[:usable], dtype="<i2").astype(np.float32) / 32768.0
        sr = int(sample_rate or self.rate)
        if sr != self.rate:
            pcm = _resample(pcm, sr, self.rate)
        self._process(pcm)
        self.bytes += usable
        self.packets += 1
        self.last_seen = time.time()
        self.client_rate = sr

    def _process(self, x: np.ndarray) -> None:
        if len(x) == 0:
            return
        x = x * float(10.0 ** (self.gain_db / 20.0))

        # 一阶高通：切掉手持噪声与桌面震动
        if self.hpf_hz > 20.0:
            alpha = float(np.exp(-2.0 * np.pi * self.hpf_hz / self.rate))
            out = np.empty_like(x)
            state = self._hp_state
            for i in range(len(x)):
                state = alpha * state + (1.0 - alpha) * x[i]
                out[i] = x[i] - state
            self._hp_state = state
            x = out

        # 噪声门：块 RMS 决定目标增益，再用 attack/release 逐个样本逼近，
        # 避免整块突变带来的咔哒声（快开 ~4 ms，慢关 ~90 ms）
        rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))) + 1e-12)
        self.level_db = 20.0 * np.log10(rms + 1e-9)
        target = 1.0 if self.level_db >= self.gate_db else 0.0
        gate = self._gate_gain
        if not (target >= 1.0 and gate > 0.999):
            coef_open = 1.0 - float(np.exp(-1.0 / (self.rate * 0.004)))
            coef_close = 1.0 - float(np.exp(-1.0 / (self.rate * 0.090)))
            coef = coef_open if target > gate else coef_close
            env = np.empty(len(x), dtype=np.float32)
            for i in range(len(x)):
                gate += (target - gate) * coef
                env[i] = gate
            self._gate_gain = gate
            x = x * env

        x = np.clip(x, -1.0, 1.0).astype(np.float32)
        with self._lock:
            if len(x) >= self.capacity:
                x = x[-self.capacity:]
            overflow = self._count + len(x) - self.capacity
            if overflow > 0:
                self._read = (self._read + overflow) % self.capacity
                self._count -= overflow
                self.overflows += 1
            end = self._write + len(x)
            if end <= self.capacity:
                self._buf[self._write:end] = x
            else:
                first = self.capacity - self._write
                self._buf[self._write:] = x[:first]
                self._buf[:end - self.capacity] = x[first:]
            self._write = end % self.capacity
            self._count += len(x)

    # -- 缓冲 -> 引擎 -----------------------------------------------------
    def read(self, frames: int) -> np.ndarray:
        need = int(round(self.rate * self.jitter_ms / 1000.0))
        with self._lock:
            if self._count < need:
                # 还没攒够抖动缓冲：先给静音，避免开头爆音
                return np.zeros(frames, dtype=np.float32)
            take = min(frames, self._count)
            end = self._read + take
            if end <= self.capacity:
                out = self._buf[self._read:end].copy()
            else:
                first = self.capacity - self._read
                out = np.concatenate([self._buf[self._read:], self._buf[:end - self.capacity]])
            self._read = end % self.capacity
            self._count -= take
            if take < frames:
                self.underruns += 1
                out = np.pad(out, (0, frames - take))
            return out.astype(np.float32, copy=False)

    def metrics(self) -> dict:
        with self._lock:
            buffered = self._count
        age = time.time() - self.last_seen if self.last_seen else 999.0
        return {
            "clients": self.clients,
            "client": self.client_label,
            "rate": self.rate,
            "clientRate": self.client_rate,
            "packets": self.packets,
            "bytes": self.bytes,
            "levelDb": round(self.level_db, 1),
            "bufferMs": round(buffered * 1000.0 / self.rate, 1),
            "underruns": self.underruns,
            "overflows": self.overflows,
            "lastSeen": round(age, 1),
            "gainDb": round(self.gain_db, 1),
            "hpfHz": round(self.hpf_hz, 0),
            "gateDb": round(self.gate_db, 1),
            "jitterMs": round(self.jitter_ms, 0),
        }


# ---------------------------------------------------------------------------
# HTTP / WebSocket
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    server_version = "IntroVoiceMic/1.0"
    protocol_version = "HTTP/1.1"

    # 由 Server 注入
    mic: MobileMic
    token = ""
    apk_path: Path | None = None
    icon_path: Path | None = None
    allowed_clients = 0

    def log_message(self, fmt: str, *args) -> None:  # 安静一点，避免刷日志
        return

    def do_GET(self) -> None:  # noqa: N802
        path, _, query = self.path.partition("?")
        params = {}
        for item in query.split("&"):
            if "=" in item:
                key, value = item.split("=", 1)
                params[key] = value

        if path == "/ws":
            self._handle_ws(params.get("t", ""))
            return
        if path in ("/", "/install"):
            self._serve_html()
            return
        if path == "/app.apk":
            self._serve_file(self.apk_path, "application/vnd.android.package-archive",
                             "IntroVoiceMic.apk")
            return
        if path == "/icon.png":
            self._serve_file(self.icon_path, "image/png", "icon.png")
            return
        if path == "/health":
            body = json.dumps({"ok": True, **self.mic.metrics()}, ensure_ascii=False).encode()
            self._send(200, "application/json; charset=utf-8", body)
            return
        self._send(404, "text/plain; charset=utf-8", "not found".encode())

    # -- 静态 -------------------------------------------------------------
    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path | None, content_type: str, name: str) -> None:
        if path is None or not Path(path).is_file():
            self._send(404, "text/plain; charset=utf-8",
                       "安装包还没生成：请先在电脑端完成 APK 构建。".encode("utf-8"))
            return
        data = Path(path).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.end_headers()
        self.wfile.write(data)

    def _serve_html(self) -> None:
        pin = self.token
        apk_ready = self.apk_path is not None and Path(self.apk_path).is_file()
        download = (
            '<a class="btn" href="/app.apk">下载 Android 安装包</a>'
            if apk_ready else
            '<p class="warn">安装包尚未生成。请让电脑端先构建 APK，或直接使用已安装的 App。</p>'
        )
        html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Intro Voice 手机麦克风</title>
<style>
:root{{color-scheme:dark}}
body{{margin:0;background:#14100e;color:#f5efe6;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;
display:flex;min-height:100vh;align-items:center;justify-content:center;padding:24px}}
.card{{max-width:420px;width:100%;background:#1e1916;border:1px solid #3a322c;border-radius:20px;padding:26px}}
h1{{font-size:22px;margin:0 0 6px}} .sub{{color:#b7ada3;font-size:14px;line-height:1.6;margin:0 0 18px}}
.pin{{font:700 30px/1 "Consolas",monospace;letter-spacing:8px;color:#e8b24a;background:#14100e;
border:1px dashed #5a4a38;border-radius:12px;padding:14px;text-align:center;margin:14px 0}}
.btn{{display:block;text-align:center;background:#e8b24a;color:#241c12;text-decoration:none;
font-weight:700;padding:14px;border-radius:12px;margin-top:14px}}
.warn{{color:#f0a35e;font-size:14px;line-height:1.6}}
ol{{color:#cfc5ba;font-size:14px;line-height:1.9;padding-left:20px}}
code{{color:#e8b24a}}
</style></head><body><div class="card">
<h1>Intro Voice 手机麦克风</h1>
<p class="sub">把手机变成电脑的无线麦克风。手机与电脑需在同一个 Wi-Fi 下。</p>
{download}
<ol>
<li>安装并打开 <b>Intro Voice Mic</b></li>
<li>在电脑端「手机麦克风」页复制 PIN</li>
<li>App 里填电脑 IP 与 PIN，点连接</li>
</ol>
<div class="pin">{pin}</div>
<p class="sub">也可以直接扫描电脑端的连接二维码，自动填入地址。</p>
</div></body></html>"""
        self._send(200, "text/html; charset=utf-8", html.encode("utf-8"))

    # -- WebSocket --------------------------------------------------------
    def _handle_ws(self, token: str) -> None:
        if token.strip().upper() != self.token:
            self._send(403, "text/plain; charset=utf-8", "invalid pin".encode())
            return
        if self.mic.clients >= MAX_CLIENTS:
            self._send(503, "text/plain; charset=utf-8", "too many clients".encode())
            return
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key:
            self._send(400, "text/plain; charset=utf-8", "missing key".encode())
            return
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        try:
            self.wfile.flush()
        except OSError:
            return

        self.close_connection = True
        self.connection.settimeout(15.0)
        self.mic.clients += 1
        label = self.client_address[0]
        rate = self.mic.rate
        try:
            while True:
                frame = self._read_frame()
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:
                    self._send_frame(0x8, b"")
                    break
                if opcode == 0x9:
                    self._send_frame(0xA, payload)
                    continue
                if opcode == 0x1:
                    self._handle_text(payload, label)
                    continue
                if opcode == 0x2:
                    self.mic.push_pcm16(payload, rate)
        except (OSError, ValueError):
            pass
        finally:
            self.mic.clients = max(0, self.mic.clients - 1)
            if self.mic.clients == 0:
                self.mic.client_label = ""
                self.mic.reset()

    def _handle_text(self, payload: bytes, fallback: str) -> None:
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        kind = str(data.get("type") or "")
        if kind == "hello":
            self.mic.client_label = str(data.get("label") or fallback)[:48]
            sr = int(data.get("sr") or self.mic.rate)
            if 8000 <= sr <= 192000:
                self.mic.client_rate = sr
            self.mic.reset()
            self._send_frame(0x1, json.dumps({
                "type": "welcome", "sr": self.mic.rate, "jitterMs": self.mic.jitter_ms,
                "mic": self.mic.metrics(),
            }, ensure_ascii=False).encode("utf-8"))
        elif kind == "settings":
            # 手机端可以直接调电脑侧的增益 / 高通 / 噪声门 / 抖动缓冲
            mapping = {"gainDb": "gain_db", "hpfHz": "hpf_hz",
                       "gateDb": "gate_db", "jitterMs": "jitter_ms"}
            values = {}
            for key, attr in mapping.items():
                if key not in data:
                    continue
                try:
                    values[attr] = float(data[key])
                except (TypeError, ValueError):
                    continue
            if values:
                self.mic.configure(**values)
            self._send_frame(0x1, json.dumps(
                {"type": "settings", "mic": self.mic.metrics()},
                ensure_ascii=False).encode("utf-8"))
        elif kind == "ping":
            self._send_frame(0x1, json.dumps({"type": "pong", "t": data.get("t")}).encode())

    def _read_exact(self, size: int) -> bytes | None:
        data = b""
        while len(data) < size:
            chunk = self.connection.recv(size - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def _read_frame(self) -> tuple[int, bytes] | None:
        head = self._read_exact(2)
        if head is None:
            return None
        b1, b2 = head[0], head[1]
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            raw = self._read_exact(2)
            if raw is None:
                return None
            length = struct.unpack(">H", raw)[0]
        elif length == 127:
            raw = self._read_exact(8)
            if raw is None:
                return None
            length = struct.unpack(">Q", raw)[0]
        if length > 1024 * 1024:
            return None
        mask = self._read_exact(4) if masked else b"\0\0\0\0"
        if mask is None:
            return None
        payload = self._read_exact(length) if length else b""
        if payload is None:
            return None
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytes([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header += bytes([length])
        elif length < 65536:
            header += bytes([126]) + struct.pack(">H", length)
        else:
            header += bytes([127]) + struct.pack(">Q", length)
        try:
            self.connection.sendall(header + payload)
        except OSError:
            pass


class _MicHTTPServer(ThreadingHTTPServer):
    """手机断开、页面关掉时不要把整段回溯刷到日志里。"""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:  # noqa: D102
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError,
                            BrokenPipeError, TimeoutError, OSError)):
            return
        super().handle_error(request, client_address)


class MicServer:
    """HTTP + WebSocket 生命周期。端口占用时自动尝试下一个。"""

    def __init__(self, mic: MobileMic, apk_path: Path | None = None,
                 icon_path: Path | None = None, port: int = DEFAULT_PORT):
        self.mic = mic
        self.apk_path = apk_path
        self.icon_path = icon_path
        self.token = new_pin()
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port = 0
        self.error = ""
        self._requested = port
        self._ip = ""

    def start(self) -> dict:
        if self.httpd is not None:
            return self.status()
        last_error = ""
        for offset in range(0, 6):
            port = self._requested + offset
            try:
                handler = type("BoundHandler", (_Handler,), {
                    "mic": self.mic,
                    "token": self.token,
                    "apk_path": self.apk_path,
                    "icon_path": self.icon_path,
                })
                self.httpd = _MicHTTPServer(("0.0.0.0", port), handler)
                self.httpd.daemon_threads = True
                self.port = int(self.httpd.server_address[1])
                break
            except OSError as exc:
                last_error = str(exc)
                self.httpd = None
        if self.httpd is None:
            self.error = f"端口被占用：{last_error}"
            return self.status()
        self.error = ""
        self._ip = lan_ip()
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self.status()

    def stop(self) -> dict:
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
        self.httpd = None
        self.thread = None
        return self.status()

    def status(self) -> dict:
        ip = self._ip or lan_ip()
        base = f"http://{ip}:{self.port}" if self.httpd else ""
        deep = f"introvoice://mic?host={ip}&port={self.port}&pin={self.token}" if self.httpd else ""
        return {
            "running": self.httpd is not None,
            "ip": ip,
            "port": self.port,
            "pin": self.token,
            "url": base,
            "installUrl": base + "/" if base else "",
            "deepLink": deep,
            "apkPath": str(self.apk_path) if self.apk_path else "",
            "apkReady": bool(self.apk_path and Path(self.apk_path).is_file()),
            "error": self.error,
            "mic": self.mic.metrics(),
        }

    def qr_png(self, content: str, scale: int = 6) -> str:
        """返回 base64 PNG；未安装 segno 时返回空串（界面会退回显示链接）。"""
        try:
            import io
            import segno
            buffer = io.BytesIO()
            segno.make(content, error="m").save(
                buffer, kind="png", scale=scale, border=2,
                dark="#14100E", light="#FFFFFF")
            return base64.b64encode(buffer.getvalue()).decode("ascii")
        except Exception:
            return ""
