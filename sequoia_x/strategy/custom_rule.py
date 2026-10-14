"""把自定义规则包装成标准策略，接入 pipeline。

`CustomRuleStrategy` 继承 `BaseStrategy`，所以 `STRATEGY_CLASSES` 里
其他策略怎么跑，它就怎么跑——工作台勾选、飞书推送、计划任务全都自动生效，
不需要在流水线里加任何特例。

它一次跑完**全部**自定义规则，返回 `{规则名: [股票代码]}`，而不是让每个规则
各自遍历一遍全市场：每遍历一次要读 5000+ 只股票的日线，规则多了会非常慢。
一次遍历、对所有规则同时判定，代价只多几条内存里的比较。
"""

from __future__ import annotations

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.custom.evaluator import RuleError, evaluate, validate
from sequoia_x.strategy.custom.store import list_rules

logger = get_logger(__name__)

# 板块判定：按代码前缀。与 Web 工作台「总览」页的统计口径保持一致。
def board_of(code: str) -> str:
    """按代码前缀判定板块。"""
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("300", "301", "302")):
        return "创业板"
    if code.startswith(("600", "601", "603", "605")):
        return "沪市主板"
    if code.startswith("000"):
        return "深市主板"
    if code.startswith(("001", "002", "003")):
        return "深市中小板"
    if code.startswith(("920", "8", "4")):
        return "北交所"
    return "其它"


class CustomRuleStrategy(BaseStrategy):
    """用户自定义规则的策略容器。

    Attributes:
        webhook_key: 固定为 'custom'。每条规则在结果里单独分组成一条推送，
            不会 15 个自定义规则共用一个飞书机器人而混成一团。
    """

    webhook_key: str = "custom"

    def run(self) -> list[str]:
        """跑全部自定义规则，返回**全部命中股票的并集**。

        为了兼容 `BaseStrategy.run()` 的签名（返回 list[str]），这里返回并集；
        但每条规则各自选出了哪些股票，可以从 `self.last_results` 拿到，
        pipeline 会用它做分组推送。

        Returns:
            全部自定义规则命中股票的并集。
        """
        self.last_results: dict[str, list[str]] = {}
        rules = list_rules()
        if not rules:
            logger.info("没有自定义策略，跳过")
            return []

        # 先过滤出能跑的规则，坏规则只跳过它自己
        usable = []
        for rule in rules:
            try:
                warnings = validate(rule)
            except RuleError as exc:
                logger.warning(f"自定义策略「{rule['name']}」规则非法，跳过：{exc}")
                continue
            for w in warnings:
                logger.info(f"自定义策略「{rule['name']}」提示：{w}")
            usable.append(rule)

        if not usable:
            logger.info("自定义策略全部非法，跳过")
            return []

        symbols = self.engine.get_local_symbols()
        hits: dict[str, list[str]] = {r["name"]: [] for r in usable}
        failed: dict[str, int] = {}

        for symbol in symbols:
            board = board_of(symbol)

            # 股票池过滤（各规则独立，所以逐规则判断）
            try:
                df = self.engine.get_ohlcv(symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{symbol}] 读取失败：{exc}")
                continue
            if df is None or len(df) < 2:
                continue

            for rule in usable:
                name = rule["name"]
                if not self._in_universe(rule, symbol, board):
                    continue
                try:
                    if evaluate(rule, df):
                        hits[name].append(symbol)
                except Exception as exc:  # noqa: BLE001
                    # 单只股票对单条规则失败：记一次就够，不要把日志刷爆
                    failed[name] = failed.get(name, 0) + 1
                    if failed[name] == 1:
                        logger.warning(
                            f"自定义策略「{name}」在 {symbol} 上计算失败（后续同类错误不再重复）：{exc}"
                        )

        # 排序 + 截断（rank / sort / limit 由 evaluator 之外这里统一做）
        for rule in usable:
            name = rule["name"]
            picked = self._post_process(rule, hits[name], symbols)
            self.last_results[name] = picked
            logger.info(f"自定义策略「{name}」选出 {len(picked)} 只股票")

        union: list[str] = []
        seen: set[str] = set()
        for picked in self.last_results.values():
            for code in picked:
                if code not in seen:
                    seen.add(code)
                    union.append(code)
        return union

    def _in_universe(self, rule: dict, symbol: str, board: str) -> bool:
        """判断股票是否在规则的股票池里。"""
        uni = rule.get("universe")
        if not uni:
            return True

        # 显式指定股票池时，其余条件全部忽略
        explicit = uni.get("symbols")
        if explicit:
            return symbol in explicit

        boards = uni.get("boards")
        if boards and board not in boards:
            return False

        if uni.get("exclude_st", True):
            name = self._stock_name(symbol)
            if name and "ST" in name.upper():
                return False

        # market_cap 是预留字段，本地没有市值数据，validate 时已给出警告，
        # 这里静默跳过（不因为缺数据就把用户的条件当成不满足）。
        return True

    def _stock_name(self, symbol: str) -> str:
        """查股票名（用于 ST 过滤）。查不到就返回空串，不做过滤。"""
        try:
            from sequoia_x.notify.feishu import FeishuNotifier

            notifier = getattr(self, "_notifier", None)
            if notifier is None:
                notifier = FeishuNotifier(self.settings)
                self._notifier = notifier
            return notifier.get_stock_names([symbol]).get(symbol, "")
        except Exception:  # noqa: BLE001
            return ""

    def _post_process(self, rule: dict, picked: list[str],
                      all_symbols: list[str]) -> list[str]:
        """对命中的股票做排名 / 排序 / 截断。"""
        limit = rule.get("limit", 100)
        rank = rule.get("rank")

        if rank:
            # 截面排名：按 rank.by 指标算全市场百分位，取前 top
            top = int(rank["top"])
            desc = rank.get("desc", True)
            scored = []
            by = rank["by"]
            params = {k: v for k, v in (rank.get("params") or {}).items()}
            for code in picked:
                try:
                    df = self.engine.get_ohlcv(code)
                    from sequoia_x.strategy.custom.indicators import compute

                    series = compute(by, df, params)
                    value = series.iloc[-1] if len(series) else None
                    if value is None or value != value:   # NaN
                        continue
                    scored.append((float(value), code))
                except Exception:  # noqa: BLE001
                    continue
            scored.sort(reverse=desc)
            picked = [code for _, code in scored[:top]]

        srt = rule.get("sort")
        if srt and picked:
            try:
                from sequoia_x.strategy.custom.indicators import compute

                key = srt["key"]
                desc = srt.get("desc", True)
                values = []
                for code in picked:
                    try:
                        df = self.engine.get_ohlcv(code)
                        series = compute(key, df, {})
                        value = series.iloc[-1] if len(series) else None
                        if value is None or value != value:
                            continue
                        values.append((float(value), code))
                    except Exception:  # noqa: BLE001
                        continue
                values.sort(reverse=desc)
                picked = [code for _, code in values]
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"排序失败，保持原顺序：{exc}")

        return picked[:limit]
