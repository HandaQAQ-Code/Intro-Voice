package com.introvoice.mic;

import android.content.Context;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.media.audiofx.AcousticEchoCanceler;
import android.media.audiofx.AutomaticGainControl;
import android.media.audiofx.NoiseSuppressor;
import android.os.Build;
import android.os.SystemClock;
import android.util.Log;

import org.json.JSONObject;

import java.io.IOException;
import java.util.Arrays;
import java.util.Locale;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;
import okio.ByteString;

/**
 * 采集 + 推流引擎：AudioRecord 读 16-bit PCM，按帧经 WebSocket 送到电脑端。
 * 单例 + 后台线程，Activity 只负责读 {@link #snapshot()} 和下发配置。
 */
public final class MicEngine {
    private static final String TAG = "IntroVoiceMic";

    public static final MicEngine I = new MicEngine();

    public enum State { IDLE, CONNECTING, LIVE, ERROR }

    /** 采集参数。 */
    public static final class Config {
        public String host = "";
        public int port = 8765;
        public String pin = "";
        public int sampleRate = 48000;
        public int frameMs = 20;
        public int source = MediaRecorder.AudioSource.VOICE_COMMUNICATION;
        public float gainDb = 0f;
        public boolean aec = false;
        public boolean ns = true;
        public boolean agc = false;

        public Config copy() {
            Config c = new Config();
            c.host = host;
            c.port = port;
            c.pin = pin;
            c.sampleRate = sampleRate;
            c.frameMs = frameMs;
            c.source = source;
            c.gainDb = gainDb;
            c.aec = aec;
            c.ns = ns;
            c.agc = agc;
            return c;
        }
    }

    /** 给界面轮询的一份快照，全部是值拷贝。 */
    public static final class Stats {
        public State state = State.IDLE;
        public String message = "未连接";
        public float levelDb = -90f;
        public float peakDb = -90f;
        public long frames;
        public long bytes;
        public long dropped;
        public int kbps;
        public int rttMs = -1;
        public int serverRate;
        public int jitterMs;
        public long uptimeMs;
        public String device = "";
    }

    private final OkHttpClient client = new OkHttpClient.Builder()
            .connectTimeout(6, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.MILLISECONDS)
            .writeTimeout(8, TimeUnit.SECONDS)
            .retryOnConnectionFailure(false)
            .build();

    private volatile Config cfg = new Config();
    private volatile boolean running;
    private volatile State state = State.IDLE;
    private volatile String message = "未连接";
    private volatile float levelDb = -90f;
    private volatile float peakDb = -90f;
    private volatile long frames;
    private volatile long bytes;
    private volatile long dropped;
    private volatile long startedAt;
    private volatile long lastFrameAt;
    private volatile int rttMs = -1;
    private volatile int serverRate;
    private volatile int jitterMs;
    private volatile String deviceName = "";
    private volatile boolean wsReady;

    private volatile WebSocket socket;
    private volatile AudioRecord recorder;
    private volatile Thread worker;
    private AcousticEchoCanceler aec;
    private NoiseSuppressor ns;
    private AutomaticGainControl agc;

    private MicEngine() {
    }

    public Config config() {
        return cfg.copy();
    }

    public boolean isRunning() {
        return running;
    }

    public State state() {
        return state;
    }

    public synchronized void start(Config config) {
        if (running) {
            stop();
        }
        cfg = config.copy();
        running = true;
        frames = 0;
        bytes = 0;
        dropped = 0;
        rttMs = -1;
        levelDb = -90f;
        peakDb = -90f;
        serverRate = 0;
        jitterMs = 0;
        startedAt = System.currentTimeMillis();
        lastFrameAt = 0;
        setState(State.CONNECTING, "正在连接 " + config.host + ":" + config.port);
        Thread t = new Thread(this::session, "mic-session");
        t.setDaemon(true);
        worker = t;
        t.start();
    }

