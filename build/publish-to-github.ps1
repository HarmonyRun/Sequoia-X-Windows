<#
.SYNOPSIS
  把本地这个 Sequoia-X 二次开发分支发布到你的 GitHub（带 fork 关系 + Release 安装包）。

.DESCRIPTION
  流程：
    1. 用 token 验证身份（顺便拿到用户名和 id）
    2. 通过 API **fork** 上游 sngyai/Sequoia-X —— 这样 GitHub 上才有
       「forked from sngyai/Sequoia-X」的关系，而不是一个凭空新建的仓库
    3. 把 fork 改名成 Sequoia-X-Windows（改名不影响 fork 关系）
    4. 推送本地分支为 main，并设为默认分支
    5. 建一个 Release，把 80.4 MB 的安装包挂成附件
    6. 逐项验证（fork 关系、README、Release 附件能不能下载）

  用法：
      .\build\publish-to-github.ps1 -DryRun     # 只检查不改动，先把要做的事列出来
      .\build\publish-to-github.ps1             # 真正执行

  ⚠️ token 从哪里来：
      创建 Personal Access Token（classic 勾 repo 权限，或 fine-grained 勾
      Contents + Administration 的读写），然后**设为用户级环境变量**：

          [Environment]::SetEnvironmentVariable('GITHUB_TOKEN','ghp_xxx','User')

      设完**新开一个窗口**再跑这个脚本。脚本只在内存里用这个 token，
      不写进任何文件、不打印出来。

  ⚠️ 关于 ghproxy：
      本机全局 git 配置里有一条
          url.https://ghproxy.net/https://github.com/.insteadof = https://github.com/
      它会把所有 github.com 的地址重写成 ghproxy.net。**如果直接 push，
      token 就会被发给那个第三方代理。** 所以脚本会先临时摘掉这条规则，
      推送完再原样加回去。
