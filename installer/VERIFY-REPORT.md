# Sequoia-X 安装程序 —— 静默模式实现与真机验收报告

> 本报告由负责「静默安装模式」的子代理产出；所有结论都来自本机实际运行，不是读代码推断。

## 0. 结论摘要

| 任务 | 状态 |
| --- | --- |
| 任务 1：安装器 `/S` 静默安装 + `/D=` `/NoData` `/NoDesktop` `/NoStartMenu` `/AutoStart` `/Launch` `/Quiet` | 已完成，真机通过 |
| 任务 2：卸载器 `/S` 静默卸载 + `/KeepData`（默认也保留数据） | 已完成，真机通过 |
| 任务 3：`README.md` | 已写（`installer\README.md`） |
| 任务 4：编译 + 造假负载 + 静默安装/覆盖/卸载 + 退出码 | 全部实测通过，退出码 `0/1/2/3/4` 逐一验证 |
| 额外 | 修掉两个**会让追加式负载完全装不上**的致命 bug（第 5 节） |

**一条必须先知道的坑**：PowerShell 用 `&` 调用这三个 exe（GUI 子系统）**不会等待**，
`$LASTEXITCODE` 拿不到真实退出码 —— 详见第 4 节，验收脚本必须改写法。

---

## 1. 环境

* 编译器：`C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe`（4.8.9232.0），C# 5 语法。
* 没有 Inno Setup / NSIS / 7-Zip，全程未联网。
* 四个 `.cs` 全部为 **UTF-8 with BOM**（无 BOM 时 csc 按 GBK 解析，中文界面会乱码）。
* 系统为简体中文 Windows，`$env:TEMP` = `C:\Users\MJXSER~1\AppData\Local\Temp`（8.3 短路径，
  与长路径 `C:\Users\mjx server\AppData\Local\Temp` 指向同一目录）。

## 2. 编译（实际输出）

```
PS> cd "C:\Users\mjx server\Documents\股票\installer"
PS> .\build.ps1
==> 编译三个 exe（/target:winexe /platform:anycpu /optimize+）
    编译 Launcher.exe ... 完成
    Launcher.exe             17,920 字节
    编译 Uninstaller.exe ... 完成
    Uninstaller.exe          20,480 字节
    编译 setup-stub.exe ... 完成
    setup-stub.exe           34,304 字节
==> 未指定 -PayloadZip：只编译，不追加负载
```

零错误、零警告。

## 3. 三个 exe 的字节大小（当前源码，最后一次全量验证）

| 文件 | 字节 |
| --- | --- |
| `dist\Launcher.exe` | **18,432** |
| `dist\Uninstaller.exe` | **20,480** |
| `dist\setup-stub.exe` | **34,304** |
| `dist\Sequoia-X-Setup.exe`（= stub + 假负载 19,865） | **54,169** |
| `dist\Sequoia-X-Setup.exe`（追加真实负载的那份，20:51:29 生成） | 214,371,681 |

> `Launcher.exe` 在我最初验收时是 17,920 字节；另一个 agent 在 20:58 重构了它的日志泵
> （新增 `StartLogPump`），现在是 18,432 字节。`Setup.cs`（91,297 字节，SHA-256 前缀
> `941C583FC0`）与 `Uninstaller.cs`（44,062 字节 → `Uninstaller.exe` 20,480 字节，
> SHA-256 前缀 `8CA079CE04`）在整个验收期间没有变过。
>
> 最后一次全量验证（当前磁盘上的源码）：编译零错误 → 静默安装 `0` → 覆盖安装 `0` →
> 静默卸载 `0` → 剩余 `app\`、`app\data\`、`app\.env`（设计如此）。
>
> 追加真实负载的 `Sequoia-X-Setup.exe` 是**更早的一份 stub** 打的包；要让真包包含本次
> 全部改动，需要重新跑一次 `.\build.ps1 -PayloadZip <真实 payload.zip> -OutDir dist`
> （真实负载 `installer\payload.zip` 目前由另一个 agent 在重建）。

## 4. 验收全过程（实际命令与输出）

### 4.1 假负载

```
%TEMP%\fake-payload\
  payload.json         119 字节   {"app":"Sequoia-X","version":"2.0.0","port":8899,
                                   "has_data":false,"runtime_size_mb":1,"app_size_mb":1,"data_size_mb":0}
  runtime\python.exe    15 字节
  app\main.py           68 字节
  app\.env              36 字节   WEBUI_PORT=8897 / FEISHU_WEBHOOK_URL=（空）
  Launcher.exe      17,920 字节   用 dist 里真的
  Uninstaller.exe   20,480 字节   用 dist 里真的