    public synchronized void stop() {
        boolean wasRunning = running;
        running = false;
        wsReady = false;
        WebSocket s = socket;
        socket = null;
        if (s != null) {
            try {
                s.close(1000, "bye");
            } catch (Exception ignored) {
            }
        }
        AudioRecord r = recorder;
        if (r != null) {
            try {
                r.stop();
            } catch (Exception ignored) {
            }
        }
        Thread t = worker;
        worker = null;
        if (t != null && t != Thread.currentThread()) {
            t.interrupt();
        }
        if (wasRunning) {
            setState(State.IDLE, "已断开");
        }
        levelDb = -90f;
        peakDb = -90f;
    }

    /** 手机端本地增益，实时生效。 */
    public void setGainDb(float db) {
        Config c = cfg.copy();
        c.gainDb = db;
        cfg = c;
    }

    /** 把电脑端 DSP 参数推给电脑（不用重连）。 */
    public void sendRemoteDsp(float gainDb, float hpfHz, float gateDb, float jitterMs) {
        JSONObject o = new JSONObject();
        try {
            o.put("type", "settings");
            o.put("gainDb", Math.round(gainDb * 10) / 10.0);
            o.put("hpfHz", Math.round(hpfHz));
            o.put("gateDb", Math.round(gateDb * 10) / 10.0);
            o.put("jitterMs", Math.round(jitterMs));
        } catch (Exception ignored) {
        }
        send(o.toString());
    }

    public Stats snapshot() {
        Stats s = new Stats();
        s.state = state;
        s.message = message;
        s.levelDb = levelDb;
        s.peakDb = peakDb;
        s.frames = frames;
        s.bytes = bytes;
        s.dropped = dropped;
        long elapsed = startedAt > 0 && running ? System.currentTimeMillis() - startedAt : 0;
        s.uptimeMs = elapsed;
        s.kbps = elapsed > 400 ? (int) (bytes * 8L / elapsed) : 0;
        s.rttMs = rttMs;
        s.serverRate = serverRate;
        s.jitterMs = jitterMs;
        s.device = deviceName;
        return s;
    }

    /** 统计值给服务通知用。 */
    public String statusLine() {
        Stats s = snapshot();
        return String.format(Locale.US, "%s · %d kbps · %d ms", stateText(s.state), s.kbps, s.rttMs);
    }

    public static String stateText(State st) {
        switch (st) {
            case LIVE:
                return "已连接";
            case CONNECTING:
                return "连接中";
            case ERROR:
                return "异常";
            default:
                return "未连接";
        }
    }

    // ---------------------------------------------------------------- 会话

    private void session() {
        int attempt = 0;
        while (running) {
            try {
                if (!openSocket()) {
                    if (!running) {
                        break;
                    }
                    attempt++;
                    setState(State.CONNECTING, "连接失败，第 " + attempt + " 次重试…");
                    sleep(backoff(attempt));
                    continue;
                }
                attempt = 0;
                capture();
            } catch (Throwable t) {
                if (running) {
                    Log.w(TAG, "session error", t);
                    setState(State.ERROR, friendly(t));
                }
            } finally {
                closeSocket();
                releaseRecorder();
            }
            if (!running) {
                break;
            }
            setState(State.CONNECTING, "连接中断，正在重连…");
            sleep(backoff(++attempt));
        }
        running = false;
        wsReady = false;
        if (state != State.ERROR) {
            setState(State.IDLE, "已断开");
        }
    }

    private long backoff(int attempt) {
        return Math.min(5000L, 600L * Math.max(1, attempt));
    }

    private void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    private boolean openSocket() throws InterruptedException {
        Config c = cfg;
        String url = String.format(Locale.US, "ws://%s:%d/ws?t=%s", c.host, c.port, c.pin);
        CountDownLatch latch = new CountDownLatch(1);
        final boolean[] opened = {false};
        final String[] failure = {""};
        WebSocket w = client.newWebSocket(new Request.Builder().url(url).build(),
                new WebSocketListener() {
                    @Override
                    public void onOpen(WebSocket webSocket, Response response) {
                        wsReady = true;
                        opened[0] = true;
                        hello(webSocket);
                        setState(State.LIVE, "已连接");
                        latch.countDown();
                    }

                    @Override
                    public void onMessage(WebSocket webSocket, String text) {
                        handleText(text);
                    }

                    @Override
                    public void onFailure(WebSocket webSocket, Throwable t, Response response) {
                        wsReady = false;
                        failure[0] = String.valueOf(t.getMessage());
                        latch.countDown();
                    }

                    @Override
                    public void onClosed(WebSocket webSocket, int code, String reason) {
                        wsReady = false;
                        latch.countDown();
                    }
                });
        socket = w;
        boolean success = latch.await(6, TimeUnit.SECONDS) && opened[0];
        if (!success) {
            setState(State.ERROR, describeFailure(failure[0]));
            closeSocket();
        }
        return success;
    }

