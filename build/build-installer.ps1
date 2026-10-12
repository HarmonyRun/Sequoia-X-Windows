<#
.SYNOPSIS
  一键构建 Sequoia-X 的 Windows 安装包。

.DESCRIPTION
  完整流程：
    1. 组装负载目录（便携 Python + 依赖 + 应用 + 配置 +（可选）行情数据）
    2. 编译 C# 的 Launcher.exe / Uninstaller.exe 并拷进负载根目录
    3. 把负载压成 payload.zip（deflate 9）
    4. 用 installer\build.ps1 把 zip 追加到 setup-stub.exe 后面，
       产出最终的 Sequoia-X-Setup.exe

用法：
    .\build-installer.ps1                  # 默认：**不含**行情数据（约 110 MB，装完自己回填）
    .\build-installer.ps1 -WithData        # 带上预装行情数据（约 204 MB，省去首次 1.5 小时回填）
    .\build-installer.ps1 -SkipAssemble    # 负载目录已经铺好了，直接从第 2 步开始

产物：
    installer-payload\            负载目录（中间产物，可删）
    installer\payload.zip         压缩后的负载（中间产物，可删）
    installer\dist\Sequoia-X-Setup.exe   ← 最终安装包
#>
[CmdletBinding()]
param(
    [string]$Workspace = (Split-Path -Parent $PSScriptRoot),
    [switch]$WithData,
    [switch]$SkipAssemble
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

function Head($m) { Write-Host ""; Write-Host "===== $m =====" -ForegroundColor Cyan }
function Say($m)  { Write-Host "  $m" }

$BuildDir    = $PSScriptRoot
$InstallerDir= Join-Path $Workspace "installer"
$PayloadDir  = Join-Path $BuildDir "out\payload"
$PayloadZip  = Join-Path $BuildDir "out\payload.zip"
$Py          = Join-Path $Workspace ".venv\Scripts\python.exe"

$t0 = Get-Date

# ── 1. 组装负载 ──────────────────────────────────────────────────
if (-not $SkipAssemble) {
    Head "1/4 组装负载目录"
    $asm = Join-Path $BuildDir "assemble-payload.ps1"
    # 注意：调用 .ps1 之后不要看 $LASTEXITCODE —— 它留着的是脚本内部最后一次原生
    # 调用的退出码（robocopy 成功时返回 1~7），会被误判成失败。
    # assemble-payload.ps1 内部是 $ErrorActionPreference=Stop + throw，真失败会抛上来。
    if ($WithData) { & $asm -OutDir $PayloadDir } else { & $asm -OutDir $PayloadDir -SkipData }
} else {
    Head "1/4 跳过组装（-SkipAssemble）"
    if (-not (Test-Path $PayloadDir)) { throw "负载目录不存在：$PayloadDir" }
}

# ── 2. 编译 C# 并拷进负载 ────────────────────────────────────────
Head "2/4 编译启动器与卸载器"
$buildPs1 = Join-Path $InstallerDir "build.ps1"
if (-not (Test-Path $buildPs1)) { throw "找不到安装器构建脚本：$buildPs1" }
& $buildPs1 -OutDir (Join-Path $InstallerDir "dist")
if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "编译 C# 失败" }

foreach ($exe in @("Launcher.exe", "Uninstaller.exe")) {
    $src = Join-Path $InstallerDir "dist\$exe"
    if (-not (Test-Path $src)) { throw "编译产物缺失：$src" }
    Copy-Item $src $PayloadDir -Force
    Say "$exe -> 负载根目录（$((Get-Item $src).Length) 字节）"
}

# ── 3. 压缩 ──────────────────────────────────────────────────────
Head "3/4 压缩负载（deflate 9，几百 MB 要几分钟）"
& $Py (Join-Path $BuildDir "make-payload-zip.py") $PayloadDir $PayloadZip
if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "压缩失败" }

# ── 4. 生成最终安装包 ────────────────────────────────────────────
Head "4/4 生成安装包（把 zip 追加到 setup-stub.exe 后面）"
& $buildPs1 -PayloadZip $PayloadZip -OutDir (Join-Path $InstallerDir "dist")
if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "生成安装包失败" }

Head "完成"
$setup = Join-Path $InstallerDir "dist\Sequoia-X-Setup.exe"
if (Test-Path $setup) {
    $sz = (Get-Item $setup).Length
    Say ("安装包：$setup")
    Say ("大小  ：{0:N1} MB" -f ($sz / 1MB))
} else {
    Say "⚠️ 没有找到 Sequoia-X-Setup.exe，检查 build.ps1 的输出名"
    Get-ChildItem (Join-Path $InstallerDir "dist") | Select-Object Name, Length | Format-Table -Auto
}
Say ("总耗时：{0:N1} 分钟" -f ((Get-Date) - $t0).TotalMinutes)
