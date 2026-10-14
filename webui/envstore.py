"""配置文件（.env）的读写 + 设置项描述 + 校验。

Web 工作台的「设置」页靠这个模块工作。设计上的两个要点：

1. **写 .env 必须保住原文件**。这个文件里有大量解释性注释（为什么用某个数据源、
   踩过什么坑），是给人看的文档，程序不能因为改一个值就把它们冲掉。所以按行处理：
   注释、空行、顺序全部保留，只替换目标键的值；被注释掉的键（比如
   ``# STRATEGY_WEBHOOK_MA_VOLUME=...``）就地取消注释，而不是在文件末尾再追加一行。

2. **写完要回读验证**。改配置改坏了比不改更糟，所以写完立刻重新解析一遍，
   确认每个键都能读出预期值；不对就用备份还原并报错。
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENV_PATH = REPO / ".env"

# 支持增量回填的数据源（与 sequoia_x.data.engine.AKSHARE_SOURCES 保持一致）
_AKSHARE_SOURCES = ("eastmoney", "tencent", "sina")

# 7 个策略的中文短名（与 sequoia_x.pipeline.STRATEGY_LABELS 一致，这里独立一份
# 是为了让设置页在没有加载引擎的情况下也能渲染）
STRATEGY_KEYS: list[tuple[str, str]] = [
    ("ma_volume", "均线放量"),
    ("turtle", "海龟交易"),
    ("flag", "高旗形整理"),
    ("shakeout", "涨停洗盘"),
    ("limit_down", "上升趋势跌停"),
    ("rps", "RPS极强动量"),
    ("private_placement", "定增监控"),
]


# ══════════════════════════════════════════════════════════════════
#  设置项描述
# ══════════════════════════════════════════════════════════════════

def _strategy_items() -> list[dict]:
    items = []
    for key, label in STRATEGY_KEYS:
        items.append({
            "key": f"STRATEGY_WEBHOOK_{key.upper()}",
            "label": f"{label} · 专属 Webhook",
            "type": "password",
            "secret": True,
            "url": True,
            "required": False,
            "placeholder": "留空 = 使用主 Webhook",
            "help": f"只想把「{label}」的信号推到另一个飞书群时填这里，留空走主通道。",
            "default": "",
        })
    return items


GROUPS: list[dict] = [
    {
        "key": "feishu",
        "title": "飞书推送",
        "desc": "选股结果推送到飞书群机器人。改完记得点「测试推送」验证一下——"
                "Webhook 写错不会报错，只会静默收不到消息。",
        "items": [
            {
                "key": "FEISHU_WEBHOOK_URL",
                "label": "主 Webhook 地址",
                "type": "password",
                "secret": True,
                "url": True,
                "required": True,
                "placeholder": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx",
                "help": "飞书群 → 设置 → 群机器人 → 添加机器人 → 自定义机器人 → 复制 Webhook 地址。"
                        "没有单独配置的策略都用这个通道。",
                "default": "",
            },
            *_strategy_items(),
        ],
    },
    {
        "key": "datasource",
        "title": "数据源与回填参数",
        "desc": "行情数据从哪里来、回填时开几个线程、每次请求间隔多久。",
        "items": [
            {
                "key": "DATA_SOURCE",
                "label": "数据源",
                "type": "select",
                "required": True,
                "default": "auto",
                "choices": [
                    {"value": "auto", "label": "auto · 自动探测并换源（推荐）"},
                    {"value": "eastmoney", "label": "eastmoney · 东方财富"},
                    {"value": "tencent", "label": "tencent · 腾讯"},
                    {"value": "sina", "label": "sina · 新浪"},
                    {"value": "baostock", "label": "baostock · 项目原设计（本机 IP 已被拉黑）"},
                ],
                "help": "auto 会按下面的顺序依次探测，某个源连接异常或被限流就自动切到下一个，"
                        "继续把剩余数据补完。单源模式则只用那一个源。",
            },
            {
                "key": "AUTO_SOURCE_ORDER",
                "label": "auto 尝试顺序",
                "type": "text",
                "required": True,
                "default": "eastmoney,sina,tencent",
                "placeholder": "eastmoney,sina,tencent",
                "help": "逗号分隔。实测速度（拉 2024 年起全历史）：新浪 0.56 秒/只、"
                        "腾讯 1.85 秒/只、东方财富约 0.5 秒/只但连拉几千只会被硬限流，"
                        "所以把新浪排在腾讯前面。",
            },
            {
                "key": "AK_WORKERS",
                "label": "回填并发线程数",
                "type": "int",
                "min": 1,
                "max": 8,
                "required": True,
                "default": "3",
                "help": "⚠️ 实测 4 线程无间隔连拉 4000+ 只后，东方财富会从「偶发断连」"
                        "升级为「硬限流」（失败率 13% → 79%）。3 线程以内比较稳。",
            },
            {
                "key": "AK_DELAY",
                "label": "每次请求间隔（秒）",
                "type": "float",
                "min": 0,
                "max": 5,
                "step": 0.1,
                "required": True,
                "default": "0.3",
                "help": "配合并发数一起控制请求速率。0 = 不等待（容易被限流）。",
            },
            {
                "key": "REBASE_STALE_DAYS",
                "label": "换源重拉的陈旧阈值（交易日）",
                "type": "int",
                "min": 1,
                "max": 30,
                "required": True,
                "default": "1",
                "help": "某只股票连续多少个交易日拉不到新数据后，允许换一个可用源「整只重拉」。"
                        "设为 1：源不可用就立刻换，保证全市场数据截止日一致（选股不会混用"
                        "不同截止日）；想更保守就调大。",
            },
            {
                "key": "START_DATE",
                "label": "历史起始日期",
                "type": "date",
                "required": True,
                "default": "2024-01-01",
                "help": "回填从这里开始。⚠️ RPS 策略需要 120 个交易日的回看窗口，"
                        "所以不能设得太近（至少往前留 7~8 个月）。",
            },
            {
                "key": "DB_PATH",
                "label": "数据库文件路径",
                "type": "text",
                "required": True,
                "default": "data/sequoia_v2.db",
                "help": "相对于项目根目录。改成一个新路径等于换一个数据库——"
                        "新库里没有任何历史数据，需要重新回填。",
            },
        ],
    },
    {
        "key": "strategies",
        "title": "选股策略",
        "desc": "勾选参与选股的策略。不勾任何一项 = 全部启用（保持与升级前一致）。"
                "这个开关是全局的：命令行、工作台手动触发、每天定时任务都读同一份配置。",
        "items": [
            {
                "key": "ENABLED_STRATEGIES",
                "label": "启用的策略",
                "type": "text",
                "required": False,
                "default": "",
                "placeholder": "留空 = 全部启用；或用逗号分隔，如 ma_volume,turtle,rps",
                "help": "留空表示全部启用（向后兼容，升级后行为不变）。"
                        "要只跑其中几个，就把它们的 key 用英文逗号隔开填进来；"
                        "填了非法 key 会直接报错并列出所有可用 key，不会静默忽略。"
                        "工作台的勾选面板走 /api/strategies，最终也是写这一项。",
            },
        ],
    },
    {
        "key": "llm",
        "title": "大模型分析（可选）",
        "desc": "开启后，每次跑完选股会把「当天的市场情况 + 各策略选出的股票」交给大模型分析，"
                "分析结果自动推送到飞书。用的是 OpenAI 兼容接口，"
                "DeepSeek / 通义 / 智谱 / Moonshot / 本地 Ollama 都能接。",
        "items": [
            {
                "key": "LLM_ENABLED",
                "label": "启用大模型分析",
                "type": "select",
                "required": True,
                "default": "0",
                "choices": [
                    {"value": "0", "label": "关闭（不分析、不推送）"},
                    {"value": "1", "label": "开启"},
                ],
                "help": "开启后每天自动选股跑完会多推一条「AI 分析」卡片到飞书。"
                        "下面三项没填齐的话不会分析，只会在日志里提示。",
            },
            {
                "key": "LLM_BASE_URL",
                "label": "接口地址（Base URL）",
                "type": "text",
                "required": False,
                "default": "",
                "placeholder": "https://api.deepseek.com/v1",
                "help": "填到 /v1 为止即可，程序会自动补 /chat/completions。"
                        "常见值：DeepSeek → https://api.deepseek.com/v1 ；"
                        "通义千问 → https://dashscope.aliyuncs.com/compatible-mode/v1 ；"
                        "本地 Ollama → http://localhost:11434/v1",
            },
            {
                "key": "LLM_MODEL",
                "label": "模型名",
                "type": "text",
                "required": False,
                "default": "",
                "placeholder": "deepseek-chat",
                "help": "要和服务商文档里的名字完全一致，例如 deepseek-chat、"
                        "qwen-plus、glm-4-plus、gpt-4o-mini。本地 Ollama 就是 "
                        "ollama list 里那个名字。",
            },
            {
                "key": "LLM_API_KEY",
                "label": "API Key",
                "type": "password",
                "secret": True,
                "required": False,
                "default": "",
                "placeholder": "sk-…",
                "help": "本地 Ollama 不需要鉴权，留空即可。填完建议点右上角「测试大模型」验证一下——"
                        "地址或模型名写错不会报错，只会静默分析不出来。",
            },
        ],
    },
    {
        "key": "webui",
        "title": "Web 工作台",
        "desc": "工作台自身的设置。",
        "items": [
            {
                "key": "WEBUI_PORT",
                "label": "端口",
                "type": "int",
                "min": 1024,
                "max": 65535,
                "required": True,
                "default": "8899",
                "restart": True,
                "help": "工作台只监听 127.0.0.1，局域网其它机器访问不到。"
                        "改完要重启工作台（关掉窗口重新运行 run-webui.cmd）才生效。",
            },
        ],
    },
]

_SPEC_BY_KEY: dict[str, tuple[dict, dict]] = {
    item["key"]: (group, item) for group in GROUPS for item in group["items"]
}


# ══════════════════════════════════════════════════════════════════
#  .env 读写
# ══════════════════════════════════════════════════════════════════

# 匹配 `KEY=value`，也匹配被注释掉的 `# KEY=value`（值是空的或占位符都算）
_KEY_RE = re.compile(r"^\s*#?\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def read_env() -> dict[str, str]:
    """解析 .env，返回 ``{键: 值}``。找不到文件就返回空字典。"""
    if not ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _KEY_RE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if line.lstrip().startswith("#"):
            # 被注释掉的键：只在没有其他来源时才当作有效值，而且不覆盖真实键
            out.setdefault(key, "")
            continue
        out[key] = value
    return out


def write_env(updates: dict[str, str]) -> list[str]:
    """只改指定键，保留注释/空行/顺序；返回实际写入的键列表。

    写完会回读校验，任何不一致就用备份还原并抛错——改配置改坏了比不改更糟。
    """
    if not updates:
        return []

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    if ENV_PATH.exists():
        shutil.copy2(ENV_PATH, ENV_PATH.with_name(".env.bak"))

    remaining = dict(updates)
    written: list[str] = []
    out: list[str] = []

    for line in lines:
        m = _KEY_RE.match(line)
        key = m.group(1) if m else None
        if key and key in remaining:
            if key in written:
                # 同一个键出现多次（比如上面有真实行、下面还有注释模板）：
                # 保留原样，不要再写一遍，免得文件里出现两个有效定义
                out.append(line)
                continue
            out.append(f"{key}={remaining.pop(key)}")
            written.append(key)
        else:
            out.append(line)

    if remaining:
        out.append("")
        out.append("# ===== 由 Web 工作台追加 =====")
        for key, value in remaining.items():
            out.append(f"{key}={value}")
            written.append(key)

    tmp = ENV_PATH.with_name(".env.tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, ENV_PATH)

    # 回读校验
    after = read_env()
    bad = [k for k, v in updates.items() if after.get(k, None) != v]
    if bad:
        backup = ENV_PATH.with_name(".env.bak")
        if backup.exists():
            shutil.copy2(backup, ENV_PATH)
        raise ValueError(f"写入 .env 后回读校验失败，已还原备份。不一致的键：{bad}")

    # 同步到当前进程的环境变量：
    #  - 有值：写入 os.environ（strategy_webhooks 是扫 os.environ 得到的）
    #  - 空值：从 os.environ 删掉，否则会被当成「配置了一个空 Webhook」
    for key, value in updates.items():
        if value == "":
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    return written


# ══════════════════════════════════════════════════════════════════
#  当前值 → 给前端的结构
# ══════════════════════════════════════════════════════════════════

def current_value(item: dict, env: dict[str, str]) -> tuple[str, bool]:
    """返回 ``(当前值, 是否来自 .env 文件)``。

    优先 .env；.env 里没有再看环境变量（可能是手工 export 的）；
    都没有就用默认值。
    """
    key = item["key"]
    if key in env and env[key] != "":
        return env[key], True
    if key in env:
        return "", True          # 文件里显式写了空值
    value = os.environ.get(key)
    if value:
        return value, False      # 来自环境变量，不在 .env 里
    return item.get("default", ""), False


def build_groups() -> list[dict]:
    """把 GROUPS 里描述的设置项配上当前值，返回给前端渲染。"""
    env = read_env()
    groups = []
    for group in GROUPS:
        items = []
        for item in group["items"]:
            value, in_file = current_value(item, env)
            items.append({
                "key": item["key"],
                "label": item["label"],
                "type": item["type"],
                "value": value,
                "placeholder": item.get("placeholder", ""),
                "help": item.get("help", ""),
                "required": bool(item.get("required", False)),
                "choices": item.get("choices"),
                "min": item.get("min"),
                "max": item.get("max"),
                "step": item.get("step"),
                "restart": bool(item.get("restart", False)),
                "secret": bool(item.get("secret", False)),
                "in_file": in_file,
            })
        groups.append({
            "key": group["key"],
            "title": group["title"],
            "desc": group["desc"],
            "items": items,
        })
    return groups


# ══════════════════════════════════════════════════════════════════
#  校验
# ══════════════════════════════════════════════════════════════════

class SettingError(ValueError):
    """设置项校验失败。消息直接给用户看。"""


def strategy_keys() -> list[str]:
    """合法的策略 key 列表（= ``pipeline.STRATEGY_LABELS`` 的键）。

    走**延迟导入**：``webui/envstore`` 被 ``server`` 导入的时间点很早，
    顶层直接 import ``sequoia_x.pipeline`` 会连带把 15 个策略模块全拉起来。
    延迟到真正校验/展示时再导入，既省启动时间，也避免设置页在极端情况下
    因为某个策略模块导入失败而整个打不开。

    导入失败时返回空列表 —— 调用方据此**跳过白名单校验**（宁可放过也不
    要因为拿不到清单而把所有保存都拒掉）。
    """
    try:
        from sequoia_x.pipeline import STRATEGY_LABELS
    except Exception:  # noqa: BLE001
        return []
    return list(STRATEGY_LABELS.keys())


def strategy_labels() -> dict[str, str]:
    """``{key: 中文短名}``；拿不到时返回空字典。"""
    try:
        from sequoia_x.pipeline import STRATEGY_LABELS
    except Exception:  # noqa: BLE001
        return {}
    return dict(STRATEGY_LABELS)


def parse_enabled_strategies(value: str) -> list[str]:
    """把 ``ENABLED_STRATEGIES`` 的原始字符串切成 key 列表。

    逗号分隔，去空白、丢空项。**不判合法性**（合法判断在 ``validate_one``），
    这里只负责切分，方便测试和复用。
    """
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def raw_enabled_strategies() -> str:
    """读取当前 ``ENABLED_STRATEGIES`` 的原值（空串 = 全部启用）。

    供 ``/api/strategies`` 回显用，避免调用方去碰 ``_SPEC_BY_KEY`` 这类内部结构。
    """
    spec = _SPEC_BY_KEY.get("ENABLED_STRATEGIES")
    if spec is None:
        return ""
    value, _in_file = current_value(spec[1], read_env())
    return value


def _clean(key: str, raw) -> str:
    if raw is None:
        return ""
    return str(raw).strip()


def validate_one(key: str, raw) -> tuple[str, list[str]]:
    """校验单个设置项，返回 ``(规范化后的值, 警告列表)``；不合法抛 SettingError。"""
    spec = _SPEC_BY_KEY.get(key)
    if spec is None:
        raise SettingError(f"未知的配置项：{key}")
    _group, item = spec
    value = _clean(key, raw)
    label = item["label"]
    warnings: list[str] = []

    if item.get("required") and value == "":
        raise SettingError(f"「{label}」不能为空")

    kind = item["type"]

    if kind in ("int", "float"):
        if value == "":
            value = item.get("default", "0")
        try:
            num = int(value) if kind == "int" else float(value)
        except ValueError:
            raise SettingError(f"「{label}」必须是{'整数' if kind == 'int' else '数字'}") from None
        lo, hi = item.get("min"), item.get("max")
        if lo is not None and num < lo:
            raise SettingError(f"「{label}」不能小于 {lo}")
        if hi is not None and num > hi:
            raise SettingError(f"「{label}」不能大于 {hi}")
        value = str(num)

    elif kind == "date":
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise SettingError(f"「{label}」格式必须是 YYYY-MM-DD") from None

    elif kind == "select":
        allowed = [c["value"] for c in item.get("choices", [])]
        if value not in allowed:
            raise SettingError(f"「{label}」只能是：{' / '.join(allowed)}")

    elif kind in ("text", "password"):
        # 只有明确标了 url 的字段才校验「必须是网址」。
        # 早先是「secret 字段一律当网址」，结果加 API Key 时会被误伤——
        # sk-xxxx 当然不是 http 开头。
        if value and item.get("url") and not value.startswith(("http://", "https://")):
            raise SettingError(f"「{label}」必须是以 http:// 或 https:// 开头的地址")
        if value and item.get("url") and "open.feishu.cn" not in value:
            warnings.append(
                f"「{label}」不是 open.feishu.cn 域名，除非你确实在用自建代理，否则推送会失败"
            )

    # 跨字段/取值层面的提醒
    if key == "ENABLED_STRATEGIES":
        # 空字符串合法 = 全部启用（向后兼容）
        keys = parse_enabled_strategies(value)
        allowed = strategy_keys()
        if keys and allowed:
            unknown = [k for k in keys if k not in allowed]
            if unknown:
                # 拼错了必须报错而不是静默忽略，否则用户以为限制了、其实全跑
                raise SettingError(
                    f"「{label}」里有未知的策略：{', '.join(unknown)}。"
                    f"可用的策略 key：{', '.join(allowed)}"
                )
        if len(keys) != len(set(keys)):
            # 去重后写回，避免 .env 里出现重复 key
            deduped = list(dict.fromkeys(keys))
            warnings.append(f"「{label}」里有重复的策略，已去重：{', '.join(deduped)}")
            value = ",".join(deduped)
        else:
            value = ",".join(keys)
        if keys and allowed and len(keys) == len(allowed):
            warnings.append(
                f"「{label}」勾选了全部 {len(allowed)} 个策略，等价于留空；"
                "要跑全部建议直接留空，以后新增策略时会自动带上"
            )
    if key == "AK_WORKERS" and int(value) > 3:
        warnings.append(f"并发调到 {value} 容易被东方财富限流（实测 4 线程无间隔会触发硬限流）")
    if key == "DATA_SOURCE" and value == "baostock":
        warnings.append("本机 IP 已被 baostock 拉黑，选它会导致回填全部失败")
    if key == "AUTO_SOURCE_ORDER":
        parts = [p.strip() for p in value.split(",") if p.strip()]
        if not parts:
            raise SettingError("「auto 尝试顺序」不能为空")
        unknown = [p for p in parts if p not in _AKSHARE_SOURCES]
        if unknown:
            raise SettingError(
                f"「auto 尝试顺序」里有不支持的源：{', '.join(unknown)}"
                f"（只能是 {' / '.join(_AKSHARE_SOURCES)}）"
            )
        if "baostock" in parts:
            warnings.append("baostock 本机不可用，放在 auto 顺序里只会拖慢每次探测")
        if len(parts) == 1:
            warnings.append("只配了一个源，它挂掉时就没有备份源可切了")
    if key == "DB_PATH" and value != "data/sequoia_v2.db":
        warnings.append("换了数据库路径后，新库里没有任何历史数据，需要重新回填一次")

    return value, warnings


def validate(updates: dict) -> tuple[dict[str, str], list[str]]:
    """批量校验，返回 ``(规范化后的更新, 警告列表)``。任一项不合法就抛 SettingError。"""
    normalized: dict[str, str] = {}
    warnings: list[str] = []
    for key, raw in updates.items():
        value, warns = validate_one(key, raw)
        normalized[key] = value
        warnings.extend(warns)

    _validate_llm_combined(normalized)
    return normalized, warnings


def _validate_llm_combined(updates: dict[str, str]) -> None:
    """跨字段校验：开启大模型分析就必须把地址和模型名填齐。

    单字段校验做不到这件事——「地址为空」在没开启时是合法的。所以要把
    「已保存的值 + 本次改动」合并起来看。
    """
    env = read_env()
    merged: dict[str, str] = {}
    for key in ("LLM_ENABLED", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"):
        if key in updates:
            merged[key] = updates[key]
        else:
            spec = _SPEC_BY_KEY.get(key)
            value, _ = current_value(spec[1], env) if spec else ("", False)
            merged[key] = value

    enabled = merged.get("LLM_ENABLED", "0") in ("1", "true", "True", "on")
    if not enabled:
        return

    missing = []
    if not merged.get("LLM_BASE_URL", "").strip():
        missing.append("接口地址")
    if not merged.get("LLM_MODEL", "").strip():
        missing.append("模型名")
    if missing:
        raise SettingError(
            "开启了「大模型分析」，但还没填：" + "、".join(missing)
            + "（不想用大模型就把「启用大模型分析」改回关闭）"
        )


def llm_from(body: dict) -> dict:
    """从请求体里取「测试大模型」要用的参数。

    优先用请求里直接给的（这样可以先测试再保存），缺的用已保存的值兜底。
    """
    env = read_env()
    out = {}
    for key, field in (
        ("LLM_BASE_URL", "base_url"),
        ("LLM_MODEL", "model"),
        ("LLM_API_KEY", "api_key"),
    ):
        raw = body.get(field)
        if raw is not None and str(raw).strip():
            out[field] = str(raw).strip()
        else:
            spec = _SPEC_BY_KEY.get(key)
            value, _ = current_value(spec[1], env) if spec else ("", False)
            out[field] = value
    return out


def resolve_webhook(body: dict) -> str:
    """解析「测试推送」要用哪个 Webhook 地址。

    优先用请求里直接给的 ``webhook_url``（这样可以先测试再保存）；
    否则按 ``strategy_key`` 取已保存的地址；再否则用主 Webhook。
    """
    url = _clean("FEISHU_WEBHOOK_URL", body.get("webhook_url"))
    if url:
        return url
    env = read_env()
    strategy_key = _clean("strategy_key", body.get("strategy_key"))
    if strategy_key:
        item_key = f"STRATEGY_WEBHOOK_{strategy_key.upper()}"
        if item_key in _SPEC_BY_KEY:
            value, _ = current_value(_SPEC_BY_KEY[item_key][1], env)
            if value:
                return value
    main, _ = current_value(_SPEC_BY_KEY["FEISHU_WEBHOOK_URL"][1], env)
    return main


def mask_url(url: str) -> str:
    """把 Webhook 地址中间打码，用于回显（别把完整密钥写进日志和界面提示）。"""
    if not url:
        return ""
    if len(url) <= 12:
        return "****"
    return url[:36] + "****" + url[-6:]


def needs_restart(key: str) -> bool:
    """这个配置项是不是必须重启工作台才生效。"""
    spec = _SPEC_BY_KEY.get(key)
    return bool(spec and spec[1].get("restart"))