    private void hello(WebSocket webSocket) {
        try {
            JSONObject o = new JSONObject();
            o.put("type", "hello");
            o.put("label", deviceName);
            o.put("sr", cfg.sampleRate);
            webSocket.send(o.toString());
        } catch (Exception ignored) {
        }
    }

    private void handleText(String text) {
        try {
            JSONObject o = new JSONObject(text);
            String type = o.optString("type");
            if ("welcome".equals(type)) {
                serverRate = o.optInt("sr", 0);
                jitterMs = o.optInt("jitterMs", 0);
                JSONObject mic = o.optJSONObject("mic");
                if (mic != null) {
                    jitterMs = mic.optInt("jitterMs", jitterMs);
                    if (serverRate == 0) {
                        serverRate = mic.optInt("rate", 0);
                    }
                }
            } else if ("pong".equals(type)) {
                long sent = o.optLong("t", 0L);
                if (sent > 0) {
                    rttMs = (int) Math.max(0, SystemClock.elapsedRealtime() - sent);
                }
            }
        } catch (Exception ignored) {
        }
    }

    private void sendPing() {
        JSONObject o = new JSONObject();
        try {
            o.put("type", "ping");
            o.put("t", SystemClock.elapsedRealtime());
        } catch (Exception ignored) {
        }
        send(o.toString());
    }

    private void send(String text) {
        WebSocket w = socket;
        if (w != null && wsReady) {
            try {
                w.send(text);
            } catch (Exception ignored) {
            }
        }
    }

    // ------------------------------------------------------------ 采集循环

    private void capture() throws IOException {
        Config c = cfg;
        int frameSamples = Math.max(64, c.sampleRate * c.frameMs / 1000);
        int minBuf = AudioRecord.getMinBufferSize(c.sampleRate,
                android.media.AudioFormat.CHANNEL_IN_MONO,
                android.media.AudioFormat.ENCODING_PCM_16BIT);
        if (minBuf <= 0) {
            throw new IOException("手机不支持 " + c.sampleRate + " Hz 采样，请换一个采样率");
        }
        int bufBytes = Math.max(minBuf, frameSamples * 2 * 4);
        AudioRecord rec;
        try {
            rec = new AudioRecord(c.source, c.sampleRate,
                    android.media.AudioFormat.CHANNEL_IN_MONO,
                    android.media.AudioFormat.ENCODING_PCM_16BIT, bufBytes);
        } catch (Exception e) {
            throw new IOException("无法创建录音（可能被其他应用占用）");
        }
        if (rec.getState() != AudioRecord.STATE_INITIALIZED) {
            rec.release();
            throw new IOException("麦克风不可用，请检查权限或是否被占用");
        }
        recorder = rec;
        enableEffects(rec, c);
        short[] pcm = new short[frameSamples];
        byte[] out = new byte[frameSamples * 2];
        try {
            rec.startRecording();
            long lastPing = 0;
            while (running && wsReady) {
                int n = rec.read(pcm, 0, frameSamples);
                if (n <= 0) {
                    if (n == AudioRecord.ERROR_DEAD_OBJECT) {
                        throw new IOException("录音设备已断开，请重新连接");
                    }
                    continue;
                }
                float gain = (float) Math.pow(10.0, cfg.gainDb / 20.0);
                double sum = 0;
                for (int i = 0; i < n; i++) {
                    int v = Math.round(pcm[i] * gain);
                    if (v > 32767) {
                        v = 32767;
                    } else if (v < -32768) {
                        v = -32768;
                    }
                    sum += (double) v * v;
                    out[i * 2] = (byte) (v & 0xFF);
                    out[i * 2 + 1] = (byte) ((v >> 8) & 0xFF);
                }
                double rms = Math.sqrt(sum / Math.max(1, n)) / 32768.0;
                updateLevel(rms);

                WebSocket w = socket;
                if (w != null) {
                    if (w.queueSize() > 64 * 1024) {
                        dropped++;
                    } else if (w.send(ByteString.of(Arrays.copyOf(out, n * 2)))) {
                        frames++;
                        bytes += n * 2L;
                        lastFrameAt = System.currentTimeMillis();
                    }
                }
                long now = SystemClock.elapsedRealtime();
                if (now - lastPing > 2000) {
                    lastPing = now;
                    sendPing();
                }
            }
        } finally {
            try {
                rec.stop();
            } catch (Exception ignored) {
            }
        }
    }

