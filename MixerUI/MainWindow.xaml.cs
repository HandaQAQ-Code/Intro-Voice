using System.Collections.ObjectModel;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Windows.Graphics;
using Windows.UI;

namespace MixerUI;

public sealed class NotBoolConverter : Microsoft.UI.Xaml.Data.IValueConverter
{
    public object Convert(object value, Type targetType, object parameter, string language)
        => value is bool b ? !b : true;

    public object ConvertBack(object value, Type targetType, object parameter, string language)
        => value is bool b ? !b : false;
}

public sealed partial class MainWindow : Window
{
    private readonly ServiceClient _client = new();
    private readonly ObservableCollection<PluginItem> _plugins = new();
    private readonly ObservableCollection<PresetItem> _presets = new();
    private readonly ObservableCollection<MarketItem> _market = new();
    private readonly ObservableCollection<string> _messages = new();
    private readonly Dictionary<(int plugin, int param), double> _pendingParams = new();

    private List<DeviceItem> _inputs = new();
    private List<DeviceItem> _outputs = new();
    private readonly DispatcherQueueTimer _statusTimer;
    private readonly DispatcherQueueTimer _paramTimer;
    private readonly DispatcherQueueTimer _optionTimer;
    private readonly DispatcherQueueTimer _dspTimer;

    private string _savedInputLabel = "";
    private string _savedOutputLabel = "";
    private bool _suppressOptions;
    private bool _suppressParam;
    private bool _dspSyncing;
    private bool _dspDirty;
    private bool _polling;
    private int _pollCount;
    private bool _flushingParams;
    private bool _busy;
    private bool _connected;
    private bool _running;
    private bool _muted;
    private bool _gpuAvailable;
    private string _gpuNote = "";
    private string _messageCache = "";
    private bool _marketLoading;
    private JsonElement _updateInfo;
    private bool _updateAvailable;
    private string _updateFile = "";
    private bool _installInProgress;

    public MainWindow()
    {
        InitializeComponent();
        App.Trace("mainwindow: xaml loaded");
        // Negative ranges must be set in code: assigning Maximum below the
        // default Minimum (0) is rejected by RangeBase during XAML load.
        FloorSlider.Minimum = -80;
        FloorSlider.Maximum = -20;
        FloorSlider.Value = -55;
        SetupDspControls();
        Title = "Record Out · 直播调音台";
        try
        {
            SystemBackdrop = new MicaBackdrop();
        }
        catch
        {
            // Mica is unavailable on older systems; the solid theme brush remains.
        }
        try
        {
            // Fit inside the monitor work area: a fixed 1480x940 window is
            // clipped by the taskbar on 1920x1080 displays.
            var work = DisplayArea.GetFromWindowId(AppWindow.Id, DisplayAreaFallback.Primary).WorkArea;
            AppWindow.Resize(new SizeInt32(Math.Min(1480, work.Width - 32), Math.Min(940, work.Height - 32)));
        }
        catch
        {
        }

        AiModeCombo.ItemsSource = new[]
        {
            new AiModeItem { Value = "off", Label = "关闭（48 kHz 全带宽旁通）" },
            new AiModeItem { Value = "cpu", Label = "CPU（声纹提取 + 降噪）" },
            new AiModeItem { Value = "gpu", Label = "GPU（声纹 + 降噪 + LavaSR 超分）" },
        };
        AiModeCombo.SelectedIndex = 0;

        LatencyCombo.ItemsSource = new[]
        {
            new LatencyItem { Value = "balanced", Label = "平衡（224 ms 前瞻）" },
            new LatencyItem { Value = "fast", Label = "低延迟（192 ms 前瞻）" },
            new LatencyItem { Value = "quality", Label = "完整上下文（352 ms 前瞻）" },
        };
        LatencyCombo.SelectedIndex = 0;

        ChainList.ItemsSource = _plugins;
        PresetList.ItemsSource = _presets;
        MarketList.ItemsSource = _market;
        MessageList.ItemsSource = _messages;
        // 空状态：没有数据时显示引导文案，列表隐藏。
        ChainList.Visibility = Visibility.Collapsed;
        PresetList.Visibility = Visibility.Collapsed;
        ParamEmpty.Visibility = Visibility.Visible;
        PresetEmpty.Visibility = Visibility.Visible;
        ChainEmpty.Visibility = Visibility.Visible;

        _statusTimer = DispatcherQueue.CreateTimer();
        _statusTimer.Interval = TimeSpan.FromMilliseconds(500);
        _statusTimer.Tick += PollStatus;
        _statusTimer.Start();

        _paramTimer = DispatcherQueue.CreateTimer();
        _paramTimer.Interval = TimeSpan.FromMilliseconds(150);
        _paramTimer.Tick += FlushParams;
        _paramTimer.Start();

        _optionTimer = DispatcherQueue.CreateTimer();
        _optionTimer.Interval = TimeSpan.FromMilliseconds(300);
        _optionTimer.IsRepeating = false;
        _optionTimer.Tick += ApplyLiveOptions;

        // 拖动滑块时每帧都会触发 ValueChanged，用一次性定时器合并成一次下发。
        _dspTimer = DispatcherQueue.CreateTimer();
        _dspTimer.Interval = TimeSpan.FromMilliseconds(200);
        _dspTimer.IsRepeating = false;
        _dspTimer.Tick += ApplyDspParams;

        Nav.SizeChanged += (_, _) => UpdatePageHeights();
        Closed += OnClosed;
        App.Trace("mainwindow: ctor end");
        _ = ConnectAsync();
        App.Trace("mainwindow: connect started");
    }

    // ------------------------------------------------------------------
    // connection and population
    // ------------------------------------------------------------------
    private async Task ConnectAsync()
    {
        // Let the window paint before any pipe/process work begins.
        await Task.Yield();
        SetBusy(true, "正在连接调音台服务…");
        try
        {
            App.Trace("connect: awaiting service");
            var notice = await Task.Run(async () => await _client.ConnectAsync());
            App.Trace("connect: service ready");
            var hello = await _client.CallAsync("hello", null, 30000);
            App.Trace("connect: hello received");
            ApplyDevices(hello.GetProperty("devices"));
            App.Trace("connect: devices applied");
            ApplySettings(hello.GetProperty("settings"));
            App.Trace("connect: settings applied");
            ApplyPlugins(hello.GetProperty("plugins"));
            App.Trace("connect: plugins applied");
            string chainError = Str(hello, "chainError");
            if (chainError.Length > 0) ShowError(chainError);
            _gpuAvailable = Bool(hello, "gpuAvailable");
            _gpuNote = Str(hello, "gpuName");
            UpdateTierTexts();
            await RefreshPresets();
            App.Trace("connect: presets refreshed");
            _connected = true;
            StatusDot.Fill = new SolidColorBrush(Color.FromArgb(255, 76, 201, 116));
            ServiceText.Text = notice;
            StartButton.IsEnabled = true;
            App.Trace("connect: done");
        }
        catch (Exception ex)
        {
            _connected = false;
            StatusDot.Fill = new SolidColorBrush(Color.FromArgb(255, 214, 69, 69));
            ServiceText.Text = "服务未连接";
            App.Trace($"connect failed: {ex.Message}");
            ShowError(ex.Message);
        }
        finally
        {
            SetBusy(false);
        }
    }

    private static List<DeviceItem> ParseDevices(JsonElement array)
    {
        var list = new List<DeviceItem>();
        if (array.ValueKind != JsonValueKind.Array) return list;
        foreach (var item in array.EnumerateArray())
        {
            list.Add(new DeviceItem
            {
                Id = (int)Num(item, "id", -1),
                Label = Str(item, "label"),
            });
        }
        return list;
    }

