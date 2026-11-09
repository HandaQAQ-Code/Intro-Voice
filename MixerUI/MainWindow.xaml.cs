using System.Collections.ObjectModel;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.WindowsRuntime;
using System.Text.Json;
using WinRT.Interop;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Imaging;
using Microsoft.UI.Xaml.Hosting;
using Microsoft.UI.Composition;
using Windows.ApplicationModel.DataTransfer;
using Windows.Graphics;
using Windows.Storage.Streams;
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
    private readonly DispatcherQueueTimer _micTimer;
    private readonly DispatcherQueueTimer _micSettingTimer;

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
    private bool _micRunning;
    private bool _micPolling;
    private bool _micSyncing;
    private string _micQrKey = "";
    private string _micSource = "device";
    private string _micView = "virtual";
    private DateTime _micSourcePushedAt = DateTime.MinValue;

    public MainWindow()
    {
        InitializeComponent();
        SetupTitleBar();
        App.Trace("mainwindow: xaml loaded");
        // Negative ranges must be set in code: assigning Maximum below the
        // default Minimum (0) is rejected by RangeBase during XAML load.
        FloorSlider.Minimum = -80;
        FloorSlider.Maximum = -20;
        FloorSlider.Value = -55;
        // 手机麦克风的负范围同样只能在代码里设：XAML 先赋值 Maximum 时会被拒绝。
        _micSyncing = true;
        MicGainSlider.Minimum = -24;
        MicGainSlider.Maximum = 24;
        MicGainSlider.StepFrequency = 1;
        MicGainSlider.Value = 0;
        MicHpfSlider.Minimum = 20;
        MicHpfSlider.Maximum = 300;
        MicHpfSlider.StepFrequency = 10;
        MicHpfSlider.Value = 80;
        MicGateSlider.Minimum = -80;
        MicGateSlider.Maximum = -20;
        MicGateSlider.StepFrequency = 1;
        MicGateSlider.Value = -62;
        MicJitterSlider.Minimum = 20;
        MicJitterSlider.Maximum = 300;
        MicJitterSlider.StepFrequency = 10;
        MicJitterSlider.Value = 80;
        _micSyncing = false;
        UpdateMicValueLabels();
        SetupDspControls();
        Title = "Intro Voice 音拓";
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
        PresetCombo.ItemsSource = _presets;
        MarketList.ItemsSource = _market;
        WorkshopList.ItemsSource = _workshop;
        foreach (var (name, _, _, _) in WorkshopTemplates)
            WorkshopTemplateCombo.Items.Add(name);
        foreach (var license in new[] { "MIT", "GPL-3.0", "CC0-1.0", "专有" })
            WsLicenseCombo.Items.Add(license);
        WsLicenseCombo.SelectedIndex = 0;
        WorkshopList.Visibility = Visibility.Collapsed;
        WorkshopEmpty.Visibility = Visibility.Visible;
        MessageList.ItemsSource = _messages;
        // 空状态：没有数据时显示引导文案，列表隐藏。
        ChainList.Visibility = Visibility.Collapsed;
        ParamEmpty.Visibility = Visibility.Visible;
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

        // 手机麦克风页：状态 900 ms 轮询；滑块合并成 220 ms 一次下发。
        _micTimer = DispatcherQueue.CreateTimer();
        _micTimer.Interval = TimeSpan.FromMilliseconds(900);
        _micTimer.Tick += PollMic;
        _micTimer.Start();

        _micSettingTimer = DispatcherQueue.CreateTimer();
        _micSettingTimer.Interval = TimeSpan.FromMilliseconds(220);
        _micSettingTimer.IsRepeating = false;
        _micSettingTimer.Tick += FlushMicSettings;

        ApplyWorkshopMode();
        SetMicView("virtual");
        SetMicSource("device", false);
        Nav.SizeChanged += (_, _) => UpdatePageHeights();
        Closed += OnClosed;
        App.Trace("mainwindow: ctor end");
        _ = ConnectAsync();
        App.Trace("mainwindow: connect started");
    }

    [DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute, ref int value, int size);

    /// <summary>把自绘顶栏变成真正的标题栏，去掉系统标题栏这一层；并抹掉窗口外描边。</summary>
    private void SetupTitleBar()
    {
        try
        {
            ExtendsContentIntoTitleBar = true;
            SetTitleBar(AppDragRegion);
            var bar = AppWindow.TitleBar;
            var transparent = Windows.UI.Color.FromArgb(0, 0, 0, 0);
            bar.ButtonBackgroundColor = transparent;
            bar.ButtonInactiveBackgroundColor = transparent;
            bar.ButtonHoverBackgroundColor = Windows.UI.Color.FromArgb(24, 0, 0, 0);
            bar.ButtonPressedBackgroundColor = Windows.UI.Color.FromArgb(36, 0, 0, 0);

            var hwnd = WindowNative.GetWindowHandle(this);
            int none = unchecked((int)0xFFFFFFFE);  // DWMWA_COLOR_NONE
            DwmSetWindowAttribute(hwnd, 34, ref none, sizeof(int));
        }
        catch
        {
            // 老系统不支持时退回系统标题栏，不影响功能
        }
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
            if (PageMicConfig.Visibility == Visibility.Visible)
                _ = RefreshMicStatusAsync(true);
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
        if (devices.TryGetProperty("virtual", out var virtualStatus))
            ApplyVirtualStatus(virtualStatus);
        UpdateMicDeviceName();
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
        ApplySpectrum(status, running);
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

    /// <summary>把引擎算好的 32 段频谱交给自绘控件；停止时归零。</summary>
    private void ApplySpectrum(JsonElement status, bool running)
    {
        if (!status.TryGetProperty("spectrum", out var array) || array.ValueKind != JsonValueKind.Array)
            return;
        var levels = new List<float>(32);
        foreach (var item in array.EnumerateArray())
            levels.Add((float)item.GetDouble());
        if (levels.Count == 0) return;
        if (!running)
        {
            for (int i = 0; i < levels.Count; i++) levels[i] = 0.06f;
        }
        Spectrum.SetLevels(levels);
        SpectrumHint.Text = running ? "处理后的实时频谱；峰值保持由引擎侧平滑。" : "未运行：频谱静止。";
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
            ("live", PageLive), ("fx", PageFx), ("workshop", PageWorkshop),
            ("mic", PageMicConfig), ("plugins", PagePlugins), ("market", PageMarket),
            ("help", PageHelp),
        };
        foreach (var (pageTag, page) in pages)
        {
            bool show = pageTag == tag;
            page.Visibility = show ? Visibility.Visible : Visibility.Collapsed;
            if (show)
            {
                FadeIn(page);
                ResetScroll(page);
            }
        }

        if (tag == "market" && _market.Count == 0)
            _ = LoadMarketAsync(false);
        if (tag == "workshop" && _workshop.Count == 0)
            _ = LoadWorkshopAsync();
        if (tag == "mic")
        {
            UpdateMicDeviceName();
            UpdateMicSourceHints();
            _ = RefreshMicStatusAsync(true);
            _ = RefreshVirtualAsync();
        }
    }

    /// <summary>切页时把内容滚回顶部，避免沿用上一页的滚动位置。</summary>
    private static void ResetScroll(DependencyObject root)
    {
        for (int i = 0; i < VisualTreeHelper.GetChildrenCount(root); i++)
        {
            var child = VisualTreeHelper.GetChild(root, i);
            if (child is ScrollViewer viewer)
            {
                viewer.ChangeView(null, 0, null, true);
                return;
            }
            ResetScroll(child);
        }
    }

    /// <summary>页面切换的 180 ms 淡入，Composition 驱动，不触发重新布局。</summary>
    private static void FadeIn(UIElement element)
    {
        try
        {
            var visual = ElementCompositionPreview.GetElementVisual(element);
            var compositor = visual.Compositor;
            visual.Opacity = 0f;
            var animation = compositor.CreateScalarKeyFrameAnimation();
            animation.InsertKeyFrame(0f, 0f);
            animation.InsertKeyFrame(1f, 1f);
            animation.Duration = TimeSpan.FromMilliseconds(180);
            visual.StartAnimation("Opacity", animation);
        }
        catch
        {
            // 动画失败不影响功能
        }
    }

    // ------------------------------------------------------------------
    // creative workshop
    // ------------------------------------------------------------------
    private readonly ObservableCollection<WorkshopItem> _workshop = new();
    private List<WorkshopParam> _workshopParams = new();
    private string? _currentWorkshopSlug;
    private string _codeSnapshot = "";
    private bool _workshopLoading;
    private bool _suppressWsToggle;
    private bool _suppressWsParams;
    private DispatcherQueueTimer? _wsParamTimer;

    private static readonly (string Name, string Slug, string Desc, string Code)[] WorkshopTemplates =
    {
        ("空白模板", "blank", "最小可运行的骨架，自己写处理逻辑。", """
PARAMS = [
    {"id": "gain", "name": "输出", "min": -24.0, "max": 12.0, "default": 0.0, "step": 0.5, "unit": "dB"},
]

def process(x, sr, params):
    # x 是 (块长度, 2) 的 float32；必须返回同形状数组
    return (x * db_to_gain(params["gain"])).astype(np.float32)
"""),
        ("空气感", "air-detail", "提亮 6 kHz 以上，让人声更通透。", """
PARAMS = [
    {"id": "amount", "name": "空气感", "min": 0.0, "max": 1.0, "default": 0.35, "step": 0.01},
    {"id": "trim", "name": "输出", "min": -12.0, "max": 6.0, "default": 0.0, "step": 0.5, "unit": "dB"},
]

def process(x, sr, params):
    # 一阶差分近似高频搁架：抬起 6 kHz 以上，再按 trim 收电平
    high = np.diff(x, axis=0, prepend=x[:1])
    y = x + high * (params["amount"] * 2.0)
    return (y * db_to_gain(params["trim"])).astype(np.float32)
"""),
        ("温和饱和", "warm-saturation", "轻度过载与混合，增加密度和温度。", """
PARAMS = [
    {"id": "drive", "name": "驱动", "min": 1.0, "max": 8.0, "default": 2.0, "step": 0.1},
    {"id": "mix", "name": "混合", "min": 0.0, "max": 1.0, "default": 0.6, "step": 0.01},
]

def process(x, sr, params):
    wet = soft_clip(x * params["drive"], 1.0) * 0.6
    return (x * (1.0 - params["mix"]) + wet * params["mix"]).astype(np.float32)
"""),
        ("立体声加宽", "stereo-wide", "拉开左右声场，直播与游戏语音更立体。", """
PARAMS = [
    {"id": "width", "name": "宽度", "min": 0.0, "max": 2.0, "default": 1.3, "step": 0.05},
]

def process(x, sr, params):
    mid = (x[:, 0] + x[:, 1]) * 0.5
    side = (x[:, 0] - x[:, 1]) * 0.5 * params["width"]
    return np.stack([mid + side, mid - side], axis=1).astype(np.float32)
"""),
        ("低频收紧", "low-tight", "切掉桌面震动与喷麦低频，人声更干净。", """
PARAMS = [
    {"id": "freq", "name": "截止", "min": 40.0, "max": 220.0, "default": 90.0, "step": 5.0, "unit": "Hz"},
]

def process(x, sr, params):
    # 一阶高通：保留人声基频，切掉桌面震动与喷麦低频
    alpha = float(np.exp(-2.0 * np.pi * params["freq"] / sr))
    y = np.empty_like(x)
    prev = np.zeros(x.shape[1], dtype=np.float32)
    for i in range(len(x)):
        prev = alpha * prev + (1.0 - alpha) * x[i]
        y[i] = x[i] - prev
    return y.astype(np.float32)
"""),
    };

    private async Task LoadWorkshopAsync(string? select = null)
    {
        if (!_connected) return;
        _workshopLoading = true;
        try
        {
            var result = await _client.CallAsync("workshop_list", null, 20000);
            var wanted = select ?? _currentWorkshopSlug;
            _workshop.Clear();
            if (result.TryGetProperty("plugins", out var plugins) && plugins.ValueKind == JsonValueKind.Array)
            {
                foreach (var p in plugins.EnumerateArray())
                {
                    var values = p.TryGetProperty("values", out var v) && v.ValueKind == JsonValueKind.Object
                        ? v : default;
                    _workshop.Add(new WorkshopItem
                    {
                        Slug = Str(p, "slug"),
                        Name = Str(p, "name"),
                        Author = Str(p, "author"),
                        Version = Str(p, "version"),
                        License = Str(p, "license"),
                        Description = Str(p, "description"),
                        Code = Str(p, "code"),
                        CompileError = Str(p, "compileError"),
                        Enabled = Bool(p, "enabled"),
                        Params = ParseWorkshopParams(p, values),
                    });
                }
            }

            WorkshopCountText.Text = $"{_workshop.Count} 个";
            bool empty = _workshop.Count == 0;
            WorkshopEmpty.Visibility = empty ? Visibility.Visible : Visibility.Collapsed;
            WorkshopList.Visibility = empty ? Visibility.Collapsed : Visibility.Visible;
            if (result.TryGetProperty("workshopDir", out var dir))
            {
                WorkshopPathText.Text = "目录：data\\mixer\\workshop";
                ToolTipService.SetToolTip(WorkshopPathText, dir.GetString());
            }

            var item = _workshop.FirstOrDefault(w => w.Slug == wanted) ?? _workshop.FirstOrDefault();
            if (item is not null)
            {
                WorkshopList.SelectedItem = item;
                ApplyWorkshop(item);
            }
            else
            {
                WorkshopList.SelectedItem = null;
                ResetWorkshopEditor();
            }
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
        finally
        {
            _workshopLoading = false;
        }
    }

    private static List<WorkshopParam> ParseWorkshopParams(JsonElement plugin, JsonElement values)
    {
        var list = new List<WorkshopParam>();
        if (!plugin.TryGetProperty("params", out var defs) || defs.ValueKind != JsonValueKind.Array)
            return list;
        foreach (var def in defs.EnumerateArray())
        {
            double min = Num(def, "min", 0);
            double max = Num(def, "max", 1);
            double step = Num(def, "step", 0);
            if (step <= 0) step = Math.Max(0.001, (max - min) / 100.0);
            double value = def.TryGetProperty("default", out var d) ? d.GetDouble() : min;
            if (values.ValueKind == JsonValueKind.Object &&
                values.TryGetProperty(Str(def, "id"), out var current))
                value = current.GetDouble();
            list.Add(new WorkshopParam
            {
                Id = Str(def, "id"),
                Name = Str(def, "name"),
                Min = min,
                Max = max,
                Step = step,
                Unit = Str(def, "unit"),
                Value = value,
            });
        }
        return list;
    }

    private void WsCode_TextChanged(object sender, TextChangedEventArgs e)
    {
        if (_workshopLoading) return;
        WsCodeStatus.Text = NormalizeNewlines(WsCodeBox.Text) == NormalizeNewlines(_codeSnapshot)
            ? "已保存"
            : "草稿 · 未保存";
    }

    private static string NormalizeNewlines(string text) => text.Replace("\r\n", "\n").Replace('\r', '\n');

    private void ResetWorkshopEditor()
    {
        _currentWorkshopSlug = null;
        WorkshopPublishButton.IsEnabled = false;
        WsEnableSwitch.IsEnabled = false;
        WsNameBox.Text = "";
        WsSlugBox.Text = "";
        WsAuthorBox.Text = "";
        WsVersionBox.Text = "1.0.0";
        WsDescBox.Text = "";
        WsCodeBox.Text = "";
        WsCodeStatus.Text = "未保存";
        _workshopParams = new List<WorkshopParam>();
        WorkshopParamList.ItemsSource = _workshopParams;
        _suppressWsToggle = true;
        WsEnableSwitch.IsOn = false;
        _suppressWsToggle = false;
        WorkshopTestText.Text = "点「测试」用 1 秒合成信号离线跑一遍，检查输出电平、峰值与每块耗时。";
    }

    private void ApplyWorkshop(WorkshopItem item)
    {
        _currentWorkshopSlug = item.Slug;
        WorkshopPublishButton.IsEnabled = true;
        WsEnableSwitch.IsEnabled = true;
        WsNameBox.Text = item.Name;
        WsSlugBox.Text = item.Slug;
        WsAuthorBox.Text = item.Author;
        WsVersionBox.Text = item.Version;
        WsDescBox.Text = item.Description;
        _codeSnapshot = item.Code;
        WsCodeBox.Text = item.Code;
        WsCodeStatus.Text = item.CompileError.Length > 0 ? "编译失败" : "已保存";
        _workshopParams = item.Params;
        _suppressWsParams = true;
        WorkshopParamList.ItemsSource = _workshopParams;
        _suppressWsParams = false;
        _suppressWsToggle = true;
        WsEnableSwitch.IsOn = item.Enabled;
        _suppressWsToggle = false;
        if (item.CompileError.Length > 0)
            WorkshopTestText.Text = $"编译失败：{item.CompileError}";
    }

    private void WorkshopList_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_workshopLoading) return;
        if (WorkshopList.SelectedItem is WorkshopItem item)
            ApplyWorkshop(item);
        else
            ResetWorkshopEditor();
    }

    private void WorkshopNew_Click(object sender, RoutedEventArgs e)
    {
        WorkshopList.SelectedItem = null;
        ResetWorkshopEditor();
        WsAuthorBox.Text = Environment.UserName;
        ApplyWorkshopTemplate(0);
        WsModeSwitch.IsOn = true;      // 新建意味着要写代码，直接进进阶模式
        WsNameBox.Focus(FocusState.Programmatic);
    }

    private void WorkshopTemplate_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (WorkshopTemplateCombo.SelectedIndex >= 0)
            ApplyWorkshopTemplate(WorkshopTemplateCombo.SelectedIndex);
    }

    private void ApplyWorkshopTemplate(int index)
    {
        if (index < 0 || index >= WorkshopTemplates.Length) return;
        var (name, slug, desc, code) = WorkshopTemplates[index];
        WsCodeBox.Text = code;
        if (WsNameBox.Text.Trim().Length == 0)
            WsNameBox.Text = name;
        if (WsSlugBox.Text.Trim().Length == 0)
            WsSlugBox.Text = slug;
        if (WsDescBox.Text.Trim().Length == 0)
            WsDescBox.Text = desc;
        WsCodeStatus.Text = "模板 · 未保存";
    }

    // ------------------------------------------------------------------
    // 创意工坊：新手模式 + 接口文档
    // ------------------------------------------------------------------
    private void WsMode_Toggled(object sender, RoutedEventArgs e) => ApplyWorkshopMode();

    private void ApplyWorkshopMode()
    {
        bool advanced = WsModeSwitch.IsOn;
        WsBeginnerCard.Visibility = advanced ? Visibility.Collapsed : Visibility.Visible;
        WsMetaCard.Visibility = advanced ? Visibility.Visible : Visibility.Collapsed;
        WsCodeCard.Visibility = advanced ? Visibility.Visible : Visibility.Collapsed;
        WorkshopTemplateCombo.Visibility = advanced ? Visibility.Visible : Visibility.Collapsed;
        WsCodeRow.Height = advanced ? new GridLength(1, GridUnitType.Star) : GridLength.Auto;
        WsModeHint.Text = advanced
            ? "进阶模式：完整元数据与代码编辑，适合改算法、调整参数结构。"
            : "新手模式只显示模板和参数；需要改代码时切到进阶。";
    }

    private void WsTemplateCard_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button button || button.Tag is not string tag
            || !int.TryParse(tag, out int index)) return;
        ApplyWorkshopTemplate(index);
        WsBeginnerHint.Text = $"已载入「{WorkshopTemplates[index].Name}」：点右上角「保存并启用」，"
                              + "然后在下方拖动滑块微调；想换回来重新点卡片即可。";
    }

    private async void WorkshopDocs_Click(object sender, RoutedEventArgs e)
    {
        var panel = new StackPanel { Spacing = 8, MaxWidth = 640 };

        panel.Children.Add(DocHeading("三句话说明白"));
        panel.Children.Add(DocText("创意工坊插件就是一个 Python 文件：写一个 process(x, sr, params) 函数，"
                                   + "决定声音怎么变。保存后立刻进入效果链，不用重启；写错了也只会旁通这一块，"
                                   + "不会把声音弄坏。想改音色又不想写代码，用新手模式的模板就够。"));
        panel.Children.Add(DocHeading("process 的四个约定"));
        panel.Children.Add(DocText("· x：numpy float32，形状是 (块长度, 2)，左右声道各一列，数值大约在 -1 ~ 1。"));
        panel.Children.Add(DocText("· sr：采样率，固定 48000，可以忽略。"));
        panel.Children.Add(DocText("· params：字典，键是下面 PARAMS 里的 id，值是滑块当前位置。"));
        panel.Children.Add(DocText("· 返回值：必须是和 x 形状、类型都一样的数组，否则会被判为出错并旁通。"));
        panel.Children.Add(DocHeading("PARAMS：滑块从哪来"));
        panel.Children.Add(DocCode(
            "PARAMS = [\n"
            + "    {\"id\": \"amount\", \"name\": \"空气感\", \"min\": 0.0, \"max\": 1.0,\n"
            + "     \"default\": 0.35, \"step\": 0.01, \"unit\": \"\"},\n"
            + "]"));
        panel.Children.Add(DocText("id 用于 params[\"id\"] 取值；name 显示在界面上；unit 可写 dB、Hz，也可以留空。"
                                   + "缺少 default 时会取 min。"));
        panel.Children.Add(DocHeading("可以直接用的工具"));
        panel.Children.Add(DocText("np：numpy；db_to_gain(6) 把 dB 换成线性倍数；soft_clip(x, 1.0) 做软限幅。"
                                   + "除此之外不能 import，也不能读写文件或联网。"));
        panel.Children.Add(DocHeading("最小示例"));
        panel.Children.Add(DocCode(
            "PARAMS = [\n"
            + "    {\"id\": \"gain\", \"name\": \"输出\", \"min\": -24.0, \"max\": 12.0,\n"
            + "     \"default\": 0.0, \"step\": 0.5, \"unit\": \"dB\"},\n"
            + "]\n\n"
            + "def process(x, sr, params):\n"
            + "    return (x * db_to_gain(params[\"gain\"])).astype(np.float32)"));
        panel.Children.Add(DocHeading("性能与排查"));
        panel.Children.Add(DocText("每块音频是 40 ms（1920 样本），建议单块处理控制在 8 ms 内；尽量用 numpy 整体运算，"
                                   + "少写逐样本循环。页面下方「测试」会用一秒合成信号跑一遍，给出峰值、电平与耗时。"));
        panel.Children.Add(DocHeading("发布给其他人用"));
        panel.Children.Add(DocText("填好名称、作者、版本与许可，点「发布到市场」。插件会推到公开仓库，"
                                   + "别人在插件市场刷新即可一键安装；不发布也可以只在本机使用。"));

        var dialog = new ContentDialog
        {
            Title = "创意工坊 · 接口文档",
            Content = new ScrollViewer
            {
                Content = panel,
                MaxHeight = 520,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            },
            CloseButtonText = "知道了",
            XamlRoot = RootLayout.XamlRoot,
        };
        await dialog.ShowAsync();
    }

    private TextBlock DocHeading(string text)
    {
        var block = new TextBlock { Text = text, Margin = new Thickness(0, 10, 0, 0) };
        block.Style = (Style)Application.Current.Resources["SectionTitle"];
        return block;
    }

    private TextBlock DocText(string text)
    {
        var block = new TextBlock { Text = text, TextWrapping = TextWrapping.Wrap, IsTextSelectionEnabled = true };
        block.Style = (Style)Application.Current.Resources["HintText"];
        return block;
    }

    private Border DocCode(string code)
    {
        return new Border
        {
            Background = (Brush)Application.Current.Resources["ControlFillColorDefaultBrush"],
            BorderBrush = (Brush)Application.Current.Resources["CardStrokeColorDefaultBrush"],
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(8),
            Padding = new Thickness(12, 10, 12, 10),
            Child = new TextBlock
            {
                Text = code,
                FontFamily = new FontFamily("Consolas"),
                FontSize = 12,
                TextWrapping = TextWrapping.Wrap,
                IsTextSelectionEnabled = true,
            },
        };
    }

    private JsonObject BuildWorkshopManifest() => new()
    {
        ["name"] = WsNameBox.Text.Trim(),
        ["slug"] = WsSlugBox.Text.Trim(),
        ["author"] = WsAuthorBox.Text.Trim(),
        ["version"] = WsVersionBox.Text.Trim().Length > 0 ? WsVersionBox.Text.Trim() : "1.0.0",
        ["license"] = WsLicenseCombo.SelectedItem as string ?? "MIT",
        ["description"] = WsDescBox.Text.Trim(),
        ["code"] = WsCodeBox.Text,
    };

    private async void WorkshopSave_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        if (WsNameBox.Text.Trim().Length == 0)
        {
            ShowError("请填写插件名称", InfoBarSeverity.Warning);
            return;
        }
        if (WsSlugBox.Text.Trim().Length == 0)
            WsSlugBox.Text = SlugFromName(WsNameBox.Text);

        SetBusy(true, "正在保存工坊插件…");
        try
        {
            var result = await _client.CallAsync("workshop_save",
                new JsonObject { ["plugin"] = BuildWorkshopManifest() }, 30000);
            var plugin = result.GetProperty("plugin");
            var slug = Str(plugin, "slug");
            await LoadWorkshopAsync(slug);
            ShowError($"「{Str(plugin, "name")}」已保存并加入效果链", InfoBarSeverity.Success);
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

    private static string SlugFromName(string name)
    {
        var builder = new System.Text.StringBuilder();
        foreach (var ch in name.Trim().ToLowerInvariant())
        {
            if (ch is >= 'a' and <= 'z' or >= '0' and <= '9') builder.Append(ch);
            else if (ch is ' ' or '-' or '_') builder.Append('-');
        }
        var slug = builder.ToString().Trim('-');
        return slug.Length > 0 ? slug : "my-plugin";
    }

    private async void WorkshopTest_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        WorkshopTestText.Text = "正在离线测试…";
        try
        {
            var result = await _client.CallAsync("workshop_test",
                new JsonObject { ["plugin"] = BuildWorkshopManifest() }, 30000);
            if (!Bool(result, "ok"))
            {
                WorkshopTestText.Text = $"测试失败：{Str(result, "error")}";
                return;
            }
            WorkshopTestText.Text =
                $"通过：RMS {Num(result, "rmsIn", 0):0.000} → {Num(result, "rmsOut", 0):0.000} " +
                $"（{Num(result, "changeDb", 0):+0.0;-0.0;0} dB）· 峰值 {Num(result, "peak", 0):0.000} " +
                $"· {Num(result, "msPerBlock", 0):0.000} ms/块（预算 40 ms）";
            if (result.TryGetProperty("params", out var defs) && defs.ValueKind == JsonValueKind.Array)
            {
                _workshopParams = ParseWorkshopParams(result, default);
                _suppressWsParams = true;
                WorkshopParamList.ItemsSource = _workshopParams;
                _suppressWsParams = false;
            }
        }
        catch (Exception ex)
        {
            WorkshopTestText.Text = ex.Message;
        }
    }

    private async void WorkshopDelete_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        var slug = _currentWorkshopSlug;
        if (slug is null)
        {
            ShowError("请先选择要删除的插件", InfoBarSeverity.Warning);
            return;
        }
        if (!await ConfirmAsync("删除工坊插件", $"删除「{WsNameBox.Text}」后无法恢复，确定继续？", "删除"))
            return;
        try
        {
            await _client.CallAsync("workshop_delete", new JsonObject { ["slug"] = slug }, 15000);
            await LoadWorkshopAsync();
            ResetWorkshopEditor();
            ShowError("已删除", InfoBarSeverity.Success);
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
    }

    private async void WsEnable_Toggled(object sender, RoutedEventArgs e)
    {
        if (_suppressWsToggle || !_connected || _currentWorkshopSlug is null) return;
        try
        {
            var result = await _client.CallAsync("workshop_enable",
                new JsonObject { ["slug"] = _currentWorkshopSlug, ["enabled"] = WsEnableSwitch.IsOn }, 20000);
            var error = Str(result, "error");
            if (error.Length > 0)
                ShowError($"已切换，但部分插件未加载：{error}", InfoBarSeverity.Warning);
            await LoadWorkshopAsync(_currentWorkshopSlug);
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
            _suppressWsToggle = true;
            WsEnableSwitch.IsOn = !WsEnableSwitch.IsOn;
            _suppressWsToggle = false;
        }
    }

    private void WorkshopParam_Changed(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (_suppressWsParams || sender is not Slider slider) return;
        if (slider.DataContext is not WorkshopParam param) return;
        param.Value = slider.Value;
        if (_currentWorkshopSlug is null) return;

        _wsParamTimer ??= DispatcherQueue.CreateTimer();
        _wsParamTimer.Interval = TimeSpan.FromMilliseconds(220);
        _wsParamTimer.IsRepeating = false;
        _wsParamTimer.Tick -= FlushWorkshopParams;
        _wsParamTimer.Tick += FlushWorkshopParams;
        _wsParamTimer.Start();
    }

    private async void FlushWorkshopParams(DispatcherQueueTimer sender, object args)
    {
        if (!_connected || _currentWorkshopSlug is null) return;
        var values = new JsonObject();
        foreach (var param in _workshopParams)
            values[param.Id] = param.Value;
        try
        {
            await _client.CallAsync("workshop_params",
                new JsonObject { ["slug"] = _currentWorkshopSlug, ["params"] = values }, 10000);
        }
        catch
        {
            // 参数下发的失败不打断拖动，下一次会覆盖。
        }
    }

    private async void WorkshopPublish_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        if (_currentWorkshopSlug is null)
        {
            ShowError("请先保存插件，再发布到市场", InfoBarSeverity.Warning);
            return;
        }
        var license = WsLicenseCombo.SelectedItem as string ?? "MIT";
        var dialog = new ContentDialog
        {
            XamlRoot = Content.XamlRoot,
            Title = "发布到插件市场",
            Content = $"将把「{WsNameBox.Text}」推送到自托管仓库 HandaQAQ-Code/Intro-Voice-Plugin-Market。\n\n" +
                      $"许可：{license}\n发布后所有用户都能在插件市场一键安装；脚本会在本机以 Python 权限执行，" +
                      "请确认代码没有危险操作。",
            PrimaryButtonText = "发布",
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Close,
        };
        if (await dialog.ShowAsync() != ContentDialogResult.Primary) return;

        SetBusy(true, "正在推送到市场仓库…");
        WorkshopTestText.Text = "正在推送，首次会克隆市场仓库…";
        try
        {
            var result = await _client.CallAsync("workshop_publish",
                new JsonObject { ["slug"] = _currentWorkshopSlug }, 240000);
            var commit = Str(result, "commit");
            var url = Str(result, "url");
            WorkshopTestText.Text = $"已发布：commit {commit} · {Str(result, "repo")}";
            var done = new ContentDialog
            {
                XamlRoot = Content.XamlRoot,
                Title = "已发布到市场",
                Content = $"提交 {commit} 已推送到 {Str(result, "repo")}。\n" +
                          "其他人刷新插件目录即可看到并一键安装。",
                PrimaryButtonText = "打开发布页",
                CloseButtonText = "完成",
                DefaultButton = ContentDialogButton.Close,
            };
            if (await done.ShowAsync() == ContentDialogResult.Primary && url.Length > 0)
            {
                try { System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(url) { UseShellExecute = true }); }
                catch { }
            }
            await LoadMarketAsync(true);
        }
        catch (Exception ex)
        {
            WorkshopTestText.Text = $"发布失败：{ex.Message}";
            ShowError(ex.Message);
        }
        finally
        {
            SetBusy(false);
        }
    }

    // ------------------------------------------------------------------
    // 手机麦克风
    // ------------------------------------------------------------------
    private async void PollMic(object? sender, object args)
    {
        if (!_connected || _busy || _micPolling) return;
        if (PageMicConfig.Visibility != Visibility.Visible) return;
        await RefreshMicStatusAsync(false);
    }

    private async Task RefreshMicStatusAsync(bool includeQr)
    {
        if (!_connected || _micPolling) return;
        _micPolling = true;
        try
        {
            var status = await _client.CallAsync("mic_status",
                new JsonObject { ["qr"] = includeQr }, 8000);
            ApplyMicStatus(status, includeQr);
        }
        catch
        {
            // 服务刚重启时可能瞬间拿不到状态，下一轮再试。
        }
        finally
        {
            _micPolling = false;
        }
    }

    private async void ApplyMicStatus(JsonElement status, bool includeQr)
    {
        bool running = Bool(status, "running");
        _micRunning = running;
        string pin = Str(status, "pin");
        string ip = Str(status, "ip");
        int port = (int)Num(status, "port", 0);

        MicStateText.Text = running ? "服务已启动" : "未启动";
        MicStateDot.Background = new SolidColorBrush(running
            ? Color.FromArgb(255, 76, 201, 116)
            : Color.FromArgb(255, 138, 128, 120));
        MicToggleButton.Content = running ? "停止服务" : "启动服务";
        MicEndpointText.Text = running && ip.Length > 0 ? $"{ip}:{port}" : "服务未启动";
        MicPinText.Text = running && pin.Length > 0 ? pin : "——";

        string error = Str(status, "error");
        bool apkReady = Bool(status, "apkReady");
        if (!running)
        {
            MicQrHint.Text = error.Length > 0
                ? $"启动失败：{error}"
                : "点「启动服务」后，这里会出现二维码与 PIN。";
        }
        else if (!apkReady)
        {
            MicQrHint.Text = "服务已启动，但没有找到安装包；先构建 mobile-android，或直接安装已分发的 App。";
        }
        else
        {
            MicQrHint.Text = "手机与电脑连同一个 Wi-Fi，用系统相机或微信扫码；已装 App 会自动带出地址与 PIN。";
        }

        if (status.TryGetProperty("mic", out var mic) && mic.ValueKind == JsonValueKind.Object)
        {
            double clients = Num(mic, "clients", 0);
            int rate = (int)Num(mic, "clientRate", 0);
            double level = Num(mic, "levelDb", -90);
            double buffer = Num(mic, "bufferMs", 0);
            double packets = Num(mic, "packets", 0);
            double underruns = Num(mic, "underruns", 0);
            double overflows = Num(mic, "overflows", 0);

            MicClientsText.Text = $"{clients:0}";
            MicRateText.Text = rate > 0 && clients > 0 ? $"{rate / 1000.0:0.#} kHz" : "—";
            MicLevelText.Text = level <= -89.5 ? "—" : $"{level:0} dB";
            MicBufferText.Text = running && clients > 0 ? $"{buffer:0} ms" : "—";
            MicPacketsText.Text = $"{packets:0}";
            MicDropText.Text = $"{underruns:0}/{overflows:0}";
            MicLevelBar.Value = Math.Clamp((level + 60.0) / 60.0 * 100.0, 0, 100);

            double inDb = Num(status, "inputDb", -90);
            bool hasLevel = running && inDb > -89.5;
            MicCfgLevelText.Text = hasLevel ? $"{inDb:0.0} dB" : "—";
            MicCfgLevelBar.Value = Math.Clamp((inDb + 60.0) / 60.0 * 100.0, 0, 100);

            int inRate = (int)Num(status, "inputRate", 0);
            int outRate = (int)Num(status, "outputRate", 0);
            double latencyMs = Num(status, "latencyMs", 0);
            double drops = Num(status, "drops", 0);
            MicCfgInRateText.Text = inRate > 0 ? $"{inRate / 1000.0:0.#} kHz" : "—";
            MicCfgOutRateText.Text = outRate > 0 ? $"{outRate / 1000.0:0.#} kHz" : "—";
            MicCfgLatencyText.Text = running ? $"{latencyMs:0} ms" : "—";
            MicCfgDropsText.Text = $"{drops:0}";
            MicCfgHealthText.Text = running
                ? drops > 0
                    ? "有掉块：把超分延迟模式调高一档，或先减掉几个插件。"
                    : "链路稳定，没有掉块。"
                : "引擎未运行；开始后这里显示实际采样率与延迟。";

            string client = Str(mic, "client");
            MicDeviceText.Text = client.Length > 0
                ? $"正在接收：{client} · 手机端 {rate} Hz"
                : running ? "服务已就绪，等待手机连接…" : "服务未启动。";
        }

        if (status.TryGetProperty("inputSource", out var src)
            && src.ValueKind == JsonValueKind.String)
        {
            string source = src.GetString() ?? "";
            // 刚在界面点过来源时，等 push 落地再以服务端为准，避免被上一轮状态拉回。
            if (source.Length > 0 && (DateTime.UtcNow - _micSourcePushedAt).TotalSeconds > 2.5)
                SetMicSource(source, false);
        }

        // 只在进入页面 / 启动服务时同步滑块，避免和拖动打架。
        if (includeQr && status.TryGetProperty("settings", out var cfg)
            && cfg.ValueKind == JsonValueKind.Object)
        {
            _micSyncing = true;
            MicGainSlider.Value = Math.Clamp(Num(cfg, "gainDb", 0), -24, 24);
            MicHpfSlider.Value = Math.Clamp(Num(cfg, "hpfHz", 80), 20, 300);
            MicGateSlider.Value = Math.Clamp(Num(cfg, "gateDb", -62), -80, -20);
            MicJitterSlider.Value = Math.Clamp(Num(cfg, "jitterMs", 80), 20, 300);
            _micSyncing = false;
            UpdateMicValueLabels();
        }

        string key = $"{pin}:{port}";
        if (includeQr && running && key != _micQrKey)
        {
            _micQrKey = key;
            var connect = await Base64ImageAsync(Str(status, "qrConnect"));
            var install = await Base64ImageAsync(Str(status, "qrInstall"));
            MicConnectQr.Source = connect;
            MicInstallQr.Source = install;
            MicConnectQrBox.Background = new SolidColorBrush(
                connect is null ? Microsoft.UI.Colors.Transparent : Microsoft.UI.Colors.White);
            MicInstallQrBox.Background = new SolidColorBrush(
                install is null ? Microsoft.UI.Colors.Transparent : Microsoft.UI.Colors.White);
            MicQrRow.Visibility = connect is null && install is null
                ? Visibility.Collapsed : Visibility.Visible;
            if (connect is null && install is null)
                MicQrHint.Text = "二维码生成失败：确认 data 目录可写，且运行环境已安装 segno。";
        }
        else if (!running)
        {
            _micQrKey = "";
            MicConnectQr.Source = null;
            MicInstallQr.Source = null;
            MicQrRow.Visibility = Visibility.Collapsed;
        }
    }

    private void MicToggle_Click(object sender, RoutedEventArgs e)
    {
        _ = ToggleMicAsync();
    }

    private async Task ToggleMicAsync()
    {
        if (_busy || !_connected) return;
        SetBusy(true, _micRunning ? "正在停止手机麦克风…" : "正在启动手机麦克风…");
        try
        {
            if (_micRunning)
                await _client.CallAsync("mic_stop", null, 15000);
            else
                await _client.CallAsync("mic_start", new JsonObject(), 20000);
            await RefreshMicStatusAsync(true);
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

    private void MicCopy_Click(object sender, RoutedEventArgs e)
    {
        string endpoint = MicEndpointText.Text;
        string pin = MicPinText.Text;
        if (!_micRunning || endpoint.Length == 0) return;
        var package = new DataPackage();
        package.SetText($"{endpoint}  PIN {pin}");
        Clipboard.SetContent(package);
        MicQrHint.Text = "连接信息已复制，可以发给手机或粘贴到 App 里。";
    }

    private async void MicInstallPage_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            await _client.CallAsync("mic_open_apk", new JsonObject { ["mode"] = "page" }, 10000);
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
    }

    private async void MicApk_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            await _client.CallAsync("mic_open_apk", new JsonObject(), 10000);
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
    }

    private void MicSlider_Changed(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (_micSyncing) return;
        UpdateMicValueLabels();
        _micSettingTimer.Stop();
        _micSettingTimer.Start();
    }

    private void UpdateMicValueLabels()
    {
        MicGainValue.Text = $"{MicGainSlider.Value:0} dB";
        MicHpfValue.Text = $"{MicHpfSlider.Value:0} Hz";
        MicGateValue.Text = $"{MicGateSlider.Value:0} dB";
        MicJitterValue.Text = $"{MicJitterSlider.Value:0} ms";
    }

    private async void FlushMicSettings(object? sender, object args)
    {
        if (!_connected || _micSyncing) return;
        try
        {
            await _client.CallAsync("mic_settings", new JsonObject
            {
                ["gainDb"] = Math.Round(MicGainSlider.Value, 0),
                ["hpfHz"] = Math.Round(MicHpfSlider.Value, 0),
                ["gateDb"] = Math.Round(MicGateSlider.Value, 0),
                ["jitterMs"] = Math.Round(MicJitterSlider.Value, 0),
            }, 8000);
        }
        catch
        {
            // 服务忙时下一轮拖动会再发一次。
        }
    }

    // ------------------------------------------------------------------
    // 麦克风配置：虚拟麦克风输出（主视图）+ 手机麦克风（派生输入）
    // ------------------------------------------------------------------
    private void MicViewToggle_Click(object sender, RoutedEventArgs e)
    {
        if (_micSyncing) return;
        SetMicView(ReferenceEquals(sender, MicViewMobileToggle) ? "mobile" : "virtual");
    }

    private void SetMicView(string view)
    {
        _micView = string.Equals(view, "mobile", StringComparison.OrdinalIgnoreCase)
            ? "mobile" : "virtual";
        _micSyncing = true;
        MicViewMobileToggle.IsChecked = _micView == "mobile";
        MicViewVirtualToggle.IsChecked = _micView == "virtual";
        _micSyncing = false;

        bool mobile = _micView == "mobile";
        MicMobileView.Visibility = mobile ? Visibility.Visible : Visibility.Collapsed;
        MicVirtualView.Visibility = mobile ? Visibility.Collapsed : Visibility.Visible;
        UpdateMicDeviceName();
        if (mobile)
            _ = RefreshMicStatusAsync(true);
        else
            _ = RefreshVirtualAsync();
    }

    private void MicGoLive_Click(object sender, RoutedEventArgs e) => GoToPage("live");

    private void MicGoMobile_Click(object sender, RoutedEventArgs e) => SetMicView("mobile");

    private void MicInputSource_Changed(object sender, RoutedEventArgs e)
    {
        if (_micSyncing) return;
        if (sender is not RadioButton radio || radio.IsChecked != true) return;
        SetMicSource(ReferenceEquals(radio, MicInputMobileRadio) ? "mobile" : "device", true);
    }

    private async void MicRefreshDevices_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        SetBusy(true, "正在重新枚举音频设备…");
        try
        {
            ApplyDevices(await _client.CallAsync("devices"));
            UpdateMicDeviceName();
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

    /// <summary>进页面时重新检测虚拟端点，不修改系统设备。</summary>
    private async Task RefreshVirtualAsync()
    {
        if (!_connected) return;
        try
        {
            ApplyDevices(await _client.CallAsync("devices"));
        }
        catch
        {
            // 服务忙时下一次刷新或进入页面会补上。
        }
    }

    /// <summary>渲染虚拟设备检测结果：只展示状态，不安装、不分发驱动。</summary>
    private void ApplyVirtualStatus(JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Object) return;
        bool installed = value.TryGetProperty("installed", out var flag)
                         && flag.ValueKind == JsonValueKind.True;
        VirtStateText.Text = installed ? "已就绪" : "未检测到";
        VirtStateDot.Background = new SolidColorBrush(installed
            ? Microsoft.UI.Colors.ForestGreen : Microsoft.UI.Colors.Gray);
        VirtHintText.Text = Str(value, "hint");

        if (value.TryGetProperty("render", out var render)
            && render.ValueKind == JsonValueKind.Object)
            VirtRenderText.Text = Str(render, "label");
        else
            VirtRenderText.Text = "—（未检测到 CABLE Input）";
        if (value.TryGetProperty("capture", out var capture)
            && capture.ValueKind == JsonValueKind.Object)
            VirtCaptureText.Text = Str(capture, "label");
        else
            VirtCaptureText.Text = "—（未检测到 CABLE Output）";

        string note = Str(value, "driverNote");
        if (value.TryGetProperty("pack", out var pack)
            && pack.ValueKind == JsonValueKind.Object
            && pack.TryGetProperty("present", out var present)
            && present.ValueKind == JsonValueKind.True)
        {
            string dir = Str(pack, "dir");
            if (dir.Length > 0) note += "\n本机已有官方解压包：" + dir;
        }
        VirtDriverNote.Text = note;
        VirtInstallerButton.IsEnabled = true;
    }

    private async void VirtInstaller_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        try
        {
            var result = await _client.CallAsync("virtual_open_installer", new JsonObject(), 10000);
            string hint = Str(result, "hint");
            if (hint.Length > 0) VirtHintText.Text = hint;
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
    }

    private async void VirtDownload_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        try
        {
            var result = await _client.CallAsync("virtual_open_download", new JsonObject(), 10000);
            string hint = Str(result, "hint");
            if (hint.Length > 0) VirtHintText.Text = hint;
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
    }

    /// <summary>同步输入来源（声卡 / 手机）；push 为真时写回服务端。</summary>
    private void SetMicSource(string source, bool push)
    {
        _micSource = string.Equals(source, "mobile", StringComparison.OrdinalIgnoreCase)
            ? "mobile" : "device";
        _micSyncing = true;
        MicInputMobileRadio.IsChecked = _micSource == "mobile";
        MicInputDeviceRadio.IsChecked = _micSource == "device";
        _micSyncing = false;
        UpdateMicSourceHints();
        if (push) _ = PushMicSourceAsync(_micSource);
    }

    private async Task PushMicSourceAsync(string source)
    {
        if (!_connected) return;
        try
        {
            await _client.CallAsync("set_options",
                new JsonObject { ["inputSource"] = source }, 8000);
            _micSourcePushedAt = DateTime.UtcNow;
            MicDeviceText.Text = source == "mobile"
                ? "输入源已切到手机麦克风；回「主控」页重新开始即可生效。"
                : "输入源已切回电脑声卡；回「主控」页重新开始即可生效。";
            UpdateMicSourceHints();
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
    }

    private void UpdateMicDeviceName()
    {
        var device = InputCombo.SelectedItem as DeviceItem;
        MicDeviceNameText.Text = device?.Label ?? "未选择设备";
    }

    private void UpdateMicSourceHints()
    {
        MicDeviceRunText.Text = _micSource == "mobile"
            ? "当前输入来自手机麦克风；配对与电平在「手机麦克风」视图。"
            : _micRunning
                ? "链路正在运行，输入来自电脑声卡；输出会自动选中虚拟麦克风。"
                : "链路未运行：到主控页点开始，声音才会写进虚拟麦克风。";
    }

    /// <summary>按导航项定位页面，同时更新侧边栏选中态。</summary>
    private void GoToPage(string tag)
    {
        foreach (var item in Nav.MenuItems)
        {
            if (item is NavigationViewItem navItem && (navItem.Tag as string) == tag)
            {
                Nav.SelectedItem = navItem;
                return;
            }
        }
        ShowPage(tag);
    }

    private static async Task<ImageSource?> Base64ImageAsync(string base64)
    {
        if (string.IsNullOrWhiteSpace(base64)) return null;
        try
        {
            byte[] bytes = Convert.FromBase64String(base64);
            using var stream = new InMemoryRandomAccessStream();
            await stream.WriteAsync(bytes.AsBuffer());
            stream.Seek(0);
            var bitmap = new BitmapImage();
            await bitmap.SetSourceAsync(stream);
            return bitmap;
        }
        catch
        {
            return null;
        }
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
                    string kind = Str(p, "type");
                    bool workshop = kind == "workshop";
                    double size = Num(p, "sizeMb", 0);
                    var parts = new List<string>();
                    if (workshop) parts.Add("创意工坊");
                    if (vendor.Length > 0) parts.Add(vendor);
                    if (version.Length > 0) parts.Add($"v{version}");
                    if (size > 0) parts.Add($"{size:0.#} MB");

                    _market.Add(new MarketItem
                    {
                        Slug = slug,
                        Name = Str(p, "name"),
                        Badge = license.Length > 0 ? license : workshop ? "工坊" : "VST3",
                        Subtitle = string.Join(" · ", parts),
                        Summary = Str(p, "summary"),
                        Kind = kind,
                        Installed = installedSet.Contains(slug),
                        CanInstall = (kind == "zip" && Str(p, "download").Length > 0) ||
                                     (workshop && Str(p, "manifest").Length > 0),
                    });
                }
            }

            bool empty = _market.Count == 0;
            MarketEmpty.Visibility = empty ? Visibility.Visible : Visibility.Collapsed;
            MarketList.Visibility = empty ? Visibility.Collapsed : Visibility.Visible;

            string error = Str(catalog, "error");
            bool cached = catalog.TryGetProperty("cached", out var c) && c.ValueKind == JsonValueKind.True;
            MarketSourceText.Text = $"目录：{Str(catalog, "source")}" + (cached ? "（本地缓存）" : "");
            bool bundled = catalog.TryGetProperty("bundled", out var b) && b.ValueKind == JsonValueKind.True;
            MarketStatusText.Text = cached || bundled
                ? $"远程目录暂不可用，显示本地目录 · {_market.Count} 个条目 · 已安装 {installedSet.Count} 个"
                : error.Length > 0
                    ? "目录加载失败，点「刷新目录」重试"
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
                if (item.IsWorkshop)
                    await LoadWorkshopAsync();
            }
            else
            {
                await _client.CallAsync("market_install", new JsonObject { ["slug"] = item.Slug }, 300000);
                ShowError(item.IsWorkshop
                        ? $"{item.Name} 已安装，可在「创意工坊」页启用"
                        : $"{item.Name} 已安装，可在「VST3 插件」页扫描后加入链",
                    InfoBarSeverity.Success);
                if (item.IsWorkshop)
                    await LoadWorkshopAsync(item.Slug);
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
                    // 兼容旧服务：字符串数组；新服务返回带内容摘要的对象。
                    if (p.ValueKind == JsonValueKind.String)
                    {
                        items.Add(new PresetItem { Name = p.GetString() ?? "" });
                    }
                    else if (p.ValueKind == JsonValueKind.Object)
                    {
                        var effects = new List<string>();
                        if (p.TryGetProperty("effects", out var fx) && fx.ValueKind == JsonValueKind.Array)
                            foreach (var one in fx.EnumerateArray())
                                if (one.GetString() is { Length: > 0 } text) effects.Add(text);
                        items.Add(new PresetItem
                        {
                            Name = Str(p, "name"),
                            SavedAt = Str(p, "savedAt"),
                            Plugins = (int)Num(p, "plugins", 0),
                            Ai = Str(p, "aiLabel"),
                            Effects = string.Join(" / ", effects),
                            Workshop = (int)Num(p, "workshop", 0),
                        });
                    }
                }
            }

            var previous = select ?? (PresetCombo.SelectedItem as PresetItem)?.Name;
            _presets.Clear();
            foreach (var item in items.Where(n => n.Name.Length > 0))
                _presets.Add(item);

            PresetCombo.SelectedItem = _presets.FirstOrDefault(p => p.Name == previous) ?? _presets.FirstOrDefault();
            PresetCountText.Text = $"{_presets.Count} 套";
            UpdatePresetDetail();
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
    }

    private void UpdatePresetDetail()
    {
        bool has = PresetCombo.SelectedItem is PresetItem;
        if (PresetCombo.SelectedItem is PresetItem item)
            PresetDetailText.Text = item.Detail;
        else
            PresetDetailText.Text = "保存当前的全部设置后，可在这里一键切换场景。";
        PresetLoadButton.IsEnabled = has;
        PresetSaveButton.IsEnabled = has;
        PresetDeleteButton.IsEnabled = has;
    }

    private void PresetCombo_SelectionChanged(object sender, SelectionChangedEventArgs e)
        => UpdatePresetDetail();

    private string? CurrentPresetName() => (PresetCombo.SelectedItem as PresetItem)?.Name;

    /// <summary>输入预设名。返回 null 表示用户取消。</summary>
    private async Task<string?> PromptPresetNameAsync(string title, string initial)
    {
        var box = new TextBox
        {
            Text = initial,
            PlaceholderText = "例如：直播·暖声",
            SelectionStart = initial.Length,
        };
        var dialog = new ContentDialog
        {
            XamlRoot = Content.XamlRoot,
            Title = title,
            Content = box,
            PrimaryButtonText = "保存",
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Primary,
            IsPrimaryButtonEnabled = initial.Trim().Length > 0,
        };
        box.TextChanged += (_, _) =>
            dialog.IsPrimaryButtonEnabled = box.Text.Trim().Length > 0;
        if (await dialog.ShowAsync() != ContentDialogResult.Primary)
            return null;
        var name = box.Text.Trim();
        if (name.Length == 0 || name.IndexOfAny(new[] { '\\', '/', ':', '*', '?', '"', '<', '>', '|' }) >= 0)
        {
            ShowError("预设名称不能为空，也不能包含 \\ / : * ? \" < > |", InfoBarSeverity.Warning);
            return null;
        }
        return name;
    }

    private async Task<bool> ConfirmAsync(string title, string message, string primary)
    {
        var dialog = new ContentDialog
        {
            XamlRoot = Content.XamlRoot,
            Title = title,
            Content = message,
            PrimaryButtonText = primary,
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Close,
        };
        return await dialog.ShowAsync() == ContentDialogResult.Primary;
    }

    private async void PresetSave_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        var name = CurrentPresetName();
        if (name is null)
        {
            ShowError("请先选择一套预设；新预设请用「另存为」", InfoBarSeverity.Warning);
            return;
        }
        if (!await ConfirmAsync("覆盖预设", $"用当前的全部设置覆盖「{name}」？", "覆盖"))
            return;

        SetBusy(true, "正在保存预设…");
        try
        {
            await _client.CallAsync("preset_save", new JsonObject { ["name"] = name }, 30000);
            await RefreshPresets(name);
            ShowError($"预设「{name}」已更新", InfoBarSeverity.Success);
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

    private async void PresetSaveAs_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        var initial = CurrentPresetName();
        initial = initial is { Length: > 0 } ? $"{initial} 副本" : $"预设 {_presets.Count + 1}";
        var name = await PromptPresetNameAsync("另存为新预设", initial);
        if (name is null) return;

        SetBusy(true, "正在保存预设…");
        try
        {
            await _client.CallAsync("preset_save", new JsonObject { ["name"] = name }, 30000);
            await RefreshPresets(name);
            ShowError($"预设「{name}」已保存", InfoBarSeverity.Success);
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
        var name = CurrentPresetName();
        if (name is null)
        {
            ShowError("请先选择要载入的预设", InfoBarSeverity.Warning);
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
            ShowError($"预设「{name}」已载入", InfoBarSeverity.Success);
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
        var name = CurrentPresetName();
        if (name is null)
        {
            ShowError("请先选择要删除的预设", InfoBarSeverity.Warning);
            return;
        }
        if (!await ConfirmAsync("删除预设", $"删除「{name}」后无法恢复，确定继续？", "删除"))
            return;
        try
        {
            await _client.CallAsync("preset_delete", new JsonObject { ["name"] = name }, 10000);
            await RefreshPresets();
            ShowError($"预设「{name}」已删除", InfoBarSeverity.Success);
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
