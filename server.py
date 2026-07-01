"""
server.py - Local backend that proxies GitHub repo fetches to minimax.

Listens on http://127.0.0.1:8765 (NOT exposed to network).
Reads minimax_API_KEY from .env at startup.

Endpoints:
  POST /explain                       Body: {"repo": "user/name"}   Streams SSE
  GET  /chat/<owner>/<repo>           Returns saved conversation history JSON
  POST /chat/<owner>/<repo>           Body: {"content": "user message"}  Streams SSE
  DELETE /chat/<owner>/<repo>         Clears saved conversation
  GET  /health                        Returns {"status": "ok"}
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8765
README_MAX_CHARS = 8000
CHAT_DIRNAME = "chats"  # saved under <BASE_DIR>/data/chats/<owner>/<repo>.json
MAX_HISTORY_TURNS = 20  # cap to keep prompt size sane

# Agent API is OpenAI-compatible. We support any provider that speaks the
# OpenAI Chat Completions protocol (POST {base}/chat/completions with Bearer
# auth). For presets just change AGENT_* env vars — no code changes needed.
#
# Provider presets (set AGENT_PROVIDER, base/model auto-fill unless overridden):
#   minimax     api.minimaxi.com/v1                    MiniMax-M3      (default)
#   deepseek    api.deepseek.com/v1                    deepseek-chat
#   moonshot    api.moonshot.cn/v1                     moonshot-v1-8k
#   zhipu       open.bigmodel.cn/api/paas/v4           glm-4-flash
#   qwen        dashscope.aliyuncs.com/compatible-mode/v1  qwen-turbo
#   baiduqianfan qianfan.baidubce.com/v2               ernie-speed
#   yi          api.lingyiwanwu.com/v1                 yi-large
#   openai      api.openai.com/v1                      gpt-4o-mini
#   ollama      http://127.0.0.1:11434/v1              llama3.1
#   openrouter  openrouter.ai/api/v1                   openai/gpt-4o-mini
#   custom      <whatever the user types>              <whatever>
DEFAULT_AGENT = {
    "provider": "minimax",
    "api_base": "https://api.minimaxi.com/v1",
    "model": "MiniMax-M3",
}

PROVIDER_PRESETS = {
    "minimax":      {"api_base": "https://api.minimaxi.com/v1",        "model": "MiniMax-M3"},
    "deepseek":     {"api_base": "https://api.deepseek.com/v1",         "model": "deepseek-chat"},
    "moonshot":     {"api_base": "https://api.moonshot.cn/v1",          "model": "moonshot-v1-8k"},
    "zhipu":        {"api_base": "https://open.bigmodel.cn/api/paas/v4","model": "glm-4-flash"},
    "qwen":         {"api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-turbo"},
    "baiduqianfan": {"api_base": "https://qianfan.baidubce.com/v2",     "model": "ernie-speed"},
    "yi":           {"api_base": "https://api.lingyiwanwu.com/v1",     "model": "yi-large"},
    "openai":       {"api_base": "https://api.openai.com/v1",           "model": "gpt-4o-mini"},
    "ollama":       {"api_base": "http://127.0.0.1:11434/v1",          "model": "llama3.1"},
    "openrouter":   {"api_base": "https://openrouter.ai/api/v1",        "model": "openai/gpt-4o-mini"},
}


def get_agent_config() -> dict:
    """Resolve active agent provider config from env with fallback defaults.
    Read on every request so the web UI can hot-swap providers without restart."""
    provider = (os.environ.get("AGENT_PROVIDER") or DEFAULT_AGENT["provider"]).strip() or DEFAULT_AGENT["provider"]
    preset = PROVIDER_PRESETS.get(provider, {})
    api_base = (
        os.environ.get("AGENT_API_BASE")
        or preset.get("api_base")
        or DEFAULT_AGENT["api_base"]
    ).rstrip("/")
    model = (
        os.environ.get("AGENT_MODEL")
        or preset.get("model")
        or DEFAULT_AGENT["model"]
    )
    api_key = os.environ.get("AGENT_API_KEY") or os.environ.get("minimax_API_KEY") or ""
    return {"provider": provider, "api_base": api_base, "model": model, "api_key": api_key}

# Credentials are stored in .env (editable via the web UI /settings/credentials).
# We treat this file as the source of truth, with the running process's
# os.environ updated in-place after every save so subsequent fetches see new
# values without a restart.
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

# User profile is loaded from ./user_profile.md so end-users can swap the
# persona without touching Python. See user_profile.md for what to edit.
# The file can also be edited live via the /profile endpoints (web UI).
USER_PROFILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_profile.md")
USER_PROFILE_TEMPLATE = """\
---
# 这份文件是 AI 讲解 GitHub 项目时的「听众画像」。
# 它会被 server.py 读取，填到 system prompt 的「我的个人身份」一节。
#
# 想换人设？只改这一份文件就行。
# 把下面「---」分隔线之后的几行换成你自己的实际情况（职业、技术栈、关注方向、痛点），
# 改完保存，重启 server.py 即可生效。
# 文件用 UTF-8 编码保存，避免中文乱码。
# （分隔线以上的注释会被自动忽略，server.py 只读取分隔线以下的内容。）
---

