// ═══════════════════════════════════════════════════════════════════════════
//  Sequoia-X 安装程序（单文件自解压安装器）
//  ---------------------------------------------------------------------------
//  本文件是一套纯 .NET Framework 4.x / WinForms 的安装器，使用系统自带的
//  csc.exe 编译（C# 5 语法），不依赖 Inno Setup / NSIS / 7-Zip / 网络。
//
//  负载（payload.zip）的原始字节直接追加在本 exe 文件末尾：
//
//      [ Setup.exe 的 PE 镜像 ][ payload.zip 的原始字节 ]
//
//  因此运行时会从文件尾部向前搜索 End of Central Directory 签名 PK\x05\x06
//  来确定 zip 起始偏移，再用 ZipArchive 按前缀过滤解压。
//
//  编译：见同目录 build.ps1
//  语言约束：csc.exe (C# 5) —— 不使用字符串插值、?. 运算符、async/await、
//            表达式体成员、out var、异常筛选器、自动属性初始化器。
// ═══════════════════════════════════════════════════════════════════════════

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.IO.Compression;
using System.IO.MemoryMappedFiles;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

namespace SequoiaX.Installer
{
    // ═══════════════════════════════════════════════════════════════════════
    //  1. Win32 快捷方式接口声明（IShellLinkW + IPersistFile）
    //     说明：刻意不使用 COM 的 WScript.Shell（dynamic），
    //           这样 csc 不需要额外的 interop 程序集，也能保持强类型。
    // ═══════════════════════════════════════════════════════════════════════

    [ComImport]
    [Guid("00021401-0000-0000-C000-000000000046")]
    internal class ShellLinkCoClass
    {
    }

