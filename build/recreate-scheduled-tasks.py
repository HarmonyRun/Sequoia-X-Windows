"""把本机那两个定时任务用「写 XML + schtasks /create /xml」的方式重建。

为什么不用 `schtasks /create /tr ...`：只有 XML 能指定 <WorkingDirectory>，
而这两个任务必须在自己所在目录下跑（脚本里用相对路径找 logs\）。实测用 /tr 建的
任务工作目录是 system32，脚本一跑就找不到路径。
"""
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

WORKSPACE = Path(r"C:\Users\mjx server\Documents\股票")

TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{desc}</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <DaysOfWeek><Monday/><Tuesday/><Wednesday/><Thursday/><Friday/></DaysOfWeek>
        <WeeksInterval>1</WeeksInterval>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>{limit}</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{cwd}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""

PLANS = [
    ("Sequoia-X收盘后回填", "每个交易日收盘后一小时回填全市场最新行情数据",
     "16:00", "PT3H", "run-backfill-latest.cmd", "--no-pause"),
    ("Sequoia-X每日选股", "每个交易日 19:15 运行全部策略并推送飞书",
     "19:15", "PT2H", "run-daily.cmd", "--no-pause"),
]

tomorrow = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
rc_all = 0
for name, desc, hhmm, limit, script, arguments in PLANS:
    xml = TASK_XML.format(
        desc=desc, start=f"{tomorrow}T{hhmm}:00", limit=limit,
        command=str(WORKSPACE / script), arguments=arguments, cwd=str(WORKSPACE),
    )
    tmp = Path(__import__("tempfile").gettempdir()) / f"seqx-recreate-{hhmm.replace(':', '')}.xml"
    tmp.write_text(xml, encoding="utf-16")   # schtasks 要 Unicode XML
    proc = subprocess.run(["schtasks", "/create", "/tn", name, "/xml", str(tmp), "/f"],
                          capture_output=True, timeout=30)
    tmp.unlink(missing_ok=True)
    ok = proc.returncode == 0
    print(f"  {name:22s} 退出码={proc.returncode}  {'OK' if ok else 'FAILED'}")
    if not ok:
        rc_all = 1
        print("    ", (proc.stdout or b"").decode("gbk", "replace")[:300])
        print("    ", (proc.stderr or b"").decode("gbk", "replace")[:300])

sys.exit(rc_all)
