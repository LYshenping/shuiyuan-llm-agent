"""
tools.py — Agent 可调用工具的定义与执行注册表。

工具列表（LLM 通过 function calling 调用）：
    1. search_forum         搜索论坛帖子
    2. read_topic           读取单帖内容（楼主 + 回复，超长楼截断；可指定 username 只看某人）
    3. browse_latest        浏览最新帖子
    4. deep_summarize_topic 深度阅读：分块读取整楼（支持数千层）并汇总总结
    5. list_tags            查看论坛全部标签
    6. list_categories      查看论坛版块列表
    7. get_user_topics      查看某用户发起的主题或回复过的帖子

深度阅读的上下文管理：
    - 工具内部按"楼层块"读取原文，每块用一次独立的 LLM 调用（干净上下文）生成块摘要；
    - 原文永不进入 Agent 主上下文，主上下文只累积小块摘要；
    - 最后将全部块摘要一次性交给 LLM 做整楼合并总结（Map-Reduce）。
"""

import base64
import json
import re
from typing import Any, Callable
from urllib.parse import urlparse

import requests

import course_community as cc
import jwxt as jw
from config import BASE_DIR, Config
from forum_client import ForumClient
from llm import LLMClient, TaskCancelled
from web_fetcher import SearchRateLimitedError, WebFetcher

# 预置外部网站库文件路径（data/web_sites.json，可手动编辑增删，实时生效）
WEB_SITES_PATH = BASE_DIR / "data" / "web_sites.json"

# 深度阅读：单块总结用的 System Prompt（每次块总结使用独立上下文）
DEEP_READ_BLOCK_PROMPT = """\
你是水源社区帖子阅读助手。请仔细阅读给定的楼层内容（楼主正文与回复），
提炼出其中的核心信息与主要观点。要求：
- 用简洁的要点形式输出，默认中文；
- 保留与"用户关注点"相关的细节，忽略无关的闲聊；
- 忠实原文，不要编造内容；如某层无实质内容（如纯表情/引用），可一句话带过；
- 输出控制在 300 字以内。
"""

# 深度阅读：最终合并总结用的 System Prompt
DEEP_READ_MERGE_PROMPT = """\
你是水源社区助手。以下是某个帖子的全部分块摘要（已按楼层顺序排列）。
请综合这些摘要，给出该帖的整楼总结。要求：
- 归纳整楼的主要观点、结论或信息脉络；
- 如果存在分歧/争论，如实呈现各方观点；
- 结合用户关注点（如有）重点回答；
- 使用中文，结构清晰（可用小标题/列表）。
"""

# 深度阅读：直接读全文后一次总结用的 System Prompt（容量感知分支，不走 Map-Reduce）
DEEP_READ_DIRECT_PROMPT = """\
你是水源社区助手。请仔细阅读给定的帖子全文，给出该帖的整楼总结。要求：
- 归纳整楼的主要观点、结论或信息脉络；
- 如果存在分歧/争论，如实呈现各方观点；
- 结合用户关注点（如有）重点回答；
- 忠实原文，不要编造内容；
- 使用中文，结构清晰（可用小标题/列表）。
"""

# 帖子/网页清洗时图片占位符格式：[图片: alt文本](完整URL)
# （forum_client._html_to_text 与 web_fetcher._parse_html 输出一致）
IMAGE_PLACEHOLDER_RE = re.compile(r"\[图片: ([^\]]*)\]\(([^)]+)\)")


def _resolve_limit(limit: int | None, default: int, hard_max: int = 50) -> int:
    """
    解析工具调用中的 limit 参数。

    未显式传 limit 时用 default（配置里的默认返回条数）；显式传入时尊重
    传入值，仅做正整数归一化与绝对硬保护（hard_max，防 agent 传超大数）。

    :param limit: LLM 显式传入的 limit（可为 None）
    :param default: 未传时的默认值（通常为 cfg.search_limit）
    :param hard_max: 绝对硬上限
    :return: 归一化后的 limit
    """
    if limit is None:
        limit = default
    return max(1, min(int(limit), hard_max))


def _format_topics(topics: list[dict], limit_excerpt: int = 200) -> str:
    """
    将帖子列表格式化为 LLM 友好的文本。

    :param topics: forum_client 返回的帖子摘要列表
    :param limit_excerpt: 摘要截断长度
    :return: 格式化后的文本
    """
    if not topics:
        return "（没有找到相关帖子）"
    lines = []
    for t in topics:
        excerpt = (t.get("excerpt") or "")[:limit_excerpt]
        lines.append(
            f"- [帖#{t['id']}] {t.get('title')}\n"
            f"  摘要: {excerpt}\n"
            f"  链接: {t.get('link')}\n"
            f"  发布时间: {t.get('created_at')}"
        )
    return "\n".join(lines)


def _topic_ref(topic_id: int, title: str | None) -> str:
    """
    构造帖子引用文本：标题非空且非 fallback 时返回 '#编号《标题》'，否则仅 '#编号'。

    供前端把 #编号 渲染为可点击链接、并展示帖子标题用。标题缺失（如读取前的日志）
    时退化为纯 #编号，前端仍会链接化。

    :param topic_id: 帖子 id
    :param title: 帖子标题（可能为 None 或 fallback 形如 '#编号'）
    :return: '#编号《标题》' 或 '#编号'
    """
    t = (title or "").strip()
    if not t or t == f"#{topic_id}":  # 兜底标题（deep_summarize 的 fallback）不重复
        return f"#{topic_id}"
    return f"#{topic_id}《{t}》"


def _format_topics_with_stats(topics: list[dict], limit_excerpt: int = 150) -> str:
    """
    将带热度统计的帖子列表格式化为 LLM 友好的文本（热门帖/标签帖用）。

    :param topics: forum_client 返回的帖子摘要列表（含 views/like_count 等）
    :param limit_excerpt: 摘要截断长度
    :return: 格式化后的文本
    """
    if not topics:
        return "（没有找到相关帖子）"
    lines = []
    for t in topics:
        excerpt = (t.get("excerpt") or "")[:limit_excerpt]
        stats = []
        if t.get("views") is not None:
            stats.append(f"浏览 {t['views']}")
        if t.get("like_count") is not None:
            stats.append(f"赞 {t['like_count']}")
        if t.get("posts_count") is not None:
            stats.append(f"{t['posts_count']} 层")
        stat_suffix = f"（{' · '.join(stats)}）" if stats else ""
        lines.append(
            f"- [帖#{t['id']}] {t.get('title')}{stat_suffix}\n"
            f"  摘要: {excerpt}\n"
            f"  链接: {t.get('link')}\n"
            f"  发布时间: {t.get('created_at')}"
        )
    return "\n".join(lines)


def _format_topic(topic: dict, max_chars_per_post: int | None = None,
                  username: str | None = None) -> str:
    """
    将单帖详情格式化为 LLM 友好的文本。

    :param topic: forum_client.get_topic / get_topic_range 的返回
    :param max_chars_per_post: 单层正文的字符截断上限（None 不截断）
    :param username: 可选：只看该用户的发言楼层（功能2，与 llm 只看某人）
    :return: 格式化后的文本
    """
    lines = [f"标题: {topic.get('title')}", f"链接: {topic.get('link')}"]
    shown = 0
    for p in topic.get("posts", []):
        if username and p.get("username") != username:
            continue  # 只看某人：跳过非目标用户的楼层
        role = "楼主" if p["post_number"] == 1 else f"回复{p['post_number']}"
        created = (p.get("created_at") or "")[:10]
        content = p.get("content") or ""
        if max_chars_per_post:
            content = content[:max_chars_per_post]
        # 楼层引用关系（功能3）：该层回复了第几层、回复了谁
        ref = ""
        if p.get("reply_to_post_number"):
            ref = f"（回复第{p['reply_to_post_number']}层"
            if p.get("reply_to_username"):
                ref += f" @{p['reply_to_username']}"
            ref += "）"
        lines.append(f"\n[{role}]{ref} {p.get('username')} ({created}):\n{content}")
        shown += 1
    if username and shown == 0:
        lines.append(f"\n（用户 {username} 在此帖没有发言记录）")
    return "\n".join(lines)


