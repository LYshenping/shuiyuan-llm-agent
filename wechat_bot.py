#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wechat_bot.py — 水源 Agent 微信 Bot 扩展（独立脚本，不依赖主程序代码）

通过腾讯官方 iLink Bot API（微信 ClawBot 插件，2026 年官方开放）把
水源 Agent 接入微信：电脑开着本脚本，手机微信发消息即可远程使用 Agent。

工作原理：
  1. 首次运行：调用 ilink 接口获取登录二维码，用手机微信扫码绑定；
  2. 扫码后获得 bot_token，持久化到 data/wechat_bot.json；
  3. 长轮询 getupdates 接口接收消息；
  4. 把文本消息交给 ShuiyuanAgent 处理，把回复发回微信：
     - 普通回答：markdown 清洗为纯文本后分块发送（官方插件同款策略）；
     - 超长（>1200 字）或含表格/代码块的回答：用 playwright + marked 渲染成
       图片（PNG），通过 iLink 加密上传 CDN 后以图片消息发送（微信阅读体验更好）；
  5. 可主动调用 WeChatBot.push(text) 发送消息（如推送通知）。

与主程序的隔离性：
  - 本文件是独立扩展，不改动 web_app.py / cli.py / config.py / agent.py 的任何代码；
  - 仅复用 Config / LLMClient / ToolRegistry / ShuiyuanAgent / Session 等现有组件；
  - 微信登录态单独存于 data/wechat_bot.json，不影响 data/settings.json。

前置条件（微信端）：
  - 微信更新到最新版（iOS 8.0.69+ / 安卓 8.0.69+）；
  - 手机微信：我 → 设置 → 插件 → 启用「微信 ClawBot」插件（灰度推送，
    找不到入口请更新微信、重启后等待 1~2 天）；
  - 绑定成功后微信联系人里会出现「ClawBot」会话（仅本人可用，不支持群聊）。

用法：
  python wechat_bot.py             # 正常运行（长轮询）
  python wechat_bot.py --login     # 强制重新扫码登录
  python wechat_bot.py --push "消息内容"  # 主动推送一条消息后退出
  python wechat_bot.py --test      # 测试 token 连通性
