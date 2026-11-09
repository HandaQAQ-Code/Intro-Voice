package com.introvoice.mic;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.media.MediaRecorder;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.util.Log;

/** 前台服务：屏幕熄灭后继续采集，通知栏常驻并可一键停止。 */
public final class MicService extends Service {
    private static final String TAG = "IntroVoiceMic";
    public static final String ACTION_START = "com.introvoice.mic.START";
    public static final String ACTION_STOP = "com.introvoice.mic.STOP";
    private static final String CHANNEL = "mic_stream";
    private static final int NOTIF_ID = 4201;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private PowerManager.WakeLock wakeLock;
    private NotificationManager nm;

    private final Runnable ticker = new Runnable() {
        @Override
        public void run() {
            if (!MicEngine.I.isRunning()) {
                shutdown();
                return;
            }
            notifyState();
            handler.postDelayed(this, 2000);
        }
    };

    @Override
    public void onCreate() {
        super.onCreate();
        nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        createChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            MicEngine.I.stop();
            shutdown();
            return START_NOT_STICKY;
        }
        if (!MicEngine.I.isRunning()) {
            SharedPreferences p = getSharedPreferences("mic", MODE_PRIVATE);
            String host = p.getString("host", "");
            if (host.isEmpty()) {
                stopSelf();
                return START_NOT_STICKY;
            }
            MicEngine.I.start(loadConfig(p));
        }
        startForeground(NOTIF_ID, buildNotification());
        acquireWakeLock();
        handler.removeCallbacks(ticker);
        handler.postDelayed(ticker, 2000);
        return START_NOT_STICKY;
    }

    @Override
    public void onDestroy() {
        handler.removeCallbacks(ticker);
        releaseWakeLock();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private MicEngine.Config loadConfig(SharedPreferences p) {
        MicEngine.Config c = new MicEngine.Config();
        c.host = p.getString("host", "");
        c.port = p.getInt("port", 8765);
        c.pin = p.getString("pin", "");
        c.sampleRate = p.getInt("rate", 48000);
        c.frameMs = p.getInt("frame", 20);
        c.source = p.getInt("source", MediaRecorder.AudioSource.VOICE_COMMUNICATION);
        c.gainDb = p.getFloat("gain", 0f);
        c.aec = p.getBoolean("aec", false);
        c.ns = p.getBoolean("ns", true);
        c.agc = p.getBoolean("agc", false);
        return c;
    }

    private void shutdown() {
        handler.removeCallbacks(ticker);
        releaseWakeLock();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            stopForeground(STOP_FOREGROUND_REMOVE);
        } else {
            stopForeground(true);
        }
        stopSelf();
    }

    private void createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O || nm == null) {
            return;
        }
        NotificationChannel ch = new NotificationChannel(CHANNEL, "手机麦克风",
                NotificationManager.IMPORTANCE_LOW);
        ch.setDescription("保持手机麦克风向电脑推流");
        ch.setShowBadge(false);
        ch.setSound(null, null);
        nm.createNotificationChannel(ch);
    }

    private void notifyState() {
        if (nm != null) {
            nm.notify(NOTIF_ID, buildNotification());
        }
    }

    private Notification buildNotification() {
        MicEngine.Config c = MicEngine.I.config();
        MicEngine.Stats s = MicEngine.I.snapshot();
        String title = s.state == MicEngine.State.LIVE ? "手机麦克风已连接" : "手机麦克风" + " · " + MicEngine.stateText(s.state);
        String text;
        if (s.state == MicEngine.State.LIVE) {
            text = String.format(java.util.Locale.US, "%s → %s · %d kbps · %d ms",
                    device(), c.host, s.kbps, Math.max(0, s.rttMs));
        } else {
            text = s.message;
        }
        Intent open = new Intent(this, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent content = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | immutable());
        Intent stop = new Intent(this, MicService.class).setAction(ACTION_STOP);
        PendingIntent stopPi = PendingIntent.getService(this, 1, stop,
                PendingIntent.FLAG_UPDATE_CURRENT | immutable());

        Notification.Builder b = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(this, CHANNEL)
                : new Notification.Builder(this);
        b.setSmallIcon(R.drawable.ic_stat_mic)
                .setContentTitle(title)
                .setContentText(text)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setContentIntent(content)
                .addAction(new Notification.Action.Builder(
                        android.graphics.drawable.Icon.createWithResource(this, R.drawable.ic_stat_mic),
                        "停止", stopPi).build());
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            b.setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE);
        }
        return b.build();
    }

    private String device() {
        String d = android.os.Build.MANUFACTURER + " " + android.os.Build.MODEL;
        return d.trim();
    }

    private int immutable() {
        return Build.VERSION.SDK_INT >= Build.VERSION_CODES.M
                ? PendingIntent.FLAG_IMMUTABLE : 0;
    }

    private void acquireWakeLock() {
        if (wakeLock != null && wakeLock.isHeld()) {
            return;
        }
        try {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            if (pm == null) {
                return;
            }
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "IntroVoice:mic");
            wakeLock.setReferenceCounted(false);
            wakeLock.acquire(6 * 60 * 60 * 1000L);
        } catch (Exception e) {
            Log.w(TAG, "wake lock failed", e);
        }
    }

    private void releaseWakeLock() {
        try {
            if (wakeLock != null && wakeLock.isHeld()) {
                wakeLock.release();
            }
        } catch (Exception ignored) {
        }
        wakeLock = null;
    }
}
