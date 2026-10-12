# Sequoia-X Windows 安装程序

纯 .NET Framework 4.x + WinForms，只用系统自带的 `csc.exe` 编译，
**不依赖** Inno Setup / NSIS / 7-Zip / WiX，也不需要联网下载任何东西。

```
installer\
├── Setup.cs              安装器（GUI + 静默 /S）
├── Launcher.cs           应用启动器（拉起 webui 服务并打开浏览器）
├── Uninstaller.cs        卸载器（GUI + 静默 /S）
├── AssemblyInfo.g.cs     程序集版本信息（三个 exe 共用）
├── build.ps1             编译三个 exe，并把 payload.zip 追加到安装器末尾
├── verify-payload.ps1    负载定位 / 前缀过滤 / 解压的自动化验证（辅助，不参与发布）
├── verify\Probe.cs       verify-payload.ps1 用的探针程序
└── dist\                 产物目录（build.ps1 默认输出到这里）
```

---

## 1. 三个 exe 各自做什么

| 文件 | 作用 |
| --- | --- |
| `Sequoia-X-Setup.exe` | 安装包。= `setup-stub.exe` 的 PE 镜像 **+ 追加在末尾的 `payload.zip` 原始字节**。GUI 双击安装；`/S` 无界面静默安装（给自动化用）。 |
| `Launcher.exe` | 启动器。读 `app\.env` 里的 `WEBUI_PORT`，探测 `http://127.0.0.1:<port>/api/overview`；没在跑就用 `runtime\pythonw.exe -m webui.server` 拉起服务（PID 写 `app\data\webui.pid`，stdout/stderr 落 `app\logs\launcher.log`），就绪后打开浏览器。`--silent` 用于开机自启（不显示窗口）。 |
| `Uninstaller.exe` | 卸载器。读自身目录的 `install.json` 得到安装目录，停服务、删快捷方式与注册表项、删程序文件；**默认保留** `app\data` 与 `app\.env`。`/S` 静默卸载。 |

三个 exe 都是 `winexe`（无控制台窗口），`anycpu`，`/optimize+`，零警告编译。

---

## 2. 怎么编译 / 打包

```powershell
# 只编译，不追加负载：产出 dist\Launcher.exe、dist\Uninstaller.exe、dist\setup-stub.exe
.\build.ps1

# 编译 + 把负载追加到安装器末尾，产出 dist\Sequoia-X-Setup.exe
.\build.ps1 -PayloadZip "D:\build\payload.zip" -OutDir "D:\out\dist"
```

* 编译器固定用 `C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe`（C# 5 语法）。
* `-PayloadZip` 省略时只编译，用于确认源码能过编译。
* `-OutDir` 省略时输出到 `installer\dist`。
* 追加后的 `Sequoia-X-Setup.exe` **不会覆盖** `setup-stub.exe`，两者并存：
  最终大小 = `setup-stub.exe` 字节数 + `payload.zip` 字节数（脚本里会校验这一点）。

> **源码必须存成 UTF-8 with BOM**。`csc.exe` 在没有 BOM 时按系统 ANSI 代码页（简体中文 = GBK）
> 解析源文件，中文界面文字会变成乱码。改完 `.cs` 记得确认 BOM 还在。

---

## 3. 负载 zip 的结构约定

`payload.zip` 是一个普通 zip（根目录就是安装目录的根），约定如下：

```
payload.json                 负载元数据（见下）
Launcher.exe                 顶层可执行文件
Uninstaller.exe
runtime\python.exe           Python 运行时（runtime\pythonw.exe 等）
app\.env                     环境变量（WEBUI_PORT / FEISHU_WEBHOOK_URL）
app\main.py                  应用代码
app\data\sequoia_v2.db       预装行情数据（可选的大条目，几百 MB）
```

`payload.json`（扁平 JSON，安装器只读这几个键，缺了就用默认值）：

