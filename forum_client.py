"""
forum_client.py — 水源社区（Discourse）数据访问客户端。

职责：
    1. 加载登录态 cookie（storage_state.json），构造带会话的 requests.Session；
    2. 封装 Discourse JSON API：搜索、读帖、浏览最新、获取版块；
    3. 将帖子正文由 HTML 清洗为纯文本，控制传给 LLM 的 token 量。

Discourse API 端点（已实测可用）：
    - GET /search.json?q=<关键词>       → 搜索
    - GET /latest.json?category=<id>    → 最新帖子
    - GET /t/<topic_id>.json            → 帖子详情（含全部楼层）
    - GET /categories.json              → 版块列表
"""

import json
import threading
import time
import warnings
from pathlib import Path
from typing import Any, Callable

import requests
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning, NavigableString

# 论坛根地址（类级常量，供 _html_to_text 拼接相对链接时使用）
BASE_URL = "https://shuiyuan.sjtu.edu.cn"


class LoginExpiredError(Exception):
    """登录态失效（论坛返回 403）时的自定义异常。"""


class ForumClient:
    """封装水源社区 Discourse API 的客户端。"""

    def __init__(self, state_file: Path, request_delay: float = 1.0):
        """
        初始化论坛客户端。

        :param state_file: Playwright 导出的登录态文件（storage_state.json）路径
        :param request_delay: 相邻两次 API 请求的间隔秒数，避免触发限流
        """
        self.base_url = BASE_URL
        self.request_delay = request_delay
        self.state_file = Path(state_file)
        # 写回 storage_state.json 的锁（防止多线程并发写文件）
        self._state_lock = threading.Lock()
        self.session = self._build_session(state_file)

    # ---------- 内部工具方法 ----------

    def _build_session(self, state_file: Path) -> requests.Session:
        """根据 storage_state.json 构建带登录 cookie 的 requests 会话。"""
        session = requests.Session()
        state = json.loads(state_file.read_text(encoding="utf-8"))
        for c in state.get("cookies", []):
            session.cookies.set(
                c["name"], c["value"],
                domain=c.get("domain"), path=c.get("path", "/"),
            )
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/javascript, */*; q=0.01",
        })
        return session

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        发起带 cookie 的 GET 请求并解析 JSON。

        :param path: 端点路径，如 "/search.json"
        :param params: 查询参数
        :return: 解析后的 JSON 字典
        :raises LoginExpiredError: 论坛返回 403（登录态失效）
        """
        time.sleep(self.request_delay)  # 限流保护
        url = self.base_url + path
        resp = self.session.get(url, params=params, timeout=30)
        if resp.status_code == 403:
            raise LoginExpiredError(
                "论坛返回 403：登录态已失效，请重新运行登录流程（auth.ensure_login）"
            )
        resp.raise_for_status()
        # 请求成功：把服务端下发的最新 cookie（含轮换续期的新认证 token/keepalive）
        # 合并写回 storage_state.json，使登录态像浏览器一样持续保鲜（详见 _sync_state_file）
        self._sync_state_file()
        return resp.json()

    def _sync_state_file(self) -> None:
        """
        把当前会话的最新 cookie 合并写回 storage_state.json（登录态保活续期）。

        背景：Discourse 的认证 token（_t）与保活 cookie（keepalive）会在使用过程中
        被服务端通过 Set-Cookie 轮换续期——浏览器会自动保存新 cookie，因此用户
        浏览器里登录态能保持好几天；而 agent 若只在启动时读一次文件、从不写回，
        下次启动时用的还是旧 token，一旦旧 token 已被服务端轮换作废，就会 403
        要求重新登录。

        本方法在每次请求成功后调用：以当前会话 cookie 为准，合并写回文件
        （保留 jaccount 等其他域 cookie 及原文件中的 localStorage 等字段）。
        写失败静默忽略（不影响主流程），仅在有变化时写盘（减少无效 IO）。
        """
        try:
            with self._state_lock:
                # 读取现有文件（保留 cookies 之外的字段：localStorage/origins 等）
                if self.state_file.exists():
                    state = json.loads(self.state_file.read_text(encoding="utf-8"))
                else:
                    state = {}
                old_cookies = state.get("cookies", []) or []
                # 以 "域+路径+名称" 为键，合并新旧 cookie：会话中的值优先
                merged: dict[tuple, dict] = {}
                for c in old_cookies:
                    merged[(c.get("domain"), c.get("path"), c.get("name"))] = c
                changed = False
                for c in self.session.cookies:
                    key = (c.domain, c.path, c.name)
                    entry = {
                        "name": c.name,
                        "value": c.value,
                        "domain": c.domain,
                        "path": c.path or "/",
                        # requests 会话内 cookie 的 expires 多为 None（加载时未带过期
                        # 时间），保留文件原有效期，避免把 _t 等有期 token 降级为 session cookie
                        "expires": (c.expires or
                                    (merged[key].get("expires", -1)
                                     if key in merged else -1)),
                        # httpOnly/secure 等属性以文件旧值为准（requests 的 Cookie
                        # 对象拿不到完整属性，如 has_nonstandard_attr 是方法而非值）
                        "httpOnly": merged[key].get("httpOnly", False)
                        if key in merged else False,
                        "secure": merged[key].get("secure", c.secure)
                        if key in merged else c.secure,
                        "sameSite": (merged[key].get("sameSite")
                                     if key in merged else "Lax"),
                    }
                    if key in merged and merged[key].get("value") == c.value:
                        continue  # 该 cookie 未变化
                    merged[key] = entry
                    changed = True
                if not changed:
                    return  # 所有 cookie 均未变化，不写盘
                state["cookies"] = list(merged.values())
                self.state_file.write_text(
                    json.dumps(state, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        except Exception:  # noqa: BLE001 写回失败不影响主流程
            pass

    @staticmethod
    def _html_to_text(html: str) -> str:
        """
        将 Discourse 帖子的 HTML 正文清洗为 LLM 友好的文本。

        保留信息（功能4：正文保留代码/链接/表格/引用/图片）：
            - 代码块 <pre><code> → fenced code block（带语言标记）；
            - 表格 <table> → 类 Markdown 表格（| 分隔）；
            - 链接 <a> → [文本](完整URL)；
            - 图片 <img> → [图片: 描述](完整URL)；
            - 引用块 <blockquote> → > 引用文本。

        处理顺序（先深层元素后块级元素，避免内部信息被块级替换吞掉）：
            script/style → pre 代码块 → img → a → table → blockquote → 行结构。
        """
        if not html:
            return ""
        # Discourse 的 blurb/excerpt 等字段可能是纯文本 URL（如整楼只发一个链接），
        # 会触发 bs4 的 MarkupResemblesLocatorWarning（把输入误判为 URL）。
        # 解析结果不受影响（URL 会被当作纯文本原样保留），仅屏蔽这条误报警告。
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)
            soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()  # 移除脚本、样式节点（图片不再直接丢弃，转为文本占位）
        # 投票容器：其数据已由 _polls_to_text 单独解析附在楼层末尾，
        # 这里移除 HTML 里的 poll 结构，避免与 [投票] 块重复输出
        for tag in soup.select(".poll"):
            tag.decompose()

        # 1) 代码块：整体替换为 fenced code block（内部文本原样保留，不再做链接/换行处理）
        for pre in soup.find_all("pre"):
            code = pre.find("code") or pre
            lang = ""
            for cls in (code.get("class") or []):
                if cls.startswith("lang-"):
                    lang = cls[len("lang-"):]
                    break
            text = code.get_text("\n").strip()
            if text:
                fence = f"```{lang}\n{text}\n```" if lang else f"```\n{text}\n```"
                pre.replace_with(NavigableString(f"\n{fence}\n"))
            else:
                pre.replace_with(NavigableString(""))

        # 2) 图片 → [图片: 描述](URL)；Discourse 装饰性图片（头像/表情）不转占位符，
        #    避免被视觉模型当作正文图片下载解析（如引用块里的用户头像、:emoji: 表情）
        for img in soup.find_all("img"):
            classes = img.get("class") or []
            alt = (img.get("alt") or "").strip()
            src = (img.get("src") or img.get("data-src") or "").strip()
            # 用户头像：纯装饰（alt 为空，旁边的用户名文本是独立节点会保留），直接移除
            if "avatar" in classes:
                img.decompose()
                continue
            # 表情：保留其文本形式（如 :drooling_face:），保留语义但不下载解析
            if "emoji" in classes:
                img.replace_with(NavigableString(alt or img.get("title") or ""))
                continue
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE_URL + src
            img.replace_with(
                NavigableString(f"[图片: {alt}]({src})" if src else f"[图片: {alt}]")
            )

        # 3) 链接 → [文本](完整URL)；图片放大链接（class=lightbox）已含图片占位，只保留文本
        for a in soup.find_all("a"):
            text = a.get_text(" ", strip=True)
            href = (a.get("href") or "").strip()
            if not href or href.startswith("#") or "lightbox" in (a.get("class") or []):
                a.replace_with(NavigableString(text))
                continue
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = BASE_URL + href
            a.replace_with(NavigableString(f"[{text}]({href})"))

        # 4) 表格 → 类 Markdown 表格（首行视为表头，单元格用 | 分隔）
        for tbl in soup.find_all("table"):
            rows = []
            for tr in tbl.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                rows.append("| " + " | ".join(cells) + " |")
            if rows:
                tbl.replace_with(NavigableString("\n" + "\n".join(rows) + "\n"))
            else:
                tbl.replace_with(NavigableString(""))

        # 5) 引用块 → > 引用文本（内部链接/图片已在上方转为文本）
        for q in soup.find_all("blockquote"):
            text = q.get_text("\n").strip()
            if text:
                quoted = "\n".join("> " + ln for ln in text.splitlines())
                q.replace_with(NavigableString("\n" + quoted + "\n"))

        # 6) 用换行分隔块级元素，保留可读结构
        for br in soup.find_all("br"):
            br.replace_with("\n")
        for p in soup.find_all(["p", "div", "li"]):
            p.append("\n")
        text = soup.get_text("\n")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return "\n".join(lines)

    @staticmethod
    def _topic_link(topic_id: int, slug: str | None = None) -> str:
        """拼接帖子链接（Discourse 支持按 id 访问，无需 slug）。"""
        return f"https://shuiyuan.sjtu.edu.cn/t/{topic_id}"

    @staticmethod
    def _polls_to_text(polls: list[dict] | None) -> str:
        """
        将帖子的投票（discourse-poll 插件）结构转为 LLM 友好的文本。

        已实测字段结构（/t/{id}.json 的 post.polls，为列表可含多个投票）：
            [{"name": "poll", "type": "regular", "status": "open",
              "results": "always|on_vote|on_close",
              "options": [{"id": "...", "html": "选项文本", "votes": 数量(可选)}],
              "voters": 总投票人数, "chart_type": "bar", "title": 投票标题(可为 null)}]

        可见性（已实测确认）：options[].votes 仅在 results=always（或当前用户已投票/
        有权限）时返回；results=on_vote/on_close 时无票数，仅能展示选项与总人数。

        :param polls: post 的 polls 字段（可能为 None）
        :return: 格式化文本；无投票时返回空串
        """
        if not polls:
            return ""
        type_names = {"regular": "单选", "multiple": "多选", "number": "数字", "ranking": "排名"}
        status_names = {"open": "进行中", "closed": "已结束"}
        results_hint = {
            "always": "结果公开",
            "on_vote": "投票后才能查看结果",
            "on_close": "结束后才能查看结果",
        }
        blocks = []
        for poll in polls:
            title = poll.get("title") or poll.get("name") or "投票"
            ptype = type_names.get(poll.get("type"), poll.get("type") or "单选")
            status = status_names.get(poll.get("status"), poll.get("status") or "进行中")
            voters = poll.get("voters")
            voters_text = f" · 共 {voters} 人投票" if voters is not None else ""
            options = poll.get("options") or []
            has_votes = any(o.get("votes") is not None for o in options)
            lines = [f"[投票]《{title}》 类型:{ptype} | 状态:{status}{voters_text}"]
            if has_votes:
                total = sum(o.get("votes") or 0 for o in options)
                for o in options:
                    v = o.get("votes") or 0
                    pct = f"（{v / total * 100:.1f}%）" if total else ""
                    lines.append(f"  - {o.get('html')}: {v} 票{pct}")
            else:
                hint = results_hint.get(poll.get("results"))
                if hint:
                    lines[0] += f"（{hint}）"
                for o in options:
                    lines.append(f"  - {o.get('html')}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def _fetch_topic_pages(self, path: str, params: dict[str, Any], limit: int,
                           page_start: int = 0, max_pages: int = 5,
                           parse: Callable[[dict], dict] | None = None) -> list[dict]:
        """
        通用翻页抓取帖子列表，逐页请求直到凑够 limit 或翻完 max_pages 页。

        适用 Discourse 的 topic_list 系列接口（/latest.json、/top.json、
        /tag/x.json 等，响应含 topic_list.topics）；search.json 的 topics 在
        顶层且 page 为 1-based，单独处理。跨页按 topic id 去重，防止数据
        变动导致重复。

        :param path: 端点路径（如 /latest.json）
        :param params: 除 page 外的查询参数
        :param limit: 目标条数
        :param page_start: page 起始值（0-based 接口用 0，search 用 1）
        :param max_pages: 最大翻页数（鲁棒性保护，防接口异常导致请求过多）
        :param parse: 单条 topic 的解析函数（返回摘要 dict）；None 表示原样返回
        :return: 去重后的摘要列表
        """
        results: list[dict] = []
        seen: set[int] = set()
        for i in range(max_pages):
            data = self._get(path, params={**params, "page": page_start + i})
            topics = data.get("topics") or data.get("topic_list", {}).get("topics", [])
            if not topics:
                break  # 本页无数据说明已到底
            for t in topics:
                tid = t.get("id")
                if tid is None or tid in seen:
                    continue
                seen.add(tid)
                results.append(parse(t) if parse else t)
                if len(results) >= limit:
                    return results
        return results

    # ---------- 对外数据接口 ----------

    def search(self, query: str, page: int = 1, limit: int = 10,
               tags: list[str] | None = None, max_pages: int = 5) -> list[dict]:
        """
        搜索论坛帖子（支持跨页抓取，limit 可超过单页上限）。

        :param query: 搜索关键词（Discourse 支持中文全文检索）
        :param page: 起始页码（从 1 开始；实测 page=0 与 page=1 同为第一页）
        :param limit: 返回条数上限
        :param tags: 可选：标签过滤列表，仅返回带这些标签的帖子（如 ["转专业"]）
        :param max_pages: 最大翻页数（鲁棒性保护）
        :return: 帖子摘要列表，每项含 id/title/excerpt/link/created_at
        """
        # 组合 tag 过滤：Discourse 搜索语法 "关键词 #tag1 #tag2"（已实测可用）
        if tags:
            tag_q = " ".join(f"#{t}" for t in tags)
            query = f"{query} {tag_q}".strip()
        results: list[dict] = []
        seen: set[int] = set()
        for i in range(max_pages):
            data = self._get("/search.json", params={"q": query, "page": page + i})
            # search.json 中 topics[] 是结果主题；摘要不在 topics 里，需从
            # posts[].blurb 按 topic_id 关联提取（已实测确认字段结构）。
            blurb_by_topic: dict[int, str] = {
                p.get("topic_id"): p.get("blurb", "")
                for p in data.get("posts", [])
                if p.get("topic_id") is not None
            }
            topics = data.get("topics", [])
            if not topics:
                break  # 本页无数据说明已到底
            for t in topics:
                tid = t.get("id")
                if tid is None or tid in seen:
                    continue
                seen.add(tid)
                results.append({
                    "id": tid,
                    "title": t.get("title"),
                    "excerpt": self._html_to_text(blurb_by_topic.get(tid, "")),
                    "link": self._topic_link(tid),
                    "created_at": t.get("created_at"),
                    "posts_count": t.get("posts_count"),
                })
                if len(results) >= limit:
                    return results
        return results

    def get_topic(self, topic_id: int, max_posts: int | None = None,
                  max_chars: int | None = None,
                  max_pages: int = 300) -> dict:
        """
        读取单个帖子的内容（楼主正文 + 回复楼层），按需翻页。

        Discourse 的 /t/{id}.json 默认只返回第一页（约 20 层）；page 参数为 1-indexed
        （page=1 第一页、page=2 第二页...），page=0 与 page=1 相同。超大帖的
        post_stream.stream 可能为空，因此以 posts_count 作为总楼层数依据。

        截断策略（鲁棒性）：
            - max_posts：最多返回的楼层数（从楼主开始）；
            - max_chars：按实际清洗后的文本字符数截断（边读边累计，超出即停止翻页）；
            - max_pages：最多翻页数，防止接口异常或超大帖导致无限翻页；
            三重条件取先达者。

        :param topic_id: 帖子 id
        :param max_posts: 最多返回的楼层数（None 表示不限制层数）
        :param max_chars: 最多返回的文本字符数（None 表示不限制）
        :param max_pages: 最多翻页数（鲁棒性保护，默认 300）
        :return: 含 title/link/posts/posts_count/used_chars/truncated 的字典
        """
        data = self._get(f"/t/{topic_id}.json")
        posts_count = data.get("posts_count") or 0
        stream = data.get("post_stream", {}).get("stream", [])
        # stream 对超大帖可能为空，用 posts_count 兜底
        total = posts_count if posts_count else len(stream)

        # 本次需要读取的目标层数（max_posts 显式指定时受限）
        target = total if max_posts is None else min(total, max_posts)

        collected: dict[int, dict] = {}  # post id -> 原始帖子对象
        texts: dict[int, str] = {}       # post id -> 清洗后的纯文本（避免二次解析）
        used_chars = 0                   # 已吸收楼层的文本字符数（字符预算截断用）

        def absorb(posts: list[dict]) -> None:
            """按帖子 id 去重吸收一层帖子，并累计文本字符数。"""
            nonlocal used_chars
            for p in posts:
                if p["id"] in collected:
                    continue
                collected[p["id"]] = p
                text = self._html_to_text(p.get("cooked", ""))
                # 投票（poll）数据不在 cooked 里，单独从 post.polls 字段解析后附在楼层末尾
                polls_text = self._polls_to_text(p.get("polls"))
                if polls_text:
                    text = f"{text}\n\n{polls_text}"
                texts[p["id"]] = text
                used_chars += len(text)

        absorb(data.get("post_stream", {}).get("posts", []))

        # 逐页翻页：以"层数目标 / 字符预算 / 页数上限 / 连续两页无新增"四重条件收敛
        page = 1
        no_new_streak = 0
        char_budget = max_chars if max_chars else float("inf")
        while (len(collected) < target and used_chars < char_budget
               and page < max_pages and no_new_streak < 2):
            page += 1
            page_data = self._get(f"/t/{topic_id}.json", {"page": page})
            before = len(collected)
            absorb(page_data.get("post_stream", {}).get("posts", []))
            no_new_streak = 0 if len(collected) > before else no_new_streak + 1

        # 排序：优先 stream 原始顺序，否则按楼层号
        if stream:
            ordered = [collected[pid] for pid in stream if pid in collected]
        else:
            ordered = sorted(collected.values(), key=lambda p: p.get("post_number") or 0)
        if max_posts is not None:
            ordered = ordered[:max_posts]

        # 按字符预算顺序精截断（保留靠前楼层；楼主层始终保留）
        posts: list[dict] = []
        used = 0
        for p in ordered:
            content = texts[p["id"]]
            if used > 0 and max_chars is not None and used + len(content) > max_chars:
                break
            posts.append({
                "post_number": p.get("post_number"),
                "username": p.get("username"),
                "created_at": p.get("created_at"),
                "content": content,
                # 楼层引用关系（功能3）：该层回复了第几层、回复了谁
                "reply_to_post_number": p.get("reply_to_post_number"),
                "reply_to_username": (p.get("reply_to_user") or {}).get("username"),
            })
            used += len(content)
        return {
            "id": topic_id,
            "title": data.get("title"),
            "link": self._topic_link(topic_id),
            "posts": posts,
            "posts_count": total,
            "used_chars": used,
            "truncated": len(posts) < total,
        }

    def get_topic_range(self, topic_id: int, start_floor: int, end_floor: int) -> dict:
        """
        读取帖子的指定楼层区间 [start_floor, end_floor]（深度阅读的分块读取用）。

        按 page（1-indexed，每页约 20 层）只请求区间覆盖的页面，避免翻完全帖。

        :param topic_id: 帖子 id
        :param start_floor: 起始楼层号（含）
        :param end_floor: 结束楼层号（含）
        :return: 含 title/link/posts/posts_count 的字典
        """
        page_start = max(1, (start_floor - 1) // 20 + 1)
        page_end = (end_floor - 1) // 20 + 1

        collected: dict[int, dict] = {}
        title = None
        posts_count = 0
        for page in range(page_start, page_end + 1):
            d = self._get(f"/t/{topic_id}.json", {"page": page})
            if title is None:
                title = d.get("title")
            posts_count = d.get("posts_count") or posts_count
            for p in d.get("post_stream", {}).get("posts", []):
                collected[p["id"]] = p

        ordered = sorted(collected.values(), key=lambda p: p.get("post_number") or 0)
        ranged = [p for p in ordered if start_floor <= (p.get("post_number") or 0) <= end_floor]

        posts: list[dict] = []
        for p in ranged:
            content = self._html_to_text(p.get("cooked", ""))
            # 投票（poll）数据不在 cooked 里，单独从 post.polls 字段解析后附在楼层末尾
            polls_text = self._polls_to_text(p.get("polls"))
            if polls_text:
                content = f"{content}\n\n{polls_text}"
            posts.append({
                "post_number": p.get("post_number"),
                "username": p.get("username"),
                "created_at": p.get("created_at"),
                "content": content,
                # 楼层引用关系（功能3）：该层回复了第几层、回复了谁
                "reply_to_post_number": p.get("reply_to_post_number"),
                "reply_to_username": (p.get("reply_to_user") or {}).get("username"),
            })
        return {
            "id": topic_id,
            "title": title,
            "link": self._topic_link(topic_id),
            "posts": posts,
            "posts_count": posts_count,
        }

    def get_latest(self, category: int | str | None = None, limit: int = 10,
                   tags: list[str] | None = None,
                   exclude_tags: list[str] | None = None,
                   page: int = 1, max_pages: int = 5) -> list[dict]:
        """
        获取最新帖子列表（支持跨页抓取，limit 可超过单页上限）。

        :param category: 版块过滤（版块 id 或 slug，可选）
        :param limit: 返回条数上限
        :param tags: 可选：仅返回带这些标签的帖子（如 ["日记"]）
        :param exclude_tags: 可选：排除带这些标签的帖子（如 ["日记"]）
        :param page: 起始页码（从 1 开始，1=第一页）
        :param max_pages: 最大翻页数（鲁棒性保护）
        :return: 帖子摘要列表（结构与 search 一致）
        """
        # 排除 tag：/latest.json 不支持负向 tag 参数（已实测 tags=-xxx 与
        # exclude_tags=xxx 均不生效），改用 Discourse 搜索语法
        # "order:latest -tags:xxx" 实现"排除 tag 看最新"（已实测完全排除）。
        if exclude_tags:
            q = "order:latest " + " ".join(f"-tags:{t}" for t in exclude_tags)
            return self.search(q.strip(), limit=limit, page=page)

        params: dict[str, Any] = {"order": "activity"}
        if category is not None:
            params["category"] = category
        if tags:
            params["tags"] = ",".join(tags)

        def parse(t: dict) -> dict:
            """把单个 topic 转成摘要字典。"""
            return {
                "id": t.get("id"),
                "title": t.get("title"),
                "excerpt": self._html_to_text(t.get("excerpt", "")),
                "link": self._topic_link(t["id"]),
                "created_at": t.get("created_at"),
                "posts_count": t.get("posts_count"),
            }

        # /latest.json 的 page 实测从 0 开始；对外 page 从 1 开始，内部减 1
        return self._fetch_topic_pages(
            "/latest.json", params, limit, page_start=page - 1,
            max_pages=max_pages, parse=parse,
        )

    def get_categories(self) -> list[dict]:
        """
        获取论坛版块列表，供 Agent 了解论坛结构。

        :return: 版块列表，每项含 id/name/slug
        """
        data = self._get("/categories.json")
        return [
            {"id": c.get("id"), "name": c.get("name"), "slug": c.get("slug")}
            for c in data.get("category_list", {}).get("categories", [])
        ]

    def get_tags(self) -> list[dict]:
        """
        获取论坛全部标签及其使用数量（按使用数量降序）。

        :return: 标签列表，每项含 name/count
        """
        data = self._get("/tags.json")
        tags = sorted(
            data.get("tags", []),
            key=lambda t: t.get("count") or 0,
            reverse=True,
        )
        return [
            {"name": t.get("name"), "count": t.get("count")}
            for t in tags
        ]

    def get_user_profile(self, username: str) -> dict:
        """
        获取用户公开资料与统计信息（已实测 /u/{username}.json 与 summary 接口）。

        基础资料来自 /u/{username}.json（注册时间、等级、徽章等）；
        统计信息（发帖数、获赞数、在线天数等）来自 /u/{username}/summary.json
        （stats 等字段在 /u/{username}.json 中为 null，必须走 summary 接口）。

        :param username: 用户名
        :return: 含基础资料 + 统计的字典
        """
        data = self._get(f"/u/{username}.json")
        u = data.get("user", {}) or {}
        profile = {
            "username": u.get("username"),
            "name": u.get("name"),
            "title": u.get("title"),
            "trust_level": u.get("trust_level"),
            "badge_count": u.get("badge_count"),
            "profile_view_count": u.get("profile_view_count"),
            "primary_group_name": u.get("primary_group_name"),
            "created_at": u.get("created_at"),
            "last_seen_at": u.get("last_seen_at"),
            "last_posted_at": u.get("last_posted_at"),
            "time_read": u.get("time_read"),
            "accepted_answers": u.get("accepted_answers"),
            "bio_excerpt": u.get("bio_excerpt"),
            "stats": {},
        }
        # 统计信息：单独请求 summary 接口
        try:
            s = self._get(f"/u/{username}/summary.json").get("user_summary", {}) or {}
            profile["stats"] = {
                "topic_count": s.get("topic_count"),
                "post_count": s.get("post_count"),
                "likes_given": s.get("likes_given"),
                "likes_received": s.get("likes_received"),
                "topics_entered": s.get("topics_entered"),
                "posts_read_count": s.get("posts_read_count"),
                "days_visited": s.get("days_visited"),
                "solved_count": s.get("solved_count"),
            }
        except Exception:  # noqa: BLE001  summary 接口异常不影响基础资料返回
            pass
        return profile

    def get_top_topics(self, period: str = "weekly", limit: int = 10,
                       page: int = 1, max_pages: int = 5) -> list[dict]:
        """
        获取热门帖子列表（支持跨页抓取，limit 可超过单页上限）。

        已实测 /top.json 接口，period 支持 daily/weekly/monthly/yearly/all。

        :param period: 统计周期（daily/weekly/monthly/yearly/all），默认 weekly
        :param limit: 返回条数上限
        :param page: 起始页码（从 1 开始，1=第一页）
        :param max_pages: 最大翻页数（鲁棒性保护）
        :return: 帖子摘要列表，每项含 id/title/excerpt/link/created_at/views/like_count/posts_count
        """
        def parse(t: dict) -> dict:
            """把单个 topic 转成摘要字典。"""
            return {
                "id": t.get("id"),
                "title": t.get("title"),
                "excerpt": self._html_to_text(t.get("excerpt", "")),
                "link": self._topic_link(t["id"]),
                "created_at": t.get("created_at"),
                "views": t.get("views"),
                "like_count": t.get("like_count"),
                "posts_count": t.get("posts_count"),
            }

        # /top.json 的 page 实测从 0 开始；对外 page 从 1 开始，内部减 1
        return self._fetch_topic_pages(
            "/top.json", {"period": period}, limit, page_start=page - 1,
            max_pages=max_pages, parse=parse,
        )

    def search_time_range(self, query: str, start_date: str | None = None,
                          end_date: str | None = None, limit: int = 10,
                          tags: list[str] | None = None,
                          page: int | None = None) -> list[dict]:
        """
        按时间范围搜索帖子（按主题创建时间过滤，已实测 in:first after:/before: 语法）。

        Discourse 的 after:/before: 默认过滤楼层回复的创建时间，返回的主题
        created_at 是主题创建时间，两者不一致会导致"区间外的旧帖混入结果"；
        因此必须附加 in:first 限定为楼主帖，从而按主题创建时间过滤（已实测严格生效）。

        :param query: 搜索关键词
        :param start_date: 起始日期（含），格式 YYYY-MM-DD
        :param end_date: 结束日期（含），格式 YYYY-MM-DD
        :param limit: 返回条数上限
        :param tags: 可选：标签过滤列表
        :param page: 起始页码（从 1 开始，1=第一页），可选
        :return: 帖子摘要列表（结构与 search 一致）
        """
        q = f"{query} in:first"
        if start_date:
            q += f" after:{start_date}"
        if end_date:
            q += f" before:{end_date}"
        if tags:
            tag_q = " ".join(f"#{t}" for t in tags)
            q = f"{q} {tag_q}".strip()
        # 复用 search 的跨页抓取（/search.json 的 page 为 1-based）
        return self.search(q, limit=limit, page=page or 1)

    def get_topic_stats(self, topic_id: int) -> dict:
        """
        获取单个帖子的热度统计（浏览量/点赞/回复数，已实测 /t/{id}.json 顶层字段）。

        :param topic_id: 帖子 id
        :return: 含 title/views/like_count/posts_count/reply_count/created_at/last_posted_at 的字典
        """
        data = self._get(f"/t/{topic_id}.json")
        return {
            "id": topic_id,
            "title": data.get("title"),
            "views": data.get("views"),
            "like_count": data.get("like_count"),
            "posts_count": data.get("posts_count"),
            "reply_count": data.get("reply_count"),
            "created_at": data.get("created_at"),
            "last_posted_at": data.get("last_posted_at"),
            "link": self._topic_link(topic_id),
        }

    def get_related_topics(self, topic_id: int, limit: int = 10) -> dict:
        """
        获取某个帖子的相关帖子（功能1）。

        已实测：/t/{id}/similar.json 与 /t/{id}/related.json 在本论坛均返回 404，
        但 /t/{id}.json 响应中自带 Discourse 算法生成的 related_topics 字段（可能为空，
        空时由调用方回退为标题关键词搜索）。本方法复用读帖接口，不产生额外请求。

        :param topic_id: 帖子 id
        :param limit: 返回条数上限
        :return: {"title": 帖子标题, "related": [相关帖子摘要, ...]}
        """
        data = self._get(f"/t/{topic_id}.json")
        related: list[dict] = []
        for t in (data.get("related_topics") or []):
            related.append({
                "id": t.get("id"),
                "title": t.get("title"),
                "link": self._topic_link(t["id"]),
                "created_at": t.get("created_at"),
                "views": t.get("views"),
                "like_count": t.get("like_count"),
                "posts_count": t.get("posts_count"),
            })
            if len(related) >= limit:
                break
        return {"title": data.get("title") or "", "related": related}

    def get_tag_topics(self, tag: str, limit: int = 10,
                       page: int = 1, max_pages: int = 5) -> list[dict]:
        """
        获取某个标签（tag）下的帖子列表（支持跨页抓取，limit 可超过单页上限）。

        已实测 /tag/{tag}.json 接口。

        :param tag: 标签名
        :param limit: 返回条数上限
        :param page: 起始页码（从 1 开始，1=第一页）
        :param max_pages: 最大翻页数（鲁棒性保护）
        :return: 帖子摘要列表，每项含 id/title/excerpt/link/created_at/views/posts_count
        """
        def parse(t: dict) -> dict:
            """把单个 topic 转成摘要字典。"""
            return {
                "id": t.get("id"),
                "title": t.get("title"),
                "excerpt": self._html_to_text(t.get("excerpt", "")),
                "link": self._topic_link(t["id"]),
                "created_at": t.get("created_at"),
                "views": t.get("views"),
                "posts_count": t.get("posts_count"),
            }

        # /tag/{tag}.json 的 page 实测从 0 开始；对外 page 从 1 开始，内部减 1
        return self._fetch_topic_pages(
            f"/tag/{tag}.json", {}, limit, page_start=page - 1,
            max_pages=max_pages, parse=parse,
        )

    def get_user_topics(self, username: str, action_type: str = "topics",
                        limit: int = 10, page: int = 1,
                        max_pages: int = 5) -> list[dict]:
        """
        获取某个用户发起的主题或回复过的帖子（支持跨页抓取，limit 可超过单页上限）。

        已实测 user_actions.json 接口。该接口不支持 page 参数，改用 offset
        递增翻页（每页约 30 条），offset 由页码换算（offset = (page-1) * 30）。

        Discourse 用户活动接口：GET /user_actions.json?username=X&filter=N&offset=0
            - filter=5（action_type="topics"）→ 该用户发起的主题；
            - filter=4（action_type="replies"）→ 该用户回复过的帖子。
        实测返回的每条 user_action 含 topic_id/title/excerpt/post_number/created_at，
        并带 deleted/hidden 标记，用于过滤已被删除或隐藏的内容。

        :param username: 用户名（Discourse 登录名，如 ishuiyuan）
        :param action_type: "topics"（发起的主题）或 "replies"（回复过的帖子）
        :param limit: 返回条数上限
        :param page: 起始页码（从 1 开始，1=第一页）
        :param max_pages: 最大翻页数（鲁棒性保护）
        :return: 帖子摘要列表，每项含 id/title/excerpt/link/created_at
        """
        filter_id = 5 if action_type == "topics" else 4
        results: list[dict] = []
        seen: set[int] = set()
        offset = (page - 1) * 30  # user_actions 每页约 30 条
        for _ in range(max_pages):
            data = self._get("/user_actions.json", params={
                "username": username, "filter": filter_id, "offset": offset,
            })
            actions = data.get("user_actions", [])
            if not actions:
                break  # 本页无数据说明已到底
            offset += len(actions)
            for a in actions:
                if a.get("deleted") or a.get("hidden"):
                    continue  # 跳过已删除/隐藏的活动记录
                topic_id = a.get("topic_id")
                if not topic_id or topic_id in seen:
                    continue
                seen.add(topic_id)
                post_no = a.get("post_number")
                # replies 类型附上楼层号便于定位到具体回复
                link = self._topic_link(topic_id)
                if action_type == "replies" and post_no:
                    link = f"{link}/{post_no}"
                results.append({
                    "id": topic_id,
                    "title": a.get("title") or "",
                    "excerpt": self._html_to_text(a.get("excerpt", "")),
                    "link": link,
                    "created_at": a.get("created_at"),
                })
                if len(results) >= limit:
                    return results
        return results
