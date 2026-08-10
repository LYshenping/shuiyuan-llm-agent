"""
jwxt.py — 教学信息服务网（i.sjtu.edu.cn）客户端：jAccount 登录 + 课表 + 成绩查询。

背景：
    i.sjtu.edu.cn 的课表/成绩页面是 jqGrid 壳，底层均有结构化 JSON 接口
    （学长项目 temp/sjtu-agent-main/ 已实测验证，本模块接口与字段以其为准）：
        - 课表：POST /kbcx/xskbcx_cxXsKb.html   body={xnm, xqm}          → kbList[]
        - 成绩：SSO 后 POST /cjcx/cjcx_cxXsgrcj.html?doType=query&gnmkdm=N305005 → items[]

登录态：
    独立保存 i.sjtu.edu.cn 域 cookie 到 data/jwxt_cookies.json（类似 course_cookies.json），
    与水源登录态（storage_state.json）互不干扰。jAccount 账号密码从 .env 读取
    （JACCOUNT_USERNAME / JACCOUNT_PASSWORD，注意用户名是英文登录名而非学号）。

登录模式：
    1. 自动模式：配置了 JACCOUNT 凭据时，无头 Playwright 自动填表 + 图形验证码自动识别
       （思源极客协会 ResNet API → 手动兜底）。检测到短信/二次验证时抛
       ManualLoginRequiredError，提示改用浏览器手动登录。
    2. 手动模式：有头浏览器打开 i.sjtu.edu.cn，用户自行完成登录（含短信验证），
       检测到登录成功 cookie 后自动保存（与 auth.py 水源登录同模式）。
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Callable

import requests

# jAccount 统一身份认证公共模块：自动填表 + 图形验证码识别（供水源/教学网/选课社区复用）
from jaccount import ManualLoginRequiredError, _fill_jaccount_auto, _jaccount_creds

# 教学信息服务网基址
JWXT_BASE = "https://i.sjtu.edu.cn"

# i.sjtu.edu.cn 域 cookie 保存路径（独立于水源登录态）
BASE_DIR = Path(__file__).resolve().parent
COOKIE_PATH = BASE_DIR / "data" / "jwxt_cookies.json"

# 每节课的开始/结束时间（SJTU 作息，学长项目 _SLOT_TIMES 同源）
_SLOT_TIMES: list[tuple[str, str]] = [
    ("8:00", "8:45"), ("8:55", "9:40"),      # 第 1~2 节
    ("10:00", "10:45"), ("10:55", "11:40"),  # 第 3~4 节
    ("12:00", "12:45"), ("12:55", "13:40"),  # 第 5~6 节
    ("14:00", "14:45"), ("14:55", "15:40"),  # 第 7~8 节
    ("16:00", "16:45"), ("16:55", "17:40"),  # 第 9~10 节
    ("18:00", "18:45"), ("18:55", "19:40"),  # 第 11~12 节
    ("20:00", "20:45"), ("20:55", "21:40"),  # 第 13~14 节
]

_WEEKDAY_CN = ["", "周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 成绩接口的学期参数映射（学长的 _XQM_MAP 同源）：对外 semester 1/2/3 → 接口 xqm
_XQM_MAP = {"1": "3", "2": "12", "3": "16", "": ""}

# 成绩查询页与数据接口（学长实测使用 gnmkdm=N305005，当前学生成绩）
_GRADE_PAGE_URL = JWXT_BASE + "/cjcx/cjcx_cxDgXscj.html?gnmkdm=N305005"
_GRADE_API_URL = JWXT_BASE + "/cjcx/cjcx_cxXsgrcj.html?doType=query&gnmkdm=N305005"

def _log(on_wait: Callable[[str], None] | None, msg: str) -> None:
    """输出登录/执行过程提示（有回调则调用回调，否则打印到 stdout）。"""
    if on_wait:
        on_wait(msg)
    else:
        print(msg, flush=True)


# ==================== cookie 读写 ====================

# cookie 存储结构：{"i.sjtu.edu.cn": {name: value}, "jaccount.sjtu.edu.cn": {...}}
# 两个域都要保存：i.sjtu.edu.cn 域是业务 session（课表/成绩接口直接携带）；
# jaccount.sjtu.edu.cn 域用于 SSO 过期后的自动重新认证（成绩查询会走一遍 SSO）。


def _load_cookies() -> dict:
    """
    读取已保存的 jAccount 相关 cookie。

    文件缺失或损坏时返回空字典（视为未登录）。
    兼容旧版扁平格式：直接 {name: value} 时视为 i.sjtu.edu.cn 域。

    :return: {"i.sjtu.edu.cn": {name: value}, "jaccount.sjtu.edu.cn": {...}}
    """
    try:
        if COOKIE_PATH.exists():
            raw = json.loads(COOKIE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                if "i.sjtu.edu.cn" in raw or "jaccount.sjtu.edu.cn" in raw:
                    return raw
                return {"i.sjtu.edu.cn": raw, "jaccount.sjtu.edu.cn": {}}
    except Exception:  # noqa: BLE001 cookie 文件损坏不影响主流程
        pass
    return {"i.sjtu.edu.cn": {}, "jaccount.sjtu.edu.cn": {}}


def _save_cookies(iwxt: dict, jaccount: dict | None = None) -> None:
    """
    保存 cookie 到 data/jwxt_cookies.json。

    :param iwxt: i.sjtu.edu.cn 域 cookie {name: value}
    :param jaccount: jaccount.sjtu.edu.cn 域 cookie {name: value}（可省略）
    """
    data = {"i.sjtu.edu.cn": iwxt, "jaccount.sjtu.edu.cn": jaccount or {}}
    COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _collect_browser_cookies(ctx) -> dict:
    """
    从 Playwright 浏览器上下文收集 jAccount 相关 cookie（按域分组）。

    :param ctx: Playwright BrowserContext
    :return: {"i.sjtu.edu.cn": {name: value}, "jaccount.sjtu.edu.cn": {...}}
    """
    iwxt = {}
    jaccount = {}
    for c in ctx.cookies():
        domain = (c.get("domain") or "").lstrip(".")
        if domain == "i.sjtu.edu.cn" or domain.endswith(".i.sjtu.edu.cn"):
            iwxt[c["name"]] = c["value"]
        elif domain == "jaccount.sjtu.edu.cn" or domain.endswith(".jaccount.sjtu.edu.cn"):
            jaccount[c["name"]] = c["value"]
    return {"i.sjtu.edu.cn": iwxt, "jaccount.sjtu.edu.cn": jaccount}


# ==================== 登录态验证 ====================


def validate_cookies(cookies: dict | None = None) -> bool:
    """
    验证 i.sjtu.edu.cn 登录态是否有效（POST 课表接口判断响应是否含 kbList）。

    课表接口无需隐藏表单字段，直接带 cookie 请求即可，适合作为登录态探针。

    :param cookies: 待验证的 cookie；不传则读已保存的 cookie。
                    兼容 {"i.sjtu.edu.cn": {...}} 与旧版扁平 {name: value} 两种格式
    :return: True=有效，False=失效或未配置
    """
    if cookies is None:
        cookies = _load_cookies()
    if "i.sjtu.edu.cn" in cookies:      # 新版按域分组结构
        ck = cookies.get("i.sjtu.edu.cn") or {}
    else:                                # 旧版扁平结构
        ck = cookies
    if not ck:
        return False
    try:
        r = requests.post(
            JWXT_BASE + "/kbcx/xskbcx_cxXsKb.html",
            data={"xnm": "2025", "xqm": "12"},
            cookies=ck,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        # 有效 session：返回 JSON 且含 kbList 字段；未登录会被重定向到 jAccount
        return r.status_code == 200 and "kbList" in r.text
    except Exception:  # noqa: BLE001 网络异常视为失效
        return False


def _refresh_session(on_wait: Callable[[str], None] | None = None) -> bool:
    """
    用已保存的 jAccount cookie 通过 SSO 刷新 i.sjtu.edu.cn session，并写回 cookie 文件。

    i.sjtu.edu.cn 的 session 有效期较短（数小时到一天级），过期后 requests 直连
    课表接口会被重定向到登录页。本方法无头打开 jaccountlogin，若 jAccount 域
    session 仍有效会自动跳回 i.sjtu.edu.cn 并建立新 session。

    :param on_wait: 可选日志回调
    :return: True=刷新成功（新 session 已保存）
    """
    from playwright.sync_api import sync_playwright

    saved = _load_cookies()
    iwxt_ck = saved.get("i.sjtu.edu.cn") or {}
    jaccount_ck = saved.get("jaccount.sjtu.edu.cn") or {}
    try:
        with sync_playwright() as pw:
            # 复用系统 Chrome 的无头模式（channel="chrome"）
            browser = pw.chromium.launch(headless=True, channel="chrome")
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            ctx.add_cookies(
                [
                    {"name": k, "value": v, "domain": ".i.sjtu.edu.cn", "path": "/"}
                    for k, v in iwxt_ck.items()
                ]
                + [
                    {"name": k, "value": v, "domain": ".jaccount.sjtu.edu.cn", "path": "/"}
                    for k, v in jaccount_ck.items()
                ]
            )
            page = ctx.new_page()
            page.goto("https://i.sjtu.edu.cn/jaccountlogin",
                      wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)
            # 仍停在 jAccount → jAccount session 也过期，SSO 无法自动完成
            if "jaccount" in page.url:
                browser.close()
                return False
            collected = _collect_browser_cookies(ctx)
            browser.close()
    except Exception:  # noqa: BLE001 浏览器/网络异常视为刷新失败
        return False
    if not collected.get("i.sjtu.edu.cn"):
        return False
    _save_cookies(collected.get("i.sjtu.edu.cn"), collected.get("jaccount.sjtu.edu.cn"))
    _log(on_wait, "[jAccount] session 已刷新并保存。")
    return True


def auto_login(on_wait: Callable[[str], None] | None = None) -> dict:
    """
    无头自动登录 i.sjtu.edu.cn（需要 .env 配置 JACCOUNT_USERNAME / JACCOUNT_PASSWORD）。

    流程：无头 Chrome 打开 i.sjtu.edu.cn → 落到 jAccount 登录页 → 自动填表 +
    验证码自动识别 → 跳回 i.sjtu.edu.cn 后收集该域 cookie 保存。

    :param on_wait: 可选日志回调
    :return: {"ok": True, "message": ...} 或 {"error": ...}
    """
    from playwright.sync_api import sync_playwright

    username, password = _jaccount_creds()
    if not username or not password:
        return {
            "error": "未配置 jAccount 账号密码，请在项目根目录 .env 中设置 "
                     "JACCOUNT_USERNAME 和 JACCOUNT_PASSWORD（用户名是英文登录名而非学号）。",
        }
    try:
        with sync_playwright() as p:
            # 复用系统 Chrome 的无头模式（channel="chrome"），无需 playwright install 下载内核，
            # 与 auth.py / web_fetcher.py 的用法保持一致
            browser = p.chromium.launch(headless=True, channel="chrome")
            ctx = browser.new_context()
            page = ctx.new_page()
            _log(on_wait, "[jAccount] 正在自动登录教学信息服务网（无头模式）...")
            page.goto(JWXT_BASE, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)  # 等待可能的跳转/渲染稳定
            # session 过期时可能重定向到 i.sjtu.edu.cn/xtgl/login_slogin.html
            # （URL 仍是 i.sjtu 域、不含 jaccount 字样），需统一强制走一次 SSO
            if "jaccount" in page.url or "login" in page.url:
                _log(on_wait, "[jAccount] 检测到需登录，执行 jAccount SSO...")
                page.goto(JWXT_BASE + "/jaccountlogin",
                          wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(2000)
                if "jaccount" in page.url:
                    try:
                        _fill_jaccount_auto(page, username, password)
                    except ManualLoginRequiredError:
                        raise  # 需要人工介入，直接上抛
                    except Exception as e:  # noqa: BLE001 点击超时等：登录可能已成功触发跳转，继续收集验证
                        _log(on_wait, f"[jAccount] 自动填表出现异常（{e!r}），继续尝试收集登录态...")
            # 等待回到 i.sjtu.edu.cn
            try:
                page.wait_for_url("**/i.sjtu.edu.cn/**", timeout=20000)
            except Exception:  # noqa: BLE001
                pass
            collected = _collect_browser_cookies(ctx)
            browser.close()
    except ManualLoginRequiredError as e:
        _log(on_wait, f"[jAccount] 自动登录失败：{e}")
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001 浏览器/网络异常
        _log(on_wait, f"[jAccount] 自动登录异常：{e!r}")
        return {"error": f"自动登录失败: {e!r}"}

    # 保存前校验：cookie 必须真实有效，否则视为失败（避免"假成功"覆盖有效登录态）
    if not validate_cookies(collected):
        return {"error": "自动登录后 i.sjtu.edu.cn 登录态无效，请重试或改用浏览器手动登录。"}
    _save_cookies(collected.get("i.sjtu.edu.cn"), collected.get("jaccount.sjtu.edu.cn"))
    _log(on_wait, f"[jAccount] 自动登录成功，已保存登录态。")
    return {"ok": True, "message": "jAccount 自动登录成功，登录态已保存。"}


def login_via_browser(on_wait: Callable[[str], None] | None = None) -> dict:
    """
    有头浏览器手动登录 i.sjtu.edu.cn（适用于短信验证码/异地二次验证等无法自动化的场景）。

    用户在弹出的 Chrome 窗口中自行完成登录（含短信验证码），脚本轮询检测
    登录成功 cookie 后自动保存。与 auth.py 水源登录同模式。

    :param on_wait: 可选日志回调
    :return: {"ok": True, "message": ...} 或 {"error": ...}
    """
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, channel="chrome")
            context = browser.new_context()
            page = context.new_page()
            _log(on_wait, "[提示] 已打开浏览器窗口，请在窗口中完成 jAccount 登录（含短信验证码）。")
            _log(on_wait, "[提示] 登录成功后脚本会自动检测并保存登录态，无需其他操作。")
            _log(on_wait, f"[提示] 目标网址: {JWXT_BASE}")
            page.goto(JWXT_BASE, wait_until="domcontentloaded", timeout=60000)

            _log(on_wait, "[等待] 正在等待登录完成（最多 5 分钟）...")
            deadline = time.time() + 300
            logged_in = False
            collected = None
            while time.time() < deadline:
                collected = _collect_browser_cookies(context)
                if collected.get("i.sjtu.edu.cn") and validate_cookies(collected):
                    logged_in = True
                    break
                time.sleep(2)

            if not logged_in:
                _log(on_wait, "[超时] 5 分钟内未检测到登录完成，请重试。")
                browser.close()
                return {"error": "登录超时（5 分钟未检测到登录完成），请重试。"}

            _save_cookies(collected.get("i.sjtu.edu.cn"), collected.get("jaccount.sjtu.edu.cn"))
            _log(on_wait, "[完成] 教学信息服务网登录成功，登录态已保存。")
            browser.close()
            return {"ok": True, "message": "教学信息服务网登录成功，登录态已自动保存。"}
    except Exception as e:  # noqa: BLE001 浏览器启动/导航失败
        _log(on_wait, f"[错误] 手动登录失败: {e}")
        return {"error": f"手动登录失败: {e!r}"}


def ensure_login(on_wait: Callable[[str], None] | None = None) -> bool:
    """
    确保教学信息服务网登录态可用：已有有效登录态直接复用，否则自动登录，再降级手动登录。

    :param on_wait: 可选日志回调
    :return: True=登录态可用
    """
    if validate_cookies():
        _log(on_wait, "[jAccount] 已检测到有效的教学信息服务网登录态，直接复用。")
        return True
    _log(on_wait, "[jAccount] 未找到有效登录态，尝试自动登录（需 .env 配置 JACCOUNT 凭据）...")
    result = auto_login(on_wait=on_wait)
    if result.get("ok"):
        return True
    _log(on_wait, f"[jAccount] 自动登录不可用（{result.get('error')}），改用浏览器手动登录。")
    result = login_via_browser(on_wait=on_wait)
    return bool(result.get("ok"))


# ==================== 课表 ====================


def _parse_week_set(zcd: str) -> set[int]:
    """
    解析周次字符串（如 '1-16周'、'1,3,5-10周(单)'）为周次集合。

    :param zcd: 课表接口返回的周次字段（如 "1-16周"）
    :return: 周次集合，如 {1,2,3,...}
    """
    weeks: set[int] = set()
    for part in str(zcd).split(","):
        part = part.strip()
        step = 1
        if "(单)" in part:
            step = 2
            part = part.replace("(单)", "")
        if "(双)" in part:
            step = 2
            part = part.replace("(双)", "")
        part = part.replace("周", "").strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            for w in range(int(a), int(b) + 1, step):
                weeks.add(w)
        elif part.isdigit():
            weeks.add(int(part))
    return weeks


def _parse_jcs(jcs: str) -> tuple[int, int]:
    """
    解析节次字符串：'3-4' → (3, 4)；单节 '5' → (5, 5)。

    :param jcs: 课表接口返回的节次字段（如 "3-4"）
    :return: (起始节, 结束节)
    """
    parts = str(jcs).split("-")
    start = int(parts[0])
    end = int(parts[-1])
    return start, end


def _auto_year_term() -> tuple[str, str]:
    """
    根据当前月份自动判断学年与接口 xqm 参数（学长 _auto_year_term 同源）。

    :return: (学年起始年, xqm)：秋季(9-1月)为 (当年, "3")，春季(2-8月)为 (上年, "12")
    """
    m = time.localtime().tm_mon
    y = time.localtime().tm_year
    if m >= 9:            # 秋季（当年9月 - 次年1月）
        return str(y), "3"
    elif m == 1:          # 1 月仍属上一学年秋季
        return str(y - 1), "3"
    else:                 # 春季（2-8月）
        return str(y - 1), "12"


def fetch_schedule(year: str = "", term: str = "", refresh: bool = False,
                   on_wait: Callable[[str], None] | None = None) -> dict:
    """
    从教学信息服务网拉取课表（POST JSON 接口，无需 PDF）。

    session 过期处理：若请求被重定向到登录页，自动用 jAccount cookie SSO 刷新
    一次并重试（jAccount session 仍有效时对用户无感）。

    :param year: 学年起始年（如 "2025" 表示 2025-2026）；留空自动判断
    :param term: "1"=秋季 / "2"=春季；留空自动判断
    :param refresh: True=忽略登录态探针直接请求（仅用于登录态验证后确认）
    :param on_wait: 可选日志回调
    :return: {"courses": [...], "year": str, "term": str, "total": int, "error": str|None}
             courses 每项含 name/code/teacher/location/campus/day/slot/time/week_str
    """
    auto_year, auto_xqm = _auto_year_term()
    if not year:
        year = auto_year
    if not term:
        xqm = auto_xqm
        term = "1" if xqm == "3" else "2"
    else:
        xqm = "3" if term == "1" else "12"

    cookies = (_load_cookies().get("i.sjtu.edu.cn") or {})
    if not cookies:
        return {"error": "教学信息服务网未配置登录态，请先登录（setup_jwxt / 设置面板）。"}

    # 最多尝试两次：第一次直接请求；被重定向到登录页时自动刷新 session 后重试一次
    r = None
    for attempt in range(2):
        try:
            r = requests.post(
                JWXT_BASE + "/kbcx/xskbcx_cxXsKb.html",
                data={"xnm": year, "xqm": xqm},
                cookies=cookies,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": JWXT_BASE + "/",
                },
                timeout=15,
            )
        except Exception as e:  # noqa: BLE001 网络异常
            return {"error": f"教学信息服务网请求异常: {e!r}"}

        if r.status_code != 200:
            return {"error": f"教学信息服务网请求失败: HTTP {r.status_code}"}
        if "jAccount" in r.text or "login" in r.url:
            # session 过期：尝试自动刷新后重试
            if attempt == 0 and _refresh_session(on_wait=on_wait):
                cookies = (_load_cookies().get("i.sjtu.edu.cn") or {})
                if cookies:
                    continue
            return {"error": "教学信息服务网登录态已过期，请重新登录（setup_jwxt / 设置面板）。"}
        break  # 请求成功且未被重定向

    try:
        data = r.json()
    except ValueError:  # noqa: BLE001 非 JSON 响应
        return {"error": "教学信息服务网返回了非 JSON 数据，登录态可能已过期。"}

    courses = []
    for item in data.get("kbList", []):
        jcs = item.get("jcs", "1-1")
        slot_s, slot_e = _parse_jcs(jcs)
        week_set = _parse_week_set(item.get("zcd", ""))
        courses.append({
            "name":       item.get("kcmc", ""),
            "code":       item.get("kch", ""),
            "teacher":    item.get("xm", ""),
            "location":   item.get("cdmc", ""),
            "campus":     item.get("xqmc", ""),
            "day":        int(item.get("xqj", 0) or 0),  # 1=周一 … 7=周日
            "slot_start": slot_s,
            "slot_end":   slot_e,
            "time_start": _SLOT_TIMES[slot_s - 1][0] if 1 <= slot_s <= 14 else "",
            "time_end":   _SLOT_TIMES[slot_e - 1][1] if 1 <= slot_e <= 14 else "",
            "weeks":      sorted(week_set),
            "week_str":   item.get("zcd", ""),
        })
    courses.sort(key=lambda c: (c["day"], c["slot_start"]))
    return {
        "courses": courses,
        "year": year,
        "term": term,
        "total": len(courses),
        "error": None,
    }


def format_schedule(data: dict) -> str:
    """
    把 fetch_schedule 结果格式化为 LLM 友好的文本。

    :param data: fetch_schedule 的返回
    :return: 格式化文本（按星期几分组展示）
    """
    if data.get("error"):
        return f"[错误] {data['error']}"
    courses = data.get("courses") or []
    if not courses:
        return "（该学期暂无课表数据）"
    term_cn = "秋季" if data.get("term") == "1" else "春季"
    lines = [f"课表（{data.get('year')}-{int(data['year']) + 1} 学年·{term_cn}学期，共 {len(courses)} 门）："]
    cur_day = None
    for c in courses:
        day = c["day"]
        if day != cur_day:
            cur_day = day
            lines.append(f"\n【{_WEEKDAY_CN[day] if 1 <= day <= 7 else '未知'}】")
        week = c.get("week_str") or ""
        lines.append(
            f"  - {c['name']}（{c['code']}） 第{c['slot_start']}-{c['slot_end']}节"
            f"（{c['time_start']}-{c['time_end']}） {week}"
            f" {c.get('campus') or ''}{c.get('location') or ''}"
            f"{(' / ' + c['teacher']) if c.get('teacher') else ''}"
        )
    return "\n".join(lines)


# ==================== 成绩 ====================


def query_grades(year: str = "", semester: str = "") -> dict:
    """
    查询成绩（Playwright SSO + jqGrid JSON 接口，自动计算加权 GPA）。

    流程（学长 tool_query_grades 同源）：
        1. 注入 jAccount cookie 打开 i.sjtu.edu.cn/jaccountlogin 完成 SSO；
        2. 访问成绩查询页提取隐藏表单字段（含身份信息）；
        3. 逐个 POST 数据接口 cjcx_cxXsgrcj.html 拿 items[]。

    学期覆盖策略（已实测：xnm/xqm 传空串时接口只返回"第一学期"，不会返回全部）：
        - 显式传了 year/semester → 精确查询该组合；
        - 未传学年 → 从当前学年回溯最多 6 个学年；
        - 未传学期 → 遍历秋(3)/春(12)/夏(16) 三个学期。
        每个 (学年, 学期) 组合单独请求，全部合并后按学年/学期倒序排列。

    :param year: 学年起始年（如 "2025" 表示 2025-2026）；空=自动回溯最近 6 个学年
    :param semester: "1"=秋 / "2"=春 / "3"=夏；空=三个学期都查
    :return: {"count", "weighted_gpa", "total_credits", "grades": [...], "error": str|None}
    """
    from playwright.sync_api import sync_playwright

    saved = _load_cookies()
    iwxt_ck = saved.get("i.sjtu.edu.cn") or {}
    jaccount_ck = saved.get("jaccount.sjtu.edu.cn") or {}
    if not iwxt_ck:
        return {"error": "教学信息服务网未配置登录态，请先登录（setup_jwxt / 设置面板）。"}

    # 构造待查询的 (学年, xqm) 组合列表：学年从新到旧，学期按 秋/春/夏
    cur_year = int(_auto_year_term()[0])
    y_end = int(year) if year else cur_year
    y_start = y_end if year else max(cur_year - 6, 2015)  # 未指定学年时回溯 6 个学年
    if semester:
        xqms = [_XQM_MAP.get(str(semester), "")]
    else:
        xqms = ["3", "12", "16"]
    combos = [(str(y), x) for y in range(y_end, y_start - 1, -1) for x in xqms]

    try:
        with sync_playwright() as pw:
            # 复用系统 Chrome 的无头模式（channel="chrome"），无需 playwright install 下载内核
            browser = pw.chromium.launch(headless=True, channel="chrome")
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            # 注入已保存的 cookie：i.sjtu.edu.cn 域（业务 session）+ jaccount.sjtu.edu.cn 域
            # （SSO 会话，session 过期后可自动重新认证）
            ctx.add_cookies(
                [
                    {"name": k, "value": v, "domain": ".i.sjtu.edu.cn", "path": "/"}
                    for k, v in iwxt_ck.items()
                ]
                + [
                    {"name": k, "value": v, "domain": ".jaccount.sjtu.edu.cn", "path": "/"}
                    for k, v in jaccount_ck.items()
                ]
            )
            page = ctx.new_page()
            page.goto(
                "https://i.sjtu.edu.cn/jaccountlogin",
                wait_until="domcontentloaded", timeout=20000,
            )
            page.wait_for_timeout(2000)
            if "jaccount" in page.url:
                browser.close()
                return {"error": "jAccount 登录态已过期，请重新登录（setup_jwxt / 设置面板）。"}

            # 访问成绩查询页，获取隐藏字段（含用户身份信息）
            page.goto(_GRADE_PAGE_URL, wait_until="networkidle", timeout=15000)
            page.wait_for_timeout(500)
            form_data = page.evaluate(
                """() => {
                    const inputs = document.querySelectorAll('input[type=hidden]');
                    const data = {};
                    for (const i of inputs) { data[i.name] = i.value; }
                    return data;
                }"""
            )

            # 逐个 (学年, 学期) 调用 jqGrid 数据接口并合并（同一 SSO 会话内完成）。
            # 关键：分页参数名是 queryModel.showCount / queryModel.currentPage
            # （jqGrid 的 prmNames 映射，见 jquery.jqgrid.settings.js），
            # 而非常见的 page/rows——后者在本接口会被忽略，固定只返回前 10 条。
            all_items: list[dict] = []
            for yy, xx in combos:
                resp = ctx.request.post(
                    _GRADE_API_URL,
                    form={
                        **form_data,
                        "xnm": yy,
                        "xqm": xx,
                        "queryModel.showCount": "500",
                        "queryModel.currentPage": "1",
                        "queryModel.sortName": "xnm",
                        "queryModel.sortOrder": "desc",
                        "nd": str(int(time.time() * 1000)),
                        "zd_fzdm": "N305005-xs",
                    },
                    headers={
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": _GRADE_PAGE_URL,
                    },
                )
                try:
                    data = resp.json()
                    all_items.extend(data.get("items", []) or [])
                except ValueError:  # noqa: BLE001 单个学期接口异常跳过
                    continue
            # 把本次 SSO 后浏览器里的最新 cookie 写回文件，保持课表 requests 直连可用
            fresh = _collect_browser_cookies(ctx)
            browser.close()
            if fresh.get("i.sjtu.edu.cn"):
                _save_cookies(fresh.get("i.sjtu.edu.cn"), fresh.get("jaccount.sjtu.edu.cn"))
    except Exception as e:  # noqa: BLE001 浏览器/接口异常
        return {"error": f"成绩查询失败: {e!r}"}

    if not all_items:
        return {
            "error": None,
            "count": 0,
            "year_filter": year or "全部",
            "semester_filter": semester or "全部",
            "weighted_gpa": None,
            "total_credits": 0.0,
            "grades": [],
            "message": "未找到成绩数据，该学期可能还未录入。",
        }

    # 按 (学年, 学期, 课程) 去重（多次遍历可能重叠），并按 学年/学期 倒序排序
    seen: set[tuple] = set()
    unique_items: list[dict] = []
    for item in all_items:
        key = (item.get("xnm", ""), item.get("xqmmc", ""), item.get("kch", ""))
        if key in seen:
            continue
        seen.add(key)
        unique_items.append(item)
    unique_items.sort(key=lambda it: (str(it.get("xnm", "")), str(it.get("xqmmc", ""))),
                      reverse=True)

    grades = []
    total_credits = 0.0      # 有绩点课程的学分和（加权 GPA 的分母，P 成绩不计入）
    earned_credits = 0.0     # 全部课程的修读学分和（含 P 成绩课程）
    weighted_sum = 0.0
    for item in unique_items:
        xf_str = item.get("xf", "")
        jd_str = item.get("jd", "")
        try:
            xf = float(xf_str) if xf_str else 0.0
            jd = float(jd_str) if jd_str else None
        except ValueError:
            xf = 0.0
            jd = None
        grades.append({
            "year":        f"{item.get('xnm', '')}学年",
            "semester":    f"第{item.get('xqmmc', '')}学期",
            "course_id":   item.get("kch", ""),
            "course_name": item.get("kcmc", ""),
            "score":       item.get("cj", ""),
            "gpa":         jd_str,
            "credits":     xf_str,
            "type":        (item.get("kcbj", "") or "").strip(),
            "exam_type":   item.get("khfsmc", ""),
        })
        if xf > 0:
            earned_credits += xf
        if jd is not None and xf > 0:
            total_credits += xf
            weighted_sum += jd * xf

    avg_gpa = weighted_sum / total_credits if total_credits > 0 else None
    return {
        "error": None,
        "count": len(grades),
        "year_filter": year or "全部",
        "semester_filter": semester or "全部",
        "weighted_gpa": round(avg_gpa, 4) if avg_gpa is not None else None,
        "total_credits": round(total_credits, 1),   # GPA 计算学分（不含 P）
        "earned_credits": round(earned_credits, 1),  # 修读总学分（含 P）
        "grades": grades,
    }


def format_grades(data: dict) -> str:
    """
    把 query_grades 结果格式化为 LLM 友好的文本。

    :param data: query_grades 的返回
    :return: 格式化文本（含加权 GPA 与成绩明细）
    """
    if data.get("error"):
        return f"[错误] {data['error']}"
    grades = data.get("grades") or []
    if not grades:
        return "（未查询到成绩数据）"
    yf = data.get("year_filter") or "全部"
    sf = data.get("semester_filter") or "全部"
    if yf == "全部" and sf == "全部":
        filter_desc = "全部学年学期"
    elif yf == "全部":
        filter_desc = f"第{sf}学期（各学年）"
    elif sf == "全部":
        filter_desc = f"{yf}学年（全部学期）"
    else:
        filter_desc = f"{yf}学年·第{sf}学期"
    lines = [
        f"成绩查询结果（{filter_desc}，共 {len(grades)} 条）：",
        f"加权 GPA：{data.get('weighted_gpa')} ｜ "
        f"GPA计算学分：{data.get('total_credits')} ｜ "
        f"修读总学分：{data.get('earned_credits')}（含 P 成绩课程）",
        "",
    ]
    for g in grades:
        lines.append(
            f"- {g['course_name']}（{g['course_id']}） 成绩: {g['score']} 绩点: {g['gpa']} "
            f"学分: {g['credits']} [{g['type']}] {g['semester']}"
        )
    return "\n".join(lines)


# ==================== GPA 排名 ====================

# GPA 排名页与接口（学长项目未实现，本次逆向自页面 JS cxGpaxjfcxIndex.js）：
#   统计接口：POST /cjpmtj/gpapmtj_tjGpapmtj.html（先执行排名计算）
#   列表接口：POST /cjpmtj/gpapmtj_cxGpaxjfcxIndex.html?doType=query（返回自己的排名行）
# 学生视角下列表只返回当前登录用户一行，含 gpa/gpapm(绩点排名)/xjf/xjfpm(学积分排名)等。
_GPA_PAGE_URL = JWXT_BASE + "/cjpmtj/gpapmtj_cxGpaxjfcxIndex.html?gnmkdm=N309131&layout=default"
_GPA_STAT_URL = JWXT_BASE + "/cjpmtj/gpapmtj_tjGpapmtj.html"
_GPA_LIST_URL = JWXT_BASE + "/cjpmtj/gpapmtj_cxGpaxjfcxIndex.html?doType=query"

# 学年学期编码（下拉框 value）：YYYY + 03(秋)/12(春)/16(夏)，如 202503 = 2025-2026 第一学期
_XNXQ_MAP = {"1": "03", "2": "12", "3": "16"}


def _xnxq_code(year: str, semester: str) -> str:
    """
    构造接口用的学年学期编码（YYYY+03/12/16）。

    :param year: 学年起始年（如 "2025" 表示 2025-2026）
    :param semester: "1"=秋 / "2"=春 / "3"=夏
    :return: 编码，如 "202503"；参数不完整时返回 ""
    """
    if year and semester:
        return f"{year}{_XNXQ_MAP.get(str(semester), '03')}"
    return ""


def query_gpa_rank(year: str = "", semester: str = "", refresh: bool = False) -> dict:
    """
    查询个人 GPA 排名（绩点排名 + 学积分排名，统计范围为同年级同专业）。

    流程（接口逆向自 cxGpaxjfcxIndex.js）：
        1. SSO（注入 cookie 打开 jaccountlogin）建立 i.sjtu.edu.cn session；
        2. 打开 GPA 排名页（建立该模块的访问上下文）；
        3. POST tjGpapmtj.html 执行排名统计（必须先算后查）；
        4. POST cxGpaxjfcxIndex.html?doType=query 查询列表，取当前用户的排名行。

    学期范围三档（页面 qsXnxq/zzXnxq 为起止区间参数）：
        - 都不传：全部学期累计（默认，通常的"总排名"口径）；
        - 只传 year：该学年全部学期（如 year="2025" → 2025-2026 秋~夏）；
        - year + semester：仅该学期。

    :param year: 学年起始年（如 "2025" 表示 2025-2026）；不传=全部学期累计
    :param semester: "1"=秋 / "2"=春 / "3"=夏；不传时按 year 的范围规则处理
    :param refresh: 保留参数（兼容），无实际作用
    :return: {"error": str|None, "rank": {...}}；rank 含姓名/学号/GPA/绩点排名/学积分排名/班级等
    """
    from playwright.sync_api import sync_playwright

    saved = _load_cookies()
    iwxt_ck = saved.get("i.sjtu.edu.cn") or {}
    jaccount_ck = saved.get("jaccount.sjtu.edu.cn") or {}
    if not iwxt_ck:
        return {"error": "教学信息服务网未配置登录态，请先登录（setup_jwxt / 设置面板）。"}

    # 学期范围：year+semester → 单学期；只传 year → 该学年秋~夏；都不传 → 全部累计
    if year and semester:
        qs_xnxq = zz_xnxq = _xnxq_code(year, semester)
    elif year:
        qs_xnxq = f"{year}03"
        zz_xnxq = f"{year}16"
    else:
        qs_xnxq = zz_xnxq = ""

    try:
        with sync_playwright() as pw:
            # 复用系统 Chrome 的无头模式（channel="chrome"）
            browser = pw.chromium.launch(headless=True, channel="chrome")
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            ctx.add_cookies(
                [
                    {"name": k, "value": v, "domain": ".i.sjtu.edu.cn", "path": "/"}
                    for k, v in iwxt_ck.items()
                ]
                + [
                    {"name": k, "value": v, "domain": ".jaccount.sjtu.edu.cn", "path": "/"}
                    for k, v in jaccount_ck.items()
                ]
            )
            page = ctx.new_page()
            page.goto("https://i.sjtu.edu.cn/jaccountlogin",
                      wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)
            if "jaccount" in page.url:
                browser.close()
                return {"error": "jAccount 登录态已过期，请重新登录（setup_jwxt / 设置面板）。"}
            # 打开 GPA 排名页（建立该模块访问上下文）
            page.goto(_GPA_PAGE_URL, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(1500)

            headers = {"X-Requested-With": "XMLHttpRequest", "Referer": _GPA_PAGE_URL}
            # 统计范围固定为"年级专业"（页面默认，njzy）；必传，空值会导致查询失败
            maps = {
                "qsXnxq": qs_xnxq,
                "zzXnxq": zz_xnxq,
                "tjgx": "0",
                "alsfj": "",
                "sspjfblws": "2",
                "pjjdblws": "2",
                "bjpjf": "wbz",
                "bjjd": "wbz",
                "kch_ids": "",
                "bcjkc_id": "",
                "bcjkz_id": "",
                "cjkz_id": "",
                "cjxzm": "zhyccj",
                "kcfw": "qbkc",
                "tjfw": "njzy",
            }
            # 1) 执行排名统计
            r_stat = ctx.request.post(_GPA_STAT_URL, form=maps, headers=headers)
            if r_stat.status != 200:
                browser.close()
                return {"error": f"GPA 排名统计请求失败: HTTP {r_stat.status}"}
            # 2) 查询列表（jqGrid 数据接口 + queryModel 分页参数）
            r_list = ctx.request.post(
                _GPA_LIST_URL,
                form={
                    **maps,
                    "queryModel.showCount": "500",
                    "queryModel.currentPage": "1",
                    "queryModel.sortName": "gpapm",
                    "queryModel.sortOrder": "asc",
                    "nd": str(int(time.time() * 1000)),
                },
                headers=headers,
            )
            try:
                data = r_list.json()
            except ValueError:  # noqa: BLE001 非 JSON 响应
                browser.close()
                return {"error": "GPA 排名接口返回了非 JSON 数据，登录态可能已过期。"}
            items = data.get("items") or []
            # 学生视角接口只返回当前登录用户一行，直接取第一条
            row = items[0] if items else None
            # 把本次 SSO 后浏览器里的最新 cookie 写回文件，保持课表 requests 直连可用
            fresh = _collect_browser_cookies(ctx)
            browser.close()
            if fresh.get("i.sjtu.edu.cn"):
                _save_cookies(fresh.get("i.sjtu.edu.cn"), fresh.get("jaccount.sjtu.edu.cn"))
    except Exception as e:  # noqa: BLE001 浏览器/接口异常
        return {"error": f"GPA 排名查询失败: {e!r}"}

    if not row:
        return {"error": None, "rank": None,
                "message": "未查询到排名数据，可能是该学期还没有成绩或未参加统计。"}
    return {
        "error": None,
        "rank": {
            "name":        row.get("xm", ""),
            "student_id":  row.get("xh", ""),
            "gpa":         row.get("gpa", ""),
            "gpa_rank":    row.get("gpapm", ""),      # 形如 "3/60"：绩点排名/范围人数
            "xjf":         row.get("xjf", ""),        # 学积分
            "xjf_rank":    row.get("xjfpm", ""),      # 形如 "3/60"：学积分排名
            "total_score": row.get("zf", ""),         # 总分
            "course_count": row.get("ms", ""),        # 门数
            "fail_count":  row.get("bjgms", ""),      # 不及格门数
            "earned_credits": row.get("hdxf", ""),    # 获得学分
            "total_credits":  row.get("zxf", ""),     # 总学分
            "college":     row.get("jgmc", ""),
            "grade":       row.get("njmc", ""),
            "major":       row.get("zymc", ""),
            "class_name":  row.get("bj", ""),
        },
    }


def format_gpa_rank(data: dict) -> str:
    """
    把 query_gpa_rank 结果格式化为 LLM 友好的文本。

    :param data: query_gpa_rank 的返回
    :return: 格式化文本（含绩点排名与学积分排名）
    """
    if data.get("error"):
        return f"[错误] {data['error']}"
    r = data.get("rank")
    if not r:
        return data.get("message") or "（未查询到排名数据）"
    lines = [
        f"GPA 排名查询结果（统计范围：同年级同专业）：",
        f"姓名: {r.get('name')} ｜ 学号: {r.get('student_id')}",
        f"绩点 GPA: {r.get('gpa')} ｜ 绩点排名: {r.get('gpa_rank')}",
        f"学积分: {r.get('xjf')} ｜ 学积分排名: {r.get('xjf_rank')}",
        f"总学分: {r.get('total_credits')} ｜ 获得学分: {r.get('earned_credits')}"
        f" ｜ 门数: {r.get('course_count')} ｜ 不及格门数: {r.get('fail_count')}",
        f"年级: {r.get('grade')} ｜ 专业: {r.get('major')} ｜ 班级: {r.get('class_name')}"
        f" ｜ 学院: {r.get('college')}",
    ]
    return "\n".join(lines)


# ==================== 培养计划 ====================

# 培养计划接口（学长项目未实现，本次逆向自页面 JS pyjhxxcx10248.js）：
#   列表接口：POST /jxzxjhgl/pyjhxxcx_cxPyjhxxIndex.html?doType=query（按年级/学院/专业查培养计划列表）
#   内容接口：GET  /jxzxjhgl/pyjhxxcx_cxPyjhylIndex.html?jxzxjhxx_id=xxx&flag=yl
#             → 返回培养方案 PDF 字节流（Content-Type: application/pdf），需 PDF 解析提取文本。
_TRAIN_LIST_URL = JWXT_BASE + "/jxzxjhgl/pyjhxxcx_cxPyjhxxIndex.html?doType=query"
_TRAIN_PDF_URL = JWXT_BASE + "/jxzxjhgl/pyjhxxcx_cxPyjhylIndex.html"
_TRAIN_MAX_CHARS = 30000  # 培养方案 PDF 提取文本的字符上限


def _training_plan_context(handler, on_wait: Callable[[str], None] | None = None) -> dict:
    """
    SSO 并打开培养计划查询页，在同一个 Playwright 上下文中执行 handler(ctx)。

    供 query_training_plan / list_training_majors 共用，避免重复 SSO 代码：
        1. 注入 cookie 打开 jaccountlogin 完成 SSO；
        2. 打开培养计划查询页（建立模块访问上下文）；
        3. 执行 handler(ctx) 完成具体业务请求；
        4. 把浏览器里的最新 cookie 写回文件。

    :param handler: 接收 ctx 的函数，返回结果 dict
    :param on_wait: 可选日志回调
    :return: handler 的返回 dict；SSO/浏览器异常时返回 {"error": ...}
    """
    from playwright.sync_api import sync_playwright

    saved = _load_cookies()
    iwxt_ck = saved.get("i.sjtu.edu.cn") or {}
    jaccount_ck = saved.get("jaccount.sjtu.edu.cn") or {}
    if not iwxt_ck:
        return {"error": "教学信息服务网未配置登录态，请先登录（setup_jwxt / 设置面板）。"}
    try:
        with sync_playwright() as pw:
            # 复用系统 Chrome 的无头模式（channel="chrome"）
            browser = pw.chromium.launch(headless=True, channel="chrome")
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            ctx.add_cookies(
                [
                    {"name": k, "value": v, "domain": ".i.sjtu.edu.cn", "path": "/"}
                    for k, v in iwxt_ck.items()
                ]
                + [
                    {"name": k, "value": v, "domain": ".jaccount.sjtu.edu.cn", "path": "/"}
                    for k, v in jaccount_ck.items()
                ]
            )
            page = ctx.new_page()
            page.goto("https://i.sjtu.edu.cn/jaccountlogin",
                      wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)
            if "jaccount" in page.url:
                browser.close()
                return {"error": "jAccount 登录态已过期，请重新登录（setup_jwxt / 设置面板）。"}
            # 打开培养计划查询页（建立该模块访问上下文）
            page.goto(JWXT_BASE + "/jxzxjhgl/pyjhxxcx_cxPyjhxxIndex.html",
                      wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(1500)
            result = handler(ctx)
            # 把本次 SSO 后浏览器里的最新 cookie 写回文件，保持课表 requests 直连可用
            fresh = _collect_browser_cookies(ctx)
            browser.close()
            if fresh.get("i.sjtu.edu.cn"):
                _save_cookies(fresh.get("i.sjtu.edu.cn"), fresh.get("jaccount.sjtu.edu.cn"))
            return result
    except Exception as e:  # noqa: BLE001 浏览器/接口异常
        return {"error": f"培养计划查询失败: {e!r}"}


def _training_plan_headers() -> dict:
    """培养计划列表/PDF 接口的公共请求头。"""
    return {"X-Requested-With": "XMLHttpRequest",
            "Referer": JWXT_BASE + "/jxzxjhgl/pyjhxxcx_cxPyjhxxIndex.html"}


def _training_plan_row_name(row: dict) -> str:
    """拼接培养计划行的专业展示名（大类/一级/二级任一，空格分隔）。"""
    return " ".join(x for x in (row.get("dlmc"), row.get("yjzymc"),
                                row.get("ejzymc")) if x)


def _query_training_list(ctx, year: str) -> list[dict]:
    """
    调用培养计划列表接口，返回某年级的全部培养计划条目。

    :param ctx: Playwright APIRequestContext（SSO 后）
    :param year: 年级（入学年份）
    :return: items 列表；接口异常时抛 ValueError
    """
    r = ctx.request.post(
        _TRAIN_LIST_URL,
        form={
            "jg_id": "", "njdm_id": year, "dlbs": "", "zyh_id_dl": "",
            "zyh_id_yj": "", "zyh_id_ej": "", "xdlx": "",
            "_search": "false", "nd": str(int(time.time() * 1000)),
            "queryModel.showCount": "500", "queryModel.currentPage": "1",
            "queryModel.sortName": "jg_id,dlbs,dldm",
            "queryModel.sortOrder": "desc", "time": "0",
        },
        headers=_training_plan_headers(),
    )
    return r.json().get("items") or []


def _clean_cell(text: str) -> str:
    """
    清洗表格单元格文本：去掉中文/数字之间的空格，换行折叠为空格。

    培养方案 PDF 的单元格内文字被拆开绘制（如 '课程 代码'、'MARX1 205'），
    但英文单词间的空格必须保留（如 'Physical Education I'）。

    :param text: 原始单元格文本
    :return: 清洗后的文本
    """
    s = (text or "").strip()
    # 中文与中文、中文与数字、数字与数字之间的空格去掉（课程 代码→课程代码、MARX1 205→MARX1205）
    s = re.sub(r"(?<=[\u4e00-\u9fff0-9])\s+(?=[\u4e00-\u9fff0-9])", "", s)
    # 其余换行/多空格折叠为单个空格
    s = re.sub(r"\s+", " ", s)
    return s


def _extract_pdf_text(pdf_bytes: bytes, max_chars: int) -> tuple[str, int, bool]:
    """
    用 PyMuPDF 提取 PDF 文本（支持 UniGB 中文编码，PyPDF2 不支持会乱码）。

    培养方案主要是课程表格，纯文本流提取会把表头拆得支离破碎。本方法：
        - 表格区域用 find_tables() 按行列结构化提取（单元格清洗 + 跨页表头去重）；
        - 非表格文本（标题/学分要求/说明）用 blocks 模式提取，按坐标与表格混排。

    :param pdf_bytes: PDF 文件字节
    :param max_chars: 返回文本的字符上限
    :return: (提取文本, PDF 页数, 是否因超限截断)
    """
    try:
        import pymupdf  # noqa: F401  # 新版 PyMuPDF 推荐入口（fitz 已弃用）
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore  # 旧版兼容
        except ImportError:
            raise RuntimeError("未安装 PyMuPDF，无法解析培养方案 PDF。请运行: pip install pymupdf")
    import io
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    all_lines: list[str] = []
    total = 0
    truncated = False
    header_seen = False  # 跨页去重表头行

    def _inside_table(bbox: tuple, table_bboxes: list) -> bool:
        """判断文本块 bbox 是否与任一表格区域重叠（重叠则视为表格内容，跳过）。"""
        x0, y0, x1, y1 = bbox
        for tb in table_bboxes:
            if not (x1 < tb[0] or x0 > tb[2] or y1 < tb[1] or y0 > tb[3]):
                return True
        return False

    for page in doc:
        tabs = page.find_tables()
        table_bboxes = [t.bbox for t in tabs.tables]
        page_lines: list[tuple[float, str]] = []

        # 非表格文本块（标题/学分要求/说明文字）
        for b in page.get_text("blocks"):
            x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4]
            text = text.strip()
            if not text or _inside_table((x0, y0, x1, y1), table_bboxes):
                continue
            page_lines.append((y0, text))

        # 表格行（find_tables 结构化，按行列提取）
        for tab in tabs.tables:
            for row in tab.extract():
                cells = [_clean_cell(c) for c in row]
                joined = " | ".join(cells).strip(" | ")
                if not joined.strip():
                    continue
                # 表头行去重：整份文档只保留第一次出现的表头
                if "课程" in joined and "名称" in joined:
                    if header_seen:
                        continue
                    header_seen = True
                page_lines.append((tab.bbox[1], joined))

        # 页面内按 y 坐标排序（标题在上、表格在下）
        page_lines.sort(key=lambda item: item[0])
        for _, line in page_lines:
            all_lines.append(line)
            total += len(line)
            if total >= max_chars:
                truncated = True
                break
        if truncated:
            break
    return "\n".join(all_lines), len(doc), truncated


def query_training_plan(year: str = "", major: str = "", college: str = "",
                        on_wait: Callable[[str], None] | None = None) -> dict:
    """
    查询某专业的培养计划（培养方案 PDF，提取为纯文本返回）。

    流程（接口逆向自 pyjhxxcx10248.js，SSO/列表逻辑由 _training_plan_context 复用）：
        1. SSO + 打开培养计划查询页；
        2. 查列表接口，按年级 + 专业名模糊匹配（精确名/普通班优先）；
        3. 取匹配行的 jxzxjhxx_id，GET 内容接口拿培养方案 PDF 字节流；
        4. PyMuPDF 提取 PDF 文本（截断到 _TRAIN_MAX_CHARS）。

    :param year: 年级（入学年份，如 "2025"）；不传自动推断当前学年
    :param major: 专业名（模糊匹配，如 "工业工程"；匹配大类/一级/二级专业名）
    :param college: 学院名（可选，模糊匹配，如 "机械与动力工程学院"；用于缩小范围）
    :param on_wait: 可选日志回调
    :return: {"error": str|None, "title": str, "major": str, "college": str,
              "year": str, "text": str, "pages": int, "truncated": bool,
              "message": str|None}
    """
    if not major:
        return {"error": "请提供要查询的专业名称（如 '工业工程'）。"
                          "若不确定有哪些专业，可先用 list_training_majors 查看可选专业列表。"}
    year = year or _auto_year_term()[0]  # 默认当前学年（入学年级）

    def handler(ctx) -> dict:
        """在 SSO 上下文内：查列表 → 匹配 → 下载 PDF → 提取文本。"""
        try:
            items = _query_training_list(ctx, year)
        except ValueError:  # noqa: BLE001 非 JSON 响应
            return {"error": "培养计划列表接口返回了非 JSON 数据，登录态可能已过期。"}

        # 模糊匹配专业（优先匹配二级专业名，其次一级/大类；可按学院过滤）
        candidates = [it for it in items if major in (_training_plan_row_name(it) or "")]
        if college:
            candidates = [it for it in candidates if college in (it.get("jgmc") or "")]
        if not candidates:
            return {
                "error": None,
                "title": "", "major": major, "college": college or "", "year": year,
                "text": "", "pages": 0, "truncated": False,
                "message": (
                    f"在 {year} 级培养计划中未找到与『{major}』匹配的专业。"
                    f"可调用 list_training_majors 查看该年级全部可选专业，"
                    f"或按学院名缩小范围后重试。"
                ),
            }

        # 匹配排序：精确名优先 > 无括号后缀的普通班优先（如 IEEE/强基/荣誉等特殊班靠后）
        # > 专业层级更具体优先（二级 > 一级 > 大类）。
        # 例：major='计算机' 时，"计算机科学与技术"（普通班）排在
        # "计算机科学与技术(IEEE试点班)" 之前。
        def _match_score(row: dict) -> tuple:
            """计算匹配优先级得分，越小越优先。"""
            name = _training_plan_row_name(row)
            exact = 0 if name.strip() == major.strip() else 1  # 精确名最优先
            parens = len(re.findall(r"[()（）]", name))         # 特殊班带括号，扣分
            depth = 0 if row.get("ejzydm") else (1 if row.get("yjzydm") else 2)
            return (exact, parens, depth)

        candidates.sort(key=_match_score)

        # 取匹配最优行的 id（优先二级专业，其次一级、大类）
        row = candidates[0]
        pid = (row.get("ejzydm") or row.get("yjzydm") or row.get("dldm") or "")
        matched_name = _training_plan_row_name(row)
        matched_college = row.get("jgmc") or ""
        if not pid:
            return {"error": "匹配到的培养计划缺少 id，无法获取内容。"}

        # 获取培养方案 PDF 字节流
        r_pdf = ctx.request.get(
            _TRAIN_PDF_URL,
            params={"jxzxjhxx_id": pid, "flag": "yl"},
            headers=_training_plan_headers(),
        )
        pdf_bytes = r_pdf.body()
        if not pdf_bytes or pdf_bytes[:4] != b"%PDF":
            return {"error": "培养方案接口未返回 PDF 数据，请稍后重试。"}

        # 提取 PDF 文本
        try:
            text, pages, truncated = _extract_pdf_text(pdf_bytes, _TRAIN_MAX_CHARS)
        except RuntimeError as e:
            return {"error": str(e)}

        if not text.strip():
            return {"error": None, "title": "", "major": matched_name,
                    "college": matched_college, "year": year, "text": "", "pages": pages,
                    "truncated": False,
                    "message": f"已获取《{matched_name}》培养方案 PDF，但未能提取出文本（可能是扫描版）。"}
        return {
            "error": None,
            "title": text.strip().splitlines()[0] if text.strip() else "",
            "major": matched_name,
            "college": matched_college,
            "year": year,
            "text": text.strip(),
            "pages": pages,
            "truncated": truncated,
            "message": None,
        }

    return _training_plan_context(handler, on_wait=on_wait)


def list_training_majors(keyword: str = "", year: str = "",
                         on_wait: Callable[[str], None] | None = None) -> dict:
    """
    列出某年级培养计划中的可选专业（可按关键词/学院过滤），供 Agent 在查询前确认专业。

    解决"Agent 盲猜专业名"问题：用户说不清专业时，先调用本工具看到真实存在的
    专业列表（含学院），再拿准确的专业名去调 query_training_plan。

    :param keyword: 可选关键词，模糊匹配专业名或学院名（如 "计算机"）
    :param year: 年级（入学年份，如 "2025"）；不传自动推断当前学年
    :param on_wait: 可选日志回调
    :return: {"error": str|None, "year": str, "total": int, "matched": int,
              "majors": [{"name": str, "college": str}, ...]}
    """
    year = year or _auto_year_term()[0]

    def handler(ctx) -> dict:
        """在 SSO 上下文内：查列表 → 过滤 → 格式化。"""
        try:
            items = _query_training_list(ctx, year)
        except ValueError:  # noqa: BLE001 非 JSON 响应
            return {"error": "培养计划列表接口返回了非 JSON 数据，登录态可能已过期。"}
        if not items:
            return {"error": None, "year": year, "total": 0, "matched": 0,
                    "majors": [], "message": f"{year} 级暂无培养计划数据。"}

        kw = (keyword or "").strip()
        # 去重：同一专业可能同时出现在大类/一级/二级多个行，按 (专业名, 学院) 去重
        seen: set[tuple] = set()
        majors: list[dict] = []
        for it in items:
            name = _training_plan_row_name(it)
            college = it.get("jgmc") or ""
            if not name:
                continue
            if kw and kw not in name and kw not in college:
                continue
            key = (name, college)
            if key in seen:
                continue
            seen.add(key)
            majors.append({"name": name, "college": college})
        # 按学院分组排序，方便阅读
        majors.sort(key=lambda m: (m["college"], m["name"]))
        return {"error": None, "year": year, "total": len(items),
                "matched": len(majors), "majors": majors}

    return _training_plan_context(handler, on_wait=on_wait)


def format_list_training_majors(data: dict) -> str:
    """
    把 list_training_majors 结果格式化为 LLM 友好的文本。

    :param data: list_training_majors 的返回
    :return: 专业列表文本
    """
    if data.get("error"):
        return f"[错误] {data['error']}"
    majors = data.get("majors") or []
    if not majors:
        return f"（{data.get('year')} 级{'未找到匹配的专业' if data.get('keyword') else '暂无培养计划数据'}）"
    lines = [
        f"{data.get('year')} 级培养计划可选专业（共 {len(majors)} 个，"
        f"匹配 {data.get('matched', len(majors))} 个）："
    ]
    cur_college = None
    for m in majors:
        college = m["college"]
        if college != cur_college:
            cur_college = college
            lines.append(f"\n【{college}】")
        lines.append(f"- {m['name']}")
    return "\n".join(lines)


def format_training_plan(data: dict) -> str:
    """
    把 query_training_plan 结果格式化为 LLM 友好的文本。

    :param data: query_training_plan 的返回
    :return: 格式化文本（培养计划正文或提示）
    """
    if data.get("error"):
        return f"[错误] {data['error']}"
    if data.get("message"):
        return f"（{data['message']}）"
    text = data.get("text") or ""
    header = (
        f"培养计划《{data.get('title')}》（{data.get('year')}级 · {data.get('major')}"
        f" · {data.get('college')}，共 {data.get('pages')} 页）："
    )
    if data.get("truncated"):
        header += "\n[提示] 内容较长，已截断至前部分，如需完整内容请缩小查询范围或分段阅读。"
    return header + "\n\n" + text


if __name__ == "__main__":
    # 独立测试入口：python jwxt.py schedule / python jwxt.py grades
    #                / python jwxt.py gpa_rank / python jwxt.py plan [专业名]
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "schedule":
        print(format_schedule(fetch_schedule()))
    elif len(sys.argv) > 1 and sys.argv[1] == "grades":
        print(format_grades(query_grades()))
    elif len(sys.argv) > 1 and sys.argv[1] == "gpa_rank":
        print(format_gpa_rank(query_gpa_rank()))
    elif len(sys.argv) > 1 and sys.argv[1] == "plan":
        major = sys.argv[2] if len(sys.argv) > 2 else "工业工程"
        print(format_training_plan(query_training_plan(major=major)))
    else:
        ok = ensure_login()
        print("登录态就绪: ", ok)