```json
{
  "app": "Sequoia-X",
  "version": "2.0.0",
  "built_at": "2026-10-12 20:30:00",
  "port": 8899,
  "has_data": true,
  "runtime_size_mb": 210,
  "app_size_mb": 1,
  "data_size_mb": 493.9
}
```

打包要点：

* **压缩方法必须是 Deflate(8) 或 Stored(0)**。.NET Framework 只内置支持这两种；
  用 LZMA / Zstd / BZip2 / AES 打包的条目安装时会报错（安装器会给出中文提示）。
  `Compress-Archive` 与 `[System.IO.Compression.ZipFile]::CreateFromDirectory` 都是 Deflate，都可以。
* 目录层级用「根目录内容」而不是「多一层外壳」：`CreateFromDirectory($src, $zip, ..., $false)`
  的最后那个 `$false` 就是「不要把 $src 这一层打进去」。
* `app\data\sequoia_v2.db` 是唯一会被「不安装行情数据」选项跳过的条目；
  路径固定，靠 zip 条目名精确匹配。
* `has_data` 只是给界面显示用的提示，**以 zip 里是否真有 `app\data\sequoia_v2.db` 条目为准**。

安装器怎么找到负载：从 exe 文件末尾往前搜索 EOCD 签名 `PK\x05\x06`，
用 `zip 起点 = (EOCD - 中央目录大小) - 中央目录偏移` 反推出追加数据的起点，
再校验中央目录签名与第一个本地文件头签名；然后把这**一段**包装成独立流交给
`ZipArchive`（不能把整个 exe 当流传进去，否则条目偏移会整体错位）。

---

## 4. 静默模式（自动化用）

### 4.1 安装器

```
Sequoia-X-Setup.exe /S [/D=<安装目录>] [/NoData] [/NoDesktop] [/NoStartMenu]
                    [/AutoStart] [/Launch] [/Quiet] [/LOG=<日志文件>]
```

| 参数 | 含义 |
| --- | --- |
| `/S` | 静默安装：**完全不显示窗口、不弹任何 MessageBox**，靠退出码与日志汇报结果 |
| `/D=<目录>` | 指定安装目录。默认 `%LOCALAPPDATA%\Programs\Sequoia-X`。惯例上写在最后，本安装器**位置不限**（值只取 `=` 之后的部分；路径含空格时把整个参数用引号包住） |
| `/NoData` | 跳过 `app\data\sequoia_v2.db`（不装预装行情数据） |
| `/NoDesktop` | 不创建桌面快捷方式 |
| `/NoStartMenu` | 不创建开始菜单快捷方式（含「卸载 Sequoia-X」） |
| `/AutoStart` | 写 HKCU `...\Run` 开机自启（`Launcher.exe --silent`） |
| `/Launch` | 装完立即启动 `Launcher.exe`（启动失败不影响退出码） |
| `/Quiet` | 显示窗口但**不弹任何确认框**，并自动开始安装（不需要点按钮） |
| `/LOG=<文件>` | 额外把过程日志写到指定文件（UTF-8） |

退出码（安装器）：

| 码 | 含义 | 细节写在哪 |
| --- | --- | --- |
| `0` | 成功 | — |
| `1` | 通用错误（解压失败、写文件失败、被占用等） | `<安装目录>\install-error.log` + `%TEMP%\sequoia-setup-error.log` |
| `2` | 参数错误（例如 `/D` 少了 `=`、`/D=` 后面是空的、未知开关） | 同上（安装目录未知时只写 `%TEMP%`） |
| `3` | 找不到负载（zip 没有追加到 exe 末尾 / 负载被破坏） | 同上 |
| `4` | 目标目录不可写（无法创建或无法写入探测文件） | 同上 |

静默模式**跳过**「已检测到已安装的 Sequoia-X，是否覆盖安装」的交互确认，直接覆盖安装
（不会删除用户的 `app\data`）。安装成功后会把上一次失败留下的 `install-error.log` 清掉，
避免自动化脚本把旧日志误判成本次失败。

### 4.2 卸载器

```
Uninstaller.exe /S [/KeepData]
```

