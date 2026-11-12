package com.introvoice.mic;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Typeface;
import android.media.MediaRecorder;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.SeekBar;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

import com.google.zxing.integration.android.IntentIntegrator;
import com.google.zxing.integration.android.IntentResult;

import java.util.Locale;
import java.util.function.IntConsumer;

/** 主界面：连接、电平、采集细节、电脑端 DSP。全部用代码构建，避免布局文件与主题差异。 */
public final class MainActivity extends Activity {
    private static final int REQ_MIC = 101;
    private static final int REQ_NOTIF = 102;
    private static final int REQ_CAMERA = 103;
    private static final String PREFS = "mic";

    private SharedPreferences prefs;
    private EditText hostInput;
    private EditText portInput;
    private EditText pinInput;
    private TextView connectBtn;
    private TextView statusText;
    private TextView statusDetail;
    private View statusDot;
    private LevelMeter meter;
    private TextView levelValue;
    private TextView statsRate;
    private TextView statsRtt;
    private TextView statsBuffer;
    private TextView statsDrop;
    private Switch keepSwitch;
    private ChoiceRow rateRow;
    private ChoiceRow frameRow;
    private ChoiceRow sourceRow;
    private SeekBar deviceGainBar;
    private TextView deviceGainValue;
    private SeekBar pcGainBar;
    private SeekBar pcHpfBar;
    private SeekBar pcGateBar;
    private SeekBar pcJitterBar;
    private TextView pcGainValue;
    private TextView pcHpfValue;
    private TextView pcGateValue;
    private TextView pcJitterValue;
    private boolean dspSent;
    private boolean tickerRunning;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final Runnable ticker = new Runnable() {
        @Override
        public void run() {
            if (!tickerRunning) {
                return;
            }
            refresh();
            ui.postDelayed(this, 120);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        MicEngine.I.setDeviceName(this);
        setContentView(buildUi());
        loadPrefs();
        handleDeepLink(getIntent());
        applyKeepScreen(prefs.getBoolean("keepOn", true));
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleDeepLink(intent);
    }

    @Override
    protected void onResume() {
        super.onResume();
        tickerRunning = true;
        ui.removeCallbacks(ticker);
        ui.post(ticker);
    }

    @Override
    protected void onPause() {
        tickerRunning = false;
        ui.removeCallbacks(ticker);
        savePrefs();
        super.onPause();
    }

    // ------------------------------------------------------------------ UI

    private View buildUi() {
        ScrollView scroll = new ScrollView(this);
        scroll.setBackgroundColor(Ui.BG);
        scroll.setFillViewport(true);
        scroll.setClipToPadding(false);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = Ui.dp(this, 20);
        root.setPadding(pad, Ui.dp(this, 18), pad, Ui.dp(this, 28));
        scroll.addView(root, new ScrollView.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        root.addView(hero());

        // ---- 连接 ----
        LinearLayout conn = card(root, "连接电脑", "扫电脑上的二维码最快；也可以手动填写地址和 PIN");
        LinearLayout addrRow = new LinearLayout(this);
        addrRow.setOrientation(LinearLayout.HORIZONTAL);
        addrRow.setGravity(Gravity.CENTER_VERTICAL);
        hostInput = field("电脑 IP，例如 192.168.1.8", InputType.TYPE_CLASS_TEXT
                | InputType.TYPE_TEXT_VARIATION_URI);
        portInput = field("端口", InputType.TYPE_CLASS_NUMBER);
        pinInput = field("PIN", InputType.TYPE_CLASS_TEXT
                | InputType.TYPE_TEXT_FLAG_CAP_CHARACTERS);
        pinInput.setFilters(new android.text.InputFilter[]{new android.text.InputFilter.LengthFilter(6)});
        pinInput.setLetterSpacing(0.28f);
        pinInput.setTextSize(18f);

        LinearLayout.LayoutParams hostLp = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        hostLp.rightMargin = Ui.dp(this, 8);
        addrRow.addView(hostInput, hostLp);
        LinearLayout.LayoutParams portLp = new LinearLayout.LayoutParams(Ui.dp(this, 84),
                ViewGroup.LayoutParams.WRAP_CONTENT);
        addrRow.addView(portInput, portLp);
        add(conn, addrRow, 0, 12, 0, 0);

        add(conn, pinInput, 0, 10, 0, 0);

        TextView scanBtn = (TextView) button("扫一扫连接", Ui.CARD_SOFT, Ui.TEXT);
        scanBtn.setBackground(Ui.round(Ui.CARD_SOFT, Ui.dp(this, 14), Ui.STROKE, Ui.dp(this, 1)));
        scanBtn.setOnClickListener(v -> startScan());
        add(conn, scanBtn, 0, 14, 0, 0);

        connectBtn = (TextView) button("连接电脑", Ui.ACCENT, 0xFF241C12);
        connectBtn.setOnClickListener(v -> toggleConnection());
        add(conn, connectBtn, 0, 10, 0, 0);

        LinearLayout statusRow = new LinearLayout(this);
        statusRow.setOrientation(LinearLayout.HORIZONTAL);
        statusRow.setGravity(Gravity.CENTER_VERTICAL);
        statusDot = new View(this);
        statusDot.setBackground(Ui.round(Ui.SUB, Ui.dp(this, 5), 0, 0));
        LinearLayout.LayoutParams dotLp = new LinearLayout.LayoutParams(Ui.dp(this, 9), Ui.dp(this, 9));
        dotLp.rightMargin = Ui.dp(this, 8);
        statusRow.addView(statusDot, dotLp);
        statusText = Ui.text(this, "未连接", 14f, Ui.SUB, true);
        statusRow.addView(statusText);
        add(conn, statusRow, 0, 14, 0, 0);

        statusDetail = Ui.text(this, "点上方按钮开始；连接后可以锁屏使用", 12f, Ui.SUB, false);
        statusDetail.setLineSpacing(Ui.dp(this, 2), 1f);
        add(conn, statusDetail, 0, 6, 0, 0);

        // ---- 电平 ----
        LinearLayout level = card(root, "实时电平", null);
        LinearLayout levelHead = new LinearLayout(this);
        levelHead.setOrientation(LinearLayout.HORIZONTAL);
        levelHead.setGravity(Gravity.CENTER_VERTICAL);
        TextView levelLabel = Ui.text(this, "输入电平", 13f, Ui.SUB, false);
        levelValue = Ui.text(this, "—", 22f, Ui.TEXT, true);
        levelValue.setGravity(Gravity.END);
        levelHead.addView(levelLabel, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        levelHead.addView(levelValue);
        level.addView(levelHead);

        meter = new LevelMeter(this);
        LinearLayout.LayoutParams meterLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, Ui.dp(this, 14));
        meterLp.topMargin = Ui.dp(this, 10);
        level.addView(meter, meterLp);

        LinearLayout statsRow = new LinearLayout(this);
        statsRow.setOrientation(LinearLayout.HORIZONTAL);
        LinearLayout.LayoutParams statsLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        statsLp.topMargin = Ui.dp(this, 14);
        level.addView(statsRow, statsLp);
        statsRate = statCell(statsRow, "码率");
        statsRtt = statCell(statsRow, "延迟");
        statsBuffer = statCell(statsRow, "抖动缓冲");
        statsDrop = statCell(statsRow, "丢帧");

        // ---- 采集 ----
        LinearLayout capture = card(root, "采集细节", "改动采样率 / 帧长会立即重连");
        capture.addView(rowLabel("采样率"));
        rateRow = new ChoiceRow(new String[]{"48 kHz", "44.1 kHz", "32 kHz", "16 kHz"},
                new int[]{48000, 44100, 32000, 16000}, 48000, v -> onCaptureChanged());
        add(capture, rateRow.row, 0, 8, 0, 0);

        capture.addView(rowLabel("帧长"));
        frameRow = new ChoiceRow(new String[]{"10 ms", "20 ms", "40 ms"},
                new int[]{10, 20, 40}, 20, v -> onCaptureChanged());
        add(capture, frameRow.row, 0, 8, 0, 0);

        capture.addView(rowLabel("采集源"));
        sourceRow = new ChoiceRow(new String[]{"通话降噪", "人声优先", "系统默认"},
                new int[]{MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                        MediaRecorder.AudioSource.VOICE_RECOGNITION,
                        MediaRecorder.AudioSource.MIC}, MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                v -> onCaptureChanged());
        add(capture, sourceRow.row, 0, 8, 0, 0);

        LinearLayout gainBox = slider(capture, "手机端增益", -12, 18, 0, "%d dB", true,
                v -> {
                    MicEngine.I.setGainDb(v);
                    savePrefs();
                });
        deviceGainBar = (SeekBar) gainBox.getChildAt(1);
        deviceGainValue = (TextView) ((LinearLayout) gainBox.getChildAt(0)).getChildAt(1);

        LinearLayout toggles = new LinearLayout(this);
        toggles.setOrientation(LinearLayout.HORIZONTAL);
        toggles.setGravity(Gravity.CENTER_VERTICAL);
        Switch nsSwitch = new Switch(this);
        styleSwitch(nsSwitch);
        nsSwitch.setText("系统降噪");
        nsSwitch.setTextColor(Ui.SUB);
        nsSwitch.setTextSize(13f);
        Switch aecSwitch = new Switch(this);
        styleSwitch(aecSwitch);
        aecSwitch.setText("回声消除");
        aecSwitch.setTextColor(Ui.SUB);
        aecSwitch.setTextSize(13f);
        LinearLayout.LayoutParams half = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        toggles.addView(nsSwitch, half);
        toggles.addView(aecSwitch, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        add(capture, toggles, 0, 10, 0, 0);
        nsSwitch.setChecked(prefs.getBoolean("ns", true));
        aecSwitch.setChecked(prefs.getBoolean("aec", false));
        nsSwitch.setOnCheckedChangeListener((b, on) -> {
            prefs.edit().putBoolean("ns", on).apply();
            onCaptureChanged();
        });
        aecSwitch.setOnCheckedChangeListener((b, on) -> {
            prefs.edit().putBoolean("aec", on).apply();
            onCaptureChanged();
        });

        // ---- 电脑端 DSP ----
        LinearLayout dsp = card(root, "电脑端处理", "这些参数直接写到电脑端的麦克风 DSP，即时生效");
        LinearLayout pc1 = slider(dsp, "输入增益", -12, 12, 0, "%d dB", false, v -> pushDsp());
        pcGainBar = (SeekBar) pc1.getChildAt(1);
        pcGainValue = (TextView) ((LinearLayout) pc1.getChildAt(0)).getChildAt(1);
        LinearLayout pc2 = slider(dsp, "高通（切低频噪声）", 20, 200, 80, "%d Hz", false, v -> pushDsp());
        pcHpfBar = (SeekBar) pc2.getChildAt(1);
        pcHpfValue = (TextView) ((LinearLayout) pc2.getChildAt(0)).getChildAt(1);
        LinearLayout pc3 = slider(dsp, "噪声门", -80, -20, -62, "%d dB", false, v -> pushDsp());
        pcGateBar = (SeekBar) pc3.getChildAt(1);
        pcGateValue = (TextView) ((LinearLayout) pc3.getChildAt(0)).getChildAt(1);
        LinearLayout pc4 = slider(dsp, "抖动缓冲", 20, 200, 80, "%d ms", false, v -> pushDsp());
        pcJitterBar = (SeekBar) pc4.getChildAt(1);
        pcJitterValue = (TextView) ((LinearLayout) pc4.getChildAt(0)).getChildAt(1);

        // ---- 其它 ----
        LinearLayout misc = card(root, "使用提示", null);
        LinearLayout keepRow = new LinearLayout(this);
        keepRow.setOrientation(LinearLayout.HORIZONTAL);
        keepRow.setGravity(Gravity.CENTER_VERTICAL);
        TextView keepLabel = Ui.text(this, "连接时保持屏幕常亮", 14f, Ui.TEXT, false);
        keepSwitch = new Switch(this);
        styleSwitch(keepSwitch);
        keepSwitch.setChecked(prefs.getBoolean("keepOn", true));
        keepSwitch.setOnCheckedChangeListener((b, on) -> {
            prefs.edit().putBoolean("keepOn", on).apply();
            applyKeepScreen(on);
        });
        keepRow.addView(keepLabel, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        keepRow.addView(keepSwitch);
        misc.addView(keepRow);

        TextView tips = Ui.text(this,
                "· 手机与电脑要在同一个 Wi-Fi，优先 5 GHz\n"
                        + "· 已开启前台服务，锁屏或切到后台仍会继续采集\n"
                        + "· 延迟太高时把帧长调到 10 ms，或把抖动缓冲降到 40 ms\n"
                        + "· 音画不同步属于正常现象，可在电脑端调节缓冲",
                12f, Ui.SUB, false);
        tips.setLineSpacing(Ui.dp(this, 3), 1f);
        add(misc, tips, 0, 10, 0, 0);

        TextView footer = Ui.text(this, "Intro Voice Mic 1.0.0 · 局域网直连，音频不经过服务器",
                11f, Ui.alpha(Ui.SUB, 0.7f), false);
        footer.setGravity(Gravity.CENTER);
        add(root, footer, 0, 6, 0, 0);

        return scroll;
    }

    private View hero() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.HORIZONTAL);
        box.setGravity(Gravity.CENTER_VERTICAL);
        box.setPadding(0, Ui.dp(this, 4), 0, Ui.dp(this, 18));

        TextView logo = Ui.text(this, "IV", 18f, Ui.ACCENT, true);
        logo.setGravity(Gravity.CENTER);
        logo.setBackground(Ui.round(0xFF241C12, Ui.dp(this, 14), Ui.ACCENT_DIM, Ui.dp(this, 1)));
        LinearLayout.LayoutParams logoLp = new LinearLayout.LayoutParams(Ui.dp(this, 48), Ui.dp(this, 48));
        logoLp.rightMargin = Ui.dp(this, 14);
        box.addView(logo, logoLp);

        LinearLayout titles = new LinearLayout(this);
        titles.setOrientation(LinearLayout.VERTICAL);
        titles.addView(Ui.text(this, "Intro Voice Mic", 23f, Ui.TEXT, true));
        TextView sub = Ui.text(this, "把手机变成电脑的无线麦克风", 12f, Ui.SUB, false);
        add(titles, sub, 0, 3, 0, 0);
        box.addView(titles);
        return box;
    }

    private LinearLayout card(LinearLayout parent, String title, String sub) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int p = Ui.dp(this, 16);
        box.setPadding(p, p, p, p);
        box.setBackground(Ui.round(Ui.CARD, Ui.dp(this, 18), Ui.STROKE, Ui.dp(this, 1)));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.bottomMargin = Ui.dp(this, 14);
        parent.addView(box, lp);
        if (title != null) {
            box.addView(Ui.section(this, title));
        }
        if (sub != null) {
            TextView s = Ui.text(this, sub, 12f, Ui.SUB, false);
            s.setLineSpacing(Ui.dp(this, 2), 1f);
            box.addView(s);
            Ui.setMargins(s, 0, Ui.dp(this, 3), 0, 0);
        }
        return box;
    }

    private TextView rowLabel(String text) {
        TextView t = Ui.text(this, text, 12f, Ui.SUB, false);
        Ui.setMargins(t, 0, Ui.dp(this, 12), 0, 0);
        return t;
    }

    private EditText field(String hint, int inputType) {
        EditText e = new EditText(this);
        e.setHint(hint);
        e.setHintTextColor(Ui.alpha(Ui.SUB, 0.75f));
        e.setTextColor(Ui.TEXT);
        e.setTextSize(15f);
        e.setSingleLine(true);
        e.setInputType(inputType);
        e.setBackground(Ui.round(0xFF100D0B, Ui.dp(this, 12), Ui.STROKE, Ui.dp(this, 1)));
        e.setPadding(Ui.dp(this, 13), Ui.dp(this, 11), Ui.dp(this, 13), Ui.dp(this, 11));
        return e;
    }

    private View button(String text, int bg, int fg) {
        TextView b = Ui.text(this, text, 16f, fg, true);
        b.setGravity(Gravity.CENTER);
        b.setPadding(Ui.dp(this, 16), Ui.dp(this, 15), Ui.dp(this, 16), Ui.dp(this, 15));
        b.setBackground(Ui.round(bg, Ui.dp(this, 14), 0, 0));
        b.setClickable(true);
        b.setFocusable(true);
        return b;
    }

    private void styleSwitch(Switch s) {
        int[][] states = new int[][]{
                new int[]{android.R.attr.state_checked},
                new int[]{}
        };
        s.setThumbTintList(new ColorStateList(states,
                new int[]{Ui.ACCENT, 0xFF7A7168}));
        s.setTrackTintList(new ColorStateList(states,
                new int[]{Ui.alpha(Ui.ACCENT, 0.45f), 0xFF3A322C}));
    }

    private TextView statCell(LinearLayout row, String label) {
        LinearLayout cell = new LinearLayout(this);
        cell.setOrientation(LinearLayout.VERTICAL);
        TextView value = Ui.text(this, "—", 15f, Ui.TEXT, true);
        TextView name = Ui.text(this, label, 11f, Ui.SUB, false);
        cell.addView(value);
        add(cell, name, 0, 1, 0, 0);
        row.addView(cell, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        return value;
    }

    /** 一行标签 + 数值 + 滑杆，返回整个容器（child0 表头，child1 SeekBar）。 */
    private LinearLayout slider(LinearLayout parent, String name, int min, int max,
                                int value, String unit, boolean live, IntConsumer onChange) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        LinearLayout head = new LinearLayout(this);
        head.setOrientation(LinearLayout.HORIZONTAL);
        head.setGravity(Gravity.CENTER_VERTICAL);
        TextView label = Ui.text(this, name, 12f, Ui.SUB, false);
        TextView val = Ui.text(this, String.format(Locale.US, unit, value), 13f, Ui.ACCENT, true);
        val.setGravity(Gravity.END);
        head.addView(label, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        head.addView(val);
        box.addView(head);

        SeekBar bar = new SeekBar(this);
        bar.setMax(max - min);
        bar.setProgress(Math.max(0, Math.min(max - min, value - min)));
        bar.setProgressTintList(ColorStateList.valueOf(Ui.ACCENT));
        bar.setProgressBackgroundTintList(ColorStateList.valueOf(0xFF3A322C));
        bar.setThumbTintList(ColorStateList.valueOf(Ui.ACCENT));
        bar.setPadding(Ui.dp(this, 6), 0, Ui.dp(this, 6), 0);
        bar.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override
            public void onProgressChanged(SeekBar seekBar, int progress, boolean fromUser) {
                int v = min + progress;
                val.setText(String.format(Locale.US, unit, v));
                if (live && fromUser) {
                    onChange.accept(v);
                }
            }

            @Override
            public void onStartTrackingTouch(SeekBar seekBar) {
            }

            @Override
            public void onStopTrackingTouch(SeekBar seekBar) {
                if (!live) {
                    onChange.accept(min + seekBar.getProgress());
                }
            }
        });
        LinearLayout.LayoutParams barLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        box.addView(bar, barLp);
        add(parent, box, 0, 8, 0, 0);
        return box;
    }