    private void ApplyDevices(JsonElement devices)
    {
        _inputs = ParseDevices(devices.GetProperty("inputs"));
        _outputs = ParseDevices(devices.GetProperty("outputs"));
        InputCombo.ItemsSource = _inputs;
        OutputCombo.ItemsSource = _outputs;

        string cableLabel = Str(devices, "cableOutput");
        string defaultInput = Str(devices, "inputDefault");
        SelectDevice(InputCombo, _inputs, _savedInputLabel, defaultInput);
        SelectDevice(OutputCombo, _outputs, _savedOutputLabel, cableLabel);
        UpdateRoutingText();
    }

    private static void SelectDevice(ComboBox combo, List<DeviceItem> items, string saved, string fallback)
    {
        int index = items.FindIndex(d => d.Label == saved);
        if (index < 0) index = items.FindIndex(d => d.Label == fallback);
        if (index < 0 && items.Count > 0) index = 0;
        combo.SelectedIndex = index;
    }

    private void UpdateRoutingText()
    {
        var input = InputCombo.SelectedItem as DeviceItem;
        var output = OutputCombo.SelectedItem as DeviceItem;
        bool cable = output is not null && output.Label.ToLowerInvariant().Contains("cable");
        RoutingText.Text =
            $"输入：{(input?.Label ?? "未选择")}\n输出：{(output?.Label ?? "未选择")}" +
            (cable ? "" : "\n注意：所选输出不是 CABLE Input，OBS 将无法从 CABLE Output 采集本路音频。");
    }

    private void ApplySettings(JsonElement settings)
    {
        _suppressOptions = true;
        try
        {
            string aiMode = Str(settings, "aiMode");
            AiModeCombo.SelectedIndex = aiMode == "cpu" ? 1 : aiMode == "gpu" ? 2 : 0;
            DenoiseSwitch.IsOn = Bool(settings, "denoise");
            GainSlider.Value = Math.Clamp(Num(settings, "gainDb", 0), -24, 24);
            FloorSlider.Value = Math.Clamp(Num(settings, "floorDb", -55), -80, -20);
            SrSlider.Value = Math.Clamp(Num(settings, "srStrength", 0.7), 0, 1);
            string latency = Str(settings, "latencyMode");
            LatencyCombo.SelectedIndex = latency == "fast" ? 1 : latency == "quality" ? 2 : 0;
            _savedInputLabel = Str(settings, "input");
            _savedOutputLabel = Str(settings, "output");
            if (settings.TryGetProperty("dsp", out var dsp) && dsp.ValueKind == JsonValueKind.Object)
                ApplyDsp(dsp);
            UpdateOptionLabels();
            UpdateAiControls();
            UpdateTierTexts();
        }
        finally
        {
            _suppressOptions = false;
        }
    }