| 参数 | 含义 |
| --- | --- |
| `/S` | 静默卸载：不显示窗口、不弹任何 MessageBox |
| `/KeepData` | 保留 `app\data` 与 `app\.env` |

**默认（不给 `/KeepData`）也保留数据与配置**——和界面默认一致，静默卸载不会因为少一个参数
就把用户的行情库删掉。想连数据一起删必须显式给 `--delete-data`（危险，非默认）。

退出码：`0` = 成功，`1` = 有文件删不掉或发生异常。细节写 `%TEMP%\sequoia-uninstall.log`。

安装目录里的 `Uninstaller.exe` 自身在运行中删不掉，卸载器会启动一条**退出后执行**的
隐藏 `cmd` 命令把它删掉（并顺手删掉变空的安装目录）。因此卸载刚返回时目录里可能还残留
一瞬间的 `Uninstaller.exe`，属于正常现象。

---

## 5. 从 PowerShell / cmd 调用时的一个坑

`Sequoia-X-Setup.exe` 和 `Uninstaller.exe` 都是 **GUI 子系统**程序。
Windows PowerShell 用 `&` 直接调用 GUI 程序时**不会等待**它结束，`$LASTEXITCODE` 也因此
拿不到真实退出码（会是空的或上一次命令留下的旧值）；`cmd /c app.exe` 同样不等。

请用下面任一种写法：

```powershell
# ① 管道给 Out-Null：强制等待，$LASTEXITCODE 正确
& "$env:TEMP\seqx-out\Sequoia-X-Setup.exe" /S /D="$env:TEMP\seqx-silent-test" /NoData | Out-Null
"退出码=$LASTEXITCODE"

# ② Start-Process -Wait -PassThru：最明确
$p = Start-Process -FilePath "$env:TEMP\seqx-out\Sequoia-X-Setup.exe" `
                   -ArgumentList "/S", "/D=$env:TEMP\seqx-silent-test", "/NoData" -Wait -PassThru
"退出码=$($p.ExitCode)"
```

```bat
:: ③ cmd：必须用 start /wait
start /wait "" Sequoia-X-Setup.exe /S /D=C:\path\Sequoia-X /NoData
echo 退出码=%ERRORLEVEL%
```

---

## 6. 验证脚本

`verify-payload.ps1` 会编译一个探针（`verify\Probe.cs` 与生产源码一起编译），在**不运行 GUI**
的前提下真实驱动 `PayloadLocator` / `EntryFilter` / `ServiceControl`：

```powershell
.\verify-payload.ps1            # 造假负载 → 追加 → 校验定位、前缀过滤、逐字节比对
.\verify-payload.ps1 -KeepTemp  # 保留临时目录，便于人工排查
```

真机静默安装 / 卸载的验收步骤（PowerShell）：

```powershell
.\build.ps1 -PayloadZip "$env:TEMP\fake-payload.zip" -OutDir "$env:TEMP\seqx-out"
& "$env:TEMP\seqx-out\Sequoia-X-Setup.exe" /S /D="$env:TEMP\seqx-silent-test" /NoData | Out-Null
"安装退出码=$LASTEXITCODE"
& "$env:TEMP\seqx-silent-test\Uninstaller.exe" /S | Out-Null
"卸载退出码=$LASTEXITCODE"
```

---

## 7. 安装器写进系统的东西（便于手工清理）

* 快捷方式：桌面 `Sequoia-X 工作台.lnk`；开始菜单 `Sequoia-X\Sequoia-X 工作台.lnk`、
  `Sequoia-X\卸载 Sequoia-X.lnk`
* 注册表：`HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\Sequoia-X`
  （显示名 / 版本 / 安装位置 / 卸载命令 / 估算体积）
* 可选：`HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 的 `Sequoia-X` 值（`/AutoStart` 或勾选）
* 安装目录里的 `install.json`（记录安装位置、时间、版本，卸载器据此定位）

卸载器会清理以上全部内容，并按 `app\.env` 里的 `WEBUI_PORT`（默认 8899）停掉正在运行的工作台服务。
