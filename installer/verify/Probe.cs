// ═══════════════════════════════════════════════════════════════════════════
//  verify-payload.ps1 的探针程序（验证用，不参与发布）
//  ---------------------------------------------------------------------------
//  目的：在不运行 GUI 的前提下，真实地驱动 Setup.cs / Launcher.cs 里的核心逻辑，
//        以验证「追加式负载」的定位、前缀过滤与解压结果。
//
//  编译方式（由 verify-payload.ps1 自动完成）：
//      csc /target:exe /main:Probe.Main /out:SetupProbe.exe \
//          AssemblyInfo.g.cs Setup.cs verify\Probe.cs
//      csc /target:exe /main:Probe.Main /out:OtherProbe.exe \
//          AssemblyInfo.g.cs Launcher.cs Uninstaller.cs verify\Probe.cs
//
//  通过反射调用非公开的 internal 类型，避免为了测试而放宽生产代码的可见性。
//
//  子命令：
//      setup  <安装包exe> [解压目标目录]
//             从追加了 zip 的 exe 里定位 zip 起始偏移，打印条目列表并（可选）解压。
//      launcher <netstat样例文件> <端口>
//              驱动 Launcher.ServiceControl.ParseNetstat 解析 netstat 输出。
// ═══════════════════════════════════════════════════════════════════════════

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Text;

