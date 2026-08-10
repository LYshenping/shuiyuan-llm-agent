"""
auth.py — 登录态（cookie）管理：水源社区 + jAccount 自动登录。

流程：
    1. 若 storage_state.json 存在且论坛 API 验证通过 → 直接复用；
    2. 否则优先无头自动登录（.env 配置 JACCOUNT_USERNAME/JACCOUNT_PASSWORD 时，
       自动填表 + 图形验证码识别）；
    3. 自动登录不可用（未配凭据/验证码识别失败/短信二次验证）→ 降级为启动
       Playwright 打开有头 Chrome，引导用户手动完成 jAccount 登录。
"""

import json
import time
from pathlib import Path
from typing import Callable

import requests

from forum_client import ForumClient

# jAccount 统一身份认证公共模块：自动填表 + 图形验证码识别
from jaccount import ManualLoginRequiredError, _fill_jaccount_auto, _jaccount_creds

FORUM_URL = "https://shuiyuan.sjtu.edu.cn"


def validate_state(state_file: Path, request_delay: float = 1.0) -> bool:
    """
    验证已保存的登录态是否仍有效（请求 /latest.json 判断是否为 200）。

    :param state_file: storage_state.json 路径
    :param request_delay: 请求间隔秒数
    :return: True=有效，False=失效或文件不存在
    """
    if not state_file.exists():
        return False
    try:
        client = ForumClient(state_file, request_delay=request_delay)
        client.get_latest(limit=1)
        return True
    except Exception:  # 403 或解析异常均视为失效
        return False


def auto_login(state_file: Path, on_wait: Callable[[str], None] | None = None) -> dict:
    """
    无头自动登录水源社区（需要 .env 配置 JACCOUNT_USERNAME / JACCOUNT_PASSWORD）。

    流程：无头 Chrome 打开水源 → 落到 jAccount 登录页 → 自动填表 + 图形验证码
    自动识别 → 跳回水源后保存整个浏览器登录态（storage_state.json，Playwright 格式）。

    仅支持"验证码可自动识别 + 无需短信/二次验证"的路径；触发短信验证码或
    验证码识别失败时抛 ManualLoginRequiredError，由调用方降级为手动登录。

    :param state_file: 登录态保存路径（storage_state.json）
    :param on_wait: 可选回调，用于输出等待提示
    :return: {"ok": True, "message": ...} 或 {"error": ...}
    """
    from playwright.sync_api import sync_playwright

    def log(msg: str) -> None:
        """输出提示信息（有回调则调用回调，否则打印）。"""
        if on_wait:
            on_wait(msg)
        else:
            print(msg, flush=True)

    username, password = _jaccount_creds()
    if not username or not password:
        return {
            "error": "未配置 jAccount 账号密码，请在项目根目录 .env 中设置 "
                     "JACCOUNT_USERNAME 和 JACCOUNT_PASSWORD（用户名是英文登录名而非学号）。",
        }
    try:
        with sync_playwright() as p:
            # 复用系统 Chrome 的无头模式（channel="chrome"），与 jwxt.py 一致
            browser = p.chromium.launch(headless=True, channel="chrome")
            context = browser.new_context()
            page = context.new_page()
            log("[登录] 正在自动登录水源社区（无头模式）...")
            page.goto(FORUM_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)  # 等待可能的 jAccount 跳转/渲染稳定
            if "jaccount" in page.url:
                try:
                    _fill_jaccount_auto(page, username, password)
                except ManualLoginRequiredError:
                    raise  # 需要人工介入，直接上抛
                except Exception as e:  # noqa: BLE001 点击超时等：登录可能已成功触发跳转，继续收集验证
                    log(f"[登录] 自动填表出现异常（{e!r}），继续尝试收集登录态...")
            try:
                page.wait_for_url("**/shuiyuan.sjtu.edu.cn/**", timeout=20000)
            except Exception:  # noqa: BLE001 跳回超时不影响后续判断
                pass
            state = context.storage_state()
            browser.close()
    except ManualLoginRequiredError as e:
        log(f"[登录] 自动登录失败：{e}")
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001 浏览器/网络异常
        log(f"[登录] 自动登录异常：{e!r}")
        return {"error": f"自动登录失败: {e!r}"}

    # 检查是否已拿到水源登录凭证（Discourse 登录后有 _t 或 authentication_data cookie）
    has_auth = any(
        c.get("name") in ("_t", "authentication_data")
        and "shuiyuan" in (c.get("domain") or "")
        for c in state.get("cookies", [])
    )
    if not has_auth:
        return {"error": "自动登录后未检测到水源登录凭证，登录可能未完成。"}
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    log("[登录] 水源自动登录成功，登录态已保存。")
    return {"ok": True, "message": "水源自动登录成功，登录态已保存。"}


