from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import queue
import socket
import struct
import threading
import time

import numpy as np

OBS_DEVICE = -100
OBS_LABEL = "OBS: 独立人声（不进入桌面混音）"
OBS_PORT = 28766
OBS_URL = f"http://127.0.0.1:{OBS_PORT}/voice.wav"
RATE = 48000
FRAMES = 960


def wave_header():
    # Float32 PCM WAV with an unspecified length, decoded as a live stream.
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 0xffffffff, b"WAVE",
                       b"fmt ", 16, 3, 1, RATE, RATE * 4, 4, 32, b"data", 0xffffffff)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path != "/voice.wav":
            self.send_error(404)
            return
        output = self.server.output
        packets = queue.Queue(maxsize=12)
        with output.lock:
            if len(output.clients) >= 6:
                self.send_error(503, "Too many audio receivers")
                return
            output.clients.add(packets)
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.connection.settimeout(2)
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(wave_header())
            while not output.stopping.is_set():
                try:
                    packet = packets.get(timeout=.25)
                except queue.Empty:
                    continue
                if packet is None:
                    break
                self.wfile.write(packet)
        except OSError:
            pass
        finally:
            with output.lock:
                output.clients.discard(packets)


class ObsAudioOutput:
    """Clocked local PCM output; no playback endpoint or virtual driver needed."""

    def __init__(self, callback, port=OBS_PORT):
        self.callback = callback
        self.port = port
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.clients = set()
        self.slow_clients = 0
        self.error = None
        self.server = None
        self.server_thread = self.clock_thread = None

    @property
    def active(self):
        return (not self.stopping.is_set() and self.clock_thread is not None
                and self.clock_thread.is_alive() and self.server_thread.is_alive())

    @property
    def receivers(self):
        with self.lock:
            return len(self.clients)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/voice.wav"

    def __enter__(self):
        try:
            self.server = _Server(("127.0.0.1", self.port), _Handler)
        except OSError as error:
            raise RuntimeError(f"OBS 独立音频端口 {self.port} 被系统保留或占用，无法启动直连输出。") from error
        self.server.output = self
        self.server_thread = threading.Thread(target=self.server.serve_forever,
                                             kwargs={"poll_interval": .1}, daemon=True)
        self.clock_thread = threading.Thread(target=self._clock, daemon=True)
        self.server_thread.start()
        self.clock_thread.start()
        return self

    def _clock(self):
        audio = np.zeros((FRAMES, 1), np.float32)
        deadline = time.perf_counter()
        try:
            while not self.stopping.is_set():
                self.callback(audio, FRAMES, None, None)
                packet = audio.astype("<f4", copy=False).tobytes()
                with self.lock:
                    clients = tuple(self.clients)
                for client in clients:
                    try:
                        client.put_nowait(packet)
                    except queue.Full:
                        # A slow receiver reconnects at live time. Never let it
                        # block microphone processing or accumulate old speech.
                        with self.lock:
                            self.clients.discard(client)
                            self.slow_clients += 1
                        while True:
                            try:
                                client.get_nowait()
                            except queue.Empty:
                                break
                        client.put_nowait(None)
                deadline += FRAMES / RATE
                if deadline < time.perf_counter() - .12:
                    deadline = time.perf_counter()
                self.stopping.wait(max(0, deadline - time.perf_counter()))
        except Exception as error:
            self.error = str(error)
            self.stopping.set()

    def __exit__(self, *_args):
        self.stopping.set()
        with self.lock:
            clients = tuple(self.clients)
        for client in clients:
            try:
                client.put_nowait(None)
            except queue.Full:
                pass
        self.server.shutdown()
        self.server.server_close()
        self.clock_thread.join(timeout=1)
        self.server_thread.join(timeout=1)
