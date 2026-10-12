<#
.SYNOPSIS
    用系统自带 csc.exe 编译 Sequoia-X 安装程序的三个 exe，并把 payload.zip 追加到安装器末尾。

.DESCRIPTION
    产物：
        out\Launcher.exe             启动器
        out\Uninstaller.exe          卸载器
        out\setup-stub.exe           只有安装器本体（未追加负载），供编译验证
        out\Sequoia-X-Setup.exe      最终安装包 = setup-stub.exe + payload.zip 原始字节

    不依赖网络、Inno Setup、NSIS、7-Zip。只用到 .NET Framework 4.x 自带的 csc.exe。

.PARAMETER PayloadZip
    负载 zip 的路径。省略则只编译，不追加负载（仅产出 setup-stub.exe）。

.PARAMETER OutDir
    输出目录，默认 installer\dist。

.EXAMPLE
    .\build.ps1 -PayloadZip "D:\path\payload.zip" -OutDir "D:\path\out"

.EXAMPLE
    .\build.ps1
#>
[CmdletBinding()]
param(
    [string]$PayloadZip,
    [string]$OutDir
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

# ═══════════════════════════════════════════════════════════════════════════
#  0. 常量与路径
# ═══════════════════════════════════════════════════════════════════════════

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
if ([string]::IsNullOrWhiteSpace($ScriptDir)) { $ScriptDir = (Get-Location).Path }

if ([string]::IsNullOrWhiteSpace($OutDir)) {
    $OutDir = Join-Path $ScriptDir 'dist'
}

$Csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$FrameworkDir = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319'

# 编译期引用的程序集（按需求显式列出）
$References = @(
    'System.dll'
    'System.Core.dll'
    'System.Windows.Forms.dll'
    'System.Drawing.dll'
    'System.IO.Compression.dll'
    'System.IO.Compression.FileSystem.dll'
    'Microsoft.CSharp.dll'
)

# 每个 exe 的：源文件、输出名、条件编译符号
$Targets = @(
    [pscustomobject]@{
        Name    = 'Launcher.exe'
        Source  = Join-Path $ScriptDir 'Launcher.cs'
        Define  = 'LAUNCHER'
        Desc    = '应用启动器'
    }
    [pscustomobject]@{
        Name    = 'Uninstaller.exe'
        Source  = Join-Path $ScriptDir 'Uninstaller.cs'
        Define  = 'UNINSTALLER'
        Desc    = '卸载器'
    }
    [pscustomobject]@{
        Name    = 'setup-stub.exe'
        Source  = Join-Path $ScriptDir 'Setup.cs'
        Define  = 'SETUP'
        Desc    = '安装器本体（未追加负载）'
    }
)

$AssemblyInfo = Join-Path $ScriptDir 'AssemblyInfo.g.cs'
$SetupStubName = 'setup-stub.exe'
$FinalSetupName = 'Sequoia-X-Setup.exe'

function Write-Step {
    param([string]$Text)
    Write-Host ''
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Text)
    Write-Host "    $Text" -ForegroundColor Green
}

# ═══════════════════════════════════════════════════════════════════════════
#  1. 前置检查
# ═══════════════════════════════════════════════════════════════════════════

Write-Step '检查编译环境'

if (-not (Test-Path -LiteralPath $Csc)) {
    throw "找不到 C# 编译器：$Csc"
}
$cscVer = (Get-Item -LiteralPath $Csc).VersionInfo.FileVersion
Write-Ok "csc.exe           : $Csc ($cscVer)"

foreach ($src in @('Setup.cs', 'Launcher.cs', 'Uninstaller.cs', 'AssemblyInfo.g.cs')) {
    $p = Join-Path $ScriptDir $src
    if (-not (Test-Path -LiteralPath $p)) { throw "缺少源文件：$p" }
}
Write-Ok "源文件            : Setup.cs / Launcher.cs / Uninstaller.cs / AssemblyInfo.g.cs"