    private void ApplyPlugins(JsonElement array)
    {
        int previous = (ChainList.SelectedItem as PluginItem)?.Index ?? -1;
        _suppressParam = true;
        _plugins.Clear();
        if (array.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in array.EnumerateArray())
            {
                _plugins.Add(new PluginItem
                {
                    Index = (int)Num(item, "index", _plugins.Count),
                    Name = Str(item, "name"),
                    Vendor = Str(item, "vendor"),
                    Path = Str(item, "path"),
                    ClassId = Str(item, "classId"),
                    Bypass = Bool(item, "bypass"),
                    ParamCount = (int)Num(item, "paramCount", 0),
                });
            }
        }
        var restored = _plugins.FirstOrDefault(p => p.Index == previous);
        ChainList.SelectedItem = restored;
        if (restored is null)
        {
            ParamList.ItemsSource = null;
            ParamsTitle.Text = "插件参数";
            ParamEmpty.Visibility = Visibility.Visible;
        }
        ChainEmpty.Visibility = _plugins.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        ChainList.Visibility = _plugins.Count == 0 ? Visibility.Collapsed : Visibility.Visible;
        ChainCountText.Text = _plugins.Count == 0 ? "尚未加载插件" : $"{_plugins.Count} 个插件 · 按顺序串联";
        PluginBadge.Value = _plugins.Count;
        PluginBadge.Visibility = _plugins.Count == 0 ? Visibility.Collapsed : Visibility.Visible;
        _suppressParam = false;
        UpdateChainButtons();
    }

    // ------------------------------------------------------------------
    // settings controls
    // ------------------------------------------------------------------
    private void AiMode_Changed(object sender, SelectionChangedEventArgs e)
    {
        UpdateAiControls();
        UpdateTierTexts();
    }

    private void Latency_Changed(object sender, SelectionChangedEventArgs e)
    {
        UpdateTierTexts();
    }

    private void UpdateAiControls()
    {
        bool ai = (AiModeCombo.SelectedItem as AiModeItem)?.Value != "off";
        DenoiseSwitch.IsEnabled = ai;
        FloorSlider.IsEnabled = ai;
        SrSlider.IsEnabled = ai;
        LatencyCombo.IsEnabled = ai;
    }

    /// <summary>Explains what each of the three tiers does, and how good it sounds.</summary>
    private void UpdateTierTexts()
    {
        string mode = (AiModeCombo.SelectedItem as AiModeItem)?.Value ?? "off";
        AiModeHint.Text = mode switch
        {
            "cpu" => "CPU 档：声纹提取 + 实时降噪 + FlashSR 超分（16→48 kHz）。" +
                     "音质自然清晰，人声有一层轻微“重建”感；占用约 1~2 个核心，无需显卡。",
            "gpu" => (_gpuAvailable
                        ? $"GPU 档：声纹 + 降噪 + LavaSR v2 超分（{_gpuNote}）。"
                        : $"GPU 档（当前不可用：{_gpuNote}）。")
                     + "音质最好：高频细节、齿音和气息最接近真实，几乎听不出重建痕迹；需要 NVIDIA 显卡与 CUDA 版 PyTorch。",
            _ => "关闭档：48 kHz 全带宽直通，不做声纹、不做降噪，音质与原声完全一致；" +
                 "CPU 占用最低，延迟最小。代价是环境噪声与房间混响都会原样输出。",
        };

        TierCompare.Text = _gpuAvailable
            ? "关闭：原声直通 · 音质 100% 保真 · CPU ≈0% · 无降噪，适合安静环境与低配机器\n" +
              "CPU：降噪 + FlashSR 超分 · 音质“清晰耐听”，细节略有平滑 · CPU 中高、GPU 不用 · 适合没有 N 卡的用户\n" +
              $"GPU：降噪 + LavaSR v2 超分 · 音质最好、高频最自然 · 显存约 1~2 GB · 适合直播/录制追求音质（{_gpuNote}）"
            : "关闭：原声直通 · 音质 100% 保真 · CPU ≈0% · 无降噪，适合安静环境与低配机器\n" +
              "CPU：降噪 + FlashSR 超分 · 音质“清晰耐听”，细节略有平滑 · CPU 中高 · 当前环境可用，推荐\n" +
              $"GPU：降噪 + LavaSR v2 超分 · 音质最好但需要 NVIDIA 显卡 + CUDA PyTorch（{_gpuNote}）";

        string latency = (LatencyCombo.SelectedItem as LatencyItem)?.Value ?? "balanced";
        LatencyHint.Text = latency switch
        {
            "fast" => "低延迟：192 ms 前瞻。互动连麦更跟手，代价是超分会略保守、瞬态细节少一些。",
            "quality" => "完整上下文：352 ms 前瞻。超分最稳定、齿音最自然，但延迟明显，适合录制与后期。",
            _ => "平衡：224 ms 前瞻，音质与延迟折中，日常直播/录屏推荐。",
        };
    }

    private void UpdateOptionLabels()
    {
        GainValue.Text = $"{GainSlider.Value:0.0} dB";
        FloorValue.Text = $"{FloorSlider.Value:0} dB";
        SrValue.Text = $"{SrSlider.Value:0.00}";
    }

    private void LiveOption_Changed(object sender, Microsoft.UI.Xaml.Controls.Primitives.RangeBaseValueChangedEventArgs e)
    {
        UpdateOptionLabels();
        if (_suppressOptions || !_connected) return;
        _optionTimer.Stop();
        _optionTimer.Start();
    }

    private async void ApplyLiveOptions(object? sender, object args)
    {
        if (!_connected) return;
        try
        {
            await _client.CallAsync("set_options", BuildOptionArgs(), 15000);
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
    }

    private JsonObject BuildOptionArgs() => new()
    {
        ["gainDb"] = GainSlider.Value,
        ["denoise"] = DenoiseSwitch.IsOn,
        ["floorDb"] = FloorSlider.Value,
        ["srStrength"] = SrSlider.Value,
        ["latencyMode"] = (LatencyCombo.SelectedItem as LatencyItem)?.Value ?? "balanced",
    };

    private async void RefreshDevices_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        SetBusy(true, "正在重新枚举音频设备…");
        try
        {
            var devices = await _client.CallAsync("devices");
            ApplyDevices(devices);
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
        finally
        {
            SetBusy(false);
        }
    }

    // ------------------------------------------------------------------
    // transport
    // ------------------------------------------------------------------
    private async void Start_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        var input = InputCombo.SelectedItem as DeviceItem;
        var output = OutputCombo.SelectedItem as DeviceItem;
        if (input is null || output is null)
        {
            ShowError("请先选择输入麦克风和输出设备");
            return;
        }
        var args = new JsonObject
        {
            ["inputDevice"] = input.Id,
            ["outputDevice"] = output.Id,
            ["aiMode"] = (AiModeCombo.SelectedItem as AiModeItem)?.Value ?? "off",
            ["denoise"] = DenoiseSwitch.IsOn,
            ["gainDb"] = GainSlider.Value,
            ["floorDb"] = FloorSlider.Value,
            ["srStrength"] = SrSlider.Value,
            ["latencyMode"] = (LatencyCombo.SelectedItem as LatencyItem)?.Value ?? "balanced",
            ["dsp"] = BuildDspParams(),
        };
        SetBusy(true, "正在启动音频链路…");
        try
        {
            await _client.CallAsync("start", args, 30000);
            ErrorBar.IsOpen = false;
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
        finally
        {
            SetBusy(false);
        }
    }

    private async void Stop_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        try
        {
            await _client.CallAsync("stop", null, 15000);
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
    }

    private async void Mute_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        var target = !_muted;
        try
        {
            await _client.CallAsync("set_options", new JsonObject { ["muted"] = target }, 10000);
            _muted = target;
            MuteButton.Content = _muted ? "取消静音" : "静音";
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
    }

    // ------------------------------------------------------------------
    // status polling
    // ------------------------------------------------------------------
    private async void PollStatus(object? sender, object args)
    {
        if (_polling || _busy || !_connected) return;
        _polling = true;
        _pollCount++;
        try
        {
            if (_pollCount <= 5 || _pollCount % 60 == 0) App.Trace($"poll: request #{_pollCount}");
            var status = await _client.CallAsync("status", null, 6000);
            if (_pollCount <= 5) App.Trace("poll: response");
            ApplyStatus(status);
            if (_pollCount <= 5) App.Trace("poll: applied");
        }
        catch
        {
            // The next tick retries; transient timeouts during scanning are benign.
        }
        finally
        {
            _polling = false;
        }
    }

    private void ApplyStatus(JsonElement status)
    {
        bool running = Bool(status, "running");
        bool loading = Bool(status, "loading");
        _running = running;
        RunStateText.Text = loading ? "正在加载 AI 模型…" : running ? "运行中" : "已停止";
        EnginePillText.Text = loading ? "引擎 · 加载中" : running ? "引擎 · 运行中" : "引擎 · 已停止";
        var backend = Str(status, "backend");
        BackendText.Text = backend.Length > 0 ? backend : "—";

        double inputDb = Num(status, "inputDb", -90);
        double outputDb = Num(status, "outputDb", -90);
        InputMeter.Value = MeterValue(inputDb);
        OutputMeter.Value = MeterValue(outputDb);
        InputDbText.Text = DbText(inputDb);
        OutputDbText.Text = DbText(outputDb);
        BarInputMeter.Value = MeterValue(inputDb);
        BarInputText.Text = DbText(inputDb);
        BarOutputMeter.Value = MeterValue(outputDb);
        BarOutputText.Text = DbText(outputDb);

        int inputRate = (int)Num(status, "inputRate", 0);
        int outputRate = (int)Num(status, "outputRate", 48000);
        RateText.Text = inputRate > 0 ? $"{inputRate} Hz → {outputRate} Hz" : "—";
        double latency = Num(status, "latencyMs", 0);
        LatencyText.Text = $"{latency:0.0} ms";
        RtfText.Text = $"{Num(status, "rtf", 0):0.0000}";
        BarRtfText.Text = $"{Num(status, "rtf", 0):0.000}";
        DropsText.Text = $"{Num(status, "drops", 0):0} / {Num(status, "underruns", 0):0}";
        BarDropsText.Text = $"{Num(status, "drops", 0):0}/{Num(status, "underruns", 0):0}";
        TrimText.Text = $"{Num(status, "trims", 0):0}";

        _muted = Bool(status, "muted");
        MuteButton.Content = _muted ? "取消静音" : "静音";

        ApplySystemMetrics(status);
        ApplyDspStatus(status);

        var messages = new List<string>();
        if (status.TryGetProperty("messages", out var m) && m.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in m.EnumerateArray())
                messages.Add(item.GetString() ?? "");
        }
        var cache = string.Join("\n", messages);
        if (cache != _messageCache)
        {
            _messageCache = cache;
            _messages.Clear();
            foreach (var line in messages) _messages.Add(line);
            if (messages.Count > 0) MessageList.ScrollIntoView(messages[^1]);
        }

        string fault = Str(status, "fault");
        if (fault.Length > 0) ShowError(fault);

        BusyRing.IsActive = _busy || loading;
        StartButton.IsEnabled = _connected && !running && !loading;
        StopButton.IsEnabled = _connected && (running || loading);
        MuteButton.IsEnabled = _connected && running;
    }

    /// <summary>把引擎侧的真实压缩量 / 门状态显示出来，方便对准阈值。</summary>
    private void ApplyDspStatus(JsonElement status)
    {
        if (!status.TryGetProperty("dsp", out var dsp) || dsp.ValueKind != JsonValueKind.Object)
            return;
        bool on = Bool(dsp, "enabled");
        double gr = Num(dsp, "grDb", 0);
        if (!on)
        {
            CompGrText.Text = "压缩量 —";
            GateStateText.Text = "门状态 —";
            return;
        }
        CompGrText.Text = CompSwitch.IsOn ? $"压缩量 −{gr:0.0} dB" : "压缩量 —（未启用）";
        GateStateText.Text = GateSwitch.IsOn
            ? (Bool(dsp, "gateOpen") ? "门状态：开启（信号通过）" : "门状态：关闭（已衰减）")
            : "门状态 —（未启用）";
        int nan = (int)Num(dsp, "nanBlocks", 0);
        if (nan > 0) DspStatusText.Text = $"顺序：高通 → 低通 → 噪声门 → 压缩 → 三段 EQ → VST3 链 → 限幅（异常块 {nan}）";
    }

    private static double MeterValue(double db) => Math.Clamp((db + 60) / 60, 0, 1);

    private static string DbText(double db)
        => db <= -70 ? "−∞ dB" : $"{db:0.0} dB";

    private void ApplySystemMetrics(JsonElement status)
    {
        if (!status.TryGetProperty("system", out var system) || system.ValueKind != JsonValueKind.Object)
            return;
        double cpu = Num(system, "cpu", 0);
        double ram = Num(system, "ram", 0);
        double gpu = Num(system, "gpu", -1);
        double ramUsed = Num(system, "ramUsedMb", 0);
        string gpuName = Str(system, "gpuName");

        CpuBar.Value = Math.Clamp(cpu, 0, 100);
        CpuText.Text = $"{cpu:0}%";
        BarCpuText.Text = $"{cpu:0}%";
        RamBar.Value = Math.Clamp(ram, 0, 100);
        RamText.Text = $"{ram:0}%";
        if (gpu >= 0)
        {
            GpuBar.Value = Math.Clamp(gpu, 0, 100);
            GpuText.Text = $"{gpu:0}%";
            BarGpuText.Text = $"{gpu:0}%";
        }
        else
        {
            // -1 means nvidia-smi is unavailable: say so instead of showing 0%,
            // which would look like an idle GPU on an AMD/Intel machine.
            GpuBar.Value = 0;
            GpuText.Text = "无";
            BarGpuText.Text = "无";
        }
        SystemDetailText.Text = (gpuName.Length > 0 ? gpuName + " · " : "") +
                                $"内存占用 {ramUsed:0} MB" + (gpu >= 0 ? "" : " · 未检测到 NVIDIA GPU");
    }

    // ------------------------------------------------------------------
    // layout
    // ------------------------------------------------------------------
    /// <summary>TabView 的内容区按内容高度测量，这里让插件/预设页的卡片铺满窗口。</summary>
    private void MainTabs_SizeChanged(object sender, SizeChangedEventArgs e) => UpdatePageHeights();

    private void RootLayout_Loaded(object sender, RoutedEventArgs e)
    {
        // 首次布局还没结束时 ActualHeight 为 0，排到下一个调度周期再算一次。
        DispatcherQueue.TryEnqueue(UpdatePageHeights);
    }

    private void UpdatePageHeights()
    {
        double available = PagesRoot.ActualHeight;
        if (available <= 0) return;
        double min = Math.Max(360, available - 78);
        PluginChainCard.MinHeight = min;
        PluginParamsCard.MinHeight = min;
        PresetCard.MinHeight = min;
    }

    // ------------------------------------------------------------------
    // navigation
    // ------------------------------------------------------------------
    private void Nav_SelectionChanged(NavigationView sender, NavigationViewSelectionChangedEventArgs args)
    {
        string tag = (args.SelectedItemContainer?.Tag as string) ?? "live";
        ShowPage(tag);
    }

    private void ShowPage(string tag)
    {
        var pages = new (string Tag, Grid Page)[]
        {
            ("live", PageLive), ("fx", PageFx), ("plugins", PagePlugins),
            ("market", PageMarket), ("presets", PagePresets), ("help", PageHelp),
        };
        foreach (var (pageTag, page) in pages)
            page.Visibility = pageTag == tag ? Visibility.Visible : Visibility.Collapsed;

        if (tag == "market" && _market.Count == 0)
            _ = LoadMarketAsync(false);
    }

    // ------------------------------------------------------------------
    // plugin market
    // ------------------------------------------------------------------
    private async Task LoadMarketAsync(bool force)
    {
        if (_marketLoading || !_connected) return;
        _marketLoading = true;
        MarketStatusText.Text = "正在从远程目录获取…";
        try
        {
            var result = await _client.CallAsync("market", new JsonObject { ["force"] = force }, 30000);
            var catalog = result.GetProperty("catalog");
            var installedSet = new HashSet<string>();
            if (result.TryGetProperty("installed", out var installed) && installed.ValueKind == JsonValueKind.Array)
            {
                foreach (var item in installed.EnumerateArray())
                    installedSet.Add(Str(item, "slug"));
            }

            _market.Clear();
            if (catalog.TryGetProperty("plugins", out var plugins) && plugins.ValueKind == JsonValueKind.Array)
            {
                foreach (var p in plugins.EnumerateArray())
                {
                    string slug = Str(p, "slug");
                    string vendor = Str(p, "vendor");
                    string version = Str(p, "version");
                    string license = Str(p, "license");
                    double size = Num(p, "sizeMb", 0);
                    var parts = new List<string>();
                    if (vendor.Length > 0) parts.Add(vendor);
                    if (version.Length > 0) parts.Add($"v{version}");
                    if (size > 0) parts.Add($"{size:0.#} MB");

                    _market.Add(new MarketItem
                    {
                        Slug = slug,
                        Name = Str(p, "name"),
                        Badge = license.Length > 0 ? license : "VST3",
                        Subtitle = string.Join(" · ", parts),
                        Summary = Str(p, "summary"),
                        Installed = installedSet.Contains(slug),
                        CanInstall = Str(p, "type") == "zip" && Str(p, "download").Length > 0,
                    });
                }
            }

            bool empty = _market.Count == 0;
            MarketEmpty.Visibility = empty ? Visibility.Visible : Visibility.Collapsed;
            MarketList.Visibility = empty ? Visibility.Collapsed : Visibility.Visible;

            string error = Str(catalog, "error");
            bool cached = catalog.TryGetProperty("cached", out var c) && c.ValueKind == JsonValueKind.True;
            MarketSourceText.Text = $"目录：{Str(catalog, "source")}" + (cached ? "（本地缓存）" : "");
            MarketStatusText.Text = error.Length > 0
                ? error
                : $"共 {_market.Count} 个条目 · 已安装 {installedSet.Count} 个 · 安装目录 {Str(result, "userDir")}";
        }
        catch (Exception ex)
        {
            MarketStatusText.Text = ex.Message;
        }
        finally
        {
            _marketLoading = false;
        }
    }

    private async void MarketRefresh_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        SetBusy(true, "正在刷新插件目录…");
        try
        {
            await LoadMarketAsync(true);
        }
        finally
        {
            SetBusy(false);
        }
    }

    private MarketItem? FindMarketItem(object? tag)
        => _market.FirstOrDefault(m => m.Slug == tag as string);

    private async void MarketAction_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected || sender is not Button button) return;
        var item = FindMarketItem(button.Tag);
        if (item is null) return;

        if (!item.Installed && !item.CanInstall)
        {
            MarketOpen_Click(sender, e);
            return;
        }
        if (_installInProgress) return;
        _installInProgress = true;
        SetBusy(true, item.Installed ? "正在卸载…" : "正在下载并安装…");
        try
        {
            if (item.Installed)
            {
                await _client.CallAsync("market_uninstall", new JsonObject { ["slug"] = item.Slug }, 30000);
                ShowError($"已卸载 {item.Name}", InfoBarSeverity.Success);
            }
            else
            {
                await _client.CallAsync("market_install", new JsonObject { ["slug"] = item.Slug }, 300000);
                ShowError($"{item.Name} 已安装，可在「VST3 插件」页扫描后加入链", InfoBarSeverity.Success);
            }
            await LoadMarketAsync(true);
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
        finally
        {
            _installInProgress = false;
            SetBusy(false);
        }
    }

    private async void MarketOpen_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected || sender is not Button button) return;
        var item = FindMarketItem(button.Tag);
        if (item is null) return;
        try
        {
            await _client.CallAsync("market_open", new JsonObject { ["slug"] = item.Slug }, 15000);
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
    }

    // ------------------------------------------------------------------
    // update
    // ------------------------------------------------------------------
    private async void UpdateCheck_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        UpdateStatusText.Text = "正在检查更新…";
        try
        {
            var info = await _client.CallAsync("update_check", null, 20000);
            if (!Bool(info, "ok"))
            {
                UpdateStatusText.Text = Str(info, "error");
                return;
            }
            _updateInfo = info.Clone();
            _updateAvailable = Bool(info, "available");
            UpdateInstallButton.IsEnabled = _updateAvailable;
            UpdateStatusText.Text = _updateAvailable
                ? $"发现新版本 {Str(info, "latest")}（当前 {Str(info, "current")}），下载后会自动校验收据。"
                : $"已是最新版本（{Str(info, "current")}）。";
        }
        catch (Exception ex)
        {
            UpdateStatusText.Text = ex.Message;
        }
    }

    private async void UpdateInstall_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected || !_updateAvailable) return;
        UpdateInstallButton.IsEnabled = false;
        UpdateProgress.Visibility = Visibility.Visible;
        UpdateProgress.Value = 0;
        UpdateStatusText.Text = "正在下载更新包…";
        try
        {
            var result = await _client.CallAsync("update_download", null, 600000);
            _updateFile = Str(result.GetProperty("download"), "file");
            string sha = Str(result.GetProperty("download"), "sha256");
            UpdateProgress.Value = 100;
            UpdateStatusText.Text = $"已下载并通过 SHA-256 校验（{sha[..12]}…）。";

            var dialog = new ContentDialog
            {
                XamlRoot = Content.XamlRoot,
                Title = "立即安装更新",
                Content = "更新会关闭当前程序，替换程序文件后自动重启；失败时自动回滚到当前版本。",
                PrimaryButtonText = "现在安装",
                CloseButtonText = "稍后",
                DefaultButton = ContentDialogButton.Primary,
            };
            if (await dialog.ShowAsync() != ContentDialogResult.Primary)
            {
                UpdateInstallButton.IsEnabled = true;
                return;
            }
            await _client.CallAsync("update_apply", new JsonObject { ["file"] = _updateFile }, 20000);
        }
        catch (Exception ex)
        {
            UpdateStatusText.Text = ex.Message;
            UpdateInstallButton.IsEnabled = true;
        }
        finally
        {
            UpdateProgress.Visibility = Visibility.Collapsed;
        }
    }

    private void UpdatePage_Click(object sender, RoutedEventArgs e)
    {
        string page = _updateInfo.ValueKind == JsonValueKind.Object ? Str(_updateInfo, "page") : "";
        if (page.Length == 0) page = "https://github.com/HandaQAQ-Code/Intro-Voice/releases";
        try
        {
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(page) { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
    }

    // ------------------------------------------------------------------
    // built-in DSP
    // ------------------------------------------------------------------
    /// <summary>滑块区间必须在代码里设置：XAML 直接给负 Minimum 会与默认 Maximum 冲突。</summary>
    private void SetupDspControls()
    {
        SetRange(HpfFreqSlider, 20, 400);
        SetRange(LpfFreqSlider, 2000, 20000);
        SetRange(EqLowSlider, -18, 18);
        SetRange(EqMidSlider, -18, 18);
        SetRange(EqHighSlider, -18, 18);
        SetRange(EqMidFreqSlider, 200, 6000);
        SetRange(EqMidQSlider, 0.2, 6);
        SetRange(CompThreshSlider, -60, 0);
        SetRange(CompRatioSlider, 1, 20);
        SetRange(CompAttackSlider, 0.5, 100);
        SetRange(CompReleaseSlider, 10, 600);
        SetRange(CompMakeupSlider, -12, 24);
        SetRange(GateThreshSlider, -80, -20);
        SetRange(GateHoldSlider, 0, 300);
        SetRange(GateReleaseSlider, 20, 600);
        SetRange(GateRangeSlider, -60, 0);

        var slopes = new[] { "12 dB/oct", "24 dB/oct" };
        HpfSlopeCombo.ItemsSource = slopes;
        LpfSlopeCombo.ItemsSource = slopes;
        HpfSlopeCombo.SelectedIndex = 0;
        LpfSlopeCombo.SelectedIndex = 0;

        ApplyDsp(JsonSerializer.SerializeToElement(BuildDspParams()));
    }

    private static void SetRange(Slider slider, double min, double max)
    {
        slider.Maximum = max;
        slider.Minimum = min;
    }

    private void ApplyDsp(JsonElement dsp)
    {
        if (dsp.ValueKind != JsonValueKind.Object) return;
        _dspSyncing = true;
        try
        {
            DspMasterSwitch.IsOn = Bool(dsp, "enabled");
            if (dsp.TryGetProperty("hpf", out var hpf))
            {
                HpfSwitch.IsOn = Bool(hpf, "enabled");
                HpfFreqSlider.Value = Math.Clamp(Num(hpf, "freq", 80), 20, 400);
                HpfSlopeCombo.SelectedIndex = (int)Num(hpf, "slope", 12) == 24 ? 1 : 0;
            }
            if (dsp.TryGetProperty("lpf", out var lpf))
            {
                LpfSwitch.IsOn = Bool(lpf, "enabled");
                LpfFreqSlider.Value = Math.Clamp(Num(lpf, "freq", 16000), 2000, 20000);
                LpfSlopeCombo.SelectedIndex = (int)Num(lpf, "slope", 12) == 24 ? 1 : 0;
            }
            if (dsp.TryGetProperty("eq", out var eq))
            {
                EqSwitch.IsOn = Bool(eq, "enabled");
                EqLowSlider.Value = Math.Clamp(Num(eq, "lowDb", 0), -18, 18);
                EqMidSlider.Value = Math.Clamp(Num(eq, "midDb", 0), -18, 18);
                EqHighSlider.Value = Math.Clamp(Num(eq, "highDb", 0), -18, 18);
                EqMidFreqSlider.Value = Math.Clamp(Num(eq, "midFreq", 1000), 200, 6000);
                EqMidQSlider.Value = Math.Clamp(Num(eq, "midQ", 1), 0.2, 6);
            }
            if (dsp.TryGetProperty("comp", out var comp))
            {
                CompSwitch.IsOn = Bool(comp, "enabled");
                CompThreshSlider.Value = Math.Clamp(Num(comp, "thresholdDb", -18), -60, 0);
                CompRatioSlider.Value = Math.Clamp(Num(comp, "ratio", 3), 1, 20);
                CompAttackSlider.Value = Math.Clamp(Num(comp, "attackMs", 8), 0.5, 100);
                CompReleaseSlider.Value = Math.Clamp(Num(comp, "releaseMs", 120), 10, 600);
                CompMakeupSlider.Value = Math.Clamp(Num(comp, "makeupDb", 0), -12, 24);
            }
            if (dsp.TryGetProperty("gate", out var gate))
            {
                GateSwitch.IsOn = Bool(gate, "enabled");
                GateThreshSlider.Value = Math.Clamp(Num(gate, "thresholdDb", -50), -80, -20);
                GateHoldSlider.Value = Math.Clamp(Num(gate, "holdMs", 80), 0, 300);
                GateReleaseSlider.Value = Math.Clamp(Num(gate, "releaseMs", 150), 20, 600);
                GateRangeSlider.Value = Math.Clamp(Num(gate, "rangeDb", -18), -60, 0);
            }
        }
        finally
        {
            _dspSyncing = false;
        }
        UpdateDspLabels();
        UpdateDspControls();
    }

    private JsonObject BuildDspParams() => new()
    {
        ["enabled"] = DspMasterSwitch.IsOn,
        ["hpf"] = new JsonObject
        {
            ["enabled"] = HpfSwitch.IsOn,
            ["freq"] = HpfFreqSlider.Value,
            ["slope"] = HpfSlopeCombo.SelectedIndex == 1 ? 24 : 12,
        },
        ["lpf"] = new JsonObject
        {
            ["enabled"] = LpfSwitch.IsOn,
            ["freq"] = LpfFreqSlider.Value,
            ["slope"] = LpfSlopeCombo.SelectedIndex == 1 ? 24 : 12,
        },
        ["eq"] = new JsonObject
        {
            ["enabled"] = EqSwitch.IsOn,
            ["lowDb"] = EqLowSlider.Value,
            ["midDb"] = EqMidSlider.Value,
            ["highDb"] = EqHighSlider.Value,
            ["midFreq"] = EqMidFreqSlider.Value,
            ["midQ"] = EqMidQSlider.Value,
        },
        ["comp"] = new JsonObject
        {
            ["enabled"] = CompSwitch.IsOn,
            ["thresholdDb"] = CompThreshSlider.Value,
            ["ratio"] = CompRatioSlider.Value,
            ["attackMs"] = CompAttackSlider.Value,
            ["releaseMs"] = CompReleaseSlider.Value,
            ["makeupDb"] = CompMakeupSlider.Value,
        },
        ["gate"] = new JsonObject
        {
            ["enabled"] = GateSwitch.IsOn,
            ["thresholdDb"] = GateThreshSlider.Value,
            ["holdMs"] = GateHoldSlider.Value,
            ["releaseMs"] = GateReleaseSlider.Value,
            ["rangeDb"] = GateRangeSlider.Value,
        },
    };

    private void UpdateDspLabels()
    {
        HpfFreqText.Text = $"{HpfFreqSlider.Value:0} Hz";
        LpfFreqText.Text = $"{LpfFreqSlider.Value:0} Hz";
        EqLowText.Text = $"{EqLowSlider.Value:+0.0;-0.0;0.0} dB";
        EqMidText.Text = $"{EqMidSlider.Value:+0.0;-0.0;0.0} dB";
        EqHighText.Text = $"{EqHighSlider.Value:+0.0;-0.0;0.0} dB";
        EqMidFreqText.Text = $"{EqMidFreqSlider.Value:0} Hz";
        EqMidQText.Text = $"{EqMidQSlider.Value:0.0}";
        CompThreshText.Text = $"{CompThreshSlider.Value:0} dB";
        CompRatioText.Text = $"{CompRatioSlider.Value:0.0} : 1";
        CompAttackText.Text = $"{CompAttackSlider.Value:0.#} ms";
        CompReleaseText.Text = $"{CompReleaseSlider.Value:0} ms";
        CompMakeupText.Text = $"{CompMakeupSlider.Value:+0.0;-0.0;0.0} dB";
        GateThreshText.Text = $"{GateThreshSlider.Value:0} dB";
        GateHoldText.Text = $"{GateHoldSlider.Value:0} ms";
        GateReleaseText.Text = $"{GateReleaseSlider.Value:0} ms";
        GateRangeText.Text = $"{GateRangeSlider.Value:0} dB";
    }

    private void UpdateDspControls()
    {
        bool master = DspMasterSwitch.IsOn;
        bool hpf = master && HpfSwitch.IsOn;
        bool lpf = master && LpfSwitch.IsOn;
        bool eq = master && EqSwitch.IsOn;
        bool comp = master && CompSwitch.IsOn;
        bool gate = master && GateSwitch.IsOn;

        HpfSwitch.IsEnabled = master;
        HpfFreqSlider.IsEnabled = hpf;
        HpfSlopeCombo.IsEnabled = hpf;
        LpfSwitch.IsEnabled = master;
        LpfFreqSlider.IsEnabled = lpf;
        LpfSlopeCombo.IsEnabled = lpf;

        EqSwitch.IsEnabled = master;
        EqLowSlider.IsEnabled = eq;
        EqMidSlider.IsEnabled = eq;
        EqHighSlider.IsEnabled = eq;
        EqMidFreqSlider.IsEnabled = eq;
        EqMidQSlider.IsEnabled = eq;

        CompSwitch.IsEnabled = master;
        CompThreshSlider.IsEnabled = comp;
        CompRatioSlider.IsEnabled = comp;
        CompAttackSlider.IsEnabled = comp;
        CompReleaseSlider.IsEnabled = comp;
        CompMakeupSlider.IsEnabled = comp;

        GateSwitch.IsEnabled = master;
        GateThreshSlider.IsEnabled = gate;
        GateHoldSlider.IsEnabled = gate;
        GateReleaseSlider.IsEnabled = gate;
        GateRangeSlider.IsEnabled = gate;

        if (!master)
        {
            CompGrText.Text = "压缩量 —";
            GateStateText.Text = "门状态 —";
        }
    }

    private void Dsp_Toggled(object sender, RoutedEventArgs e)
    {
        UpdateDspControls();
        QueueDspApply();
    }

    private void Dsp_Changed(object sender, Microsoft.UI.Xaml.Controls.Primitives.RangeBaseValueChangedEventArgs e)
    {
        UpdateDspLabels();
        QueueDspApply();
    }

    private void DspSlope_Changed(object sender, SelectionChangedEventArgs e) => QueueDspApply();

    /// <summary>恢复出厂参数：本地立即生效，已连接时同步给引擎。</summary>
    private async void ResetDsp_Click(object sender, RoutedEventArgs e)
    {
        var defaults = DefaultDspParams();
        ApplyDsp(JsonSerializer.SerializeToElement(defaults));
        if (!_connected) return;
        try
        {
            await _client.CallAsync("dsp", new JsonObject { ["params"] = defaults }, 10000);
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
    }

    private static JsonObject DefaultDspParams() => new()
    {
        ["enabled"] = false,
        ["hpf"] = new JsonObject { ["enabled"] = false, ["freq"] = 80.0, ["slope"] = 12 },
        ["lpf"] = new JsonObject { ["enabled"] = false, ["freq"] = 16000.0, ["slope"] = 12 },
        ["eq"] = new JsonObject
        {
            ["enabled"] = false, ["lowDb"] = 0.0, ["midDb"] = 0.0, ["highDb"] = 0.0,
            ["midFreq"] = 1000.0, ["midQ"] = 1.0,
        },
        ["comp"] = new JsonObject
        {
            ["enabled"] = false, ["thresholdDb"] = -18.0, ["ratio"] = 3.0,
            ["attackMs"] = 8.0, ["releaseMs"] = 120.0, ["makeupDb"] = 0.0,
        },
        ["gate"] = new JsonObject
        {
            ["enabled"] = false, ["thresholdDb"] = -50.0, ["holdMs"] = 80.0,
            ["releaseMs"] = 150.0, ["rangeDb"] = -18.0,
        },
    };

    private void QueueDspApply()
    {
        if (_dspSyncing || !_connected) return;
        _dspDirty = true;
        _dspTimer.Stop();
        _dspTimer.Start();
    }

    private async void ApplyDspParams(object? sender, object args)
    {
        if (!_dspDirty || !_connected) return;
        _dspDirty = false;
        try
        {
            // 回包里的参数就是刚发出去的那份，不必回填控件（会打断正在拖动的滑块）。
            await _client.CallAsync("dsp", new JsonObject { ["params"] = BuildDspParams() }, 10000);
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
    }

    // ------------------------------------------------------------------
    // plugin chain
    // ------------------------------------------------------------------
    private PluginItem? SelectedPlugin => ChainList.SelectedItem as PluginItem;

    private void UpdateChainButtons()
    {
        var plugin = SelectedPlugin;
        bool has = plugin is not null;
        EditorButton.IsEnabled = has;
        BypassButton.IsEnabled = has;
        RemoveButton.IsEnabled = has;
        UpButton.IsEnabled = has && plugin!.Index > 0;
        DownButton.IsEnabled = has && plugin!.Index < _plugins.Count - 1;
    }

    private async void ChainList_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        UpdateChainButtons();
        await LoadParamsAsync();
    }

    private async Task LoadParamsAsync()
    {
        var plugin = SelectedPlugin;
        if (plugin is null || !_connected)
        {
            ParamList.ItemsSource = null;
            ParamsTitle.Text = "插件参数";
            ParamEmpty.Visibility = Visibility.Visible;
            return;
        }
        try
        {
            var result = await _client.CallAsync("plugin_params",
                new JsonObject { ["index"] = plugin.Index }, 30000);
            _suppressParam = true;
            var items = new List<VstParamItem>();
            if (result.TryGetProperty("params", out var array) && array.ValueKind == JsonValueKind.Array)
            {
                foreach (var p in array.EnumerateArray())
                {
                    string title = Str(p, "title");
                    if (title.Length == 0) title = Str(p, "shortTitle");
                    int flags = (int)Num(p, "flags", 0);
                    items.Add(new VstParamItem
                    {
                        PluginIndex = plugin.Index,
                        ParamIndex = (int)Num(p, "index", 0),
                        ParamId = (uint)Num(p, "id", 0),
                        Name = title.Length > 0 ? title : $"参数 {Num(p, "index", 0):0}",
                        ReadOnly = (flags & 2) != 0,
                        Value = Math.Clamp(Num(p, "value", 0), 0, 1),
                    });
                }
            }
            ParamList.ItemsSource = items;
            ParamEmpty.Visibility = items.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
            ParamsTitle.Text = $"插件参数 · {plugin.Name}（{items.Count}）";
            _suppressParam = false;
            _pendingParams.Clear();
        }
        catch (Exception ex)
        {
            _suppressParam = false;
            ShowError(ex.Message);
        }
    }

    private void ParamSlider_ValueChanged(object sender, Microsoft.UI.Xaml.Controls.Primitives.RangeBaseValueChangedEventArgs e)
    {
        if (_suppressParam || !_connected) return;
        if (sender is not Slider slider || slider.DataContext is not VstParamItem item) return;
        // The slider binds one-way from the model; a real move is the only way
        // the incoming value can differ from the model value.
        if (Math.Abs(item.Value - e.NewValue) < 1e-6) return;
        item.Value = e.NewValue;
        _pendingParams[(item.PluginIndex, item.ParamIndex)] = e.NewValue;
    }

    private async void FlushParams(object? sender, object args)
    {
        if (_flushingParams || !_connected || _pendingParams.Count == 0) return;
        _flushingParams = true;
        try
        {
            var pending = _pendingParams.ToList();
            _pendingParams.Clear();
            foreach (var entry in pending)
            {
                await _client.CallAsync("plugin_param", new JsonObject
                {
                    ["index"] = entry.Key.plugin,
                    ["param"] = entry.Key.param,
                    ["value"] = entry.Value,
                }, 10000);
            }
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
        finally
        {
            _flushingParams = false;
        }
    }

    private async void Editor_Click(object sender, RoutedEventArgs e)
    {
        var plugin = SelectedPlugin;
        if (plugin is null || !_connected) return;
        try
        {
            var result = await _client.CallAsync("plugin_open_editor",
                new JsonObject { ["index"] = plugin.Index }, 30000);
            long hwnd = (long)Num(result, "hwnd", 0);
            if (hwnd <= 0)
                ShowError("该插件没有提供可嵌入的编辑器视图", InfoBarSeverity.Warning);
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
    }

    private async void Bypass_Click(object sender, RoutedEventArgs e)
    {
        var plugin = SelectedPlugin;
        if (plugin is null || !_connected) return;
        try
        {
            var result = await _client.CallAsync("plugin_bypass", new JsonObject
            {
                ["index"] = plugin.Index,
                ["bypass"] = !plugin.Bypass,
            }, 10000);
            ApplyPlugins(result.GetProperty("plugins"));
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
    }

    private async void Up_Click(object sender, RoutedEventArgs e) => await MovePluginAsync(-1);

    private async void Down_Click(object sender, RoutedEventArgs e) => await MovePluginAsync(1);

    private async Task MovePluginAsync(int delta)
    {
        var plugin = SelectedPlugin;
        if (plugin is null || !_connected) return;
        int target = plugin.Index + delta;
        if (target < 0 || target >= _plugins.Count) return;
        try
        {
            var result = await _client.CallAsync("plugin_move", new JsonObject
            {
                ["from"] = plugin.Index,
                ["to"] = target,
            }, 10000);
            _suppressParam = true;
            _plugins.Clear();
            foreach (var item in result.GetProperty("plugins").EnumerateArray())
            {
                _plugins.Add(new PluginItem
                {
                    Index = (int)Num(item, "index", _plugins.Count),
                    Name = Str(item, "name"),
                    Vendor = Str(item, "vendor"),
                    Path = Str(item, "path"),
                    ClassId = Str(item, "classId"),
                    Bypass = Bool(item, "bypass"),
                    ParamCount = (int)Num(item, "paramCount", 0),
                });
            }
            _suppressParam = false;
            ChainList.SelectedItem = _plugins.FirstOrDefault(p => p.Index == target);
            UpdateChainButtons();
            await LoadParamsAsync();
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
    }

    private async void Remove_Click(object sender, RoutedEventArgs e)
    {
        var plugin = SelectedPlugin;
        if (plugin is null || !_connected) return;
        try
        {
            var result = await _client.CallAsync("plugin_remove",
                new JsonObject { ["index"] = plugin.Index }, 10000);
            ApplyPlugins(result.GetProperty("plugins"));
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
    }

    // ------------------------------------------------------------------
    // plugin scan dialog
    // ------------------------------------------------------------------
    private async void Scan_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        var directoryBox = new TextBox
        {
            PlaceholderText = "可选：额外 VST3 目录，多个用分号分隔；留空扫描系统标准目录",
            Margin = new Thickness(0, 0, 0, 8),
        };
        var scanButton = new Button { Content = "扫描" };
        var ring = new ProgressRing { Width = 20, Height = 20, IsActive = false };
        var statusText = new TextBlock { TextWrapping = TextWrapping.Wrap, VerticalAlignment = VerticalAlignment.Center };
        var list = new ListView { Height = 300, SelectionMode = ListViewSelectionMode.Single };
        var dialog = new ContentDialog
        {
            XamlRoot = Content.XamlRoot,
            Title = "添加 VST3 插件",
            PrimaryButtonText = "添加到链",
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Primary,
            IsPrimaryButtonEnabled = false,
            Content = new StackPanel
            {
                Spacing = 6,
                Children =
                {
                    directoryBox,
                    new StackPanel
                    {
                        Orientation = Orientation.Horizontal,
                        Spacing = 8,
                        Children = { scanButton, ring, statusText },
                    },
                    list,
                },
            },
        };

        async Task RunScan()
        {
            scanButton.IsEnabled = false;
            ring.IsActive = true;
            statusText.Text = "正在扫描；插件模块在独立子进程中加载，可能需要数十秒…";
            try
            {
                JsonObject? args = null;
                var dirs = directoryBox.Text.Split(new[] { ';', ',' }, StringSplitOptions.RemoveEmptyEntries)
                    .Select(s => s.Trim()).Where(s => s.Length > 0).ToList();
                if (dirs.Count > 0)
                    args = new JsonObject
                    {
                        ["dirs"] = new JsonArray(dirs.Select(d => JsonValue.Create(d)).ToArray()),
                    };
                var result = await _client.CallAsync("scan", args, 180000);
                var items = new List<PluginScanItem>();
                if (result.TryGetProperty("plugins", out var array) && array.ValueKind == JsonValueKind.Array)
                {
                    foreach (var p in array.EnumerateArray())
                    {
                        items.Add(new PluginScanItem
                        {
                            Name = Str(p, "name"),
                            Vendor = Str(p, "vendor"),
                            Path = Str(p, "path"),
                            ClassId = Str(p, "classId"),
                        });
                    }
                }
                items.Sort((a, b) => string.Compare(a.Name, b.Name, StringComparison.OrdinalIgnoreCase));
                list.ItemsSource = items;
                long errors = 0;
                if (result.TryGetProperty("errors", out var e) && e.ValueKind == JsonValueKind.Array)
                    errors = e.GetArrayLength();
                statusText.Text = $"发现 {items.Count} 个效果器；{errors} 个模块报告错误。";
            }
            catch (Exception ex)
            {
                statusText.Text = ex.Message;
            }
            finally
            {
                ring.IsActive = false;
                scanButton.IsEnabled = true;
            }
        }

        scanButton.Click += async (_, _) => await RunScan();
        list.SelectionChanged += (_, _) => dialog.IsPrimaryButtonEnabled = list.SelectedItem is PluginScanItem;

        dialog.PrimaryButtonClick += async (_, buttonArgs) =>
        {
            var deferral = buttonArgs.GetDeferral();
            try
            {
                if (list.SelectedItem is not PluginScanItem selected) return;
                var result = await _client.CallAsync("plugin_add", new JsonObject
                {
                    ["path"] = selected.Path,
                    ["classId"] = selected.ClassId,
                }, 60000);
                ApplyPlugins(result.GetProperty("plugins"));
                ChainList.SelectedItem = _plugins.LastOrDefault();
                await LoadParamsAsync();
            }
            catch (Exception ex)
            {
                buttonArgs.Cancel = true;
                statusText.Text = ex.Message;
            }
            finally
            {
                deferral.Complete();
            }
        };

        await dialog.ShowAsync();
    }

    // ------------------------------------------------------------------
    // presets
    // ------------------------------------------------------------------
    private async Task RefreshPresets(string? select = null)
    {
        try
        {
            var result = await _client.CallAsync("preset_list");
            var items = new List<PresetItem>();
            if (result.TryGetProperty("presets", out var array) && array.ValueKind == JsonValueKind.Array)
            {
                foreach (var p in array.EnumerateArray())
                {
                    // 兼容旧服务：字符串数组；新服务返回 {name, savedAt}。
                    if (p.ValueKind == JsonValueKind.String)
                    {
                        items.Add(new PresetItem { Name = p.GetString() ?? "" });
                    }
                    else if (p.ValueKind == JsonValueKind.Object)
                    {
                        items.Add(new PresetItem
                        {
                            Name = Str(p, "name"),
                            SavedAt = Str(p, "savedAt"),
                        });
                    }
                }
            }
            _presets.Clear();
            foreach (var item in items.Where(n => n.Name.Length > 0))
                _presets.Add(item);
            var wanted = select ?? (PresetList.SelectedItem as PresetItem)?.Name;
            PresetList.SelectedItem = _presets.FirstOrDefault(p => p.Name == wanted);
            PresetEmpty.Visibility = _presets.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
            PresetList.Visibility = _presets.Count == 0 ? Visibility.Collapsed : Visibility.Visible;
            PresetCountText.Text = _presets.Count == 0 ? "还没有预设" : $"{_presets.Count} 套预设";
            PresetBadge.Value = _presets.Count;
            PresetBadge.Visibility = _presets.Count == 0 ? Visibility.Collapsed : Visibility.Visible;
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
    }

    private void PresetList_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (PresetList.SelectedItem is PresetItem item && PresetNameBox.Text.Trim().Length == 0)
            PresetNameBox.Text = item.Name;
    }

    private async void PresetSave_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        var name = PresetNameBox.Text.Trim();
        if (name.Length == 0) name = (PresetList.SelectedItem as PresetItem)?.Name ?? "";
        if (name.Length == 0)
        {
            ShowError("请填写预设名称", InfoBarSeverity.Warning);
            return;
        }
        SetBusy(true, "正在保存预设…");
        try
        {
            await _client.CallAsync("preset_save", new JsonObject { ["name"] = name }, 30000);
            PresetNameBox.Text = "";
            await RefreshPresets(name);
            ShowError($"预设“{name}”已保存", InfoBarSeverity.Success);
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
        finally
        {
            SetBusy(false);
        }
    }

    private async void PresetLoad_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        var name = (PresetList.SelectedItem as PresetItem)?.Name ?? PresetNameBox.Text.Trim();
        if (name.Length == 0)
        {
            ShowError("请选择或填写要载入的预设", InfoBarSeverity.Warning);
            return;
        }
        SetBusy(true, "正在载入预设…");
        try
        {
            var result = await _client.CallAsync("preset_load", new JsonObject { ["name"] = name }, 60000);
            ApplyPlugins(result.GetProperty("plugins"));
            if (result.TryGetProperty("settings", out var settings))
                ApplySettings(settings);
            await RefreshPresets(name);
            ShowError($"预设“{name}”已载入", InfoBarSeverity.Success);
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
        finally
        {
            SetBusy(false);
        }
    }

    private async void PresetDelete_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        var name = (PresetList.SelectedItem as PresetItem)?.Name ?? PresetNameBox.Text.Trim();
        if (name.Length == 0)
        {
            ShowError("请选择要删除的预设", InfoBarSeverity.Warning);
            return;
        }
        try
        {
            await _client.CallAsync("preset_delete", new JsonObject { ["name"] = name }, 10000);
            await RefreshPresets();
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
    }

    // ------------------------------------------------------------------
    // helpers
    // ------------------------------------------------------------------
    private void SetBusy(bool busy, string? status = null)
    {
        _busy = busy;
        BusyRing.IsActive = busy;
        if (status is not null && !_connected) ServiceText.Text = status;
    }

    private void ShowError(string message, InfoBarSeverity severity = InfoBarSeverity.Error)
    {
        ErrorBar.Severity = severity;
        ErrorBar.Title = severity switch
        {
            InfoBarSeverity.Success => "完成",
            InfoBarSeverity.Warning => "提示",
            _ => "错误",
        };
        ErrorBar.Message = message;
        ErrorBar.IsOpen = true;
    }

    private void ErrorBar_Closed(object sender, InfoBarClosedEventArgs args)
    {
        ErrorBar.Message = "";
    }

    private static string Str(JsonElement element, string name)
    {
        if (!element.TryGetProperty(name, out var value)) return "";
        return value.ValueKind == JsonValueKind.String ? value.GetString() ?? "" : "";
    }

    private static double Num(JsonElement element, string name, double fallback)
    {
        if (!element.TryGetProperty(name, out var value)) return fallback;
        try
        {
            return value.ValueKind switch
            {
                JsonValueKind.Number => value.GetDouble(),
                JsonValueKind.String => double.TryParse(value.GetString(), out var parsed) ? parsed : fallback,
                _ => fallback,
            };
        }
        catch
        {
            return fallback;
        }
    }

    private static bool Bool(JsonElement element, string name)
    {
        if (!element.TryGetProperty(name, out var value)) return false;
        return value.ValueKind == JsonValueKind.True;
    }

    private void OnClosed(object sender, WindowEventArgs args)
    {
        _statusTimer.Stop();
        _paramTimer.Stop();
        _optionTimer.Stop();
        _client.Dispose();
    }
}
