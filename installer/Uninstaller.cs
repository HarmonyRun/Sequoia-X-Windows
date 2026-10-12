// ═══════════════════════════════════════════════════════════════════════════
//  Sequoia-X 卸载器（Uninstaller）
//  ---------------------------------------------------------------------------
//  行为：
//    · 从自身目录读 install.json 拿到 install_dir（读不到就用自身所在目录）
//    · 询问是否「同时删除数据与配置」
//        未勾选：逐项删除，但保留 app\data 与 app\.env（用户的行情库与配置）
//        勾  选：整个安装目录删除
//    · 同时清理：桌面 / 开始菜单快捷方式、HKCU 卸载注册表项、HKCU Run 自启项
//    · 删除前先按端口杀掉正在运行的工作台服务
//    · 删不掉的文件汇总后在最后提示手动清理
//
//  命令行：
//    Uninstaller.exe [/S] [/KeepData]
//      /S         静默卸载：不显示窗口、不弹任何 MessageBox
//      /KeepData  保留 app\data 与 app\.env
//    注意：**默认行为就是保留数据与配置**（与界面默认一致），
//          /KeepData 只是显式写出来；想连数据一起删要显式给 --delete-data。
//    退出码：0 = 成功，1 = 有文件删不掉或发生异常（细节见 %TEMP%\sequoia-uninstall.log）
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
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

namespace SequoiaX.Uninstaller
{
    // ═══════════════════════════════════════════════════════════════════════
    //  1. 轻量 JSON 取值（读 install.json）
    // ═══════════════════════════════════════════════════════════════════════