"""

import argparse
import atexit
import base64
import hashlib
import json
import logging
import math
import os
import random
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
import datetime as _dt
from pathlib import Path
from urllib.parse import quote

import httpx
import qrcode  # pip install qrcode[pil]

# Windows 控制台可能默认 GBK，先切到 UTF-8 避免中文/emoji 打印报错
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---- 复用主程序组件（只读使用，不修改）----
from config import BASE_DIR, Config
from llm import LLMClient
from tools import ToolRegistry
from auth import ensure_login
from agent import Session, ShuiyuanAgent
from forum_client import LoginExpiredError

# 微信 bot 独立配置存储（不影响主程序的 data/settings.json）
WECHAT_CFG_PATH = BASE_DIR / "data" / "wechat_bot.json"

_logger = logging.getLogger("wechat_bot")


# ==================== ilink 协议常量 ====================

ILINK_BASE = "https://ilinkai.weixin.qq.com"
# CDN 域名（协议文档标注；实测失败时可尝试登录响应 baseurl + "/c2c"）
CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"

# 回答长度超过该值（清洗后字符数）或含表格/代码块时，自动渲染为图片发送
IMG_MAX_CHARS = 1200

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mKABCDEFGHJKST]")
_TMP_DIR = Path(tempfile.mkdtemp(prefix="shuiyuan_wechat_"))
atexit.register(shutil.rmtree, _TMP_DIR, ignore_errors=True)


# ==================== 配置读写 ====================

def load_bot_cfg() -> dict:
    """读取微信 bot 配置（data/wechat_bot.json），文件缺失/损坏返回空字典。"""
    try:
        if WECHAT_CFG_PATH.exists():
            raw = json.loads(WECHAT_CFG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
    except Exception:  # noqa: BLE001 配置损坏不影响启动
        pass
    return {}


def save_bot_cfg(**kwargs) -> None:
    """合并保存微信 bot 配置字段到 data/wechat_bot.json。"""
    cfg = load_bot_cfg()
    cfg.update(kwargs)
    WECHAT_CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    WECHAT_CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ==================== markdown → 纯文本 / 图片 ====================

def markdown_to_text(text: str) -> str:
    """把 Agent 的 markdown 回复清洗成微信友好的纯文本。

    逻辑对齐腾讯官方插件 markdownToPlainText：代码块保留内容、
    图片移除、链接只留显示文字、表格竖线转空格，再去除常见
    markdown 标记符号（标题/引用/加粗/斜体/删除线/分隔线）。
    """
    t = text or ""
    # 代码块：去掉围栏，保留代码内容
    t = re.sub(r"```[^\n]*\n?([\s\S]*?)```", lambda m: m.group(1).strip(), t)
    # 图片：整个移除
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
    # 链接：只保留显示文字
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    # 表格：去掉分隔行，去掉首尾竖线，内部竖线转空格
    t = re.sub(r"^\s*\|[\s:|-]+\|\s*$", "", t, flags=re.M)
    t = re.sub(r"^\s*\|(.+)\|\s*$",
               lambda m: "  ".join(c.strip() for c in m.group(1).split("|")), t, flags=re.M)
    # 行内代码 / 加粗 / 斜体 / 删除线
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)
    t = re.sub(r"\*([^*]+)\*", r"\1", t)
    t = re.sub(r"~~([^~]+)~~", r"\1", t)
    # 标题符号 / 引用符号 / 分隔线
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^>\s?", "", t, flags=re.M)
    t = re.sub(r"^\s*[-*_]{3,}\s*$", "", t, flags=re.M)
    # 无序列表符号 → 圆点（有序列表序号保留，可读性更好）
    t = re.sub(r"^\s*[-+*]\s+", "· ", t, flags=re.M)
    # 压缩多余空行
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _needs_image(text: str) -> bool:
    """判断回答是否需要渲染成图片发送：超长，或含代码块/表格。"""
    if len(markdown_to_text(text)) > IMG_MAX_CHARS:
        return True
    if "```" in text:
        return True
    if re.search(r"^\s*\|.*\|", text, flags=re.M):
        return True
    return False


# 渲染图片用的 HTML 模板：白底 + 通用 markdown 排版样式，内联 marked.min.js
_IMG_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<style>
  * { box-sizing: border-box; }
  body { margin: 0; padding: 0; background: #fff; }
  #content {
    width: 800px; padding: 24px 28px; margin: 0 auto;
    font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
    font-size: 15px; line-height: 1.75; color: #1f1f1f; word-break: break-word;
  }
  #content h1 { font-size: 22px; margin: 18px 0 10px; }
  #content h2 { font-size: 19px; margin: 16px 0 8px; padding-bottom: 6px; border-bottom: 1px solid #eee; }
  #content h3 { font-size: 16.5px; margin: 14px 0 6px; }
  #content p { margin: 8px 0; }
  #content a { color: #0b6bcb; text-decoration: none; }
  #content code { background: #f4f4f5; padding: 2px 5px; border-radius: 4px;
                  font-family: Consolas, "Courier New", monospace; font-size: 13.5px; }
  #content pre { background: #f6f6f7; padding: 12px 14px; border-radius: 8px;
                 overflow-x: auto; margin: 10px 0; }
  #content pre code { background: none; padding: 0; }
  #content blockquote { margin: 8px 0; padding: 2px 12px; border-left: 4px solid #d0d7de;
                        color: #57606a; background: #fafafa; }
  #content ul, #content ol { padding-left: 22px; margin: 8px 0; }
  #content li { margin: 3px 0; }
  #content table { border-collapse: collapse; margin: 10px 0; font-size: 14px; }
  #content th, #content td { border: 1px solid #d8dee4; padding: 6px 10px; text-align: left; }
  #content th { background: #f0f1f3; font-weight: 600; }
  #content hr { border: none; border-top: 1px solid #e3e3e6; margin: 16px 0; }
  #content img { max-width: 100%; }
</style>
</head>
<body>
<div id="content"></div>
<script>__MARKED_JS__</script>
<script>__PURIFY_JS__</script>
</body>
</html>"""


