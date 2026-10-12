#!/usr/bin/env python
"""把负载目录压成安装器要用的 payload.zip。

为什么用 Python 的 zipfile 而不是 PowerShell 的 Compress-Archive：
- 需要控制压缩级别（deflate 9）；Compress-Archive 用默认级别
- 需要精确控制条目顺序：494MB 的数据库放最后，安装器解压时前面都是小文件，
  进度条能正常走，最后再切到 marquee 提示「正在解压历史行情数据」
- Compress-Archive 处理 20 万个文件的目录树很慢

⚠️ 只能用 DEFLATE，不能用 ZIP_LZMA：安装器是 C# 写的，.NET Framework 的
ZipArchive 只认 Store / Deflate，遇到 LZMA 条目会抛异常。
"""

from __future__ import annotations

import os
import sys
import time
import zipfile
from pathlib import Path

# 这些顶层条目必须排在 zip 前面（小文件，安装时先解出来）
FIRST = ("payload.json", "Launcher.exe", "Uninstaller.exe")
# 这个排最后（大文件，安装器会用 marquee 进度条单独提示）
LAST = ("app/data/sequoia_v2.db",)


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: make-payload-zip.py <payload目录> <输出zip路径>")
        return 2

    src = Path(sys.argv[1]).resolve()
    dst = Path(sys.argv[2]).resolve()
    if not src.is_dir():
        print(f"错误：负载目录不存在：{src}")
        return 2

    # 收集所有文件（相对路径用正斜杠，和 zip 约定一致）
    entries: list[tuple[str, Path]] = []
    for path in src.rglob("*"):
        if path.is_file():
            rel = str(path.relative_to(src)).replace(os.sep, "/")
            entries.append((rel, path))

    def rank(rel: str) -> tuple:
        if rel in FIRST:
            return (0, FIRST.index(rel))
        if rel in LAST:
            return (2, 0)
        return (1, rel)

    entries.sort(key=lambda e: rank(e[0]))

    missing = [name for name in FIRST if not any(r == name for r, _ in entries)]
    if missing:
        print(f"⚠️  负载里缺少这些条目：{missing}")
        print("   （Launcher.exe / Uninstaller.exe 由 installer\\build.ps1 编译后拷进来）")

    total_bytes = sum(p.stat().st_size for _, p in entries)
    print(f"负载目录 : {src}")
    print(f"条目数   : {len(entries):,}")
    print(f"原始大小 : {total_bytes / 1048576:.1f} MB")
    print(f"输出     : {dst}")
    print()

    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()

    t0 = time.time()
    done_bytes = 0
    last_report = 0.0
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=9, allowZip64=True) as z:
        for i, (rel, path) in enumerate(entries, 1):
            z.write(path, rel)
            done_bytes += path.stat().st_size
            now = time.time()
            if now - last_report > 5 or i == len(entries):
                pct = done_bytes / total_bytes * 100 if total_bytes else 100
                speed = done_bytes / 1048576 / max(now - t0, 0.001)
                print(f"  [{i:>6}/{len(entries)}] {pct:5.1f}%  {speed:6.1f} MB/s  "
                      f"当前：{rel[:70]}")
                last_report = now

    sz = dst.stat().st_size
    print()
    print(f"完成：{sz / 1048576:.1f} MB（压缩率 {sz / total_bytes * 100:.1f}%），"
          f"耗时 {time.time() - t0:.0f} 秒")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