    internal static class MiniJson
    {
        public static string GetString(string json, string key)
        {
            if (json == null) { return null; }
            int i = json.IndexOf("\"" + key + "\"", StringComparison.Ordinal);
            if (i < 0) { return null; }
            int colon = json.IndexOf(':', i + key.Length + 2);
            if (colon < 0) { return null; }
            int p = colon + 1;
            while (p < json.Length && char.IsWhiteSpace(json[p])) { p++; }
            if (p >= json.Length || json[p] != '"') { return null; }
            p++;
            StringBuilder sb = new StringBuilder();
            while (p < json.Length && json[p] != '"')
            {
                if (json[p] == '\\' && p + 1 < json.Length)
                {
                    p++;
                    char c = json[p];
                    if (c == 'n') { sb.Append('\n'); }
                    else if (c == 't') { sb.Append('\t'); }
                    else if (c == 'r') { sb.Append('\r'); }
                    else { sb.Append(c); }
                }
                else { sb.Append(json[p]); }
                p++;
            }
            return sb.ToString();
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    //  2. 服务停止（与 Launcher 相同的按端口找 PID 逻辑）
    // ═══════════════════════════════════════════════════════════════════════

    internal static class ServiceStopper
    {
        /// <summary>探测端口上的 /api/overview，判断服务是否在跑。</summary>
        public static bool IsRunning(int port, int timeoutMs)
        {
            try
            {
                HttpWebRequest req = (HttpWebRequest)WebRequest.Create(
                    "http://127.0.0.1:" + port.ToString(CultureInfo.InvariantCulture) + "/api/overview");
                req.Method = "GET";
                req.Timeout = timeoutMs;
                req.ReadWriteTimeout = timeoutMs;
                req.Proxy = null;
                req.KeepAlive = false;
                using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
                {
                    return (int)resp.StatusCode > 0;
                }
            }
            catch (WebException wex)
            {
                HttpWebResponse r = wex.Response as HttpWebResponse;
                if (r != null) { r.Close(); return true; }
                return false;
            }
            catch { return false; }
        }

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
            catch { return 0; }
        }

        /// <summary>解析 netstat -ano 输出，取监听指定端口的 PID。</summary>
        public static int ParseNetstat(string output, int port)
        {
            if (output == null) { return 0; }
            string[] needles = new string[]
            {
                "127.0.0.1:" + port.ToString(CultureInfo.InvariantCulture),
                "0.0.0.0:" + port.ToString(CultureInfo.InvariantCulture),
                "[::]:" + port.ToString(CultureInfo.InvariantCulture),
                "[::1]:" + port.ToString(CultureInfo.InvariantCulture)
            };
            string[] lines = output.Split(new char[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries);
            for (int i = 0; i < lines.Length; i++)
            {
                string line = lines[i];
                if (line.IndexOf("LISTENING", StringComparison.OrdinalIgnoreCase) < 0) { continue; }
                string[] cells = Regex.Split(line.Trim(), "\\s+");
                if (cells.Length < 4) { continue; }
                bool hit = false;
                for (int n = 0; n < needles.Length; n++)
                {
                    if (cells[1].IndexOf(needles[n], StringComparison.OrdinalIgnoreCase) >= 0) { hit = true; break; }
                }
                if (!hit) { continue; }
                int pid;
                if (int.TryParse(cells[cells.Length - 1], NumberStyles.Integer, CultureInfo.InvariantCulture, out pid) && pid > 0)
                {
                    return pid;
                }
            }
            return 0;
        }

        /// <summary>结束指定 PID 的整棵进程树（taskkill /T /F）。</summary>
        public static bool KillTree(int pid)
        {
            if (pid <= 0) { return true; }
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
            catch { return false; }
        }

        /// <summary>
        /// 尽力停止 Sequoia-X 服务：先按 app\data\webui.pid 杀，再按端口找 PID 杀。
        /// </summary>
        public static void StopAll(string installDir)
        {
            // ① PID 文件
            try
            {
                string pidFile = Path.Combine(installDir, "app\\data\\webui.pid");
                if (File.Exists(pidFile))
                {
                    string text = File.ReadAllText(pidFile).Trim();
                    int pid;
                    if (int.TryParse(text, NumberStyles.Integer, CultureInfo.InvariantCulture, out pid) && pid > 0)
                    {
                        KillTree(pid);
                    }
                }
            }
            catch { }

            // ② 按端口（8899 是默认端口；.env 里的实际端口优先）
            int port = ReadPort(Path.Combine(installDir, "app\\.env"));
            int byPort = FindPidByPort(port);
            if (byPort > 0) { KillTree(byPort); }

            // 给进程一点退出时间
            int spent = 0;
            while (spent < 5000)
            {
                if (FindPidByPort(port) == 0) { break; }
                Thread.Sleep(300);
                spent += 300;
            }
        }

        private static int ReadPort(string envPath)
        {
            try
            {
                if (!File.Exists(envPath)) { return 8899; }
                string[] lines = File.ReadAllLines(envPath, Encoding.UTF8);
                for (int i = 0; i < lines.Length; i++)
                {
                    string line = (lines[i] ?? "").Trim();
                    if (line.StartsWith("WEBUI_PORT", StringComparison.OrdinalIgnoreCase))
                    {
                        int eq = line.IndexOf('=');
                        if (eq > 0)
                        {
                            int p;
                            if (int.TryParse(line.Substring(eq + 1).Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out p)
                                && p > 0 && p < 65536)
                            {
                                return p;
                            }
                        }
                    }
                }
            }
            catch { }
            return 8899;
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    //  3. 文件删除
    // ═══════════════════════════════════════════════════════════════════════

    internal static class FileCleaner
    {
        /// <summary>
        /// 逐项删除 root 下的内容，但跳过 keepRelativePaths 里列出的相对路径
        /// （保留 app\data 与 app\.env）。返回未能删除的路径列表。
        /// </summary>
        public static List<string> DeleteContentsExcept(string root, string[] keepRelativePaths)
        {
            return DeleteContentsExcept(root, keepRelativePaths, null);
        }

        /// <summary>
        /// 同 DeleteContentsExcept，但额外跳过 skipPath（正在运行的卸载器自身）。
        /// 它删不掉是正常的，不能算作删除失败——退出后由延迟命令自删。
        /// </summary>
        public static List<string> DeleteContentsExcept(string root, string[] keepRelativePaths, string skipPath)
        {
            List<string> failures = new List<string>();
            HashSet<string> keep = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            for (int i = 0; i < keepRelativePaths.Length; i++)
            {
                keep.Add(NormalizeRel(keepRelativePaths[i]));
            }

            string skip = null;
            if (skipPath != null)
            {
                try { skip = Path.GetFullPath(skipPath); }
                catch { skip = null; }
            }

            try
            {
                string[] entries = Directory.GetFileSystemEntries(root);
                for (int i = 0; i < entries.Length; i++)
                {
                    DeleteEntry(entries[i], root, keep, failures, skip);
                }
            }
            catch (Exception ex)
            {
                failures.Add(root + "（枚举失败：" + ex.Message + "）");
            }
            return failures;
        }

        private static void DeleteEntry(string path, string root, HashSet<string> keep, List<string> failures, string skip)
        {
            // 正在运行的卸载器自身：跳过（由延迟自删命令处理）
            if (skip != null)
            {
                try
                {
                    if (string.Equals(Path.GetFullPath(path), skip, StringComparison.OrdinalIgnoreCase)) { return; }
                }
                catch { }
            }

            string rel = NormalizeRel(MakeRelative(root, path));

            // 该路径本身要保留
            if (keep.Contains(rel)) { return; }

            // 该路径是某个保留路径的祖先目录（例如 app 是 app\data 的祖先）：
            // 进入其中，只删除不需要保留的部分。
            bool isAncestor = false;
            foreach (string k in keep)
            {
                if (k.StartsWith(rel + "\\", StringComparison.OrdinalIgnoreCase)) { isAncestor = true; break; }
            }

            if (Directory.Exists(path))
            {
                if (isAncestor)
                {
                    try
                    {
                        string[] children = Directory.GetFileSystemEntries(path);
                        for (int i = 0; i < children.Length; i++)
                        {
                            DeleteEntry(children[i], root, keep, failures, skip);
                        }
                    }
                    catch (Exception ex)
                    {
                        failures.Add(path + "（枚举失败：" + ex.Message + "）");
                    }
                    return;
                }
                TryDeleteDirectory(path, failures);
                return;
            }

            // 普通文件
            try
            {
                File.SetAttributes(path, FileAttributes.Normal);
                File.Delete(path);
            }
            catch (Exception ex)
            {
                failures.Add(path + "（" + ex.Message + "）");
            }
        }

        /// <summary>
        /// 删除整个目录；失败时递归重试并把删不掉的记下来。
        /// skipPath 指定一个本轮无法删除的路径（正在运行的卸载器自身），跳过它且不记为失败。
        /// </summary>
        public static void TryDeleteDirectory(string dir, List<string> failures)
        {
            TryDeleteDirectory(dir, failures, null);
        }

        public static void TryDeleteDirectory(string dir, List<string> failures, string skipPath)
        {
            string skip = (skipPath == null) ? null : Path.GetFullPath(skipPath);

            try
            {
                Directory.Delete(dir, true);
                return;
            }
            catch { }

            // 递归删除：跳过占用中的文件
            try
            {
                string[] files = Directory.GetFiles(dir, "*", SearchOption.AllDirectories);
                for (int i = 0; i < files.Length; i++)
                {
                    if (skip != null && string.Equals(Path.GetFullPath(files[i]), skip, StringComparison.OrdinalIgnoreCase))
                    {
                        continue;   // 卸载器自身，交给延迟自删
                    }
                    try
                    {
                        File.SetAttributes(files[i], FileAttributes.Normal);
                        File.Delete(files[i]);
                    }
                    catch (Exception ex)
                    {
                        failures.Add(files[i] + "（" + ex.Message + "）");
                    }
                }
            }
            catch { }

            // 自底向上删空目录
            try
            {
                List<string> dirs = new List<string>(Directory.GetDirectories(dir, "*", SearchOption.AllDirectories));
                dirs.Sort();
                dirs.Reverse();
                for (int i = 0; i < dirs.Count; i++)
                {
                    try { Directory.Delete(dirs[i], false); }
                    catch { /* 非空或占用，留着 */ }
                }
                Directory.Delete(dir, false);
            }
            catch (Exception ex)
            {
                failures.Add(dir + "（" + ex.Message + "）");
            }
        }

        private static string MakeRelative(string root, string full)
        {
            string r = root.TrimEnd('\\');
            if (full.StartsWith(r, StringComparison.OrdinalIgnoreCase))
            {
                return full.Substring(r.Length).TrimStart('\\');
            }
            return full;
        }

        private static string NormalizeRel(string p)
        {
            return (p ?? "").Replace('/', '\\').Trim('\\');
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    //  4. 快捷方式与注册表清理
    // ═══════════════════════════════════════════════════════════════════════

    internal static class CleanupUtil
    {
        public const string UninstallKey = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\Sequoia-X";
        public const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
        public const string RunValueName = "Sequoia-X";
        public const string ShortcutName = "Sequoia-X 工作台.lnk";

        public static void RemoveShortcuts(List<string> notes)
        {
            try
            {
                string desktop = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
                string lnk = Path.Combine(desktop, ShortcutName);
                if (File.Exists(lnk)) { File.Delete(lnk); }
            }
            catch (Exception ex) { notes.Add("桌面快捷方式未删除：" + ex.Message); }

            try
            {
                string programs = Environment.GetFolderPath(Environment.SpecialFolder.Programs);
                string folder = Path.Combine(programs, "Sequoia-X");
                if (Directory.Exists(folder))
                {
                    // 只删我们自己的 .lnk，然后尝试删空目录
                    try
                    {
                        string[] lnks = Directory.GetFiles(folder, "*.lnk");
                        for (int i = 0; i < lnks.Length; i++)
                        {
                            try { File.Delete(lnks[i]); } catch { }
                        }
                    }
                    catch { }
                    try { Directory.Delete(folder, true); }
                    catch { try { Directory.Delete(folder, false); } catch { } }
                }
            }
            catch (Exception ex) { notes.Add("开始菜单快捷方式未删除：" + ex.Message); }
        }

        public static void RemoveRegistry(List<string> notes)
        {
            try
            {
                using (RegistryKey k = Registry.CurrentUser.OpenSubKey(UninstallKey, true))
                {
                    if (k != null) { k.Dispose(); }
                }
                Registry.CurrentUser.DeleteSubKeyTree(UninstallKey, false);
            }
            catch (Exception ex) { notes.Add("卸载注册表项未删除：" + ex.Message); }

            try
            {
                using (RegistryKey k = Registry.CurrentUser.OpenSubKey(RunKey, true))
                {
                    if (k != null && k.GetValue(RunValueName) != null)
                    {
                        k.DeleteValue(RunValueName, false);
                    }
                }
            }
            catch (Exception ex) { notes.Add("开机自启项未删除：" + ex.Message); }
        }

        /// <summary>清理开始菜单里可能残留的旧目录名。</summary>
        public static void RemoveEmptyProgramsFolder(List<string> notes)
        {
            try
            {
                string programs = Environment.GetFolderPath(Environment.SpecialFolder.Programs);
                string folder = Path.Combine(programs, "Sequoia-X");
                if (Directory.Exists(folder))
                {
                    string[] rest = Directory.GetFileSystemEntries(folder);
                    if (rest.Length == 0) { Directory.Delete(folder, false); }
                }
            }
            catch { }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    //  4b. 自删调度 + 静默卸载日志
    // ═══════════════════════════════════════════════════════════════════════

    /// <summary>
    /// 卸载器自身的 exe 在运行中删不掉，交给一条在本进程退出后执行的 cmd 命令。
    /// 命令会重试若干次删除自身，最后尝试删掉已经变空的安装目录。
    /// </summary>
    internal static class SelfDeleteScheduler
    {
        public static void Schedule(string installDir, string selfPath)
        {
            try
            {
                if (string.IsNullOrEmpty(selfPath)) { return; }

                // 只有卸载器位于安装目录里才需要自删（否则删的是别的副本）
                if (!installDir.TrimEnd('\\').Equals(
                        Path.GetDirectoryName(selfPath).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
                {
                    return;
                }

                StringBuilder cmd = new StringBuilder();
                cmd.Append("/c ");
                // 4 次尝试，每次之间等 1 秒（第 1 次必然删不掉，本进程还占着文件）
                for (int i = 0; i < 4; i++)
                {
                    if (i > 0) { cmd.Append("ping 127.0.0.1 -n 2 >nul & "); }
                    cmd.Append("del /f /q \"").Append(selfPath).Append("\" >nul 2>&1 & ");
                }
                // 目录空了就一并删掉（保留数据时目录非空，rd 会失败，属预期）
                cmd.Append("rd /q \"").Append(installDir.TrimEnd('\\')).Append("\" >nul 2>&1");

                ProcessStartInfo psi = new ProcessStartInfo("cmd.exe", cmd.ToString());
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.WindowStyle = ProcessWindowStyle.Hidden;
                Process.Start(psi);
            }
            catch
            {
                // 自删失败不影响卸载结果，用户可手动删除
            }
        }
    }

    /// <summary>静默卸载的日志（%TEMP%\sequoia-uninstall.log）。</summary>
    internal static class UninstallLog
    {
        public static string LogPath()
        {
            return Path.Combine(Path.GetTempPath(), "sequoia-uninstall.log");
        }

        public static void WriteResult(string installDir, bool keptData,
                                       List<string> failures, List<string> notes)
        {
            StringBuilder sb = new StringBuilder();
            sb.AppendLine("Sequoia-X 静默卸载记录");
            sb.AppendLine("时间    ：" + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture));
            sb.AppendLine("命令行  ：" + Environment.CommandLine);
            sb.AppendLine("安装目录：" + installDir);
            sb.AppendLine("数据配置：" + (keptData ? "已保留（app\\data 与 app\\.env）" : "已删除"));
            sb.AppendLine("结果    ：" + (failures.Count == 0 ? "成功" : "有 " + failures.Count.ToString(CultureInfo.InvariantCulture) + " 项未能删除"));
            if (notes.Count > 0)
            {
                sb.AppendLine();
                sb.AppendLine("提示：");
                for (int i = 0; i < notes.Count; i++) { sb.AppendLine("  · " + notes[i]); }
            }
            if (failures.Count > 0)
            {
                sb.AppendLine();
                sb.AppendLine("未能删除（卸载器 exe 本身由退出后的延迟命令删除，属正常）：");
                for (int i = 0; i < failures.Count; i++) { sb.AppendLine("  · " + failures[i]); }
            }
            Write(sb.ToString());
        }

        public static void WriteError(string installDir, string message, Exception ex)
        {
            StringBuilder sb = new StringBuilder();
            sb.AppendLine("Sequoia-X 静默卸载失败记录");
            sb.AppendLine("时间    ：" + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture));
            sb.AppendLine("命令行  ：" + Environment.CommandLine);
            sb.AppendLine("安装目录：" + (string.IsNullOrEmpty(installDir) ? "(未知)" : installDir));
            sb.AppendLine("说明    ：" + (message == null ? "" : message));
            if (ex != null)
            {
                sb.AppendLine();
                sb.AppendLine("技术细节：");
                sb.AppendLine(ex.ToString());
            }
            Write(sb.ToString());
        }

        private static void Write(string text)
        {
            try { File.WriteAllText(LogPath(), text, new UTF8Encoding(true)); }
            catch { }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    //  5. 主窗口
    // ═══════════════════════════════════════════════════════════════════════

    internal sealed class UninstallForm : Form
    {
        private static readonly Color ClrBrand = Color.FromArgb(17, 94, 89);
        private static readonly Color ClrMuted = Color.FromArgb(110, 118, 125);
        private static readonly Color ClrErr = Color.FromArgb(190, 44, 44);

        private readonly string _selfDir;
        private string _installDir;

        private Label _lblTitle;
        private Label _lblQuestion;
        private Label _lblDataNote;
        private CheckBox _chkData;
        private Label _lblStatus;
        private Button _btnUninstall;
        private Button _btnCancel;
        private bool _busy;

        public UninstallForm()
        {
            _selfDir = Path.GetDirectoryName(Application.ExecutablePath);
            _installDir = ResolveInstallDir(_selfDir);
            BuildUi();
        }

        /// <summary>读 install.json 的 install_dir；读不到就用自身所在目录。</summary>
        private static string ResolveInstallDir(string selfDir)
        {
            try
            {
                string manifest = Path.Combine(selfDir, "install.json");
                if (File.Exists(manifest))
                {
                    string json = File.ReadAllText(manifest, Encoding.UTF8);
                    string dir = MiniJson.GetString(json, "install_dir");
                    if (!string.IsNullOrEmpty(dir) && Directory.Exists(dir))
                    {
                        return Path.GetFullPath(dir);
                    }
                }
            }
            catch { }
            return selfDir;
        }

        private void BuildUi()
        {
            SuspendLayout();

            Text = "卸载 Sequoia-X";
            Font = new Font("Microsoft YaHei UI", 9f, FontStyle.Regular, GraphicsUnit.Point);
            BackColor = Color.White;
            ForeColor = Color.FromArgb(33, 37, 41);
            ClientSize = new Size(460, 200);
            StartPosition = FormStartPosition.CenterScreen;
            FormBorderStyle = FormBorderStyle.FixedSingle;
            MaximizeBox = false;
            MinimizeBox = false;
            ShowIcon = false;
            AutoScaleMode = AutoScaleMode.None;

            _lblTitle = new Label();
            _lblTitle.Text = "卸载 Sequoia-X";
            _lblTitle.Font = new Font("Microsoft YaHei UI", 14f, FontStyle.Bold, GraphicsUnit.Point);
            _lblTitle.ForeColor = ClrBrand;
            _lblTitle.SetBounds(20, 16, 420, 28);

            _lblQuestion = new Label();
            _lblQuestion.Text = "确定要卸载 Sequoia-X 吗？";
            _lblQuestion.SetBounds(22, 52, 416, 20);

            _lblDataNote = new Label();
            _lblDataNote.Text = "你的行情数据库在 " + Path.Combine(_installDir, "app\\data\\") + "，是否一并删除？";
            _lblDataNote.ForeColor = ClrMuted;
            _lblDataNote.SetBounds(22, 74, 416, 20);

            _chkData = new CheckBox();
            _chkData.Text = "同时删除数据与配置（不可恢复）";
            _chkData.Checked = false;
            _chkData.SetBounds(22, 98, 416, 22);

            _lblStatus = new Label();
            _lblStatus.Text = "";
            _lblStatus.ForeColor = ClrMuted;
            _lblStatus.SetBounds(22, 124, 416, 20);

            _btnUninstall = new Button();
            _btnUninstall.Text = "卸载";
            _btnUninstall.SetBounds(238, 152, 100, 32);
            _btnUninstall.FlatStyle = FlatStyle.System;
            _btnUninstall.Click += OnUninstallClick;

            _btnCancel = new Button();
            _btnCancel.Text = "取消";
            _btnCancel.SetBounds(346, 152, 92, 32);
            _btnCancel.FlatStyle = FlatStyle.System;
            _btnCancel.Click += OnCancelClick;

            Controls.Add(_lblTitle);
            Controls.Add(_lblQuestion);
            Controls.Add(_lblDataNote);
            Controls.Add(_chkData);
            Controls.Add(_lblStatus);
            Controls.Add(_btnUninstall);
            Controls.Add(_btnCancel);

            ResumeLayout(false);
        }

        private void OnCancelClick(object sender, EventArgs e)
        {
            if (_busy) { return; }
            Close();
        }

        private void OnUninstallClick(object sender, EventArgs e)
        {
            if (_busy) { return; }

            bool keepData = !_chkData.Checked;
            if (keepData)
            {
                DialogResult r = MessageBox.Show(this,
                    "未勾选「同时删除数据与配置」，将保留：\r\n" +
                    "  · " + Path.Combine(_installDir, "app\\data") + "\r\n" +
                    "  · " + Path.Combine(_installDir, "app\\.env") + "\r\n\r\n" +
                    "确定继续卸载吗？",
                    "卸载 Sequoia-X", MessageBoxButtons.YesNo, MessageBoxIcon.Question, MessageBoxDefaultButton.Button1);
                if (r != DialogResult.Yes) { return; }
            }

            _busy = true;
            _btnUninstall.Enabled = false;
            _btnCancel.Enabled = false;
            _chkData.Enabled = false;
            _btnUninstall.Text = "正在卸载…";
            Cursor = Cursors.WaitCursor;

            List<string> failures = new List<string>();
            List<string> notes = new List<string>();

            try
            {
                // ① 停服务
                _lblStatus.Text = "正在停止工作台服务…";
                Application.DoEvents();
                ServiceStopper.StopAll(_installDir);

                // ② 删快捷方式
                _lblStatus.Text = "正在删除快捷方式…";
                Application.DoEvents();
                CleanupUtil.RemoveShortcuts(notes);

                // ③ 删注册表
                _lblStatus.Text = "正在清理注册表…";
                Application.DoEvents();
                CleanupUtil.RemoveRegistry(notes);

                // ④ 删文件
                _lblStatus.Text = "正在删除程序文件…";
                Application.DoEvents();

                string selfPath = Application.ExecutablePath;
                if (_chkData.Checked)
                {
                    // 全删。卸载器自身的 exe 在运行中删不掉，跳过它，末尾用 cmd 延迟自删。
                    FileCleaner.TryDeleteDirectory(_installDir, failures, selfPath);
                }
                else
                {
                    string[] keep = new string[] { "app\\data", "app\\.env" };
                    // 跳过正在运行的卸载器自身，否则会被误记为「删除失败」
                    List<string> f = FileCleaner.DeleteContentsExcept(_installDir, keep, selfPath);
                    for (int i = 0; i < f.Count; i++) { failures.Add(f[i]); }
                }

                // 卸载器自身的 exe / 配置残留（自删由末尾的 cmd 完成）
                RemoveSelfArtifacts(_installDir, selfPath, failures);
                CleanupUtil.RemoveEmptyProgramsFolder(notes);

                _lblStatus.Text = "卸载完成";
            }
            catch (Exception ex)
            {
                failures.Add("卸载过程中发生异常：" + ex.Message);
            }
            finally
            {
                Cursor = Cursors.Default;
            }

            // ⑤ 结果提示
            StringBuilder sb = new StringBuilder();
            sb.AppendLine("卸载完成。");
            if (keepData)
            {
                sb.AppendLine();
                sb.AppendLine("已保留你的数据与配置：");
                sb.AppendLine("  " + Path.Combine(_installDir, "app\\data"));
                sb.AppendLine("  " + Path.Combine(_installDir, "app\\.env"));
            }
            if (notes.Count > 0)
            {
                sb.AppendLine();
                sb.AppendLine("以下项目未能完全清理（可忽略，或手动处理）：");
                for (int i = 0; i < notes.Count; i++) { sb.AppendLine("  · " + notes[i]); }
            }
            if (failures.Count > 0)
            {
                sb.AppendLine();
                sb.AppendLine("以下文件未能删除，请手动清理：");
                int max = Math.Min(failures.Count, 20);
                for (int i = 0; i < max; i++) { sb.AppendLine("  · " + failures[i]); }
                if (failures.Count > max)
                {
                    sb.AppendLine("  · …等共 " + failures.Count.ToString(CultureInfo.InvariantCulture) + " 项");
                }
            }

            MessageBox.Show(this, sb.ToString(), "卸载 Sequoia-X",
                MessageBoxButtons.OK, failures.Count > 0 ? MessageBoxIcon.Warning : MessageBoxIcon.Information);

            // ⑥ 调度自身删除（卸载器 exe 正在运行，必须在进程退出后删）
            SelfDeleteScheduler.Schedule(_installDir, Application.ExecutablePath);
            Close();
        }

        /// <summary>删除安装目录里除「正在运行的卸载器自身」以外的残留。</summary>
        private static void RemoveSelfArtifacts(string installDir, string selfPath, List<string> failures)
        {
            try
            {
                string[] leftovers = new string[]
                {
                    Path.Combine(installDir, "install.json"),
                    Path.Combine(installDir, "Launcher.exe"),
                    Path.Combine(installDir, "Setup.exe"),
                    Path.Combine(installDir, "setup-stub.exe")
                };
                for (int i = 0; i < leftovers.Length; i++)
                {
                    try
                    {
                        if (File.Exists(leftovers[i])) { File.Delete(leftovers[i]); }
                    }
                    catch (Exception ex)
                    {
                        failures.Add(leftovers[i] + "（" + ex.Message + "）");
                    }
                }
            }
            catch { }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    //  6. 入口
    // ═══════════════════════════════════════════════════════════════════════
    internal static class Program
    {
        [STAThread]
        private static int Main(string[] argv)
        {
            // 命令行：
            //   /S、/silent、--silent   静默卸载：不显示窗口、不弹任何对话框
            //   /KeepData               保留 app\data 与 app\.env
            //   （注意：**默认就是保留**，与界面上的默认一致；不给 /KeepData 也不会删数据）
            //   --delete-data、/DeleteData  连数据与配置一起删除（危险，非默认）
            bool silent = false;
            bool deleteData = false;
            if (argv != null)
            {
                for (int i = 0; i < argv.Length; i++)
                {
                    string a = (argv[i] ?? "").Trim();
                    if (string.Equals(a, "--silent", StringComparison.OrdinalIgnoreCase)
                        || string.Equals(a, "/silent", StringComparison.OrdinalIgnoreCase)
                        || string.Equals(a, "/S", StringComparison.OrdinalIgnoreCase)
                        || string.Equals(a, "-s", StringComparison.OrdinalIgnoreCase))
                    {
                        silent = true;
                    }
                    if (string.Equals(a, "--delete-data", StringComparison.OrdinalIgnoreCase)
                        || string.Equals(a, "/DeleteData", StringComparison.OrdinalIgnoreCase))
                    {
                        deleteData = true;
                    }
                    // /KeepData 是默认行为，这里显式接受（保证脚本怎么写都不会误删数据）
                }
            }

            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            if (silent)
            {
                // 静默模式：绝对不弹任何 MessageBox，一切靠退出码 + %TEMP%\sequoia-uninstall.log
                int code = 1;
                string selfDir = null;
                try
                {
                    selfDir = Path.GetDirectoryName(Application.ExecutablePath);
                    code = ConsoleUninstall.Run(selfDir, deleteData);
                }
                catch (Exception ex)
                {
                    code = 1;
                    UninstallLog.WriteError(selfDir, "静默卸载过程中发生未处理的异常。", ex);
                }
                Environment.ExitCode = code;
                return code;
            }

            try
            {
                Application.Run(new UninstallForm());
            }
            catch (Exception ex)
            {
                MessageBox.Show("卸载程序发生未处理的错误：\r\n\r\n" + ex.ToString(),
                    "卸载 Sequoia-X", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 1;
            }
            return 0;
        }
    }

    /// <summary>
    /// 静默卸载（/S）流程，与 GUI 版共用全部清理逻辑。
    /// 返回退出码：0 = 成功，1 = 有文件删不掉 / 发生异常。
    /// </summary>
    internal static class ConsoleUninstall
    {
        public static int Run(string selfDir, bool deleteData)
        {
            string installDir = selfDir;
            try
            {
                string manifest = Path.Combine(selfDir, "install.json");
                if (File.Exists(manifest))
                {
                    string dir = MiniJson.GetString(File.ReadAllText(manifest, Encoding.UTF8), "install_dir");
                    if (!string.IsNullOrEmpty(dir) && Directory.Exists(dir)) { installDir = Path.GetFullPath(dir); }
                }
            }
            catch { }

            List<string> failures = new List<string>();
            List<string> notes = new List<string>();
            int exitCode = 0;

            string self = null;
            try { self = Path.GetFullPath(System.Reflection.Assembly.GetEntryAssembly().Location); }
            catch { }

            try
            {
                // ① 停服务
                ServiceStopper.StopAll(installDir);

                // ② 快捷方式 + 注册表
                CleanupUtil.RemoveShortcuts(notes);
                CleanupUtil.RemoveRegistry(notes);

                // ③ 删文件
                if (deleteData)
                {
                    FileCleaner.TryDeleteDirectory(installDir, failures, self);
                }
                else
                {
                    // 默认（以及显式 /KeepData）：保留 app\data 与 app\.env
                    List<string> f = FileCleaner.DeleteContentsExcept(installDir,
                        new string[] { "app\\data", "app\\.env" }, self);
                    for (int i = 0; i < f.Count; i++) { failures.Add(f[i]); }
                }

                // ④ 残留：install.json 与几个 exe（自身除外）
                try
                {
                    string[] leftovers = new string[]
                    {
                        Path.Combine(installDir, "install.json"),
                        Path.Combine(installDir, "Launcher.exe"),
                        Path.Combine(installDir, "Setup.exe"),
                        Path.Combine(installDir, "setup-stub.exe")
                    };
                    for (int i = 0; i < leftovers.Length; i++)
                    {
                        try { if (File.Exists(leftovers[i])) { File.Delete(leftovers[i]); } } catch { }
                    }
                }
                catch { }

                CleanupUtil.RemoveEmptyProgramsFolder(notes);
            }
            catch (Exception ex)
            {
                exitCode = 1;
                UninstallLog.WriteError(installDir, "静默卸载过程中发生异常。", ex);
            }

            // ⑤ 结果日志（静默模式没有界面，只能靠日志）
            UninstallLog.WriteResult(installDir, !deleteData, failures, notes);
            if (failures.Count > 0) { exitCode = 1; }

            // ⑥ 调度自身删除（连同变空的安装目录）
            SelfDeleteScheduler.Schedule(installDir, self);

            return exitCode;
        }
    }
}