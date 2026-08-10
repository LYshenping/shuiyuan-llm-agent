"""
web_fetcher.py — 外部链接（网页）抓取客户端。

职责：
    1. 安全校验：仅允许 http/https；DNS 解析后拒绝私网/回环/链路本地等内网地址
       （SSRF 防护），跟随重定向时逐跳重新校验；
    2. 会话隔离：使用独立 requests.Session，绝不携带论坛登录 Cookie
       （防止登录态泄露给第三方网站）；
    3. 内容清洗：HTML → 可读文本（保留链接/图片/代码块/表格/引用），
       Markdown/纯文本/JSON 直读；
    4. GitBook 适配：文档站根链接返回 llms.txt 全站章节索引（便于 Agent 了解
       站内结构后逐页阅读），页面链接返回 .md 干净正文；
    5. 渲染模式：页面内容由脚本动态加载（通知列表页、JS 渲染正文等）时，
       可启用 Playwright（系统 Chrome headless）渲染后再提取；
    6. 资源控制：响应体大小上限 + 返回文本字符上限双重保护，
       防止内存与 LLM 上下文被撑爆。
"""

import ipaddress
import json
import re
import socket
import threading
import urllib.parse
from typing import Any

import requests
from bs4 import BeautifulSoup, NavigableString

# 抓取时使用的浏览器 User-Agent（部分站点对无 UA 请求返回 403）
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 文本类 Content-Type 白名单（PDF/图片/音视频等二进制一律拒绝）
_TEXT_TYPES = {
    "text/html", "application/xhtml+xml",
    "text/markdown", "text/x-markdown", "text/x-gitbook-markdown", "text/plain",
    "application/json", "application/x-json",
}

# 图片类 Content-Type 白名单（视觉模型支持识别的格式）
_IMAGE_TYPES = {
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp",
}

# 跟随重定向的最大次数（防重定向循环）
_MAX_REDIRECTS = 10

# HTML 清洗时剔除的装饰性标签（避免导航/页脚/表单等无关文本混入正文）
_NOISE_TAGS = ("script", "style", "noscript", "nav", "footer", "aside",
               "form", "iframe", "svg", "button")

# Playwright 渲染模式的浏览器单例（懒加载，与 auth.py 一致使用系统 Chrome）
_playwright = None
_browser = None
_browser_lock = threading.Lock()


class SearchRateLimitedError(Exception):
    """搜索引擎触发反爬/安全验证（连续请求可能被限流）时的自定义异常。"""


# 搜索引擎反爬验证页的特征标记（360 触发验证时返回极小页面并含这些字样）
_RATE_LIMIT_MARKS = ("安全验证", "访问过于频繁", "请输入验证码", "验证码")
# 反爬验证页的响应体大小下限（正常结果页通常 > 100KB，验证页约 6KB）
_RATE_LIMIT_MIN_BYTES = 20000


def _is_blocked_ip(ip: str) -> bool:
    """
    判断 IP 是否属于应拦截的内网/特殊地址（SSRF 防护核心）。

    拦截范围：私网（10/8、172.16/12、192.168/16）、回环（127/8、::1）、
    链路本地（169.254/16，含云厂商元数据地址 169.254.169.254）、
    保留/组播/未指定地址。

    :param ip: IP 字符串（IPv4 或 IPv6）
    :return: True 表示应拒绝访问
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # 无法解析的地址一律拒绝
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def _validate_url(url: str) -> str:
    """
    校验并归一化 URL（协议白名单 + 主机名解析后的 IP 白名单）。

    :param url: 原始 URL
    :return: 归一化后的 URL
    :raises ValueError: 协议不支持、缺少主机名或解析到内网/特殊地址
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"仅支持 http/https 链接，收到协议: {parsed.scheme or '（空）'!r}")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("链接缺少主机名")
    # 显式 IP 字面量直接检查；域名则解析全部 IP 逐一检查
    try:
        candidates = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            candidates = sorted({
                item[4][0]
                for item in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
            })
        except socket.gaierror as e:
            raise ValueError(f"域名解析失败: {host}（{e}）") from e
    for ip in candidates:
        if _is_blocked_ip(ip):
            raise ValueError(
                f"链接解析到内网/特殊地址（{ip}），已拒绝访问（SSRF 防护）"
            )
    return urllib.parse.urlunparse(parsed)