def _load_marked_js() -> str:
    """读取项目自带的 marked.min.js 源码（与 Web 端同一渲染引擎）。"""
    marked_path = BASE_DIR / "static" / "vendor" / "marked.min.js"
    return marked_path.read_text(encoding="utf-8")


def _load_purify_js() -> str:
    """读取项目自带的 DOMPurify 源码（渲染图片前对 markdown 输出做 HTML 清洗）。"""
    purify_path = BASE_DIR / "static" / "vendor" / "purify.min.js"
    return purify_path.read_text(encoding="utf-8")


# playwright 无头浏览器单例（懒加载，仅发图时启用）
_browser_singleton = None


def _get_browser():
    """获取 playwright 无头 Chromium 单例（首次调用时启动浏览器进程）。

    优先使用 headless shell；若未安装（常见于只装过完整版 chromium 的
    环境），回退用完整版 chromium 以无头方式启动，保证开箱即用。
    """
    global _browser_singleton
    if _browser_singleton is None:
        from playwright.sync_api import sync_playwright
        p = sync_playwright().start()
        try:
            browser = p.chromium.launch()
        except Exception:  # noqa: BLE001 headless shell 缺失时回退完整版
            browser = p.chromium.launch(
                headless=True,
                executable_path=p.chromium.executable_path,
            )
        _browser_singleton = (p, browser)
    return _browser_singleton


def _render_markdown_to_png(md: str, out_path: Path) -> Path:
    """用无头浏览器把 markdown 渲染成白底 PNG 图片。

    复用项目自带的 marked.min.js（与 Web 端同一渲染引擎），
    device_scale_factor=2 保证图片在高分屏上依然清晰。
    """
    p, browser = _get_browser()
    page = browser.new_page(viewport={"width": 840, "height": 1200},
                            device_scale_factor=2)
    try:
        html = (_IMG_HTML_TEMPLATE
                .replace("__MARKED_JS__", _load_marked_js())
                .replace("__PURIFY_JS__", _load_purify_js()))
        page.set_content(html, wait_until="load")
        # 与 Web 端 renderMarkdown 一致：marked 解析后先经 DOMPurify 清洗再渲染，
        # 防止 LLM 回答中的内联 HTML/事件属性在无头浏览器中执行
        page.evaluate(
            "(md) => { document.getElementById('content').innerHTML = "
            "DOMPurify.sanitize(marked.parse(md || '')); }",
            md,
        )
        page.wait_for_timeout(400)  # 等待字体与布局完成
        el = page.query_selector("#content")
        el.screenshot(path=str(out_path))
        return out_path
    finally:
        page.close()


# ==================== AES-128-ECB 加密（iLink CDN 要求） ====================

def _aes_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    """AES-128-ECB 加密并补 PKCS7 填充（与官方插件 node:crypto 默认行为一致）。"""
    from Crypto.Cipher import AES
    pad_len = 16 - (len(plaintext) % 16)
    data = plaintext + bytes([pad_len]) * pad_len
    return AES.new(key, AES.MODE_ECB).encrypt(data)


# ==================== ilink HTTP 客户端 ====================