%TEMP%\fake-payload.zip  19,865 字节（[System.IO.Compression.ZipFile]::CreateFromDirectory，Deflate）
```

> `.env` 的端口写的是 **8897 而不是 8899**：本机 8899 被另一个 agent 正在跑的
> `pythonw -m webui.server` 占用，而卸载器会按 `.env` 里的端口停服务；为避免打断它的测试，
> 本次验收换了端口。`.env` 其余内容与约定完全一致，代码路径也完全相同。
> 若要在 8899 上空闲时按原样重跑，把 `.env` 改回 `WEBUI_PORT=8899` 即可。

### 4.2 打包

```
PS> .\build.ps1 -PayloadZip "$env:TEMP\fake-payload.zip" -OutDir "$env:TEMP\seqx-out"
    负载 zip 可读      : 6 个条目
==> 把负载追加到安装器末尾
    stub 34,304 字节 + payload 19,865 字节 = 54,169 字节
安装包：C:\Users\...\Temp\seqx-out\Sequoia-X-Setup.exe
```

### 4.3 静默安装（题面给的原样命令）

```powershell
PS> cmd /c exit 42          # 先制造一个已知的旧 $LASTEXITCODE
PS> & "$env:TEMP\seqx-out\Sequoia-X-Setup.exe" /S /D="$env:TEMP\seqx-silent-test" /NoData
PS> echo "退出码=$LASTEXITCODE"
退出码=42                   # ← 旧值！进程还没跑完，PowerShell 没有等待
（3 秒后目录存在 = True）    # 安装其实成功了
```

**同一件事，换成会等待的写法：**

```powershell
PS> $p = Start-Process -FilePath "$env:TEMP\seqx-out\Sequoia-X-Setup.exe" `
                       -ArgumentList "/S","/D=$env:TEMP\seqx-silent-test","/NoData" -Wait -PassThru
PS> $p.ExitCode
0
```

安装后的目录树：

```
seqx-silent-test\app\  <DIR>
seqx-silent-test\runtime\  <DIR>
seqx-silent-test\install.json   134 字节
seqx-silent-test\Launcher.exe   17920 字节
seqx-silent-test\payload.json   119 字节
seqx-silent-test\Uninstaller.exe   20480 字节
seqx-silent-test\app\data\  <DIR>
seqx-silent-test\app\logs\  <DIR>
seqx-silent-test\app\.env   36 字节
seqx-silent-test\app\main.py   68 字节
seqx-silent-test\runtime\python.exe   15 字节
```

`install.json` 内容：

```json
{"install_dir":"C:\\Users\\mjx server\\AppData\\Local\\Temp\\seqx-silent-test",
 "installed_at":"2026-10-12 20:51:45","version":"2.0.0"}
```

同时确认写入/创建了：桌面 `Sequoia-X 工作台.lnk`、开始菜单 `Sequoia-X\`（工作台 + 卸载两个 lnk）、
`HKCU\...\Uninstall\Sequoia-X` 卸载项。

### 4.4 覆盖安装（同样命令再跑一次）

```
退出码 = 0
install.json 时间戳 = 20:51:46      # 刷新了
错误日志是否残留 = False            # 成功安装会清掉上次失败的 install-error.log
```

### 4.5 静默卸载

```
PS> $p3 = Start-Process -FilePath "$env:TEMP\seqx-silent-test\Uninstaller.exe" -ArgumentList "/S" -Wait -PassThru
PS> $p3.ExitCode
0
剩余内容：
  seqx-silent-test\app\  <DIR>
  seqx-silent-test\app\data\  <DIR>
  seqx-silent-test\app\.env   36 字节