class ToolRegistry:
    """工具注册表：维护工具 schema 与 Python 实现的映射。"""

    # OpenAI 兼容的工具 schema 定义（供 LLM function calling 使用）
    SCHEMA: list[dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": "search_forum",
                "description": (
                    "在水源社区论坛搜索帖子，返回帖子标题、摘要和链接。"
                    "支持中文关键词全文检索，可多次调用不同关键词组合；"
                    "也可通过 tags 参数按标签（tag）筛选帖子。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词，如'计算机 毕业 去向'"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选：仅返回带这些标签的帖子，如 ['转专业']；先用 list_tags 查看论坛有哪些标签",
                        },
                        "limit": {"type": "integer", "description": "返回条数（默认 10），需要更多结果时可显式传更大值（如 30）"},
                        "page": {"type": "integer", "description": "起始页码（从 1 开始，1=第一页，每页约 50 条；如想看第 51 条及以后的结果可传 page=2），可选；通常无需指定，条数由 limit 控制"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_topic",
                "description": (
                    "直接读取某个帖子的正文内容（楼主和回复楼层）。"
                    "帖子在字符预算内（约 30 万字符，通常 5000 层以内的楼）会一次完整返回，"
                    "无需担心被截断。只有遇到极少数几千层以上的超长楼、或显式指定 max_posts 时"
                    "才会部分读取；若需总结超长楼整楼观点，或不想一次性塞入过多信息，可改用 deep_summarize_topic。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic_id": {"type": "integer", "description": "帖子 id（形如 12345，来自搜索结果）"},
                        "max_posts": {
                            "type": "integer",
                            "description": "可选：限制最多读取的楼层数。通常无需指定，预算内会完整读完整楼；仅在只想看前 N 层时指定",
                        },
                        "username": {
                            "type": "string",
                            "description": "可选：只看该用户的发言楼层（用户名是论坛登录名，如 ishuiyuan）。"
                                           "例如\"这个帖子里 X 说了什么\"时传入 X 的用户名，结果只包含该用户的楼层",
                        },
                    },
                    "required": ["topic_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browse_latest",
                "description": (
                    "浏览论坛最新帖子，可选按版块过滤或按标签（tag）过滤/排除标签，"
                    "用于了解当前热门话题。例如屏蔽『日记』标签看最新可传 exclude_tags=['日记']。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "description": "版块 slug 或 id（可选），如 '求职招聘'"},
                        "limit": {"type": "integer", "description": "返回条数（默认 10），需要更多结果时可显式传更大值（如 30）"},
                        "page": {"type": "integer", "description": "起始页码（从 1 开始，1=第一页，每页约 30 条；如想看第 31 条及以后的结果可传 page=2），可选；通常无需指定，条数由 limit 控制"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选：仅返回带这些标签的帖子，如 ['日记']",
                        },
                        "exclude_tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选：排除带这些标签的帖子，如 ['日记']",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_tags",
                "description": (
                    "查看论坛全部标签（tag）及每个标签的使用数量（按使用数量降序）。"
                    "在需要按标签筛选帖子（search_forum 的 tags、browse_latest 的 tags/exclude_tags）"
                    "之前，可先调用本工具了解论坛上有哪些标签可用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "deep_summarize_topic",
                "description": (
                    "深度阅读：分块读取帖子的全部楼层（支持数千层的大楼），逐块总结后给出整楼综合摘要。"
                    "适用于帖子楼层很多（超过约100层）、需要了解整楼内容或主要观点的场景。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic_id": {"type": "integer", "description": "帖子 id"},
                        "focus": {"type": "string", "description": "可选：用户关心的重点或问题，摘要会围绕该重点"},
                    },
                    "required": ["topic_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_categories",
                "description": (
                    "查看论坛全部版块（category）列表。当用户想按版块浏览/筛选内容"
                    "（如'看看求职招聘版块'），可先调用本工具了解论坛有哪些版块。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_user_topics",
                "description": (
                    "查看某个用户在水源社区发起的主题（帖子）或回复过的帖子，返回标题、摘要和链接。"
                    "适用于'看看 XX 发过什么''XX 的保研/求职经验'等按用户检索的场景。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "username": {"type": "string", "description": "用户名（Discourse 登录名）"},
                        "action_type": {
                            "type": "string",
                            "enum": ["topics", "replies"],
                            "description": "可选：topics=该用户发起的主题（默认），replies=该用户回复过的帖子",
                        },
                        "limit": {"type": "integer", "description": "返回条数（默认 10），需要更多结果时可显式传更大值（如 30）"},
                        "page": {"type": "integer", "description": "起始页码（从 1 开始，1=第一页，每页约 30 条；如想看第 31 条及以后的结果可传 page=2），可选；通常无需指定，条数由 limit 控制"},
                    },
                    "required": ["username"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_user_profile",
                "description": (
                    "查看某个用户的公开资料与统计信息，包括注册时间、等级、徽章数、个人简介，"
                    "以及发帖数、获赞数（收到/给出）、在线天数、阅读帖子数、已解决数等统计。"
                    "适用于'这个人是什么来头''这位学长/学姐靠谱吗''XX 在水源的活跃度怎么样'等"
                    "了解用户身份与资历的场景。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "username": {"type": "string", "description": "用户名（Discourse 登录名）"},
                    },
                    "required": ["username"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browse_top",
                "description": (
                    "查看论坛热门帖子（按日/周/月/年统计），返回标题、摘要、链接及浏览量、点赞数、楼层数。"
                    "适用于'最近有什么热门话题''这周/本月最火的帖子''大家都在看什么'等了解论坛热度的场景。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "period": {
                            "type": "string",
                            "enum": ["daily", "weekly", "monthly", "yearly", "all"],
                            "description": "统计周期：daily=今日，weekly=本周（默认），monthly=本月，yearly=今年，all=历史全部",
                        },
                        "limit": {"type": "integer", "description": "返回条数（默认 10），需要更多结果时可显式传更大值（如 30）"},
                        "page": {"type": "integer", "description": "起始页码（从 1 开始，1=第一页，每页约 50 条；如想看第 51 条及以后的结果可传 page=2），可选；通常无需指定，条数由 limit 控制"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_time_range",
                "description": (
                    "按时间范围搜索论坛帖子（按主题创建时间过滤）。"
                    "适用于'近半年关于转专业的帖子''2024 年的保研经验'等有时间区间要求的检索。"
                    "时间格式为 YYYY-MM-DD，可只给起始或只给结束。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词，如'保研'"},
                        "start_date": {"type": "string", "description": "起始日期（含），格式 YYYY-MM-DD，可选"},
                        "end_date": {"type": "string", "description": "结束日期（含），格式 YYYY-MM-DD，可选"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选：仅返回带这些标签的帖子，如 ['转专业']",
                        },
                        "limit": {"type": "integer", "description": "返回条数（默认 10），需要更多结果时可显式传更大值（如 30）"},
                        "page": {"type": "integer", "description": "起始页码（从 1 开始，1=第一页，每页约 50 条；如想看第 51 条及以后的结果可传 page=2），可选；通常无需指定，条数由 limit 控制"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_topic_likes",
                "description": (
                    "获取某个帖子的热度统计：浏览量、点赞数、回复数、总楼层数、创建与最后活动时间。"
                    "适用于'这个帖子有多少人看''这个帖子火不火''有多少点赞'等只关心热度数据、"
                    "不需要读取帖子正文内容的场景。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic_id": {"type": "integer", "description": "帖子 id（形如 12345，来自搜索结果）"},
                    },
                    "required": ["topic_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_tag_topics",
                "description": (
                    "浏览某个标签（tag）下的帖子列表，返回标题、摘要、链接及浏览量、楼层数。"
                    "适用于'看看转专业标签下有什么''这个标签下最近有什么新帖'等按标签浏览内容的场景。"
                    "先用 list_tags 查看论坛有哪些标签。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tag": {"type": "string", "description": "标签名（来自 list_tags）"},
                        "limit": {"type": "integer", "description": "返回条数（默认 10），需要更多结果时可显式传更大值（如 30）"},
                        "page": {"type": "integer", "description": "起始页码（从 1 开始，1=第一页，每页约 30 条；如想看第 31 条及以后的结果可传 page=2），可选；通常无需指定，条数由 limit 控制"},
                    },
                    "required": ["tag"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_similar_topics",
                "description": (
                    "查找与某个帖子相似或相关的帖子，用于'还有没有类似的帖子''相关的讨论有哪些'"
                    "'这事的其他说法'等场景。优先使用论坛内置的相关帖子推荐；"
                    "若论坛无推荐，则用帖子标题作为关键词搜索兜底。返回标题、链接与热度统计。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic_id": {"type": "integer", "description": "帖子 id（形如 12345）"},
                        "limit": {"type": "integer", "description": "返回条数（默认 10），需要更多结果时可显式传更大值（如 30）"},
                        "page": {"type": "integer", "description": "起始页码（从 1 开始，1=第一页，每页约 50 条），可选；仅当论坛无相关推荐、改用关键词搜索兜底时生效"},
                    },
                    "required": ["topic_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_url",
                "description": (
                    "阅读外部网页链接（联网浏览用户指定的网站内容），返回网页正文的纯文本/Markdown。"
                    "适用于用户给出论坛之外的链接（如校园手册、学长博客、官方文档、通知公告页等）"
                    "并想了解其内容的场景。"
                    "注意：仅支持公开可访问的 http/https 网页；需要登录才能看的内容或二进制文件（PDF/图片等）无法读取。"
                    "若页面内容较长，默认只返回前 30000 字符并附截断提示，需要更多内容时可显式指定更大的 max_chars。"
                    "对于 GitBook 文档站（如 survivesjtu.gitbook.io），站点根链接会返回全站章节索引，"
                    "具体章节链接会返回该页正文，可根据索引逐页阅读。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "要阅读的完整网页链接，如 https://survivesjtu.gitbook.io/survivesjtumanual"},
                        "max_chars": {"type": "integer", "description": "可选：返回正文的字符上限（默认 30000），页面过长时用于控制信息量"},
                        "render": {
                            "type": "boolean",
                            "description": (
                                "可选：是否用浏览器渲染 JavaScript 后再提取（默认 false）。"
                                "当页面内容由脚本动态加载、直接抓取不到内容时（如通知公告列表页），设为 true 可渲染出列表；"
                                "渲染较慢（约 3~8 秒），仅在需要时使用"
                            ),
                        },
                    },
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_web",
                "description": (
                    "在互联网上搜索网页（类似浏览器搜索引擎），返回相关网页的标题、链接和摘要。"
                    "适用于：需要查找外部网站上特定内容但不知道具体网址时"
                    "（如'计算机学院转专业通知''教务处选课通知''某通知的原文'），"
                    "或想了解某话题有哪些公开网页时。"
                    "支持 site: 语法限定网站范围（如 site:www.cs.sjtu.edu.cn 转专业）。"
                    "搜到相关网页后可再用 read_url 阅读其内容。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词，如'上海交通大学 转专业'或'site:www.cs.sjtu.edu.cn 转专业'"},
                        "limit": {"type": "integer", "description": "返回结果条数（默认 5，最多 10）"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_github",
                "description": (
                    "在 GitHub 上搜索开源仓库（走 GitHub 官方搜索 API，返回仓库名、Star 数、"
                    "语言、描述和链接）。适用于：想找 GitHub 上的开源项目时"
                    "（如'推荐几个 GitHub 上的水管工游戏''找接水管游戏的源码'）。"
                    "注意：GitHub 网页搜索/话题页是动态渲染、通用搜索引擎对 site:github.com 收录差，"
                    "找 GitHub 项目请优先用本工具而不是 search_web。"
                    "支持 GitHub 限定语法（如 'pipe puzzle in:name,description'、'topic:game language:python'）。"
                    "搜到仓库后可再用 read_url 阅读其 README 页面。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词，如 'pipe puzzle game' 或 'pipe puzzle in:name,description language:javascript'"},
                        "limit": {"type": "integer", "description": "返回结果条数（默认 5，最多 20）"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_web_sites",
                "description": (
                    "查看预置的外部网站库（常用网站列表，按分类组织），供选择后用 read_url 阅读。"
                    "当用户提到某个校园常用网站或外部网站（如'生存手册''教务处''图书馆''Canvas'等），"
                    "或不确定该用哪个网站获取信息时，可先调用本工具查看网站库中是否有对应网站，"
                    "再用 read_url 阅读该网站内容。支持按分类筛选（如'官方服务''学习资源'）。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "description": "可选：分类名，仅返回该分类下的网站（如'官方服务'）；不传返回全部"},
                        "limit": {"type": "integer", "description": "可选：返回条数上限；不传返回该分类下全部网站"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "setup_jwxt",
                "description": (
                    "配置教学信息服务网（i.sjtu.edu.cn）登录，用于查询课表/成绩/GPA。"
                    "会优先尝试用 .env 中的 JACCOUNT_USERNAME/JACCOUNT_PASSWORD 自动登录，"
                    "失败或未配置时打开浏览器引导用户手动登录（含短信验证码），"
                    "登录成功后自动保存登录态。用户说『配置教学信息服务网』『配置教务系统』"
                    "『登录 jAccount』时调用。"
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_schedule",
                "description": (
                    "查询个人课表（来自教学信息服务网 i.sjtu.edu.cn，结构化数据）。"
                    "用户问『今天有什么课』『本周/下周课表』『这学期课表』等时调用；"
                    "也可查询其他/历史学期的课表——用户提到某个具体学年或学期"
                    "（如『大一上学期的课表』『2024-2025 课表』）时，传对应 year/term 参数。"
                    "返回按星期几分组的课程列表（课程名/教师/教室/节次/周次）。"
                    "未配置登录态时会提示先调用 setup_jwxt。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "year": {"type": "string", "description": "可选：学年起始年（如 2025 表示 2025-2026 学年）；不传自动判断当前学期"},
                        "term": {"type": "string", "description": "可选：学期（1=秋季，2=春季）；不传自动判断当前学期"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "query_grades",
                "description": (
                    "查询个人学科成绩与加权 GPA（来自教学信息服务网 i.sjtu.edu.cn）。"
                    "用户问『我的成绩』『上学期成绩』『GPA 多少』『绩点』等时调用。"
                    "返回成绩明细（课程/成绩/绩点/学分/学期）并自动计算加权 GPA。"
                    "未配置登录态时会提示先调用 setup_jwxt。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "year": {"type": "string", "description": "可选：学年起始年（如 2025 表示 2025-2026 学年）；不传返回全部学年"},
                        "semester": {"type": "string", "description": "可选：学期（1=秋季，2=春季，3=夏季）；不传返回全部学期"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "query_gpa_rank",
                "description": (
                    "查询个人 GPA 排名（绩点排名 + 学积分排名，统计范围为同年级同专业）。"
                    "用户问『我的 GPA 排名』『绩点排名』『我在专业排第几』『学积分排名』"
                    "『我这个学年的排名』等时调用。"
                    "学期范围：不传参数=全部学期累计排名；只传 year=该学年排名；"
                    "year+semester=单个学期排名。"
                    "返回姓名/学号/绩点/绩点排名（如 3/60）/学积分/学积分排名/班级/专业/学院。"
                    "未配置登录态时会提示先调用 setup_jwxt。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "year": {"type": "string", "description": "可选：学年起始年（如 2025 表示 2025-2026 学年）；只传 year 时查询该学年全部学期排名；不传=全部学期累计排名"},
                        "semester": {"type": "string", "description": "可选：学期（1=秋季，2=春季，3=夏季）；需与 year 同时传，仅查该学期；不传=按 year 的范围规则"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "query_training_plan",
                "description": (
                    "查询某专业的培养计划/培养方案（来自教学信息服务网 i.sjtu.edu.cn，"
                    "PDF 自动提取为文本）。用户问『我的专业培养计划』『工业工程的培养方案』"
                    "『这个专业要修什么课』『毕业要求多少学分』等时调用。"
                    "返回课程设置一览（通识/学科基础/专业课程各类要求学分与课程列表）。"
                    "按年级+专业名匹配；未配置登录态时会提示先调用 setup_jwxt。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "major": {"type": "string", "description": "专业名称（模糊匹配，如 '工业工程'；匹配大类/一级/二级专业名）"},
                        "year": {"type": "string", "description": "可选：年级（入学年份，如 2025）；不传自动推断当前学年"},
                        "college": {"type": "string", "description": "可选：学院名称（如 '机械与动力工程学院'），用于缩小匹配范围"},
                    },
                    "required": ["major"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_training_majors",
                "description": (
                    "列出某年级培养计划中的可选专业（可按关键词过滤），返回按学院分组的专业列表。"
                    "当用户想查培养计划但没说清/不确定专业名时，先调用本工具确认真实存在的专业，"
                    "再拿准确专业名调用 query_training_plan。也可用于『2026级有哪些专业』"
                    "『机械学院有什么专业』『有哪些带IEEE的专业』等场景。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string", "description": "可选：关键词，模糊匹配专业名或学院名（如 '计算机'、'机械'）；不传列出全部"},
                        "year": {"type": "string", "description": "可选：年级（入学年份，如 2026）；不传自动推断当前学年"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "setup_course_community",
                "description": (
                    "配置选课社区 course.sjtu.plus 登录：会打开浏览器窗口引导用户手动登录，"
                    "登录成功后自动保存登录态。用户说『配置选课社区』『授权选课社区』"
                    "『登录 course.sjtu.plus』时调用。"
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_courses",
                "description": (
                    "在选课社区 course.sjtu.plus 搜索课程，返回候选课程列表（id/课名/老师/评分/评价数）。"
                    "用户问『XX 课怎么样』『XX 老师的 XX 课口碑如何』『推荐选什么课』『XX 课难不难』"
                    "等选课/课评相关问题时优先调用此工具，再用 get_course_detail 读取详情和评价。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词，可以是课程名、老师名、课程代码"},
                        "page_size": {"type": "integer", "description": "返回结果数，默认 8，最大 20"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_course_detail",
                "description": (
                    "查看 course.sjtu.plus 上某门课的详情和最新若干条学生评价。"
                    "通常在 search_courses 拿到 course_id 后调用，用来回答『这门课具体咋样』"
                    "『有什么真实评价』。禁止编造评价内容：用户想了解课程口碑必须用此工具读取真实评价。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "course_id": {"type": "integer", "description": "课程 id（来自 search_courses 结果）"},
                        "max_reviews": {"type": "integer", "description": "最多返回多少条评价，默认 10，最大 20"},
                    },
                    "required": ["course_id"],
                },
            },
        },
    ]

    def __init__(self, forum: ForumClient, cfg: Config,
                 llm: LLMClient | None = None,
                 on_event: Callable[[str], None] | None = None,
                 is_cancelled: Callable[[], bool] | None = None):
        """
        初始化工具注册表。

        :param forum: 论坛客户端
        :param cfg: 全局配置
        :param llm: LLM 客户端（深度阅读工具需要；未传入时该工具返回错误提示）
        :param on_event: 可选回调，工具执行过程中输出提示信息（如"正在搜索xxx"）
        :param is_cancelled: 可选回调，返回是否收到用户取消信号；为 True 时工具尽早中断
        """
        self.forum = forum
        self.cfg = cfg
        self.llm = llm
        self._on_event = on_event
        self._is_cancelled = is_cancelled or (lambda: False)
        # 外部链接抓取器：独立会话（不携带论坛登录 Cookie）+ SSRF 防护
        self.fetcher = WebFetcher(
            timeout=cfg.web_fetch_timeout,
            max_response_bytes=cfg.web_max_response_bytes,
        )
        self._impl = {
            "search_forum": self._search_forum,
            "read_topic": self._read_topic,
            "browse_latest": self._browse_latest,
            "deep_summarize_topic": self._deep_summarize_topic,
            "list_tags": self._list_tags,
            "list_categories": self._list_categories,
            "get_user_topics": self._get_user_topics,
            "get_user_profile": self._get_user_profile,
            "browse_top": self._browse_top,
            "search_time_range": self._search_time_range,
            "read_topic_likes": self._read_topic_likes,
            "get_tag_topics": self._get_tag_topics,
            "find_similar_topics": self._find_similar_topics,
            "read_url": self._read_url,
            "search_web": self._search_web,
            "search_github": self._search_github,
            "list_web_sites": self._list_web_sites,
            "setup_course_community": self._setup_course_community,
            "search_courses": self._search_courses,
            "get_course_detail": self._get_course_detail,
            "setup_jwxt": self._setup_jwxt,
            "get_schedule": self._get_schedule,
            "query_grades": self._query_grades,
            "query_gpa_rank": self._query_gpa_rank,
            "query_training_plan": self._query_training_plan,
            "list_training_majors": self._list_training_majors,
        }

    def _emit(self, msg: str) -> None:
        """输出过程提示（有回调则调用回调，否则默认打印）。"""
        if self._on_event:
            self._on_event(msg)
        else:
            print(msg, flush=True)

    def _check_cancel(self) -> None:
        """若收到用户取消信号则抛出 TaskCancelled，中断当前工具执行。"""
        if self._is_cancelled():
            raise TaskCancelled()

    @property
    def schema(self) -> list[dict[str, Any]]:
        """返回工具 schema 列表（传给 LLM）。"""
        return self.SCHEMA

    def run(self, name: str, args_json: str) -> str:
        """
        执行指定工具。

        :param name: 工具名
        :param args_json: 工具参数（JSON 字符串）
        :return: 工具执行结果的文本
        """
        impl = self._impl.get(name)
        if impl is None:
            return f"[错误] 未知工具: {name}"
        try:
            args = json.loads(args_json) if args_json else {}
            return impl(**args)
        except Exception as e:  # noqa: BLE001
            return f"[错误] 工具 {name} 执行失败: {e!r}"

    # ---------- 图片理解（视觉模型后处理） ----------

    def _understand_images(self, text: str,
                           limit: int | None = None) -> tuple[str, int]:
        """
        图片理解后处理：把文本中的 [图片: 描述](URL) 占位符替换为视觉模型生成的文字描述。

        主 LLM（deepseek-v4-flash）不支持多模态，因此阅读帖子/网页时遇到图片，
        自动调用独立的视觉模型（如 qwen3.7-flash）把图片内容转成文字，再交给主 LLM。

        处理流程（对每张图）：
            1. 正则提取占位符中的图片 URL；
            2. 下载图片（论坛域附件用带登录态的会话，外链用独立会话，均走 SSRF 防护）；
            3. base64 编码后调用视觉模型理解，得到简洁文字描述；
            4. 占位符替换为 [图片: 描述]（图片内容: <描述>）。

        任一步骤失败（下载失败/非图片/视觉调用异常）都保留原占位符，不影响主流程。
        达到 limit 上限后，其余图片保留占位符（控制视觉调用成本与响应时间）。

        :param text: 含 [图片: ...](URL) 占位符的正文文本
        :param limit: 本次最多理解的图片数（默认用配置 vision_max_images；
                      深度阅读分块时可传剩余预算，控制单帖总成本）
        :return: (处理后的文本, 实际成功理解的图片数)
        """
        if not text:
            return text, 0
        llm = self.llm
        # 视觉模型未配置（vision_api_key 为空）或总开关关闭：保留占位符，不做任何视觉调用
        if (llm is None or not getattr(llm, "vision_enabled", False)
                or getattr(llm, "_vision_client", None) is None):
            return text, 0
        limit = limit if limit is not None else max(0, int(self.cfg.vision_max_images))
        if limit <= 0:
            return text, 0

        state = {"done": 0, "failed": 0}

        def repl(m: re.Match) -> str:
            """按占位符逐个替换：成功才消耗预算；失败保留原占位符。"""
            if state["done"] >= limit:
                return m.group(0)  # 达到单帖上限：保留原占位符
            desc = self._describe_one_image(m.group(2), m.group(1))
            if not desc:
                state["failed"] += 1
                return m.group(0)  # 理解失败：保留原占位符
            state["done"] += 1
            return f"[图片: {m.group(1)}]（图片内容: {desc}）"

        new_text = IMAGE_PLACEHOLDER_RE.sub(repl, text)
        if state["done"]:
            self._emit(
                f"  └ 已理解 {state['done']} 张图片"
                + (f"（{state['failed']} 张失败保留占位）" if state["failed"] else "")
            )
        return new_text, state["done"]

    def _describe_one_image(self, url: str, alt: str) -> str:
        """
        下载并让视觉模型理解单张图片，返回文字描述（失败返回空串）。

        :param url: 图片完整链接
        :param alt: 图片 alt 文本（作为提示辅助模型理解）
        :return: 图片内容描述；任一步骤失败返回 ""（调用方保留原占位符）
        """
        try:
            # 仅当 URL 主机与论坛一致时才用带登录态的会话下载。
            # 用 netloc 精确比较而非 startswith 前缀匹配——后者会把
            # "https://shuiyuan.sjtu.edu.cn.evil.com/..." 这类相似域名误判为论坛附件图
            session = None
            if urlparse(url).netloc.lower() == urlparse(self.forum.base_url).netloc.lower():
                session = self.forum.session
            data, mime = self.fetcher.download_image(url, session=session)
            b64 = base64.b64encode(data).decode("ascii")
            return self.llm.describe_image(b64, mime, hint=alt)
        except Exception:  # noqa: BLE001 下载/视觉调用任一失败都静默降级
            return ""

    # ---------- 工具实现 ----------

    def _search_forum(self, query: str, tags: list[str] | None = None,
                      limit: int | None = None, page: int | None = None) -> str:
        """
        实现 search_forum：搜索帖子并返回摘要文本。

        :param query: 搜索关键词
        :param tags: 可选标签过滤
        :param limit: 返回条数上限（默认用配置值）
        :param page: 起始页码（从 1 开始，1=第一页），可选
        :return: 帖子摘要文本
        """
        limit = _resolve_limit(limit, self.cfg.search_limit)
        page = max(1, int(page or 1))
        topics = self.forum.search(query, limit=limit, tags=tags, page=page)
        tag_suffix = f" +标签[{', '.join(tags)}]" if tags else ""
        n = len(topics)
        self._emit(f'搜索 "{query}"{tag_suffix}  →  {"无结果" if n == 0 else f"{n} 条"}')
        return _format_topics(topics)

    def _read_topic(self, topic_id: int, max_posts: int | None = None,
                    username: str | None = None) -> str:
        """
        实现 read_topic：按字符预算自动分级读取帖子内容。

        策略（容量自适应，基于 100 万 token 上下文）：
            - 短/中帖（总字符在 read_budget_chars 内，实测约 5400 层以下）
              → 全量原文直接返回，一次读完、无需深度阅读；
            - 超预算长帖 → 返回预算内的楼层 + 截断提示，由 LLM 决定是否深度阅读。
        截断依据是实际清洗后的文本字符数（forum_client 逐页累计），而非估算层数，
        因此短帖不会再因"估算值偏高"被误截断。

        :param topic_id: 帖子 id
        :param max_posts: LLM 显式指定的楼层数（受默认层数上限与硬上限约束）
        :param username: 可选：只看该用户的发言楼层（功能2）
        :return: 帖子内容文本（含截断提示）
        """
        budget = self.cfg.read_budget_chars
        # 默认读取层数上限 = 预算 ÷ 估算单层字符（est=100 → 约 3000 层），
        # 防止对几千层楼全文翻页过久；字符预算仍在 get_topic 内做最终精确截断。
        default_floors = max(1, budget // max(1, self.cfg.est_chars_per_post))
        user_specified = max_posts is not None  # 是否 LLM 显式指定楼层数
        if user_specified:
            max_posts = min(int(max_posts), default_floors, self.cfg.max_posts_per_topic)
        else:
            max_posts = min(default_floors, self.cfg.max_posts_per_topic)
        max_posts = max(1, max_posts)

        topic = self.forum.get_topic(
            topic_id, max_posts=max_posts,
            max_chars=budget, max_pages=self.cfg.max_topic_pages,
        )
        total = topic.get("posts_count") or 0
        got = len(topic["posts"])
        used = topic.get("used_chars") or 0
        ref = _topic_ref(topic_id, topic.get("title"))
        text = _format_topic(topic, username=username)
        if username:
            # 只看某人：仅统计该用户的发言层数并给出针对性提示
            user_posts = [p for p in topic["posts"] if p.get("username") == username]
            if not user_posts:
                self._emit(f"读取 {ref} 仅看 {username}  →  无发言")
                return f"（用户 {username} 在帖子 #{topic_id} 中没有发言记录）"
            self._emit(f"读取 {ref} 仅看 {username}  →  {len(user_posts)} 层")
            # 图片理解后处理：帖子中的图片转为文字描述（视觉模型未配置时原样返回）
            text, _ = self._understand_images(text)
            text += f"\n\n[提示] 以上为该帖中用户 {username} 的全部发言（共 {len(user_posts)} 层）。"
            return text
        # 结果行：按是否读完 / 是否显式指定层数给出不同摘要
        if got >= total:
            self._emit(f"读取 {ref}  →  {got} 层（全部读完）")
        elif user_specified:
            self._emit(f"读取 {ref}  →  前 {got} 层 / 共 {total} 层")
        else:
            self._emit(f"读取 {ref}  →  {got} 层 / 共 {total} 层（已截断，建议深读）")
        # 图片理解后处理：帖子中的图片转为文字描述（视觉模型未配置时原样返回）
        text, _ = self._understand_images(text)
        if got < total:
            if user_specified:
                # LLM 显式指定 max_posts 导致的"未读完"，与字符预算无关
                text += (
                    f"\n\n[提示] 该帖共 {total} 层，本次按你指定的 max_posts 只读取了前 {got} 层。"
                    f"若需查看整楼内容/观点，请再次调用 read_topic（不指定 max_posts）"
                    f"或使用 deep_summarize_topic 总结。"
                )
            else:
                text += (
                    f"\n\n[提示] 该帖共 {total} 层，超过单次读取预算，本次返回前 {got} 层"
                    f"（约 {used} 字符 / 预算 {budget}）已截断。"
                    f"如需整楼内容/观点，请调用 deep_summarize_topic 工具进行总结。"
                )
        return text

    def _browse_latest(self, category: str | None = None, limit: int | None = None,
                       tags: list[str] | None = None,
                       exclude_tags: list[str] | None = None,
                       page: int | None = None) -> str:
        """
        实现 browse_latest：浏览最新帖子并返回文本。

        :param category: 版块过滤（可选）
        :param limit: 返回条数上限（默认用配置值）
        :param tags: 可选：仅返回带这些标签的帖子
        :param exclude_tags: 可选：排除带这些标签的帖子
        :param page: 起始页码（从 1 开始，1=第一页），可选
        :return: 帖子摘要文本
        """
        limit = _resolve_limit(limit, self.cfg.search_limit)
        page = max(1, int(page or 1))
        topics = self.forum.get_latest(
            category=category, limit=limit,
            tags=tags, exclude_tags=exclude_tags, page=page,
        )
        parts = []
        if category:
            parts.append(f"+版块[{category}]")
        if tags:
            parts.append(f"+标签[{','.join(tags)}]")
        if exclude_tags:
            parts.append(f"-标签[{','.join(exclude_tags)}]")
        suffix = " " + " ".join(parts) if parts else ""
        n = len(topics)
        self._emit(f"浏览最新{suffix}  →  {'无结果' if n == 0 else f'{n} 条'}")
        return _format_topics(topics)

    def _list_tags(self) -> str:
        """实现 list_tags：查看论坛全部标签及使用数量。"""
        tags = self.forum.get_tags()
        self._emit(f"论坛标签  →  {'无' if not tags else f'{len(tags)} 个'}")
        if not tags:
            return "（论坛暂无标签）"
        # 展示使用量最高的前 30 个标签，避免输出过长占用上下文
        lines = [f"- {t['name']}（{t['count']} 个帖子）" for t in tags[:30]]
        lines.append(f"\n（共 {len(tags)} 个标签，以上为使用量最高的 30 个）")
        return "\n".join(lines)

    def _list_categories(self) -> str:
        """实现 list_categories：查看论坛全部版块列表。"""
        cats = self.forum.get_categories()
        self._emit(f"论坛版块  →  {'无' if not cats else f'{len(cats)} 个'}")
        if not cats:
            return "（论坛暂无版块）"
        return "\n".join(f"- {c['name']}（slug: {c['slug']}）" for c in cats)

    def _get_user_topics(self, username: str, action_type: str = "topics",
                         limit: int | None = None,
                         page: int | None = None) -> str:
        """
        实现 get_user_topics：查看某用户发起的主题或回复过的帖子。

        :param username: 用户名
        :param action_type: "topics"（默认，发起的主题）或 "replies"（回复的帖子）
        :param limit: 返回条数上限
        :param page: 起始页码（从 1 开始，1=第一页），可选
        :return: 帖子摘要文本
        """
        if action_type not in ("topics", "replies"):
            return "[错误] action_type 仅支持 topics（发起的主题）或 replies（回复的帖子）"
        limit = _resolve_limit(limit, self.cfg.search_limit)
        page = max(1, int(page or 1))
        kind = "主题" if action_type == "topics" else "回复"
        topics = self.forum.get_user_topics(
            username, action_type=action_type, limit=limit, page=page,
        )
        n = len(topics)
        self._emit(f"查询 {username} 的{kind}  →  {'无结果' if n == 0 else f'{n} 条'}")
        return _format_topics(topics)

    def _get_user_profile(self, username: str) -> str:
        """
        实现 get_user_profile：查看用户公开资料与统计信息。

        :param username: 用户名
        :return: 用户资料文本
        """
        p = self.forum.get_user_profile(username)
        if not p or not p.get("username"):
            self._emit(f"用户 {username} 资料  →  未找到")
            return f"（未找到用户 {username} 的资料）"
        lines = [
            f"用户名: {p.get('username')}",
            f"显示名: {p.get('name') or '（无）'}",
            f"头衔: {p.get('title') or '（无）'}",
            f"注册时间: {(p.get('created_at') or '')[:10]}",
            f"最后上线: {(p.get('last_seen_at') or '')[:10]}",
            f"最后发帖: {(p.get('last_posted_at') or '')[:10]}",
            f"信任等级: TL{p.get('trust_level')}",
            f"徽章数: {p.get('badge_count')}",
            f"主页被浏览: {p.get('profile_view_count')} 次",
            f"所属组: {p.get('primary_group_name') or '（无）'}",
            f"累计阅读时长: {p.get('time_read')} 秒",
            f"已解决帖数: {p.get('accepted_answers')}",
        ]
        if p.get("bio_excerpt"):
            lines.append(f"个人简介: {p['bio_excerpt']}")
        s = p.get("stats") or {}
        if s:
            lines.append(
                "统计信息: "
                f"发主题 {s.get('topic_count')} · 回帖 {s.get('post_count')} · "
                f"获赞 {s.get('likes_received')} · 点赞 {s.get('likes_given')} · "
                f"在线 {s.get('days_visited')} 天 · 读帖 {s.get('posts_read_count')} · "
                f"解答 {s.get('solved_count')}"
            )
        summary_parts = [f"TL{p.get('trust_level')}"]
        if s:
            if s.get('topic_count') is not None:
                summary_parts.append(f"{s.get('topic_count')}主题")
            if s.get('likes_received') is not None:
                summary_parts.append(f"{s.get('likes_received')}赞")
        self._emit(f"用户 {username} 资料  →  {' · '.join(summary_parts)}")
        return "\n".join(lines)

    def _browse_top(self, period: str = "weekly", limit: int | None = None,
                    page: int | None = None) -> str:
        """
        实现 browse_top：查看论坛热门帖子。

        :param period: 统计周期（daily/weekly/monthly/yearly/all）
        :param limit: 返回条数上限
        :param page: 起始页码（从 1 开始，1=第一页），可选
        :return: 热门帖子列表文本
        """
        if period not in ("daily", "weekly", "monthly", "yearly", "all"):
            return "[错误] period 仅支持 daily/weekly/monthly/yearly/all"
        limit = _resolve_limit(limit, self.cfg.search_limit)
        page = max(1, int(page or 1))
        period_name = {"daily": "今日", "weekly": "本周", "monthly": "本月",
                       "yearly": "今年", "all": "历史全部"}[period]
        topics = self.forum.get_top_topics(period=period, limit=limit, page=page)
        n = len(topics)
        self._emit(f"浏览{period_name}热门  →  {'无结果' if n == 0 else f'{n} 条'}")
        if not topics:
            return "（当前周期暂无热门帖子）"
        return _format_topics_with_stats(topics)

    def _search_time_range(self, query: str, start_date: str | None = None,
                           end_date: str | None = None, tags: list[str] | None = None,
                           limit: int | None = None,
                           page: int | None = None) -> str:
        """
        实现 search_time_range：按时间范围搜索帖子。

        :param query: 搜索关键词
        :param start_date: 起始日期（含），YYYY-MM-DD
        :param end_date: 结束日期（含），YYYY-MM-DD
        :param tags: 可选标签过滤
        :param limit: 返回条数上限
        :param page: 起始页码（从 1 开始，1=第一页），可选
        :return: 帖子列表文本
        """
        limit = _resolve_limit(limit, self.cfg.search_limit)
        page = max(1, int(page or 1))
        if start_date and end_date:
            date_suffix = f" {start_date}~{end_date}"
        elif start_date:
            date_suffix = f" {start_date}起"
        elif end_date:
            date_suffix = f" 至{end_date}"
        else:
            date_suffix = ""
        tag_suffix = f" +标签[{', '.join(tags)}]" if tags else ""
        topics = self.forum.search_time_range(
            query, start_date=start_date, end_date=end_date,
            limit=limit, tags=tags, page=page,
        )
        n = len(topics)
        self._emit(f'搜索 "{query}"{date_suffix}{tag_suffix}  →  {"无结果" if n == 0 else f"{n} 条"}')
        return _format_topics(topics)

    def _read_topic_likes(self, topic_id: int) -> str:
        """
        实现 read_topic_likes：查看单个帖子的热度统计。

        :param topic_id: 帖子 id
        :return: 帖子热度统计文本
        """
        s = self.forum.get_topic_stats(topic_id)
        if not s or not s.get("title"):
            self._emit(f"#{topic_id} 热度  →  未找到")
            return f"（未找到帖子 #{topic_id}）"
        lines = [
            f"标题: {s.get('title')}",
            f"链接: {s.get('link')}",
            f"浏览量: {s.get('views')}",
            f"点赞数: {s.get('like_count')}",
            f"回复数: {s.get('reply_count')}",
            f"总楼层数: {s.get('posts_count')}",
            f"创建时间: {(s.get('created_at') or '')[:10]}",
            f"最后活动: {(s.get('last_posted_at') or '')[:10]}",
        ]
        self._emit(
            f"{_topic_ref(topic_id, s.get('title'))} 热度  →  "
            f"浏览{s.get('views')} · 赞{s.get('like_count')} · {s.get('posts_count')}层"
        )
        return "\n".join(lines)

    def _get_tag_topics(self, tag: str, limit: int | None = None,
                        page: int | None = None) -> str:
        """
        实现 get_tag_topics：浏览某个标签下的帖子列表。

        :param tag: 标签名
        :param limit: 返回条数上限
        :param page: 起始页码（从 1 开始，1=第一页），可选
        :return: 帖子列表文本
        """
        limit = _resolve_limit(limit, self.cfg.search_limit)
        page = max(1, int(page or 1))
        topics = self.forum.get_tag_topics(tag, limit=limit, page=page)
        n = len(topics)
        self._emit(f"浏览标签[{tag}]  →  {'无结果' if n == 0 else f'{n} 条'}")
        return _format_topics_with_stats(topics)

    @staticmethod
    def _title_to_query(title: str) -> str:
        """
        把帖子标题清洗为搜索关键词（供相关帖兜底搜索用）。

        :param title: 帖子标题
        :return: 清洗后的关键词串（去括号、常见标点，压缩空白）
        """
        q = re.sub(r"[\[\]【】《》〈〉\"'“”‘’，。、！？；：\s]+", " ", title or "")
        return q.strip()

    def _find_similar_topics(self, topic_id: int, limit: int | None = None,
                             page: int | None = None) -> str:
        """
        实现 find_similar_topics：查找与某帖相似/相关的帖子（功能1）。

        策略：
            1. 优先用论坛内置的 related_topics 推荐（/t/{id}.json 自带字段，实测部分帖存在）；
            2. 无推荐时回退为"标题关键词搜索"（复用 search 接口，排除自身）。

        :param topic_id: 帖子 id
        :param limit: 返回条数上限
        :param page: 起始页码（从 1 开始，1=第一页），可选；仅兜底搜索时生效
        :return: 相关帖子列表文本
        """
        limit = _resolve_limit(limit, self.cfg.search_limit)
        page = max(1, int(page or 1))
        data = self.forum.get_related_topics(topic_id, limit=limit)
        related = data.get("related") or []
        if related:
            self._emit(f"#{topic_id} 的相关帖子  →  {len(related)} 条")
            return _format_topics_with_stats(related)
        title = data.get("title") or ""
        if not title:
            self._emit(f"#{topic_id} 的相关帖子  →  无法获取标题")
            return "（无法获取帖子标题，无法查找相关帖子）"
        # 兜底：标题关键词搜索（排除自身）
        q = self._title_to_query(title)
        if not q:
            self._emit(f"#{topic_id} 的相关帖子  →  无有效关键词")
            return "（帖子标题无法提取有效关键词）"
        topics = self.forum.search(q, limit=limit + 1, page=page)
        topics = [t for t in topics if t.get("id") != topic_id][:limit]
        if not topics:
            self._emit(f"#{topic_id} 的相关帖子  →  无结果（关键词搜索）")
            return f"（未找到与帖子 #{topic_id} 相关的其他帖子）"
        self._emit(f"#{topic_id} 的相关帖子  →  {len(topics)} 条（关键词搜索）")
        return _format_topics(topics)

    def _read_url(self, url: str, max_chars: int | None = None,
                  render: bool = False) -> str:
        """
        实现 read_url：阅读外部网页链接并返回清洗后的正文。

        安全策略（详见 web_fetcher.WebFetcher）：
            - 独立会话，不携带论坛登录 Cookie，防止泄露给第三方网站；
            - SSRF 防护：仅 http/https，DNS 解析拒绝内网/回环/链路本地地址，重定向逐跳校验；
            - 响应体大小与返回字符双重上限，防止内存与上下文被撑爆。

        render 参数用于页面内容由脚本动态加载的场景（如通知公告列表页）：
        启用 Playwright 渲染后再提取，可拿到 JS 生成的内容。

        :param url: 要阅读的完整链接
        :param max_chars: 返回正文的字符上限（默认用配置 web_read_chars，硬上限 read_budget_chars）
        :param render: 是否启用浏览器渲染（Playwright），默认 False
        :return: 网页正文文本（含链接/标题与截断提示）
        """
        # 字符上限归一化：默认用配置值；显式传入时限制在 [1000, read_budget_chars]
        if max_chars is None:
            max_chars = self.cfg.web_read_chars
        max_chars = max(1000, min(int(max_chars), self.cfg.read_budget_chars))

        mode = "（浏览器渲染）" if render else ""
        self._emit(f"阅读链接 {url}{mode}")
        try:
            result = self.fetcher.fetch(
                url, max_chars=max_chars, render=bool(render)
            )
        except ValueError as e:
            self._emit(f"阅读链接 {url}  →  已拒绝: {e}")
            return f"[错误] {e}"
        except requests.RequestException as e:
            self._emit(f"阅读链接 {url}  →  抓取失败")
            return f"[错误] 抓取 {url} 失败: {e!r}"

        lines = [f"链接: {result['url']}"]
        if result.get("title"):
            lines.append(f"标题: {result['title']}")
        lines.append(result["text"])
        if result.get("js_list_hint"):
            # 疑似脚本加载的列表页但未提取到条目：提示改用渲染模式重试
            lines.append(
                "\n[提示] 此页面是通知/公告列表页，但列表项可能由脚本动态加载，"
                "本次未读取到具体条目；可再次调用 read_url 并设置 render=true"
                "（启用浏览器渲染）重新抓取该列表页。"
            )
        if result.get("truncated"):
            lines.append(
                f"\n[提示] 内容超过 {max_chars} 字符上限已截断；"
                f"如需更多内容可再次调用 read_url 并指定更大的 max_chars。"
            )
        self._emit(
            f"阅读链接 {result['url']}{mode}  →  {len(result['text'])} 字符"
            + ("（已截断）" if result.get("truncated") else "")
        )
        text = "\n".join(lines)
        # 图片理解后处理：网页中的图片转为文字描述（视觉模型未配置时原样返回）
        text, _ = self._understand_images(text)
        return text

    def _search_web(self, query: str, limit: int | None = None) -> str:
        """
        实现 search_web：搜索引擎检索网页，返回标题/链接/摘要列表。

        基于 360 搜索（so.com，详见 WebFetcher.search_web），结果中的真实链接
        可直接用于 read_url 阅读（read_url 内部仍有 SSRF 防护）。

        :param query: 搜索关键词（支持 site: 语法）
        :param limit: 返回结果条数（默认 5，最多 10）
        :return: 搜索结果列表文本
        """
        limit = max(1, min(int(limit or 5), 10))
        self._emit(f'搜索网页 "{query}"')
        try:
            results = self.fetcher.search_web(query, limit=limit)
        except SearchRateLimitedError as e:
            # 连续搜索触发搜索引擎反爬：明确提示限流，避免 agent 误以为没结果
            self._emit(f'搜索网页 "{query}"  →  被限流')
            return f"[提示] {e}，可稍后重试或改用 list_web_sites + read_url 直接访问目标网站。"
        except requests.RequestException as e:
            self._emit(f'搜索网页 "{query}"  →  失败')
            return f"[错误] 网页搜索失败: {e!r}"
        if not results:
            self._emit(f'搜索网页 "{query}"  →  无结果')
            return "（未找到相关网页，可尝试更换关键词或去掉 site: 限定）"
        lines = [
            f"- {r['title']}\n"
            f"  链接: {r['url']}\n"
            f"  摘要: {(r['desc'] or '')[:150]}"
            for r in results
        ]
        self._emit(f'搜索网页 "{query}"  →  {len(results)} 条')
        return "\n".join(lines)

    def _search_github(self, query: str, limit: int | None = None) -> str:
        """
        实现 search_github：用 GitHub 官方搜索 API 检索开源仓库。

        GitHub 网页搜索/话题页为 JS 动态渲染（render 后也拿不到多少内容），
        且通用搜索引擎对 site:github.com 收录差，因此走 GitHub REST API
        （详见 WebFetcher.search_github），返回结构化仓库列表。

        :param query: 搜索关键词（支持 GitHub 限定语法）
        :param limit: 返回结果条数（默认 5，最多 20）
        :return: 仓库列表文本
        """
        limit = max(1, min(int(limit or 5), 20))
        self._emit(f'搜索 GitHub "{query}"')
        try:
            results = self.fetcher.search_github(query, limit=limit)
        except SearchRateLimitedError as e:
            # GitHub API 限流：明确提示，避免 agent 误以为没结果
            self._emit(f'搜索 GitHub "{query}"  →  被限流')
            return f"[提示] {e}，可稍后重试或改用 read_url 直接访问已知仓库。"
        except requests.RequestException as e:
            self._emit(f'搜索 GitHub "{query}"  →  失败')
            return f"[错误] GitHub 搜索失败: {e!r}"
        if not results:
            self._emit(f'搜索 GitHub "{query}"  →  无结果')
            return "（GitHub 上未找到匹配的仓库，可尝试更换关键词或使用 in:name,description 限定）"
        lines = [
            f"- {r['name']}（⭐{r['stars']} · {r['lang'] or '未知语言'}）\n"
            f"  链接: {r['url']}\n"
            f"  描述: {(r['desc'] or '')[:200]}"
            for r in results
        ]
        self._emit(f'搜索 GitHub "{query}"  →  {len(results)} 条')
        return "\n".join(lines)

    @staticmethod
    def _load_web_sites() -> list[dict]:
        """
        从 data/web_sites.json 加载预置网站库。

        每次调用实时读文件，用户手动编辑增删网站后无需重启即生效；
        文件缺失/损坏时返回空列表，不影响主流程。

        :return: 网站列表，每项含 name/category/url/desc
        """
        try:
            if WEB_SITES_PATH.exists():
                raw = json.loads(WEB_SITES_PATH.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    return [
                        s for s in raw
                        if isinstance(s, dict) and s.get("url")
                    ]
        except Exception:  # noqa: BLE001 网站库损坏不影响主流程
            pass
        return []

    def _list_web_sites(self, category: str | None = None,
                        limit: int | None = None) -> str:
        """
        实现 list_web_sites：列出网站库中的网站（可按分类筛选）。

        未显式传 limit 时返回全部（网站库规模小，全量返回可控）；
        显式传 limit 时限制条数（硬上限 100，防 agent 传超大值）。

        :param category: 可选分类名，仅返回该分类下的网站
        :param limit: 可选返回条数上限（不传返回全部）
        :return: 网站列表文本
        """
        sites = self._load_web_sites()
        if category:
            sites = [s for s in sites if (s.get("category") or "") == category]
        if limit is not None:
            sites = sites[:max(1, min(int(limit), 100))]
        suffix = f"分类[{category}]" if category else "全部"
        if not sites:
            self._emit(f"网站库（{suffix}）  →  无结果")
            return (
                f"（网站库{suffix}中暂无网站，"
                f"可在 data/web_sites.json 中添加）"
            )
        lines = [
            f"- {s.get('name')}（{s.get('category') or '未分类'}）\n"
            f"  链接: {s.get('url')}\n"
            f"  说明: {s.get('desc') or ''}"
            for s in sites
        ]
        self._emit(f"网站库（{suffix}）  →  {len(sites)} 个")
        return "\n".join(lines)

    def _deep_summarize_topic(self, topic_id: int, focus: str | None = None) -> str:
        """
        实现 deep_summarize_topic：容量感知的整楼总结。

        策略（基于 100 万 token 上下文）：
            1. 探测第一页，用实测平均每层字符估算帖子总字符；
            2. 总字符 ≤ read_budget_chars（能一次放下，约 5400 层以下）
               → 直接读全文 + 一次 LLM 总结，不走 Map-Reduce，信息无损、调用次数最少；
            3. 总字符超预算（真正的万层大楼）
               → 按字符预算分块 Map-Reduce：每块原文 ≤ deep_read_block_chars，
                 块数 = 总字符 ÷ 每块预算（受 deep_read_max_blocks 保护）。

        :param topic_id: 帖子 id
        :param focus: 用户关注点（可选）
        :return: 整楼综合摘要文本
        """
        if self.llm is None:
            return "[错误] deep_summarize_topic 需要 LLM 客户端，但工具未配置。"

        budget = self.cfg.read_budget_chars
        block_chars = self.cfg.deep_read_block_chars
        max_blocks = self.cfg.deep_read_max_blocks
        max_chars_per_post = self.cfg.deep_read_max_chars_per_post
        summary_chars = self.cfg.deep_read_summary_chars
        link = f"https://shuiyuan.sjtu.edu.cn/t/{topic_id}"

        # 1. 探测总楼层数 + 实测平均每层字符（读第一页 20 层，顺便拿帖子标题）
        probe = self.forum.get_topic_range(topic_id, 1, 20)
        total = probe.get("posts_count") or 0
        if total == 0:
            return f"[错误] 帖子 #{topic_id} 没有可读楼层。"
        first_posts = probe.get("posts", [])
        avg_chars = (sum(len(p.get("content") or "") for p in first_posts)
                     / len(first_posts)) if first_posts else 0
        est_total_chars = total * avg_chars
        title = probe.get("title") or f"#{topic_id}"
        ref = _topic_ref(topic_id, title)
        focus_suffix = f" 关注：{focus}" if focus else ""

        # 2. 容量感知：总字符在预算内 → 直接读全文 + 一次总结（不 Map-Reduce）
        if est_total_chars <= budget:
            self._emit(
                f"深读 {ref}{focus_suffix}  →  {total} 层 / {est_total_chars:.0f}字（全文一次总结）"
            )
            topic = self.forum.get_topic(
                topic_id, max_chars=budget, max_pages=self.cfg.max_topic_pages,
            )
            block_text = _format_topic(topic, max_chars_per_post=max_chars_per_post)
            # 图片理解后处理：帖子中的图片转为文字描述后再交给 LLM 总结
            block_text, _ = self._understand_images(block_text)
            focus_line = f"\n用户关注点：{focus}" if focus else ""
            resp = self.llm.chat([
                {"role": "system", "content": DEEP_READ_DIRECT_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"帖子《{topic.get('title')}》\n链接: {link}\n\n"
                        f"全文如下：\n\n{block_text}{focus_line}"
                    ),
                },
            ])
            return (resp["content"] or "").strip()

        # 3. 超预算（真正的超长楼）→ 按字符预算分块 Map-Reduce。
        # 块大小 = 每块字符预算 ÷ 单层截断上限（保证每块原文不超过预算）
        block_floors = max(1, block_chars // max(1, max_chars_per_post))
        total_to_process = total
        n_blocks = (total + block_floors - 1) // block_floors
        truncate_note = ""
        if n_blocks > max_blocks:
            total_to_process = max_blocks * block_floors
            truncate_note = f"，仅前 {total_to_process}/{total} 层"

        self._emit(
            f"深读 {ref}{focus_suffix}  →  {total} 层 / {est_total_chars:.0f}字（分块总结{truncate_note}）"
        )

        # 4. 逐块：读原文 → 独立上下文总结 → 只保留块摘要
        summaries: list[str] = []
        block_idx = 0
        # 图片理解总预算：跨块共享（每块用剩余额度，控制单帖视觉调用总成本）
        img_budget = max(0, int(self.cfg.vision_max_images))
        for start in range(1, total_to_process + 1, block_floors):
            self._check_cancel()  # 每块之间检查用户取消信号，尽早中断
            end = min(start + block_floors - 1, total_to_process)
            block_idx += 1
            block = self.forum.get_topic_range(topic_id, start, end)
            block_text = _format_topic(block, max_chars_per_post=max_chars_per_post)
            # 图片理解后处理（剩余预算随块递减，0 后其余图片保留占位符）
            block_text, used = self._understand_images(block_text, limit=img_budget)
            img_budget -= used
            resp = self.llm.chat([
                {"role": "system", "content": DEEP_READ_BLOCK_PROMPT},
                {"role": "user", "content": f"帖子《{title}》楼层 {start}-{end}：\n{block_text}"},
            ])
            summary = (resp["content"] or "").strip()
            if len(summary) > summary_chars:
                summary = summary[:summary_chars] + "…"
            summaries.append(f"[楼层{start}-{end}摘要] {summary}")
            self._emit(f"  └ 块{block_idx} [{start}-{end}层] 已总结")

        # 5. 合并所有块摘要（Reduce）
        focus_line = f"\n用户关注点：{focus}" if focus else ""
        merge_resp = self.llm.chat([
            {"role": "system", "content": DEEP_READ_MERGE_PROMPT},
            {
                "role": "user",
                "content": (
                    f"帖子《{title}》\n链接: {link}\n\n"
                    f"全部分块摘要如下：\n\n{chr(10).join(summaries)}{focus_line}"
                ),
            },
        ])
        self._emit("  └ 合并完成")
        return (merge_resp["content"] or "").strip()

    # ---------- 选课社区（course.sjtu.plus） ----------

    def _setup_course_community(self) -> str:
        """
        实现 setup_course_community：配置选课社区登录。

        优先无头自动登录（.env 配置 COURSE_PLUS_PASSWORD/JACCOUNT 凭据时），
        失败或未配置时降级为浏览器手动登录（打开有头 Chrome 引导用户完成）。

        :return: 登录结果 JSON 文本
        """
        self._emit("正在配置选课社区（优先自动登录，失败会打开浏览器）...")
        result = cc.auto_login(on_wait=self._emit)
        if not result.get("ok"):
            self._emit(f"自动登录不可用（{result.get('error')}），改用浏览器手动登录。")
            result = cc.login_via_browser(on_wait=self._emit)
        ok = bool(result.get("ok"))
        self._emit("配置选课社区  →  " + ("成功" if ok else "失败"))
        return json.dumps(result, ensure_ascii=False)

    def _search_courses(self, query: str, page_size: int | None = None) -> str:
        """
        实现 search_courses：在选课社区搜索课程。

        :param query: 搜索关键词（课程名/老师名/课程代码）
        :param page_size: 返回条数（默认 8，最大 20）
        :return: 课程候选列表 JSON 文本
        """
        result = cc.search_courses(query, page_size or 8)
        if result.get("error"):
            self._emit(f'搜索选课社区 "{query}"  →  {result["error"]}')
        elif result.get("message"):
            self._emit(f'搜索选课社区 "{query}"  →  无结果')
        else:
            self._emit(f'搜索选课社区 "{query}"  →  {result.get("returned", 0)} 条')
        return json.dumps(result, ensure_ascii=False)

    def _get_course_detail(self, course_id: int,
                           max_reviews: int | None = None) -> str:
        """
        实现 get_course_detail：获取选课社区课程详情与最新评价。

        :param course_id: 课程 id（来自 search_courses 结果）
        :param max_reviews: 最多返回的评价条数（默认 10，最大 20）
        :return: 课程详情 JSON 文本（含 reviews 列表）
        """
        result = cc.get_course_detail(course_id, max_reviews or 10)
        if result.get("error"):
            self._emit(f"读取选课社区课程 #{course_id}  →  {result['error']}")
        else:
            n = len(result.get("reviews") or [])
            self._emit(
                f"读取选课社区课程 #{course_id}《{result.get('name', '')}》"
                f"  →  {n} 条评价"
            )
        return json.dumps(result, ensure_ascii=False)

    # ---------- 教学信息服务网（i.sjtu.edu.cn：课表/成绩/GPA） ----------

    def _setup_jwxt(self) -> str:
        """
        实现 setup_jwxt：配置教学信息服务网登录。

        优先自动登录（.env 配置了 JACCOUNT 凭据时），失败或未配置时降级为
        浏览器手动登录（打开有头 Chrome 引导用户完成，含短信验证码）。

        :return: 登录结果 JSON 文本
        """
        # 已有有效登录态则直接复用，无需重新登录
        if jw.validate_cookies():
            self._emit("教学信息服务网  →  已登录（复用现有登录态）")
            return json.dumps({"ok": True, "message": "已检测到有效登录态，无需重新登录。"},
                              ensure_ascii=False)
        self._emit("正在配置教学信息服务网登录（优先自动登录，失败会打开浏览器）...")
        result = jw.auto_login(on_wait=self._emit)
        if not result.get("ok"):
            self._emit(f"自动登录不可用（{result.get('error')}），改用浏览器手动登录。")
            result = jw.login_via_browser(on_wait=self._emit)
        ok = bool(result.get("ok"))
        self._emit("配置教学信息服务网  →  " + ("成功" if ok else "失败"))
        return json.dumps(result, ensure_ascii=False)

    def _get_schedule(self, year: str | None = None, term: str | None = None) -> str:
        """
        实现 get_schedule：查询个人课表并格式化为文本。

        :param year: 学年起始年（可选，不传自动判断）
        :param term: 学期（1=秋季，2=春季，可选，不传自动判断）
        :return: 课表文本
        """
        self._emit(f"查询课表（{'自动学期' if not year and not term else f'{year} {term}'}）")
        data = jw.fetch_schedule(year=year or "", term=term or "", on_wait=self._emit)
        text = jw.format_schedule(data)
        if data.get("error"):
            self._emit("查询课表  →  失败")
        else:
            self._emit(f"查询课表  →  {data.get('total', 0)} 门课程")
        return text

    def _query_grades(self, year: str | None = None,
                      semester: str | None = None) -> str:
        """
        实现 query_grades：查询成绩与加权 GPA 并格式化为文本。

        :param year: 学年起始年（可选，不传返回全部）
        :param semester: 学期（1=秋季，2=春季，3=夏季，可选，不传返回全部）
        :return: 成绩文本
        """
        self._emit(f"查询成绩（{'全部' if not year and not semester else f'{year} {semester}'}）")
        data = jw.query_grades(year=year or "", semester=semester or "")
        text = jw.format_grades(data)
        if data.get("error"):
            self._emit("查询成绩  →  失败")
        else:
            self._emit(f"查询成绩  →  {data.get('count', 0)} 条（加权 GPA: {data.get('weighted_gpa')}）")
        return text

    def _query_gpa_rank(self, year: str | None = None,
                        semester: str | None = None) -> str:
        """
        实现 query_gpa_rank：查询个人 GPA 排名并格式化为文本。

        :param year: 学年起始年（可选，不传=全部学期累计）
        :param semester: 学期（1=秋季，2=春季，3=夏季，可选，不传=全部学期累计）
        :return: 排名文本
        """
        self._emit(f"查询 GPA 排名（{'全部学期累计' if not year and not semester else f'{year} {semester}'}）")
        data = jw.query_gpa_rank(year=year or "", semester=semester or "")
        text = jw.format_gpa_rank(data)
        if data.get("error"):
            self._emit("查询 GPA 排名  →  失败")
        else:
            r = data.get("rank") or {}
            self._emit(
                f"查询 GPA 排名  →  {r.get('name', '')} 绩点 {r.get('gpa')} 排名 {r.get('gpa_rank')}"
                + (f" 学积分排名 {r.get('xjf_rank')}" if r.get("xjf_rank") else "")
            )
        return text

    def _query_training_plan(self, major: str, year: str | None = None,
                             college: str | None = None) -> str:
        """
        实现 query_training_plan：查询专业培养计划并格式化为文本。

        :param major: 专业名称（模糊匹配）
        :param year: 年级（可选，不传自动推断）
        :param college: 学院名称（可选，用于缩小范围）
        :return: 培养计划文本
        """
        self._emit(f"查询培养计划（{major} / {'自动年级' if not year else year}级）")
        data = jw.query_training_plan(
            year=year or "", major=major, college=college or "", on_wait=self._emit,
        )
        text = jw.format_training_plan(data)
        if data.get("error"):
            self._emit("查询培养计划  →  失败")
        elif data.get("message"):
            self._emit(f"查询培养计划  →  未匹配到专业")
        else:
            self._emit(
                f"查询培养计划  →  {data.get('title', '')}（{data.get('pages', 0)} 页，"
                f"{len(data.get('text') or '')} 字符）"
            )
        return text

    def _list_training_majors(self, keyword: str | None = None,
                              year: str | None = None) -> str:
        """
        实现 list_training_majors：列出某年级可选专业并格式化为文本。

        :param keyword: 关键词（可选，匹配专业名/学院名）
        :param year: 年级（可选，不传自动推断）
        :return: 专业列表文本
        """
        self._emit(f"列出培养计划专业（{'全部' if not keyword else f'关键词[{keyword}]'} / "
                   f"{'自动年级' if not year else year}级）")
        data = jw.list_training_majors(
            keyword=keyword or "", year=year or "", on_wait=self._emit,
        )
        text = jw.format_list_training_majors(data)
        if data.get("error"):
            self._emit("列出培养计划专业  →  失败")
        else:
            self._emit(
                f"列出培养计划专业  →  {data.get('matched', 0)} 个"
                f"（共 {data.get('total', 0)} 条培养计划）"
            )
        return text