class ILinkClient:
    """封装 iLink Bot 协议的 HTTP 请求层（扫码/长轮询/文本与图片发送）。"""

    def __init__(self, token: str):
        """初始化客户端。

        :param token: 扫码登录后获得的 bot_token
        """
        self.token = token
        self._cursor: str = ""  # getupdates 的游标

    def _headers(self) -> dict:
        """生成每次请求的随机 UIN 头（iLink 协议要求每次变化，防重放）。"""
        uin = base64.b64encode(str(random.randint(0, 0xFFFFFFFF)).encode()).decode()
        return {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {self.token}",
            "X-WECHAT-UIN": uin,
        }

    def _post(self, endpoint: str, body: dict) -> dict:
        """POST 到 ilink bot 接口，自动注入 base_info 与 Content-Length。

        sendmessage 成功时响应体可能是空 `{}`，此时按成功处理。
        """
        body["base_info"] = {"channel_version": "1.0.3"}
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = self._headers()
        headers["Content-Length"] = str(len(raw))
        resp = httpx.post(
            f"{ILINK_BASE}/ilink/bot/{endpoint}",
            content=raw,
            headers=headers,
            timeout=35,
        )
        text = resp.text.strip()
        if text and text != "{}":
            return resp.json()
        return {"ret": 0}

    def get_updates(self) -> list[dict]:
        """长轮询拉取新消息，自动更新游标，返回消息列表。"""
        result = self._post("getupdates", {"get_updates_buf": self._cursor})
        self._cursor = result.get("get_updates_buf", self._cursor)
        msgs = result.get("msgs", [])
        return msgs if isinstance(msgs, list) else []

    def send(self, text: str, to_user_id: str = "", context_token: str = "") -> dict:
        """发送文本消息（必须携带 context_token）。"""
        return self._post("sendmessage", {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": f"bot-{uuid.uuid4().hex[:12]}",
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [{"type": 1, "text_item": {"text": text}}],
            }
        })

    def send_image(self, image_path: Path, to_user_id: str = "",
                   context_token: str = "") -> None:
        """发送图片消息：AES 加密 → getuploadurl → CDN 上传 → sendmessage。

        流程与腾讯官方插件 upload.ts / send.ts 逐字段对齐：
          1. 生成随机 16 字节 aeskey 与 filekey；
          2. getuploadurl 获取预签名上传参数（带明文大小/MD5/密文大小/aeskey）；
          3. 图片密文 POST 到 CDN，响应头 x-encrypted-param 为下载参数；
          4. sendmessage 携带 CDN 引用（encrypt_query_param + aes_key + encrypt_type=1）。

        :param image_path: 本地 PNG 图片路径
        :param to_user_id: 接收者 ilink_user_id
        :param context_token: 会话上下文 token
        """
        plaintext = image_path.read_bytes()
        rawsize = len(plaintext)
        rawfilemd5 = hashlib.md5(plaintext).hexdigest()
        # AES-128-ECB + PKCS7 填充后的密文大小
        filesize = math.ceil((rawsize + 1) / 16) * 16
        filekey = os.urandom(16).hex()
        aeskey = os.urandom(16)

        # 1) 获取预签名上传参数
        resp = self._post("getuploadurl", {
            "filekey": filekey,
            "media_type": 1,          # UploadMediaType.IMAGE
            "to_user_id": to_user_id,
            "rawsize": rawsize,
            "rawfilemd5": rawfilemd5,
            "filesize": filesize,
            "no_need_thumb": True,
            "aeskey": aeskey.hex(),   # 明文 16 字节 → hex 字符串
        })
        upload_param = resp.get("upload_param")
        if not upload_param:
            raise RuntimeError(f"getuploadurl 未返回 upload_param：{resp}")

        # 2) 加密并上传到 CDN
        ciphertext = _aes_ecb_encrypt(plaintext, aeskey)
        upload_url = (f"{CDN_BASE}/upload?encrypted_query_param={quote(upload_param)}"
                      f"&filekey={quote(filekey)}")
        cdn_resp = httpx.post(
            upload_url,
            content=ciphertext,
            headers={"Content-Type": "application/octet-stream"},
            timeout=35,
        )
        if cdn_resp.status_code != 200:
            raise RuntimeError(f"CDN 上传失败 HTTP {cdn_resp.status_code}")
        download_param = cdn_resp.headers.get("x-encrypted-param", "")
        if not download_param:
            raise RuntimeError("CDN 响应缺少 x-encrypted-param 头")

        # 3) 发送图片消息（aes_key 按官方插件写法：hex 字符串做 utf8 编码后 base64）
        self._post("sendmessage", {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": f"bot-{uuid.uuid4().hex[:12]}",
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [{
                    "type": 2,
                    "image_item": {
                        "media": {
                            "encrypt_query_param": download_param,
                            "aes_key": base64.b64encode(aeskey.hex().encode("utf-8")).decode(),
                            "encrypt_type": 1,
                        },
                        "mid_size": filesize,
                    },
                }],
            }
        })

    def send_typing(self, context_token: str) -> None:
        """发送"正在输入"状态（失败静默，不影响主流程）。"""
        try:
            ticket = load_bot_cfg().get("wechat_typing_ticket", "")
            if not ticket:
                r = self._post("getconfig", {})
                ticket = r.get("typing_ticket", "")
            if ticket:
                save_bot_cfg(wechat_typing_ticket=ticket)
                self._post("sendtyping", {
                    "context_token": context_token,
                    "typing_ticket": ticket,
                })
        except Exception:  # noqa: BLE001 typing 失败不影响主流程
            pass