- 职业：（填入你的职业）
- 技术栈：（填入你常用的技术）
- 当前关注方向：（填入你最近在关注的方向）
- 痛点：（填入你最想解决的问题）
"""


def load_user_profile() -> str:
    """Read the editable user-profile markdown. Returns the raw markdown body
    (frontmatter stripped). Falls back to a placeholder if file is missing."""
    if not os.path.exists(USER_PROFILE_PATH):
        return "（未配置个人身份。请在网页右上角 ⚙️ 设置里填一下。）"
    with open(USER_PROFILE_PATH, "r", encoding="utf-8") as f:
        text = f.read()
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:].lstrip("\n")
    return text.strip()


def get_user_profile() -> str:
    """Read profile on each call so live edits from the web UI take effect
    without restarting server.py."""
    return load_user_profile()


def save_user_profile(content: str) -> None:
    """Atomically write the new profile content. Validates frontmatter markers."""
    if not content.startswith("---"):
        content = "---\n（这里是说明区，server.py 会自动忽略两行 --- 之间的内容）\n---\n\n" + content
    # Ensure file ends with a newline
    if not content.endswith("\n"):
        content += "\n"
    tmp = USER_PROFILE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, USER_PROFILE_PATH)


# Cached at startup so /profile GET is fast; overwritten on POST.
USER_PROFILE_CACHE = load_user_profile()

EXPLAIN_SYSTEM_TEMPLATE = """\
# 角色定义

你是我的「GitHub日报解读员」。你的工作是：每天拿到GitHub trending排名前10的工具/项目后，帮我搞清楚每个东西到底是干嘛的，对我有没有用，怎么用。

# 我的个人身份

{user_profile}

# 工作规则

## 信息来源（最重要的规则）
- 你必须先通过实际访问/搜索获取到项目的真实信息（README、官方描述、star数、最近更新时间）后才能开口
- 如果某个项目你没能获取到有效信息，直接说"这个我没查到详细信息"，不要猜
- 禁止根据项目名字推测功能——名字叫"lightning"不代表它跟闪电有关系
- 你的每条解读必须能追溯到README或官方文档中的具体描述

## 输出风格
- 用大白话说，像跟朋友聊天一样解释
- 技术术语第一次出现时用括号加一句人话解释
- 不要写长篇大论，每个工具的解读控制在150-300字
- 不需要深度分析、不需要思维链、不需要学术论述，直接给结论

## 每个工具的解读结构（按这个格式输出）

### [序号]. 工具名 ⭐ star数
**一句话说清楚：** [这个东西是什么，一句话，不超过20字]
**它解决什么问题：** [用生活化的比喻解释它的核心价值]
**对我有没有用：** [结合我的个人身份，直接判断：🟢有用 / 🟡可能有用 / 🔴跟我无关]
**如果要用，怎么上手：** [如果判断为有用，给出最简单的第一步行动指引。如果跟我无关就写"跳过"。]

---

