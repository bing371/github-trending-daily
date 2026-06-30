# GitHub Trending Daily

每天 21:00 自动抓 GitHub Trending Top 10，翻译成中文，生成一份深色主题的 HTML 报告并在浏览器打开。每个项目还能点「🤖 让 AI 讲解」+「💬 和 AI 聊聊」——基于真实 README 内容回答。

零数据库、零云服务、零登录。跑在你自己电脑上。

---

## ✨ 核心特性

- **🕐 全自动** — Windows 任务计划 / Linux cron，每天 21:00 主抓 + 4 次巡检补抓，失败自动重试
- **🌐 中文翻译** — 百度翻译 API 把英文描述翻成中文（其他厂商可自行接入）
- **📊 三档窗口** — 24h / 7d / 30d 三个 Tab 一键切换，避开"日榜每天都是老面孔"问题
- **🤖 AI 讲解** — 每张卡片点一下，基于 README 给你讲"它是什么、对有什么用、怎么上手"
- **💬 AI 对话** — 针对单个项目多轮对话，可追问"能不能和我的工具集成""怎么部署"
- **🎨 网页端改设置** — 右上角 ⚙️ 设置，**不用碰文件**就能换 API Key / 改人设 / 切服务商
- **🔌 多家 LLM 兼容** — minimax / DeepSeek / Moonshot / 智谱 / 通义 / 千帆 / 零一万物 / OpenAI / Ollama / OpenRouter 都行（任何 OpenAI 兼容端点）

---

## 🚀 一键安装（Windows）

需要 Windows 10/11 + Python 3.9+。

```powershell
# 1. 克隆仓库
git clone https://github.com/你的用户名/github-trending-daily.git
cd github-trending-daily

# 2. 装依赖
pip install -r requirements.txt

# 3. 跑安装脚本（交互式：会让你填百度 Key + agent API Key）
powershell -ExecutionPolicy Bypass -File installer/install.ps1
```

安装脚本会：
1. 把项目复制到 `%LOCALAPPDATA%\GitHubTrending\`
2. 写 `.env`（含你的真实 key，**不会**上传任何地方）
3. 注册 5 个 Windows 计划任务（21:00 主任务 + 08/12/16/18 巡检）
4. 桌面创建「今日 GitHub 热榜」快捷方式

每天 21:00 浏览器会自动弹出当日报告。

## 🐧 Linux / macOS

```bash
git clone https://github.com/你的用户名/github-trending-daily.git
cd github-trending-daily
pip install -r requirements.txt
cp .env.example .env
nano .env  # 填入你的 key

# 跑一次试试
python run.py

# 装 crontab（每天 21:00）
crontab -e
# 加一行：
0 21 * * * cd /path/to/github-trending-daily && python run.py >> log/cron.log 2>&1
```

## 📋 你需要准备的两个 API Key

| Key | 在哪申请 | 用来干什么 |
|---|---|---|
| 百度翻译 APPID + 密钥 | [api.fanyi.baidu.com](https://api.fanyi.baidu.com/) | 翻译 GitHub 项目描述 |
| agent API Key | minimax / DeepSeek / 任何 OpenAI 兼容服务 | 讲解 + 对话 |

免费额度都够个人日常用。详细申请步骤见 [docs/baidu-api-guide.md](docs/baidu-api-guide.md)。

---

## 🎯 使用流程

1. 21:00 — 浏览器自动弹出当日报告
2. 想了解更多项目 → 点卡片右下「🤖 让 AI 讲解」
3. 还有疑问 → 在弹出的面板里输入问题，对话历史自动存盘
4. 切换时间窗口 → 点顶部 24h / 7d / 30d Tab
5. 改设置 → 右上角 ⚙️，人设 / 翻译 / agent API 全在网页改

---

## 📁 数据存放在哪（你的电脑本地）

```
github-trending-daily/
├── data/2026/07/2026-07-01.json   # 原始抓取数据 + 中文翻译
├── pages/2026/07/2026-07-01.html  # 生成的当日 HTML
├── cache/weekly_cache.json        # 周榜缓存（每周一更新一次）
├── data/chats/<owner>/<repo>.json # 你跟 AI 的对话历史（私有）
└── log/                            # 运行日志
```

所有文件都在你电脑，**没有任何数据上传到云**。

---

## 🗑️ 卸载

```powershell
powershell -ExecutionPolicy Bypass -File installer/uninstall.ps1
```

会删所有计划任务、快捷方式、安装目录。

---

## 🔧 进阶：手工跑

```bash
# 抓今天的榜单 + 生成 HTML
python run.py

# 只检查昨天的报告有没有缺失（巡检模式）
python run.py --check

# 强制刷新（即使缓存有效）
python run.py --force

# 抓三天前的（补抓某天）
python run.py --date 2026-06-28
```

---

## 🛡️ 隐私

- **不会上传任何东西** — 抓 GitHub Trending 是公开 API；调 LLM 只把项目 README 发出去
- **本地对话历史** — 你跟 AI 的对话存在 `data/chats/`，永远不出本机
- **API Key 安全** — `.env` 已被 `.gitignore` 排除；UI 输入的 key 不写日志

---

## 📄 License

MIT — 见 [LICENSE](LICENSE)。

---

## 🤝 二次开发

代码很简单：

- `run.py` — 主入口，抓数据 + 翻译 + 渲染 HTML
- `server.py` — 本地 HTTP 服务，给 HTML 提供 AI 讲解 + 对话
- `cache.py` — 周榜/月榜缓存策略
- `translator.py` — 百度翻译封装
- `template_daily.html` / `template_index.html` — HTML 模板（Jinja2）

想换翻译服务商？改 `translator.py`。想换 LLM？直接改 UI 里的下拉框（已经内置 11 个预设）。