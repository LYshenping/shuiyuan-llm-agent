"""
jaccount.py — 交大 jAccount 统一身份认证（SSO）自动登录公共模块。

供水源社区（auth.py）、教学信息服务网（jwxt.py）、选课社区（course_community.py）
等各站点复用：只要配好了 .env 中的 JACCOUNT_USERNAME / JACCOUNT_PASSWORD，
就能在无头浏览器中自动填表 + 图形验证码自动识别完成 jAccount 登录，
再由各站点自己的 SSO 跳转流程获取对应域的登录态 cookie。

验证码识别三级方案（学长 login.py 同源）：
    1. 思源极客协会 ResNet 在线 API（https://geek.sjtu.edu.cn/captcha-solver/）；
    2. （可选）Claude 视觉 API —— 本项目未内置，需要时自行扩展；
    3. 手动兜底 —— 无头模式下无法交互，检测到识别失败即抛 ManualLoginRequiredError，
       由调用方降级为有头浏览器手动登录。

自动填表逻辑（_fill_jaccount_auto）实测自学长 login.py 的 _fill_jaccount，
选择器与流程均已在其项目验证；本项目教学网自动登录也已实际跑通。
"""

import os
from pathlib import Path
from typing import Callable

import requests

from dotenv import load_dotenv

# 加载项目根目录 .env（本模块是各站自动登录的公共入口，
# 独立于 config.py 的加载时机，必须自己加载才能读到 JACCOUNT_* 凭据）
load_dotenv(Path(__file__).resolve().parent / ".env")

# 思源极客协会的 ResNet 在线验证码识别 API（针对 jAccount 验证码训练）
_GEEK_API = "https://geek.sjtu.edu.cn/captcha-solver/"


class ManualLoginRequiredError(Exception):
    """jAccount 需要人工介入（图形验证码自动识别失败、或触发短信/二次验证码）时抛出。"""


def _jaccount_creds() -> tuple[str, str]:
    """
    读取 .env / 环境变量中的 jAccount 账号密码。

    :return: (用户名, 密码)；未配置时返回 ("", "")
    """
    u = os.environ.get("JACCOUNT_USERNAME", "").strip()
    p = os.environ.get("JACCOUNT_PASSWORD", "").strip()
    return u, p


def _png_to_jpeg(png_bytes: bytes) -> bytes:
    """
    把 PNG 验证码转为 110×40 JPEG（极客 API 要求尺寸）。

    Pillow 未安装时原样返回 PNG。

    :param png_bytes: PNG 图片字节
    :return: JPEG 字节
    """
    try:
        from PIL import Image  # type: ignore
        import io
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB").resize((110, 40))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()
    except ImportError:
        return png_bytes


def _solve_captcha_geek(img_bytes: bytes) -> str | None:
    """
    用思源极客协会 ResNet 在线 API 识别 jAccount 图形验证码。

    :param img_bytes: 验证码图片 PNG 字节
    :return: 识别出的验证码文本；失败返回 None
    """
    try:
        jpeg = _png_to_jpeg(img_bytes)
        r = requests.post(
            _GEEK_API,
            files={"image": ("captcha.jpg", jpeg, "image/jpeg")},
            timeout=10,
        )
        r.raise_for_status()
        result = r.json().get("result", "").strip()
        return result if result else None
    except Exception:  # noqa: BLE001 识别服务不可用则交给调用方降级
        return None


def _fill_jaccount_auto(page, username: str, password: str) -> None:
    """
    在已跳转到 jAccount 登录页的 page 上自动填表登录（无头模式用）。

    仅处理"图形验证码可自动识别 + 无需短信/二次验证"的路径：
        - 切到密码登录模式，填账号密码；
        - 图形验证码用极客 API 自动识别（最多 3 次尝试）；
        - 若检测到短信/二次验证码输入框 → 抛 ManualLoginRequiredError。

    :param page: Playwright Page（当前位于 jAccount 登录页）
    :param username: jAccount 用户名
    :param password: jAccount 密码
    :raises ManualLoginRequiredError: 需要人工介入时抛出
    """
    # 等待密码登录表单渲染完成：页面 JS 未加载完时元素不存在/不可交互，
    # 立即 fill/click 会等待超时（实测 goto 后需等待表单就绪）
    page.wait_for_selector("#input-login-user", state="visible", timeout=15000)
    page.wait_for_selector("#submit-password-button", state="visible", timeout=15000)

    # 切换到密码登录模式（默认可能是短信登录）
    page.evaluate("if (typeof switchLoginType === 'function') switchLoginType('password')")
    page.wait_for_timeout(400)

    page.fill("#input-login-user", username)
    page.fill("#input-login-pass", password)

    for attempt in range(3):
        cap = page.locator("#captcha-img")
        if cap.count() and cap.is_visible():
            code = _solve_captcha_geek(cap.screenshot())
            if not code:
                raise ManualLoginRequiredError(
                    "图形验证码自动识别失败，无法在无头模式下完成登录。"
                    "请改用「手动登录」模式（会打开浏览器窗口），或稍后重试。"
                )
            page.fill("#input-login-captcha", code)

        try:
            # no_wait_after：登录成功会立即触发页面跳转，点击不必等待导航完成，
            # 否则会因"等待导航"而超时（元素随后被替换）
            page.click("#submit-password-button", timeout=10000, no_wait_after=True)
        except Exception:  # noqa: BLE001 点击超时/元素被替换：若已离开 jaccount 视为登录成功
            if "jaccount.sjtu.edu.cn" not in page.url:
                return

        # 等待：成功（URL 离开 jaccount）、或出现短信/二次验证输入框、或错误提示
        try:
            page.wait_for_function(
                "() => !location.href.includes('jaccount.sjtu.edu.cn') || "
                "!!document.querySelector('.alert-danger, [class*=errorMsg], "
                "#input-login-sms-code, #input-bind-sms-code, "
                "[id*=sms-code], [name*=sms], [id*=twoFactor], "
                "#mfa-input, [id*=mfa], [id*=otp], [placeholder*=验证码][id*=sms]')",
                timeout=12_000,
            )
        except Exception:  # noqa: BLE001 超时继续走失败分支
            pass

        if "jaccount.sjtu.edu.cn" not in page.url:
            return  # 登录成功

        # 检测短信/二次验证码输入框 → 需要人工介入
        _sms_selectors = [
            "#input-login-sms-code", "#input-bind-sms-code", "[id*=sms-code]",
            "[name=smsCode]", "[name*=sms][type=text]", "[id*=twoFactor]",
            "#mfa-input", "[id*=mfa][type=text]", "[id*=otp][type=text]",
        ]
        for sel in _sms_selectors:
            try:
                loc = page.locator(sel)
                if loc.count() and loc.is_visible(timeout=500):
                    raise ManualLoginRequiredError(
                        "jAccount 需要短信/二次验证码，无法在无头模式下自动完成。"
                        "请改用「手动登录」模式（会打开浏览器窗口）完成登录。"
                    )
            except ManualLoginRequiredError:
                raise
            except Exception:  # noqa: BLE001
                continue

        # 普通登录失败（验证码错误等）：刷新验证码重试
        page.evaluate("if (typeof refreshCaptcha === 'function') refreshCaptcha()")
        page.wait_for_timeout(700)

    raise ManualLoginRequiredError(
        "多次尝试后仍无法自动登录 jAccount（验证码识别持续失败）。"
        "请改用「手动登录」模式（会打开浏览器窗口）完成登录。"
    )
