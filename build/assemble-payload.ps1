<#
.SYNOPSIS
  组装 Sequoia-X 安装包的负载目录（不打包，只铺目录）。

.DESCRIPTION
  把「便携 Python 运行时 + 全部依赖 + 应用代码 + 默认配置 + （可选）预装行情数据」
  铺到一个目录里。之所以先铺目录再压缩，是因为 Launcher.exe / Uninstaller.exe
  要等 C# 那边编译好才能放进来，压缩一次很贵，所以最后才压。

用法：
    .\assemble-payload.ps1                          # 默认：不含行情数据
    .\assemble-payload.ps1 -SkipData                # 同上（显式写法）
    .\assemble-payload.ps1 -OutDir D:\x             # 换输出目录
    .\assemble-payload.ps1 -PortablePython D:\py    # 指定便携 Python 位置

产物：build\out\payload\ 目录
    runtime\            便携 Python 3.12 + site-packages
    app\                应用代码 + .env +（可选）data\sequoia_v2.db
    payload.json        元数据（安装器和压缩脚本都会读）

关于 -PortablePython：打包需要一个**可重定位的独立 CPython**（不能是 venv，
因为 venv 里的 python.exe 依赖创建时的绝对路径，换台机器就废了）。
本机默认用之前装好的那份；换机器打包时用这个参数指过去。
#>
[CmdletBinding()]
param(
    [string]$Workspace      = (Split-Path -Parent $PSScriptRoot),
    [string]$OutDir         = (Join-Path $PSScriptRoot "out\payload"),
    [string]$PortablePython = (Join-Path $env:LOCALAPPDATA "Programs\sequoia-tools\pythonroot\python"),
    [switch]$SkipData
)

$ErrorActionPreference = "Stop"

# build\ 就在仓库根目录下，所以 $Workspace 就是仓库本身
$Repo     = $Workspace
$Portable = $PortablePython
$VenvSP   = Join-Path $Repo ".venv\Lib\site-packages"
$AppDb    = Join-Path $Repo "data\sequoia_v2.db"

function Say($msg)  { Write-Host "  $msg" }
function Head($msg) { Write-Host ""; Write-Host "== $msg ==" -ForegroundColor Cyan }
function MB($path)  {
    if (-not (Test-Path $path)) { return 0.0 }
    $sum = (Get-ChildItem $path -Recurse -Force -File -ErrorAction SilentlyContinue |
            Measure-Object Length -Sum).Sum
    if (-not $sum) { return 0.0 }
    return [math]::Round($sum / 1MB, 1)
}

