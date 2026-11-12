using System.Runtime.InteropServices;
using Microsoft.UI;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Shapes;
using Windows.Graphics;
using Windows.UI;
using WinRT.Interop;

namespace MixerUI;

/// <summary>悬浮窗配置：显示哪些数据、透明度、上次位置（-1 表示自动放右上角）。</summary>
public sealed class FloatingConfig
{
    /// <summary>所有可显示的数据项，顺序就是悬浮窗里的显示顺序。</summary>
    public static readonly string[] AllItems =
        { "status", "ai", "input", "output", "latency", "cpu", "gpu", "ram" };

    public bool Enabled { get; set; }
    public double Opacity { get; set; } = 0.92;
    public HashSet<string> Items { get; set; } = new(AllItems);
    public int X { get; set; } = -1;
    public int Y { get; set; } = -1;
}

/// <summary>
/// 桌面悬浮窗（HUD）：置顶、无边框、可拖动、可调透明度，
/// 只显示用户在设置里勾选的数据。所有控件都在代码里搭，不额外起 XAML 文件。
/// </summary>
public sealed class FloatingWindow : Window
{
    private const int GWL_EXSTYLE = -20;
    private const long WS_EX_LAYERED = 0x00080000;
    private const long WS_EX_TOOLWINDOW = 0x00000080;
    private const uint LWA_ALPHA = 0x2;
    private const int DWMWA_WINDOW_CORNER_PREFERENCE = 33;
    private const uint SWP_NOSIZE = 0x1;
    private const uint SWP_NOMOVE = 0x2;
    private const uint SWP_NOZORDER = 0x4;
    private const uint SWP_NOACTIVATE = 0x10;
    private const uint SWP_FRAMECHANGED = 0x20;

    private readonly FloatingConfig _config;
    private readonly Grid _root;
    private readonly StackPanel _rowsPanel;
    private readonly Ellipse _statusDot;
    private readonly Dictionary<string, RowView> _rows = new();
    private readonly Grid _dragArea;

    private PointInt32 _dragStartPos;
    private POINT _dragStartCursor;
    private bool _dragging;

    private sealed class RowView
    {
        public TextBlock Value = null!;
        public ProgressBar? Bar;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT
    {
        public int X;
        public int Y;
    }

    [DllImport("user32.dll")]
    private static extern bool GetCursorPos(out POINT lpPoint);

    [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW")]
    private static extern IntPtr SetWindowLongPtr(IntPtr hWnd, int nIndex, IntPtr dwNewLong);

    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")]
    private static extern IntPtr GetWindowLongPtr(IntPtr hWnd, int nIndex);

    [DllImport("user32.dll")]
    private static extern bool SetLayeredWindowAttributes(IntPtr hWnd, uint crKey, byte bAlpha, uint dwFlags);

    [DllImport("user32.dll")]
    private static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter,
        int x, int y, int cx, int cy, uint uFlags);

    [DllImport("user32.dll")]
    private static extern uint GetDpiForWindow(IntPtr hWnd);

    [DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute, ref int value, int size);

    public event Action? ConfigChanged;

    public FloatingWindow(FloatingConfig config)
    {
        _config = config;
        Title = "音拓 HUD";

        // 置顶、不可缩放、没有系统标题栏。
        var presenter = OverlappedPresenter.Create();
        presenter.IsAlwaysOnTop = true;
        presenter.IsResizable = false;
        presenter.IsMaximizable = false;
        presenter.IsMinimizable = false;
        presenter.SetBorderAndTitleBar(false, false);
        AppWindow.SetPresenter(presenter);
        ExtendsContentIntoTitleBar = true;

        _statusDot = new Ellipse
        {
            Width = 7,
            Height = 7,
            Fill = new SolidColorBrush(Color.FromArgb(255, 150, 150, 150)),
            VerticalAlignment = VerticalAlignment.Center,
        };

        var headerText = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            Spacing = 6,
            Background = new SolidColorBrush(Colors.Transparent),
        };
        headerText.Children.Add(_statusDot);
        headerText.Children.Add(new TextBlock
        {
            Text = "音拓 HUD",
            FontSize = 11.5,
            VerticalAlignment = VerticalAlignment.Center,
            Foreground = Res("TextFillColorSecondaryBrush"),
        });

        var hide = new Button
        {
            Content = new FontIcon { Glyph = "\uE711", FontSize = 10 },
            Width = 24,
            Height = 24,
            Padding = new Thickness(0),
            CornerRadius = new CornerRadius(12),
            Background = new SolidColorBrush(Colors.Transparent),
            BorderThickness = new Thickness(0),
            VerticalAlignment = VerticalAlignment.Center,
        };
        ToolTipService.SetToolTip(hide, "隐藏悬浮窗");
        // 别让按钮上的按下事件冒泡到拖动区，否则点关闭会顺带挪动窗口。
        hide.PointerPressed += (_, e) => e.Handled = true;
        hide.Click += (_, _) => HideFloating();

