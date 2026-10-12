<#
.SYNOPSIS
    验证 Sequoia-X 安装包的「追加式负载」定位、前缀过滤与解压逻辑。

.DESCRIPTION
    这不会运行 GUI 安装器，而是：
      1. 编译一个探针程序（Probe.cs 与 Setup.cs / Launcher.cs 一起编译），
         通过反射直接驱动生产代码里的 PayloadLocator / EntryFilter / ServiceControl。
      2. 造一个小的假 payload（含 payload.json、runtime/、app/、以及顶层
         Launcher.exe / Uninstaller.exe），用 build.ps1 追加到安装器末尾。
      3. 校验：
         · 能从追加了 zip 的 exe 里正确定位到 zip 起始偏移
           （与 [System.IO.Compression.ZipFile]::OpenRead 直接打开 payload.zip 的条目列表逐条比对）
         · 未追加负载的 stub 返回 -1（不崩）
         · 前缀过滤：has_data 为 false 时不会去解 app/data/sequoia_v2.db；
           即使 has_data 为 true，用户不勾选时同样跳过
         · 解压出来的每个文件字节大小与源文件一致
         · Launcher 的 netstat 解析能取到正确的 PID

.PARAMETER SkipBuild
    跳过编译探针（复用上一次的探针 exe，加快重复验证）。

.PARAMETER KeepTemp
    保留临时工作目录，便于人工排查。

.EXAMPLE
    .\verify-payload.ps1
