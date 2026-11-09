using System.IO;
using Microsoft.UI.Xaml;

namespace MixerUI;

public partial class App : Application
{
    private MainWindow? _window;

    public App()
    {
        InitializeComponent();
        UnhandledException += (_, e) => Log(e.Exception);
        AppDomain.CurrentDomain.UnhandledException += (_, e) => Log(e.ExceptionObject as Exception);
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        try
        {
            Trace("OnLaunched begin");
            _window = new MainWindow();
            Trace("window constructed");
            _window.Activate();
            Trace("window activated");
        }
        catch (Exception ex)
        {
            Log(ex);
            throw;
        }
    }

    private static string? _touchedPath;

    private static string? Root()
    {
        if (_touchedPath is not null) return _touchedPath;
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        for (var i = 0; i < 7 && dir is not null; i++)
        {
            if (File.Exists(Path.Combine(dir.FullName, "mixer_service.py"))
                || File.Exists(Path.Combine(dir.FullName, "mixer_service.exe"))
                || File.Exists(Path.Combine(dir.FullName, "mixer_service", "mixer_service.exe"))
                || File.Exists(Path.Combine(dir.FullName, "start_mixer.bat")))
            {
                _touchedPath = dir.FullName;
                return _touchedPath;
            }
            dir = dir.Parent;
        }
        return null;
    }

    internal static void Trace(string text)
    {
        try
        {
            var root = Root();
            if (root is null) return;
            var target = Path.Combine(root, "data", "mixer", "ui.log");
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            File.AppendAllText(target, $"[{DateTime.Now:yyyy-MM-dd HH:mm:ss.fff}] {text}\n");
        }
        catch
        {
        }
    }

    private static void Log(Exception? ex)
    {
        if (ex is null) return;
        Trace($"{ex}\n");
    }
}