#>
[CmdletBinding()]
param(
    [string]$Workspace  = (Split-Path -Parent $PSScriptRoot),
    [string]$RepoName   = "Sequoia-X-Windows",
    [string]$Branch     = "main",
    [string]$Upstream   = "sngyai/Sequoia-X",
    [string]$Tag        = "v2.0.0-windows.1",
    [string]$SetupExe   = "",
    [switch]$DryRun,
    [switch]$Force          # 目标分支已存在且不是快进时，才需要它
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"

function Head($m) { Write-Host ""; Write-Host "===== $m =====" -ForegroundColor Cyan }
function Say($m)  { Write-Host "  $m" }
function Ok($m)   { Write-Host "  ✓ $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  ⚠ $m" -ForegroundColor Yellow }
# 退出前一定要把 ghproxy 规则恢复回去（Die 用的是 exit，不会走 trap/finally）
function Die($m)  {
    if (Get-Command Restore-GhproxyRule -EA SilentlyContinue) { Restore-GhproxyRule }
    Write-Host ""
    Write-Host "✗ $m" -ForegroundColor Red
    exit 1
}

$RepoDir = $Workspace   # 脚本在 <仓库>\build 下，所以 Workspace 就是仓库本身
if (-not (Test-Path $RepoDir)) { Die "找不到仓库目录：$RepoDir" }
$Git = (Get-Command git -EA SilentlyContinue).Source
if (-not $Git) { $Git = "$env:LOCALAPPDATA\Programs\sequoia-tools\mingit\cmd\git.exe" }
if (-not (Test-Path $Git)) { Die "找不到 git" }
if (-not $SetupExe) { $SetupExe = Join-Path $RepoDir "installer\dist\Sequoia-X-Setup.exe" }

# ── ghproxy 规则的摘除与恢复 ──
# 这台机器的全局 git 配置会把 github.com 重写成 ghproxy.net；带着这条规则 push，
# token 就会被发给那个第三方代理。所以推送前必须摘掉、之后恢复。
# ⚠️ Die 走的是 exit，不会触发 trap/finally，所以恢复动作必须显式写进 Die 里——
#    第一次跑的时候就因为改名 403 退出，把这条规则留在了「已摘除」状态。
$script:GhproxyRule = "url.https://ghproxy.net/https://github.com/.insteadof"
$script:GhproxyPrev = @()
$script:GhproxyRemoved = $false

function Restore-GhproxyRule {
    if (-not $script:GhproxyRemoved) { return }
    foreach ($v in $script:GhproxyPrev) { & $Git config --global --add $script:GhproxyRule $v }
    $script:GhproxyRemoved = $false
    Write-Host "  ✓ 已恢复 ghproxy 重写规则" -ForegroundColor DarkGray
}

# ══════════════════════════════════════════════════════════════════
#  0. 读 token
# ══════════════════════════════════════════════════════════════════
Head "0/7 读取 GitHub token"
$Token = [Environment]::GetEnvironmentVariable("GITHUB_TOKEN", "Process")
if (-not $Token) { $Token = [Environment]::GetEnvironmentVariable("GITHUB_TOKEN", "User") }
if (-not $Token) {
    Die @"
没有找到 GITHUB_TOKEN。

请先创建一个 Personal Access Token：
    https://github.com/settings/tokens   （classic，勾 `repo` 权限）
然后设为用户级环境变量：

    [Environment]::SetEnvironmentVariable('GITHUB_TOKEN','你的token','User')

设完**新开一个 PowerShell 窗口**再运行本脚本。
"@
}
Ok "已读到 token（长度 $($Token.Length)，前 4 位 $($Token.Substring(0,4))…，不会打印完整值）"

$Headers = @{
    Authorization          = "Bearer $Token"
    Accept                 = "application/vnd.github+json"
    "X-GitHub-Api-Version" = "2022-11-28"
    "User-Agent"           = "sequoia-x-publish"
}

# ══════════════════════════════════════════════════════════════════
#  1. 验证身份
# ══════════════════════════════════════════════════════════════════
Head "1/7 验证 token"
try {
    $me = Invoke-RestMethod -Uri "https://api.github.com/user" -Headers $Headers -TimeoutSec 30
} catch {
    Die "token 无效或网络不通：$($_.Exception.Message)"
}
$Owner = $me.login
$OwnerId = $me.id
$Noreply = "$OwnerId+$Owner@users.noreply.github.com"   # GitHub 官方认可的匿名邮箱
Ok "身份：$Owner（id=$OwnerId）"
Ok "提交署名将用：$Owner <$Noreply>"

# ══════════════════════════════════════════════════════════════════
#  2. 处理 ghproxy 重写规则（关键安全步骤）
# ══════════════════════════════════════════════════════════════════
Head "2/7 检查 git 的 URL 重写规则"
$script:GhproxyPrev = @(& $Git config --global --get-all $script:GhproxyRule 2>$null)
if ($script:GhproxyPrev.Count -gt 0 -and $script:GhproxyPrev[0]) {
    Warn "发现重写规则：$($script:GhproxyRule) = $($script:GhproxyPrev -join ', ')"
    Warn "如果不摘掉，push 时会把 token 发给 ghproxy.net 这个第三方代理"
    if (-not $DryRun) {
        & $Git config --global --unset-all $script:GhproxyRule
        $script:GhproxyRemoved = $true
        Ok "已临时摘掉（推送结束后会原样恢复）"
    } else {
        Say "（DryRun：不会真的摘掉）"
    }
} else {
    Ok "没有 ghproxy 重写规则"
}

# 后面任何一步抛错（终止性异常）也要把规则恢复回去。
# 注意 Die 用的是 exit，不走这里，所以 Die 自己也调了 Restore-GhproxyRule。
trap {
    Restore-GhproxyRule
    exit 1
}

# ══════════════════════════════════════════════════════════════════
#  3. fork 上游
# ══════════════════════════════════════════════════════════════════
Head "3/7 fork 上游仓库（建立 fork 关系）"
$forkApi = "https://api.github.com/repos/$Upstream/forks"
$existing = $null
try {
    $existing = Invoke-RestMethod -Uri "https://api.github.com/repos/$Owner/Sequoia-X" -Headers $Headers -TimeoutSec 30
} catch { }

if ($existing -and $existing.fork) {
    Ok "你已经 fork 过了：$($existing.full_name)（parent=$($existing.parent.full_name)）"
} else {
    Say "正在 fork $Upstream …"
    if ($DryRun) {
        Say "（DryRun：跳过）"
    } else {
        $body = @{ name = "Sequoia-X"; default_branch_only = $false } | ConvertTo-Json
        try {
            $fork = Invoke-RestMethod -Uri $forkApi -Method Post -Headers $Headers -Body $body `
                -ContentType "application/json" -TimeoutSec 120
            Ok "已 fork：$($fork.full_name)"
        } catch {
            Die "fork 失败：$($_.Exception.Message)"
        }
        Start-Sleep -Seconds 5    # GitHub 建 fork 是异步的
    }
}

# ══════════════════════════════════════════════════════════════════
#  4. 改名 + 推送
# ══════════════════════════════════════════════════════════════════
Head "4/7 设置仓库名与推送分支"
$target = $null
try {
    $target = Invoke-RestMethod -Uri "https://api.github.com/repos/$Owner/$RepoName" -Headers $Headers -TimeoutSec 30
} catch { }

if ($target) {
    Ok "仓库已存在：$($target.full_name)"
    if (-not $target.fork) {
        Warn "注意：这个仓库**不是** fork，fork 关系可能不对"
    } else {
        Ok "fork 关系：parent = $($target.parent.full_name)"
    }
} else {
    Say "把 Sequoia-X 改名为 $RepoName …"
    if ($DryRun) { Say "（DryRun：跳过）" }
    else {
        try {
            $body = @{ name = $RepoName } | ConvertTo-Json
            $renamed = Invoke-RestMethod -Uri "https://api.github.com/repos/$Owner/Sequoia-X" `
                -Method Patch -Headers $Headers -Body $body -ContentType "application/json" -TimeoutSec 60
            Ok "已改名：$($renamed.full_name)（fork=$($renamed.fork)，parent=$($renamed.parent.full_name)）"
        } catch {
            # fine-grained token 没有 Administration 权限时会 403。
            # 改名纯属美观问题，不该因此中断整个发布——继续用现有的仓库名。
            Warn "改名失败：$($_.Exception.Message)"
            Warn "（fine-grained token 需要「Administration: Read and write」才能改名）"
            Warn "先按现有名字 ***** 发布，你之后可以在 GitHub 网页上手动改名："
            Warn "    Settings → General → Repository name → 改成 $RepoName"
            Warn "    改名不影响 fork 关系，Release 和附件都会跟着走（旧链接自动跳转）"
            $RepoName = "Sequoia-X"
        }
    }
}

$pushUrl = "https://github.com/$Owner/$RepoName.git"
Say "推送目标：$pushUrl"

# 本机直连 github.com:443 **很不稳定**（实测经常 "Could not connect to server" /
# "Empty reply from server"，而 api.github.com 却一直是通的）。
# 注意：ghproxy 那条 insteadOf 规则对 push 帮不上忙——它只是把地址换个域名，
# 而 push 必须打到真正的 github.com（否则 token 就给了第三方）。
# 所以这里探一下本地代理，有就走它。
$proxy = ""
try {
    $tcp = New-Object System.Net.Sockets.TcpClient
    $tcp.Connect("127.0.0.1", 7890)
    $tcp.Close()
    $proxy = "http://127.0.0.1:7890"
    Say "检测到本地代理 127.0.0.1:7890，push 会走它（直连 github.com 很不稳）"
} catch {
    Say "没有可用本地代理，直连 github.com"
}

if ($DryRun) { Say "（DryRun：不推送）" }
else {
    # askpass：git 问用户名时给固定值，问密码时给 token。
    # 这样 token 不会出现在命令行里（不会进进程列表/日志）。
    $askpass = Join-Path $env:TEMP "seqx-askpass.cmd"
    @"
@echo off
echo %~1 | findstr /i "username" >nul
if %errorlevel%==0 (echo x-access-token) else (echo %GITHUB_TOKEN%)
"@ | Set-Content -Path $askpass -Encoding ASCII

    $oldAskpass = $env:GIT_ASKPASS
    $env:GIT_ASKPASS = $askpass
    try {
        # -c credential.helper=  清掉全局的 credential manager（本机没装 GCM，留着会失败）
        # 默认**不加 --force**：万一你之后在 GitHub 上直接改过 main，
        # 强推会把那些提交冲掉。确实要覆盖再显式加 -Force。
        $pushArgs = @("-C", $RepoDir, "-c", "credential.helper=")
        if ($proxy) { $pushArgs += @("-c", "http.proxy=$proxy", "-c", "https.proxy=$proxy") }
        $pushArgs += "push"
        if ($Force) { $pushArgs += "--force" }
        $pushArgs += @($pushUrl, "windows-compat:refs/heads/$Branch")
        & $Git @pushArgs
        if ($LASTEXITCODE -ne 0) {
            if (-not $Force) {
                Die @"
推送失败（退出码 $LASTEXITCODE）。

最常见的原因：这个仓库里已经有 $Branch 分支、而且不是快进推送
（比如你之前在 GitHub 上直接改过东西）。
确认要覆盖的话，加 -Force 重跑：

    .\build\publish-to-github.ps1 -Force
"@
            }
            Die "推送失败（退出码 $LASTEXITCODE）"
        }
        Ok "已推送 windows-compat → $Branch"
    } finally {
        $env:GIT_ASKPASS = $oldAskpass
        Remove-Item $askpass -Force -EA SilentlyContinue
    }

    # 默认分支
    $body = @{ default_branch = $Branch } | ConvertTo-Json
    Invoke-RestMethod -Uri "https://api.github.com/repos/$Owner/$RepoName" -Method Patch `
        -Headers $Headers -Body $body -ContentType "application/json" -TimeoutSec 60 | Out-Null
    Ok "默认分支已设为 $Branch"
}

# ══════════════════════════════════════════════════════════════════
#  5. Release + 安装包附件
# ══════════════════════════════════════════════════════════════════
Head "5/7 建 Release 并上传安装包"
if (-not (Test-Path $SetupExe)) {
    Warn "找不到安装包：$SetupExe"
    Warn "先跑 .\build\build-installer.ps1 生成，然后重跑本脚本（或用 -SetupExe 指定路径）"
} else {
    $sizeMb = [math]::Round((Get-Item $SetupExe).Length / 1MB, 1)
    Say "安装包：$SetupExe（$sizeMb MB）"
    if ($DryRun) { Say "（DryRun：不上传）" }
    else {
        $notes = @"
## Sequoia-X Windows 一键安装包

约 $sizeMb MB 的单文件安装包，**不需要命令行、不需要装 Python、不需要配依赖**。

- 安装位置默认 ``%LOCALAPPDATA%\Programs\Sequoia-X``，**不需要管理员权限**
- 内置便携 Python 运行时 + 全部依赖 + 7 个选股策略 + Web 工作台
- **不含行情数据**：装完请打开工作台 → 设置 → 数据操作 → 「补齐全市场缺口」
  （约 1.5 小时，可中断续传）。不回填就没有选股结果。
- 第一次启动会自动登记两个计划任务：每个交易日 16:00 回填、19:15 选股并推送

静默安装（自动化用）：
``````
Sequoia-X-Setup.exe /S /D="D:\Apps\Sequoia-X" [/NoData] [/NoDesktop] [/NoStartMenu] [/AutoStart] [/Launch]
``````
退出码：0 成功 / 1 通用错误 / 2 参数错误 / 3 负载缺失 / 4 目标目录不可写。

---
本仓库是 [sngyai/Sequoia-X](https://github.com/$Upstream) 的二次开发分支，
策略算法来自原作者。详见 README 与 NOTICE.md。
"@
        $relBody = @{
            tag_name         = $Tag
            target_commitish = $Branch
            name             = "Sequoia-X Windows 版 $Tag"
            body             = $notes
            draft            = $false
            prerelease       = $false
        } | ConvertTo-Json -Depth 3

        # ⚠️ 必须把 JSON 转成 UTF-8 **字节**再发。
        # PowerShell 5.1 的 Invoke-RestMethod 收到字符串时按 ISO-8859-1 编码，
        # 中文会全变成「?」——第一次发布就把 Release 正文毁了 204 个问号。
        $relBytes = [System.Text.Encoding]::UTF8.GetBytes($relBody)

        try {
            $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$Owner/$RepoName/releases" `
                -Method Post -Headers $Headers -Body $relBytes `
                -ContentType "application/json; charset=utf-8" -TimeoutSec 60
            Ok "Release 已建：$($rel.tag_name)"
        } catch {
            # 已存在就复用
            Say "创建 Release 失败（可能已存在），尝试复用 tag $Tag …"
            $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$Owner/$RepoName/releases/tags/$Tag" `
                -Headers $Headers -TimeoutSec 30
            Ok "复用已有 Release：$($rel.tag_name)"
        }

        # 传附件（走 uploads.github.com，跟 git 的 URL 重写无关）
        Say "上传安装包（$sizeMb MB，可能要一两分钟）…"
        $uploadUrl = "https://uploads.github.com/repos/$Owner/$RepoName/releases/$($rel.id)/assets?name=Sequoia-X-Setup.exe"
        $bytes = [System.IO.File]::ReadAllBytes($SetupExe)
        try {
            $asset = Invoke-RestMethod -Uri $uploadUrl -Method Post -Headers $Headers `
                -Body $bytes -ContentType "application/vnd.microsoft.portable-executable" -TimeoutSec 900
            Ok "已上传：$($asset.name)（$([math]::Round($asset.size/1MB,1)) MB）"
            Ok "下载地址：$($asset.browser_download_url)"
        } catch {
            Die "上传失败：$($_.Exception.Message)"
        }
    }
}

# ══════════════════════════════════════════════════════════════════
#  6. 恢复 ghproxy 规则
# ══════════════════════════════════════════════════════════════════
Head "6/7 恢复 git 配置"
if ($script:GhproxyRemoved -and -not $DryRun) {
    Restore-GhproxyRule
} else {
    Say "无需恢复"
}

# ══════════════════════════════════════════════════════════════════
#  7. 验证
# ══════════════════════════════════════════════════════════════════
Head "7/7 验证发布结果"
if ($DryRun) { Say "（DryRun：跳过验证）"; exit 0 }

try {
    $repo = Invoke-RestMethod -Uri "https://api.github.com/repos/$Owner/$RepoName" -Headers $Headers -TimeoutSec 30
    Ok "仓库：$($repo.html_url)"
    Ok "可见性：$(if ($repo.private) {'私有'} else {'公开'})"
    if ($repo.fork) { Ok "fork 关系：forked from $($repo.parent.full_name)" }
    else { Warn "这不是 fork —— GitHub 页面上不会显示 forked from" }
    Ok "默认分支：$($repo.default_branch)"
} catch { Warn "读取仓库信息失败：$($_.Exception.Message)" }

try {
    $rels = Invoke-RestMethod -Uri "https://api.github.com/repos/$Owner/$RepoName/releases" -Headers $Headers -TimeoutSec 30
    foreach ($r in $rels) {
        Say "Release $($r.tag_name)："
        foreach ($a in $r.assets) {
            Say "    附件 $($a.name)  $([math]::Round($a.size/1MB,1)) MB  已下载 $($a.download_count) 次"
        }
    }
} catch { Warn "读取 Release 失败：$($_.Exception.Message)" }

Write-Host ""
Ok "完成！"
Say "仓库地址：https://github.com/$Owner/$RepoName"
Say "Releases：https://github.com/$Owner/$RepoName/releases"
Say ""
Say "建议再去仓库页面上确认三件事："
Say "  1. 标题下面显示「forked from $Upstream」"
Say "  2. README 顶部能看到「关于这个仓库（二次开发声明）」"
Say "  3. Releases 里的 Sequoia-X-Setup.exe 能下载"
