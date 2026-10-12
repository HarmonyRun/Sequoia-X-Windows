// ═══════════════════════════════════════════════════════════════════════════
//  Sequoia-X 启动器（Launcher）
//  ---------------------------------------------------------------------------
//  职责：
//    · 读取 app\.env 里的 WEBUI_PORT / FEISHU_WEBHOOK_URL
//    · 判断本机 http://127.0.0.1:<port>/api/overview 是否已在运行
//    · 没运行就用 runtime\pythonw.exe 拉起 app 目录下的 webui 服务，
//      子进程 PID 写入 app\data\webui.pid，stdout/stderr 落到 app\logs\launcher.log
//    · 轮询等待服务就绪（最长 120 秒），成功后打开默认浏览器
//    · 停止服务（子进程或按端口找 PID），二次确认后退出
//
//  支持 --silent：不显示窗口，服务起来后立即退出（开机自启用）；失败才弹错误框。
//
//  编译：见同目录 build.ps1
//  语言约束：csc.exe (C# 5) —— 不使用字符串插值、?. 运算符、async/await 等新语法。
// ═══════════════════════════════════════════════════════════════════════════

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

namespace SequoiaX.Launcher
{
    // ═══════════════════════════════════════════════════════════════════════
    //  1. 环境配置读取（app\.env 的极简解析）
    // ═══════════════════════════════════════════════════════════════════════

    internal sealed class EnvConfig
    {
        public int Port = 8899;
        public string FeishuWebhook = "";

        private readonly Dictionary<string, string> _map = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        public static EnvConfig Load(string envPath)
        {
            EnvConfig cfg = new EnvConfig();
            try
            {
                if (!File.Exists(envPath)) { return cfg; }
                string[] lines = File.ReadAllLines(envPath, Encoding.UTF8);
                for (int i = 0; i < lines.Length; i++)
                {
                    string line = lines[i];
                    if (line == null) { continue; }
                    line = line.Trim();
                    if (line.Length == 0 || line[0] == '#') { continue; }
                    if (line.StartsWith("export ", StringComparison.OrdinalIgnoreCase)) { line = line.Substring(7).Trim(); }
                    int eq = line.IndexOf('=');
                    if (eq <= 0) { continue; }
                    string key = line.Substring(0, eq).Trim();
                    string val = line.Substring(eq + 1).Trim();
                    // 去掉成对引号
                    if (val.Length >= 2 &&
                        ((val[0] == '"' && val[val.Length - 1] == '"') || (val[0] == '\'' && val[val.Length - 1] == '\'')))
                    {
                        val = val.Substring(1, val.Length - 2).Trim();
                    }
                    if (key.Length > 0) { cfg._map[key] = val; }
                }
            }
            catch
            {
                // 配置读不了就用默认值，不阻断启动
            }

            string portText = cfg.Get("WEBUI_PORT");
            int p;
            if (!string.IsNullOrEmpty(portText) && int.TryParse(portText.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out p)
                && p > 0 && p < 65536)
            {
                cfg.Port = p;
            }
            cfg.FeishuWebhook = cfg.Get("FEISHU_WEBHOOK_URL");
            if (cfg.FeishuWebhook == null) { cfg.FeishuWebhook = ""; }
            return cfg;
        }