        _dragArea = new Grid { ColumnSpacing = 6, Background = new SolidColorBrush(Colors.Transparent) };
        _dragArea.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        _dragArea.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        _dragArea.Children.Add(headerText);
        Grid.SetColumn(hide, 1);
        _dragArea.Children.Add(hide);
        _dragArea.PointerPressed += Drag_PointerPressed;
        _dragArea.PointerMoved += Drag_PointerMoved;
        _dragArea.PointerReleased += Drag_PointerReleased;
        _dragArea.PointerCaptureLost += (_, _) => _dragging = false;

        _rowsPanel = new StackPanel { Spacing = 1 };
        var body = new StackPanel { Spacing = 6 };
        body.Children.Add(_dragArea);
        body.Children.Add(_rowsPanel);

        var card = new Border
        {
            Background = Res("CardBackgroundFillColorDefaultBrush"),
            BorderBrush = Res("CardStrokeColorDefaultBrush"),
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(10),
            Padding = new Thickness(10, 8, 10, 8),
            Child = body,
        };
        _root = new Grid { Padding = new Thickness(0) };
        _root.Children.Add(card);
        Content = _root;

        BuildRows();
        var hwnd = WindowNative.GetWindowHandle(this);
        int round = 2;   // DWMWCP_ROUND
        DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ref round, sizeof(int));
        ApplyOpacity();
    }

    private static Brush Res(string key)
        => (Brush)Application.Current.Resources[key];

    /// <summary>按当前配置重建显示项（勾选变化后调用）。</summary>
    public void Rebuild()
    {
        BuildRows();
        FitSize();
        if (AppWindow.IsVisible) ApplyOpacity();
    }

    private void BuildRows()
    {
        _rowsPanel.Children.Clear();
        _rows.Clear();
        foreach (var id in FloatingConfig.AllItems)
        {
            if (!_config.Items.Contains(id)) continue;
            _rowsPanel.Children.Add(BuildRow(id));
        }
        // 至少要有一行，否则窗口只剩标题。
        if (_rowsPanel.Children.Count == 0)
            _rowsPanel.Children.Add(BuildRow("status"));
    }

    private Grid BuildRow(string id)
    {
        var grid = new Grid { ColumnSpacing = 8, Height = 20 };
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(46) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(64) });

        grid.Children.Add(new TextBlock
        {
            Text = LabelOf(id),
            FontSize = 11,
            VerticalAlignment = VerticalAlignment.Center,
            Foreground = Res("TextFillColorSecondaryBrush"),
        });

        var value = new TextBlock
        {
            Text = "—",
            FontSize = 11.5,
            FontWeight = Microsoft.UI.Text.FontWeights.SemiBold,
            FontFamily = new FontFamily("Consolas"),
            TextAlignment = TextAlignment.Right,
            VerticalAlignment = VerticalAlignment.Center,
        };

        ProgressBar? bar = null;
        if (id is "input" or "output" or "cpu" or "gpu" or "ram")
        {
            bar = new ProgressBar
            {
                Height = 4,
                Minimum = 0,
                Maximum = 100,
                VerticalAlignment = VerticalAlignment.Center,
                Margin = new Thickness(0, 2, 0, 0),
            };
            Grid.SetColumn(bar, 1);
            grid.Children.Add(bar);
            Grid.SetColumn(value, 2);
        }
        else
        {
            Grid.SetColumn(value, 1);
            Grid.SetColumnSpan(value, 2);
            value.TextAlignment = TextAlignment.Left;
        }
        grid.Children.Add(value);

        _rows[id] = new RowView { Value = value, Bar = bar };
        return grid;
    }

    private static string LabelOf(string id) => id switch
    {
        "status" => "状态",
        "ai" => "档位",
        "input" => "输入",
        "output" => "输出",
        "latency" => "延迟",
        "cpu" => "CPU",
        "gpu" => "GPU",
        "ram" => "内存",
        _ => id,
    };

    /// <summary>显示/隐藏、位置、透明度都按配置来。</summary>
    public void ShowFloating()
    {
        ApplyOpacity();
        if (_config.X < 0 || _config.Y < 0)
        {
            var area = DisplayArea.GetFromWindowId(AppWindow.Id, DisplayAreaFallback.Primary).WorkArea;
            int width = WindowWidth();
            AppWindow.Move(new PointInt32(area.X + area.Width - width - 24, area.Y + 72));
            _config.X = AppWindow.Position.X;
            _config.Y = AppWindow.Position.Y;
            ConfigChanged?.Invoke();
        }
        else
        {
            AppWindow.Move(new PointInt32(_config.X, _config.Y));
        }
        FitSize();
        Activate();
        AppWindow.Show();
        ApplyOpacity();
    }

    public void HideFloating()
    {
        AppWindow.Hide();
        _config.Enabled = false;
        ConfigChanged?.Invoke();
    }

    /// <summary>主题跟着主界面走：浅色/深色/跟随系统。</summary>
    public void SetTheme(ElementTheme theme) => _root.RequestedTheme = theme;

    public void SetOpacity(double opacity)
    {
        _config.Opacity = Math.Clamp(opacity, 0.2, 1.0);
        ApplyOpacity();
    }

    private void ApplyOpacity()
    {
        try
        {
            var hwnd = WindowNative.GetWindowHandle(this);
            long style = GetWindowLongPtr(hwnd, GWL_EXSTYLE).ToInt64();
            long wanted = style | WS_EX_LAYERED | WS_EX_TOOLWINDOW;
            if (wanted != style)
                SetWindowLongPtr(hwnd, GWL_EXSTYLE, new IntPtr(wanted));
            byte alpha = (byte)Math.Round(Math.Clamp(_config.Opacity, 0.2, 1.0) * 255);
            SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA);
            SetWindowPos(hwnd, IntPtr.Zero, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED);
        }
        catch
        {
            // 老系统不支持分层窗口时退化为不透明，不影响使用。
        }
    }

    private int WindowWidth()
    {
        double scale = DpiScale();
        return (int)Math.Round(238 * scale);
    }

    private double DpiScale()
    {
        try
        {
            uint dpi = GetDpiForWindow(WindowNative.GetWindowHandle(this));
            return dpi > 0 ? dpi / 96.0 : 1.0;
        }
        catch
        {
            return 1.0;
        }
    }

    private void FitSize()
    {
        double scale = DpiScale();
        int rows = Math.Max(1, _rowsPanel.Children.Count);
        int heightDip = 34 + rows * 21 + 10;
        AppWindow.Resize(new SizeInt32(WindowWidth(), (int)Math.Round(heightDip * scale)));
    }

    // -- 拖动 ------------------------------------------------------------
    private void Drag_PointerPressed(object sender, PointerRoutedEventArgs e)
    {
        if (!_dragArea.CapturePointer(e.Pointer)) return;
        GetCursorPos(out _dragStartCursor);
        _dragStartPos = AppWindow.Position;
        _dragging = true;
    }

    private void Drag_PointerMoved(object sender, PointerRoutedEventArgs e)
    {
        if (!_dragging) return;
        if (!GetCursorPos(out var cursor)) return;
        AppWindow.Move(new PointInt32(
            _dragStartPos.X + cursor.X - _dragStartCursor.X,
            _dragStartPos.Y + cursor.Y - _dragStartCursor.Y));
    }

    private void Drag_PointerReleased(object sender, PointerRoutedEventArgs e)
    {
        if (!_dragging) return;
        _dragging = false;
        _dragArea.ReleasePointerCapture(e.Pointer);
        var pos = AppWindow.Position;
        _config.X = pos.X;
        _config.Y = pos.Y;
        ConfigChanged?.Invoke();
    }

    // -- 数据刷新 --------------------------------------------------------
    public void Update(bool running, bool loading, double inputDb, double outputDb,
                       string aiMode, double latencyMs,
                       double cpu, double gpu, double ram)
    {
        if (_rows.TryGetValue("status", out var status))
        {
            status.Value.Text = loading ? "加载中" : running ? "运行中" : "已停止";
            status.Value.Foreground = new SolidColorBrush(loading
                ? Color.FromArgb(255, 224, 166, 74)
                : running ? Color.FromArgb(255, 76, 201, 116) : Color.FromArgb(255, 150, 150, 150));
        }
        _statusDot.Fill = new SolidColorBrush(loading
            ? Color.FromArgb(255, 224, 166, 74)
            : running ? Color.FromArgb(255, 76, 201, 116) : Color.FromArgb(255, 150, 150, 150));

        if (_rows.TryGetValue("ai", out var ai))
            ai.Value.Text = aiMode switch
            {
                "gpu" => "GPU 档",
                "cpu" => "CPU 档",
                _ => "关闭",
            };
        if (_rows.TryGetValue("latency", out var latency))
            latency.Value.Text = $"{latencyMs:0.0} ms";
        if (_rows.TryGetValue("input", out var input))
        {
            input.Value.Text = DbText(inputDb);
            if (input.Bar is not null) input.Bar.Value = MeterValue(inputDb);
        }
        if (_rows.TryGetValue("output", out var output))
        {
            output.Value.Text = DbText(outputDb);
            if (output.Bar is not null) output.Bar.Value = MeterValue(outputDb);
        }
        if (_rows.TryGetValue("cpu", out var cpuRow))
        {
            cpuRow.Value.Text = $"{cpu:0}%";
            if (cpuRow.Bar is not null) cpuRow.Bar.Value = Math.Clamp(cpu, 0, 100);
        }
        if (_rows.TryGetValue("gpu", out var gpuRow))
        {
            bool has = gpu >= 0;
            gpuRow.Value.Text = has ? $"{gpu:0}%" : "无";
            if (gpuRow.Bar is not null) gpuRow.Bar.Value = has ? Math.Clamp(gpu, 0, 100) : 0;
        }
        if (_rows.TryGetValue("ram", out var ramRow))
        {
            ramRow.Value.Text = $"{ram:0}%";
            if (ramRow.Bar is not null) ramRow.Bar.Value = Math.Clamp(ram, 0, 100);
        }
    }

    private static double MeterValue(double db) => Math.Clamp((db + 60) / 60, 0, 1) * 100;

    private static string DbText(double db) => db <= -70 ? "−∞ dB" : $"{db:0.0} dB";
}

