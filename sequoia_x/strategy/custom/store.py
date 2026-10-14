"""自定义策略的存储：把规则 JSON 存在 data/custom_strategies.json。

存储格式（一个文件存全部规则，便于整体备份和查看）：

```json
{
  "version": 1,
  "strategies": {
    "缩量回调放量收阳": {
      "name": "缩量回调放量收阳",
      "description": "...",
      "conditions": { ... },
      "created_at": "2026-10-14 10:00:00",
      "updated_at": "2026-10-14 10:00:00",
      "source": "llm" | "manual"
    }
  }
}
```

为什么用「名字」当 key 而不是生成的 id：用户是拿名字认策略的，
界面上显示的、飞书卡片上印的都是名字。用 id 会多一层「名字↔id」的映射，
而名字本身已经天然唯一（重名直接拒绝保存）。改名就等于删旧建新。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.custom.evaluator import RuleError, validate

logger = get_logger(__name__)

# 允许出现在 webhook_key 里的字符（策略名要能变成合法的 webhook key）
_SAFE_KEY_RE = re.compile(r"[^0-9a-zA-Z_\u4e00-\u9fff]+")

# 自定义策略 key 的统一前缀。加前缀有两个作用：
#   1. 和 15 个内置策略的 key 天然区分开，不会撞名
#   2. 飞书推送时一眼能看出是用户自定义的
CUSTOM_PREFIX = "custom_"


def _data_dir() -> Path:
    """数据目录。跟随 DB_PATH 的目录，保持「所有运行数据都在 data/ 下」。"""
    from sequoia_x.core.config import get_settings

    db_path = Path(get_settings().db_path)
    # db_path 是相对项目根的，形如 data/sequoia_v2.db
    return db_path.parent if db_path.parent != Path(".") else Path("data")


def store_path() -> Path:
    """规则文件路径。"""
    return _data_dir() / "custom_strategies.json"


def webhook_key_of(name: str) -> str:
    """把策略名转成 webhook key。

    例：``缩量回调放量收阳`` -> ``custom_缩量回调放量收阳``（中文保留，可读性好）
        ``MA5 上穿 MA20``    -> ``custom_MA5_上穿_MA20``
    """
    cleaned = _SAFE_KEY_RE.sub("_", name.strip()).strip("_")
    if not cleaned:
        cleaned = "unnamed"
    return f"{CUSTOM_PREFIX}{cleaned}"


def _read_all() -> dict:
    """读整个文件。文件不存在或损坏时返回空结构（不让坏文件卡死程序）。"""
    path = store_path()
    if not path.is_file():
        return {"version": 1, "strategies": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.error(f"自定义策略文件损坏，按空处理：{path} -> {exc}")
        return {"version": 1, "strategies": {}}
    if not isinstance(data, dict) or not isinstance(data.get("strategies"), dict):
        logger.error(f"自定义策略文件结构异常，按空处理：{path}")
        return {"version": 1, "strategies": {}}
    return data


def _write_all(data: dict) -> None:
    """原子写：先写临时文件再替换，避免中途崩溃留下半个文件。

    规则文件一旦损坏，用户辛苦描述出来的策略就没了，所以这里值得多几行。
    """
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def list_rules() -> list[dict]:
    """列出全部自定义策略（按名字排序）。

    Returns:
        ``[{"webhook_key", "name", "description", "conditions", "created_at",
           "updated_at", "source", "warnings"}]``
    """
    data = _read_all()
    out = []
    for name, item in sorted(data["strategies"].items()):
        try:
            warnings = validate(item)
        except RuleError as exc:
            # 校验不过的策略仍然列出来，但带上错误，让用户在界面上能看到并修
            warnings = [f"规则已失效：{exc}"]
        out.append({
            "webhook_key": webhook_key_of(name),
            "name": name,
            "description": item.get("description", ""),
            "conditions": item.get("conditions"),
            "created_at": item.get("created_at", ""),
            "updated_at": item.get("updated_at", ""),
            "source": item.get("source", "manual"),
            "warnings": warnings,
        })
    return out


def load_rule(name: str) -> dict | None:
    """按名字取一条规则。"""
    data = _read_all()
    item = data["strategies"].get(name)
    if item is None:
        return None
    out = dict(item)
    out["webhook_key"] = webhook_key_of(name)
    return out


class CustomStrategyStore:
    """自定义策略的增删改查。静态函数已够用，这个类是为了方便调用方持有配置。"""

    def __init__(self) -> None:
        pass

    def list(self) -> list[dict]:
        return list_rules()

    def get(self, name: str) -> dict | None:
        return load_rule(name)

    def save(self, rule: dict, source: str = "manual") -> dict:
        return save_rule(rule, source=source)

    def delete(self, name: str) -> bool:
        return delete_rule(name)


def save_rule(rule: dict, source: str = "manual") -> dict:
    """保存一条规则。

    Args:
        rule: 规则字典，必须含 name 和 conditions。
        source: ``"llm"``（大模型生成）或 ``"manual"``（用户手写/手改）。

    Returns:
        保存后的完整记录（含 webhook_key）。

    Raises:
        RuleError: 规则非法。
    """
    warnings = validate(rule)          # 非法直接抛，调用方把消息给用户看

    name = rule["name"].strip()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data = _read_all()
    existing = data["strategies"].get(name)

    # 只存 schema 里定义的字段，避免大模型塞进莫名其妙的东西
    record = {
        "name": name,
        "description": (rule.get("description") or "").strip(),
        "conditions": rule["conditions"],
        "created_at": existing.get("created_at", now) if existing else now,
        "updated_at": now,
        "source": source,
    }
    for field in ("universe", "rank", "sort", "limit"):
        if rule.get(field) is not None:
            record[field] = rule[field]

    data["strategies"][name] = record
    _write_all(data)
    logger.info(f"已保存自定义策略「{name}」（来源 {source}）")
    record["webhook_key"] = webhook_key_of(name)
    record["warnings"] = warnings
    return record


def delete_rule(name: str) -> bool:
    """删除一条规则。返回是否真的删掉了。"""
    data = _read_all()
    if name not in data["strategies"]:
        return False
    del data["strategies"][name]
    _write_all(data)
    logger.info(f"已删除自定义策略「{name}」")
    return True