# ==================== 扫码登录 ====================

def do_login() -> tuple[str, str, str]:
    """执行扫码登录流程，返回 (bot_token, account_id, user_id)。

    在终端打印 ASCII 二维码，等待手机微信扫码确认。
    """
    print("\n正在获取微信登录二维码…")
    resp = httpx.get(f"{ILINK_BASE}/ilink/bot/get_bot_qrcode?bot_type=3", timeout=15)
    resp.raise_for_status()
    data = resp.json()
    qrcode_key = data["qrcode"]
    qrcode_url = data["qrcode_img_content"]

    # 在终端打印 ASCII 二维码
    qr = qrcode.QRCode(border=1)
    qr.add_data(qrcode_url)
    qr.make(fit=True)
    print("\n请用手机微信扫描以下二维码（微信 → 我 → 扫一扫）：\n")
    qr.print_ascii(invert=True)
    print(f"\n若终端二维码显示异常，可复制链接在浏览器打开后扫码：\n{qrcode_url}\n")

    print("等待扫码确认…")
    while True:
        try:
            status_resp = httpx.get(
                f"{ILINK_BASE}/ilink/bot/get_qrcode_status?qrcode={qrcode_key}",
                headers={"iLink-App-ClientVersion": "1"},
                timeout=40,
            )
            try:
                status = status_resp.json()
            except Exception:  # noqa: BLE001
                time.sleep(2)
                continue
        except httpx.ReadTimeout:
            continue  # 长轮询正常超时
        except Exception as e:
            _logger.warning(f"轮询扫码状态出错（继续重试）：{e}")
            time.sleep(2)
            continue

        s = status.get("status", "")
        if s == "scaned":
            print("✅ 已扫码，请在手机上确认绑定…")
        elif s == "confirmed":
            bot_token = status["bot_token"]
            account_id = status.get("ilink_bot_id", "")
            user_id = status.get("ilink_user_id", "")
            print("\n✅ 绑定成功！")
            return bot_token, account_id, user_id
        elif s == "expired":
            raise RuntimeError("二维码已过期，请重新运行")
        time.sleep(2)


# ==================== Agent 初始化（复用主程序组件） ====================

class _ThoughtLogger:
    """把 LLM 逐 token 的思考流聚合为完整段落再打印，避免终端刷屏。

    llm.py 的 on_thought 回调是逐增量调用的（"思考：xxx"建块、"思考+yyy"追加、
    "思考="收起），专为 Web 端流式展示设计；bot 终端不需要流式，这里把碎片
    累积起来，每满一段（约 200 字）打印一次，结束时打印剩余部分。
    """

    def __init__(self) -> None:
        self._buf: list[str] = []
        self._logger = logging.getLogger("wechat_bot")

    def __call__(self, msg: str) -> None:
        """聚合并输出一条 on_event 消息（思考流或阶段提示）。"""
        if msg.startswith("思考："):
            self._buf = [msg[len("思考："):]]
            self._emit_if_enough()
            return
        if msg.startswith("思考+"):
            self._buf.append(msg[len("思考+"):])
            self._emit_if_enough()
            return
        # 非思考流消息：先把已聚合的思考落盘，再处理本条
        self._flush()
        if msg.startswith("思考=") or not msg.strip():
            return  # 思考收起标记 / 空消息不打印
        self._logger.info(f"[agent] {msg}")

    def _emit_if_enough(self) -> None:
        """思考流累积超过阈值时打印一段。"""
        if sum(len(x) for x in self._buf) >= 200:
            self._flush()

    def _flush(self) -> None:
        """把已聚合的思考段落一次性打印并清空缓冲区。"""
        text = "".join(self._buf).strip()
        self._buf = []
        if text:
            self._logger.info(f"[agent] 思考：{text}")