def login_via_browser(state_file: Path, on_wait: Callable[[str], None] | None = None) -> None:
    """
    启动有头 Chrome，引导用户手动完成 jAccount 登录并保存登录态。

    :param state_file: 登录态保存路径
    :param on_wait: 可选回调，用于向用户输出等待提示（如打印日志）
    """
    from playwright.sync_api import sync_playwright

    def log(msg: str) -> None:
        """输出提示信息。"""
        if on_wait:
            on_wait(msg)
        else:
            print(msg, flush=True)

    with sync_playwright() as p:
        # 复用系统 Chrome，有头模式便于手动登录
        browser = p.chromium.launch(headless=False, channel="chrome")
        context = browser.new_context()
        page = context.new_page()

        log("[提示] 已打开浏览器窗口，请手动完成 jAccount 登录；")
        log("[提示] 登录成功跳回水源社区后，脚本会自动保存登录态并继续，无需其他操作。")
        log(f"[提示] 目标网址: {FORUM_URL}")
        page.goto(FORUM_URL, wait_until="domcontentloaded", timeout=60000)

        # 轮询检测登录是否成功：
        #   1) URL 已回到 shuiyuan 域名；
        #   2) 存在登录凭证 cookie（Discourse 登录后会种下 CSRF token `_t`
        #      及 SSO 回调产生的 authentication_data）。
        # 每轮打印诊断信息，便于定位检测失败原因。
        log("[等待] 正在等待登录完成（最多 5 分钟）...")

        def has_auth_cookie() -> bool:
            """判断 shuiyuan 域下是否已存在登录凭证 cookie。"""
            names = [c["name"] for c in context.cookies("https://shuiyuan.sjtu.edu.cn/")]
            return "_t" in names or "authentication_data" in names

        deadline = time.time() + 300
        logged_in = False
        while time.time() < deadline:
            try:
                url = page.url
                title = page.title()
                log(f"  [轮询] URL={url} | 标题={title} | 已登录: {has_auth_cookie()}")

                on_forum = url.startswith("https://shuiyuan.sjtu.edu.cn")
                if on_forum and has_auth_cookie():
                    logged_in = True
                    break
            except Exception:  # 浏览器窗口可能被用户提前关闭
                if has_auth_cookie():
                    # 窗口已关但登录凭证已种下 → 视为登录成功
                    log("[检测] 浏览器窗口已关闭，但已检测到登录凭证，视为登录成功。")
                    logged_in = True
                    break
                log("[错误] 浏览器窗口已关闭，未检测到登录完成。")
                browser.close()
                return
            time.sleep(2)

        if not logged_in:
            log("[超时] 5 分钟内未检测到登录完成，请重试。")
            browser.close()
            return

        log("[完成] 检测到登录成功，正在保存登录态...")
        state = context.storage_state()
        state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"[完成] 登录态已保存到 {state_file}")
        browser.close()


def ensure_login(state_file: Path, request_delay: float = 1.0,
                 on_wait: Callable[[str], None] | None = None) -> ForumClient:
    """
    确保登录态可用：已有有效登录态则复用；否则优先自动登录，失败降级为浏览器手动登录。

    :param state_file: storage_state.json 路径
    :param request_delay: 论坛请求间隔秒数
    :param on_wait: 可选日志回调
    :return: 已就绪的 ForumClient
    """
    if validate_state(state_file, request_delay=request_delay):
        if on_wait:
            on_wait("[登录] 已检测到有效登录态，直接复用。")
        return ForumClient(state_file, request_delay=request_delay)

    # 优先无头自动登录（.env 配了 JACCOUNT 凭据时）
    if on_wait:
        on_wait("[登录] 未找到有效登录态，尝试自动登录（需 .env 配置 JACCOUNT 凭据）...")
    result = auto_login(state_file, on_wait=on_wait)
    if not result.get("ok"):
        # 自动登录不可用（未配凭据/验证码失败/短信验证）→ 降级为浏览器手动登录
        if on_wait:
            on_wait(f"[登录] 自动登录不可用（{result.get('error')}），改用浏览器手动登录。")
        login_via_browser(state_file, on_wait=on_wait)

    # 登录完成后再次验证
    if validate_state(state_file, request_delay=request_delay):
        return ForumClient(state_file, request_delay=request_delay)
    raise RuntimeError("登录后验证仍失败，请检查登录是否完成。")