internal static class Probe
{
    // ───────────────────────────────────────────────────────────────────────
    //  JSON 输出小工具
    // ───────────────────────────────────────────────────────────────────────
    private static string J(string s)
    {
        if (s == null) { return "null"; }
        StringBuilder sb = new StringBuilder();
        sb.Append('"');
        for (int i = 0; i < s.Length; i++)
        {
            char c = s[i];
            if (c == '"' || c == '\\') { sb.Append('\\').Append(c); }
            else if (c == '\n') { sb.Append("\\n"); }
            else if (c == '\r') { sb.Append("\\r"); }
            else if (c == '\t') { sb.Append("\\t"); }
            else if (c < ' ') { sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture)); }
            else { sb.Append(c); }
        }
        sb.Append('"');
        return sb.ToString();
    }

    // ───────────────────────────────────────────────────────────────────────
    //  反射调用 Setup.cs 里的内部逻辑
    // ───────────────────────────────────────────────────────────────────────
    private static Type FindType(string simpleName)
    {
        Assembly asm = typeof(Probe).Assembly;
        Type[] all = asm.GetTypes();
        for (int i = 0; i < all.Length; i++)
        {
            if (string.Equals(all[i].Name, simpleName, StringComparison.Ordinal)) { return all[i]; }
        }
        throw new InvalidOperationException("在本程序集里找不到类型：" + simpleName);
    }

    private static object StaticCall(string typeName, string methodName, object[] args)
    {
        Type t = FindType(typeName);
        MethodInfo mi = t.GetMethod(methodName,
            BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
        if (mi == null) { throw new InvalidOperationException("找不到方法：" + typeName + "." + methodName); }

        // 注意：MethodInfo.Invoke 要求参数数组长度与形参个数严格一致（out 参数用 null 占位），
        // 少一个就会抛“目标数组的长度不足”。这里按目标方法签名显式扩容。
        ParameterInfo[] pars = mi.GetParameters();
        object[] callArgs = new object[pars.Length];
        for (int i = 0; i < pars.Length; i++)
        {
            if (args != null && i < args.Length) { callArgs[i] = args[i]; }
            else { callArgs[i] = null; }
        }

        try
        {

            return mi.Invoke(null, callArgs);
        }
        catch (TargetInvocationException tie)
        {
            throw tie.InnerException;
        }
    }

    /// <summary>调用 Setup.cs 的 PayloadLocator.FindZipOffset。</summary>
    private static long FindZipOffset(string exePath)
    {
        // 注意：FindZipOffset 只有一个形参（exePath），返回值即 zip 起始偏移。
        // 这里用强类型委托调用（而不是 MethodInfo.Invoke），既更快，
        // 也避免反射参数装箱在某些运行库版本上出现的类型绑定问题。
        Type t = FindType("PayloadLocator");
        MethodInfo mi = t.GetMethod("FindZipOffset",
            BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
        if (mi == null) { throw new InvalidOperationException("找不到 PayloadLocator.FindZipOffset"); }
        Func<string, long> fn = (Func<string, long>)Delegate.CreateDelegate(typeof(Func<string, long>), mi);
        return fn(exePath);
    }

    /// <summary>调用 Setup.cs 的 PayloadLocator.OpenZipAt。</summary>
    private static ZipArchive OpenZipAt(string exePath, long offset)
    {
        object r = StaticCall("PayloadLocator", "OpenZipAt", new object[] { exePath, offset });
        return (ZipArchive)r;
    }

    /// <summary>调用 Setup.cs 的 EntryFilter.Normalize。</summary>
    private static string Normalize(string entryName)
    {
        object r = StaticCall("EntryFilter", "Normalize", new object[] { entryName });
        return (string)r;
    }

    /// <summary>调用 Setup.cs 的 EntryFilter.ShouldExtract。</summary>
    private static bool ShouldExtract(string entryName, bool isDir, bool installData)
    {
        object r = StaticCall("EntryFilter", "ShouldExtract", new object[] { entryName, isDir, installData });
        return (bool)r;
    }

    /// <summary>调用 Setup.cs 的 EntryFilter.IsBigDataEntry。</summary>
    private static bool IsBigDataEntry(string entryName)
    {
        object r = StaticCall("EntryFilter", "IsBigDataEntry", new object[] { entryName });
        return (bool)r;
    }

    // ───────────────────────────────────────────────────────────────────────
    //  子命令：setup
    // ───────────────────────────────────────────────────────────────────────
    private static int CmdSetup(string exePath, string extractDir)
    {
        StringBuilder outJson = new StringBuilder();
        outJson.Append('{');

        long offset = FindZipOffset(exePath);
        outJson.Append("\"exe\":").Append(J(exePath));
        outJson.Append(",\"exe_size\":").Append(new FileInfo(exePath).Length.ToString(CultureInfo.InvariantCulture));
        outJson.Append(",\"offset\":").Append(offset.ToString(CultureInfo.InvariantCulture));
        outJson.Append(",\"found\":").Append(offset >= 0 ? "true" : "false");

        if (offset < 0)
        {
            outJson.Append(",\"entries\":[],\"big_data\":false,\"extracted\":[]}");
            Console.WriteLine(outJson.ToString());
            return 0;
        }

        // 读 payload.json（决定 has_data）
        string metaJson = null;
        List<string> names = new List<string>();
        bool hasBig = false;
        using (ZipArchive zip = OpenZipAt(exePath, offset))
        {
            for (int i = 0; i < zip.Entries.Count; i++)
            {
                ZipArchiveEntry e = zip.Entries[i];
                names.Add(e.FullName);
                if (IsBigDataEntry(e.FullName)) { hasBig = true; }
                string norm = Normalize(e.FullName);
                if (norm != null && string.Equals(norm, "payload.json", StringComparison.OrdinalIgnoreCase))
                {
                    using (Stream s = e.Open())
                    using (StreamReader r = new StreamReader(s, new UTF8Encoding(false), true))
                    {
                        metaJson = r.ReadToEnd();
                    }
                }
            }

            outJson.Append(",\"entries\":[");
            for (int i = 0; i < names.Count; i++)
            {
                if (i > 0) { outJson.Append(','); }
                outJson.Append(J(names[i]));
            }
            outJson.Append(']');
            outJson.Append(",\"big_data\":").Append(hasBig ? "true" : "false");
            outJson.Append(",\"meta_raw\":").Append(J(metaJson));

            // 决定 installData：payload.json 的 has_data 为 true 且 zip 里确实有该条目
            bool installData = hasBig
                && metaJson != null
                && metaJson.IndexOf("\"has_data\": true", StringComparison.OrdinalIgnoreCase) >= 0;

            // 前缀过滤：列出应当解压的条目，与跳过的条目
            List<string> willExtract = new List<string>();
            List<string> skipped = new List<string>();
            for (int i = 0; i < zip.Entries.Count; i++)
            {
                ZipArchiveEntry e = zip.Entries[i];
                bool isDir = e.FullName.EndsWith("/", StringComparison.Ordinal) || e.FullName.EndsWith("\\", StringComparison.Ordinal);
                if (ShouldExtract(e.FullName, isDir, installData)) { willExtract.Add(e.FullName); }
                else { skipped.Add(e.FullName); }
            }

            outJson.Append(",\"install_data\":").Append(installData ? "true" : "false");
            outJson.Append(",\"will_extract\":[");
            for (int i = 0; i < willExtract.Count; i++)
            {
                if (i > 0) { outJson.Append(','); }
                outJson.Append(J(willExtract[i]));
            }
            outJson.Append(']');
            outJson.Append(",\"skipped\":[");
            for (int i = 0; i < skipped.Count; i++)
            {
                if (i > 0) { outJson.Append(','); }
                outJson.Append(J(skipped[i]));
            }
            outJson.Append(']');

            // 复现安装器的解压循环（与 Setup.cs ExtractAll 的过滤条件一致）
            List<string> extracted = new List<string>();
            List<long> extractedSizes = new List<long>();
            if (!string.IsNullOrEmpty(extractDir))
            {
                if (Directory.Exists(extractDir)) { Directory.Delete(extractDir, true); }
                Directory.CreateDirectory(extractDir);

                for (int i = 0; i < zip.Entries.Count; i++)
                {
                    ZipArchiveEntry e = zip.Entries[i];
                    bool isDir = e.FullName.EndsWith("/", StringComparison.Ordinal) || e.FullName.EndsWith("\\", StringComparison.Ordinal);
                    if (!ShouldExtract(e.FullName, isDir, installData)) { continue; }
                    string rel = Normalize(e.FullName);
                    if (rel == null) { continue; }
                    string target = Path.Combine(extractDir, rel);
                    string dir = Path.GetDirectoryName(target);
                    if (!string.IsNullOrEmpty(dir)) { Directory.CreateDirectory(dir); }
                    using (Stream src = e.Open())
                    using (FileStream dst = new FileStream(target, FileMode.Create, FileAccess.Write, FileShare.None))
                    {
                        byte[] buf = new byte[1024 * 128];
                        int n;
                        long total = 0;
                        while ((n = src.Read(buf, 0, buf.Length)) > 0)
                        {
                            dst.Write(buf, 0, n);
                            total += n;
                        }
                        extracted.Add(rel);
                        extractedSizes.Add(total);
                    }
                }
            }

            outJson.Append(",\"extracted\":[");
            for (int i = 0; i < extracted.Count; i++)
            {
                if (i > 0) { outJson.Append(','); }
                outJson.Append("{\"rel\":").Append(J(extracted[i]))
                       .Append(",\"size\":").Append(extractedSizes[i].ToString(CultureInfo.InvariantCulture)).Append('}');
            }
            outJson.Append(']');
        }

        outJson.Append('}');
        Console.WriteLine(outJson.ToString());
        return 0;
    }

    // ───────────────────────────────────────────────────────────────────────
    //  子命令：stub —— 确认没追加负载的 stub 返回 -1（不崩）
    // ───────────────────────────────────────────────────────────────────────
    private static int CmdStub(string exePath)
    {
        long offset = FindZipOffset(exePath);
        Console.WriteLine("{\"exe\":" + J(exePath) + ",\"offset\":" + offset.ToString(CultureInfo.InvariantCulture) + "}");
        return 0;
    }

    // ───────────────────────────────────────────────────────────────────────
    //  子命令：launcher —— 验证 netstat 解析
    // ───────────────────────────────────────────────────────────────────────
    private static int CmdLauncher(string netstatFile, int port)
    {
        string text = File.ReadAllText(netstatFile, Encoding.UTF8);
        object r = StaticCall("ServiceControl", "ParseNetstat", new object[] { text, port });
        int pid = Convert.ToInt32(r, CultureInfo.InvariantCulture);
        Console.WriteLine("{\"port\":" + port.ToString(CultureInfo.InvariantCulture)
                          + ",\"pid\":" + pid.ToString(CultureInfo.InvariantCulture) + "}");
        return 0;
    }

    // ───────────────────────────────────────────────────────────────────────
    //  子命令：filter —— 按前缀规则判定「哪些条目会 / 不会 被解压」
    //      filter <exe>              以 payload.json 的 has_data 决定 installData
    //      filter <exe> true|false   强制指定 installData（验证取消勾选路径）
    // ───────────────────────────────────────────────────────────────────────
    private static int CmdFilter(string exePath, string installDataArg)
    {
        long offset = FindZipOffset(exePath);
        StringBuilder sb = new StringBuilder();
        sb.Append('{');
        sb.Append("\"offset\":").Append(offset.ToString(CultureInfo.InvariantCulture));

        bool installData;
        List<string> willExtract = new List<string>();
        List<string> skipped = new List<string>();

        using (ZipArchive zip = OpenZipAt(exePath, offset))
        {
            if (installDataArg == null)
            {
                bool hasBig = false;
                bool metaSaysData = false;
                for (int i = 0; i < zip.Entries.Count; i++)
                {
                    ZipArchiveEntry e = zip.Entries[i];
                    if (IsBigDataEntry(e.FullName)) { hasBig = true; }
                    string norm = Normalize(e.FullName);
                    if (norm != null && string.Equals(norm, "payload.json", StringComparison.OrdinalIgnoreCase))
                    {
                        using (Stream s = e.Open())
                        using (StreamReader r = new StreamReader(s, new UTF8Encoding(false), true))
                        {
                            string meta = r.ReadToEnd();
                            metaSaysData = meta.IndexOf("\"has_data\": true", StringComparison.OrdinalIgnoreCase) >= 0;
                        }
                    }
                }
                installData = hasBig && metaSaysData;
            }
            else
            {
                installData = string.Equals(installDataArg, "true", StringComparison.OrdinalIgnoreCase);
            }

            for (int i = 0; i < zip.Entries.Count; i++)
            {
                ZipArchiveEntry e = zip.Entries[i];
                bool isDir = e.FullName.EndsWith("/", StringComparison.Ordinal) || e.FullName.EndsWith("\\", StringComparison.Ordinal);
                if (ShouldExtract(e.FullName, isDir, installData)) { willExtract.Add(e.FullName); }
                else { skipped.Add(e.FullName); }
            }
        }

        sb.Append(",\"install_data\":").Append(installData ? "true" : "false");
        sb.Append(",\"will_extract\":[");
        for (int i = 0; i < willExtract.Count; i++)
        {
            if (i > 0) { sb.Append(','); }
            sb.Append(J(willExtract[i]));
        }
        sb.Append("],\"skipped\":[");
        for (int i = 0; i < skipped.Count; i++)
        {
            if (i > 0) { sb.Append(','); }
            sb.Append(J(skipped[i]));
        }
        sb.Append("]}");
        Console.WriteLine(sb.ToString());
        return 0;
    }

    // ───────────────────────────────────────────────────────────────────────
    //  子命令：setupform —— 驱动「真实安装流程」（不显示窗口）
    //      setupform <安装包exe> <安装目录> [true|false]
    //    直接实例化 SetupForm（不 Show），通过反射调用它的
    //    ExtractAll / WriteInstallJson / WriteAutoRun / WriteUninstallEntry，
    //    把安装器安装过程中真正执行的动作完整跑一遍。
    //    这是对 GUI 安装器最接近「真的点了一次安装」的验证方式。
    // ───────────────────────────────────────────────────────────────────────
    private static void SetPrivateField(object obj, string name, object value)
    {
        Type t = obj.GetType();
        while (t != null)
        {
            FieldInfo fi = t.GetField(name, BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public);
            if (fi != null) { fi.SetValue(obj, value); return; }
            t = t.BaseType;
        }
        throw new InvalidOperationException("找不到字段：" + name);
    }

    private static object GetPrivateField(object obj, string name)
    {
        Type t = obj.GetType();
        while (t != null)
        {
            FieldInfo fi = t.GetField(name, BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public);
            if (fi != null) { return fi.GetValue(obj); }
            t = t.BaseType;
        }
        return null;
    }

    private static object CallInstance(object obj, string methodName, object[] args)
    {
        Type t = obj.GetType();
        MethodInfo mi = null;
        while (t != null && mi == null)
        {
            mi = t.GetMethod(methodName, BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public);
            t = t.BaseType;
        }
        if (mi == null) { throw new InvalidOperationException("找不到实例方法：" + methodName); }
        ParameterInfo[] pars = mi.GetParameters();
        object[] callArgs = new object[pars.Length];
        for (int i = 0; i < pars.Length; i++)
        {
            if (args != null && i < args.Length) { callArgs[i] = args[i]; }
        }
        try
        {
            return mi.Invoke(obj, callArgs);
        }
        catch (TargetInvocationException tie)
        {
            throw tie.InnerException;
        }
    }

    private static void SetCheck(object form, string fieldName, bool value)
    {
        object chk = GetPrivateField(form, fieldName);
        if (chk == null) { throw new InvalidOperationException("找不到复选框字段：" + fieldName); }
        chk.GetType().GetProperty("Checked").SetValue(chk, value, null);
    }

    private static int CmdSetupForm(string exePath, string installDir, string installDataArg)
    {
        Type formType = FindType("SetupForm");
        object form;
        try
        {
            form = Activator.CreateInstance(formType, true);
        }
        catch (TargetInvocationException tie)
        {
            throw tie.InnerException;
        }

        // 让 SetupForm 把「自己」当成目标安装包，而不是探针 exe
        SetPrivateField(form, "_exePath", exePath);
        SetPrivateField(form, "_zipOffset", (long)-1);
        SetPrivateField(form, "_zipHasBigData", false);
        SetPrivateField(form, "_warnings", new List<string>());

        MethodInfo find = FindType("PayloadLocator").GetMethod("FindZipOffset",
            BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
        Func<string, long> locator = (Func<string, long>)Delegate.CreateDelegate(typeof(Func<string, long>), find);
        long offset = locator(exePath);
        SetPrivateField(form, "_zipOffset", offset);
        if (offset < 0)
        {
            Console.WriteLine("{\"ok\":false,\"error\":\"无法定位负载\"}");
            return 1;
        }

        // 重新读 payload.json，填 _meta / _zipHasBigData
        object meta = null;
        bool hasBig = false;
        MethodInfo openZip = FindType("PayloadLocator").GetMethod("OpenZipAt",
            BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
        using (ZipArchive zip = (ZipArchive)openZip.Invoke(null, new object[] { exePath, offset }))
        {
            for (int i = 0; i < zip.Entries.Count; i++)
            {
                ZipArchiveEntry e = zip.Entries[i];
                if (IsBigDataEntry(e.FullName)) { hasBig = true; }
                string norm = Normalize(e.FullName);
                if (norm != null && string.Equals(norm, "payload.json", StringComparison.OrdinalIgnoreCase))
                {
                    string json;
                    using (Stream s = e.Open())
                    using (StreamReader r = new StreamReader(s, new UTF8Encoding(false), true))
                    {
                        json = r.ReadToEnd();
                    }
                    MethodInfo pm = formType.GetMethod("ParseMeta", BindingFlags.NonPublic | BindingFlags.Static);
                    if (pm != null) { meta = pm.Invoke(null, new object[] { json }); }
                }
            }
        }
        if (meta != null) { SetPrivateField(form, "_meta", meta); }
        SetPrivateField(form, "_zipHasBigData", hasBig);

        // 复选框：探针不污染真实桌面 / 开始菜单 / 自启注册表
        SetCheck(form, "_chkDesktop", false);
        SetCheck(form, "_chkStartMenu", false);
        SetCheck(form, "_chkAutoRun", false);
        bool installData = string.Equals(installDataArg, "true", StringComparison.OrdinalIgnoreCase);
        SetCheck(form, "_chkData", installData);

        // ── 真实安装动作 ──
        object written = CallInstance(form, "ExtractAll", new object[] { installDir, installData });
        long writtenBytes = Convert.ToInt64(written, CultureInfo.InvariantCulture);
        CallInstance(form, "WriteInstallJson", new object[] { installDir });

        // 单独验证 .lnk 真能被创建（写到安装目录下的 lnk-test，不碰真实桌面）
        string lnkDir = Path.Combine(installDir, "lnk-test");
        Directory.CreateDirectory(lnkDir);
        string lnkPath = Path.Combine(lnkDir, "Sequoia-X 工作台.lnk");
        MethodInfo create = FindType("ShortcutUtil").GetMethod("Create",
            BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
        bool lnkOk = (bool)create.Invoke(null, new object[]
        {
            lnkPath,
            Path.Combine(installDir, "Launcher.exe"),
            "",
            installDir,
            Path.Combine(installDir, "Launcher.exe"),
            "Sequoia-X 量化选股工作台"
        });

        CallInstance(form, "WriteAutoRun", new object[] { installDir });
        CallInstance(form, "WriteUninstallEntry", new object[] { installDir, writtenBytes });

        // ── 汇总 ──
        List<string> tree = new List<string>();
        if (Directory.Exists(installDir))
        {
            string[] files = Directory.GetFiles(installDir, "*", SearchOption.AllDirectories);
            for (int i = 0; i < files.Length; i++)
            {
                tree.Add(files[i].Substring(installDir.Length).TrimStart('\\') + "|" + new FileInfo(files[i]).Length);
            }
        }
        tree.Sort(StringComparer.OrdinalIgnoreCase);

        string installJsonPath = Path.Combine(installDir, "install.json");
        string installJson = File.Exists(installJsonPath) ? File.ReadAllText(installJsonPath, Encoding.UTF8) : null;

        StringBuilder sb = new StringBuilder();
        sb.Append('{');
        sb.Append("\"ok\":true");
        sb.Append(",\"offset\":").Append(offset.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"install_dir\":").Append(J(installDir));
        sb.Append(",\"install_data\":").Append(installData ? "true" : "false");
        sb.Append(",\"written_bytes\":").Append(writtenBytes.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"lnk_created\":").Append((lnkOk && File.Exists(lnkPath)) ? "true" : "false");
        sb.Append(",\"lnk_size\":").Append((lnkOk && File.Exists(lnkPath))
            ? new FileInfo(lnkPath).Length.ToString(CultureInfo.InvariantCulture) : "0");
        sb.Append(",\"install_json\":").Append(J(installJson));
        sb.Append(",\"warnings\":[");
        List<string> warns = (List<string>)GetPrivateField(form, "_warnings");
        if (warns != null)
        {
            for (int i = 0; i < warns.Count; i++)
            {
                if (i > 0) { sb.Append(','); }
                sb.Append(J(warns[i]));
            }
        }
        sb.Append(']');
        sb.Append(",\"files\":[");
        for (int i = 0; i < tree.Count; i++)
        {
            if (i > 0) { sb.Append(','); }
            sb.Append(J(tree[i]));
        }
        sb.Append("]}");
        Console.WriteLine(sb.ToString());

        IDisposable d = form as IDisposable;
        if (d != null) { d.Dispose(); }
        return 0;
    }

    // ───────────────────────────────────────────────────────────────────────
    //  入口
    // ───────────────────────────────────────────────────────────────────────
    public static int Main(string[] argv)
    {
        try
        {
            if (argv == null || argv.Length == 0)
            {
                Console.Error.WriteLine("用法: Probe setup <exe> [extractDir] | Probe setupform <exe> <installDir> [true|false] | Probe stub <exe> | Probe filter <exe> [true|false] | Probe launcher <netstatFile> <port>");
                return 2;
            }
            string cmd = argv[0].ToLowerInvariant();
            if (cmd == "setup")
            {
                return CmdSetup(argv[1], argv.Length > 2 ? argv[2] : null);
            }
            if (cmd == "setupform")
            {
                return CmdSetupForm(argv[1], argv[2], argv.Length > 3 ? argv[3] : "false");
            }
            if (cmd == "stub")
            {
                return CmdStub(argv[1]);
            }
            if (cmd == "filter")
            {
                return CmdFilter(argv[1], argv.Length > 2 ? argv[2] : null);
            }
            if (cmd == "launcher")
            {
                return CmdLauncher(argv[1], int.Parse(argv[2], CultureInfo.InvariantCulture));
            }
            Console.Error.WriteLine("未知子命令：" + argv[0]);
            return 2;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("PROBE-ERROR: " + ex.ToString());
            return 1;
        }
    }
}