class _AgentRuntime:
    """懒加载的单例运行时：保存 Agent 及按用户隔离的会话。

    属性：
        cfg: 全局配置
        agent: ShuiyuanAgent 实例
        sessions: {user_id: Session}，每个微信用户独立的对话历史
        lock: 串行化对话处理（微信消息可能连发）
    """

    _instance: "_AgentRuntime | None" = None
    _init_lock = threading.Lock()

    def __init__(self):
        self.cfg: Config | None = None
        self.agent: ShuiyuanAgent | None = None
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()
        self._thought_logger = _ThoughtLogger()

    @classmethod
    def get(cls) -> "_AgentRuntime":
        """获取单例；首次调用时完成 Agent 初始化（含论坛登录态校验）。"""
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = cls()
                    cls._instance._init_agent()
        return cls._instance

    def _init_agent(self) -> None:
        """初始化 Config / 论坛登录 / LLM / 工具 / Agent（与 cli.py 相同流程）。"""
        self.cfg = Config.load()
        self.cfg.check_llm_ready()

        # 确保论坛登录态可用（无登录态时弹出浏览器手动登录，与主程序行为一致）
        forum = ensure_login(
            self.cfg.state_file,
            request_delay=self.cfg.request_delay,
            on_wait=lambda msg: _logger.info(msg),
        )
        llm = LLMClient(self.cfg)
        tools = ToolRegistry(
            forum, self.cfg, llm=llm,
            on_event=self._thought_logger,
        )
        self.agent = ShuiyuanAgent(
            self.cfg, llm, tools,
            on_event=self._thought_logger,
        )

    def get_session(self, user_id: str) -> Session:
        """获取（必要时创建）某用户独立的会话，支持多轮追问。

        会话按用户长期驻留内存；为防止长时间运行无限增长，超过上限时
        淘汰最早创建的会话（字典保持插入顺序）。
        """
        sess = self.sessions.get(user_id)
        if sess is None:
            sess = Session(history_window=self.cfg.history_window)
            if len(self.sessions) >= 200:
                self.sessions.pop(next(iter(self.sessions)))
            self.sessions[user_id] = sess
        return sess

    def run_turn(self, user_id: str, question: str) -> str:
        """执行一轮对话：Agent 回答 + 归档会话，返回完整回答文本。"""
        sess = self.get_session(user_id)
        # 微信不要求流式，on_token 传 None 即可（最终一次性回复）
        answer = self.agent.ask(question, sess, on_token=None, memory=None)
        sess.add_exchange(question, answer)
        return answer


# ==================== 消息处理 ====================

def _extract_text(item_list: list[dict]) -> str:
    """从 iLink 消息 item_list 中提取文本内容（type=1 为文本项）。"""
    text = ""
    for item in item_list or []:
        try:
            itype = int(item.get("type"))
        except Exception:  # noqa: BLE001
            itype = 0
        if itype == 1 and not text:
            text = item.get("text_item", {}).get("text", "").strip()
    return text


def _send_chunks(client: ILinkClient, text: str, to_user: str, ctx_token: str,
                 max_len: int = 3000) -> None:
    """将长文本分段发送（微信单条消息有长度限制，实测约 4096，取 3000 留余量）。"""
    while text:
        chunk = text[:max_len]
        text = text[max_len:]
        client.send(chunk, to_user_id=to_user, context_token=ctx_token)
        if text:
            time.sleep(0.3)  # 避免发送过快


def _send_reply(client: ILinkClient, answer: str, to_user: str, ctx_token: str) -> None:
    """按内容决定回复方式：普通回答发纯文本，超长/结构化回答渲染成图片。"""
    if _needs_image(answer):
        try:
            png_path = _render_markdown_to_png(
                answer, _TMP_DIR / f"reply_{int(time.time() * 1000)}.png")
            client.send_image(png_path, to_user_id=to_user, context_token=ctx_token)
            _logger.info(f"已发送图片回复（{png_path.name}）")
            return
        except Exception as e:  # noqa: BLE001 图片链路失败则回退纯文本
            _logger.warning(f"图片回复失败，回退纯文本：{e}")
    plain = markdown_to_text(answer)
    _logger.info(f"[回复] 已发送 {len(plain)} 字文本回复")
    _send_chunks(client, plain, to_user, ctx_token)


