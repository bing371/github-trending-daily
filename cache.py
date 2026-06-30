"""周榜/月榜本地缓存模块

策略：
- daily  每次必抓，不缓存
- weekly / monthly 在配置的更新日抓取，其余日期读本地缓存
- 缓存超过 max_age_days 也强制重抓
"""
import json
import os
from datetime import datetime, timedelta

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")


def _cache_path(since: str) -> str:
    return os.path.join(CACHE_DIR, f"{since}_cache.json")


def save_cache(since: str, data) -> None:
    """保存榜单原始数据到本地缓存"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    obj = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "data": data,
    }
    with open(_cache_path(since), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def load_cache(since: str):
    """读取缓存。返回 (data, updated_at_str)；不存在返回 (None, None)"""
    p = _cache_path(since)
    if not os.path.exists(p):
        return None, None
    try:
        with open(p, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj.get("data"), obj.get("updated_at", "")
    except (json.JSONDecodeError, OSError):
        return None, None


def is_cache_expired(since: str, max_age_days: int) -> bool:
    """判断缓存是否过期（不存在视为过期）"""
    _, updated_at = load_cache(since)
    if not updated_at:
        return True
    try:
        ts = datetime.strptime(updated_at, "%Y-%m-%d %H:%M")
    except ValueError:
        return True
    return (datetime.now() - ts) > timedelta(days=max_age_days)


def cache_age_days(since: str):
    """返回缓存距今的天数；不存在返回 None"""
    _, updated_at = load_cache(since)
    if not updated_at:
        return None
    try:
        ts = datetime.strptime(updated_at, "%Y-%m-%d %H:%M")
        return (datetime.now() - ts).days
    except ValueError:
        return None