    private void add(LinearLayout parent, View child, int l, int t, int r, int b) {
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.setMargins(Ui.dp(this, l), Ui.dp(this, t), Ui.dp(this, r), Ui.dp(this, b));
        parent.addView(child, lp);
    }

    // ------------------------------------------------------------ 选择条

    private final class ChoiceRow {
        final LinearLayout row = new LinearLayout(MainActivity.this);
        final String[] labels;
        final int[] values;
        int selected;

        ChoiceRow(String[] labels, int[] values, int initial, IntConsumer onChange) {
            this.labels = labels;
            this.values = values;
            this.selected = 0;
            row.setOrientation(LinearLayout.HORIZONTAL);
            for (int i = 0; i < labels.length; i++) {
                final int index = i;
                TextView chip = Ui.text(MainActivity.this, labels[i], 13f, Ui.SUB, false);
                chip.setGravity(Gravity.CENTER);
                chip.setPadding(Ui.dp(MainActivity.this, 4), Ui.dp(MainActivity.this, 10),
                        Ui.dp(MainActivity.this, 4), Ui.dp(MainActivity.this, 10));
                LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(0,
                        ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
                if (i < labels.length - 1) {
                    lp.rightMargin = Ui.dp(MainActivity.this, 8);
                }
                chip.setOnClickListener(v -> {
                    if (selected != index) {
                        selected = index;
                        styleChips();
                        onChange.accept(values[index]);
                    }
                });
                row.addView(chip, lp);
            }
            setValue(initial);
        }

        void setValue(int value) {
            for (int i = 0; i < values.length; i++) {
                if (values[i] == value) {
                    selected = i;
                    break;
                }
            }
            styleChips();
        }

        int value() {
            return values[selected];
        }

        void styleChips() {
            for (int i = 0; i < row.getChildCount(); i++) {
                TextView chip = (TextView) row.getChildAt(i);
                boolean on = i == selected;
                chip.setTextColor(on ? 0xFF241C12 : Ui.SUB);
                chip.setTypeface(chip.getTypeface(), on ? Typeface.BOLD : Typeface.NORMAL);
                chip.setBackground(on
                        ? Ui.round(Ui.ACCENT, Ui.dp(MainActivity.this, 11), 0, 0)
                        : Ui.round(Ui.CARD_SOFT, Ui.dp(MainActivity.this, 11), Ui.STROKE, Ui.dp(MainActivity.this, 1)));
            }
        }
    }

