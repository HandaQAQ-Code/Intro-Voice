using System.Runtime.InteropServices;

namespace MixerUI;

/// <summary>
/// 系统托盘图标（Win32 Shell_NotifyIcon）。
/// WinUI 3 没有内置托盘 API，主窗口关闭后靠它驻留后台：双击恢复窗口，
/// 右键菜单可显示/隐藏悬浮窗、启动/停止链路、退出。
/// </summary>
public sealed class TrayIcon : IDisposable
{
    private const string ClassName = "IntroVoiceTrayWindow";
    private const int WM_TRAY = 0x8000 + 1;   // WM_APP + 1
    private const int WM_LBUTTONUP = 0x0202;
    private const int WM_LBUTTONDBLCLK = 0x0203;
    private const int WM_RBUTTONUP = 0x0205;

    private const uint NIM_ADD = 0;
    private const uint NIM_MODIFY = 1;
    private const uint NIM_DELETE = 2;
    private const uint NIF_MESSAGE = 0x1;
    private const uint NIF_ICON = 0x2;
    private const uint NIF_TIP = 0x4;
    private const uint NIF_INFO = 0x10;
    private const uint MF_STRING = 0;
    private const uint MF_SEPARATOR = 0x800;
    private const uint TPM_RIGHTBUTTON = 0x2;
    private const uint TPM_RETURNCMD = 0x100;
    private const uint TPM_NONOTIFY = 0x80;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct NOTIFYICONDATAW
    {
        public int cbSize;
        public IntPtr hWnd;
        public uint uID;
        public uint uFlags;
        public uint uCallbackMessage;
        public IntPtr hIcon;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string szTip;
        public uint dwState;
        public uint dwStateMask;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string szInfo;
        public uint uVersion;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)] public string szInfoTitle;
        public uint dwInfoFlags;
        public Guid guidItem;
        public IntPtr hBalloonIcon;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct WNDCLASSW
    {
        public uint style;
        public IntPtr lpfnWndProc;
        public int cbClsExtra;
        public int cbWndExtra;
        public IntPtr hInstance;
        public IntPtr hIcon;
        public IntPtr hCursor;
        public IntPtr hbrBackground;
        [MarshalAs(UnmanagedType.LPWStr)] public string? lpszMenuName;
        [MarshalAs(UnmanagedType.LPWStr)] public string lpszClassName;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT
    {
        public int X;
        public int Y;
    }

    private delegate IntPtr WndProcDelegate(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern ushort RegisterClassW(ref WNDCLASSW lpWndClass);

    [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateWindowExW(uint dwExStyle, string lpClassName, string lpWindowName,
        uint dwStyle, int x, int y, int width, int height, IntPtr hWndParent, IntPtr hMenu,
        IntPtr hInstance, IntPtr lpParam);

    [DllImport("user32.dll")]
    private static extern IntPtr DefWindowProcW(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern bool DestroyWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool UnregisterClassW(string lpClassName, IntPtr hInstance);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr GetModuleHandleW(string? lpModuleName);

    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    private static extern bool Shell_NotifyIconW(uint dwMessage, ref NOTIFYICONDATAW lpData);

    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    private static extern int ExtractIconExW(string lpszFile, int nIconIndex,
        out IntPtr phiconLarge, out IntPtr phiconSmall, int nIcons);

    [DllImport("user32.dll")]
    private static extern IntPtr CreatePopupMenu();

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool AppendMenuW(IntPtr hMenu, uint uFlags, uint uIDNewItem, string? lpNewItem);

    [DllImport("user32.dll")]
    private static extern bool DestroyMenu(IntPtr hMenu);

    [DllImport("user32.dll")]
    private static extern uint TrackPopupMenu(IntPtr hMenu, uint uFlags, int x, int y,
        int nReserved, IntPtr hWnd, IntPtr prcRect);

    [DllImport("user32.dll")]
    private static extern bool GetCursorPos(out POINT lpPoint);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool PostMessageW(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    private readonly Action _showMain;
    private readonly Action _toggleFloating;
    private readonly Action _toggleEngine;
    private readonly Action _exit;
    private readonly Func<(bool Running, bool Floating)> _state;
    private readonly string _tip;
    private readonly WndProcDelegate _wndProc;

    private IntPtr _hwnd;
    private IntPtr _icon;
    private NOTIFYICONDATAW _data;
    private bool _added;
    private bool _disposed;

    public TrayIcon(string tip, Action showMain, Action toggleFloating,
                    Action toggleEngine, Action exit, Func<(bool Running, bool Floating)> state)
    {
        _tip = tip;
        _showMain = showMain;
        _toggleFloating = toggleFloating;
        _toggleEngine = toggleEngine;
        _exit = exit;
        _state = state;
        // 委托必须由字段持有：交给 RegisterClassW 的是函数指针，回收后会崩溃。
        _wndProc = WndProc;

        var wc = new WNDCLASSW
        {
            lpfnWndProc = Marshal.GetFunctionPointerForDelegate(_wndProc),
            hInstance = GetModuleHandleW(null),
            lpszClassName = ClassName,
        };
        RegisterClassW(ref wc);
        _hwnd = CreateWindowExW(0, ClassName, "Intro Voice Tray", 0,
            0, 0, 0, 0, IntPtr.Zero, IntPtr.Zero, wc.hInstance, IntPtr.Zero);
        if (_hwnd == IntPtr.Zero)
            throw new InvalidOperationException("无法创建托盘消息窗口");

        var exe = Environment.ProcessPath;
        if (!string.IsNullOrEmpty(exe))
        {
            ExtractIconExW(exe, 0, out var large, out var small, 1);
            _icon = small != IntPtr.Zero ? small : large;
        }

        _data = new NOTIFYICONDATAW
        {
            cbSize = Marshal.SizeOf<NOTIFYICONDATAW>(),
            hWnd = _hwnd,
            uID = 1,
            uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP,
            uCallbackMessage = WM_TRAY,
            hIcon = _icon,
            szTip = tip,
            szInfo = "",
            szInfoTitle = "",
        };
        _added = Shell_NotifyIconW(NIM_ADD, ref _data);
        if (!_added)
            throw new InvalidOperationException("无法添加托盘图标");
    }

    /// <summary>气泡提示：第一次收进托盘时告诉用户程序还在后台。</summary>
    public void ShowBalloon(string title, string text)
    {
        if (!_added || _disposed) return;
        var data = _data;
        data.uFlags = NIF_INFO;
        data.szInfoTitle = title;
        data.szInfo = text;
        data.dwInfoFlags = 0x1;   // NIIF_INFO
        Shell_NotifyIconW(NIM_MODIFY, ref data);
    }

    private IntPtr WndProc(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam)
    {
        if (msg == WM_TRAY)
        {
            int mouse = (int)(lParam.ToInt64() & 0xFFFF);
            if (mouse == WM_LBUTTONDBLCLK)
                _showMain();
            else if (mouse == WM_RBUTTONUP)
                ShowMenu();
            return IntPtr.Zero;
        }
        return DefWindowProcW(hWnd, msg, wParam, lParam);
    }

    private void ShowMenu()
    {
        var (running, floating) = _state();
        var menu = CreatePopupMenu();
        AppendMenuW(menu, MF_STRING, 1, "显示主界面");
        AppendMenuW(menu, MF_STRING, 2, floating ? "隐藏悬浮窗" : "显示悬浮窗");
        AppendMenuW(menu, MF_STRING, 3, running ? "停止音频链路" : "启动音频链路");
        AppendMenuW(menu, MF_SEPARATOR, 0, null);
        AppendMenuW(menu, MF_STRING, 4, "退出");
        GetCursorPos(out var pt);
        // 先抢到前台，否则点菜单外面时菜单不会消失。
        SetForegroundWindow(_hwnd);
        uint cmd = TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
            pt.X, pt.Y, 0, _hwnd, IntPtr.Zero);
        PostMessageW(_hwnd, 0, IntPtr.Zero, IntPtr.Zero);
        DestroyMenu(menu);

        switch (cmd)
        {
            case 1: _showMain(); break;
            case 2: _toggleFloating(); break;
            case 3: _toggleEngine(); break;
            case 4: _exit(); break;
        }
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        try
        {
            if (_added) Shell_NotifyIconW(NIM_DELETE, ref _data);
            _added = false;
        }
        catch
        {
            // 资源管理器重启后托盘句柄可能失效，忽略即可。
        }
        if (_hwnd != IntPtr.Zero)
        {
            DestroyWindow(_hwnd);
            _hwnd = IntPtr.Zero;
        }
        UnregisterClassW(ClassName, GetModuleHandleW(null));
        if (_icon != IntPtr.Zero)
        {
            DestroyIcon(_icon);
            _icon = IntPtr.Zero;
        }
    }

    [DllImport("user32.dll")]
    private static extern bool DestroyIcon(IntPtr hIcon);
}
