"""
config.py — 全局配置管理。

配置来源（优先级从高到低）：
    1. data/settings.json（用户通过 Web 设置面板保存的覆盖项，最高优先级）；
    2. 系统/进程环境变量（如 DEEPSEEK_API_KEY）；
    3. 项目根目录 .env 文件（可选，配合 python-dotenv 自动加载）；
    4. 代码内默认值。
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from dotenv import load_dotenv

# 项目根目录（本文件所在目录）
BASE_DIR = Path(__file__).resolve().parent

# 用户通过 Web 设置面板保存的覆盖项文件（优先级最高）
SETTINGS_PATH = BASE_DIR / "data" / "settings.json"


@dataclass
class Config:
    """shuiyuan-agent 的配置项集合。"""

    # Web 设置面板可修改的字段白名单（字段名 -> 类型，用于保存时校验）
    SETTABLE_FIELDS: ClassVar[dict[str, type]] = {
        "llm_model": str,          # 模型名称（热生效）
        "llm_api_key": str,        # API Key（需重启生效）
        "llm_base_url": str,       # API 地址（需重启生效）
        "llm_timeout": float,      # LLM 请求超时秒数（需重启生效）
        "max_agent_rounds": int,   # 单次提问最大工具调用轮数（热生效）
        "history_window": int,     # 多轮对话保留轮数（热生效）
        "memory_enabled": bool,    # 是否开启长期记忆（热生效）
        "memory_max_chars": int,   # 长期记忆字数上限（热生效）
        "request_delay": float,    # 论坛请求间隔秒数（热生效）
        "search_limit": int,       # 搜索返回条数默认值（agent 未显式指定 limit 时生效，热生效）
        # ---- 视觉模型（图片理解）----
        "vision_enabled": bool,    # 图片理解总开关（阅读帖子/网页时自动用视觉模型描述图片，热生效）
        "vision_base_url": str,    # 视觉模型 API 地址（OpenAI 兼容，需重启生效）
        "vision_api_key": str,     # 视觉模型 API Key（需重启生效）
        "vision_model": str,       # 视觉模型名称（多模态，热生效）
        "vision_max_images": int,  # 单帖/单页最多自动理解的图片数（热生效）
    }

    # ---- 论坛相关 ----
    forum_url: str = "https://shuiyuan.sjtu.edu.cn"
    # 登录态 cookie 文件路径（Playwright 导出）
    state_file: Path = field(default_factory=lambda: BASE_DIR / "storage_state.json")

    # ---- LLM 相关（DeepSeek，OpenAI 兼容接口）----
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""          # 从环境变量 DEEPSEEK_API_KEY 读取
    llm_model: str = "deepseek-v4-flash"
    llm_timeout: float = 120.0     # 单次 LLM 请求超时（秒）

    # ---- 视觉模型（图片理解）相关 ----
    # 主 LLM（deepseek-v4-flash）不支持多模态，阅读帖子/网页遇到图片时，
    # 自动调用独立的视觉模型（默认阿里云百炼 qwen3.7-flash，OpenAI 兼容接口）
    # 将图片内容转为文字描述，再交给主 LLM 处理。
    vision_enabled: bool = True    # 图片理解总开关
    vision_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"  # 视觉模型 API 地址
    # 注：阿里云百炼推荐使用业务空间专属域名以获得更优性能：
    #   https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
    # 其中 {WorkspaceId} 为百炼控制台"业务空间详情"中的业务空间 ID
    vision_api_key: str = ""       # 视觉模型 API Key（从环境变量 VISION_API_KEY 读取；为空则不启用）
    vision_model: str = "qwen3.7-flash"  # 视觉模型名称（多模态）
    vision_max_images: int = 10    # 单帖/单页最多自动理解的图片数（超出部分保留 [图片: 描述](URL) 占位符）
    vision_description_chars: int = 200  # 单张图片文字描述的字符上限（控制上下文膨胀）

    # ---- Agent 行为相关 ----
    max_agent_rounds: int = 10     # 单次提问内最多执行的工具调用轮数（防失控）
    history_window: int = 5        # 多轮对话中保留的最近完整轮次数
    memory_enabled: bool = True    # 是否开启长期记忆（关闭后不传记忆给 LLM、也不自动更新）
    memory_max_chars: int = 2000   # 全局长期记忆的字数上限（超出截断）

    # ---- 论坛数据抓取相关 ----
    request_delay: float = 1.0     # 每次论坛 API 请求间隔（秒），避免触发限流
    search_limit: int = 10         # 搜索/浏览时返回给 LLM 的帖子条数默认值（agent 显式传 limit 时以 agent 为准）
    # read_topic 的单次读取字符预算。基于 deepseek-v4-flash 100 万 token 上下文
    # （中文约 0.7 token/字符），30 万字符 ≈ 20 万 token，约覆盖 5400 层的楼一次读完。
    read_budget_chars: int = 300000
    # 估算单层平均字符数（实测论坛平均约 55，取 100 留余量）。
    # 仅用于深度阅读触发前的"总字符预估"，不再决定 read_topic 的读取上限（改按实际字符截断）。
    est_chars_per_post: int = 100
    max_posts_per_topic: int = 5000  # LLM 显式传 max_posts 时的绝对上限（硬保护）
    max_topic_pages: int = 300       # 单帖最多翻页数（鲁棒性保护，防止接口异常导致无限翻页）

    # ---- 深度阅读（超长楼分块总结）相关 ----
    # 每块读取的字符预算（替代原层数制）：每块原文不超过该值。
    # 块数 = 总字符 ÷ 每块预算，1 万层楼（约 55 万字符）约 2 块。
    deep_read_block_chars: int = 300000
    deep_read_max_blocks: int = 200       # 最多处理的块数（超过则截断并提示）
    deep_read_max_chars_per_post: int = 1000  # 单层正文截断字符上限（控制 token）
    deep_read_summary_chars: int = 400    # 每块摘要的长度上限（字符）

    # ---- 外部链接阅读（read_url）相关 ----
    web_read_chars: int = 30000      # read_url 返回正文的字符上限（默认；agent 可显式传 max_chars）
    web_fetch_timeout: float = 15.0  # 单次网页抓取超时（秒），防慢速响应拖住 Agent
    web_max_response_bytes: int = 5 * 1024 * 1024  # 响应体大小上限（5MB），防内存耗尽


    @staticmethod
    def load_settings() -> dict:
        """
        读取用户在 Web 设置面板保存的覆盖项。

        读取失败（文件损坏等）时视为无覆盖，不影响主流程。

        :return: {字段名: 值, ...}
        """
        try:
            if SETTINGS_PATH.exists():
                raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return raw
        except Exception:  # noqa: BLE001 设置文件损坏不影响启动
            pass
        return {}

    @staticmethod
    def save_settings(overrides: dict) -> dict:
        """
        合并保存设置项到 data/settings.json（白名单校验在调用方完成）。

        :param overrides: {字段名: 值, ...}
        :return: 保存后的完整设置字典
        """
        current = Config.load_settings()
        current.update(overrides)
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(
            json.dumps(current, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return current

    @classmethod
    def load(cls) -> "Config":
        """从环境变量、.env 文件与 settings.json 加载配置，生成 Config 实例。"""
        # 加载项目根目录下的 .env（若存在）
        load_dotenv(BASE_DIR / ".env")

        cfg = cls(
            forum_url=os.environ.get("FORUM_URL", "https://shuiyuan.sjtu.edu.cn"),
            llm_base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
            llm_api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            llm_model=os.environ.get("LLM_MODEL", "deepseek-v4-flash"),
            llm_timeout=float(os.environ.get("LLM_TIMEOUT", "120")),
            max_agent_rounds=int(os.environ.get("MAX_AGENT_ROUNDS", "10")),
            history_window=int(os.environ.get("HISTORY_WINDOW", "5")),
            memory_max_chars=int(os.environ.get("MEMORY_MAX_CHARS", "2000")),
            request_delay=float(os.environ.get("REQUEST_DELAY", "1.0")),
            search_limit=int(os.environ.get("SEARCH_LIMIT", "10")),
            vision_enabled=os.environ.get("VISION_ENABLED", "true").lower() != "false",
            vision_base_url=os.environ.get("VISION_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            vision_api_key=os.environ.get("VISION_API_KEY", ""),
            vision_model=os.environ.get("VISION_MODEL", "qwen3.7-flash"),
            vision_max_images=int(os.environ.get("VISION_MAX_IMAGES", "10")),
            vision_description_chars=int(os.environ.get("VISION_DESCRIPTION_CHARS", "200")),
            read_budget_chars=int(os.environ.get("READ_BUDGET_CHARS", "300000")),
            est_chars_per_post=int(os.environ.get("EST_CHARS_PER_POST", "100")),
            max_posts_per_topic=int(os.environ.get("MAX_POSTS_PER_TOPIC", "5000")),
            max_topic_pages=int(os.environ.get("MAX_TOPIC_PAGES", "300")),
            deep_read_block_chars=int(os.environ.get("DEEP_READ_BLOCK_CHARS", "300000")),
            deep_read_max_blocks=int(os.environ.get("DEEP_READ_MAX_BLOCKS", "200")),
            deep_read_max_chars_per_post=int(os.environ.get("DEEP_READ_MAX_CHARS_PER_POST", "1000")),
            deep_read_summary_chars=int(os.environ.get("DEEP_READ_SUMMARY_CHARS", "400")),
            web_read_chars=int(os.environ.get("WEB_READ_CHARS", "30000")),
            web_fetch_timeout=float(os.environ.get("WEB_FETCH_TIMEOUT", "15")),
            web_max_response_bytes=int(os.environ.get(
                "WEB_MAX_RESPONSE_BYTES", str(5 * 1024 * 1024))),
        )

        # Web 设置面板的覆盖项优先级最高：叠加到已加载配置上
        for key, value in Config.load_settings().items():
            if key in Config.SETTABLE_FIELDS and value is not None:
                try:
                    setattr(cfg, key, Config.SETTABLE_FIELDS[key](value))
                except (TypeError, ValueError):
                    continue  # 类型转换失败则忽略该覆盖项
        return cfg

    def check_llm_ready(self) -> None:
        """检查 LLM 配置是否就绪，未就绪则给出明确指引。"""
        if not self.llm_api_key:
            raise RuntimeError(
                "未检测到 DEEPSEEK_API_KEY 环境变量。\n"
                "请先设置环境变量，例如在 PowerShell 中执行：\n"
                "    $env:DEEPSEEK_API_KEY='你的key'\n"
                "或在项目根目录创建 .env 文件，内容为：\n"
                "    DEEPSEEK_API_KEY=你的key\n"
            )