桌面/开始菜单/注册表残留 = False / False / False
```

`%TEMP%\sequoia-uninstall.log`：

```
Sequoia-X 静默卸载记录
时间    ：2026-10-12 20:52:01
命令行  ："...\seqx-silent-test\Uninstaller.exe" /S
安装目录：C:\Users\mjx server\AppData\Local\Temp\seqx-silent-test
数据配置：已保留（app\data 与 app\.env）
结果    ：成功
```

> 剩余 `app\data`（空目录）与 `app\.env` 是**设计如此**：默认（以及显式 `/KeepData`）保留数据与配置。
> `Uninstaller.exe` 自身在运行中删不掉，由退出后的隐藏 `cmd` 延迟命令删除 —— 实测进程一退出
> 目录里就已经看不到它了。

### 4.6 退出码逐项实测

| 场景 | 命令要点 | 退出码 |
| --- | --- | --- |
| 成功 | `/S /D=<空目录> /NoData` | **0** |
| 通用错误 | 预先在安装目录里放一个名叫 `runtime` 的**文件**挡住解压 | **1** |
| 参数错误 | `/S /D`（少了 `=`） / `/S /Bogus`（未知开关） | **2** / **2** |
| 找不到负载 | 对未追加负载的 `setup-stub.exe` 跑 `/S` | **3** |
| 目录不可写 | `/S /D=<一个普通文件>\sub` | **4** |

退出码 1 时两处日志都写出来了：

```
<安装目录>\install-error.log            True
%TEMP%\sequoia-setup-error.log          True

Sequoia-X 安装程序 —— 安装失败记录
时间    ：2026-10-12 20:56:09
退出码  ：1
命令行  ："...\Sequoia-X-Setup.exe" /S /D=... /NoData /NoDesktop /NoStartMenu
安装目录：C:\Users\mjx server\AppData\Local\Temp\seqx-fail
过程日志：
安装目录：C:\Users\mjx server\AppData\Local\Temp\seqx-fail
安装行情数据：否
安装失败：System.IO.IOException: 无法创建"...\seqx-fail\runtime"，因为同名文件或目录已存在。
```

### 4.7 其它开关实测

| 开关 | 实测结果 |
| --- | --- |
| `/NoDesktop` `/NoStartMenu` | 退出码 0；桌面与开始菜单快捷方式**都没有**创建 |
| `/AutoStart` | 退出码 0；`HKCU\...\Run\Sequoia-X` = `"...\Launcher.exe" --silent` |
| `/Launch` | 退出码 0；确实拉起了 `Launcher.exe`（实测到进程） |
| `/Quiet` | 耗时 1 秒，退出码 0；**显示窗口但不弹任何确认框、自动开始安装、装完自动关闭** |
| 卸载 `/KeepData` | 退出码 0；`app\data` 与 `app\.env` 保留，`HKCU Run` 项被清掉 |
| 卸载（不给 `/KeepData`） | 退出码 0；同样保留数据与配置（与界面默认一致） |

---

## 5. 顺带修掉的两个致命 bug（不修的话任何负载都装不上）

### bug ① 追加 zip 的起点算错了

`PayloadLocator.FindZipOffset` 原来用

```csharp
long prefix = fileLen - cdSize - 22 - commentLen;   // 错：这是「中央目录的起点」
```

算出的是**中央目录**的位置，只有当 zip 里一个本地文件头都没有时才碰巧等于 zip 起点。
正确公式（由 EOCD 反推）：

```csharp
long cdStart   = eocdAbs - cdSize;        // 中央目录在文件中的绝对位置
long zipOffset = cdStart - cdOffset;      // cdOffset 是 CD 相对 zip 起点的偏移
```

实测数据（54,169 字节的安装包、19,865 字节负载）：`eocdAbs = 54147, cdSize = 352,
cdOffset = 19301` → `zipOffset = 54147 - 352 - 19301 = 34494`，正是 stub 的字节数。
原公式会算出 `53973`（中央目录起点），于是后续按 `prefix + cdOffset` 去读，
直接越过文件末尾 → 返回 -1 → 静默安装退出码 3「找不到负载」。

### bug ② `ZipArchive` 被喂了整个 exe

即使偏移找对了，原来的 `OpenZipAt` 是把**整个 exe** 当流交给 `ZipArchive`：

```
System.IO.InvalidDataException: 中央目录结尾中应包含的条目数与中央目录中的条目数不对应。
   在 System.IO.Compression.ZipArchive.ReadCentralDirectory()
   在 System.IO.Compression.ZipArchive.get_Entries()