def handle_message(client: ILinkClient, msg: dict) -> None:
    """处理一条收到的微信消息：调用 Agent 并把回复发回。

    仅处理文本消息；图片/语音/文件等媒体消息当前版本回复提示
    （本项目的解析工具链面向帖子/网页，尚未接入微信媒体）。
    """
    ctx_token = msg.get("context_token", "")
    from_user = msg.get("from_user_id", "")
    item_list = msg.get("item_list", [])

    text = _extract_text(item_list)
    has_media = any(
        (item.get("type") not in (None, 1)) for item in item_list or []
    )
    if not ctx_token or (not text and not has_media):
        return

    # 保存 context_token 和 to_user_id（供主动推送使用）
    save_bot_cfg(wechat_context_token=ctx_token, wechat_to_user_id=from_user)

    if has_media and not text:
        client.send("📎 当前版本暂不支持微信图片/语音/文件消息，请直接发送文字。",
                    to_user_id=from_user, context_token=ctx_token)
        return

    _logger.info(f"收到消息 from={from_user[:8]}…：{text[:50]}")
    client.send_typing(ctx_token)

    runtime = _AgentRuntime.get()
    # 串行处理，避免多条消息并发导致会话历史交错
    if runtime.lock.locked():
        client.send("⏳ 正在处理上一条消息，请稍候…",
                    to_user_id=from_user, context_token=ctx_token)
        return

    with runtime.lock:
        try:
            answer = runtime.run_turn(from_user, text)
            _send_reply(client, answer, from_user, ctx_token)
        except LoginExpiredError as e:
            # 论坛登录态过期：提示用户重启 bot 触发重新登录
            _logger.error(f"论坛登录态过期：{e}")
            client.send(f"⚠️ 论坛登录态已过期：{e}\n请在你的电脑上重启本 bot（python wechat_bot.py）以触发重新登录。",
                        to_user_id=from_user, context_token=ctx_token)
        except Exception as e:  # noqa: BLE001
            _logger.error(f"处理消息时出错：{e}", exc_info=True)
            try:
                client.send(f"❌ 出错了：{e}", to_user_id=from_user, context_token=ctx_token)
            except Exception:  # noqa: BLE001
                pass


def run_bot(client: ILinkClient) -> None:
    """主消息循环：长轮询接收消息并处理。"""
    _logger.info("✅ 微信 bot 已启动，等待消息…")
    cfg = load_bot_cfg()
    to_user = cfg.get("wechat_to_user_id", "")
    ctx_token = cfg.get("wechat_context_token", "")

    # 启动通知（已有历史会话时）
    if to_user and ctx_token:
        startup_time = _dt.datetime.now().strftime("%H:%M")
        try:
            client.send(
                f"✅ 水源 Agent 已上线（{startup_time}）\n直接发消息开始使用。",
                to_user_id=to_user,
                context_token=ctx_token,
            )
        except Exception:  # noqa: BLE001
            pass

    consecutive_errors = 0
    while True:
        try:
            msgs = client.get_updates()
            consecutive_errors = 0
            for msg in msgs:
                threading.Thread(
                    target=handle_message,
                    args=(client, msg),
                    daemon=True,
                ).start()
        except httpx.ReadTimeout:
            pass  # 长轮询正常超时
        except Exception as e:  # noqa: BLE001
            consecutive_errors += 1
            wait = min(5 * consecutive_errors, 60)
            _logger.warning(f"getupdates 出错（{consecutive_errors}次），{wait}s 后重试：{e}")
            time.sleep(wait)


# ==================== 主动推送封装 ====================