#>
[CmdletBinding()]
param(
    [string]$OutDir,
    [switch]$SkipBuild,
    [switch]$KeepTemp
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
if ([string]::IsNullOrWhiteSpace($ScriptDir)) { $ScriptDir = (Get-Location).Path }
if ([string]::IsNullOrWhiteSpace($OutDir)) { $OutDir = Join-Path $ScriptDir 'dist' }

$Csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$FrameworkDir = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319'
$VerifyDir = Join-Path $ScriptDir 'verify'
$ProbeSrc = Join-Path $VerifyDir 'Probe.cs'
$AssemblyInfo = Join-Path $ScriptDir 'AssemblyInfo.g.cs'
$BuildPs1 = Join-Path $ScriptDir 'build.ps1'

# ── 测试结果收集 ────────────────────────────────────────────────────────────
$script:Results = @()

function Add-Result {
    param([string]$Name, [bool]$Ok, [string]$Detail)
    $script:Results += [pscustomobject]@{ 检查项 = $Name; 结果 = ($(if ($Ok) { 'PASS' } else { 'FAIL' })); 说明 = $Detail }
    $color = if ($Ok) { 'Green' } else { 'Red' }
    Write-Host ("  [{0}] {1}" -f $(if ($Ok) { 'PASS' } else { 'FAIL' }), $Name) -ForegroundColor $color
    if (-not [string]::IsNullOrWhiteSpace($Detail)) { Write-Host ("         $Detail") -ForegroundColor DarkGray }
}

function Write-Step2 {
    param([string]$Text)
    Write-Host ''
    Write-Host "==> $Text" -ForegroundColor Cyan
}

# ═══════════════════════════════════════════════════════════════════════════
#  0. 准备临时目录
# ═══════════════════════════════════════════════════════════════════════════
$Work = Join-Path $env:TEMP ('sequoia-verify-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $Work -Force | Out-Null
Write-Host "临时工作目录：$Work" -ForegroundColor DarkGray

Add-Type -AssemblyName System.IO.Compression -ErrorAction SilentlyContinue
Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue

try {

# ═══════════════════════════════════════════════════════════════════════════
#  1. 编译探针
# ═══════════════════════════════════════════════════════════════════════════
Write-Step2 '编译验证探针（链接生产源码）'

$SetupProbe = Join-Path $Work 'SetupProbe.exe'
$OtherProbe = Join-Path $Work 'OtherProbe.exe'

if (-not $SkipBuild) {
    if (-not (Test-Path -LiteralPath $ProbeSrc)) { throw "缺少探针源码：$ProbeSrc" }

    $refArgs = @(
        ('/r:' + (Join-Path $FrameworkDir 'System.dll'))
        ('/r:' + (Join-Path $FrameworkDir 'System.Core.dll'))
        ('/r:' + (Join-Path $FrameworkDir 'System.Windows.Forms.dll'))
        ('/r:' + (Join-Path $FrameworkDir 'System.Drawing.dll'))
        ('/r:' + (Join-Path $FrameworkDir 'System.IO.Compression.dll'))
        ('/r:' + (Join-Path $FrameworkDir 'System.IO.Compression.FileSystem.dll'))
        ('/r:' + (Join-Path $FrameworkDir 'Microsoft.CSharp.dll'))
    )

    # 生产代码里的 Form 控件字段没有全部被使用，链接进探针时会出现“字段未使用”类警告，
    # 用 /warn:0 关掉（这些警告与 build.ps1 的正式编译无关）。
    # 注意：PowerShell 里 ',' 与 '+' 混用会被解析成数组拼接
    # （@('/out:' + $x, $y) 实际得到 3 个元素），因此每处都用括号把 '+' 先算完。
    $common = @('/nologo', '/target:exe', '/platform:anycpu', '/warn:0', '/main:Probe')

    Write-Host '    编译 SetupProbe.exe（Setup.cs + Probe.cs）...' -NoNewline
    $o1 = & $Csc ($common + $refArgs + @(('/out:' + $SetupProbe), $AssemblyInfo, (Join-Path $ScriptDir 'Setup.cs'), $ProbeSrc)) 2>&1
    if ($LASTEXITCODE -ne 0) { $o1 | ForEach-Object { Write-Host $_ }; throw '探针编译失败：SetupProbe.exe' }
    Write-Host ' 完成'

    Write-Host '    编译 OtherProbe.exe（Launcher.cs + Uninstaller.cs + Probe.cs）...' -NoNewline
    $o2 = & $Csc ($common + $refArgs + @(('/out:' + $OtherProbe), $AssemblyInfo, (Join-Path $ScriptDir 'Launcher.cs'), (Join-Path $ScriptDir 'Uninstaller.cs'), $ProbeSrc)) 2>&1
    if ($LASTEXITCODE -ne 0) { $o2 | ForEach-Object { Write-Host $_ }; throw '探针编译失败：OtherProbe.exe' }
    Write-Host ' 完成'
} else {
    Write-Host '    已跳过（-SkipBuild）'
}
Add-Result '编译验证探针' ((Test-Path $SetupProbe) -and (Test-Path $OtherProbe)) 'SetupProbe.exe / OtherProbe.exe'

# ═══════════════════════════════════════════════════════════════════════════
#  2. 造两个假 payload
#     A: has_data=false，没有 app/data/sequoia_v2.db
#     B: has_data=true，有 app/data/sequoia_v2.db（用可压缩的假数据）
# ═══════════════════════════════════════════════════════════════════════════
Write-Step2 '构造假 payload（模拟真实包的目录结构）'

$launcherExe = Join-Path $OutDir 'Launcher.exe'
$uninstallerExe = Join-Path $OutDir 'Uninstaller.exe'
if (-not (Test-Path -LiteralPath $launcherExe) -or -not (Test-Path -LiteralPath $uninstallerExe)) {
    # 先编译一次（不带 -PayloadZip），产出 Launcher.exe / Uninstaller.exe / setup-stub.exe
    Write-Host "    未找到 $OutDir 下的 exe，先运行 build.ps1 编译..." -ForegroundColor DarkGray
    & $BuildPs1 -OutDir $OutDir | Out-Null
}
if (-not (Test-Path -LiteralPath $launcherExe) -or -not (Test-Path -LiteralPath $uninstallerExe)) {
    throw "编译后仍找不到 Launcher.exe / Uninstaller.exe（$OutDir）"
}

# 内容源：内存里生成，避免大量小文件落盘
function New-FillerBytes {
    param([int]$Size, [int]$Seed)
    $bytes = New-Object byte[] $Size
    $rnd = New-Object System.Random $Seed
    $rnd.NextBytes($bytes)
    return ,$bytes
}

function Add-FileEntry {
    param($Zip, [string]$EntryName, [string]$SourcePath)
    $entry = $Zip.CreateEntry($EntryName, [System.IO.Compression.CompressionLevel]::Optimal)
    $src = [System.IO.File]::OpenRead($SourcePath)
    try {
        $dst = $entry.Open()
        try {
            $buf = New-Object byte[] 65536
            while (($n = $src.Read($buf, 0, $buf.Length)) -gt 0) { $dst.Write($buf, 0, $n) }
        } finally { $dst.Dispose() }
    } finally { $src.Dispose() }
}

function Add-TextEntry {
    param($Zip, [string]$EntryName, [string]$Text)
    $entry = $Zip.CreateEntry($EntryName, [System.IO.Compression.CompressionLevel]::Optimal)
    $sw = New-Object System.IO.StreamWriter($entry.Open(), (New-Object System.Text.UTF8Encoding($false)))
    try { $sw.Write($Text) } finally { $sw.Dispose() }
}

function New-PayloadZip {
    param(
        [string]$ZipPath,
        [bool]$WithData,
        [int]$DataRepeat
    )

    if (Test-Path -LiteralPath $ZipPath) { Remove-Item -LiteralPath $ZipPath -Force }

    $fs = [System.IO.File]::Open($ZipPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write)
    $zip = New-Object System.IO.Compression.ZipArchive($fs, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        # payload.json（zip 根目录）
        if ($WithData) { $hasData = 'true' } else { $hasData = 'false' }
        $meta = '{"app": "Sequoia-X", "version": "2.0.0", "built_at": "2026-10-12 20:30:00",' +
                (' "port": 8899, "has_data": {0}, "data_size_mb": 493.9,' -f $hasData) +
                ' "runtime_size_mb": 210.0, "app_size_mb": 1.0}'
        $e = $zip.CreateEntry('payload.json', [System.IO.Compression.CompressionLevel]::Optimal)
        $sw = New-Object System.IO.StreamWriter($e.Open(), (New-Object System.Text.UTF8Encoding($false)))
        $sw.Write($meta)
        $sw.Dispose()

        # 顶层：Launcher.exe / Uninstaller.exe（真的 exe，由 build.ps1 产出）
        Add-FileEntry -Zip $zip -EntryName 'Launcher.exe' -SourcePath $launcherExe
        Add-FileEntry -Zip $zip -EntryName 'Uninstaller.exe' -SourcePath $uninstallerExe

        # runtime/python.exe —— 几 KB 的假文件
        $e = $zip.CreateEntry('runtime/python.exe', [System.IO.Compression.CompressionLevel]::Optimal)
        $s = $e.Open()
        $b = New-FillerBytes -Size 65536 -Seed 1
        $s.Write($b, 0, $b.Length)
        $b = New-FillerBytes -Size 131072 -Seed 2
        $s.Write($b, 0, $b.Length)
        $s.Dispose()

        # runtime/pythonw.exe / python312.dll / DLLs / Lib
        foreach ($name in @('runtime/pythonw.exe', 'runtime/python312.dll', 'runtime/DLLs/_socket.pyd', 'runtime/Lib/os.py')) {
            $e = $zip.CreateEntry($name, [System.IO.Compression.CompressionLevel]::Optimal)
            $s = $e.Open()
            $b = New-FillerBytes -Size 20000 -Seed ($name.Length)
            $s.Write($b, 0, $b.Length)
            $s.Dispose()
        }

        # app 源码
        $e = $zip.CreateEntry('app/main.py', [System.IO.Compression.CompressionLevel]::Optimal)
        $sw = New-Object System.IO.StreamWriter($e.Open(), (New-Object System.Text.UTF8Encoding($false)))
        $sw.Write("# Sequoia-X 入口（假负载）`nprint('hello 中文')`n")
        $sw.Dispose()

        $e = $zip.CreateEntry('app/sequoia_x/core/config.py', [System.IO.Compression.CompressionLevel]::Optimal)
        $sw = New-Object System.IO.StreamWriter($e.Open(), (New-Object System.Text.UTF8Encoding($false)))
        $sw.Write("# 配置模块（假负载）`nPORT = 8899`n")
        $sw.Dispose()

        # app/.env（含 WEBUI_PORT 与空的 FEISHU_WEBHOOK_URL）
        $envText = "# Sequoia-X 环境变量（假负载）`nDB_PATH=data/sequoia_v2.db`nWEBUI_PORT=8899`nFEISHU_WEBHOOK_URL=`n"
        $e = $zip.CreateEntry('app/.env', [System.IO.Compression.CompressionLevel]::Optimal)
        $sw = New-Object System.IO.StreamWriter($e.Open(), (New-Object System.Text.UTF8Encoding($false)))
        $sw.Write($envText)
        $sw.Dispose()

        # 带中文名的条目，验证条目名编码
        $e = $zip.CreateEntry('app/README-首次使用.txt', [System.IO.Compression.CompressionLevel]::Optimal)
        $sw = New-Object System.IO.StreamWriter($e.Open(), (New-Object System.Text.UTF8Encoding($false)))
        $sw.Write("首次使用说明（假负载）`n")
        $sw.Dispose()

        # 可选的大数据库条目
        if ($WithData) {
            $e = $zip.CreateEntry('app/data/sequoia_v2.db', [System.IO.Compression.CompressionLevel]::Optimal)
            $s = $e.Open()
            for ($i = 0; $i -lt $DataRepeat; $i++) {
                $b = New-FillerBytes -Size 1048576 -Seed (100 + $i)
                $s.Write($b, 0, $b.Length)
            }
            $s.Dispose()
        }
    } finally {
        $zip.Dispose()
        $fs.Dispose()
    }
    return (Get-Item -LiteralPath $ZipPath).Length
}

$payloadA = Join-Path $Work 'payload-nodata.zip'
$payloadB = Join-Path $Work 'payload-withdata.zip'
$sizeA = New-PayloadZip -ZipPath $payloadA -WithData $false -DataRepeat 0
$sizeB = New-PayloadZip -ZipPath $payloadB -WithData $true -DataRepeat 12

Write-Host ("    payload-nodata.zip   {0:N0} 字节" -f $sizeA) -ForegroundColor DarkGray
Write-Host ("    payload-withdata.zip {0:N0} 字节" -f $sizeB) -ForegroundColor DarkGray

$zipA = [System.IO.Compression.ZipFile]::OpenRead($payloadA)
$entriesA = @($zipA.Entries | ForEach-Object { $_.FullName })
$sizesA = @{}
foreach ($en in $zipA.Entries) { $sizesA[$en.FullName] = $en.Length }
$zipA.Dispose()

$zipB = [System.IO.Compression.ZipFile]::OpenRead($payloadB)
$entriesB = @($zipB.Entries | ForEach-Object { $_.FullName })
$sizesB = @{}
foreach ($en in $zipB.Entries) { $sizesB[$en.FullName] = $en.Length }
$zipB.Dispose()

Add-Result '假 payload 创建成功' (($sizeA -gt 0) -and ($sizeB -gt 0)) `
    ("A: {0} 个条目 / B: {1} 个条目" -f $entriesA.Count, $entriesB.Count)

# ═══════════════════════════════════════════════════════════════════════════
#  3. 用 build.ps1 追加负载，产出 Sequoia-X-Setup.exe
# ═══════════════════════════════════════════════════════════════════════════
Write-Step2 '用 build.ps1 追加负载生成安装包'

$buildOut = Join-Path $Work 'build-out'
& $BuildPs1 -PayloadZip $payloadA -OutDir $buildOut | Out-Host

$setupA = Join-Path $buildOut 'Sequoia-X-Setup.exe'
$stubA = Join-Path $buildOut 'setup-stub.exe'
Add-Result 'build.ps1 产出 Sequoia-X-Setup.exe' (Test-Path -LiteralPath $setupA) $setupA
Add-Result 'setup-stub.exe 未被覆盖' (Test-Path -LiteralPath $stubA) $stubA

$stubSize = (Get-Item -LiteralPath $stubA).Length
$setupSize = (Get-Item -LiteralPath $setupA).Length
Add-Result '安装包大小 = stub + payload' ($setupSize -eq ($stubSize + $sizeA)) `
    ("{0:N0} + {1:N0} = {2:N0}（实际 {3:N0}）" -f $stubSize, $sizeA, ($stubSize + $sizeA), $setupSize)

# ═══════════════════════════════════════════════════════════════════════════
#  4. 校验：zip 定位 + 条目列表一致性
# ═══════════════════════════════════════════════════════════════════════════
Write-Step2 '校验 zip 定位与条目列表一致性（A：无行情数据）'

$extractA = Join-Path $Work 'extract-A'
$probeJson = & $SetupProbe setup $setupA $extractA
if ($LASTEXITCODE -ne 0) { throw "探针执行失败：$probeJson" }
$reportA = $probeJson | ConvertFrom-Json

Add-Result '在追加了 zip 的 exe 内定位到 zip' ($reportA.found -and $reportA.offset -gt 0) `
    ("offset = {0}（exe 大小 {1}）" -f $reportA.offset, $reportA.exe_size)

Add-Result 'offset + payload 大小 = exe 大小' (($reportA.offset + $sizeA) -eq $reportA.exe_size) `
    ("{0} + {1} = {2}" -f $reportA.offset, $sizeA, $reportA.exe_size)

$probeEntries = @($reportA.entries)
$sameCount = ($probeEntries.Count -eq $entriesA.Count)
$missing = @($entriesA | Where-Object { $probeEntries -notcontains $_ })
$extra = @($probeEntries | Where-Object { $entriesA -notcontains $_ })
Add-Result '条目列表与 ZipFile::OpenRead 完全一致' ($sameCount -and $missing.Count -eq 0 -and $extra.Count -eq 0) `
    ("探针 {0} 条 / 直读 {1} 条；缺失 {2} 条，多出 {3} 条" -f $probeEntries.Count, $entriesA.Count, $missing.Count, $extra.Count)

# ═══════════════════════════════════════════════════════════════════════════
#  5. 校验：前缀过滤（无行情数据时不碰 app/data/sequoia_v2.db）
# ═══════════════════════════════════════════════════════════════════════════
Write-Step2 '校验前缀过滤逻辑'

Add-Result 'A 包 has_data=false（探针判定）' ($reportA.big_data -eq $false) 'zip 内不含 app/data/sequoia_v2.db'
Add-Result 'A 包不会解 app/data/sequoia_v2.db' `
    (@($reportA.will_extract) -notcontains 'app/data/sequoia_v2.db') `
    ("计划解压 {0} 条" -f @($reportA.will_extract).Count)
Add-Result 'A 包解压后不存在 app/data/sequoia_v2.db' (-not (Test-Path -LiteralPath (Join-Path $extractA 'app\data\sequoia_v2.db'))) $extractA

# 解压结果逐条比对大小
# 注意：解压出来的 rel 用 Windows 反斜杠（\\），而源 zip 的条目名用正斜杠（/），
# 比对前必须统一分隔符，否则会误报"不在源 zip 条目里"。
$badSize = @()
$extractRels = @()
foreach ($item in @($reportA.extracted)) {
    $rel = ([string]$item.rel).Replace('\', '/')
    $extractRels += $rel
    if ($sizesA.ContainsKey($rel)) {
        if ([long]$item.size -ne [long]$sizesA[$rel]) { $badSize += ("{0}: {1} != {2}" -f $rel, $item.size, $sizesA[$rel]) }
    } else {
        $badSize += ("{0}: 不在源 zip 条目里" -f $rel)
    }
}
Add-Result 'A 包解压出的文件字节数与源一致' ($badSize.Count -eq 0) `
    ("比对 {0} 个文件；不一致 {1} 个" -f @($reportA.extracted).Count, $badSize.Count)
if ($badSize.Count -gt 0) { $badSize | ForEach-Object { Write-Host "         $_" -ForegroundColor DarkYellow } }

# 解压出的条目集合应与「计划解压」集合一致
$diffExtract = @($extractRels | Where-Object { @($reportA.will_extract) -notcontains $_ })
Add-Result '实际解压集合与计划解压集合一致' ($diffExtract.Count -eq 0 -and $extractRels.Count -eq @($reportA.will_extract).Count) `
    ("实际 {0} 条 / 计划 {1} 条" -f $extractRels.Count, @($reportA.will_extract).Count)

# 中文条目正确落盘
Add-Result '中文名条目正确落盘' (Test-Path -LiteralPath (Join-Path $extractA 'app\README-首次使用.txt')) `
    'app\README-首次使用.txt'
Add-Result 'app\.env 正确落盘' (Test-Path -LiteralPath (Join-Path $extractA 'app\.env')) 'app\.env'

# ═══════════════════════════════════════════════════════════════════════════
#  5b. 校验：驱动真实安装流程（不显示窗口）
#      —— 实例化真的 SetupForm，调用它的 ExtractAll / WriteInstallJson /
#         WriteAutoRun / WriteUninstallEntry，等于「真的点了一次安装」。
# ═══════════════════════════════════════════════════════════════════════════
Write-Step2 '驱动真实安装流程（SetupForm 实例，不显示窗口）'

$installDir = Join-Path $Work 'installed'
$formProbe = & $SetupProbe setupform $setupA $installDir false
if ($LASTEXITCODE -ne 0) { throw "安装流程探针执行失败：$formProbe" }
$formReport = $formProbe | ConvertFrom-Json

Add-Result '安装流程返回 ok' ($formReport.ok -eq $true) `
    ("install_dir = {0}" -f $formReport.install_dir)

$installedFiles = @($formReport.files | ForEach-Object { ($_ -split '\|')[0] })
$expectedTop = @('Launcher.exe', 'Uninstaller.exe', 'install.json', 'payload.json',
                 'runtime\python.exe', 'runtime\pythonw.exe', 'runtime\python312.dll',
                 'runtime\DLLs\_socket.pyd', 'runtime\Lib\os.py',
                 'app\main.py', 'app\.env', 'app\README-首次使用.txt',
                 'app\sequoia_x\core\config.py')
$missingFiles = @($expectedTop | Where-Object { $installedFiles -notcontains $_ })
Add-Result '安装目录布局与约定一致' ($missingFiles.Count -eq 0) `
    ("已安装 {0} 个文件；缺失 {1} 个{2}" -f $installedFiles.Count, $missingFiles.Count,
     $(if ($missingFiles.Count -gt 0) { '：' + ($missingFiles -join ', ') } else { '' }))

Add-Result '写入了 install.json' `
    ((Test-Path -LiteralPath (Join-Path $installDir 'install.json')) -and
     ($formReport.install_json -match '"install_dir"') -and
     ($formReport.install_json -match '"installed_at"') -and
     ($formReport.install_json -match '"version"')) `
    $formReport.install_json

Add-Result 'install.json 里的 install_dir 正确' `
    ($formReport.install_json -match [regex]::Escape(($installDir -replace '\\', '\\'))) `
    $formReport.install_json

Add-Result '未勾选行情数据时没有解压 db' `
    (-not (Test-Path -LiteralPath (Join-Path $installDir 'app\data\sequoia_v2.db'))) `
    'app\data\sequoia_v2.db 不存在'

Add-Result 'app\data 与 app\logs 目录已创建' `
    ((Test-Path -LiteralPath (Join-Path $installDir 'app\data')) -and
     (Test-Path -LiteralPath (Join-Path $installDir 'app\logs'))) `
    '确保首次启动不会缺目录'

Add-Result '.lnk 快捷方式可被创建（IShellLinkW）' ($formReport.lnk_created -eq $true) `
    ("lnk 大小 = {0} 字节" -f $formReport.lnk_size)

# 注册表：卸载信息（用 PowerShell 直接查，验证真的写进去了）
$unReg = Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Sequoia-X' -ErrorAction SilentlyContinue
Add-Result '注册表卸载项已写入' ($null -ne $unReg) `
    $(if ($unReg) { ("DisplayName={0} / Version={1} / Publisher={2}" -f $unReg.DisplayName, $unReg.DisplayVersion, $unReg.Publisher) } else { '未找到 HKCU\...\Uninstall\Sequoia-X' })

if ($unReg) {
    $regOk = ($unReg.DisplayName -eq 'Sequoia-X 量化选股') -and
             ($unReg.DisplayVersion -eq '2.0.0') -and
             ($unReg.Publisher -eq 'Sequoia-X') -and
             ($unReg.InstallLocation -eq $installDir) -and
             ($unReg.DisplayIcon -eq (Join-Path $installDir 'Launcher.exe')) -and
             ($unReg.UninstallString -eq ('"' + (Join-Path $installDir 'Uninstaller.exe') + '"')) -and
             ([int]$unReg.NoModify -eq 1) -and ([int]$unReg.NoRepair -eq 1) -and
             ([int]$unReg.EstimatedSize -gt 0)
    Add-Result '注册表卸载项的 9 个值都符合约定' $regOk `
        ("InstallLocation={0} / EstimatedSize={1} KB / UninstallString={2}" -f $unReg.InstallLocation, $unReg.EstimatedSize, $unReg.UninstallString)
}

# WriteAutoRun 在未勾选自启时应清掉 Run 项
$runVal = (Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'Sequoia-X' -ErrorAction SilentlyContinue)
Add-Result '未勾选开机自启时不写 Run 项' ($null -eq $runVal) `
    $(if ($runVal) { $runVal.'Sequoia-X' } else { 'HKCU\...\Run\Sequoia-X 不存在（正确）' })

# 清理探针写入的注册表（避免污染这台机器）
try { Remove-Item -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Sequoia-X' -Recurse -Force -ErrorAction SilentlyContinue } catch { }
try { Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'Sequoia-X' -ErrorAction SilentlyContinue } catch { }

# ═══════════════════════════════════════════════════════════════════════════
#  6. 校验：stub（未追加负载）返回 -1 而不是崩溃
# ═══════════════════════════════════════════════════════════════════════════
Write-Step2 '校验未追加负载的 stub'

$stubProbe = & $SetupProbe stub $stubA
$stubReport = $stubProbe | ConvertFrom-Json
Add-Result 'stub 定位结果 = -1（走「负载缺失」提示分支）' ([long]$stubReport.offset -eq -1) `
    ("offset = {0}" -f $stubReport.offset)

# ═══════════════════════════════════════════════════════════════════════════
#  7. 校验：B 包（has_data=true）时大条目按勾选决定是否解压
# ═══════════════════════════════════════════════════════════════════════════
Write-Step2 '校验 has_data=true 的包（勾选 / 不勾选两条路径）'

$buildOutB = Join-Path $Work 'build-out-b'
& $BuildPs1 -PayloadZip $payloadB -OutDir $buildOutB | Out-Host
$setupB = Join-Path $buildOutB 'Sequoia-X-Setup.exe'
Add-Result 'B 包构建成功' (Test-Path -LiteralPath $setupB) $setupB

# B 包不勾选行情数据（探针里的 installData 由 has_data 决定，这里直接看 will_extract/skipped）
$extractB = Join-Path $Work 'extract-B'
$probeJsonB = & $SetupProbe setup $setupB $extractB
$reportB = $probeJsonB | ConvertFrom-Json

Add-Result 'B 包探测到 app/data/sequoia_v2.db' ($reportB.big_data -eq $true) `
    ("install_data = {0}" -f $reportB.install_data)
Add-Result 'B 包条目列表与直读一致' (@($reportB.entries).Count -eq $entriesB.Count) `
    ("探针 {0} 条 / 直读 {1} 条" -f @($reportB.entries).Count, $entriesB.Count)

# 勾选（install_data=true）：大条目应被解压，且大小正确
Add-Result '勾选行情数据时解压大条目' (Test-Path -LiteralPath (Join-Path $extractB 'app\data\sequoia_v2.db')) `
    ("expected size = {0:N0}" -f $sizesB['app/data/sequoia_v2.db'])
$actualBig = 0
if (Test-Path -LiteralPath (Join-Path $extractB 'app\data\sequoia_v2.db')) {
    $actualBig = (Get-Item -LiteralPath (Join-Path $extractB 'app\data\sequoia_v2.db')).Length
}
Add-Result '大条目解压字节数正确' ([long]$actualBig -eq [long]$sizesB['app/data/sequoia_v2.db']) `
    ("实际 {0:N0} / 期望 {1:N0}" -f $actualBig, $sizesB['app/data/sequoia_v2.db'])

# B 包（含行情数据）再走一遍「真实安装流程」，验证大条目按勾选落盘
$installDirB = Join-Path $Work 'installed-b'
$formProbeB = & $SetupProbe setupform $setupB $installDirB true
$formReportB = $formProbeB | ConvertFrom-Json
$dbB = Join-Path $installDirB 'app\data\sequoia_v2.db'
Add-Result '真实安装流程（勾选行情数据）解压出 db' (Test-Path -LiteralPath $dbB) `
    ("db 大小 = {0:N0}（期望 {1:N0}）" -f $(if (Test-Path $dbB) { (Get-Item $dbB).Length } else { 0 }), $sizesB['app/data/sequoia_v2.db'])
Add-Result '真实安装流程解压的 db 字节数正确' `
    ((Test-Path -LiteralPath $dbB) -and ((Get-Item $dbB).Length -eq [long]$sizesB['app/data/sequoia_v2.db'])) `
    ''
# 这一轮 setupform 又会写入卸载注册表项，用完清掉，避免污染本机
try { Remove-Item -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Sequoia-X' -Recurse -Force -ErrorAction SilentlyContinue } catch { }

# 不勾选：强制 installData=false，复核大条目被跳过（等价于安装器里取消勾选）
$filterOffJson = & $SetupProbe filter $setupB false
$filterOff = $filterOffJson | ConvertFrom-Json
Add-Result 'B 包取消勾选时跳过 app/data/sequoia_v2.db' `
    ((@($filterOff.will_extract) -notcontains 'app/data/sequoia_v2.db') -and (@($filterOff.skipped) -contains 'app/data/sequoia_v2.db')) `
    ("install_data = {0}；计划解压 {1} 条，跳过 {2} 条" -f $filterOff.install_data, @($filterOff.will_extract).Count, @($filterOff.skipped).Count)

$filterOnJson = & $SetupProbe filter $setupB true
$filterOn = $filterOnJson | ConvertFrom-Json
Add-Result 'B 包勾选时解压 app/data/sequoia_v2.db' `
    (@($filterOn.will_extract) -contains 'app/data/sequoia_v2.db') `
    ("install_data = {0}；计划解压 {1} 条" -f $filterOn.install_data, @($filterOn.will_extract).Count)

$filterAOn = (& $SetupProbe filter $setupA true) | ConvertFrom-Json
Add-Result 'A 包即使强制勾选也不会解压不存在的条目' `
    (@($filterAOn.will_extract) -notcontains 'app/data/sequoia_v2.db') `
    'A 包里本就没有该条目'

# ═══════════════════════════════════════════════════════════════════════════
#  8. 校验：Launcher 的 netstat 解析
# ═══════════════════════════════════════════════════════════════════════════
Write-Step2 '校验 Launcher 的 netstat 解析'

$sample = @'
活动连接

  协议  本地地址          外部地址        状态           PID
  TCP    127.0.0.1:8899         0.0.0.0:0              LISTENING       24680
  TCP    127.0.0.1:8899         127.0.0.1:51234        ESTABLISHED     24680
  TCP    127.0.0.1:9000         0.0.0.0:0              LISTENING       11111
  TCP    0.0.0.0:8899           0.0.0.0:0              LISTENING       24680
  TCP    [::]:135                [::]:0                 LISTENING       900
'@
$netstatFile = Join-Path $Work 'netstat-sample.txt'
[System.IO.File]::WriteAllText($netstatFile, $sample, (New-Object System.Text.UTF8Encoding($false)))

$netJson = & $OtherProbe launcher $netstatFile 8899
if ($LASTEXITCODE -ne 0) { throw "netstat 探针执行失败：$netJson" }
$netReport = $netJson | ConvertFrom-Json
Add-Result 'netstat 解析取到 127.0.0.1:8899 的 PID' ([int]$netReport.pid -eq 24680) `
    ("解析结果 pid = {0}，期望 24680" -f $netReport.pid)

$netJson2 = & $OtherProbe launcher $netstatFile 9000
$netReport2 = $netJson2 | ConvertFrom-Json
Add-Result 'netstat 解析取到 127.0.0.1:9000 的 PID' ([int]$netReport2.pid -eq 11111) `
    ("解析结果 pid = {0}，期望 11111" -f $netReport2.pid)

$netJson3 = & $OtherProbe launcher $netstatFile 19888
$netReport3 = $netJson3 | ConvertFrom-Json
Add-Result 'netstat 解析对未监听端口返回 0' ([int]$netReport3.pid -eq 0) `
    ("解析结果 pid = {0}，期望 0" -f $netReport3.pid)

# ═══════════════════════════════════════════════════════════════════════════
#  9. 校验：exe 版本信息
# ═══════════════════════════════════════════════════════════════════════════
Write-Step2 '校验三个 exe 的版本信息（FileDescription / ProductName）'

foreach ($pair in @(
    @{ File = (Join-Path $OutDir 'Launcher.exe');    Desc = '启动器' },
    @{ File = (Join-Path $OutDir 'Uninstaller.exe'); Desc = '卸载器' },
    @{ File = (Join-Path $OutDir 'setup-stub.exe');  Desc = '安装器' }
)) {
    if (-not (Test-Path -LiteralPath $pair.File)) {
        Add-Result ('版本信息：' + (Split-Path -Leaf $pair.File)) $false '文件不存在'
        continue
    }
    $vi = (Get-Item -LiteralPath $pair.File).VersionInfo
    $ok = (-not [string]::IsNullOrWhiteSpace($vi.FileDescription)) -and
          (-not [string]::IsNullOrWhiteSpace($vi.ProductName)) -and
          (-not [string]::IsNullOrWhiteSpace($vi.FileVersion)) -and
          ($vi.ProductName -eq 'Sequoia-X')
    Add-Result ('版本信息：' + (Split-Path -Leaf $pair.File)) $ok `
        ("Product={0} / Desc={1} / Ver={2} / Company={3}" -f $vi.ProductName, $vi.FileDescription, $vi.FileVersion, $vi.CompanyName)
}

# ═══════════════════════════════════════════════════════════════════════════
#  10. 汇总
# ═══════════════════════════════════════════════════════════════════════════
Write-Step2 '验证汇总'

$script:Results | Format-Table -AutoSize | Out-String | Write-Host

$failCount = @($script:Results | Where-Object { $_.结果 -eq 'FAIL' }).Count
$passCount = @($script:Results | Where-Object { $_.结果 -eq 'PASS' }).Count

Write-Host ''
if ($failCount -eq 0) {
    Write-Host ("全部通过：{0} 项" -f $passCount) -ForegroundColor Green
} else {
    Write-Host ("通过 {0} 项，失败 {1} 项" -f $passCount, $failCount) -ForegroundColor Red
}

# 报告落盘
$reportPath = Join-Path $ScriptDir 'verify-report.txt'
$lines = @()
$lines += "Sequoia-X 安装包验证报告"
$lines += ("生成时间：{0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
$lines += ("安装包 A（has_data=false）：{0}  （{1:N0} 字节）" -f $setupA, $setupSize)
$lines += ("安装包 B（has_data=true） ：{0}" -f $setupB)
$lines += ""
$lines += ($script:Results | Format-Table -AutoSize | Out-String)
$lines += ("通过 {0} 项，失败 {1} 项" -f $passCount, $failCount)
[System.IO.File]::WriteAllLines($reportPath, $lines, (New-Object System.Text.UTF8Encoding($false)))
Write-Host ("报告已写入：{0}" -f $reportPath) -ForegroundColor DarkGray

if ($failCount -gt 0) { exit 1 }

} finally {
    if (-not $KeepTemp) {
        try { Remove-Item -LiteralPath $Work -Recurse -Force -ErrorAction SilentlyContinue } catch { }
    } else {
        Write-Host ("临时目录已保留：{0}" -f $Work) -ForegroundColor DarkGray
    }
}
