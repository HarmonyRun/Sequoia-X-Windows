// ═══════════════════════════════════════════════════════════════════════════
//  程序集元信息（三个 exe 共用）
//  ---------------------------------------------------------------------------
//  编译时由 build.ps1 用 /define:SETUP / /define:LAUNCHER / /define:UNINSTALLER
//  切换标题与说明，保证每个 exe 在「属性 → 详细信息」里都有正确的
//  FileDescription / ProductName / FileVersion。
//
//  注意：本文件必须是 UTF-8 with BOM，否则 csc 会按 GBK 解析中文乱码。
// ═══════════════════════════════════════════════════════════════════════════

using System.Reflection;
using System.Runtime.InteropServices;

[assembly: AssemblyVersion("2.0.0.0")]
[assembly: AssemblyFileVersion("2.0.0.0")]
[assembly: AssemblyInformationalVersion("2.0.0")]
[assembly: AssemblyConfiguration("Release")]
[assembly: ComVisible(false)]

[assembly: AssemblyProduct("Sequoia-X")]
[assembly: AssemblyCompany("Sequoia-X")]
[assembly: AssemblyCopyright("Copyright (C) 2026 Sequoia-X")]
[assembly: AssemblyTrademark("")]

#if SETUP
[assembly: AssemblyTitle("Sequoia-X 安装程序")]
[assembly: AssemblyDescription("Sequoia-X 量化选股工作台安装程序")]
#elif LAUNCHER
[assembly: AssemblyTitle("Sequoia-X 工作台启动器")]
[assembly: AssemblyDescription("Sequoia-X 量化选股工作台启动器")]
#elif UNINSTALLER
[assembly: AssemblyTitle("Sequoia-X 卸载程序")]
[assembly: AssemblyDescription("Sequoia-X 量化选股工作台卸载程序")]
#else
[assembly: AssemblyTitle("Sequoia-X")]
[assembly: AssemblyDescription("Sequoia-X 量化选股工作台")]
#endif
