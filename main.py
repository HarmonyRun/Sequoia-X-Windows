"""Sequoia-X V2 主程序入口。

两种运行模式（具体用哪个数据源由 .env 里的 DATA_SOURCE 决定）：
  python main.py               # 日常模式：增量补最新交易日 + 跑策略 + 飞书推送
  python main.py --backfill    # 回填模式：补齐全市场缺失的历史/最新 K 线（可续传）

数据源（.env 的 DATA_SOURCE）：
  auto      = 自动（推荐）：按 AUTO_SOURCE_ORDER 探测，某个源连接异常或被限流
              就自动切到下一个源继续补（东方财富 -> 腾讯 -> 新浪）
  eastmoney = 东方财富；tencent = 腾讯；sina = 新浪；baostock = 项目原设计

⚠️ 三个 akshare 系源的后复权基准互不相同，所以同一只股票绝不跨源拼接：
   每只股票用的源记录在 symbol_source 表，增量更新只认这个源。
"""

import argparse
import sys
from dotenv import load_dotenv
load_dotenv()

import socket
socket.setdefaulttimeout(10.0)

from sequoia_x.core.config import get_settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.pipeline import run_daily


def main() -> None:
    parser = argparse.ArgumentParser(description="Sequoia-X V2 选股系统")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="回填模式：拉取全市场历史 K 线（数据源由 DATA_SOURCE 决定）",
    )
    args = parser.parse_args()

    try:
        # 1. 初始化配置
        settings = get_settings()

        # 2. 初始化日志
        logger = get_logger(__name__)
        logger.info("Sequoia-X V2 启动")

        # 3. 初始化数据引擎
        engine = DataEngine(settings)

        if args.backfill:
            # ── 回填模式：补齐全市场缺失的历史/最新 K 线，自动多轮重跑 ──
            logger.info("进入回填模式...")
            all_symbols = engine.get_all_symbols()
            engine.backfill(all_symbols)
            logger.info("Sequoia-X V2 回填模式运行完成")
            return

        # ── 日常模式：增量补最新交易日 + 策略 + 推送 ──
        logger.info("开始拉取最新快照...")
        count = engine.sync_today_bulk()
        logger.info(f"增量同步完成，新增 {count} 行")

        # 4/5. 策略列表、执行、以及（可选）大模型分析都在 sequoia_x.pipeline 里
        #      （命令行与 Web 工作台共用同一条流程）
        run_daily(engine, settings, push=True)

    except Exception:
        try:
            _logger = get_logger(__name__)
            _logger.exception("主流程发生未捕获异常，程序终止")
        except Exception:
            import traceback
            traceback.print_exc()
        sys.exit(1)

    logger.info("Sequoia-X V2 运行完成")


if __name__ == "__main__":
    main()