```

.NET Framework 会把中央目录里「相对 zip 起点」的偏移当成「相对流起点」来解释。
必须给 `ZipArchive` 一个**起点就是 zip 第一字节**的流。当前实现是把这段负载
切片成 `MemoryStream`（<768MB）或用内存映射视图流（≥768MB，零拷贝）。

### 这两条的证据

`verify\Probe.cs` 单独读取时，`Entries = 0` 且 `get_Entries()` 抛异常；
修好后同一份安装包 `offset = 34494`（= stub 大小）、12/12 条目与
`ZipFile::OpenRead` 直读结果完全一致、解压出的文件字节数逐条相符。

---

## 6. 调用方式（**必须改写法**）

`Sequoia-X-Setup.exe` / `Uninstaller.exe` 都是 GUI 子系统程序。
Windows PowerShell 用 `&` 调 GUI 程序**不等待**，`$LASTEXITCODE` 是上一次命令留下的旧值；
`cmd /c app.exe` 同样不等。三种可靠写法：

```powershell
# ① 管道给 Out-Null（改动最小）
& "$env:TEMP\seqx-out\Sequoia-X-Setup.exe" /S /D="$env:TEMP\seqx-silent-test" /NoData | Out-Null
"退出码=$LASTEXITCODE"      # ← 实测 0

# ② Start-Process -Wait -PassThru（最明确）
$p = Start-Process "$env:TEMP\seqx-out\Sequoia-X-Setup.exe" `
                   -ArgumentList "/S","/D=$env:TEMP\seqx-silent-test","/NoData" -Wait -PassThru
"退出码=$($p.ExitCode)"

# ③ cmd 必须 start /wait
start /wait "" Sequoia-X-Setup.exe /S /D=C:\path\Sequoia-X /NoData
echo 退出码=%ERRORLEVEL%
```

另外注意：`Start-Process -Wait` 会等待**整棵子进程树**。带 `/Launch` 时它会一直等到
被拉起的 `Launcher.exe` 退出为止（本次验收就因此卡住过一次，最后手动结束了那个进程）。
只想拿退出码时不要对带 `/Launch` 的调用用 `-Wait`。

---

## 7. 已知未修的问题（不在我的改动范围内，留给对应负责人）

1. **`verify-payload.ps1` 自身有 bug**：第 429 行 `& $SetupProbe setupform $setupB ...`
   用了 `$setupB`，而它到第 459 行才被赋值；脚本开头有 `Set-StrictMode -Version 2.0`，
   于是运行到这里直接抛「检索不到变量 $setupB」并中断（退出码 1）。
   **生产代码本身没有失败**：中断前 22 项检查全部 PASS（zip 定位、条目一致性、
   前缀过滤、真实安装流程、快捷方式、注册表 9 个值……）。
2. `installer\` 目录里有一个 198MB 左右的真实 `payload.zip`，以及 `dist\` 下 214MB 的
   `Sequoia-X-Setup.exe`（早期 stub + 真实负载），都不是本次改动的产物。
3. `Launcher.cs` 曾被写坏过 BOM（无 BOM 会让启动器中文界面变乱码），本次已补回。

---

## 8. 并发情况（重要，需要人工决策）

本次作业期间，`installer\` 目录**始终有另一个 agent 在并发写入**（同一个任务的重复派发）：

* 它实现了 `/S` 静默安装（`SilentSetup`）、修了上面两个 bug 中的定位公式、
  并用真实负载跑通了 `%TEMP%\seqx-e2e` 的安装（日志 `%TEMP%\seqx-e2e*.log`）；
* 我在它的成果之上**增量补齐**了：`/Launch`、`/Quiet`、退出码 4、
  强制要求的 `install-error.log` + `%TEMP%\sequoia-setup-error.log`、stderr 的 UTF-8 输出，
  并独立完成了卸载器静默化与 README；
* 它的 `verify-payload.ps1` / `Probe.cs` 在本次作业期间仍在被它改写。

**风险**：两边同时写同一批文件，谁最后保存谁生效。如果它接着用整文件覆盖的方式保存
`Setup.cs`，我在其中追加的 `/Launch`、`/Quiet`、退出码 4 与错误日志可能会被覆盖掉。
建议由一个人独占 `installer\*.cs`。
