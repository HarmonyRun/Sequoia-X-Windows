"""配置管理模块：通过 pydantic-settings 从环境变量或 .env 文件加载系统配置。"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    db_path: str = "data/sequoia_v2.db"
    start_date: str = "2024-01-01"

    # 行情数据源：
    #   baostock   = 项目原设计
    #   eastmoney  = 东方财富（akshare，本机当前默认）
    #   tencent    = 腾讯（akshare stock_zh_a_hist_tx）
    #   sina       = 新浪（akshare stock_zh_a_daily）
    #   auto       = 自动：按 auto_source_order 依次探测，某个源连接异常/被限流
    #                就自动切到下一个源，继续把剩余数据补完
    # ⚠️ 不同源的后复权算法不同（同一只股票同一天的价格基准能差 40%），
    #    所以每只股票的数据源会被记录在 symbol_source 表里，增量更新只认这个源，
    #    绝不允许同一只股票一半用 A 源、一半用 B 源。
    data_source: str = "baostock"

    # auto 模式下数据源的尝试顺序（逗号分隔）。
    auto_source_order: str = "eastmoney,tencent,sina"

    # 回填并发线程数。东方财富对高频请求会瞬时断连（RemoteDisconnected /
    # ProxyError），并发不宜高；连续拉几千只后还会升级为硬限流，2 是实测较稳的值。
    ak_workers: int = 4

    # 每次请求前的间隔（秒）。连续拉几千只之后东方财富会升级为硬限流
    # （实测失败率从 13% 一路涨到 79%，之后整段冷却期内 100% 失败），
    # 补缺口时用 2 线程 + 0.3 秒会温和很多。
    ak_delay: float = 0.0

    # 单只股票连续多少个交易日拿不到新数据后，允许换一个可用的数据源
    # 「整只重拉」。换源会改变该股票的历史价格基准，但同股票内部仍然自洽
    # （所有策略用的都是同股票内部的比值），所以不影响信号。
    # 1 = 每日回填时源不可用就立刻换（保证全市场数据截止日一致）；
    # 2 = 更保守，等源挂满 2 个交易日才换。
    rebase_stale_days: int = 1

    feishu_webhook_url: str  # 必填字段，缺失时抛出 ValidationError
    strategy_webhooks: dict[str, str] = {}

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # <--- 加上这一行！让 Pydantic 放行未定义的变量
    )

    @classmethod
    def settings_customise_sources(cls, settings_cls, **kwargs):  # type: ignore[override]
        """扩展配置源，支持从环境变量中扫描 STRATEGY_WEBHOOK_ 前缀的键。"""
        from pydantic_settings import EnvSettingsSource
        import os

        sources = super().settings_customise_sources(settings_cls, **kwargs)

        # 扫描环境变量，将 STRATEGY_WEBHOOK_<KEY> 收集到 strategy_webhooks
        prefix = "STRATEGY_WEBHOOK_"
        webhooks: dict[str, str] = {}
        for key, value in os.environ.items():
            if key.upper().startswith(prefix):
                strategy_key = key[len(prefix):].lower()
                webhooks[strategy_key] = value

        # 注入到初始化数据中（通过 init_kwargs source）
        if webhooks:
            original_init = kwargs.get("init_settings")
            # 直接在 env 层注入，通过 model_post_init 处理
            os.environ.setdefault("_STRATEGY_WEBHOOKS_PARSED", "1")
            # 存储解析结果供 model_validator 使用
            cls._parsed_strategy_webhooks = webhooks

        return sources

    def model_post_init(self, __context: object) -> None:
        """初始化后合并 STRATEGY_WEBHOOK_ 前缀的环境变量到 strategy_webhooks。"""
        import os

        prefix = "STRATEGY_WEBHOOK_"
        webhooks: dict[str, str] = dict(self.strategy_webhooks)
        for key, value in os.environ.items():
            if key.upper().startswith(prefix):
                strategy_key = key[len(prefix):].lower()
                webhooks[strategy_key] = value

        # 使用 object.__setattr__ 绕过 pydantic 的不可变保护
        object.__setattr__(self, "strategy_webhooks", webhooks)

    def get_webhook_url(self, webhook_key: str) -> str:
        """
        根据 webhook_key 返回对应的 Webhook URL。

        优先从 strategy_webhooks 查找，找不到则 fallback 到 feishu_webhook_url。

        Args:
            webhook_key: 策略标识，如 'ma_volume'、'breakout'。

        Returns:
            对应的 Webhook URL 字符串。
        """
        return self.strategy_webhooks.get(webhook_key.lower(), self.feishu_webhook_url)


_settings: Settings | None = None


def get_settings() -> Settings:
    """返回全局 Settings 单例。

    首次调用时从环境变量或 .env 文件加载配置。
    若必填字段（feishu_webhook_url）缺失，抛出 pydantic_core.ValidationError。

    Returns:
        Settings: 全局唯一的配置实例。

    Raises:
        pydantic_core.ValidationError: 当必填字段缺失或字段类型不匹配时抛出。
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
