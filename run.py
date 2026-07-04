#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub Trending Daily - 每日自动抓取GitHub热榜Top10并生成精美HTML"""

import os
import re
import sys
import json
import time
import logging
import subprocess
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader

try:
    from translator import translate_repo_descriptions
except ImportError:
    translate_repo_descriptions = None

try:
    from cache import save_cache, load_cache, is_cache_expired, cache_age_days
except ImportError:
    save_cache = load_cache = is_cache_expired = cache_age_days = None

# SSL 配置：默认用 certifi 的证书；若 GitHub 出现「unable to get local
# issuer certificate」类问题，可设环境变量 GITHUB_TRENDING_INSECURE=1
# 切到 verify=False（GitHub 是知名站点，本地脚本可接受）。
try:
    import certifi
    _CERT_PATH = certifi.where()
except ImportError:
    _CERT_PATH = True
SSL_VERIFY = False if os.environ.get("GITHUB_TRENDING_INSECURE") == "1" else _CERT_PATH


def _load_dotenv():
    """Read .env into os.environ for any var NOT already set. Cheap stdlib
    parser (no python-dotenv dependency). Used for BAIDU_APPID /
    BAIDU_TRANSLATE_KEY / minimax_API_KEY."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

# Windows 控制台强制 UTF-8（emoji 才能正常输出）
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

# ═══════════════════════════════════════
# 配置区
# ═══════════════════════════════════════
BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = BASE_DIR / "data"
PAGES_DIR = BASE_DIR / "pages"
LOG_DIR = BASE_DIR / "log"
FAILURES_LOG = LOG_DIR / "failures.log"
DAILY_LOG = LOG_DIR / "daily.log"
TEMPLATE_DIR = BASE_DIR
INDEX_PATH = BASE_DIR / "index.html"

GITHUB_TRENDING_URL = "https://github.com/trending"

# 三档窗口配置
WINDOWS = ("daily", "weekly", "monthly")
WINDOW_LABELS = {
    "daily": "今日热门",
    "weekly": "本周热门",
    "monthly": "本月热门",
}
WINDOW_BADGES = {
    "daily": "24h",
    "weekly": "7d",
    "monthly": "30d",
}
# 周榜和月榜只在周一抓（0=周一）；其余日期读缓存
WEEKLY_UPDATE_DAY = 0
MONTHLY_UPDATE_DAY = 0
WEEKLY_MAX_CACHE_DAYS = 7
MONTHLY_MAX_CACHE_DAYS = 14
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_DELAY = 5

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# GitHub Linguist 官方配色
LANGUAGE_COLORS = {
    "Python": "#3572A5",
    "JavaScript": "#f1e05a",
    "TypeScript": "#3178c6",
    "Java": "#b07219",
    "C++": "#f34b7d",
    "C": "#555555",
    "C#": "#178600",
    "Go": "#00ADD8",
    "Rust": "#dea584",
    "Ruby": "#701516",
    "PHP": "#4F5D95",
    "Swift": "#F05138",
    "Kotlin": "#A97BFF",
    "Dart": "#00B4AB",
    "Scala": "#c22d40",
    "R": "#198CE7",
    "Shell": "#89e051",
    "Lua": "#000080",
    "Perl": "#0298c3",
    "Haskell": "#5e5086",
    "Vue": "#41b883",
    "Svelte": "#ff3e00",
    "HTML": "#e34c26",
    "CSS": "#563d7c",
    "SCSS": "#c6538c",
    "Jupyter Notebook": "#DA5B0B",
    "Objective-C": "#438eff",
    "Assembly": "#6E4C13",
    "PowerShell": "#012456",
    "Elixir": "#6e4a7e",
    "Clojure": "#db5855",
    "Erlang": "#B83998",
    "Zig": "#ec915c",
    "Nim": "#ffc200",
    "OCaml": "#3be133",
    "Julia": "#a270ba",
    "MATLAB": "#e16737",
    "Dockerfile": "#384d54",
    "Makefile": "#427819",
    "Nix": "#7e7eff",
    "Astro": "#ff5a03",
    "MDX": "#fcb32c",
}


# ═══════════════════════════════════════
# 弹窗模块
# ═══════════════════════════════════════
def _open_in_browser(html_path: str) -> bool:
    """Try multiple methods to open HTML. Designed to work in schtasks (session 0)
    when user is logged on (popup appears on user's desktop). When no user is
    logged on, methods may fail silently — that's fine, the HTML is already
    generated and the user can open it manually.

    Returns True if at least one method launched the browser.
    """
    # 用 http://127.0.0.1:8765 而不是 file:// — File System Access API 需要
    # secure context（http://localhost 或 https://），file:// 不行。
    # 收藏按钮依赖 FSA，所以必须走 server。
    rel = html_path.replace("\\", "/").split("/pages/", 1)[-1]  # "2026/07/2026-07-04.html"
    url = f"http://127.0.0.1:8765/pages/{rel}"

    # Write popup log to its own file (print is lost when schtasks has no console)
    popup_log = BASE_DIR / "log" / "popup.log"
    popup_log.parent.mkdir(parents=True, exist_ok=True)
    def _log(msg: str) -> None:
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
        print(line)
        with open(popup_log, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    _log(f"尝试打开: {url}")

    attempts = []

    def _m_webbrowser():
        return webbrowser.open(url)
    attempts.append(("webbrowser.open", _m_webbrowser))

    def _m_startfile():
        return os.startfile(url)
    attempts.append(("os.startfile", _m_startfile))

    def _m_subprocess():
        return subprocess.Popen(
            ["cmd", "/c", "start", "", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    attempts.append(("subprocess.start", _m_subprocess))

    for name, fn in attempts:
        try:
            result = fn()
            _log(f"{name} OK (returned {result!r})")
            return True
        except Exception as e:
            _log(f"{name} 失败: {e}")
            continue

    _log(f"所有方法失败 (用户可能未登录) — HTML 已生成在: {html_path}")
    return False


# ═══════════════════════════════════════
# 抓取模块
# ═══════════════════════════════════════
def fetch_trending_page(since: str = "daily") -> str:
    """抓取 GitHub Trending 页面 HTML。
    since: daily / weekly / monthly
    """
    assert since in WINDOWS, f"since 必须是 {WINDOWS} 之一"
    url = f"{GITHUB_TRENDING_URL}?since={since}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Cache-Control": "max-age=0",
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"  🌐 正在抓取GitHub Trending[{since}]（第{attempt}次尝试）...")
            response = requests.get(
                url,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                verify=SSL_VERIFY,
            )
            response.raise_for_status()
            print(f"  ✅ 抓取成功，状态码: {response.status_code}")
            return response.text
        except requests.RequestException as e:
            print(f"  ❌ 第{attempt}次尝试失败: {e}")
            if attempt < MAX_RETRIES:
                print(f"  ⏳ {RETRY_DELAY}秒后重试...")
                time.sleep(RETRY_DELAY)
            else:
                print("  💀 所有重试均失败，程序退出")
                sys.exit(1)


def parse_trending(html: str, top_n: int = 10) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    repo_list = []
    articles = soup.select("article.Box-row")

    if not articles:
        print("  ⚠️ 未找到任何仓库条目，GitHub页面结构可能已变化")
        sys.exit(1)

    for rank, article in enumerate(articles[:top_n], start=1):
        repo = {"rank": rank}

        # 仓库名 (作者/仓库名)
        h2 = article.select_one("h2 a")
        if h2:
            full_name = h2.get_text(strip=True).replace("\n", "").replace(" ", "")
            parts = full_name.split("/")
            if len(parts) == 2:
                repo["author"] = parts[0].strip()
                repo["name"] = parts[1].strip()
                repo["full_name"] = f"{repo['author']}/{repo['name']}"
            else:
                repo["full_name"] = full_name
                repo["author"] = ""
                repo["name"] = full_name
            repo["url"] = f"https://github.com/{repo['full_name']}"
        else:
            repo["full_name"] = "Unknown"
            repo["author"] = ""
            repo["name"] = "Unknown"
            repo["url"] = "#"

        # 描述
        desc = article.select_one("p")
        repo["description"] = desc.get_text(strip=True) if desc else "暂无描述"

        # 编程语言
        lang_span = article.select_one("[itemprop='programmingLanguage']")
        repo["language"] = lang_span.get_text(strip=True) if lang_span else ""
        repo["language_color"] = LANGUAGE_COLORS.get(repo["language"], "#8b949e")

        # Star 和 Fork
        repo["stars"] = ""
        repo["forks"] = ""
        for link in article.select("a.Link--muted"):
            href = link.get("href", "")
            text = link.get_text(strip=True).replace(",", "")
            if "/stargazers" in href:
                repo["stars"] = text
            elif "/forks" in href:
                repo["forks"] = text

        # 今日 Star 增量
        today_stars_span = article.select_one("span.d-inline-block.float-sm-right")
        if today_stars_span:
            today_text = today_stars_span.get_text(strip=True)
            numbers = re.findall(r"[\d,]+", today_text)
            repo["today_stars"] = numbers[0].replace(",", "") if numbers else "0"
        else:
            repo["today_stars"] = "0"

        # 贡献者头像
        repo["contributors"] = []
        for avatar in article.select("span.d-inline-block img.avatar")[:5]:
            repo["contributors"].append({
                "src": avatar.get("src", ""),
                "alt": avatar.get("alt", "contributor"),
            })

        repo_list.append(repo)
        print(f"  📦 #{rank} {repo['full_name']} ⭐{repo['stars']} (+{repo['today_stars']})")

    return repo_list


# ═══════════════════════════════════════
# 数据存储
# ═══════════════════════════════════════
def smart_fetch_all(logger=None) -> tuple[dict, dict]:
    """智能调度：daily 必抓；weekly/monthly 仅在更新日或缓存过期时抓取。

    Returns:
        all_data:  {"daily": [...], "weekly": [...], "monthly": [...]}
        cache_info: {"daily": {"source": "实时抓取|缓存", "updated": "2026-06-23 21:00"},
                     "weekly": {...}, "monthly": {...}}
    """
    log = logger or setup_logging()
    today = datetime.now()
    weekday = today.weekday()

    all_data: dict = {}
    cache_info: dict = {}

    # ---- 日榜：每天必抓 ----
    print("\n📅 [日榜 daily] 每日必抓，正在抓取...")
    log.info("[调度] 日榜 daily 开始抓取")
    html = fetch_trending_page("daily")
    daily_repos = parse_trending(html, top_n=10)
    if translate_repo_descriptions is not None:
        try:
            daily_repos = translate_repo_descriptions(daily_repos, to_lang="zh")
        except Exception as e:
            log.warning(f"[调度] daily 翻译失败: {e}")
    all_data["daily"] = daily_repos
    cache_info["daily"] = {
        "source": "实时抓取",
        "updated": today.strftime("%Y-%m-%d %H:%M"),
        "window_label": WINDOW_LABELS["daily"],
        "window_badge": WINDOW_BADGES["daily"],
    }

    # ---- 周榜 / 月榜：按更新日抓，否则读缓存 ----
    week_targets = [
        ("weekly", WEEKLY_UPDATE_DAY, WEEKLY_MAX_CACHE_DAYS),
        ("monthly", MONTHLY_UPDATE_DAY, MONTHLY_MAX_CACHE_DAYS),
    ]
    for since, update_day, max_age in week_targets:
        is_update_day = (weekday == update_day)
        expired = is_cache_expired(since, max_age) if is_cache_expired else True
        need_fetch = is_update_day or expired

        if need_fetch:
            reason = "今天是更新日" if is_update_day else (
                "缓存不存在" if not load_cache(since)[0] else f"缓存超过 {max_age} 天"
            )
            print(f"\n📈 [{WINDOW_LABELS[since]} {since}] {reason}，正在抓取...")
            log.info(f"[调度] {since} 触发抓取：{reason}")
            try:
                html = fetch_trending_page(since)
                repos = parse_trending(html, top_n=10)
                if translate_repo_descriptions is not None:
                    try:
                        repos = translate_repo_descriptions(repos, to_lang="zh")
                    except Exception as e:
                        log.warning(f"[调度] {since} 翻译失败: {e}")
                if save_cache:
                    save_cache(since, repos)
                all_data[since] = repos
                cache_info[since] = {
                    "source": "实时抓取",
                    "updated": today.strftime("%Y-%m-%d %H:%M"),
                    "window_label": WINDOW_LABELS[since],
                    "window_badge": WINDOW_BADGES[since],
                }
            except Exception as e:
                log.error(f"[调度] {since} 抓取失败: {e}，尝试回退到缓存")
                cached, updated_at = (load_cache(since) if load_cache else (None, None))
                if cached:
                    all_data[since] = cached
                    cache_info[since] = {
                        "source": f"缓存（抓取失败回退）",
                        "updated": updated_at or "未知",
                        "window_label": WINDOW_LABELS[since],
                        "window_badge": WINDOW_BADGES[since],
                    }
                else:
                    # 没缓存也没抓到 → 给个空列表占位
                    all_data[since] = []
                    cache_info[since] = {
                        "source": "抓取失败且无缓存",
                        "updated": "—",
                        "window_label": WINDOW_LABELS[since],
                        "window_badge": WINDOW_BADGES[since],
                    }
        else:
            cached, updated_at = load_cache(since) if load_cache else (None, None)
            if cached is None:
                # 防御：理论上 is_cache_expired 已覆盖，但兜底再抓一次
                log.warning(f"[调度] {since} 缓存为空，强制抓取")
                try:
                    html = fetch_trending_page(since)
                    repos = parse_trending(html, top_n=10)
                    if translate_repo_descriptions is not None:
                        repos = translate_repo_descriptions(repos, to_lang="zh")
                    if save_cache:
                        save_cache(since, repos)
                    all_data[since] = repos
                    cache_info[since] = {
                        "source": "实时抓取",
                        "updated": today.strftime("%Y-%m-%d %H:%M"),
                        "window_label": WINDOW_LABELS[since],
                        "window_badge": WINDOW_BADGES[since],
                    }
                    continue
                except Exception as e:
                    log.error(f"[调度] {since} 兜底抓取也失败: {e}")
                    all_data[since] = []
                    cache_info[since] = {
                        "source": "无缓存",
                        "updated": "—",
                        "window_label": WINDOW_LABELS[since],
                        "window_badge": WINDOW_BADGES[since],
                    }
                    continue
            age = cache_age_days(since) if cache_age_days else None
            age_str = f"{age} 天前" if age is not None else updated_at
            print(f"\n📈 [{WINDOW_LABELS[since]} {since}] 命中缓存（{age_str}）")
            all_data[since] = cached
            cache_info[since] = {
                "source": "缓存",
                "updated": updated_at,
                "window_label": WINDOW_LABELS[since],
                "window_badge": WINDOW_BADGES[since],
            }

    return all_data, cache_info


def save_json(data: list[dict], date_str: str) -> Path:
    today = datetime.strptime(date_str, "%Y-%m-%d")
    month_dir = DATA_DIR / str(today.year) / f"{today.month:02d}"
    month_dir.mkdir(parents=True, exist_ok=True)
    json_path = month_dir / f"{date_str}.json"
    payload = {
        "date": date_str,
        "fetch_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": GITHUB_TRENDING_URL,
        "repos": data,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"  💾 JSON已保存: {json_path}")
    return json_path


# ═══════════════════════════════════════
# HTML 渲染
# ═══════════════════════════════════════
def render_daily_html(all_data: dict, cache_info: dict, date_str: str,
                     fetch_time: str = None) -> Path:
    """渲染每日 HTML（包含 daily / weekly / monthly 三个 Tab）。

    all_data:   {"daily": [...], "weekly": [...], "monthly": [...]}
    cache_info: {"daily": {...}, "weekly": {...}, "monthly": {...}}
    """
    today = datetime.strptime(date_str, "%Y-%m-%d")
    month_dir = PAGES_DIR / str(today.year) / f"{today.month:02d}"
    month_dir.mkdir(parents=True, exist_ok=True)
    html_path = month_dir / f"{date_str}.html"

    # 前后日导航
    prev_date = today - timedelta(days=1)
    next_date = today + timedelta(days=1)
    prev_path = f"../../{prev_date.year}/{prev_date.month:02d}/{prev_date.strftime('%Y-%m-%d')}.html"
    next_path = f"../../{next_date.year}/{next_date.month:02d}/{next_date.strftime('%Y-%m-%d')}.html"
    prev_exists = (PAGES_DIR / str(prev_date.year) / f"{prev_date.month:02d}" / f"{prev_date.strftime('%Y-%m-%d')}.html").exists()
    next_exists = (PAGES_DIR / str(next_date.year) / f"{next_date.month:02d}" / f"{next_date.strftime('%Y-%m-%d')}.html").exists()

    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    display_date = f"{today.year}年{today.month}月{today.day}日 {weekdays[today.weekday()]}"
    iso_date = today.strftime("%Y-%m-%d")
    fetch_time = fetch_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 构建 Tab 数据：含每个窗口的仓库列表 + 元信息
    tabs = []
    for since in WINDOWS:
        repos = all_data.get(since, [])
        info = cache_info.get(since, {})
        is_realtime = info.get("source") == "实时抓取"
        tabs.append({
            "since": since,
            "label": info.get("window_label", WINDOW_LABELS[since]),
            "badge": info.get("window_badge", WINDOW_BADGES[since]),
            "source": info.get("source", "—"),
            "updated": info.get("updated", "—"),
            "is_realtime": is_realtime,
            "repos": repos,
        })

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    template = env.get_template("template_daily.html")
    rendered = template.render(
        tabs=tabs,
        date_str=date_str,
        display_date=display_date,
        iso_date=iso_date,
        fetch_time=fetch_time,
        prev_path=prev_path if prev_exists else None,
        next_path=next_path if next_exists else None,
        index_path="../../../index.html",
    )

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(rendered)
    print(f"  📄 每日HTML已生成: {html_path}")
    return html_path


def scan_all_dates() -> dict:
    """扫描 pages/ 目录构建 {年: {月: [日期字符串...]}} 树结构"""
    tree = {}
    if not PAGES_DIR.exists():
        return tree
    for year_dir in sorted(PAGES_DIR.iterdir(), reverse=True):
        if not year_dir.is_dir() or not year_dir.name.isdigit():
            continue
        year = int(year_dir.name)
        tree[year] = {}
        for month_dir in sorted(year_dir.iterdir(), reverse=True):
            if not month_dir.is_dir() or not month_dir.name.isdigit():
                continue
            month = int(month_dir.name)
            dates = []
            for html_file in sorted(month_dir.glob("*.html"), reverse=True):
                dates.append(html_file.stem)
            if dates:
                tree[year][month] = dates
    return tree


def render_index_html(date_tree: dict):
    total_days = sum(
        len(dates) for year_data in date_tree.values() for dates in year_data.values()
    )
    tree_data = []
    # 默认展开最近一年的最近一个月
    years_sorted = sorted(date_tree.keys(), reverse=True)
    first_year = years_sorted[0] if years_sorted else None
    first_month = (
        sorted(date_tree[first_year].keys(), reverse=True)[0] if first_year else None
    )

    for year in years_sorted:
        year_item = {"year": year, "months": [], "open": year == first_year}
        months_sorted = sorted(date_tree[year].keys(), reverse=True)
        for month in months_sorted:
            month_item = {
                "month": month,
                "month_str": f"{month:02d}",
                "dates": date_tree[year][month],
                "count": len(date_tree[year][month]),
                "open": (year == first_year and month == first_month),
            }
            year_item["months"].append(month_item)
        year_item["total"] = sum(m["count"] for m in year_item["months"])
        tree_data.append(year_item)

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    template = env.get_template("template_index.html")
    rendered = template.render(
        tree=tree_data,
        total_days=total_days,
        generated_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(rendered)
    print(f"  📋 索引页已更新: {INDEX_PATH}")


# ═══════════════════════════════════════
# 日志 & 巡检 & 告警
# ═══════════════════════════════════════
def setup_logging() -> "logging.Logger":
    """初始化日志：同时输出到控制台和 log/daily.log。返回 logger。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("github-trending")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 文件：UTF-8 追加
    fh = logging.FileHandler(DAILY_LOG, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # 控制台
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logger


def log_failure(date_str: str, error: str):
    """失败时追加写入 log/failures.log（持久失败记录，不被 rotate 覆盖）"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(FAILURES_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {date_str} 失败: {error}\n")


def verify_html(date_str: str) -> bool:
    """检查某日期的 HTML 文件是否完整存在（>1KB 视为有效）"""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    target = PAGES_DIR / str(d.year) / f"{d.month:02d}" / f"{date_str}.html"
    if not target.exists():
        return False
    try:
        return target.stat().st_size > 1024
    except OSError:
        return False


def has_any_history() -> bool:
    """判断项目是否已经有过抓取历史（用于项目首日跳过巡检）

    项目首日 = pages 里最晚的 HTML 日期 == 今天
    首日时"昨天"自然没有数据，不应该触发补抓。
    """
    if not PAGES_DIR.exists():
        return False
    latest = None
    for html_file in PAGES_DIR.rglob("*.html"):
        date_str = html_file.stem
        if re.match(r"\d{4}-\d{2}-\d{2}", date_str):
            if latest is None or date_str > latest:
                latest = date_str
    if latest is None:
        return False
    today = datetime.now().strftime("%Y-%m-%d")
    # 只有最晚日期 < 今天（说明 21:00 主任务至少已经成功跑过一次）才算"有历史"
    return latest < today


def send_alert(date_str: str, error: str):
    """用 PowerShell 弹一个 Windows 气泡通知（无需额外模块）"""
    if sys.platform != "win32":
        return
    title = "GitHub Trending 巡检失败"
    msg = f"{date_str} 数据补抓未成功：{error[:80]}"
    # 简单 PowerShell：使用 NotifyIcon 弹气泡，3 秒后自动消失
    ps_script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon = [System.Drawing.SystemIcons]::Warning; "
        "$n.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Warning; "
        f"$n.BalloonTipTitle = '{title}'; "
        f"$n.BalloonTipText = '{msg}'; "
        "$n.Visible = $true; "
        "$n.ShowBalloonTip(5000); "
        "Start-Sleep -Milliseconds 5500; "
        "$n.Dispose()"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        # PowerShell 失败（无 GUI session）就静默
        pass


def run_pipeline(target_date: str, mode: str = "main", logger=None) -> bool:
    """共用流水线：智能调度抓取（daily/weekly/monthly）→ 存 JSON → 渲染 HTML → 更新索引。

    mode = "main"   -> 主任务，弹浏览器
    mode = "check"  -> 巡检补抓，不弹浏览器
    """
    log = logger or setup_logging()
    log.info(f"[{mode}] 开始处理 {target_date}")

    try:
        all_data, cache_info = smart_fetch_all(log)

        # 日榜 JSON 单独存（保留原有归档路径）；周/月榜单原始数据已在 cache/ 里
        save_json(all_data.get("daily", []), target_date)

        # 打印本次运行统计
        print("\n📊 本次运行统计：")
        for since in WINDOWS:
            info = cache_info.get(since, {})
            print(f"  {since:8s} | {info.get('source', '?'):20s} | 更新于 {info.get('updated', '?')}")

        render_daily_html(all_data, cache_info, target_date)
        date_tree = scan_all_dates()
        render_index_html(date_tree)

        if mode == "main":
            today_html = PAGES_DIR / target_date[:4] / target_date[5:7] / f"{target_date}.html"
            _open_in_browser(str(today_html))

        log.info(f"[{mode}] {target_date} 处理完成 ✅")
        return True
    except Exception as e:
        log.error(f"[{mode}] {target_date} 处理失败: {e}")
        log_failure(target_date, str(e))
        return False


def cmd_check() -> int:
    """巡检：检查昨天的 HTML，缺失则补抓；18:00 仍失败则弹气泡告警。"""
    logger = setup_logging()
    now = datetime.now()
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    print("=" * 60)
    print(f"🛡️  GitHub Trending 巡检 - {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    logger.info(f"[巡检] 开始检查 {yesterday}")

    if not has_any_history():
        logger.info("[巡检] 项目首次运行（pages 目录为空），跳过巡检")
        return 0

    if verify_html(yesterday):
        size = (PAGES_DIR / yesterday[:4] / yesterday[5:7] / f"{yesterday}.html").stat().st_size
        logger.info(f"[巡检] {yesterday} HTML 已存在（{size} 字节），跳过")
        return 0

    logger.warning(f"[巡检] {yesterday} HTML 缺失，开始补抓...")
    ok = run_pipeline(yesterday, mode="check", logger=logger)

    if not ok and now.hour >= 18:
        send_alert(yesterday, "所有重试均失败，请人工介入")

    print("=" * 60)
    print(f"{'✅ 巡检通过' if ok else '❌ 巡检异常'}")
    print("=" * 60)
    return 0 if ok else 1


# ═══════════════════════════════════════
# 主流程
# ═══════════════════════════════════════
def main():
    print("=" * 60)
    print("🔥 GitHub Trending Daily - 每日热榜抓取器")
    print("=" * 60)
    today_str = datetime.now().strftime("%Y-%m-%d")
    print(f"\n📅 当前日期: {today_str}\n")

    ok = run_pipeline(today_str, mode="main")

    print("\n" + "=" * 60)
    print("✅ 全部完成！" if ok else "❌ 处理失败")
    print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(cmd_check())
    else:
        sys.exit(main())