class WeChatBot:
    """简洁封装，供外部脚本主动推送消息。

    只要 data/wechat_bot.json 里有 wechat_bot_token + wechat_context_token +
    wechat_to_user_id 即可直接 push。
    """

    def __init__(self, token: str, to_user_id: str, context_token: str):
        """初始化推送客户端。

        :param token: 微信 bot_token
        :param to_user_id: 接收者的 ilink_user_id
        :param context_token: 会话上下文 token（首次用户发消息后自动保存）
        """
        self._client = ILinkClient(token)
        self._to_user_id = to_user_id
        self._context_token = context_token

    @classmethod
    def from_config(cls) -> "WeChatBot":
        """从 data/wechat_bot.json 读取配置构造客户端。"""
        cfg = load_bot_cfg()
        token = cfg.get("wechat_bot_token", "")
        to = cfg.get("wechat_to_user_id", "")
        ct = cfg.get("wechat_context_token", "")
        if not token:
            raise RuntimeError("未找到 wechat_bot_token，请先运行 wechat_bot.py 扫码登录")
        if not ct:
            raise RuntimeError("未找到 wechat_context_token，请先让微信给 bot 发一条消息")
        return cls(token, to, ct)

    def refresh_context_token(self) -> None:
        """先拉一次 getupdates 以刷新 context_token。"""
        msgs = self._client.get_updates()
        for msg in msgs:
            ct = msg.get("context_token", "")
            if ct:
                self._context_token = ct
                save_bot_cfg(wechat_context_token=ct)

    def push(self, text: str) -> None:
        """主动推送消息（先刷新 context_token 确保投递成功）。"""
        self.refresh_context_token()
        _send_chunks(self._client, text, self._to_user_id, self._context_token)

    def send(self, text: str) -> None:
        """直接发送（不刷新 context_token）。"""
        _send_chunks(self._client, text, self._to_user_id, self._context_token)


# ==================== 入口 ====================

def main() -> None:
    """命令行入口：解析参数并执行对应模式。"""
    parser = argparse.ArgumentParser(description="水源 Agent 微信 Bot（iLink 官方协议）")
    parser.add_argument("--login", action="store_true", help="强制重新扫码登录")
    parser.add_argument("--test", action="store_true", help="测试 token 连通性后退出")
    parser.add_argument("--push", metavar="MSG", help="主动推送一条消息后退出")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_bot_cfg()
    token = cfg.get("wechat_bot_token", "")

    # ---- 扫码登录 ----
    if args.login or not token:
        token, account_id, user_id = do_login()
        save_bot_cfg(
            wechat_bot_token=token,
            wechat_account_id=account_id,
            wechat_user_id=user_id,
        )
        print(f"token 已保存到 {WECHAT_CFG_PATH}")
        print("\n⚠️ 请用微信给「ClawBot」发一条任意消息（如「你好」），")
        print("   bot 才能拿到 context_token（首次必须）。之后运行 python wechat_bot.py 即可。\n")
        sys.exit(0)

    client = ILinkClient(token)

    # ---- 测试模式 ----
    if args.test:
        try:
            msgs = client.get_updates()
            print(f"✅ token 有效，长轮询返回 {len(msgs)} 条消息")
        except Exception as e:  # noqa: BLE001
            print(f"❌ token 无效或网络错误：{e}")
            sys.exit(1)
        sys.exit(0)

    # ---- 主动推送模式 ----
    if args.push:
        to = cfg.get("wechat_to_user_id", "")
        ct = cfg.get("wechat_context_token", "")
        if not to or not ct:
            print("❌ 未找到 wechat_to_user_id / wechat_context_token")
            print("   请先启动 bot 并让微信给 bot 发一条消息。")
            sys.exit(1)
        bot = WeChatBot(token, to, ct)
        bot.push(args.push)
        print("✅ 消息已发送")
        sys.exit(0)

    # ---- 正常运行：启动消息循环 ----
    if not cfg.get("wechat_context_token"):
        print("⚠️ 还没有 context_token。")
        print("   请在微信里找到「ClawBot」会话，给它发一条任意消息（如「你好」），")
        print("   bot 会自动记录 context_token 并开始服务。\n")

    run_bot(client)


if __name__ == "__main__":
    main()