if (-not (Test-Path -LiteralPath $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
}
$OutDir = (Resolve-Path -LiteralPath $OutDir).Path
Write-Ok "输出目录          : $OutDir"

# ═══════════════════════════════════════════════════════════════════════════
#  2. 编译
# ═══════════════════════════════════════════════════════════════════════════

Write-Step '编译三个 exe（/target:winexe /platform:anycpu /optimize+）'

$refArgs = @()
foreach ($r in $References) {
    $refArgs += ('/r:' + (Join-Path $FrameworkDir $r))
}

$built = @()

foreach ($t in $Targets) {
    $outPath = Join-Path $OutDir $t.Name
    if (Test-Path -LiteralPath $outPath) { Remove-Item -LiteralPath $outPath -Force }

    # 参数顺序：选项在前，源文件在后（csc 约定）
    $argList = @(
        '/nologo'
        '/target:winexe'
        '/platform:anycpu'
        '/optimize+'
        '/warn:4'
        ('/define:' + $t.Define)
        ('/out:' + $outPath)
    ) + $refArgs + @(
        (Join-Path $ScriptDir 'AssemblyInfo.g.cs')
        $t.Source
    )

    Write-Host ("    编译 {0} ..." -f $t.Name) -NoNewline
    $output = & $Csc $argList 2>&1
    $code = $LASTEXITCODE

    if ($output) {
        Write-Host ''
        $output | ForEach-Object { Write-Host "      $_" }
    } else {
        Write-Host ' 完成'
    }

    if ($code -ne 0) {
        throw "编译失败（退出码 $code）：$($t.Name)"
    }
    if ($output) {
        # 出现任何输出（正常应为空）即视为有警告，按需求零警告处理
        $warnCount = @($output | Where-Object { $_ -match 'warning' }).Count
        if ($warnCount -gt 0) { throw "编译产生 $warnCount 条警告，要求零警告：$($t.Name)" }
    }

    $item = Get-Item -LiteralPath $outPath
    Write-Ok ("{0,-20} {1,10:N0} 字节" -f $item.Name, $item.Length)
    $built += $item
}

# ═══════════════════════════════════════════════════════════════════════════
#  3. 追加负载
# ═══════════════════════════════════════════════════════════════════════════

$stubPath = Join-Path $OutDir $SetupStubName

if ([string]::IsNullOrWhiteSpace($PayloadZip)) {
    Write-Step '未指定 -PayloadZip：只编译，不追加负载'
    Write-Ok "已产出 $SetupStubName（可用于验证编译）"
    Write-Host ''
    Write-Host '完成。' -ForegroundColor Green
    Write-Host ''
    Write-Host '产物清单：'
    foreach ($f in $built) {
        Write-Host ("  {0}  ({1:N0} 字节)" -f $f.FullName, $f.Length)
    }
    return
}

if (-not (Test-Path -LiteralPath $PayloadZip)) {
    throw "找不到负载文件：$PayloadZip"
}
$payloadItem = Get-Item -LiteralPath $PayloadZip
if ($payloadItem.Length -le 0) { throw "负载文件为空：$PayloadZip" }

# 顺手校验 zips 完整性（读得到中央目录即可）
try {
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
    $probe = [System.IO.Compression.ZipFile]::OpenRead($payloadItem.FullName)
    $entryCount = $probe.Entries.Count
    $probe.Dispose()
    Write-Ok ("负载 zip 可读      : {0} 个条目" -f $entryCount)
} catch {
    throw "负载不是有效的 zip（$($payloadItem.FullName)）：$($_.Exception.Message)"
}

Write-Step '把负载追加到安装器末尾'

$finalPath = Join-Path $OutDir $FinalSetupName
if (Test-Path -LiteralPath $finalPath) { Remove-Item -LiteralPath $finalPath -Force }

# 先复制 stub，再追加原始字节 —— 不覆盖 stub 本身
Copy-Item -LiteralPath $stubPath -Destination $finalPath -Force

$stubSize = (Get-Item -LiteralPath $finalPath).Length

$in = [System.IO.File]::Open($payloadItem.FullName, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
$out = [System.IO.File]::Open($finalPath, [System.IO.FileMode]::Append, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
try {
    $buffer = New-Object byte[] (1024 * 1024)
    $total = 0L
    while (($read = $in.Read($buffer, 0, $buffer.Length)) -gt 0) {
        $out.Write($buffer, 0, $read)
        $total += $read
        if ($payloadItem.Length -gt (8 * 1024 * 1024)) {
            $pct = [int](100 * $total / $payloadItem.Length)
            Write-Progress -Activity '追加负载' -Status ("{0:N1} / {1:N1} MB" -f ($total / 1MB), ($payloadItem.Length / 1MB)) -PercentComplete $pct
        }
    }
    $out.Flush()
} finally {
    $out.Dispose()
    $in.Dispose()
    Write-Progress -Activity '追加负载' -Completed
}

$finalItem = Get-Item -LiteralPath $finalPath
$expected = $stubSize + $payloadItem.Length
if ($finalItem.Length -ne $expected) {
    throw "追加结果大小异常：期望 $expected 字节，实际 $($finalItem.Length) 字节"
}
Write-Ok ("stub {0:N0} 字节 + payload {1:N0} 字节 = {2:N0} 字节" -f $stubSize, $payloadItem.Length, $finalItem.Length)

# ═══════════════════════════════════════════════════════════════════════════
#  4. 汇总
# ═══════════════════════════════════════════════════════════════════════════

Write-Step '完成'

$rows = @()
foreach ($f in $built) {
    $rows += [pscustomobject]@{ 文件 = $f.FullName; 字节 = $f.Length }
}
$rows += [pscustomobject]@{ 文件 = $finalItem.FullName; 字节 = $finalItem.Length }

$rows | Format-Table -AutoSize | Out-String | Write-Host
Write-Host ("安装包：{0}" -f $finalItem.FullName) -ForegroundColor Green