    private void updateLevel(double rms) {
        float db = (float) (20.0 * Math.log10(rms + 1e-7));
        float current = levelDb;
        float next = db > current ? db : Math.max(db, current - 1.6f);
        levelDb = Math.max(-90f, Math.min(0f, next));
        float p = peakDb;
        if (levelDb >= p) {
            peakDb = levelDb;
        } else {
            peakDb = Math.max(levelDb, p - 0.25f);
        }
    }

    private void enableEffects(AudioRecord rec, Config c) {
        releaseEffects();
        int session = rec.getAudioSessionId();
        try {
            if (c.aec && AcousticEchoCanceler.isAvailable()) {
                aec = AcousticEchoCanceler.create(session);
                if (aec != null) {
                    aec.setEnabled(true);
                }
            }
            if (c.ns && NoiseSuppressor.isAvailable()) {
                ns = NoiseSuppressor.create(session);
                if (ns != null) {
                    ns.setEnabled(true);
                }
            }
            if (c.agc && AutomaticGainControl.isAvailable()) {
                agc = AutomaticGainControl.create(session);
                if (agc != null) {
                    agc.setEnabled(true);
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "effect setup failed", e);
        }
    }

    private void releaseEffects() {
        try {
            if (aec != null) {
                aec.release();
            }
        } catch (Exception ignored) {
        }
        try {
            if (ns != null) {
                ns.release();
            }
        } catch (Exception ignored) {
        }
        try {
            if (agc != null) {
                agc.release();
            }
        } catch (Exception ignored) {
        }
        aec = null;
        ns = null;
        agc = null;
    }

    private void releaseRecorder() {
        releaseEffects();
        AudioRecord r = recorder;
        recorder = null;
        if (r != null) {
            try {
                r.release();
            } catch (Exception ignored) {
            }
        }
    }

    private void closeSocket() {
        wsReady = false;
        WebSocket w = socket;
        socket = null;
        if (w != null) {
            try {
                w.close(1000, "bye");
            } catch (Exception ignored) {
            }
        }
    }

    private void setState(State st, String msg) {
        state = st;
        message = msg;
    }

    private static String friendly(Throwable t) {
        String m = t.getMessage() == null ? "" : t.getMessage();
        if (t instanceof SecurityException) {
            return "没有麦克风权限，请在系统设置里允许";
        }
        if (t instanceof IOException) {
            return m.isEmpty() ? "网络或麦克风异常" : m;
        }
        return m.isEmpty() ? "未知错误" : m;
    }

    private static String describeFailure(String raw) {
        String m = raw == null ? "" : raw.toLowerCase(Locale.US);
        if (m.contains("403") || m.contains("forbidden")) {
            return "PIN 不正确，请对照电脑端显示的 PIN";
        }
        if (m.contains("503")) {
            return "电脑端连接数已满，请先在电脑上断开其它手机";
        }
        if (m.contains("timeout") || m.contains("timed out")) {
            return "连接超时：确认手机和电脑在同一 Wi-Fi，且电脑端服务已开启";
        }
        if (m.contains("failed to connect") || m.contains("refused")) {
            return "连不上电脑：检查 IP / 端口，或关闭电脑防火墙对 8765 的拦截";
        }
        return "连接失败" + (raw == null || raw.isEmpty() ? "" : "：" + raw);
    }

    /** 由 Activity 在启动时设置，便于通知里显示设备名。 */
    public void setDeviceName(Context context) {
        String name = Build.MANUFACTURER + " " + Build.MODEL;
        deviceName = name.trim();
    }
}