    [ComImport]
    [Guid("000214F9-0000-0000-C000-000000000046")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IShellLinkW
    {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszFile,
                     int cchMaxPath, IntPtr pfd, uint fFlags);
        void GetIDList(out IntPtr ppidl);
        void SetIDList(IntPtr pidl);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszName, int cchMaxName);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string pszName);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszDir, int cchMaxPath);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string pszDir);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszArgs, int cchMaxPath);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string pszArgs);
        void GetHotkey(out short pwHotkey);
        void SetHotkey(short wHotkey);
        void GetShowCmd(out int piShowCmd);
        void SetShowCmd(int iShowCmd);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszIconPath,
                             int cchIconPath, out int piIcon);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string pszIconPath, int iIcon);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string pszPathRel, uint dwReserved);
        void Resolve(IntPtr hwnd, uint fFlags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string pszFile);
    }

    [ComImport]
    [Guid("0000010b-0000-0000-C000-000000000046")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IPersistFile
    {
        void GetClassID(out Guid pClassID);
        void IsDirty();
        void Load([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, uint dwMode);
        void Save([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, bool fRemember);
        void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string pszFileName);
        void GetCurFile([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder ppszFileName);
    }

    // ═══════════════════════════════════════════════════════════════════════
    //  2. 负载元数据（payload.json）
    // ═══════════════════════════════════════════════════════════════════════

    /// <summary>app/data/sequoia_v2.db 这个特殊大条目的相对路径。</summary>
    internal static class PayloadLayout
    {
        public const string MetaEntry = "payload.json";
        public const string BigDataEntry = "app/data/sequoia_v2.db";
    }

    /// <summary>payload.json 的解析结果。解析失败时全部取保守默认值。</summary>
    internal sealed class PayloadMeta
    {
        public string App = "Sequoia-X";
        public string Version = "2.0.0";
        public string BuiltAt = "";
        public int Port = 8899;
        public bool HasData = false;
        public double DataSizeMb = 0;
        public double RuntimeSizeMb = 0;
        public double AppSizeMb = 0;

        /// <summary>「版本 2.0.0 · 构建于 2026-10-12 20:30」</summary>
        public string VersionLine()
        {
            string built = BuiltAt;
            // 只保留到分钟：2026-10-12 20:30:00 → 2026-10-12 20:30
            if (built.Length >= 16) { built = built.Substring(0, 16); }
            if (built.Length == 0) { return "版本 " + Version; }
            return "版本 " + Version + " · 构建于 " + built;
        }

        /// <summary>「运行时 210 MB · 应用 1 MB · 行情数据 493.9 MB」</summary>
        public string SizeLine()
        {
            string s = "运行时 " + Mb(RuntimeSizeMb) + " · 应用 " + Mb(AppSizeMb);
            if (HasData) { s = s + " · 行情数据 " + Mb(DataSizeMb); }
            return s;
        }

        private static string Mb(double v)
        {
            return v.ToString("0.#", CultureInfo.InvariantCulture) + " MB";
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    //  3. 极简 JSON 取值器（避免引入任何第三方依赖）
    //     只用于读 payload.json 这种扁平结构，够用且不会误判。
    // ═══════════════════════════════════════════════════════════════════════

    internal static class MiniJson
    {
        /// <summary>取字符串字段（例如 "version": "2.0.0"）。找不到返回 null。</summary>
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

        /// <summary>取数字字段（例如 "port": 8899）。找不到或格式不对返回 NaN。</summary>
        public static double GetNumber(string json, string key)
        {
            if (json == null) { return double.NaN; }
            int i = json.IndexOf("\"" + key + "\"", StringComparison.Ordinal);
            if (i < 0) { return double.NaN; }
            int colon = json.IndexOf(':', i + key.Length + 2);
            if (colon < 0) { return double.NaN; }
            int p = colon + 1;
            while (p < json.Length && char.IsWhiteSpace(json[p])) { p++; }
            int start = p;
            while (p < json.Length && (char.IsDigit(json[p]) || json[p] == '.' || json[p] == '-' || json[p] == '+' || json[p] == 'e' || json[p] == 'E'))
            {
                p++;
            }
            if (p == start) { return double.NaN; }
            double v;
            if (double.TryParse(json.Substring(start, p - start), NumberStyles.Float, CultureInfo.InvariantCulture, out v))
            {
                return v;
            }
            return double.NaN;
        }

        /// <summary>取布尔字段（例如 "has_data": true）。字段缺失返回 null（与 false 区分开）。</summary>
        public static bool? GetBool(string json, string key)
        {
            if (json == null) { return null; }
            int i = json.IndexOf("\"" + key + "\"", StringComparison.Ordinal);
            if (i < 0) { return null; }
            int colon = json.IndexOf(':', i + key.Length + 2);
            if (colon < 0) { return null; }
            int p = colon + 1;
            while (p < json.Length && char.IsWhiteSpace(json[p])) { p++; }
            if (p + 4 <= json.Length && string.Compare(json, p, "true", 0, 4, StringComparison.OrdinalIgnoreCase) == 0) { return true; }
            if (p + 5 <= json.Length && string.Compare(json, p, "false", 0, 5, StringComparison.OrdinalIgnoreCase) == 0) { return false; }
            return null;
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    //  4. 负载定位：从 exe 末尾找出追加的 zip 起始偏移
    // ═══════════════════════════════════════════════════════════════════════

    internal static class PayloadLocator
    {
        // EOCD 固定 22 字节，注释最长 65535 字节
        private const int EocdMinSize = 22;
        private const int MaxComment = 0xFFFF;
        private const int SearchWindow = MaxComment + EocdMinSize;   // 65557
        private const int CdEntryHeaderSize = 46;
        // 超过这个大小的负载改用内存映射（零拷贝），避免一次性占用过多内存
        private const long MaxInMemoryPayload = 768L * 1024 * 1024;

        /// <summary>
        /// 在当前 exe 里定位追加的 zip，返回 zip 起始偏移；找不到返回 -1。
        ///
        /// 算法（不依赖任何"zip 一定从某处开始"的假设，逐步自洽校验）：
        ///   1) 取文件末尾最后 65557 字节，从后往前找 EOCD 签名 "PK\x05\x06"；
        ///   2) 校验 EOCD 的注释长度：EOCD偏移 + 22 + 注释长度 == 文件长度；
        ///   3) 校验单卷（diskNo == cdDisk）且非 Zip64；
        ///   4) 推出 zip 起始偏移 prefix = 文件长度 - 中央目录大小 - 22 - 注释长度；
        ///      并确认中央目录结束位置正好等于 EOCD 起始位置；
        ///   5) 读出中央目录首条目（必须看到 "PK\x01\x02"），用其中的
        ///      「本地文件头偏移」得到 zip 的第一个字节位置，并确认该处是 "PK\x03\x04"。
        ///
        /// 第 5 步是排除误命中的关键：安装器 exe 自身的 PE 结构里也可能偶然
        /// 出现 PK\x05\x06 字节序列。
        /// </summary>
        public static long FindZipOffset(string exePath)
        {
            using (FileStream fs = new FileStream(exePath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            {
                long fileLen = fs.Length;
                if (fileLen < EocdMinSize) { return -1; }

                long tailStart = Math.Max(0, fileLen - SearchWindow);
                int tailLen = (int)(fileLen - tailStart);
                byte[] tail = new byte[tailLen];
                fs.Position = tailStart;
                if (ReadFully(fs, tail, 0, tailLen) != tailLen) { return -1; }

                // ① 从后往前找 EOCD 签名。起点保证 i + 22 <= tailLen，循环体内
                //    读 i+3 / i+18 / i+20 都不会越界。
                for (int i = tailLen - EocdMinSize; i >= 0; i--)
                {
                    if (tail[i] != 0x50 || tail[i + 1] != 0x4B
                        || tail[i + 2] != 0x05 || tail[i + 3] != 0x06)
                    {
                        continue;
                    }

                    long eocdAbs = tailStart + i;
                    ushort commentLen = BitConverter.ToUInt16(tail, i + 20);

                    // ② EOCD 必须恰好结束在文件末尾
                    if (eocdAbs + EocdMinSize + commentLen != fileLen) { continue; }

                    ushort diskNo = BitConverter.ToUInt16(tail, i + 4);
                    ushort cdDisk = BitConverter.ToUInt16(tail, i + 6);
                    uint cdSize = BitConverter.ToUInt32(tail, i + 12);
                    uint cdOffset = BitConverter.ToUInt32(tail, i + 16);

                    // ③ 单卷、非 Zip64
                    if (diskNo != cdDisk) { continue; }
                    if (cdSize == 0xFFFFFFFF || cdOffset == 0xFFFFFFFF) { continue; }

                    // ④ 反推 zip 起始偏移。zip 的尾部布局是：
                    //      [本地文件头与数据] [中央目录 cdSize] [EOCD 22] [注释]
                    //    设 cdStart 为中央目录在文件中的绝对起点：
                    //      cdStart   = eocdAbs - cdSize      （中央目录紧邻 EOCD 之前）
                    //      zipOffset = cdStart - cdOffset    （cdOffset 是 CD 相对 zip 起点的偏移）
                    //    这两步的顺序不能颠倒，也不能合并成"文件长度 - cdSize"
                    //    （那样会漏掉 EOCD 自身 22 字节，是最容易搞错的一步）。
                    if (cdSize > (ulong)eocdAbs) { continue; }
                    long cdStart = eocdAbs - (long)cdSize;
                    long zipOffset = cdStart - (long)cdOffset;
                    if (zipOffset < 0 || zipOffset > cdStart) { continue; }

                    // ⑤ 中央目录首条目：必须能看到 CD 签名，并取到本地头偏移
                    long cdPos = zipOffset + cdOffset;
                    if (cdSize < CdEntryHeaderSize) { continue; }
                    if (cdPos < 0 || cdPos + CdEntryHeaderSize > fileLen) { continue; }

                    byte[] head = new byte[CdEntryHeaderSize];
                    fs.Position = cdPos;
                    if (ReadFully(fs, head, 0, CdEntryHeaderSize) != CdEntryHeaderSize) { continue; }
                    if (head[0] != 0x50 || head[1] != 0x4B
                        || head[2] != 0x01 || head[3] != 0x02)
                    {
                        continue;   // 不是中央目录，说明是误命中的 EOCD
                    }

                    uint localHeaderOffset = BitConverter.ToUInt32(head, 42);
                    if (localHeaderOffset == 0xFFFFFFFF) { continue; }   // Zip64

                    long zipFirstByte = zipOffset + localHeaderOffset;
                    if (zipFirstByte < 0 || zipFirstByte + 4 > fileLen) { continue; }

                    byte[] sig = new byte[4];
                    fs.Position = zipFirstByte;
                    if (ReadFully(fs, sig, 0, 4) != 4) { continue; }
                    if (sig[0] != 0x50 || sig[1] != 0x4B
                        || sig[2] != 0x03 || sig[3] != 0x04)
                    {
                        continue;   // 本地文件头签名对不上，继续往前找
                    }

                    return zipFirstByte;
                }
            }
            return -1;
        }

        /// <summary>循环读满，避免 FileStream 一次 Read 读不满。</summary>
        public static int ReadFully(Stream s, byte[] buf, int offset, int count)
        {
            int total = 0;
            while (total < count)
            {
                int n = s.Read(buf, offset + total, count - total);
                if (n <= 0) { break; }
                total += n;
            }
            return total;
        }

        /// <summary>
        /// 按偏移打开追加在 exe 末尾的 zip（只读）。
        ///
        /// 【重要】不能直接 new ZipArchive(new FileStream(exePath), Read)：
        /// .NET Framework 的 ZipArchive 会把条目偏移当作「相对于流起点」来解释，
        /// 而本 exe 里的 zip 起点是文件末尾附近的一个非零偏移，这样读会抛
        /// InvalidDataException（"中央目录结尾中应包含的条目数与中央目录中的条目数不对应"）。
        ///
        /// 解决办法：构造一个"Position 从 0 开始、只覆盖 zip 数据"的流。两种策略：
        ///   · 负载 < 768MB（大多数情况）：把 zip 数据切片到内存的 MemoryStream。
        ///     只复制压缩后的负载（700MB 负载 → 700MB 内存），安装完即释放，
        ///     简单可靠且可以反复打开。
        ///   · 负载 ≥ 768MB：用内存映射文件的视图流（零拷贝），避免内存不足。
        ///     注意内存映射句柄的释放不是即时的，短时间内重复打开同一文件可能报
        ///     "正由另一进程使用"，因此只在确有必要时才用。
        /// </summary>
        public static ZipArchive OpenZipAt(string exePath, long zipOffset)
        {
            FileInfo info = new FileInfo(exePath);
            long len = info.Length - zipOffset;
            if (len <= 0) { throw new InvalidDataException("负载数据长度为 0。"); }

            if (len <= MaxInMemoryPayload)
            {
                byte[] all = File.ReadAllBytes(exePath);
                // MemoryStream 切片：Position 从 0 开始，Length 正好是负载长度
                MemoryStream ms = new MemoryStream(all, (int)zipOffset, (int)len, false, false);
                return new ZipArchive(ms, ZipArchiveMode.Read, false);
            }

            MemoryMappedFile mmf = MemoryMappedFile.CreateFromFile(
                exePath, FileMode.Open, null, 0, MemoryMappedFileAccess.Read);
            try
            {
                MemoryMappedViewStream view = mmf.CreateViewStream(zipOffset, len, MemoryMappedFileAccess.Read);
                return new ZipArchive(new NonClosingStream(view), ZipArchiveMode.Read, false);
            }
            catch
            {
                mmf.Dispose();
                throw;
            }
        }
    }

    /// <summary>
    /// 包装一个流，但不在 Dispose 时关闭被包装的流。
    /// 用途：ZipArchive 会关掉传给它的流，而内存映射视图流需要由外层统一释放。
    /// </summary>
    internal sealed class NonClosingStream : Stream
    {
        private readonly Stream _inner;

        public NonClosingStream(Stream inner) { _inner = inner; }

        public override bool CanRead { get { return _inner.CanRead; } }
        public override bool CanSeek { get { return _inner.CanSeek; } }
        public override bool CanWrite { get { return false; } }
        public override long Length { get { return _inner.Length; } }

        public override long Position
        {
            get { return _inner.Position; }
            set { _inner.Position = value; }
        }

        public override int Read(byte[] buffer, int offset, int count)
        {
            return _inner.Read(buffer, offset, count);
        }

        public override long Seek(long offset, SeekOrigin origin)
        {
            return _inner.Seek(offset, origin);
        }

        public override void Flush() { }

        public override void SetLength(long value)
        {
            throw new NotSupportedException();
        }

        public override void Write(byte[] buffer, int offset, int count)
        {
            throw new NotSupportedException();
        }

        protected override void Dispose(bool disposing)
        {
            // 刻意不释放 _inner：它的生命周期由调用方（外层 using）管理
            base.Dispose(disposing);
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    //  5. 解压条目过滤器（前缀规则 + 大数据库条目 + 路径安全）
    // ═══════════════════════════════════════════════════════════════════════

    internal static class EntryFilter
    {
        /// <summary>把 zip 内的正斜杠路径规范化为反斜杠相对路径；非法返回 null。</summary>
        public static string Normalize(string entryFullName)
        {
            if (string.IsNullOrEmpty(entryFullName)) { return null; }
            string s = entryFullName.Replace('\\', '/');
            while (s.StartsWith("./", StringComparison.Ordinal)) { s = s.Substring(2); }
            s = s.TrimStart('/');
            if (s.Length == 0) { return null; }

            string[] parts = s.Split('/');
            List<string> clean = new List<string>();
            for (int i = 0; i < parts.Length; i++)
            {
                string p = parts[i];
                if (p.Length == 0 || p == ".") { continue; }
                if (p == "..") { return null; }              // 目录穿越，直接拒绝
                if (p.IndexOf(':') >= 0) { return null; }    // 盘符 / 备用数据流，拒绝
                if (p.IndexOfAny(new char[] { '<', '>', '"', '|', '?', '*' }) >= 0) { return null; }
                clean.Add(p);
            }
            if (clean.Count == 0) { return null; }
            return string.Join("\\", clean.ToArray());
        }

        /// <summary>是否为「预装行情数据」条目。</summary>
        public static bool IsBigDataEntry(string entryFullName)
        {
            string norm = Normalize(entryFullName);
            if (norm == null) { return false; }
            return string.Equals(norm, PayloadLayout.BigDataEntry.Replace('/', '\\'), StringComparison.OrdinalIgnoreCase);
        }

        /// <summary>目标绝对路径是否安全地落在 installDir 之内。</summary>
        public static bool IsInside(string installDir, string targetAbsPath)
        {
            string root = Path.GetFullPath(installDir);
            if (!root.EndsWith("\\", StringComparison.Ordinal)) { root = root + "\\"; }
            string full = Path.GetFullPath(targetAbsPath);
            return full.StartsWith(root, StringComparison.OrdinalIgnoreCase);
        }

        /// <summary>
        /// 判断某条目在本次安装中是否需要解压。
        /// installData=false 时跳过 app/data/sequoia_v2.db。
        /// </summary>
        public static bool ShouldExtract(string entryFullName, bool isDirectory, bool installData)
        {
            if (isDirectory) { return false; }
            string norm = Normalize(entryFullName);
            if (norm == null) { return false; }
            if (!installData && string.Equals(norm, PayloadLayout.BigDataEntry.Replace('/', '\\'), StringComparison.OrdinalIgnoreCase))
            {
                return false;
            }
            return true;
        }

        /// <summary>
        /// 检测条目是否用了 .NET Framework 的 ZipArchive 解不开的压缩方法。
        /// 典型是 ZIP_LZMA（方法号 14）、BZip2（12）、Zstd（93）、AES（99）。
        /// .NET Framework 只内置支持 Stored(0) 与 Deflate(8)。
        ///
        /// 返回 null 表示方法受支持；否则返回中文说明，供安装器给出可读错误。
        /// 通过反射读压缩方法号，是因为 ZipArchiveEntry 没有公开该属性。
        /// </summary>
        public static string DescribeUnsupportedMethod(ZipArchiveEntry entry)
        {
            try
            {
                object raw = entry.GetType().InvokeMember(
                    "CompressionMethod",
                    System.Reflection.BindingFlags.GetProperty
                        | System.Reflection.BindingFlags.NonPublic
                        | System.Reflection.BindingFlags.Instance,
                    null, entry, null);
                if (raw == null) { return null; }

                int method = Convert.ToInt32(raw, CultureInfo.InvariantCulture);
                if (method == 0 || method == 8) { return null; }

                string name;
                switch (method)
                {
                    case 12: name = "BZip2"; break;
                    case 14: name = "ZIP_LZMA"; break;
                    case 93: name = "Zstd"; break;
                    case 95: name = "XZ"; break;
                    case 99: name = "AES 加密"; break;
                    default: name = "方法号 " + method.ToString(CultureInfo.InvariantCulture); break;
                }
                return "本安装器所依赖的 .NET Framework 只能解压 Stored/Deflate 压缩的 zip，" +
                       "当前条目使用了 " + name + " 压缩，无法解压。请改用 Deflate 重新打包负载。";
            }
            catch
            {
                // 拿不到方法号就不拦，交给真实读取去暴露问题
                return null;
            }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    //  6. 快捷方式（.lnk）创建
    // ═══════════════════════════════════════════════════════════════════════

    internal static class ShortcutUtil
    {
        /// <summary>
        /// 创建一个 .lnk。失败返回 false（调用方只记日志，不中断安装）。
        /// </summary>
        public static bool Create(string lnkPath, string targetPath, string arguments,
                                  string workingDir, string iconPath, string description)
        {
            IShellLinkW link = null;
            IPersistFile persist = null;
            try
            {
                string dir = Path.GetDirectoryName(lnkPath);
                if (!string.IsNullOrEmpty(dir)) { Directory.CreateDirectory(dir); }

                link = (IShellLinkW)new ShellLinkCoClass();
                link.SetPath(targetPath);
                if (!string.IsNullOrEmpty(arguments)) { link.SetArguments(arguments); }
                if (!string.IsNullOrEmpty(workingDir)) { link.SetWorkingDirectory(workingDir); }
                if (!string.IsNullOrEmpty(description)) { link.SetDescription(description); }
                if (!string.IsNullOrEmpty(iconPath)) { link.SetIconLocation(iconPath, 0); }

                persist = (IPersistFile)link;
                persist.Save(lnkPath, true);
                return File.Exists(lnkPath);
            }
            catch
            {
                return false;
            }
            finally
            {
                if (persist != null) { try { Marshal.ReleaseComObject(persist); } catch { } }
                if (link != null) { try { Marshal.ReleaseComObject(link); } catch { } }
            }
        }

        /// <summary>删除文件，返回是否成功（或不存在的视作成功）。</summary>
        public static bool TryDeleteFile(string path)
        {
            try
            {
                if (File.Exists(path)) { File.Delete(path); }
                return true;
            }
            catch { return false; }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    //  7. 主窗口
    // ═══════════════════════════════════════════════════════════════════════

    internal sealed class SetupForm : Form
    {
        // ── 配色 ──────────────────────────────────────────────────────────
        private static readonly Color ClrBrand   = Color.FromArgb(17, 94, 89);     // 深墨绿
        private static readonly Color ClrAccent  = Color.FromArgb(0, 137, 123);
        private static readonly Color ClrMuted   = Color.FromArgb(110, 118, 125);
        private static readonly Color ClrPageBack = Color.White;

        // ── 常量 ──────────────────────────────────────────────────────────
        private const string ProductTitle = "Sequoia-X";
        private const string UrlFallback = "http://127.0.0.1:8899";
        private const string UninstallKey = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\Sequoia-X";
        private const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
        private const string RunValueName = "Sequoia-X";

        // ── 运行期状态 ────────────────────────────────────────────────────
        private readonly string _exePath;
        private long _zipOffset = -1;
        private PayloadMeta _meta = new PayloadMeta();
        private bool _zipHasBigData = false;     // zip 里是否真的有 app/data/sequoia_v2.db
        private bool _installing = false;
        private bool _finished = false;
        private List<string> _warnings = new List<string>();

        // ── /Quiet（命令行自动化）─────────────────────────────────────────
        private bool _suppressDialogs = false;   // /Quiet：不弹任何确认框
        private bool _presetNoData = false;      // /NODATA：界面上也不勾选行情数据
        private bool _quietAutoStart = false;    // /Quiet：窗口画好后自动开始安装

        /// <summary>把 /Quiet 的命令行选项套用到界面上（目录、各开关、是否抑制弹框）。</summary>
        internal void ApplyCommandLine(QuietOptions o)
        {
            _suppressDialogs = o.Quiet;
            _quietAutoStart = o.Quiet;
            _presetNoData = !o.InstallData;
            if (!string.IsNullOrEmpty(o.Dir)) { _txtPath.Text = o.Dir; }
            _chkDesktop.Checked = o.Desktop;
            _chkStartMenu.Checked = o.StartMenu;
            _chkAutoRun.Checked = o.AutoStart;
            _chkLaunch.Checked = o.Launch;
            _chkData.Enabled = _zipHasBigData;
            _chkData.Checked = _zipHasBigData && o.InstallData;
        }

        /// <summary>安装是否已经走完（成功时才会置位，供 /Quiet 判断退出码）。</summary>
        internal bool Finished { get { return _finished; } }

        /// <summary>
        /// /Quiet：窗口画出来后自动开始安装，不需要人工点「安装」；
        /// 装完（或失败）后自动收尾关闭，全程不弹任何确认框。
        /// </summary>
        private void QuietAutoInstall()
        {
            if (_finished || _installing) { return; }
            OnInstallClick(null, EventArgs.Empty);
            if (_finished)
            {
                FinishAndLaunch();   // 成功：按「完成」收尾（必要时启动工作台，然后关闭窗口）
            }
            else
            {
                Close();             // 失败：不弹框，直接关掉（细节留给日志/退出码）
            }
        }

        // ── 控件 ──────────────────────────────────────────────────────────
        private Label _lblTitle;
        private Label _lblSubTitle;
        private Panel _rule;
        private Label _lblVersion;
        private Label _lblSizes;
        private GroupBox _grpPath;
        private TextBox _txtPath;
        private Button _btnBrowse;
        private CheckBox _chkDesktop;
        private CheckBox _chkStartMenu;
        private CheckBox _chkAutoRun;
        private CheckBox _chkData;
        private Label _lblDataNote;
        private ProgressBar _progress;
        private Label _lblStatus;
        private Button _btnInstall;
        private Button _btnExit;
        private CheckBox _chkLaunch;

        public SetupForm()
        {
            _exePath = Application.ExecutablePath;
            BuildUi();
        }

        // ───────────────────────────────────────────────────────────────────
        //  UI 构建
        // ───────────────────────────────────────────────────────────────────
        private void BuildUi()
        {
            SuspendLayout();

            Text = "Sequoia-X 安装程序";
            Font = new Font("Microsoft YaHei UI", 9f, FontStyle.Regular, GraphicsUnit.Point);
            BackColor = ClrPageBack;
            ForeColor = Color.FromArgb(33, 37, 41);
            ClientSize = new Size(560, 520);
            StartPosition = FormStartPosition.CenterScreen;
            FormBorderStyle = FormBorderStyle.FixedSingle;
            MaximizeBox = false;
            MinimizeBox = false;
            ShowIcon = false;
            AutoScaleMode = AutoScaleMode.None;

            // 1) 标题区
            _lblTitle = new Label();
            _lblTitle.Text = "Sequoia-X 安装程序";
            _lblTitle.Font = new Font("Microsoft YaHei UI", 18f, FontStyle.Bold, GraphicsUnit.Point);
            _lblTitle.ForeColor = ClrBrand;
            _lblTitle.SetBounds(24, 20, 512, 34);

            _lblSubTitle = new Label();
            _lblSubTitle.Text = "A 股量化选股 · Web 工作台";
            _lblSubTitle.Font = new Font("Microsoft YaHei UI", 9.5f, FontStyle.Regular, GraphicsUnit.Point);
            _lblSubTitle.ForeColor = ClrMuted;
            _lblSubTitle.SetBounds(26, 56, 512, 20);

            _rule = new Panel();
            _rule.BackColor = Color.FromArgb(226, 232, 240);
            _rule.SetBounds(24, 82, 512, 1);

            // 2) 版本 / 负载信息
            _lblVersion = new Label();
            _lblVersion.Text = "版本 …";
            _lblVersion.ForeColor = Color.FromArgb(33, 37, 41);
            _lblVersion.SetBounds(24, 92, 512, 18);

            _lblSizes = new Label();
            _lblSizes.Text = "";
            _lblSizes.ForeColor = ClrMuted;
            _lblSizes.SetBounds(24, 112, 512, 18);

            // 3) 安装位置
            _grpPath = new GroupBox();
            _grpPath.Text = " 安装位置 ";
            _grpPath.ForeColor = ClrBrand;
            _grpPath.SetBounds(20, 140, 520, 74);

            _txtPath = new TextBox();
            _txtPath.ReadOnly = true;
            _txtPath.BackColor = Color.FromArgb(248, 249, 250);
            _txtPath.SetBounds(14, 30, 396, 24);
            _txtPath.Text = DefaultInstallDir();

            _btnBrowse = new Button();
            _btnBrowse.Text = "浏览…";
            _btnBrowse.SetBounds(420, 29, 84, 26);
            _btnBrowse.FlatStyle = FlatStyle.System;
            _btnBrowse.Click += OnBrowseClick;

            _grpPath.Controls.Add(_txtPath);
            _grpPath.Controls.Add(_btnBrowse);

            // 4) 选项复选框
            _chkDesktop = new CheckBox();
            _chkDesktop.Text = "创建桌面快捷方式";
            _chkDesktop.Checked = true;
            _chkDesktop.SetBounds(24, 226, 300, 22);

            _chkStartMenu = new CheckBox();
            _chkStartMenu.Text = "创建开始菜单快捷方式";
            _chkStartMenu.Checked = true;
            _chkStartMenu.SetBounds(24, 250, 300, 22);

            _chkAutoRun = new CheckBox();
            _chkAutoRun.Text = "开机自动启动工作台";
            _chkAutoRun.Checked = false;
            _chkAutoRun.SetBounds(24, 274, 300, 22);

            _chkData = new CheckBox();
            _chkData.Text = "安装预装的历史行情数据（约 494 MB，跳过的话首次使用需要自己回填约 1.5 小时）";
            _chkData.Checked = true;
            _chkData.SetBounds(24, 300, 512, 22);

            _lblDataNote = new Label();
            _lblDataNote.Text = "本次安装包未包含行情数据";
            _lblDataNote.ForeColor = ClrMuted;
            _lblDataNote.SetBounds(44, 322, 460, 18);
            _lblDataNote.Visible = false;

            // 5) 进度
            _progress = new ProgressBar();
            _progress.Minimum = 0;
            _progress.Maximum = 100;
            _progress.Value = 0;
            _progress.Style = ProgressBarStyle.Continuous;
            _progress.SetBounds(24, 352, 512, 18);

            _lblStatus = new Label();
            _lblStatus.Text = "";
            _lblStatus.ForeColor = ClrMuted;
            _lblStatus.SetBounds(24, 376, 512, 20);

            // 6) 完成阶段的「立即启动」
            _chkLaunch = new CheckBox();
            _chkLaunch.Text = "立即启动 Sequoia-X 工作台";
            _chkLaunch.Checked = true;
            _chkLaunch.SetBounds(24, 404, 400, 22);
            _chkLaunch.Visible = false;

            // 7) 底部按钮
            _btnInstall = new Button();
            _btnInstall.Text = "安装";
            _btnInstall.SetBounds(336, 452, 100, 34);
            _btnInstall.FlatStyle = FlatStyle.System;
            _btnInstall.Font = new Font("Microsoft YaHei UI", 9.5f, FontStyle.Bold, GraphicsUnit.Point);
            _btnInstall.Click += OnInstallClick;

            _btnExit = new Button();
            _btnExit.Text = "退出";
            _btnExit.SetBounds(444, 452, 92, 34);
            _btnExit.FlatStyle = FlatStyle.System;
            _btnExit.Click += OnExitClick;

            Controls.Add(_lblTitle);
            Controls.Add(_lblSubTitle);
            Controls.Add(_rule);
            Controls.Add(_lblVersion);
            Controls.Add(_lblSizes);
            Controls.Add(_grpPath);
            Controls.Add(_chkDesktop);
            Controls.Add(_chkStartMenu);
            Controls.Add(_chkAutoRun);
            Controls.Add(_chkData);
            Controls.Add(_lblDataNote);
            Controls.Add(_progress);
            Controls.Add(_lblStatus);
            Controls.Add(_chkLaunch);
            Controls.Add(_btnInstall);
            Controls.Add(_btnExit);

            FormClosing += OnFormClosing;

            ResumeLayout(false);

            // 负载定位放在窗口构造里做：失败时由 OnShown 弹提示并退出
            TryLoadPayload();
        }

        private static string DefaultInstallDir()
        {
            string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            return Path.Combine(Path.Combine(local, "Programs"), "Sequoia-X");
        }

        // ───────────────────────────────────────────────────────────────────
        //  负载定位 + payload.json 读取
        // ───────────────────────────────────────────────────────────────────
        private void TryLoadPayload()
        {
            try
            {
                _zipOffset = PayloadLocator.FindZipOffset(_exePath);
                if (_zipOffset < 0) { return; }

                using (ZipArchive zip = PayloadLocator.OpenZipAt(_exePath, _zipOffset))
                {
                    // 统计是否真的有行情数据条目
                    for (int i = 0; i < zip.Entries.Count; i++)
                    {
                        if (EntryFilter.IsBigDataEntry(zip.Entries[i].FullName))
                        {
                            _zipHasBigData = true;
                            break;
                        }
                    }

                    // 读 payload.json
                    ZipArchiveEntry metaEntry = null;
                    for (int i = 0; i < zip.Entries.Count; i++)
                    {
                        string norm = EntryFilter.Normalize(zip.Entries[i].FullName);
                        if (norm != null && string.Equals(norm, PayloadLayout.MetaEntry, StringComparison.OrdinalIgnoreCase))
                        {
                            metaEntry = zip.Entries[i];
                            break;
                        }
                    }

                    if (metaEntry != null)
                    {
                        string json = ReadEntryText(metaEntry);
                        _meta = ParseMeta(json);
                    }
                    else
                    {
                        // 没有 payload.json：用条目情况兜底
                        _meta = new PayloadMeta();
                        _meta.HasData = _zipHasBigData;
                    }
                }

                // has_data 与实际情况取交集：以 zip 中真实存在的条目为准
                if (!_zipHasBigData) { _meta.HasData = false; }
            }
            catch (Exception ex)
            {
                _zipOffset = -1;
                _warnings.Add("读取安装包负载信息失败：" + ex.Message);
            }
        }

        private static string ReadEntryText(ZipArchiveEntry entry)
        {
            using (Stream s = entry.Open())
            using (StreamReader r = new StreamReader(s, new UTF8Encoding(false), true))
            {
                return r.ReadToEnd();
            }
        }

        private static PayloadMeta ParseMeta(string json)
        {
            PayloadMeta m = new PayloadMeta();
            try
            {
                string app = MiniJson.GetString(json, "app");
                if (!string.IsNullOrEmpty(app)) { m.App = app; }
                string ver = MiniJson.GetString(json, "version");
                if (!string.IsNullOrEmpty(ver)) { m.Version = ver; }
                string built = MiniJson.GetString(json, "built_at");
                if (built != null) { m.BuiltAt = built; }

                double port = MiniJson.GetNumber(json, "port");
                if (!double.IsNaN(port) && port > 0 && port < 65536) { m.Port = (int)port; }

                bool? hasData = MiniJson.GetBool(json, "has_data");
                m.HasData = (hasData == true);

                double d = MiniJson.GetNumber(json, "data_size_mb");
                if (!double.IsNaN(d)) { m.DataSizeMb = d; }
                double r = MiniJson.GetNumber(json, "runtime_size_mb");
                if (!double.IsNaN(r)) { m.RuntimeSizeMb = r; }
                double a = MiniJson.GetNumber(json, "app_size_mb");
                if (!double.IsNaN(a)) { m.AppSizeMb = a; }
            }
            catch
            {
                // 解析失败就用默认值，不影响安装
            }
            return m;
        }

        // ───────────────────────────────────────────────────────────────────
        //  窗口显示后：填充信息 / 处理不完整安装包
        // ───────────────────────────────────────────────────────────────────
        protected override void OnShown(EventArgs e)
        {
            base.OnShown(e);

            if (_zipOffset < 0)
            {
                if (!_suppressDialogs)
                {
                    MessageBox.Show(this,
                        "此安装包不完整（缺少负载数据），请重新下载。",
                        "Sequoia-X 安装程序",
                        MessageBoxButtons.OK, MessageBoxIcon.Error);
                }
                // 用 BeginInvoke 让窗口先画出来再关闭，避免 MessageBox 无父窗口
                BeginInvoke(new MethodInvoker(Close));
                return;
            }

            _lblVersion.Text = _meta.VersionLine();
            _lblSizes.Text = _meta.SizeLine();

            if (!_zipHasBigData)
            {
                _chkData.Checked = false;
                _chkData.Enabled = false;
                _lblDataNote.Visible = true;
            }
            else
            {
                _chkData.Checked = !_presetNoData;   // /NODATA 时不勾选
                _chkData.Enabled = true;
                _lblDataNote.Visible = false;
                if (_meta.DataSizeMb > 0)
                {
                    _chkData.Text = "安装预装的历史行情数据（约 " +
                        _meta.DataSizeMb.ToString("0.#", CultureInfo.InvariantCulture) +
                        " MB，跳过的话首次使用需要自己回填约 1.5 小时）";
                }
            }

            // /Quiet：窗口已经画出来了，自动开始安装（不需要人工点「安装」）
            if (_quietAutoStart && !_finished && !_installing)
            {
                BeginInvoke(new MethodInvoker(QuietAutoInstall));
            }
        }

        // ───────────────────────────────────────────────────────────────────
        //  事件
        // ───────────────────────────────────────────────────────────────────
        private void OnBrowseClick(object sender, EventArgs e)
        {
            if (_installing) { return; }
            using (FolderBrowserDialog dlg = new FolderBrowserDialog())
            {
                dlg.Description = "请选择 Sequoia-X 的安装目录";
                dlg.ShowNewFolderButton = true;
                string current = _txtPath.Text;
                try
                {
                    if (Directory.Exists(current)) { dlg.SelectedPath = current; }
                    else
                    {
                        string parent = Path.GetDirectoryName(current);
                        if (!string.IsNullOrEmpty(parent) && Directory.Exists(parent)) { dlg.SelectedPath = parent; }
                    }
                }
                catch { }
                if (dlg.ShowDialog(this) == DialogResult.OK && !string.IsNullOrEmpty(dlg.SelectedPath))
                {
                    // 用户选中的目录作为父目录，产品名固定追加，避免直接装到盘根 / 桌面
                    string picked = dlg.SelectedPath.TrimEnd('\\');
                    if (string.Equals(Path.GetFileName(picked), "Sequoia-X", StringComparison.OrdinalIgnoreCase))
                    {
                        _txtPath.Text = picked;
                    }
                    else
                    {
                        _txtPath.Text = Path.Combine(picked, "Sequoia-X");
                    }
                }
            }
        }

        private void OnExitClick(object sender, EventArgs e)
        {
            if (_installing) { return; }
            Close();
        }

        private void OnFormClosing(object sender, FormClosingEventArgs e)
        {
            if (_installing)
            {
                e.Cancel = true;   // 安装中不允许关闭
            }
        }

        // ───────────────────────────────────────────────────────────────────
        //  静默安装支持（入口在文件末尾的 SilentSetup）
        //
        //  设计：**不改动 GUI 的安装流程**，只是把同一批方法在无界面的情况下
        //  按顺序调一遍。复选框状态没法让用户点，就由参数直接写进去。
        // ───────────────────────────────────────────────────────────────────

        /// <summary>静默模式：读一次负载信息，返回是否真的找到了追加的负载。</summary>
        internal bool SilentLoadPayload()
        {
            _warnings = new List<string>();
            TryLoadPayload();
            return _zipOffset >= 0;
        }

        /// <summary>安装包里是否真的带了行情数据条目（以 zip 内容为准，不看 payload.json）。</summary>
        internal bool SilentHasData { get { return _zipHasBigData; } }

        /// <summary>静默安装过程中记下的警告（快捷方式失败之类，不致命）。</summary>
        internal List<string> SilentWarnings { get { return _warnings; } }

        /// <summary>默认安装目录（%LOCALAPPDATA%\Programs\Sequoia-X）。</summary>
        internal static string SilentDefaultDir() { return DefaultInstallDir(); }

        /// <summary>
        /// 静默执行完整安装，返回解压出的总字节数。
        /// 顺序与 OnInstallClick 里完全一致，保证两条路径行为相同。
        /// </summary>
        internal long SilentInstall(string installDir, bool installData,
                                    bool desktop, bool startMenu, bool autoStart)
        {
            _chkData.Checked = installData && _chkData.Enabled;
            _chkDesktop.Checked = desktop;
            _chkStartMenu.Checked = startMenu;
            _chkAutoRun.Checked = autoStart;

            Directory.CreateDirectory(installDir);
            long totalBytes = ExtractAll(installDir, installData);
            WriteInstallJson(installDir);
            CreateShortcuts(installDir);
            WriteAutoRun(installDir);
            WriteUninstallEntry(installDir, totalBytes);
            return totalBytes;
        }

        private void OnInstallClick(object sender, EventArgs e)
        {
            if (_finished) { FinishAndLaunch(); return; }
            if (_installing) { return; }

            string installDir = (_txtPath.Text ?? "").Trim();
            if (installDir.Length == 0)
            {
                if (!_suppressDialogs)
                {
                    MessageBox.Show(this, "请先选择安装位置。", "Sequoia-X 安装程序",
                        MessageBoxButtons.OK, MessageBoxIcon.Warning);
                }
                return;
            }
            try
            {
                installDir = Path.GetFullPath(installDir);
            }
            catch (Exception ex)
            {
                if (!_suppressDialogs)
                {
                    MessageBox.Show(this, "安装路径不合法：" + ex.Message, "Sequoia-X 安装程序",
                        MessageBoxButtons.OK, MessageBoxIcon.Warning);
                }
                return;
            }
            _txtPath.Text = installDir;

            // 已安装检测：/Quiet 自动化时直接覆盖安装，不询问
            if (!_suppressDialogs)
            {
                try
                {
                    string marker = Path.Combine(installDir, "install.json");
                    if (Directory.Exists(installDir) && File.Exists(marker))
                    {
                        DialogResult r = MessageBox.Show(this,
                            "已检测到已安装的 Sequoia-X，是否覆盖安装？（不会删除你的 data 目录）",
                            "Sequoia-X 安装程序",
                            MessageBoxButtons.YesNo, MessageBoxIcon.Question, MessageBoxDefaultButton.Button2);
                        if (r != DialogResult.Yes) { return; }
                    }
                }
                catch { }
            }

            bool installData = _chkData.Enabled && _chkData.Checked;
            SetBusy(true);
            _warnings = new List<string>();
            Application.DoEvents();

            // 安装本身在 UI 线程跑（单步解压 + 进度刷新），异常统一在此捕获
            try
            {
                long totalBytes = ExtractAll(installDir, installData);
                WriteInstallJson(installDir);
                CreateShortcuts(installDir);
                WriteAutoRun(installDir);
                WriteUninstallEntry(installDir, totalBytes);
                SetFinishedState(installData);
            }
            catch (Exception ex)
            {
                SetBusy(false);
                _progress.Style = ProgressBarStyle.Continuous;
                _lblStatus.Text = "安装失败";
                ShowError("安装过程中发生错误，安装未完成。", ex);
            }
        }

        // ───────────────────────────────────────────────────────────────────
        //  解压
        // ───────────────────────────────────────────────────────────────────
        private long ExtractAll(string installDir, bool installData)
        {
            Directory.CreateDirectory(installDir);

            long writtenBytes = 0;
            using (ZipArchive zip = PayloadLocator.OpenZipAt(_exePath, _zipOffset))
            {
                // 先算需要解压的条目数（按条目数算进度）
                int total = 0, done = 0;
                for (int i = 0; i < zip.Entries.Count; i++)
                {
                    ZipArchiveEntry en = zip.Entries[i];
                    bool isDir = en.FullName.EndsWith("/", StringComparison.Ordinal)
                              || en.FullName.EndsWith("\\", StringComparison.Ordinal);
                    if (EntryFilter.ShouldExtract(en.FullName, isDir, installData)) { total++; }
                }
                if (total <= 0) { total = 1; }

                _progress.Style = ProgressBarStyle.Continuous;
                _progress.Minimum = 0;
                _progress.Maximum = total;
                _progress.Value = 0;

                for (int i = 0; i < zip.Entries.Count; i++)
                {
                    ZipArchiveEntry en = zip.Entries[i];
                    bool isDir = en.FullName.EndsWith("/", StringComparison.Ordinal)
                              || en.FullName.EndsWith("\\", StringComparison.Ordinal);
                    if (!EntryFilter.ShouldExtract(en.FullName, isDir, installData)) { continue; }

                    string rel = EntryFilter.Normalize(en.FullName);
                    if (rel == null) { continue; }

                    // 提前识别 .NET 解不开的压缩方法，给出可读的中文错误而不是底层异常
                    string badMethod = EntryFilter.DescribeUnsupportedMethod(en);
                    if (badMethod != null)
                    {
                        throw new NotSupportedException("条目「" + en.FullName + "」无法解压：" + badMethod);
                    }

                    string target = Path.Combine(installDir, rel);
                    if (!EntryFilter.IsInside(installDir, target))
                    {
                        _warnings.Add("跳过不安全的条目：" + en.FullName);
                        continue;
                    }

                    bool isBig = EntryFilter.IsBigDataEntry(en.FullName);
                    if (isBig)
                    {
                        // 494MB 的大文件：切 marquee，给出等待提示
                        _lblStatus.Text = "正在解压历史行情数据（约 " +
                            (_meta.DataSizeMb > 0 ? _meta.DataSizeMb.ToString("0.#", CultureInfo.InvariantCulture) : "494") +
                            " MB，请稍候）…";
                        _progress.Style = ProgressBarStyle.Marquee;
                        _progress.MarqueeAnimationSpeed = 30;
                        Application.DoEvents();
                    }
                    else
                    {
                        _lblStatus.Text = "正在解压：" + rel;
                        Application.DoEvents();
                    }

                    string dir = Path.GetDirectoryName(target);
                    if (!string.IsNullOrEmpty(dir)) { Directory.CreateDirectory(dir); }

                    // 目标文件可能被杀软 / 资源管理器短暂占用，做有限次重试
                    int attempt = 0;
                    while (true)
                    {
                        try
                        {
                            using (Stream src = en.Open())
                            using (FileStream dst = new FileStream(target, FileMode.Create, FileAccess.Write, FileShare.None))
                            {
                                byte[] buf = new byte[1024 * 256];
                                int n;
                                while ((n = src.Read(buf, 0, buf.Length)) > 0)
                                {
                                    dst.Write(buf, 0, n);
                                    writtenBytes += n;
                                }
                            }
                            break;
                        }
                        catch (IOException ex)
                        {
                            attempt++;
                            if (attempt > 3)
                            {
                                throw new IOException("写入文件失败（可能被占用）：" + target + " —— " + ex.Message, ex);
                            }
                            _lblStatus.Text = "文件被占用，正在重试（" + attempt + "/3）：" + rel;
                            Application.DoEvents();
                            Thread.Sleep(300);
                        }
                    }

                    if (isBig)
                    {
                        _progress.Style = ProgressBarStyle.Continuous;
                        _progress.Minimum = 0;
                        _progress.Maximum = total;
                    }

                    done++;
                    if (done <= _progress.Maximum) { _progress.Value = done; }
                    Application.DoEvents();
                }
            }

            // 确保关键目录存在，避免首次启动找不到 logs / data
            Directory.CreateDirectory(Path.Combine(installDir, "app"));
            Directory.CreateDirectory(Path.Combine(installDir, "app\\data"));
            Directory.CreateDirectory(Path.Combine(installDir, "app\\logs"));
            return writtenBytes;
        }

        // ───────────────────────────────────────────────────────────────────
        //  install.json
        // ───────────────────────────────────────────────────────────────────
        private void WriteInstallJson(string installDir)
        {
            _lblStatus.Text = "正在写入安装信息…";
            Application.DoEvents();

            StringBuilder sb = new StringBuilder();
            sb.Append("{\"install_dir\":").Append(JsonStr(installDir));
            sb.Append(",\"installed_at\":").Append(JsonStr(DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture)));
            sb.Append(",\"version\":").Append(JsonStr(_meta.Version));
            sb.Append("}");
            string path = Path.Combine(installDir, "install.json");
            File.WriteAllText(path, sb.ToString(), new UTF8Encoding(false));
        }

        private static string JsonStr(string s)
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

        // ───────────────────────────────────────────────────────────────────
        //  快捷方式
        // ───────────────────────────────────────────────────────────────────
        private void CreateShortcuts(string installDir)
        {
            _lblStatus.Text = "正在创建快捷方式…";
            Application.DoEvents();

            string launcher = Path.Combine(installDir, "Launcher.exe");
            string uninstaller = Path.Combine(installDir, "Uninstaller.exe");
            string lnkName = "Sequoia-X 工作台.lnk";

            if (_chkDesktop.Checked)
            {
                try
                {
                    string desktop = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
                    string lnk = Path.Combine(desktop, lnkName);
                    if (!ShortcutUtil.Create(lnk, launcher, "", installDir, launcher, "Sequoia-X 量化选股工作台"))
                    {
                        _warnings.Add("创建桌面快捷方式失败：" + lnk);
                    }
                }
                catch (Exception ex) { _warnings.Add("创建桌面快捷方式失败：" + ex.Message); }
            }

            if (_chkStartMenu.Checked)
            {
                try
                {
                    string programs = Environment.GetFolderPath(Environment.SpecialFolder.Programs);
                    string folder = Path.Combine(programs, "Sequoia-X");
                    Directory.CreateDirectory(folder);

                    string lnk = Path.Combine(folder, lnkName);
                    if (!ShortcutUtil.Create(lnk, launcher, "", installDir, launcher, "Sequoia-X 量化选股工作台"))
                    {
                        _warnings.Add("创建开始菜单快捷方式失败：" + lnk);
                    }

                    string lnkUn = Path.Combine(folder, "卸载 Sequoia-X.lnk");
                    if (!ShortcutUtil.Create(lnkUn, uninstaller, "", installDir, uninstaller, "卸载 Sequoia-X"))
                    {
                        _warnings.Add("创建卸载快捷方式失败：" + lnkUn);
                    }
                }
                catch (Exception ex) { _warnings.Add("创建开始菜单快捷方式失败：" + ex.Message); }
            }
        }

        // ───────────────────────────────────────────────────────────────────
        //  注册表：开机自启 + 卸载信息
        // ───────────────────────────────────────────────────────────────────
        private void WriteAutoRun(string installDir)
        {
            try
            {
                using (RegistryKey k = Registry.CurrentUser.OpenSubKey(RunKey, true))
                {
                    if (k == null) { return; }
                    if (_chkAutoRun.Checked)
                    {
                        string launcher = Path.Combine(installDir, "Launcher.exe");
                        k.SetValue(RunValueName, "\"" + launcher + "\" --silent", RegistryValueKind.String);
                    }
                    else
                    {
                        if (k.GetValue(RunValueName) != null) { k.DeleteValue(RunValueName, false); }
                    }
                }
            }
            catch (Exception ex)
            {
                _warnings.Add("写入开机自启设置失败：" + ex.Message);
            }
        }

        private void WriteUninstallEntry(string installDir, long payloadBytes)
        {
            _lblStatus.Text = "正在登记卸载信息…";
            Application.DoEvents();

            try
            {
                long dirBytes = DirectorySize(installDir);
                int estimatedKb = (int)Math.Min(int.MaxValue, (dirBytes > 0 ? dirBytes : payloadBytes) / 1024);

                using (RegistryKey k = Registry.CurrentUser.CreateSubKey(UninstallKey))
                {
                    if (k == null) { _warnings.Add("无法创建卸载注册表项。"); return; }
                    k.SetValue("DisplayName", "Sequoia-X 量化选股", RegistryValueKind.String);
                    k.SetValue("DisplayVersion", _meta.Version, RegistryValueKind.String);
                    k.SetValue("Publisher", "Sequoia-X", RegistryValueKind.String);
                    k.SetValue("InstallLocation", installDir, RegistryValueKind.String);
                    k.SetValue("DisplayIcon", Path.Combine(installDir, "Launcher.exe"), RegistryValueKind.String);
                    k.SetValue("UninstallString", "\"" + Path.Combine(installDir, "Uninstaller.exe") + "\"", RegistryValueKind.String);
                    k.SetValue("NoModify", 1, RegistryValueKind.DWord);
                    k.SetValue("NoRepair", 1, RegistryValueKind.DWord);
                    k.SetValue("EstimatedSize", estimatedKb, RegistryValueKind.DWord);
                }
            }
            catch (Exception ex)
            {
                _warnings.Add("写入卸载注册表项失败：" + ex.Message);
            }
        }

        private static long DirectorySize(string dir)
        {
            long total = 0;
            try
            {
                string[] files = Directory.GetFiles(dir, "*", SearchOption.AllDirectories);
                for (int i = 0; i < files.Length; i++)
                {
                    try { total += new FileInfo(files[i]).Length; }
                    catch { }
                }
            }
            catch { }
            return total;
        }

        // ───────────────────────────────────────────────────────────────────
        //  界面状态切换
        // ───────────────────────────────────────────────────────────────────
        private void SetBusy(bool busy)
        {
            _installing = busy;
            _txtPath.Enabled = !busy;
            _btnBrowse.Enabled = !busy;
            _chkDesktop.Enabled = !busy;
            _chkStartMenu.Enabled = !busy;
            _chkAutoRun.Enabled = !busy;
            _chkData.Enabled = !busy && _zipHasBigData;
            _btnInstall.Enabled = !busy;
            _btnExit.Enabled = !busy;
            _btnInstall.Text = busy ? "正在安装…" : "安装";
            Cursor = busy ? Cursors.WaitCursor : Cursors.Default;
        }

        private void SetFinishedState(bool installData)
        {
            _installing = false;
            _finished = true;
            _progress.Style = ProgressBarStyle.Continuous;
            _progress.Maximum = 100;
            _progress.Value = 100;
            _lblStatus.ForeColor = ClrAccent;
            _lblStatus.Text = "安装完成";

            _txtPath.Enabled = false;
            _btnBrowse.Enabled = false;
            _chkDesktop.Enabled = false;
            _chkStartMenu.Enabled = false;
            _chkAutoRun.Enabled = false;
            _chkData.Enabled = false;
            _btnExit.Enabled = false;
            _btnExit.Visible = false;
            _btnInstall.Enabled = true;
            _btnInstall.Text = "完成";
            _btnInstall.SetBounds(436, 452, 100, 34);
            _chkLaunch.Visible = true;
            Cursor = Cursors.Default;

            if (_warnings.Count > 0)
            {
                _lblStatus.Text = "安装完成（" + _warnings.Count + " 项非致命警告）";
                if (!_suppressDialogs)
                {
                    MessageBox.Show(this,
                        "安装已完成，但有以下非致命问题：\r\n\r\n· " + string.Join("\r\n· ", _warnings.ToArray()),
                        "Sequoia-X 安装程序", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                }
            }
            if (!installData && !_suppressDialogs)
            {
                // 未安装行情数据：提示回填（/Quiet 自动化时不弹）
                MessageBox.Show(this,
                    "未安装预装行情数据。首次使用请在 Web 工作台的「数据」页回填历史行情（约需 1.5 小时）。",
                    "Sequoia-X 安装程序", MessageBoxButtons.OK, MessageBoxIcon.Information);
            }
        }

        private void FinishAndLaunch()
        {
            if (_chkLaunch.Checked)
            {
                try
                {
                    string launcher = Path.Combine(_txtPath.Text, "Launcher.exe");
                    if (File.Exists(launcher))
                    {
                        ProcessStartInfo psi = new ProcessStartInfo(launcher);
                        psi.WorkingDirectory = _txtPath.Text;
                        psi.UseShellExecute = true;    // 正常启动（走 Shell）
                        Process.Start(psi);
                    }
                    else
                    {
                        if (!_suppressDialogs)
                        {
                            MessageBox.Show(this, "未找到 Launcher.exe：" + launcher, "Sequoia-X 安装程序",
                                MessageBoxButtons.OK, MessageBoxIcon.Warning);
                        }
                    }
                }
                catch (Exception ex)
                {
                    ShowError("启动 Sequoia-X 失败。", ex);
                }
            }
            Close();
        }

        private void ShowError(string headline, Exception ex)
        {
            if (_suppressDialogs) { return; }   // /Quiet 自动化：只改状态栏，不弹框
            MessageBox.Show(this,
                headline + "\r\n\r\n" +
                "错误信息：" + ex.Message + "\r\n\r\n" +
                "技术细节：\r\n" + ex.ToString(),
                "Sequoia-X 安装程序", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    //  8. 静默安装（无界面）
    //     用法：Sequoia-X-Setup.exe /S [/D=<安装目录>] [/NODATA] [/AUTOSTART]
    //                           [/NODESKTOP] [/NOSTARTMENU] [/LOG=<日志文件>]
    //     · /S            静默模式（无界面、无任何对话框）
    //     · /D=<目录>     安装目录，默认 %LOCALAPPDATA%\Programs\Sequoia-X
    //     · /NODATA       不安装预装行情数据（即使安装包里带了）
    //     · /AUTOSTART    写入 HKCU Run 开机自启
    //     · /NODESKTOP    不创建桌面快捷方式
    //     · /NOSTARTMENU  不创建开始菜单快捷方式
    //     · /LOG=<文件>   把过程日志写到指定文件（UTF-8）
    //     退出码：0 成功，2 参数错误，3 安装包缺少负载，1 安装过程出错
    //
    //     说明：这条路径调用的是与 GUI 完全相同的那批方法（ExtractAll /
    //     WriteInstallJson / CreateShortcuts / WriteAutoRun / WriteUninstallEntry），
    //     只是没有界面，所以不会出现"两条路径行为不一致"的问题。
    // ═══════════════════════════════════════════════════════════════════════

    internal static class SilentSetup
    {
        /// <summary>是否应该走静默路径。/S、/silent、--silent 都接受。</summary>
        public static bool IsSilent(string[] args)
        {
            if (args == null) { return false; }
            for (int i = 0; i < args.Length; i++)
            {
                string a = (args[i] ?? "").Trim();
                if (string.Equals(a, "/S", StringComparison.OrdinalIgnoreCase)
                    || string.Equals(a, "-S", StringComparison.OrdinalIgnoreCase)
                    || string.Equals(a, "/silent", StringComparison.OrdinalIgnoreCase)
                    || string.Equals(a, "--silent", StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }
            return false;
        }

        public static int Run(string[] args)
        {
            StringBuilder log = new StringBuilder();
            string logPath = null;
            string installDir = null;
            bool installData = true;
            bool desktop = true;
            bool startMenu = true;
            bool autoStart = false;
            bool launch = false;
            bool badArg = false;
            int exitCode = 1;   // 兜底：任何未显式赋值的路径都算通用错误

            for (int i = 0; i < args.Length; i++)
            {
                string a = (args[i] ?? "").Trim();
                if (a.Length == 0) { continue; }
                if (a.StartsWith("/D=", StringComparison.OrdinalIgnoreCase))
                {
                    // 注：Inno / NSIS 的惯例是 /D= 写在最后，这里放宽为位置不限
                    //（值只取 “=” 之后的部分），因此 /S /D=C:\X /NODATA 也能正常工作。
                    installDir = a.Substring(3).Trim().Trim('"');
                }
                else if (a.StartsWith("/LOG=", StringComparison.OrdinalIgnoreCase))
                {
                    logPath = a.Substring(5).Trim().Trim('"');
                }
                else if (string.Equals(a, "/NODATA", StringComparison.OrdinalIgnoreCase)) { installData = false; }
                else if (string.Equals(a, "/AUTOSTART", StringComparison.OrdinalIgnoreCase)) { autoStart = true; }
                else if (string.Equals(a, "/NODESKTOP", StringComparison.OrdinalIgnoreCase)) { desktop = false; }
                else if (string.Equals(a, "/NOSTARTMENU", StringComparison.OrdinalIgnoreCase)) { startMenu = false; }
                else if (string.Equals(a, "/LAUNCH", StringComparison.OrdinalIgnoreCase)) { launch = true; }
                else if (string.Equals(a, "/QUIET", StringComparison.OrdinalIgnoreCase)) { /* /Quiet 由 GUI 路径处理；在 /S 下与静默等价 */ }
                else if (a.StartsWith("/", StringComparison.Ordinal) || a.StartsWith("-", StringComparison.Ordinal))
                {
                    // /S 本身已由 IsSilent 处理，其它未知开关视为参数错误
                    if (string.Equals(a, "/S", StringComparison.OrdinalIgnoreCase)
                        || string.Equals(a, "-S", StringComparison.OrdinalIgnoreCase)
                        || string.Equals(a, "/silent", StringComparison.OrdinalIgnoreCase)
                        || string.Equals(a, "--silent", StringComparison.OrdinalIgnoreCase))
                    {
                        continue;
                    }
                    badArg = true;
                    log.AppendLine("未知参数：" + a);
                }
            }

            if (badArg)
            {
                exitCode = 2;
                return exitCode;
            }

            try
            {
                if (string.IsNullOrEmpty(installDir))
                {
                    installDir = SetupForm.SilentDefaultDir();
                }
                installDir = Path.GetFullPath(installDir);

                // 目标目录可写性：先建目录，再写一个探测文件并删掉
                if (!EnsureWritable(installDir, log))
                {
                    exitCode = 4;
                    return exitCode;
                }

                // 覆盖安装：清掉上一次失败留下的 install-error.log，避免自动化脚本误判
                ClearStaleErrorLog(installDir);

                SetupForm form = new SetupForm();
                try
                {
                    if (!form.SilentLoadPayload())
                    {
                        log.AppendLine("此安装包不完整（缺少负载数据），请重新下载。");
                        exitCode = 3;
                        return exitCode;
                    }

                    // 安装包里没有行情数据条目时，即使没写 /NODATA 也无法安装
                    bool canData = form.SilentHasData;
                    if (installData && !canData)
                    {
                        log.AppendLine("本次安装包未包含行情数据，已跳过该步骤。");
                    }

                    log.AppendLine("安装目录：" + installDir);
                    log.AppendLine("安装行情数据：" + (installData && canData ? "是" : "否"));

                    long bytes = form.SilentInstall(installDir, installData && canData,
                                                    desktop, startMenu, autoStart);
                    log.AppendLine("解压字节数：" + bytes.ToString(CultureInfo.InvariantCulture));

                    List<string> warns = form.SilentWarnings;
                    if (warns != null && warns.Count > 0)
                    {
                        for (int i = 0; i < warns.Count; i++)
                        {
                            log.AppendLine("警告：" + warns[i]);
                        }
                    }

                    // /LAUNCH：装完立即启动工作台（失败只记日志，不影响退出码）
                    if (launch) { LaunchApp(installDir, log); }

                    log.AppendLine("安装完成。");
                    exitCode = 0;
                    return exitCode;
                }
                finally
                {
                    form.Dispose();
                }
            }
            catch (Exception ex)
            {
                log.AppendLine("安装失败：" + ex.ToString());
                exitCode = 1;
                return exitCode;
            }
            finally
            {
                // 日志尽量写出去，但写日志失败不影响退出码
                try
                {
                    if (!string.IsNullOrEmpty(logPath))
                    {
                        File.WriteAllText(logPath, log.ToString(), new UTF8Encoding(false));
                    }
                    else
                    {
                        // 没有 /LOG= 时把过程日志写到 stderr。
                        // 必须用 UTF-8：.NET 默认按控制台代码页（简体中文 = GBK）输出，
                        // 自动化脚本按 UTF-8 读就会变成乱码。
                        WriteTextToStderr(log.ToString());
                    }
                }
                catch { }

                // 失败时按约定写两处错误日志：
                //   <安装目录>\install-error.log  与  %TEMP%\sequoia-setup-error.log
                if (exitCode != 0)
                {
                    WriteErrorLogs(log, installDir, exitCode);
                }
            }
        }

        /// <summary>确认目标目录存在且确实可写（写一个探测文件再删掉）。</summary>
        private static bool EnsureWritable(string dir, StringBuilder log)
        {
            try
            {
                Directory.CreateDirectory(dir);
                string probe = Path.Combine(dir, ".sequoia-write-probe.tmp");
                File.WriteAllText(probe, "probe", new UTF8Encoding(false));
                File.Delete(probe);
                return true;
            }
            catch (Exception ex)
            {
                log.AppendLine("目标目录不可写：" + dir + " —— " + ex.Message);
                return false;
            }
        }

        /// <summary>删掉上一次失败留下的 install-error.log（成功安装时不该留着旧日志）。</summary>
        private static void ClearStaleErrorLog(string installDir)
        {
            try
            {
                string p = Path.Combine(installDir, "install-error.log");
                if (File.Exists(p)) { File.Delete(p); }
            }
            catch { }
        }

        /// <summary>/LAUNCH：启动 Launcher.exe。失败只记日志，不算安装失败。</summary>
        private static void LaunchApp(string installDir, StringBuilder log)
        {
            try
            {
                string launcher = Path.Combine(installDir, "Launcher.exe");
                if (!File.Exists(launcher))
                {
                    log.AppendLine("提示：未找到 Launcher.exe，已跳过 /LAUNCH。");
                    return;
                }
                ProcessStartInfo psi = new ProcessStartInfo(launcher);
                psi.WorkingDirectory = installDir;
                psi.UseShellExecute = true;
                Process.Start(psi);
                log.AppendLine("已启动 Launcher.exe。");
            }
            catch (Exception ex)
            {
                log.AppendLine("提示：启动 Launcher.exe 失败：" + ex.Message);
            }
        }

        /// <summary>把失败细节写到 &lt;安装目录&gt;\install-error.log 与 %TEMP%\sequoia-setup-error.log。</summary>
        private static void WriteErrorLogs(StringBuilder log, string installDir, int exitCode)
        {
            StringBuilder sb = new StringBuilder();
            sb.AppendLine("Sequoia-X 安装程序 —— 安装失败记录");
            sb.AppendLine("时间    ：" + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture));
            sb.AppendLine("退出码  ：" + exitCode.ToString(CultureInfo.InvariantCulture));
            sb.AppendLine("命令行  ：" + Environment.CommandLine);
            sb.AppendLine("安装目录：" + (string.IsNullOrEmpty(installDir) ? "(未指定)" : installDir));
            sb.AppendLine();
            sb.AppendLine("过程日志：");
            sb.Append(log.ToString());

            string text = sb.ToString();
            if (!string.IsNullOrEmpty(installDir) && Directory.Exists(installDir))
            {
                WriteSafe(Path.Combine(installDir, "install-error.log"), text);
            }
            WriteSafe(Path.Combine(Path.GetTempPath(), "sequoia-setup-error.log"), text);
        }

        private static void WriteSafe(string path, string text)
        {
            try { File.WriteAllText(path, text, new UTF8Encoding(true)); }
            catch { }
        }

        /// <summary>把文本以 UTF-8 写到 stderr（winexe 没有控制台时静默忽略）。</summary>
        private static void WriteTextToStderr(string text)
        {
            try
            {
                using (StreamWriter w = new StreamWriter(Console.OpenStandardError(), new UTF8Encoding(false)))
                {
                    w.Write(text);
                    w.Flush();
                }
            }
            catch { }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    //  8b. /Quiet：显示窗口但不弹确认框（自动化用）
    // ═══════════════════════════════════════════════════════════════════════

    /// <summary>/Quiet 模式要预置到界面上的选项。</summary>
    internal sealed class QuietOptions
    {
        public bool Quiet;                 // /Quiet：抑制所有确认框并自动开始安装
        public string Dir;                 // /D=<目录>
        public bool InstallData = true;    // !/NODATA
        public bool Desktop = true;        // !/NODESKTOP
        public bool StartMenu = true;      // !/NOSTARTMENU
        public bool AutoStart;             // /AUTOSTART
        public bool Launch;                // /LAUNCH

        public bool HasAnyOption
        {
            get
            {
                return Quiet || !string.IsNullOrEmpty(Dir) || !InstallData
                    || !Desktop || !StartMenu || AutoStart || Launch;
            }
        }

        public static QuietOptions Parse(string[] args)
        {
            QuietOptions o = new QuietOptions();
            if (args == null) { return o; }
            for (int i = 0; i < args.Length; i++)
            {
                string a = (args[i] ?? "").Trim();
                if (a.Length == 0) { continue; }
                if (a.StartsWith("/D=", StringComparison.OrdinalIgnoreCase))
                {
                    o.Dir = a.Substring(3).Trim().Trim('"');
                }
                else if (string.Equals(a, "/QUIET", StringComparison.OrdinalIgnoreCase)) { o.Quiet = true; }
                else if (string.Equals(a, "/NODATA", StringComparison.OrdinalIgnoreCase)) { o.InstallData = false; }
                else if (string.Equals(a, "/NODESKTOP", StringComparison.OrdinalIgnoreCase)) { o.Desktop = false; }
                else if (string.Equals(a, "/NOSTARTMENU", StringComparison.OrdinalIgnoreCase)) { o.StartMenu = false; }
                else if (string.Equals(a, "/AUTOSTART", StringComparison.OrdinalIgnoreCase)) { o.AutoStart = true; }
                else if (string.Equals(a, "/LAUNCH", StringComparison.OrdinalIgnoreCase)) { o.Launch = true; }
            }
            return o;
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    //  9. 入口
    // ═══════════════════════════════════════════════════════════════════════
    internal static class Program
    {
        [STAThread]
        private static void Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            // 静默安装：/S ... （见文件末尾的 SilentSetup）
            // 这条路径必须完全不碰 UI，出错只用退出码和日志汇报，
            // 因为调用方（自动化脚本、打包流水线）看不到窗口也点不了按钮。
            if (SilentSetup.IsSilent(args))
            {
                Environment.ExitCode = SilentSetup.Run(args);
                return;
            }

            // /Quiet：仍然显示窗口，但不弹任何确认框，并在窗口画好后自动开始安装
            QuietOptions quiet = QuietOptions.Parse(args);

            try
            {
                SetupForm form = new SetupForm();
                if (quiet.HasAnyOption) { form.ApplyCommandLine(quiet); }
                Application.Run(form);
                // /Quiet 也会自动安装并自动关闭，顺手给出退出码：0 成功 / 1 失败
                if (quiet.Quiet) { Environment.ExitCode = form.Finished ? 0 : 1; }
            }
            catch (Exception ex)
            {
                MessageBox.Show("安装程序发生未处理的错误：\r\n\r\n" + ex.ToString(),
                    "Sequoia-X 安装程序", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }
    }
}
