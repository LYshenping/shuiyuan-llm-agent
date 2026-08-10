"""
course_community.py — 选课社区 course.sjtu.plus 的 API 封装与登录态管理。

选课社区是纯 SPA + 私有 API，所有 /api/ 接口需要登录 cookie 才能访问。
本模块（复用 auth.py 的 Playwright 手动登录模式）：
    1. 打开有头 Chrome 引导用户手动登录选课社区；
    2. 检测登录成功后自动抓取 course.sjtu.plus 域 cookie 保存到
       data/course_cookies.json；
    3. 之后所有 API 调用直接 requests + cookie 即可，无需再开浏览器。

接口路径与返回字段参考学长项目 temp/sjtu-agent-main/sjtu_agent/agent/tools/_core.py
（其 tool_setup_course_community / tool_search_courses / tool_get_course_detail
已实测验证，字段均以该处为准，不另行假设）。
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable

import requests

# jAccount 统一身份认证公共模块（取 JACCOUNT_USERNAME 用户名）
from jaccount import _jaccount_creds

# 选课社区基址（学长项目已验证）
COURSE_PLUS_BASE = "https://course.sjtu.plus"

# cookie 保存路径（与 storage_state.json 类似，独立存放避免混入设置面板白名单）
BASE_DIR = Path(__file__).resolve().parent
COOKIE_PATH = BASE_DIR / "data" / "course_cookies.json"

# 登录流程互斥锁：防止用户重复点击触发多个浏览器窗口
_login_lock = threading.Lock()

# 请求头（学长实现同款：带 Referer 与 UA）
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": COURSE_PLUS_BASE + "/",
}

# 登录过期/未授权时的统一提示（LLM 看到后引导用户重新配置，见 agent.py 提示词）
_NEED_LOGIN_MSG = "选课社区登录已过期，请说「配置选课社区」重新登录"


def _load_cookies() -> dict:
    """
    读取已保存的选课社区 cookie。

    文件缺失或损坏时返回空字典（视为未配置）。

    :return: {cookie名: cookie值, ...}
    """
    try:
        if COOKIE_PATH.exists():
            raw = json.loads(COOKIE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
    except Exception:  # noqa: BLE001 cookie 文件损坏不影响主流程
        pass
    return {}


def _save_cookies(cookies: dict) -> None:
    """
    保存选课社区 cookie 到 data/course_cookies.json。

    :param cookies: {cookie名: cookie值, ...}
    """
    COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_PATH.write_text(
        json.dumps(cookies, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def validate_cookies(cookies: dict | None = None) -> tuple[bool, str]:
    """
    验证选课社区 cookie 是否有效（请求 /api/auth/me 判断）。

    :param cookies: 待验证的 cookie；不传则读已保存的 cookie
    :return: (是否有效, 用户名或错误说明)
    """
    ck = cookies if cookies is not None else _load_cookies()
    if not ck:
        return False, "未配置"
    try:
        r = requests.get(
            COURSE_PLUS_BASE + "/api/auth/me",
            cookies=ck, headers=_HEADERS, timeout=10,
        )
        data = r.json() if r.status_code == 200 else {}
        if r.status_code == 200 and data.get("username"):
            return True, str(data["username"])
        return False, f"cookie 已失效（HTTP {r.status_code}）"
    except Exception as e:  # noqa: BLE001 网络异常视为失效
        return False, f"验证请求失败: {e!r}"


def auto_login(on_wait: Callable[[str], None] | None = None) -> dict:
    """
    无头自动登录选课社区 course.sjtu.plus（填表单方式，学长实测路径）。

    注意：选课社区的登录密码是【选课社区独立密码】，不是 jAccount 密码
    （学长实现注释明确）。密码来源优先级：
        1. .env 中的 COURSE_PLUS_PASSWORD；
        2. 回退尝试 JACCOUNT_PASSWORD（可能不匹配，失败会明确提示）。

    流程：无头 Chrome 打开 /login → 填入 jAccount 用户名 + 选课社区密码 →
    点登录 → 访问 /api/auth/me 验证 → 收集 course.sjtu.plus 域 cookie 保存。

    :param on_wait: 可选日志回调
    :return: {"ok": True, "message": ...} 或 {"error": ...}
    """
    from playwright.sync_api import sync_playwright

    def log(msg: str) -> None:
        """输出提示信息（有回调则调用回调，否则打印）。"""
        if on_wait:
            on_wait(msg)
        else:
            print(msg, flush=True)

    username, jaccount_pwd = _jaccount_creds()
    password = os.environ.get("COURSE_PLUS_PASSWORD", "").strip() or jaccount_pwd
    if not username or not password:
        return {
            "error": "未配置登录凭据：请在 .env 中设置 JACCOUNT_USERNAME，"
                     "并设置 COURSE_PLUS_PASSWORD（选课社区独立密码，非 jAccount 密码）。",
        }
    try:
        with sync_playwright() as pw:
            # 复用系统 Chrome 的无头模式（channel="chrome"）
            browser = pw.chromium.launch(headless=True, channel="chrome")
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            page = ctx.new_page()
            log("[选课社区] 正在自动登录（无头模式）...")
            page.goto(COURSE_PLUS_BASE + "/login", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)

            # 登录表单：第一个 input 为 jAccount 用户名，第二个为选课社区密码（学长实测）
            inputs = page.locator("input")
            if inputs.count() < 2:
                browser.close()
                return {"error": "选课社区登录页结构不符合预期（未找到两个输入框），请改用浏览器手动登录。"}
            inputs.nth(0).fill(username)
            inputs.nth(1).fill(password)
            btn = page.locator("button:has-text('登录')")
            if btn.count() > 0:
                btn.first.click()
                page.wait_for_timeout(3000)
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:  # noqa: BLE001
                    pass

            # 访问 /api/auth/me 验证登录是否成功
            page.goto(COURSE_PLUS_BASE + "/api/auth/me",
                      wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(1000)

            # 收集 course.sjtu.plus 域 cookie
            course_cookies = {
                c["name"]: c["value"]
                for c in ctx.cookies()
                if "course.sjtu.plus" in (c.get("domain") or "")
            }
            browser.close()
    except Exception as e:  # noqa: BLE001 浏览器/网络异常
        log(f"[选课社区] 自动登录异常: {e!r}")
        return {"error": f"选课社区自动登录失败: {e!r}"}

    # 验证 cookie 有效性
    ok, info = validate_cookies(course_cookies)
    if not ok:
        return {
            "error": (
                f"选课社区自动登录失败（{info}）。注意：登录密码是选课社区的独立密码，"
                f"非 jAccount 密码；请确认 .env 中 COURSE_PLUS_PASSWORD 正确，"
                f"或改用浏览器手动登录。"
            ),
        }
    _save_cookies(course_cookies)
    log(f"[选课社区] 自动登录成功（{info}），登录态已保存。")
    return {"ok": True, "message": f"选课社区自动登录成功（{info}），登录态已保存。"}


def login_via_browser(on_wait: Callable[[str], None] | None = None) -> dict:
    """
    打开有头 Chrome 引导用户手动登录选课社区，登录成功后自动保存 cookie。

    复用 auth.py 的手动登录模式：不依赖页面表单结构，只要检测到
    /api/auth/me 返回有效 username 即视为登录成功（最长等待 5 分钟）。

    并发保护：同一时刻只允许一个登录流程，重复触发直接返回"正在登录"。

    :param on_wait: 可选日志回调（如打印到服务端日志）
    :return: {"ok": True, "message": ...} 或 {"error": ...}
    """
    if not _login_lock.acquire(blocking=False):
        return {"error": "选课社区登录流程正在进行中，请在弹出的浏览器窗口完成登录。"}

    def log(msg: str) -> None:
        """输出提示信息（有回调则调用回调，否则打印）。"""
        if on_wait:
            on_wait(msg)
        else:
            print(msg, flush=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _login_lock.release()
        return {"error": "Playwright 未安装，无法自动登录选课社区。"}

    try:
        with sync_playwright() as p:
            # 复用系统 Chrome，有头模式便于手动登录（与 auth.py 一致）
            browser = p.chromium.launch(headless=False, channel="chrome")
            context = browser.new_context()
            page = context.new_page()

            log("[提示] 已打开浏览器窗口，请登录选课社区（course.sjtu.plus）。")
            log("[提示] 登录成功后脚本会自动检测并保存登录态，无需其他操作。")
            log(f"[提示] 目标网址: {COURSE_PLUS_BASE}")
            page.goto(COURSE_PLUS_BASE, wait_until="domcontentloaded", timeout=60000)

            def collect_cookies() -> dict:
                """收集 course.sjtu.plus 域下的全部 cookie。"""
                ck = {}
                for c in context.cookies(COURSE_PLUS_BASE + "/"):
                    ck[c["name"]] = c["value"]
                return ck

            def auth_ok(cookies: dict) -> bool:
                """用当前 cookie 请求 /api/auth/me，返回是否已登录。"""
                ok, _ = validate_cookies(cookies)
                return ok

            log("[等待] 正在等待登录完成（最多 5 分钟）...")
            deadline = time.time() + 300
            logged_in = False
            while time.time() < deadline:
                cookies = collect_cookies()
                if cookies and auth_ok(cookies):
                    logged_in = True
                    break
                time.sleep(2)

            if not logged_in:
                log("[超时] 5 分钟内未检测到登录完成，请重试。")
                browser.close()
                return {"error": "登录超时（5 分钟未检测到登录完成），请重试。"}

            log("[完成] 检测到登录成功，正在保存登录态...")
            _save_cookies(collect_cookies())
            log(f"[完成] 选课社区登录态已保存到 {COOKIE_PATH}")
            browser.close()
            return {"ok": True, "message": "选课社区登录成功，登录态已自动保存。"}
    except Exception as e:  # noqa: BLE001 浏览器启动/导航失败
        log(f"[错误] 选课社区登录失败: {e}")
        return {
            "error": f"选课社区登录失败: {e}",
            "next_action": "请手动访问 https://course.sjtu.plus 登录后重试。",
        }
    finally:
        _login_lock.release()


def course_plus_request(path: str, params: dict | None = None,
                        max_retry: int = 2) -> tuple[dict | None, str | None]:
    """
    调用选课社区 API（携带已保存的 cookie）。

    与学长实现一致的错误分类：
        - 响应体含 unauthorized 错误 → "需要登录"提示；
        - HTTP 401/403（cookie 过期或未授权）→ "需要登录"提示；
        - 404 → "未找到"；
        - 其他 HTTP/网络异常 → 重试后返回错误说明。

    :param path: API 路径（如 /api/course/）
    :param params: 查询参数
    :param max_retry: 最多尝试次数
    :return: (数据 dict 或 None, 错误说明或 None)
    """
    cookies = _load_cookies()
    url = COURSE_PLUS_BASE + path
    last_err = ""
    for attempt in range(max(1, max_retry)):
        try:
            r = requests.get(
                url, params=params or {}, headers=_HEADERS,
                cookies=cookies, timeout=15, allow_redirects=True,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("error"):
                    if "unauthorized" in str(data["error"]).lower():
                        return None, _NEED_LOGIN_MSG
                    return None, str(data.get("error", "未知错误"))
                return data, None
            if r.status_code in (401, 403):
                # 登录过期/未授权：明确归类，避免 LLM 误以为普通请求失败
                return None, _NEED_LOGIN_MSG
            if r.status_code == 404:
                return None, "未找到（404）"
            last_err = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001 网络异常
            last_err = str(e)
            time.sleep(1 + attempt)
    return None, f"选课社区请求失败：{last_err}"


def search_courses(query: str, page_size: int = 8) -> dict:
    """
    在选课社区搜索课程（按课程名/老师/课号），返回候选课程列表。

    :param query: 搜索关键词
    :param page_size: 返回条数（1~20，默认 8）
    :return: 候选课程列表 dict（字段与学长实现一致）
    """
    q = (query or "").strip()
    if not q:
        return {"error": "请提供搜索关键词"}
    size = max(1, min(int(page_size or 8), 20))
    data, err = course_plus_request("/api/course/", {
        "q": q, "page_size": size, "page": 1,
    })
    if err:
        return {"error": err}
    if not data or not isinstance(data, dict):
        return {"error": "选课社区返回了意外的数据格式"}
    items = data.get("items") or []
    if not items:
        return {"message": f"选课社区没有找到与「{q}」相关的课程"}
    results = []
    for item in items[:size]:
        teacher = item.get("main_teacher") or {}
        rating = item.get("rating") or {}
        results.append({
            "id": item.get("id"),
            "code": item.get("code", ""),
            "name": item.get("name", ""),
            "credit": item.get("credit", 0),
            "department": item.get("department", ""),
            "teacher": teacher.get("name", ""),
            "avg_rating": rating.get("avg", 0),
            "review_count": rating.get("count", 0),
            "url": f"{COURSE_PLUS_BASE}/course/{item.get('id')}",
        })
    return {
        "total": data.get("total"),
        "returned": len(results),
        "courses": results,
    }


def get_course_detail(course_id: int, max_reviews: int = 10) -> dict:
    """
    获取选课社区某门课程的详情与最新若干条学生评价。

    :param course_id: 课程 id（来自 search_courses 结果）
    :param max_reviews: 最多返回的评价条数（1~20，默认 10）
    :return: 课程详情 dict（含 reviews 列表，字段与学长实现一致）
    """
    detail, err = course_plus_request(f"/api/course/{int(course_id)}")
    if err:
        return {"error": err}
    if not detail or not isinstance(detail, dict):
        return {"error": "选课社区返回了意外的数据格式"}
    teacher = detail.get("main_teacher") or {}
    rating = detail.get("rating") or {}
    result = {
        "id": detail.get("id"),
        "code": detail.get("code", ""),
        "name": detail.get("name", ""),
        "credit": detail.get("credit", 0),
        "department": detail.get("department", ""),
        "teacher": teacher.get("name", ""),
        "teacher_title": teacher.get("title", ""),
        "avg_rating": rating.get("avg", 0),
        "review_count": rating.get("count", 0),
        "url": f"{COURSE_PLUS_BASE}/course/{int(course_id)}",
    }
    n = max(1, min(int(max_reviews or 10), 20))
    review_data, _ = course_plus_request(
        f"/api/course/{int(course_id)}/review", {
            "order_by": "updated_at", "page_size": n, "page": 1,
        },
    )
    if review_data and isinstance(review_data, dict):
        reviews = []
        for r in (review_data.get("items") or [])[:n]:
            reviews.append({
                "rating": r.get("rating", 0),
                "content": (r.get("content") or "")[:500],
                "semester": r.get("semester", ""),
                "created_at": r.get("created_at", ""),
            })
        result["reviews"] = reviews
        result["review_total"] = review_data.get("total", len(reviews))
    return result
