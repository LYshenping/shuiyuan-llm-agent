# Shuiyuan LLM Agent

基于大语言模型（LLM）的上海交通大学水源社区（shuiyuan.sjtu.edu.cn）智能助手。支持网页版聊天、命令行和微信机器人三种使用方式，可自动登录水源社区、选课社区与教学信息服务网，实现论坛检索、帖子深度阅读、课程表/成绩查询、外部链接阅读、图片理解等功能。

## 功能特性

- **网页版聊天**：FastAPI + SSE 流式输出，多会话管理，历史记录持久化（SQLite），内置设置面板
- **论坛能力**：帖子搜索、浏览、深度阅读（超长楼分块总结）、按作者/标签检索
- **教学信息服务**：自动登录 jAccount，查询课程表、成绩（jwxt）
- **选课社区**：course.sjtu.plus 自动登录与信息查询
- **外部阅读**：`read_url` 抓取并总结外部网页正文
- **网络搜索**：可切换的联网搜索工具（`search_web`），支持在 Web 界面一键开关
- **图片理解**：主模型不支持视觉输入时，自动调用独立视觉模型（项目中使用qwen3.7-flash）将图片转为文字描述
- **长期记忆**：跨会话记忆用户偏好与关键事实
- **多入口**：CLI（`cli.py`）、Web（`web_app.py`）、微信机器人（`wechat_bot.py`）

## 技术栈

- Python 3.11+
- LLM：OpenAI 兼容 API（默认 DeepSeek `deepseek-v4-flash`，可在设置面板/环境变量中更换）
- Web：FastAPI + Uvicorn + SSE；前端为原生 HTML/JS（`static/index.html`）
- 数据：SQLite（历史记录）、JSON（配置与登录态）

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置

复制 `.env.example` 为 `.env`，至少填写 `DEEPSEEK_API_KEY`：

```bash
# Windows
copy .env.example .env

# macOS / Linux
cp .env.example .env
```

```ini
DEEPSEEK_API_KEY=sk-你的key
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-flash
```

可选配置（jAccount 登录、选课社区密码、视觉模型等）见 `.env.example` 内注释。
也可直接使用网页版设置面板，配置会保存到 `data/settings.json`（优先级最高）。

### 3. 启动

**网页版（推荐）**

```bash
python web_app.py
# 浏览器访问 http://127.0.0.1:8000
```

Windows 用户也可双击 `start_agent.bat` 一键启动并自动打开浏览器。

**命令行**

```bash
python cli.py
```

**微信机器人**

```bash
python wechat_bot.py
```

## 配置说明

配置优先级（从高到低）：`data/settings.json`（Web 设置面板）＞ 系统环境变量 ＞ `.env` 文件 ＞ 代码默认值。

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | LLM API Key（必须） | - |
| `LLM_BASE_URL` | LLM API 地址 | `https://api.deepseek.com` |
| `LLM_MODEL` | 主模型 | `deepseek-v4-flash` |
| `JACCOUNT_USERNAME` / `JACCOUNT_PASSWORD` | jAccount 登录凭据（自动登录教学信息服务网） | 可选 |
| `VISION_API_KEY` / `VISION_MODEL` | 视觉模型（图片理解） | 可选，默认关闭 |
| `MAX_AGENT_ROUNDS` | 单次提问最大工具调用轮数 | `10` |

完整配置项见 `.env.example`。

## 目录结构

```
shuiyuan-agent/
├── agent.py            # Agent 核心：会话循环、工具调度、Prompt 组装
├── tools.py            # 工具注册表：论坛搜索/阅读、课程表、选课社区等
├── llm.py              # LLM 客户端（OpenAI 兼容，含视觉模型支持）
├── forum_client.py     # 水源论坛 HTTP 客户端
├── web_fetcher.py      # 外部网页抓取（read_url / search_web）
├── auth.py / jaccount.py / jwxt.py / course_community.py  # 登录与教学服务
├── web_app.py          # 网页版后端（FastAPI + SSE）
├── static/index.html   # 网页版前端
├── cli.py              # 命令行入口
├── wechat_bot.py       # 微信机器人
├── config.py           # 全局配置管理
├── .env.example        # 环境变量模板（.env 已被 gitignore）
└── requirements.txt    # 依赖清单
```

## 合规与安全须知

> 本项目为**个人学习与本地使用**而开发的工具，并非上海交通大学水源社区（shuiyuan.sjtu.edu.cn）的官方项目，与水源社区及学校官方无关。

**合规声明**

- 水源社区对通过脚本、爬虫或其他自动化手段获取内容有明确的使用协议约束（参见论坛《水源社区协议》）。使用本项目前，请自行阅读并遵守相关条款。
- 本项目定位为**只读、低频、本地个人使用**：仅通过论坛公开接口读取信息（搜索、读帖、查询课表/成绩等），不包含发帖、回复、点赞等写操作。
- 使用者应自行评估并承担因自动化访问可能带来的账号风险（如限流 429、账号风控等）。请勿将本项目部署为公开服务供他人使用，请勿批量抓取内容后二次发布。

**使用约束（请遵守）**

- 保持 `REQUEST_DELAY` ≥ 1 秒（默认已设），不要调低或并发高频请求
- 仅限本地个人使用，不要对外开放部署
- 只读不写，不批量下载或转发他人发言内容
- 不要使用未公开接口或绕过论坛限流机制

**安全提示**

- `.env`、`data/`（含 API Key、Cookie）、`storage_state.json`（登录态）均已加入 `.gitignore`，请勿提交
- 请勿将任何 API Key、账号密码硬编码进代码或提交到公开仓库

## License

[MIT](LICENSE)