    // ------------------------------------------------------------ 逻辑

    private void toggleConnection() {
        if (MicEngine.I.isRunning()) {
            disconnect();
        } else {
            requestPermissionsAndConnect();
        }
    }

    private void requestPermissionsAndConnect() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M
                && checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_MIC);
            return;
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIF);
            // 不阻塞连接：没有通知权限也能在前台使用
        }
        connect();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        if (requestCode == REQ_MIC) {
            if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
                connect();
            } else {
                toast("没有麦克风权限，无法采集；请到系统设置里允许");
            }
        } else if (requestCode == REQ_CAMERA) {
            if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
                launchScanner();
            } else {
                toast("没有相机权限，无法扫码；请手动填写电脑 IP 和 PIN");
            }
        }
    }

    private void connect() {
        String host = hostInput.getText().toString().trim();
        int port;
        try {
            port = Integer.parseInt(portInput.getText().toString().trim());
        } catch (Exception e) {
            port = 8765;
        }
        String pin = pinInput.getText().toString().trim().toUpperCase(Locale.US);
        if (host.isEmpty()) {
            toast("请先填写电脑 IP（在电脑端页面上可以看到）");
            return;
        }
        if (port <= 0 || port > 65535) {
            toast("端口不正确，一般是 8765");
            return;
        }
        if (pin.length() < 4) {
            toast("请填写电脑端显示的 PIN");
            return;
        }
        savePrefs();
        dspSent = false;
        MicEngine.I.start(currentConfig());
        startMicService();
        refresh();
    }

    private void disconnect() {
        MicEngine.I.stop();
        stopService(new Intent(this, MicService.class));
        dspSent = false;
        refresh();
    }

    private MicEngine.Config currentConfig() {
        MicEngine.Config c = new MicEngine.Config();
        c.host = hostInput.getText().toString().trim();
        try {
            c.port = Integer.parseInt(portInput.getText().toString().trim());
        } catch (Exception e) {
            c.port = 8765;
        }
        c.pin = pinInput.getText().toString().trim().toUpperCase(Locale.US);
        c.sampleRate = rateRow.value();
        c.frameMs = frameRow.value();
        c.source = sourceRow.value();
        c.gainDb = deviceGainBar == null ? 0 : -12 + deviceGainBar.getProgress();
        c.ns = prefs.getBoolean("ns", true);
        c.aec = prefs.getBoolean("aec", false);
        c.agc = prefs.getBoolean("agc", false);
        return c;
    }

    private void onCaptureChanged() {
        savePrefs();
        if (MicEngine.I.isRunning()) {
            MicEngine.I.start(currentConfig());
            startMicService();
        }
    }

    private void pushDsp() {
        if (!MicEngine.I.isRunning()) {
            savePrefs();
            return;
        }
        float gain = -12 + pcGainBar.getProgress();
        float hpf = 20 + pcHpfBar.getProgress();
        float gate = -80 + pcGateBar.getProgress();
        float jitter = 20 + pcJitterBar.getProgress();
        MicEngine.I.sendRemoteDsp(gain, hpf, gate, jitter);
        savePrefs();
    }

    private void startMicService() {
        Intent intent = new Intent(this, MicService.class).setAction(MicService.ACTION_START);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent);
        } else {
            startService(intent);
        }
    }

    private void applyKeepScreen(boolean on) {
        if (on) {
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        } else {
            getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        }
    }

    // ------------------------------------------------------------ 刷新

    private void refresh() {
        MicEngine.Stats s = MicEngine.I.snapshot();
        boolean live = s.state == MicEngine.State.LIVE;
        boolean busy = MicEngine.I.isRunning();

        connectBtn.setText(busy ? "断开连接" : "连接电脑");
        connectBtn.setBackground(Ui.round(busy ? Ui.CARD_SOFT : Ui.ACCENT, Ui.dp(this, 14),
                busy ? Ui.STROKE : 0, busy ? Ui.dp(this, 1) : 0));
        connectBtn.setTextColor(busy ? Ui.TEXT : 0xFF241C12);

        int dotColor = live ? Ui.OK
                : s.state == MicEngine.State.ERROR ? Ui.ERR
                : s.state == MicEngine.State.CONNECTING ? Ui.WARN : Ui.SUB;
        statusDot.setBackground(Ui.round(dotColor, Ui.dp(this, 5), 0, 0));
        statusText.setText(MicEngine.stateText(s.state));
        statusText.setTextColor(live ? Ui.OK : s.state == MicEngine.State.ERROR ? Ui.ERR : Ui.SUB);

        if (live) {
            statusDetail.setText(String.format(Locale.US,
                    "电脑端 %d kHz · 抖动缓冲 %d ms · 往返延迟 %d ms · %s",
                    s.serverRate / 1000, s.jitterMs, Math.max(0, s.rttMs),
                    s.device.isEmpty() ? "" : s.device));
        } else {
            statusDetail.setText(s.message);
        }

        levelValue.setText(s.levelDb <= -89f ? "—" : String.format(Locale.US, "%.0f dB", s.levelDb));
        meter.setLevelDb(s.levelDb);

        statsRate.setText(s.kbps > 0 ? s.kbps + " kbps" : "—");
        statsRtt.setText(s.rttMs >= 0 ? s.rttMs + " ms" : "—");
        statsBuffer.setText(s.jitterMs > 0 ? s.jitterMs + " ms" : "—");
        statsDrop.setText(String.valueOf(s.dropped));

        if (live && !dspSent) {
            pushDsp();
            dspSent = true;
        } else if (!live) {
            dspSent = false;
        }
    }

    // ------------------------------------------------------------ 存取

    private void loadPrefs() {
        hostInput.setText(prefs.getString("host", ""));
        portInput.setText(String.valueOf(prefs.getInt("port", 8765)));
        pinInput.setText(prefs.getString("pin", ""));
        rateRow.setValue(prefs.getInt("rate", 48000));
        frameRow.setValue(prefs.getInt("frame", 20));
        sourceRow.setValue(prefs.getInt("source", MediaRecorder.AudioSource.VOICE_COMMUNICATION));
        deviceGainBar.setProgress(prefs.getInt("gain", 0) + 12);
        deviceGainValue.setText(String.format(Locale.US, "%d dB", prefs.getInt("gain", 0)));

        pcGainBar.setProgress(prefs.getInt("pcGain", 0) + 12);
        pcGainValue.setText(String.format(Locale.US, "%d dB", prefs.getInt("pcGain", 0)));
        pcHpfBar.setProgress(prefs.getInt("pcHpf", 80) - 20);
        pcHpfValue.setText(String.format(Locale.US, "%d Hz", prefs.getInt("pcHpf", 80)));
        pcGateBar.setProgress(prefs.getInt("pcGate", -62) + 80);
        pcGateValue.setText(String.format(Locale.US, "%d dB", prefs.getInt("pcGate", -62)));
        pcJitterBar.setProgress(prefs.getInt("pcJitter", 80) - 20);
        pcJitterValue.setText(String.format(Locale.US, "%d ms", prefs.getInt("pcJitter", 80)));
    }

    private void savePrefs() {
        SharedPreferences.Editor e = prefs.edit();
        e.putString("host", hostInput.getText().toString().trim());
        try {
            e.putInt("port", Integer.parseInt(portInput.getText().toString().trim()));
        } catch (Exception ignored) {
        }
        e.putString("pin", pinInput.getText().toString().trim().toUpperCase(Locale.US));
        e.putInt("rate", rateRow.value());
        e.putInt("frame", frameRow.value());
        e.putInt("source", sourceRow.value());
        if (deviceGainBar != null) {
            e.putInt("gain", -12 + deviceGainBar.getProgress());
        }
        if (pcGainBar != null) {
            e.putInt("pcGain", -12 + pcGainBar.getProgress());
            e.putInt("pcHpf", 20 + pcHpfBar.getProgress());
            e.putInt("pcGate", -80 + pcGateBar.getProgress());
            e.putInt("pcJitter", 20 + pcJitterBar.getProgress());
        }
        e.apply();
    }

    // ------------------------------------------------------------ 扫码

    private void startScan() {
        // 扫码库不会自己申请相机权限：先申请，授权后再打开扫码界面，
        // 否则某些机型会直接抛 SecurityException 闪退。
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M
                && checkSelfPermission(Manifest.permission.CAMERA)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.CAMERA}, REQ_CAMERA);
            return;
        }
        launchScanner();
    }

    private void launchScanner() {
        try {
            new IntentIntegrator(this)
                    .setDesiredBarcodeFormats(IntentIntegrator.QR_CODE)
                    .setPrompt("对准电脑端「手机麦克风」页的二维码")
                    .setBeepEnabled(false)
                    .setBarcodeImageEnabled(false)
                    .setOrientationLocked(true)
                    .initiateScan();
        } catch (Throwable t) {
            // 相机被占用、系统没有扫码组件等异常都不能让 App 闪退。
            toast("无法打开扫码：" + t.getClass().getSimpleName() + "；请手动填写 IP 和 PIN");
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        IntentResult result = IntentIntegrator.parseActivityResult(requestCode, resultCode, data);
        if (result != null) {
            if (result.getContents() != null) {
                applyScanned(result.getContents());
            }
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    /** 支持 introvoice://mic?host=&port=&pin= 深链；扫到安装页地址时只填地址并提示补 PIN。 */
    private void applyScanned(String raw) {
        String text = raw == null ? "" : raw.trim();
        if (text.isEmpty()) {
            return;
        }
        Uri uri = Uri.parse(text);
        String host = null;
        String port = null;
        String pin = null;
        if ("introvoice".equals(uri.getScheme())) {
            host = uri.getQueryParameter("host");
            port = uri.getQueryParameter("port");
            pin = uri.getQueryParameter("pin");
        } else if ("http".equals(uri.getScheme()) || "https".equals(uri.getScheme())) {
            host = uri.getHost();
            if (uri.getPort() > 0) {
                port = String.valueOf(uri.getPort());
            }
        }
        if (host == null || host.isEmpty()) {
            toast("这不是电脑端的配对二维码");
            return;
        }
        hostInput.setText(host);
        if (port != null && !port.isEmpty()) {
            portInput.setText(port);
        }
        if (pin != null && !pin.isEmpty()) {
            pinInput.setText(pin.toUpperCase(Locale.US));
        }
        savePrefs();
        if (pin == null || pin.length() < 4) {
            pinInput.requestFocus();
            toast("已填入地址，请补填电脑上显示的 6 位 PIN");
            return;
        }
        toast("已读取配对信息，正在连接…");
        requestPermissionsAndConnect();
    }

    private void handleDeepLink(Intent intent) {
        if (intent == null || intent.getData() == null) {
            return;
        }
        Uri uri = intent.getData();
        if (!"introvoice".equals(uri.getScheme())) {
            return;
        }
        String host = uri.getQueryParameter("host");
        String port = uri.getQueryParameter("port");
        String pin = uri.getQueryParameter("pin");
        if (host != null && !host.isEmpty()) {
            hostInput.setText(host);
        }
        if (port != null && !port.isEmpty()) {
            portInput.setText(port);
        }
        if (pin != null && !pin.isEmpty()) {
            pinInput.setText(pin.toUpperCase(Locale.US));
        }
        savePrefs();
        toast("已从电脑端读取配对信息，正在连接…");
        // 扫码后直接连接，省去再点一次「连接电脑」。
        if (host != null && !host.isEmpty() && pin != null && pin.length() >= 4) {
            requestPermissionsAndConnect();
        }
    }

    private void toast(String msg) {
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show();
    }
}