# robocopy 的退出码：0~7 都算成功（1=复制了文件，2=有额外文件，3=两者都有…）
function Robo($src, $dst, [string[]]$extra) {
    $args = @($src, $dst, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/R:1", "/W:1") + $extra
    & robocopy @args | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy 失败（退出码 $LASTEXITCODE）：$src -> $dst" }
}

Head "检查前置条件"
foreach ($p in @($Repo, $VenvSP)) {
    if (-not (Test-Path $p)) { throw "找不到：$p" }
    Say "OK  $p"
}
if (-not (Test-Path $Portable)) {
    throw @"
找不到便携 Python：$Portable

打包需要一个**可重定位的独立 CPython**（不能是 .venv，venv 里的解释器依赖创建时的
绝对路径，搬到别人机器上就废了）。两种拿法：

  1. 官方 embeddable 包（推荐，约 11MB）
     https://www.python.org/downloads/windows/  →  "Windows embeddable package (64-bit)"
     解压到任意目录，然后用 -PortablePython 指过去：
         .\assemble-payload.ps1 -PortablePython D:\python-3.12.15-embed

  2. 直接复制一份完整的 Python 安装目录（没有 pyvenv.cfg 的那种）

用 -PortablePython 指定即可，例如：
    .\assemble-payload.ps1 -PortablePython "C:\Python312"
"@
}
Say "OK  $Portable"
$hasData = (-not $SkipData) -and (Test-Path $AppDb)
Say "预装行情数据：$(if ($hasData) { '是' } else { '否（跳过或文件不存在）' })"

Head "准备输出目录"
if (Test-Path $OutDir) { Remove-Item $OutDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
Say "输出到 $OutDir"

# ── 1. 便携 Python 运行时 ────────────────────────────────────────
Head "1/5 便携 Python 运行时"
$rt = Join-Path $OutDir "runtime"
Robo $Portable $rt @()

# 裁剪：调试符号、测试套件、只在开发时用得上的东西
$junk = @(
    "Lib\test", "Lib\idlelib", "Lib\lib2to3", "Lib\turtledemo",
    "Lib\distutils", "Lib\site-packages\pip\_vendor\distlib\*.exe",
    "include", "libs"
)
foreach ($j in $junk) {
    $p = Join-Path $rt $j
    if (Test-Path $p) { Remove-Item $p -Recurse -Force -ErrorAction SilentlyContinue }
}
Get-ChildItem $rt -Recurse -Force -Include *.pdb -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue
Say ("裁掉调试符号与测试套件后：{0} MB" -f (MB $rt))
$runtimeOnlyMB = MB $rt      # 先记下来：下面拷进 site-packages 之后这个数就不准了

# 验证运行时能跑
$py = Join-Path $rt "python.exe"
$ver = & $py -c "import sys;print(sys.version.split()[0])" 2>&1
if ($LASTEXITCODE -ne 0) { throw "便携 Python 无法运行：$ver" }
Say "运行时自检通过：Python $ver"

# ── 2. 第三方依赖 ────────────────────────────────────────────────
Head "2/5 第三方依赖（site-packages）"
$sp = Join-Path $rt "Lib\site-packages"
# __pycache__ 有 69MB，而且都是可以根据 .py 重新生成的，不带走
Robo $VenvSP $sp @("/XD", "__pycache__", ".pytest_cache", "/XF", "*.pyc", "*.pyo")
Say ("site-packages: {0} MB" -f (MB $sp))

# 依赖自检：这是整个安装包最关键的一步——装到别人机器上必须 import 得起来
$probe = @"
import sys
mods = ['pandas', 'numpy', 'requests', 'pydantic', 'pydantic_settings',
        'dotenv', 'rich', 'akshare', 'baostock']
bad = []
for m in mods:
    try:
        __import__(m)
    except Exception as e:
        bad.append(f'{m}: {type(e).__name__}: {e}')
if bad:
    print('IMPORT-FAILED:' + ' | '.join(bad)); sys.exit(1)
print('all-ok:' + ','.join(mods))
"@
$probeFile = Join-Path $env:TEMP "seqx-probe.py"
Set-Content -Path $probeFile -Value $probe -Encoding UTF8
$out = & $py $probeFile 2>&1
if ($LASTEXITCODE -ne 0) { throw "依赖自检失败：`n$out" }
Say "依赖自检通过：$out"
Remove-Item $probeFile -Force -ErrorAction SilentlyContinue

# ── 3. 应用代码 ──────────────────────────────────────────────────
Head "3/5 应用代码"
$app = Join-Path $OutDir "app"
New-Item -ItemType Directory -Force -Path $app | Out-Null
foreach ($item in @("main.py", "pyproject.toml", "README.md", ".env.example")) {
    $src = Join-Path $Repo $item
    if (Test-Path $src) { Copy-Item $src $app -Force }
}
foreach ($dir in @("sequoia_x", "webui", "tests")) {
    $src = Join-Path $Repo $dir
    if (Test-Path $src) { Robo $src (Join-Path $app $dir) @("/XD", "__pycache__", "/XF", "*.pyc") }
}
Say ("app 代码：{0} MB" -f (MB $app))

# ── 4. 默认配置 + 首次使用说明 ───────────────────────────────────
Head "4/5 默认配置与说明"

# .env 刻意用 UTF-8 无 BOM：python-dotenv 能读，但带 BOM 时第一个键名会多一个字符
$envText = @'
# ===== Sequoia-X 环境变量配置（由安装包生成）=====
# 建议不要直接改这个文件，而是用 Web 工作台的「设置」页改 —— 那边有校验、
# 有「测试推送」按钮，改完立即生效。

# 数据库路径（相对 app\ 目录）
DB_PATH=data/sequoia_v2.db

# 历史起始日期（回填从这里开始）
START_DATE=2024-01-01

# ===== 行情数据源 =====
# auto       = 自动探测并按下面的顺序换源（推荐）
# eastmoney  = 东方财富 / tencent = 腾讯 / sina = 新浪 / baostock = 项目原设计
# ⚠️ 不同源的后复权基准不同，同一只股票绝不能跨源拼接。程序会把每只股票用的源
#    记在 symbol_source 表里，增量更新只认这个源。
DATA_SOURCE=auto
AUTO_SOURCE_ORDER=eastmoney,sina,tencent
REBASE_STALE_DAYS=1

# 回填并发线程数与请求间隔（实测 4 线程无间隔会把东方财富打进硬限流）
AK_WORKERS=3
AK_DELAY=0.3

# ===== 飞书推送 =====
# ⚠️ 第一次使用请到 Web 工作台的「设置」页填写，那边可以「测试推送」验证。
#    留空 = 不推送（选股结果照样会在工作台里显示）。
FEISHU_WEBHOOK_URL=

# 策略专属 Webhook（可选，留空走上面的主通道）
# 可用键：ma_volume / turtle / flag / shakeout / limit_down / rps / private_placement
# STRATEGY_WEBHOOK_MA_VOLUME=
# STRATEGY_WEBHOOK_TURTLE=
# STRATEGY_WEBHOOK_FLAG=
# STRATEGY_WEBHOOK_SHAKEOUT=
# STRATEGY_WEBHOOK_LIMIT_DOWN=
# STRATEGY_WEBHOOK_RPS=
# STRATEGY_WEBHOOK_PRIVATE_PLACEMENT=

# ===== Web 工作台 =====
# 端口（只监听 127.0.0.1，仅本机可访问）
WEBUI_PORT=8899
'@
[System.IO.File]::WriteAllText((Join-Path $app ".env"), $envText, (New-Object System.Text.UTF8Encoding($false)))
Say ".env 已写入（UTF-8 无 BOM）"

$readme = @'
========================================================
 Sequoia-X 量化选股 · 首次使用说明
========================================================

【怎么启动】
  双击桌面上的「Sequoia-X 工作台」图标（或开始菜单里的同名图标）。
  它会启动后台服务，然后自动帮你打开浏览器。
  界面上有「打开工作台」「查看日志」「停止服务并退出」三个按钮。

  不想用了：点启动器窗口的「停止服务并退出」，或者右键任务栏图标关闭。
  从「开始菜单 → Sequoia-X → 卸载 Sequoia-X」可以卸载。

【第一次要做的事】
  1. 配飞书推送（可选）
     工作台 → 设置 → 飞书推送 → 填「主 Webhook 地址」→ 点「测试推送」。
     地址从飞书群里拿：群设置 → 群机器人 → 添加机器人 → 自定义机器人。
     不填也能用，只是不往飞书发消息，选股结果在「选股结果」页看。

  2. 确认行情数据
     如果安装时勾选了「安装预装的历史行情数据」，那已经装好了，
     打开「总览」页应该能看到 5000 多只股票。
     如果没勾，去 设置 → 数据操作 → 点「补齐全市场缺口」，
     大约需要 1.5 小时（可以关掉浏览器，后台会继续跑）。

【每天会自动做什么】
  **第一次启动工作台之后**，程序会自动登记两个 Windows 计划任务：
    - 每个交易日 16:00  自动回填当天的最新行情数据
    - 每个交易日 19:15  自动跑全部策略并把结果推送到飞书
  可以在 工作台 -> 设置 -> 计划任务 里看到状态，也能在那里启用/禁用/立即运行。
  （安装时勾了「开机自动启动」的话工作台会常驻；没勾就自己双击图标启动一次 ——
    任务登记好之后就独立运行了，之后工作台开没开都不影响。）

【数据放哪里】
  都在安装目录的 app\data\ 下面：sequoia_v2.db 是行情库，
  想备份直接复制这个文件；设置 → 数据库与缓存 → 「备份数据库」也可以一键备份。

【想改端口 / 换数据源 / 调并发】
  工作台 → 设置 → 数据源与回填参数。改完点保存立即生效（端口要重启才生效）。

【出问题了】
  1. 打开启动器的「查看日志」，或者看 app\logs\ 下的文件
  2. 端口被占用：设置里换个端口，或先关掉占用 8899 的程序
  3. 杀毒软件误报：本程序没有数字签名，部分杀软会拦截，
     把安装目录加入白名单即可
========================================================
'@
[System.IO.File]::WriteAllText((Join-Path $app "README-首次使用.txt"), $readme, (New-Object System.Text.UTF8Encoding($false)))
Say "README-首次使用.txt 已写入"

if ($hasData) {
    Head "预装行情数据"
    $dataDir = Join-Path $app "data"
    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
    $dst = Join-Path $dataDir "sequoia_v2.db"
    Say "复制 $AppDb -> $dst（494MB，稍等…）"
    Copy-Item $AppDb $dst -Force
    Say ("data: {0} MB" -f (MB $dataDir))
}

# 名称表和信号缓存也一起带上（都很小，但让「装完第一次打开」就有内容）：
#  - name_cache.json   股票代码→中文名。不带的话首次启动要联网拉，而这台机器实测
#                      会因为 82.push2 被墙卡住（服务器已经改成后台预热不会阻塞启动了，
#                      但能离线直接用更好）
#  - signals_cache.json 上次算出来的信号。带上后「选股结果」页一打开就有数据，
#                      页面上会显示「生成时间」，不会误导
Head "附带的缓存文件"
$dataDir = Join-Path $app "data"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null   # 没装行情数据时也要有
foreach ($cache in @("name_cache.json", "signals_cache.json")) {
    $src = Join-Path $Repo "data\$cache"
    if (Test-Path $src) {
        Copy-Item $src (Join-Path $app "data\$cache") -Force
        Say "$cache -> app\data\（$([math]::Round((Get-Item $src).Length/1KB,1)) KB）"
    } else {
        Say "$cache 不存在，跳过（不影响）"
    }
}

# ── 5. 元数据 ────────────────────────────────────────────────────
Head "5/5 元数据 payload.json"
$dbSize = if ($hasData) { [math]::Round((Get-Item $AppDb).Length / 1MB, 1) } else { 0 }
$meta = [ordered]@{
    app              = "Sequoia-X"
    version          = "2.0.0"
    built_at         = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    port             = 8899
    has_data         = [bool]$hasData
    data_size_mb     = $dbSize
    runtime_size_mb  = $runtimeOnlyMB      # 只算 Python 本体，不含 site-packages
    deps_size_mb     = (MB $sp)
    app_size_mb      = (MB $app)
}
$json = $meta | ConvertTo-Json -Depth 3
[System.IO.File]::WriteAllText((Join-Path $OutDir "payload.json"), $json, (New-Object System.Text.UTF8Encoding($false)))
Say $json

Head "完成"
Say ("负载目录总大小：{0} MB" -f (MB $OutDir))
Say "注意：Launcher.exe / Uninstaller.exe 还没放进来，等 C# 那边编译好再补。"