def _is_gitbook_url(url: str) -> bool:
    """
    判断 URL 是否为 GitBook 文档站（gitbook.io 域）。

    :param url: 页面链接
    :return: True 表示是 GitBook 站
    """
    host = urllib.parse.urlparse(url).netloc.lower()
    return host == "gitbook.io" or host.endswith(".gitbook.io")


def _get_browser():
    """
    懒加载 Playwright 浏览器单例（系统 Chrome headless），供 JS 渲染模式使用。

    与 auth.py 登录流程一致，使用系统安装的 Chrome（channel="chrome"），
    无需额外下载浏览器内核。启动失败（未装 Chrome/Playwright）时返回 None，
    由调用方回退为普通抓取。

    :return: Playwright Browser 对象；不可用时返回 None
    """
    global _playwright, _browser
    if _browser is not None:
        return _browser
    with _browser_lock:
        if _browser is not None:
            return _browser
        try:
            from playwright.sync_api import sync_playwright
            _playwright = sync_playwright().start()
            _browser = _playwright.chromium.launch(headless=True, channel="chrome")
        except Exception:  # noqa: BLE001 浏览器不可用时渲染模式整体回退
            _playwright = None
            _browser = None
    return _browser


class WebFetcher:
    """抓取外部网页并清洗为 LLM 友好的文本。"""

    def __init__(self, timeout: float = 15.0,
                 max_response_bytes: int = 5 * 1024 * 1024):
        """
        初始化抓取器。

        :param timeout: 单次请求超时（秒），防止慢速响应拖住 Agent
        :param max_response_bytes: 响应体大小上限（字节），超过即截断，防内存耗尽
        """
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        # 独立会话：不加载论坛登录态，防止 cookie 泄露给第三方网站
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    # ---------- 对外接口 ----------

    def fetch(self, url: str, max_chars: int | None = None,
              render: bool = False) -> dict:
        """
        抓取指定链接并返回清洗后的文本。

        :param url: 要阅读的完整链接
        :param max_chars: 返回文本字符上限（None 表示不截断）
        :param render: 是否启用浏览器渲染（Playwright）。页面内容由脚本动态加载
                       （通知列表页、JS 渲染正文等）时设为 True；渲染较慢（约 3~8 秒）
        :return: {"url": 最终访问的 URL, "title": 页面标题或 None,
                  "text": 清洗后的正文, "truncated": 是否因字符上限截断,
                  "js_list_hint": 疑似脚本加载的列表页但未提取到条目}
        :raises ValueError: 链接被安全校验拒绝或内容类型不支持
        :raises requests.RequestException: 网络/HTTP 请求失败
        """
        # 1. 初始 URL 安全校验（协议 + IP）
        url = _validate_url(url)

        # 2. 手动跟随重定向，逐跳重新做 SSRF 校验
        final_url, resp = self._get_with_redirects(url)

        # 3. 读取响应体（流式 + 大小上限）
        data, body_truncated = self._read_body(resp)
        resp.close()

        # 4. 内容类型白名单校验
        content_type = (resp.headers.get("Content-Type") or "").lower().split(";")[0].strip()
        if content_type not in _TEXT_TYPES:
            raise ValueError(
                f"不支持的内容类型: {content_type or '未知'}（仅支持文本网页）"
            )

        text = self._decode_text(data, resp)
        is_html = content_type in ("text/html", "application/xhtml+xml")

        # 5. 渲染模式：内容由脚本动态加载时，用浏览器渲染后再解析（回退普通模式）
        if render and is_html:
            rendered = self._render_html(final_url)
            if rendered is not None:
                text = rendered
                body_truncated = False  # 渲染内容已完整，不再受原响应体截断影响

        # 6. GitBook 适配：站点根 → llms.txt 章节索引；页面链接 → .md 干净正文
        if is_html and _is_gitbook_url(final_url):
            parsed = urllib.parse.urlparse(final_url)
            path_segments = [s for s in parsed.path.split("/") if s]
            if len(path_segments) <= 1:
                # 根/近根候选（路径 ≤1 段）：探测全站章节索引 llms.txt，
                # 便于 Agent 了解站内结构后逐页阅读
                index_url = urllib.parse.urljoin(final_url.rstrip("/") + "/", "llms.txt")
                index = self._fetch_doc(index_url, max_chars)
                if index is not None:
                    return index
            md_url = self._gitbook_markdown_url(final_url)
            if md_url is not None:
                # 页面 .md 版本同样走完整安全校验（递归一次即终止：md 页不是 html）
                page = self._fetch_doc(md_url, max_chars)
                if page is not None:
                    return page
            # 兜底：.md 不可用（页面不存在），直接清洗 HTML 正文

        # 7. 按内容类型清洗
        if is_html:
            title, cleaned = self._parse_html(text, final_url)
            # 列表页 JS 渲染检测：标题像"通知/公告/新闻"列表页，但清洗文本中
            # 没有指向具体详情页（URL 含数字 ID）的链接 → 疑似脚本加载列表
            detail_links = len(
                re.findall(r"\]\(https?://[^)]+/\d+\.html\)", cleaned)
            )
            js_list_hint = (
                detail_links == 0 and title is not None
                and any(k in title for k in ("通知", "公告", "新闻", "列表"))
            )
        elif content_type in ("application/json", "application/x-json"):
            title, cleaned = None, self._json_to_text(text)
            js_list_hint = False
        else:
            title, cleaned = self._extract_markdown_title(text), text
            js_list_hint = False

        # 8. 字符上限截断（正文含标题，截断标记单独追加）
        truncated = body_truncated
        if max_chars and len(cleaned) > max_chars:
            cleaned = cleaned[:max_chars]
            truncated = True
        return {
            "url": final_url,
            "title": title,
            "text": cleaned,
            "truncated": truncated,
            "js_list_hint": js_list_hint,
        }

    def search_web(self, query: str, limit: int = 5) -> list[dict]:
        """
        用搜索引擎搜索网页，返回结果列表（标题/链接/摘要）。

        实现基于 360 搜索（so.com）：纯 HTTP 请求即可解析，结果链接自带
        data-mdurl 真实地址属性（已实测），无需浏览器渲染或解跳转。
        结果中的真实 URL 只作为文本返回；后续 read_url 读取时会再做 SSRF 校验。

        :param query: 搜索关键词（支持 site: 语法限定网站，如
                      "site:www.cs.sjtu.edu.cn 转专业"）
        :param limit: 返回结果条数（1~10）
        :return: [{"title": 标题, "url": 真实链接, "desc": 摘要}, ...]；无结果时为空列表
        :raises requests.RequestException: 搜索请求失败
        :raises SearchRateLimitedError: 触发搜索引擎反爬（连续请求被限流）
        """
        limit = max(1, min(int(limit), 10))
        url = "https://www.so.com/s?" + urllib.parse.urlencode({"q": query})
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        html = resp.text
        # 反爬检测：360 触发验证时会返回极小页面（实测约 6KB，正常约 40 万字节）
        # 并含"安全验证/访问过于频繁"等字样；此时应提示限流而非"无结果"
        if len(html) < _RATE_LIMIT_MIN_BYTES or any(
            m in html for m in _RATE_LIMIT_MARKS
        ):
            raise SearchRateLimitedError(
                "搜索引擎触发了安全验证（连续请求可能被限流），请稍后再试"
            )
        soup = BeautifulSoup(html, "html.parser")
        results: list[dict] = []
        for li in soup.select("li.res-list"):
            a = li.select_one("h3 a")
            if not a:
                continue
            # 360 跳转链接的真身在 data-mdurl 属性里（已实测存在）
            real = (a.get("data-mdurl") or "").strip()
            if not real:
                continue
            desc_el = li.select_one(".res-desc, .res-rich, p")
            desc = desc_el.get_text(" ", strip=True) if desc_el else ""
            results.append({
                "title": a.get_text(" ", strip=True),
                "url": real,
                "desc": desc,
            })
            if len(results) >= limit:
                break
        return results

    def search_github(self, query: str, limit: int = 5) -> list[dict]:
        """
        用 GitHub 官方搜索 API 搜索仓库（无需登录，返回结构化结果）。

        网页版 GitHub 搜索/话题页是 JS 动态渲染，且 360 对 site:github.com 收录差，
        因此单独走 GitHub REST API（api.github.com/search/repositories），
        返回仓库全名、Star 数、语言、描述与主页链接，供 Agent 直接挑选后 read_url 阅读。

        :param query: 搜索关键词（支持 GitHub 限定语法，如
                      "pipe puzzle in:name,description"、"topic:game language:python"）
        :param limit: 返回结果条数（1~20）
        :return: [{"name": 仓库全名, "url": 仓库链接, "desc": 描述,
                   "stars": Star 数, "lang": 主要语言}, ...]；无结果时为空列表
        :raises requests.RequestException: 搜索请求失败
        :raises SearchRateLimitedError: GitHub API 触发限流（未认证约 10 次/分钟）
        """
        limit = max(1, min(int(limit), 20))
        url = "https://api.github.com/search/repositories"
        params = {
            "q": query,
            "sort": "stars",
            "order": "desc",
            "per_page": limit,
        }
        resp = self.session.get(
            url, params=params, timeout=self.timeout,
            headers={"Accept": "application/vnd.github+json"},
        )
        if resp.status_code == 403:
            # 未认证的 GitHub API 有频率限制（约 10 次/分钟），超限返回 403
            raise SearchRateLimitedError(
                "GitHub API 请求过于频繁（未认证约每分钟 10 次），请稍后再试"
            )
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError:  # noqa: BLE001 响应不是合法 JSON
            return []
        results: list[dict] = []
        for item in data.get("items", []) or []:
            results.append({
                "name": item.get("full_name") or item.get("name") or "",
                "url": item.get("html_url") or "",
                "desc": item.get("description") or "",
                "stars": item.get("stargazers_count") or 0,
                "lang": item.get("language") or "",
            })
            if len(results) >= limit:
                break
        return results

    # ---------- 内部方法 ----------

    def _render_html(self, url: str) -> str | None:
        """
        用 Playwright（系统 Chrome headless）渲染页面，返回渲染后的 HTML。

        适用于列表/正文由脚本动态加载的页面（如交大各学院网站的通知公告列表页）。
        渲染较慢（约 3~8 秒），仅在显式请求渲染模式时调用。

        :param url: 页面 URL（已通过安全校验）
        :return: 渲染后的 HTML 字符串；浏览器不可用或渲染失败时返回 None
        """
        browser = _get_browser()
        if browser is None:
            return None
        page = None
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, timeout=self.timeout * 1000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)  # 等待脚本渲染列表/正文
            return page.content()
        except Exception:  # noqa: BLE001 渲染失败由调用方回退普通模式
            return None
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:  # noqa: BLE001 关闭失败不影响主流程
                    pass

    def _get_with_redirects(self, url: str,
                            session: requests.Session | None = None
                            ) -> tuple[str, requests.Response]:
        """
        手动跟随重定向（每跳重新校验，防止重定向跳转到内网）。

        :param url: 已通过初始校验的 URL
        :param session: 使用的会话（默认 self.session，即独立无登录态会话；
                        论坛附件图片可传入带登录 cookie 的会话）
        :return: (最终 URL, 响应对象)
        :raises ValueError: 重定向次数过多
        """
        session = session or self.session
        current = url
        for _ in range(_MAX_REDIRECTS):
            current = _validate_url(current)  # 重定向目标同样做 SSRF 校验
            resp = session.get(
                current, allow_redirects=False, stream=True, timeout=self.timeout
            )
            location = resp.headers.get("Location")
            if resp.status_code in (301, 302, 303, 307, 308) and location:
                resp.close()
                current = urllib.parse.urljoin(current, location)  # 支持相对重定向
                continue
            return current, resp
        raise ValueError("重定向次数过多，已停止跟随")

    def download_image(self, url: str, max_bytes: int | None = None,
                       session: requests.Session | None = None) -> tuple[bytes, str]:
        """
        下载图片字节（供视觉模型理解），带 SSRF 防护与大小限制。

        与 fetch() 的区别：fetch() 仅接受文本内容类型，图片会被拒绝；
        本方法专用于图片类资源，返回 (字节, MIME 类型)。

        会话策略：默认使用独立会话（不携带论坛登录态）；论坛附件图片
        （shuiyuan.sjtu.edu.cn 域）由调用方显式传入带登录 cookie 的会话。

        :param url: 图片完整链接（仅 http/https）
        :param max_bytes: 图片字节数上限（默认 5MB，防内存与 token 撑爆）
        :param session: 可选：带登录态的会话（用于下载需要登录的论坛附件图）
        :return: (图片字节, MIME 类型，如 image/png)
        :raises ValueError: 链接被安全校验拒绝、非图片类型或超过大小上限
        :raises requests.RequestException: 网络/HTTP 请求失败
        """
        max_bytes = max_bytes or self.max_response_bytes
        url = _validate_url(url)
        final_url, resp = self._get_with_redirects(url, session=session)
        try:
            content_type = (resp.headers.get("Content-Type") or "").lower().split(";")[0].strip()
            if content_type not in _IMAGE_TYPES:
                raise ValueError(
                    f"非图片类型: {content_type or '未知'}（仅支持 jpg/png/gif/webp/bmp）"
                )
            data, truncated = self._read_body(resp)
            if truncated:
                raise ValueError(
                    f"图片超过大小上限（{max_bytes} 字节），已拒绝下载"
                )
            return data, content_type
        finally:
            resp.close()

    def _read_body(self, resp: requests.Response) -> tuple[bytes, bool]:
        """
        流式读取响应体，超过大小上限即截断。

        :param resp: 已打开的响应对象
        :return: (响应体字节, 是否因大小上限截断)
        """
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            total += len(chunk)
            if total > self.max_response_bytes:
                return b"".join(chunks), True
            chunks.append(chunk)
        return b"".join(chunks), False

    @staticmethod
    def _decode_text(data: bytes, resp: requests.Response) -> str:
        """
        按响应声明的编码解码响应体。

        requests 对未声明 charset 的 text/* 默认按 ISO-8859-1 解码（RFC 2616 遗留），
        中文站点会因此乱码；此处对 ISO-8859-1 做 utf-8/gbk 探测回退。

        :param data: 响应体字节
        :param resp: 响应对象（取 encoding）
        :return: 解码后的字符串
        """
        enc = resp.encoding or "utf-8"
        if enc.lower() == "iso-8859-1":
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    return data.decode("gbk")
                except UnicodeDecodeError:
                    return data.decode("utf-8", errors="replace")
        try:
            return data.decode(enc, errors="replace")
        except (LookupError, UnicodeDecodeError):
            return data.decode("utf-8", errors="replace")

    def _gitbook_markdown_url(self, url: str) -> str | None:
        """
        把 GitBook 页面链接转为 .md 版本（站点根或无 .md 结尾返回 None）。

        :param url: GitBook 页面链接
        :return: .md 链接；无法转换时返回 None
        """
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.rstrip("/")
        if not path or path.endswith(".md"):
            return None
        return urllib.parse.urlunparse(parsed._replace(path=path + ".md"))

    def _fetch_doc(self, url: str, max_chars: int | None) -> dict | None:
        """
        抓取 GitBook 文档候选链接（llms.txt 或 .md），按内容判断是否可用。

        GitBook 对不存在的页面返回 HTTP 200 + '# Page Not Found' 正文，
        不能只看状态码，需检查内容开头；同时剥离每页顶部的标准提示横幅
        （指向 llms.txt 与 .md 版本的行），避免噪声混入正文。

        :param url: 候选链接
        :param max_chars: 返回文本字符上限
        :return: 抓取结果；链接不可用（404 页/网络错误/被安全校验拒绝）时返回 None
        """
        try:
            result = self.fetch(url, max_chars)
        except (ValueError, requests.RequestException):
            return None
        text = (result.get("text") or "").lstrip()
        if text.startswith("# Page Not Found"):
            return None
        # 剥离 GitBook 标准提示横幅（llms.txt / .md 版本引导），控制 token
        cleaned = "\n".join(
            line for line in (result["text"] or "").splitlines()
            if not line.lstrip().startswith("> For the complete documentation index")
            and not line.lstrip().startswith("> Markdown versions of documentation pages")
        )
        result["text"] = cleaned.strip()
        return result

    def _parse_html(self, html: str, base_url: str) -> tuple[str | None, str]:
        """
        解析 HTML：提取标题与正文并清洗为可读文本。

        :param html: 页面 HTML
        :param base_url: 页面 URL（用于把相对链接转绝对链接）
        :return: (页面标题或 None, 清洗后的正文文本)
        """
        if not html:
            return None, ""
        soup = BeautifulSoup(html, "html.parser")
        # 剔除脚本/样式与装饰区块，避免无关文本混入正文
        for tag in soup(_NOISE_TAGS):
            tag.decompose()
        # 优先取正文容器；无命中时退回整页（去噪后）。
        # 覆盖通用容器（main/article 等）与博达网站群 CMS 的正文容器
        # （.ny-cont/.v_news_content/.vsb_content，交大各院系/部门网站同源）
        main = (soup.select_one("main") or soup.select_one("article")
                or soup.select_one("[role='main']") or soup.select_one(".markdown-body")
                or soup.select_one(".post-content")
                or soup.select_one(".ny-cont")
                or soup.select_one(".v_news_content")
                or soup.select_one(".vsb_content")
                or soup.select_one("#vsb_content")
                or soup.select_one(".article-content")
                or soup.body or soup)

        def resolve(u: str) -> str:
            """相对链接/协议相对链接转绝对 URL（供链接与图片占位使用）。"""
            if u.startswith("//"):
                return "https:" + u
            if u.startswith("/"):
                return urllib.parse.urljoin(base_url, u)
            return u

        # 代码块 → fenced code block（带语言标记）
        for pre in main.find_all("pre"):
            code = pre.find("code") or pre
            lang = ""
            for cls in (code.get("class") or []):
                if cls.startswith("lang-"):
                    lang = cls[len("lang-"):]
                    break
            txt = code.get_text("\n").strip()
            pre.replace_with(
                NavigableString(f"\n```{lang}\n{txt}\n```\n" if txt else "")
            )
        # 图片 → [图片: 描述](URL)
        for img in main.find_all("img"):
            alt = (img.get("alt") or "").strip()
            src = resolve((img.get("src") or img.get("data-src") or "").strip())
            img.replace_with(
                NavigableString(f"[图片: {alt}]({src})" if src else f"[图片: {alt}]")
            )
        # 链接 → [文本](URL)
        for a in main.find_all("a"):
            txt = a.get_text(" ", strip=True)
            href = resolve((a.get("href") or "").strip())
            a.replace_with(NavigableString(f"[{txt}]({href})" if href else txt))
        # 表格 → 类 Markdown 表格（首行按表头处理，单元格用 | 分隔）
        for tbl in main.find_all("table"):
            rows = []
            for tr in tbl.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                rows.append("| " + " | ".join(cells) + " |")
            tbl.replace_with(
                NavigableString("\n" + "\n".join(rows) + "\n" if rows else "")
            )
        # 引用块 → > 引用文本
        for q in main.find_all("blockquote"):
            qtxt = q.get_text("\n").strip()
            q.replace_with(
                NavigableString("\n" + "\n".join("> " + ln for ln in qtxt.splitlines())
                                + "\n" if qtxt else "")
            )
        # 块级元素换行分隔，保留可读结构
        for br in main.find_all("br"):
            br.replace_with("\n")
        for p in main.find_all(["p", "div", "li", "h1", "h2", "h3", "h4", "tr"]):
            p.append("\n")
        lines = [ln.strip() for ln in main.get_text("\n").splitlines() if ln.strip()]
        title = (soup.title.string.strip() if soup.title and soup.title.string else None)
        return title, "\n".join(lines)

    @staticmethod
    def _json_to_text(text: str) -> str:
        """
        把 JSON 响应格式化为可读文本（解析失败时原样返回）。

        :param text: 原始 JSON 字符串
        :return: 格式化后的文本
        """
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return text
        return json.dumps(obj, ensure_ascii=False, indent=1)

    @staticmethod
    def _extract_markdown_title(text: str) -> str | None:
        """
        从 Markdown/纯文本中提取首个一级标题作为页面标题。

        :param text: 文本内容
        :return: 标题或 None
        """
        for line in text.splitlines():
            if line.startswith("# "):
                return line[2:].strip()
        return None