## 禁止事项
- 禁止在没有获取真实信息的情况下编造工具功能
- 禁止输出"让我来深入分析一下"之类的废话过渡句
- 禁止对每个工具都说"非常有用"——如果跟我无关就直说无关
- 禁止复制粘贴README原文不翻译——你的价值是翻译和适配，不是搬运原文
- **绝对不要**输出 emoji 标题、内心独白、过程性思考，也不要包 thinking 的 XML 标签。直接讲结论。
"""

CHAT_SYSTEM_TEMPLATE = """\
# 角色定义

你是我的「GitHub日报解读员」。除了给我做一次性讲解，我也会针对某个项目跟你多轮对话（比如问"怎么部署""能不能和 YY 集成"），你要基于这个项目本身的真实资料回答我。

# 我的个人身份

{user_profile}

# 仓库信息
仓库地址：https://github.com/{repo}
README（已截断到 {max_chars} 字）：
{readme}

# 工作规则

## 信息来源（最重要的规则）
- 严格基于上面的 README 内容来回答；README 没讲到的，明确说"README 没提到，我不确定"
- 不要瞎编 API、命令、参数
- 如果关键信息不知道，明确说"我没读到这部分"，**不要瞎编**

## 输出风格
- 像跟朋友聊天一样
- 用大白话，技术术语第一次出现时用括号加一句人话解释
- 简短，控制在 200 字以内
- 不要长篇大论、不要堆 markdown