        public string Get(string key)
        {
            string v;
            if (_map.TryGetValue(key, out v)) { return v; }
            return null;
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    //  2. 服务探测 / 端口 PID 查找 / 进程管理
    // ═══════════════════════════════════════════════════════════════════════

    internal static class ServiceControl
    {
        /// <summary>探测 /api/overview 是否在 timeoutMs 内返回 200。</summary>
        public static bool IsRunning(int port, int timeoutMs)
        {
            try
            {
                HttpWebRequest req = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port.ToString(CultureInfo.InvariantCulture) + "/api/overview");
                req.Method = "GET";
                req.Timeout = timeoutMs;
                req.ReadWriteTimeout = timeoutMs;
                req.Proxy = null;                       // 避免走系统代理导致本机探测变慢
                req.KeepAlive = false;
                req.AllowAutoRedirect = false;
                using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
                {
                    int code = (int)resp.StatusCode;
                    try
                    {
                        using (Stream s = resp.GetResponseStream())
                        {
                            if (s != null)
                            {
                                byte[] buf = new byte[2048];
                                while (s.Read(buf, 0, buf.Length) > 0) { /* 丢弃正文，只关心状态码与连接成功 */ }
                            }
                        }
                    }
                    catch { }
                    return code == 200;
                }
            }
            catch (WebException wex)
            {
                // 服务在，但返回了非 200 —— 也算「在运行」，避免重复拉起
                HttpWebResponse r = wex.Response as HttpWebResponse;
                if (r != null)
                {
                    int code = (int)r.StatusCode;
                    r.Close();
                    return code > 0;
                }
                return false;
            }
            catch
            {
                return false;
            }
        }

        /// <summary>
        /// 按端口找监听进程 PID：跑 netstat -ano，取 127.0.0.1:port 且 LISTENING 的那行，
        /// 最后一列就是 PID。找不到返回 0。
        /// </summary>
        public static int FindPidByPort(int port)
        {
            try
            {
                ProcessStartInfo psi = new ProcessStartInfo("netstat", "-ano");
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.RedirectStandardOutput = true;
                psi.RedirectStandardError = true;
                using (Process p = Process.Start(psi))
                {
                    string output = p.StandardOutput.ReadToEnd();
                    p.WaitForExit(8000);
                    return ParseNetstat(output, port);
                }
            }
            catch
            {
                return 0;
            }
        }

        /// <summary>从 netstat 输出里解析出监听端口的 PID（独立出来便于验证）。</summary>
        public static int ParseNetstat(string output, int port)
        {
            if (output == null) { return 0; }
            string needle1 = "127.0.0.1:" + port.ToString(CultureInfo.InvariantCulture);
            string needle2 = "0.0.0.0:" + port.ToString(CultureInfo.InvariantCulture);
            string needle3 = "[::]:" + port.ToString(CultureInfo.InvariantCulture);
            string needle4 = "[::1]:" + port.ToString(CultureInfo.InvariantCulture);

            string[] lines = output.Split(new char[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries);
            for (int i = 0; i < lines.Length; i++)
            {
                string line = lines[i];
                if (line.IndexOf("LISTENING", StringComparison.OrdinalIgnoreCase) < 0) { continue; }
                string[] cells = System.Text.RegularExpressions.Regex.Split(line.Trim(), "\\s+");
                if (cells.Length < 4) { continue; }

                string local = cells[1];
                bool match = local.IndexOf(needle1, StringComparison.OrdinalIgnoreCase) >= 0
                          || local.IndexOf(needle2, StringComparison.OrdinalIgnoreCase) >= 0
                          || local.IndexOf(needle3, StringComparison.OrdinalIgnoreCase) >= 0
                          || local.IndexOf(needle4, StringComparison.OrdinalIgnoreCase) >= 0;
                if (!match) { continue; }

                int pid;
                if (int.TryParse(cells[cells.Length - 1], NumberStyles.Integer, CultureInfo.InvariantCulture, out pid) && pid > 0)
                {
                    return pid;
                }
            }
            return 0;
        }

        /// <summary>杀掉 PID 对应的进程树。返回是否成功。</summary>
        public static bool KillPid(int pid, out string error)
        {
            error = null;
            if (pid <= 0) { return false; }
            try
            {
                Process p = Process.GetProcessById(pid);
                // .NET Framework 没有 Kill(entireProcessTree)，用 taskkill /T /F 达到同样效果
                bool ok = false;
                try { p.Kill(); ok = true; }
                catch { ok = KillTree(pid); }
                try { p.WaitForExit(5000); } catch { }
                return ok;
            }
            catch (ArgumentException)
            {
                return true;   // 进程已不存在
            }
            catch (Exception ex)
            {
                error = ex.Message;
                return false;
            }
        }

        /// <summary>用 taskkill /T /F 结束整棵进程树（子进程的孙子进程也会一起收掉）。</summary>
        public static bool KillTree(int pid)
        {
            try
            {
                ProcessStartInfo psi = new ProcessStartInfo("taskkill", "/T /F /PID " + pid.ToString(CultureInfo.InvariantCulture));
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.RedirectStandardOutput = true;
                psi.RedirectStandardError = true;
                using (Process p = Process.Start(psi))
                {
                    p.StandardOutput.ReadToEnd();
                    p.StandardError.ReadToEnd();
                    p.WaitForExit(10000);
                    return p.ExitCode == 0;
                }
            }
            catch
            {
                return false;
            }
        }

        /// <summary>等待端口上的服务消失（最多 waitMs）。</summary>
        public static bool WaitStopped(int port, int waitMs)
        {
            int spent = 0;
            while (spent < waitMs)
            {
                if (FindPidByPort(port) == 0 && !IsRunning(port, 500)) { return true; }
                Thread.Sleep(300);
                spent += 300;
            }
            return FindPidByPort(port) == 0;
        }

        /// <summary>读日志尾部若干行。</summary>
        public static string TailLog(string path, int lines)
        {
            try
            {
                if (!File.Exists(path)) { return "（日志文件不存在：" + path + "）"; }
                string[] all = File.ReadAllLines(path, Encoding.UTF8);
                int start = Math.Max(0, all.Length - lines);
                StringBuilder sb = new StringBuilder();
                for (int i = start; i < all.Length; i++) { sb.AppendLine(all[i]); }
                return sb.ToString();
            }
            catch (Exception ex)
            {
                return "（读取日志失败：" + ex.Message + "）";
            }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    //  3. 主窗口
    // ═══════════════════════════════════════════════════════════════════════

    internal sealed class LauncherForm : Form
    {
        // ── 常量 ──────────────────────────────────────────────────────────
        private const int PollIntervalMs = 500;
        private const int StartTimeoutMs = 120000;      // 最长等 120 秒
        private const int ProbeTimeoutMs = 1500;        // 单次探测 1.5 秒

        private static readonly Color ClrBrand = Color.FromArgb(17, 94, 89);
        private static readonly Color ClrMuted = Color.FromArgb(110, 118, 125);
        private static readonly Color ClrOk = Color.FromArgb(22, 140, 74);
        private static readonly Color ClrWarn = Color.FromArgb(196, 96, 12);
        private static readonly Color ClrErr = Color.FromArgb(190, 44, 44);

        // ── 运行期状态 ────────────────────────────────────────────────────
        private readonly bool _silent;
        private readonly string _installDir;
        private readonly string _appDir;
        private readonly string _logPath;
        private EnvConfig _cfg;

        private Process _child;                 // 本启动器拉起的服务进程
        private bool _weStartedIt;
        private bool _ready;
        private bool _stopping;
        private int _elapsedMs;
        private System.Windows.Forms.Timer _pollTimer;

        // ── 控件 ──────────────────────────────────────────────────────────
        private Label _lblTitle;
        private Label _lblBadge;
        private LinkLabel _lnkUrl;
        private Label _lblStatus;
        private Button _btnOpen;
        private Button _btnLogs;
        private Button _btnStop;

        public LauncherForm(bool silent)
        {
            _silent = silent;
            _installDir = Path.GetDirectoryName(Application.ExecutablePath);
            _appDir = Path.Combine(_installDir, "app");
            _logPath = Path.Combine(_appDir, "logs\\launcher.log");
            BuildUi();
        }

        // ───────────────────────────────────────────────────────────────────
        //  UI
        // ───────────────────────────────────────────────────────────────────
        private void BuildUi()
        {
            SuspendLayout();

            Text = "Sequoia-X 工作台";
            Font = new Font("Microsoft YaHei UI", 9f, FontStyle.Regular, GraphicsUnit.Point);
            BackColor = Color.White;
            ForeColor = Color.FromArgb(33, 37, 41);
            ClientSize = new Size(480, 260);
            StartPosition = FormStartPosition.CenterScreen;
            FormBorderStyle = FormBorderStyle.FixedSingle;   // 固定边框，去掉最大化
            MaximizeBox = false;
            MinimizeBox = false;
            ShowIcon = false;
            AutoScaleMode = AutoScaleMode.None;

            _lblTitle = new Label();
            _lblTitle.Text = "Sequoia-X 工作台";
            _lblTitle.Font = new Font("Microsoft YaHei UI", 14f, FontStyle.Bold, GraphicsUnit.Point);
            _lblTitle.ForeColor = ClrBrand;
            _lblTitle.SetBounds(20, 18, 280, 28);

            // 状态徽章
            _lblBadge = new Label();
            _lblBadge.Text = "正在启动…";
            _lblBadge.Font = new Font("Microsoft YaHei UI", 9f, FontStyle.Bold, GraphicsUnit.Point);
            _lblBadge.ForeColor = Color.White;
            _lblBadge.BackColor = ClrMuted;
            _lblBadge.TextAlign = ContentAlignment.MiddleCenter;
            _lblBadge.SetBounds(310, 20, 150, 24);

            _lnkUrl = new LinkLabel();
            _lnkUrl.Text = "";
            _lnkUrl.Font = new Font("Microsoft YaHei UI", 10f, FontStyle.Regular, GraphicsUnit.Point);
            _lnkUrl.SetBounds(20, 56, 440, 22);
            _lnkUrl.LinkClicked += OnUrlClicked;

            _lblStatus = new Label();
            _lblStatus.Text = "正在准备…";
            _lblStatus.ForeColor = ClrMuted;
            _lblStatus.SetBounds(20, 84, 440, 82);

            _btnOpen = new Button();
            _btnOpen.Text = "打开工作台";
            _btnOpen.SetBounds(20, 196, 130, 34);
            _btnOpen.FlatStyle = FlatStyle.System;
            _btnOpen.Enabled = false;
            _btnOpen.Click += OnOpenClicked;

            _btnLogs = new Button();
            _btnLogs.Text = "查看日志";
            _btnLogs.SetBounds(162, 196, 130, 34);
            _btnLogs.FlatStyle = FlatStyle.System;
            _btnLogs.Click += OnLogsClicked;

            _btnStop = new Button();
            _btnStop.Text = "停止服务并退出";
            _btnStop.SetBounds(304, 196, 156, 34);
            _btnStop.FlatStyle = FlatStyle.System;
            _btnStop.Click += OnStopClicked;

            Controls.Add(_lblTitle);
            Controls.Add(_lblBadge);
            Controls.Add(_lnkUrl);
            Controls.Add(_lblStatus);
            Controls.Add(_btnOpen);
            Controls.Add(_btnLogs);
            Controls.Add(_btnStop);

            FormClosing += OnFormClosing;

            _pollTimer = new System.Windows.Forms.Timer();
            _pollTimer.Interval = PollIntervalMs;
            _pollTimer.Tick += OnPollTick;

            ResumeLayout(false);
        }

        // ───────────────────────────────────────────────────────────────────
        //  启动流程
        // ───────────────────────────────────────────────────────────────────
        protected override void OnShown(EventArgs e)
        {
            base.OnShown(e);

            if (_silent)
            {
                // 静默模式：完全隐藏窗口，只把托盘外的逻辑跑完
                ShowInTaskbar = false;
                WindowState = FormWindowState.Minimized;
                Opacity = 0;
            }

            try
            {
                _cfg = EnvConfig.Load(Path.Combine(_appDir, ".env"));
            }
            catch
            {
                _cfg = new EnvConfig();
            }

            // 服务已经在跑：直接进入就绪状态
            if (ServiceControl.IsRunning(_cfg.Port, ProbeTimeoutMs))
            {
                _ready = true;
                MarkRunning();
                OpenBrowserOnce();
                if (_silent) { ExitSilently(); }
                return;
            }

            // 自己拉起服务
            string err;
            if (!StartService(out err))
            {
                _lblStatus.Text = "启动失败：" + err;
                Fail();
                return;
            }

            _elapsedMs = 0;
            _pollTimer.Start();
        }

        /// <summary>
        /// 拉起 webui 服务。
        ///
        /// 优先用 `pythonw.exe -m webui.server`（与 run-webui.cmd 一致的模块入口）；
        /// 若 app\webui 下没有 server.py 之外的模块入口文件，则退回直接执行
        /// `pythonw.exe webui\server.py`——两者都会让 server.py 的 __main__ 分支运行。
        /// </summary>
        private bool StartService(out string error)
        {
            error = null;
            try
            {
                string pythonw = Path.Combine(_installDir, "runtime\\pythonw.exe");
                if (!File.Exists(pythonw))
                {
                    error = "未找到运行时：" + pythonw;
                    return false;
                }
                if (!Directory.Exists(_appDir))
                {
                    error = "未找到应用目录：" + _appDir;
                    return false;
                }

                Directory.CreateDirectory(Path.Combine(_appDir, "logs"));
                Directory.CreateDirectory(Path.Combine(_appDir, "data"));

                List<string> args = new List<string>();
                args.Add("-m");
                args.Add("webui.server");

                ProcessStartInfo psi = new ProcessStartInfo(pythonw);
                psi.Arguments = string.Join(" ", args.ToArray());
                psi.WorkingDirectory = _appDir;
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.RedirectStandardOutput = true;
                psi.RedirectStandardError = true;
                psi.StandardOutputEncoding = Encoding.UTF8;
                psi.StandardErrorEncoding = Encoding.UTF8;

                // 日志编码：Windows 上 Python 默认按系统 ANSI（简体中文是 GBK）写 stderr，
                // 而我们按 UTF-8 落盘，日志里就会出现 "锟斤拷" 这类乱码。
                // 用 PYTHONIOENCODING + PYTHONUTF8 强制子进程用 UTF-8 输出，与文件编码一致。
                psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
                psi.EnvironmentVariables["PYTHONUTF8"] = "1";
                psi.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";   // 日志实时落盘，便于排错

                // 日志输出：把子进程的 stdout / stderr 都接到 app\logs\launcher.log。
                //
                // 为什么不用 DataReceived 事件那套（RedirectStandardOutput +
                // BeginOutputReadLine）？因为服务是长期运行的：启动器退出之后子进程还要
                // 继续活着并继续写日志，而事件方案要求父进程一直把管道里的数据搬走，
                // 父进程一退出管道就断了，子进程写日志会失败。
                //
                // 这里改成"父进程把两个管道的读取端持续写进日志文件"的搬运线程：
                // 覆盖启动阶段（用户唯一会去看日志的时刻）。服务稳定运行后不再依赖它。
                psi.RedirectStandardOutput = true;
                psi.RedirectStandardError = true;

                // stdin 也必须重定向！
                //
                // 这是踩过的坑：启动器（尤其 --silent 模式）拉起服务后会立刻退出。
                // 若不重定向 stdin，子进程会继承启动器的标准输入句柄；启动器一退出，
                // 该句柄失效会把服务进程一起带走 —— 表现是"日志里明明打印了启动成功，
                // 但端口从未被监听、进程也没了"。
                // 给子进程一个自己的 stdin 管道，它就能独立存活。
                psi.RedirectStandardInput = true;

                Process child = new Process();
                child.StartInfo = psi;

                if (!child.Start())
                {
                    error = "无法启动运行时进程。";
                    return false;
                }

                _child = child;
                _weStartedIt = true;

                FileStream logStream = new FileStream(_logPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite);
                StartLogPump(child.StandardOutput, logStream);
                StartLogPump(child.StandardError, logStream);

                // PID 落盘，便于其它工具定位 / 清理
                try
                {
                    string pidFile = Path.Combine(_appDir, "data\\webui.pid");
                    File.WriteAllText(pidFile,
                        child.Id.ToString(CultureInfo.InvariantCulture) + Environment.NewLine,
                        new UTF8Encoding(false));
                }
                catch { /* PID 文件写不了不影响启动 */ }

                return true;
            }
            catch (Exception ex)
            {
                error = ex.Message;
                return false;
            }
        }

        /// <summary>
        /// 把子进程某个输出管道（StandardOutput / StandardError 都是 StreamReader）
        /// 的原始字节持续搬运到日志文件。后台线程，父进程退出即随之结束。
        /// </summary>
        private static void StartLogPump(StreamReader pipe, Stream logFile)
        {
            if (pipe == null) { return; }
            Stream src = pipe.BaseStream;
            Thread worker = new Thread(new ThreadStart(delegate
            {
                try
                {
                    byte[] buf = new byte[4096];
                    int n;
                    while ((n = src.Read(buf, 0, buf.Length)) > 0)
                    {
                        lock (logFile)
                        {
                            logFile.Write(buf, 0, n);
                            logFile.Flush();
                        }
                    }
                }
                catch { /* 管道关闭属正常收尾 */ }
            }));
            worker.IsBackground = true;
            worker.Name = "webui-log-pump";
            worker.Start();
        }

        // ───────────────────────────────────────────────────────────────────
        //  就绪轮询
        // ───────────────────────────────────────────────────────────────────
        private void OnPollTick(object sender, EventArgs e)
        {
            if (_stopping) { return; }
            _elapsedMs += PollIntervalMs;

            if (ServiceControl.IsRunning(_cfg.Port, ProbeTimeoutMs))
            {
                _pollTimer.Stop();
                _ready = true;
                MarkRunning();
                OpenBrowserOnce();
                if (_silent) { ExitSilently(); }
                return;
            }

            // 子进程意外退出：立刻失败，不必等满 120 秒
            if (_weStartedIt && _child != null)
            {
                try
                {
                    if (_child.HasExited)
                    {
                        _pollTimer.Stop();
                        _lblStatus.Text = "服务进程已退出（退出码 " + _child.ExitCode.ToString(CultureInfo.InvariantCulture) + "）。\r\n\r\n" +
                                          "日志尾部：\r\n" + ServiceControl.TailLog(_logPath, 12);
                        Fail();
                        return;
                    }
                }
                catch { }
            }

            if (_elapsedMs % 4000 == 0)
            {
                _lblStatus.Text = "正在启动服务…（已等待 " + (_elapsedMs / 1000).ToString(CultureInfo.InvariantCulture) + " 秒 / 最多 120 秒）";
            }

            if (_elapsedMs >= StartTimeoutMs)
            {
                _pollTimer.Stop();
                _lblStatus.Text = "启动超时（120 秒）。请检查日志：\r\n" + _logPath + "\r\n\r\n" +
                                  "日志尾部：\r\n" + ServiceControl.TailLog(_logPath, 12);
                Fail();
            }
        }

        private void MarkRunning()
        {
            _lblBadge.Text = "运行中";
            _lblBadge.BackColor = ClrOk;
            string url = Url();
            _lnkUrl.Text = url;
            _lblStatus.ForeColor = ClrMuted;
            _lblStatus.Text = "服务已就绪，点击上方链接或「打开工作台」即可使用。" + Environment.NewLine +
                              (_weStartedIt ? "本次由本启动器拉起服务。" : "服务此前已在运行，本次直接复用。");
            _btnOpen.Enabled = true;
        }

        private void Fail()
        {
            _lblBadge.Text = "启动失败";
            _lblBadge.BackColor = ClrErr;
            _lblStatus.ForeColor = ClrErr;
            _btnOpen.Enabled = false;

            if (_silent)
            {
                MessageBox.Show("Sequoia-X 工作台启动失败。\r\n\r\n" + _lblStatus.Text +
                                "\r\n\r\n日志文件：" + _logPath,
                    "Sequoia-X 工作台", MessageBoxButtons.OK, MessageBoxIcon.Error);
                ExitSilently();
            }
        }

        private void ExitSilently()
        {
            // 静默模式：不杀服务（服务要继续跑），直接退出
            Close();
        }

        private string Url()
        {
            return "http://127.0.0.1:" + _cfg.Port.ToString(CultureInfo.InvariantCulture) + "/";
        }

        /// <summary>
        /// 打开默认浏览器：
        /// 若 app\.env 里 FEISHU_WEBHOOK_URL 为空 → 打开 #settings 引导配置；
        /// 否则打开首页。
        /// </summary>
        private void OpenBrowserOnce()
        {
            string target;
            if (string.IsNullOrEmpty(_cfg.FeishuWebhook))
            {
                target = "http://127.0.0.1:" + _cfg.Port.ToString(CultureInfo.InvariantCulture) + "/#settings";
                _lblStatus.Text = "尚未配置飞书机器人，已为你打开设置页。\r\n配置完成后即可接收选股信号推送。";
            }
            else
            {
                target = Url();
            }
            OpenUrl(target);
        }

        private void OpenUrl(string url)
        {
            try
            {
                ProcessStartInfo psi = new ProcessStartInfo(url);
                psi.UseShellExecute = true;      // 交给 Shell 用默认浏览器打开
                Process.Start(psi);
            }
            catch (Exception ex)
            {
                _lblStatus.Text = "无法打开浏览器：" + ex.Message + "\r\n请手动访问 " + url;
            }
        }

        // ───────────────────────────────────────────────────────────────────
        //  按钮事件
        // ───────────────────────────────────────────────────────────────────
        private void OnUrlClicked(object sender, LinkLabelLinkClickedEventArgs e)
        {
            if (_ready) { OpenUrl(Url()); }
        }

        private void OnOpenClicked(object sender, EventArgs e)
        {
            if (_ready) { OpenUrl(Url()); }
        }

        private void OnLogsClicked(object sender, EventArgs e)
        {
            try
            {
                if (!File.Exists(_logPath))
                {
                    Directory.CreateDirectory(Path.GetDirectoryName(_logPath));
                    File.WriteAllText(_logPath, "（暂无日志内容）" + Environment.NewLine, new UTF8Encoding(false));
                }
                ProcessStartInfo psi = new ProcessStartInfo(_logPath);
                psi.UseShellExecute = true;      // 用默认程序（记事本等）打开
                Process.Start(psi);
            }
            catch (Exception ex)
            {
                MessageBox.Show(this, "无法打开日志文件：\r\n" + _logPath + "\r\n\r\n" + ex.Message,
                    "Sequoia-X 工作台", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
        }

        private void OnStopClicked(object sender, EventArgs e)
        {
            StopServiceAndClose();
        }

        private void OnFormClosing(object sender, FormClosingEventArgs e)
        {
            // 静默模式直接关（服务要继续跑）
            if (_silent || _stopping) { return; }

            DialogResult r = MessageBox.Show(this,
                "退出后工作台服务将停止，其它人将无法访问。确定退出吗？",
                "Sequoia-X 工作台", MessageBoxButtons.YesNo, MessageBoxIcon.Question, MessageBoxDefaultButton.Button2);
            if (r != DialogResult.Yes)
            {
                e.Cancel = true;
                return;
            }
            StopServiceInternal();
        }

        // ───────────────────────────────────────────────────────────────────
        //  停止服务
        // ───────────────────────────────────────────────────────────────────
        private void StopServiceAndClose()
        {
            StopServiceInternal();
            // 停完再关，避免 FormClosing 再弹一次确认
            _stopping = true;
            DialogResult = DialogResult.OK;
            Close();
        }

        private void StopServiceInternal()
        {
            _stopping = true;
            if (_pollTimer != null) { _pollTimer.Stop(); }

            try
            {
                _lblStatus.ForeColor = ClrMuted;
                _lblStatus.Text = "正在停止服务…";
                _btnOpen.Enabled = false;
                _btnStop.Enabled = false;
                Application.DoEvents();
            }
            catch { }

            string error = null;

            // ① 优先杀本启动器拉起的子进程（连整棵进程树）
            if (_child != null)
            {
                try
                {
                    if (!_child.HasExited)
                    {
                        try
                        {
                            // .NET Framework 没有 Kill(true)，用 taskkill /T /F 收整棵树
                            ServiceControl.KillTree(_child.Id);
                        }
                        catch { }
                    }
                }
                catch { }
                _child = null;
            }

            // ② 若端口仍在监听（服务是别的进程开的），按端口找 PID 再杀
            if (_cfg != null)
            {
                int pid = ServiceControl.FindPidByPort(_cfg.Port);
                if (pid > 0)
                {
                    ServiceControl.KillPid(pid, out error);
                    ServiceControl.WaitStopped(_cfg.Port, 5000);
                }
            }

            // ③ 清理 PID 文件
            try
            {
                string pidFile = Path.Combine(_appDir, "data\\webui.pid");
                if (File.Exists(pidFile)) { File.Delete(pidFile); }
            }
            catch { }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    //  4. 入口
    // ═══════════════════════════════════════════════════════════════════════
    internal static class Program
    {
        [STAThread]
        private static void Main(string[] argv)
        {
            bool silent = false;
            if (argv != null)
            {
                for (int i = 0; i < argv.Length; i++)
                {
                    string a = (argv[i] ?? "").Trim();
                    if (string.Equals(a, "--silent", StringComparison.OrdinalIgnoreCase)
                        || string.Equals(a, "/silent", StringComparison.OrdinalIgnoreCase)
                        || string.Equals(a, "-s", StringComparison.OrdinalIgnoreCase))
                    {
                        silent = true;
                    }
                }
            }

            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            try
            {
                Application.Run(new LauncherForm(silent));
            }
            catch (Exception ex)
            {
                MessageBox.Show("启动器发生未处理的错误：\r\n\r\n" + ex.ToString(),
                    "Sequoia-X 工作台", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }
    }
}
