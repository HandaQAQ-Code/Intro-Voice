using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace MixerUI;

public sealed class ServiceException : Exception
{
    public ServiceException(string message) : base(message) { }
}

/// <summary>
/// Bridges the WinUI controls to the Python mixer service. Control only: audio
/// is streamed inside the service process and never crosses this pipe.
/// </summary>
public sealed class ServiceClient : IDisposable
{
    private const string PipeName = "RecordOutMixer";

    private readonly SemaphoreSlim _writeGate = new(1, 1);
    private readonly object _pendingLock = new();
    private readonly Dictionary<long, TaskCompletionSource<JsonElement>> _pending = new();
    private long _nextId;
    private NamedPipeClientStream? _pipe;
    private StreamReader? _reader;
    private StreamWriter? _writer;
    private Process? _serviceProcess;
    private volatile bool _closed;

    public static string Root { get; } = LocateRoot();
    public bool SpawnedService => _serviceProcess is not null;

    public ServiceClient()
    {
        LocatePython();
    }

    public string PythonPath { get; private set; } = "";

    private static string LocateRoot()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        for (var i = 0; i < 7 && dir is not null; i++)
        {
            if (File.Exists(Path.Combine(dir.FullName, "mixer_service.py")))
                return dir.FullName;
            dir = dir.Parent;
        }
        return AppContext.BaseDirectory;
    }

    private void LocatePython()
    {
        // Packaged build ships its own interpreter in runtime\ next to the scripts;
        // the development checkout uses the project virtual environment instead.
        foreach (var candidate in new[]
                 {
                     Path.Combine(Root, "runtime", "python.exe"),
                     Path.Combine(Root, ".venv", "Scripts", "python.exe"),
                 })
        {
            if (File.Exists(candidate))
            {
                PythonPath = candidate;
                return;
            }
        }
        var fallback = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                                    "Programs", "Python", "Python311", "python.exe");
        PythonPath = File.Exists(fallback) ? fallback : "python.exe";
    }

    public bool Connected => _pipe?.IsConnected == true && _reader is not null && !_closed;

    public async Task<string> ConnectAsync()
    {
        if (TryConnect(1500))
            return "已连接到调音台服务";

        if (!SpawnedService)
        {
            if (!File.Exists(Path.Combine(Root, "mixer_service.py")))
                throw new ServiceException($"未找到 mixer_service.py（根目录：{Root}）");
            var info = new ProcessStartInfo(PythonPath, "mixer_service.py")
            {
                WorkingDirectory = Root,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            _serviceProcess = Process.Start(info)
                ?? throw new ServiceException("无法启动调音台服务进程");
        }

        var deadline = DateTime.UtcNow.AddSeconds(60);
        while (DateTime.UtcNow < deadline)
        {
            if (TryConnect(600))
                return "已启动并连接调音台服务";
            if (SpawnedService && _serviceProcess is { HasExited: true })
                throw new ServiceException(
                    $"调音台服务启动后立即退出，请查看 data\\mixer\\service.log。Python：{PythonPath}");
            await Task.Delay(350);
        }
        throw new ServiceException("等待调音台服务连接超时，请查看 data\\mixer\\service.log");
    }

    private bool TryConnect(int timeoutMs)
    {
        // The pipe must be opened asynchronously: with the default synchronous
        // handle, a pending StreamReader.ReadLineAsync blocks the very next
        // StreamWriter.WriteLine forever (the service never sees the request).
        var pipe = new NamedPipeClientStream(".", PipeName, PipeDirection.InOut, PipeOptions.Asynchronous);
        try
        {
            pipe.Connect(timeoutMs);
        }
        catch
        {
            pipe.Dispose();
            return false;
        }
        _pipe = pipe;
        _reader = new StreamReader(pipe, new UTF8Encoding(false));
        _writer = new StreamWriter(pipe, new UTF8Encoding(false)) { AutoFlush = true };
        _ = Task.Run(ReadLoop);
        return true;
    }

    private async Task ReadLoop()
    {
        var reader = _reader;
        while (reader is not null)
        {
            string? line;
            try
            {
                line = await reader.ReadLineAsync().ConfigureAwait(false);
            }
            catch
            {
                line = null;
            }
            if (line is null)
            {
                FailPending("调音台服务连接已断开");
                return;
            }
            if (string.IsNullOrWhiteSpace(line)) continue;
            try
            {
                using var doc = JsonDocument.Parse(line);
                var root = doc.RootElement;
                if (!root.TryGetProperty("id", out var idElement)) continue;
                var id = idElement.GetInt64();
                TaskCompletionSource<JsonElement>? tcs;
                lock (_pendingLock)
                {
                    _pending.TryGetValue(id, out tcs);
                    if (tcs is not null) _pending.Remove(id);
                }
                if (tcs is null) continue;
                if (root.TryGetProperty("ok", out var ok) && ok.ValueKind == JsonValueKind.True)
                {
                    if (root.TryGetProperty("result", out var result))
                        tcs.TrySetResult(result.Clone());
                    else
                        tcs.TrySetResult(default);
                }
                else
                {
                    var error = root.TryGetProperty("error", out var e) ? e.GetString() : "未知错误";
                    tcs.TrySetException(new ServiceException(error ?? "未知错误"));
                }
            }
            catch
            {
                // A malformed line must not kill the reader thread.
            }
        }
    }

    private void FailPending(string message)
    {
        List<TaskCompletionSource<JsonElement>> waiting;
        lock (_pendingLock)
        {
            waiting = _pending.Values.ToList();
            _pending.Clear();
        }
        foreach (var tcs in waiting)
            tcs.TrySetException(new ServiceException(message));
    }

    public async Task<JsonElement> CallAsync(string cmd, JsonObject? args = null, int timeoutMs = 60000)
    {
        if (!Connected || _writer is null)
            throw new ServiceException("未连接到调音台服务");
        var id = Interlocked.Increment(ref _nextId);
        var message = new JsonObject
        {
            ["id"] = id,
            ["cmd"] = cmd,
        };
        if (args is not null)
        {
            foreach (var pair in args)
                message[pair.Key] = pair.Value?.DeepClone();
        }
        var text = message.ToJsonString(new JsonSerializerOptions
        {
            Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        });

        var tcs = new TaskCompletionSource<JsonElement>();
        lock (_pendingLock) _pending[id] = tcs;
        try
        {
            await _writeGate.WaitAsync();
            try
            {
                // WriteLineAsync keeps a slow service from blocking the caller;
                // StreamWriter itself is not safe for concurrent writers.
                await _writer.WriteLineAsync(text).WaitAsync(TimeSpan.FromSeconds(10));
            }
            finally
            {
                _writeGate.Release();
            }
        }
        catch (Exception ex)
        {
            lock (_pendingLock) _pending.Remove(id);
            throw new ServiceException($"发送命令 {cmd} 失败：{ex.Message}");
        }

        try
        {
            return await tcs.Task.WaitAsync(TimeSpan.FromMilliseconds(timeoutMs));
        }
        catch (TimeoutException)
        {
            lock (_pendingLock) _pending.Remove(id);
            throw new ServiceException($"命令 {cmd} 超时（{timeoutMs / 1000} 秒）");
        }
    }

    public void Dispose()
    {
        if (_closed) return;
        _closed = true;
        try
        {
            if (Connected)
            {
                var shutdown = new JsonObject { ["id"] = 0, ["cmd"] = "shutdown" };
                if (_writeGate.Wait(1000))
                {
                    try { _writer!.WriteLine(shutdown.ToJsonString()); }
                    finally { _writeGate.Release(); }
                }
                if (SpawnedService && _serviceProcess is not null)
                    _serviceProcess.WaitForExit(3000);
            }
        }
        catch
        {
            // Pipe already broken.
        }
        try { _pipe?.Dispose(); } catch { }
        if (SpawnedService && _serviceProcess is { HasExited: false })
        {
            try { _serviceProcess.Kill(entireProcessTree: true); } catch { }
        }
    }
}