## 禁止事项
- 不要复述 README 大段原文
- 不要说"这是一个非常优秀的开源项目"这种废话
- **绝对不要**输出 emoji 标题、内心独白、过程性思考，也不要包 thinking 的 XML 标签。直接讲结论。
"""


# Strip <think>...</think> blocks. drain() returns the longest safe prefix
# of buf (text before any unclosed think block). Anything inside an
# unclosed think stays buffered until the close arrives.
THINK_RE = re.compile(r"<(think|thinking)>.*?</(?:think|thinking)>", re.DOTALL)


def drain_safe_prefix(buf: str) -> str:
    pos = 0
    safe_end = 0
    while pos < len(buf):
        m_open = re.search(r"<(think|thinking)>", buf[pos:])
        if not m_open:
            safe_end = len(buf)
            break
        safe_end = pos + m_open.start()
        after_open = pos + m_open.end()
        m_close = re.search(r"</(think|thinking)>", buf[after_open:])
        if not m_close:
            break
        pos = after_open + m_close.end()
    return buf[:safe_end]


def load_env():
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


def update_env_file(updates: dict) -> None:
    """Rewrite .env atomically, replacing values for keys in `updates` and
    preserving every other line (comments, ordering, blank lines). Keys not
    already present get appended at the end under a 'Auto-updated' banner."""
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()

    seen = set()
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}\n")
                seen.add(key)
                continue
        out.append(line)

    missing = [k for k in updates if k not in seen]
    if missing:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append("\n# Auto-updated by settings UI\n")
        for k in missing:
            out.append(f"{k}={updates[k]}\n")

    tmp = ENV_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(out)
    os.replace(tmp, ENV_PATH)


def fetch_readme(repo: str):
    """Fetch README markdown. Returns (content, source_url) or (None, None).

    Strategy (in order):
      1. GitHub API (`/repos/{owner}/{repo}/readme` with Accept: vnd.github.raw)
         - Reliable even when local proxies hijack raw.githubusercontent.com hosts.
         - Unauthenticated: 60 req/hour per IP. Plenty for daily-trending usage.
      2. raw.githubusercontent.com fallback for repos where the API path 404s
         (e.g. README in a non-default branch or named README.txt/.rst).
    """
    user, name = repo.split("/", 1)
    # 1) GitHub API — preferred path
    api_url = f"https://api.github.com/repos/{user}/{name}/readme"
    try:
        req = Request(
            api_url,
            headers={
                "User-Agent": "github-trending-daily",
                "Accept": "application/vnd.github.raw",
            },
        )
        with urlopen(req, timeout=12) as r:
            content = r.read().decode("utf-8", errors="replace")
            if content:
                return content[:README_MAX_CHARS], api_url
    except (URLError, HTTPError, TimeoutError, OSError):
        pass

    # 2) raw.githubusercontent.com fallback (handles non-default README names)
    for branch in ("main", "master"):
        raw_url = f"https://raw.githubusercontent.com/{user}/{name}/{branch}/README.md"
        try:
            req = Request(raw_url, headers={"User-Agent": "github-trending-daily"})
            with urlopen(req, timeout=10) as r:
                content = r.read().decode("utf-8", errors="replace")
                return content[:README_MAX_CHARS], raw_url
        except (URLError, HTTPError, TimeoutError, OSError):
            continue
    return None, None


def chat_path(owner: str, repo: str) -> str:
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "data", CHAT_DIRNAME, owner, f"{repo}.json")


def load_chat(owner: str, repo: str) -> dict:
    """Return {repo, messages: [...], updated}. Empty if no history."""
    p = chat_path(owner, repo)
    if not os.path.exists(p):
        return {"repo": f"{owner}/{repo}", "messages": [], "updated": None}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"repo": f"{owner}/{repo}", "messages": [], "updated": None}


def save_chat(owner: str, repo: str, messages: list) -> None:
    p = chat_path(owner, repo)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    payload = {
        "repo": f"{owner}/{repo}",
        "messages": messages,
        "updated": datetime.now().isoformat(timespec="seconds"),
    }
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def clear_chat(owner: str, repo: str) -> None:
    p = chat_path(owner, repo)
    if os.path.exists(p):
        os.remove(p)


# ═══════════════════════════════════════
# /refresh 立即抓取（异步跑 run.py）
# ═══════════════════════════════════════
REFRESH_LOCK = threading.Lock()
REFRESH_STATE = {
    "running": False,
    "started_at": None,         # ISO timestamp
    "finished_at": None,
    "ok": None,                 # True / False / None while running
    "returncode": None,
    "log_tail": [],             # last ~30 lines of stdout
    "html_path": None,          # 成功后新生成的 HTML 绝对路径
    "html_url": None,           # 浏览器可访问的 file:// URL
    "trigger": "manual",        # 保留字段，便于以后区分 manual / scheduled
}
REFRESH_LOG_MAX = 30
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
RUN_SCRIPT = os.path.join(PROJECT_DIR, "run.py")


def _run_refresh_async():
    """Worker thread: 执行 python run.py，捕获输出，更新 REFRESH_STATE。"""
    REFRESH_STATE["log_tail"] = []
    try:
        # 在 Windows 上强制 UTF-8 输出，避免 emoji 编码炸
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW

        # 把当前进程环境拷给子进程，必要时强制注入 INSECURE
        # （server.py 不是从 run.bat 启动的，默认没有这个变量；
        #  而 run.py 依赖它来跳过 GitHub 证书校验。）
        child_env = os.environ.copy()
        if not child_env.get("GITHUB_TRENDING_INSECURE"):
            child_env["GITHUB_TRENDING_INSECURE"] = "1"

        proc = subprocess.Popen(
            [sys.executable, RUN_SCRIPT],
            cwd=PROJECT_DIR,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )

        # 流式读取输出（保留最后 REFRESH_LOG_MAX 行）
        for line in proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            tail = REFRESH_STATE["log_tail"]
            tail.append(line)
            if len(tail) > REFRESH_LOG_MAX:
                del tail[:len(tail) - REFRESH_LOG_MAX]
        proc.wait()
        REFRESH_STATE["returncode"] = proc.returncode
        REFRESH_STATE["ok"] = (proc.returncode == 0)

        if REFRESH_STATE["ok"]:
            # 找到今天生成的 HTML
            today = datetime.now()
            html_abs = os.path.join(
                PROJECT_DIR, "pages",
                f"{today.year}", f"{today.month:02d}", f"{today.strftime('%Y-%m-%d')}.html",
            )
            if os.path.exists(html_abs):
                REFRESH_STATE["html_path"] = html_abs
                REFRESH_STATE["html_url"] = "file:///" + html_abs.replace("\\", "/").lstrip("/")
    except Exception as e:
        REFRESH_STATE["log_tail"].append(f"[server] 启动失败: {e}")
        REFRESH_STATE["ok"] = False
        REFRESH_STATE["returncode"] = -1
    finally:
        REFRESH_STATE["running"] = False
        REFRESH_STATE["finished_at"] = datetime.now().isoformat(timespec="seconds")


def start_refresh() -> dict:
    """同步入口：尝试启动抓取。返回状态快照。"""
    with REFRESH_LOCK:
        if REFRESH_STATE["running"]:
            return {"status": "busy", "state": _refresh_state_snapshot()}
        REFRESH_STATE["running"] = True
        REFRESH_STATE["started_at"] = datetime.now().isoformat(timespec="seconds")
        REFRESH_STATE["finished_at"] = None
        REFRESH_STATE["ok"] = None
        REFRESH_STATE["returncode"] = None
        REFRESH_STATE["html_path"] = None
        REFRESH_STATE["html_url"] = None
        REFRESH_STATE["log_tail"] = []
        t = threading.Thread(target=_run_refresh_async, daemon=True)
        t.start()
    return {"status": "started", "state": _refresh_state_snapshot()}


def _refresh_state_snapshot() -> dict:
    return {
        "running": REFRESH_STATE["running"],
        "started_at": REFRESH_STATE["started_at"],
        "finished_at": REFRESH_STATE["finished_at"],
        "ok": REFRESH_STATE["ok"],
        "returncode": REFRESH_STATE["returncode"],
        "log_tail": list(REFRESH_STATE["log_tail"]),
        "html_path": REFRESH_STATE["html_path"],
        "html_url": REFRESH_STATE["html_url"],
    }


def parse_repo_from_path(path: str):
    """Parse /chat/<owner>/<repo> → ("owner", "repo") or None."""
    parts = path.strip("/").split("/")
    if len(parts) == 3 and parts[0] == "chat" and parts[1] and parts[2]:
        owner = parts[1]
        repo = parts[2]
        if "/" in repo or owner.startswith("/") or owner.endswith("/"):
            return None
        return owner, repo
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        if self.path == "/refresh/status":
            self._send_json(200, _refresh_state_snapshot())
            return
        if self.path == "/profile":
            self._handle_get_profile()
            return
        if self.path == "/settings/credentials":
            self._handle_get_credentials()
            return
        parsed = parse_repo_from_path(self.path)
        if parsed and parsed[1]:  # GET /chat/owner/repo
            owner, repo = parsed
            data = load_chat(owner, repo)
            self._send_json(200, data)
            return
        self.send_response(404)
        self._cors()
        self.end_headers()

    def do_DELETE(self):
        parsed = parse_repo_from_path(self.path)
        if parsed and parsed[1]:
            owner, repo = parsed
            clear_chat(owner, repo)
            self._send_json(200, {"status": "cleared", "repo": f"{owner}/{repo}"})
            return
        self.send_response(404)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if self.path == "/explain":
            self._handle_explain()
            return
        if self.path == "/refresh":
            self._send_json(200, start_refresh())
            return
        if self.path == "/profile":
            self._handle_save_profile()
            return
        if self.path == "/settings/credentials":
            self._handle_save_credentials()
            return
        parsed = parse_repo_from_path(self.path)
        if parsed and parsed[1]:
            owner, repo = parsed
            self._handle_chat(owner, repo)
            return
        self.send_response(404)
        self._cors()
        self.end_headers()

    # ---------- streaming helpers ----------

    def _open_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()

    def _emit(self, payload: dict):
        line = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        try:
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            raise

    def _stream_agent(self, messages: list, temperature: float = 0.7, max_tokens: int = 1500):
        """Stream a chat completion from the configured OpenAI-compatible provider.
        Yields cleaned text deltas via self._emit. Returns the accumulated assistant
        text (with think blocks stripped), or None on error."""
        cfg = get_agent_config()
        if not cfg["api_key"]:
            self._emit({"type": "error", "message": f"agent API key not set (provider={cfg['provider']})"})
            return None

        api_body = {
            "model": cfg["model"],
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        api_req = Request(
            f"{cfg['api_base']}/chat/completions",
            data=json.dumps(api_body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
            },
        )

        strip_buf = ""
        assistant_text = ""
        try:
            with urlopen(api_req, timeout=60) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content")
                    if content:
                        strip_buf += content
                        safe_prefix = drain_safe_prefix(strip_buf)
                        if safe_prefix:
                            cleaned = THINK_RE.sub("", safe_prefix)
                            if cleaned:
                                self._emit({"type": "delta", "content": cleaned})
                                assistant_text += cleaned
                            strip_buf = strip_buf[len(safe_prefix):]
        except (URLError, HTTPError, TimeoutError, OSError) as e:
            self._emit({"type": "error", "message": f"upstream error: {e}"})
            return None
        except (BrokenPipeError, ConnectionResetError):
            return None

        # Discard any leftover buffer (inside unclosed think).
        return assistant_text

    # ---------- /profile (read & live-edit the user persona) ----------

    def _handle_get_profile(self):
        """Return raw user_profile.md content. Frontend strips frontmatter for editing."""
        if not os.path.exists(USER_PROFILE_PATH):
            self._send_json(200, {"content": USER_PROFILE_TEMPLATE, "exists": False})
            return
        with open(USER_PROFILE_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        self._send_json(200, {"content": content, "exists": True})

    def _handle_save_profile(self):
        """Save new profile content. Validates non-empty body and size cap."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 8192:
                self._send_json(413, {"error": "profile too large (max 8KB)"})
                return
            body = json.loads(self.rfile.read(length) if length else b"{}")
            content = body.get("content")
            if not isinstance(content, str) or not content.strip():
                self._send_json(400, {"error": "content is required and must be non-empty"})
                return
            save_user_profile(content)
            self._send_json(200, {"status": "saved"})
        except OSError as e:
            self._send_json(500, {"error": f"save failed: {e}"})
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})

    # ---------- /settings/credentials (Baidu + multi-provider agent) ----------

    def _handle_get_credentials(self):
        """Return current values from os.environ (not the file), so what you see
        is exactly what the server is using. Missing keys show as empty string."""
        cfg = get_agent_config()
        self._send_json(200, {
            "translation_provider": os.environ.get("TRANSLATION_PROVIDER", "baidu") or "baidu",
            "baidu_appid": os.environ.get("BAIDU_APPID", "") or "",
            "baidu_key": os.environ.get("BAIDU_TRANSLATE_KEY", "") or "",
            "agent_provider": cfg["provider"],
            "agent_api_base": cfg["api_base"],
            "agent_api_key": cfg["api_key"],
            "agent_model": cfg["model"],
            "env_path": ENV_PATH,
        })

    def _handle_save_credentials(self):
        """Update .env with the submitted keys/fields. Empty string means
        'leave the existing value alone'. Also pushes the new values into
        os.environ so the running session picks them up without restart."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 8192:
                self._send_json(413, {"error": "payload too large"})
                return
            body = json.loads(self.rfile.read(length) if length else b"{}")
        except Exception as e:
            self._send_json(400, {"error": f"bad json: {e}"})
            return

        updated = {}
        # Translation
        tp = body.get("translation_provider")
        if isinstance(tp, str) and tp.strip():
            updated["TRANSLATION_PROVIDER"] = tp.strip().lower()
        # Baidu creds
        for key, envname in (
            ("baidu_appid", "BAIDU_APPID"),
            ("baidu_key", "BAIDU_TRANSLATE_KEY"),
        ):
            val = body.get(key)
            if val is None or val == "":
                continue
            if not isinstance(val, str):
                self._send_json(400, {"error": f"{key} must be string"})
                return
            updated[envname] = val.strip()
        # Agent provider block
        ap = body.get("agent_provider")
        if isinstance(ap, str) and ap.strip():
            updated["AGENT_PROVIDER"] = ap.strip().lower()
        for key, envname in (
            ("agent_api_base", "AGENT_API_BASE"),
            ("agent_api_key", "AGENT_API_KEY"),
            ("agent_model", "AGENT_MODEL"),
        ):
            val = body.get(key)
            if val is None or val == "":
                continue
            if not isinstance(val, str):
                self._send_json(400, {"error": f"{key} must be string"})
                return
            updated[envname] = val.strip()
        # Back-compat: if user updates API key under new name, also mirror to old
        # name so any stale callers (run.py, third-party scripts) still work.
        if "AGENT_API_KEY" in updated and not os.environ.get("minimax_API_KEY"):
            updated["minimax_API_KEY"] = updated["AGENT_API_KEY"]

        if not updated:
            self._send_json(200, {"status": "no-op", "updated": []})
            return

        try:
            update_env_file(updated)
        except OSError as e:
            self._send_json(500, {"error": f"save failed: {e}"})
            return

        for k, v in updated.items():
            os.environ[k] = v

        self._send_json(200, {"status": "saved", "updated": sorted(updated.keys())})

    # ---------- /explain ----------

    def _handle_explain(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) if length else b"{}")
            repo = (body.get("repo") or "").strip()
        except Exception as e:
            self.send_response(400)
            self._cors()
            self.end_headers()
            self.wfile.write(f"bad request: {e}".encode())
            return

        if "/" not in repo or repo.count("/") != 1 or repo.startswith("/") or repo.endswith("/"):
            self.send_response(400)
            self._cors()
            self.end_headers()
            self.wfile.write(b'invalid repo: expected "user/name"')
            return

        readme, readme_url = fetch_readme(repo)
        user_msg = f"仓库地址：https://github.com/{repo}\n\n"
        if readme:
            user_msg += (
                f"README（已截断到 {README_MAX_CHARS} 字）：\n\n"
                f"{readme}\n\n---\n\n"
                f"请按 SYSTEM 提示讲解。"
            )
        else:
            user_msg += (
                "README fetch 失败。请凭你对这个仓库的了解讲解；"
                "如果关键信息不知道，明确说。\n\n请按 SYSTEM 提示讲解。"
            )

        self._open_sse()
        self._emit({"type": "start", "repo": repo, "readme_url": readme_url})
        self._stream_agent([
            {"role": "system", "content": EXPLAIN_SYSTEM_TEMPLATE.format(user_profile=get_user_profile())},
            {"role": "user", "content": user_msg},
        ])
        self._emit({"type": "done"})

    # ---------- /chat/<owner>/<repo> ----------

    def _handle_chat(self, owner: str, repo: str):
        # Parse user message
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) if length else b"{}")
            user_content = (body.get("content") or "").strip()
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        if not user_content:
            self._send_json(400, {"error": "content is required"})
            return

        # Load existing history + README
        history_data = load_chat(owner, repo)
        history = history_data.get("messages", [])
        readme, readme_url = fetch_readme(f"{owner}/{repo}")

        # Cap history to last N turns (each turn = user + assistant)
        # Take last MAX_HISTORY_TURNS * 2 messages
        if len(history) > MAX_HISTORY_TURNS * 2:
            history = history[-MAX_HISTORY_TURNS * 2:]

        # Append the new user message to history (in-memory)
        history.append({"role": "user", "content": user_content})

        system_prompt = CHAT_SYSTEM_TEMPLATE.format(
            user_profile=get_user_profile(),
            repo=f"{owner}/{repo}",
            max_chars=README_MAX_CHARS,
            readme=readme if readme else "(README fetch 失败 — 凭印象回答，关键信息请说\"README 没提到，我不确定\")",
        )

        messages = [{"role": "system", "content": system_prompt}] + history

        self._open_sse()
        self._emit({"type": "start", "repo": f"{owner}/{repo}", "readme_url": readme_url})

        assistant_text = self._stream_agent(messages, temperature=0.6, max_tokens=1200)

        if assistant_text is not None:
            history.append({"role": "assistant", "content": assistant_text})
            try:
                save_chat(owner, repo, history)
            except OSError as e:
                self._emit({"type": "warning", "message": f"save failed: {e}"})
            self._emit({"type": "done", "saved_messages": len(history)})
        else:
            self._emit({"type": "done", "saved_messages": 0})


def main() -> int:
    load_env()
    cfg = get_agent_config()
    if not cfg["api_key"]:
        print(
            f"[server] ERROR: agent API key missing in .env "
            f"(provider={cfg['provider']}, model={cfg['model']}). "
            f"Run the web UI ⚙️ 设置 → 🤖 agent API to configure.",
            file=sys.stderr, flush=True,
        )
        return 1

    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(
        f"[server] Listening on http://{LISTEN_HOST}:{LISTEN_PORT}  "
        f"(provider={cfg['provider']}, model={cfg['model']})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] Shutting down...", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())