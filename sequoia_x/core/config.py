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
    #
    # 默认 auto 而不是 upstream 的 baostock：安装包分发给别人时要保证「装完就能用」，
    # 而 baostock 需要登录、且会拉黑高频请求的 IP；auto 在某个源不可用时会自动换源。
    # 启用哪些选股策略（逗号分隔的策略 key）。
    #
    # 空字符串（默认）= 全部启用 —— 这是为了向后兼容：老用户的 .env 里没有这一项，
    # 行为必须和以前完全一致（跑全部策略）。
    #
    # 一旦填了值，就只有列出的策略参与选股，其余不跑、不推送。
    # 这个开关是**全局生效**的：命令行 main.py、Web 工作台手动触发、
    # 以及每天 19:15 的计划任务都读同一个值，保证三处行为一致。
    #
    # 例：ENABLED_STRATEGIES=ma_volume,turtle,rps
    enabled_strategies: str = ""

    # 批量选股时是否预加载全市场数据到内存。
    #
    # 为什么需要：16 个策略各自遍历一遍全市场，每只都查一次数据库，
    # 合计 8.4 万次 SQL，光查库就要 196 秒。而所有策略用的是同一份数据，
    # 加载一次留在内存后总耗时降到约 12 秒（实测）。
    #
    # 代价：一次性约 341 MB 内存（用完即释放，不常驻）。
    # 如果机器内存紧张，或者怀疑缓存导致数据不是最新的，设成 false 即可
    # 退回逐股查库——选股结果完全一样，只是慢。
    cache_market_data: bool = True

    data_source: str = "auto"

    # auto 模式下数据源的尝试顺序（逗号分隔）。
    # 东方财富数据质量最好放第一，被限流时自动退到后面。新浪排在腾讯前面是因为
    # 实测快 3 倍（0.56 秒/只 vs 1.85 秒/只）。
    auto_source_order: str = "eastmoney,sina,tencent"

    # 回填并发线程数。东方财富对高频请求会瞬时断连（RemoteDisconnected /
    # ProxyError），并发不宜高；实测 4 线程无间隔连续拉 4000+ 只会升级为硬限流
    # （失败率 13% → 79%），3 是实测较稳的值。
    ak_workers: int = 3

    # 每次请求前的间隔（秒）。配合并发数一起控制请求速率。
    ak_delay: float = 0.3

    # 单只股票连续多少个交易日拿不到新数据后，允许换一个可用的数据源
    # 「整只重拉」。换源会改变该股票的历史价格基准，但同股票内部仍然自洽
    # （所有策略用的都是同股票内部的比值），所以不影响信号。
    # 1 = 每日回填时源不可用就立刻换（保证全市场数据截止日一致）；
    # 2 = 更保守，等源挂满 2 个交易日才换。
    rebase_stale_days: int = 1

    # 飞书群机器人 Webhook。默认空字符串而不是必填：安装包分发给别人时要保证
    # 「装完就能启动」，用户可以在 Web 工作台的「设置」页里填（那边会校验必填）。
    # 没配之前策略照常跑，只是跳过推送。
    feishu_webhook_url: str = ""
    strategy_webhooks: dict[str, str] = {}

    # ── 大模型分析（可选）──
    # 开启后，每天选股跑完会把「当天市场情况 + 各策略选出的股票」交给大模型分析，
    # 分析结果推送到飞书。用的是 **OpenAI 兼容的 /chat/completions 接口**，
    # 所以同一套代码能支持 DeepSeek / 通义 / 智谱 / Moonshot / 本地 Ollama 等，
    # 用户只要填 base_url + model + api_key 三样。
    #
    # 默认关闭：安装包分发的场景下多数用户不会配，没配就必须安静地跳过，
    # 不能因为缺配置就让整条流水线报错。
    llm_enabled: bool = False
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""

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
                # 空值不要记进来：否则 get_webhook_url 会返回空串，
                # 而不是回退到主 Webhook
                if value and value.strip():
                    webhooks[strategy_key] = value.strip()

        # 使用 object.__setattr__ 绕过 pydantic 的不可变保护
        object.__setattr__(self, "strategy_webhooks", webhooks)

    def get_webhook_url(self, webhook_key: str) -> str:
        """
        根据 webhook_key 返回对应的 Webhook URL。

        优先从 strategy_webhooks 查找，找不到**或为空**则 fallback 到
        feishu_webhook_url（安装包分发的场景下 .env 里这些键会留空，
        不能因此把推送整个丢掉）。

        Args:
            webhook_key: 策略标识，如 'ma_volume'、'breakout'。

        Returns:
            对应的 Webhook URL 字符串；两者都没配时返回空串。
        """
        specific = (self.strategy_webhooks.get(webhook_key.lower()) or "").strip()
        return specific or (self.feishu_webhook_url or "").strip()


_settings: Settings | None = None


def reset_settings() -> None:
    """清掉单例缓存，让下次 ``get_settings()`` 重新从 .env 读一遍。

    Web 工作台改了 .env 之后必须调这个，否则进程里缓存的老配置会一直生效。
    """
    global _settings
    _settings = None


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
