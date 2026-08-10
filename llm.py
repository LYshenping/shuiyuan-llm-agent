"""
llm.py — LLM 客户端封装（DeepSeek，OpenAI 兼容接口）。

基于 openai SDK 访问 DeepSeek API，提供：
    1. 普通对话 chat()；
    2. 带工具定义（function calling）的对话；
    3. 流式输出（stream=True + on_token 回调，逐 token 打印）。

统一返回结构：{"content": str|None, "tool_calls": list[dict]|None}
    - tool_calls 形如 [{"id": "...", "function": {"name": "...", "arguments": "..."}}]
"""

import contextvars
from contextlib import contextmanager
from typing import Any, Callable

from openai import OpenAI

from config import Config

# 当前请求的 token 用量累加器（contextvar 按线程/上下文隔离；None 表示未启用跟踪）
_current_usage: contextvars.ContextVar[dict[str, int] | None] = contextvars.ContextVar(
    "current_usage", default=None
)


class TaskCancelled(Exception):
    """任务被用户取消（点击"停止生成"）时抛出的异常，用于中断 Agent 运行。"""


def _accumulate_usage(usage: Any) -> None:
    """
    把一次 LLM 调用的真实用量累加到当前请求累加器（未启用跟踪时忽略）。

    :param usage: OpenAI SDK 响应中的 usage 对象（含 prompt_tokens/completion_tokens）
    """
    acc = _current_usage.get()
    if acc is None or usage is None:
        return
    acc["prompt_tokens"] += int(getattr(usage, "prompt_tokens", 0) or 0)
    acc["completion_tokens"] += int(getattr(usage, "completion_tokens", 0) or 0)


def _usage_to_dict(usage: Any) -> dict[str, int] | None:
    """
    把 SDK 的 usage 对象转为字典，便于调用方透传/存储。

    :param usage: OpenAI SDK 响应中的 usage 对象
    :return: {"prompt_tokens": int, "completion_tokens": int}；无 usage 时返回 None
    """
    if usage is None:
        return None
    return {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
    }


class LLMClient:
    """封装 DeepSeek（OpenAI 兼容）API 的客户端。

    除主对话模型外，还支持独立的视觉模型（多模态，如阿里云百炼 qwen3.7-flash），
    用于把帖子/网页中的图片内容转为文字描述（主模型 deepseek-v4-flash 不支持图片）。
    """

    def __init__(self, cfg: Config):
        """
        初始化 LLM 客户端。

        :param cfg: 全局配置（含 base_url / api_key / model / timeout 及视觉模型配置）
        """
        cfg.check_llm_ready()
        self.model = cfg.llm_model
        self._client = OpenAI(
            base_url=cfg.llm_base_url,
            api_key=cfg.llm_api_key,
            timeout=cfg.llm_timeout,
        )
        # 视觉客户端：独立于主模型（视觉服务商通常与主模型不同）。
        # 仅当配置了视觉 API Key 时创建；未配置则图片理解自动降级为占位符。
        self.vision_enabled = cfg.vision_enabled
        self.vision_model = cfg.vision_model
        self.vision_description_chars = cfg.vision_description_chars
        if cfg.vision_api_key:
            self._vision_client = OpenAI(
                base_url=cfg.vision_base_url,
                api_key=cfg.vision_api_key,
                timeout=cfg.llm_timeout,
            )
        else:
            self._vision_client = None

    def describe_image(self, image_b64: str, mime: str,
                       hint: str = "", max_chars: int | None = None) -> str:
        """
        调用视觉模型理解单张图片，返回简洁的文字描述。

        图片以 base64 data URL 传入（OpenAI 兼容 image_url 格式，阿里云百炼
        qwen 视觉系列已实测支持）。Qwen3.7 系列默认开启思考模式，这里显式
        传 enable_thinking=false 关闭，加快响应并降低成本。

        :param image_b64: 图片内容的 base64 编码（不含 data: 前缀）
        :param mime: 图片 MIME 类型（如 image/png、image/jpeg）
        :param hint: 可选提示（如图片 alt 文本/所在楼层作者），辅助模型理解
        :param max_chars: 返回描述的字符上限（默认用配置 vision_description_chars）
        :return: 图片内容描述文本（超限截断）
        :raises RuntimeError: 视觉模型未配置（vision_api_key 为空）
        :raises Exception: 视觉 API 调用失败（由调用方捕获并降级）
        """
        if self._vision_client is None:
            raise RuntimeError("未配置视觉模型（vision_api_key 为空），图片理解不可用")
        max_chars = max_chars or self.vision_description_chars
        user_content: list[dict[str, Any]] = [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
            {
                "type": "text",
                "text": (
                    "请用简洁的中文描述这张图片的核心内容（如图中的文字、图表数据、"
                    "界面内容、物品场景等）。"
                    + (f" 图片上下文提示：{hint}。" if hint else "")
                    + f" 只输出描述本身，不要任何前缀或解释，控制在 {max_chars} 字以内。"
                ),
            },
        ]
        resp = self._vision_client.chat.completions.create(
            model=self.vision_model,
            messages=[{"role": "user", "content": user_content}],
            max_tokens=max(128, max_chars * 2),  # 中文约 1 token/字，留足余量
            extra_body={"enable_thinking": False},  # 关闭思考模式，加快响应降低成本
        )
        usage = getattr(resp, "usage", None)
        _accumulate_usage(usage)
        content = (resp.choices[0].message.content or "").strip()
        return content[:max_chars] if content else ""

    def chat(self, messages: list[dict[str, Any]],
             tools: list[dict[str, Any]] | None = None,
             tool_choice: str | dict[str, Any] = "auto",
             stream: bool = False,
             on_token: Callable[[str], None] | None = None,
             on_thought: Callable[[str], None] | None = None) -> dict[str, Any]:
        """
        发起一次对话补全请求。

        :param messages: OpenAI 消息列表
        :param tools: 工具定义列表（OpenAI tools schema 格式）
        :param tool_choice: 工具选择策略，默认 "auto"
        :param stream: 是否流式接收
        :param on_token: 流式模式下，最终回答内容片段回调（用于实时打印）
        :param on_thought: 流式模式下，工具轮思考文字回调（如 Web 端 status 灰色区）
        :return: {"content": 完整回复或 None, "tool_calls": 工具调用列表或 None,
                  "usage": {"prompt_tokens": int, "completion_tokens": int} 或 None}
        """
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        if stream:
            # 流式模式开启 usage 回传：DeepSeek 会在流末尾追加一个带 usage 的 chunk
            kwargs["stream_options"] = {"include_usage": True}
            return self._chat_stream(kwargs, on_token, on_thought)
        return self._chat_once(kwargs)

    @contextmanager
    def track_usage(self):
        """
        上下文管理器：统计该上下文内所有 LLM 调用的 token 消耗（按线程隔离）。

        用法：
            with llm.track_usage() as usage:
                answer = agent.ask(...)
            print(usage)  # {"prompt_tokens": x, "completion_tokens": y}

        :return: 每次调用的 usage 会累加进返回的字典
        """
        acc = {"prompt_tokens": 0, "completion_tokens": 0}
        token = _current_usage.set(acc)
        try:
            yield acc
        finally:
            _current_usage.reset(token)

    def _chat_once(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """
        非流式：一次请求拿到完整响应。

        :param kwargs: 请求参数
        :return: 统一响应结构
        """
        resp = self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        tool_calls = None
        if getattr(msg, "tool_calls", None):
            tool_calls = [
                {
                    "id": c.id,
                    "function": {"name": c.function.name, "arguments": c.function.arguments},
                }
                for c in msg.tool_calls
            ]
        usage = getattr(resp, "usage", None)
        _accumulate_usage(usage)
        return {
            "content": msg.content,
            "tool_calls": tool_calls,
            "reasoning_content": getattr(msg, "reasoning_content", None),
            "usage": _usage_to_dict(usage),
        }

    def _chat_stream(self, kwargs: dict[str, Any],
                     on_token: Callable[[str], None] | None = None,
                     on_thought: Callable[[str], None] | None = None,
                     buffer_threshold: int = 200) -> dict[str, Any]:
        """
        流式：逐 chunk 接收，按内容归属分流输出，并聚合完整结果。

        流式过程中无法预知本轮是"工具轮（思考）"还是"最终轮（回答）"，因此：
            - content 增量先进入缓冲区，不立即发送；
            - 一旦流中出现 tool_calls 增量 → 判定为工具轮，缓冲区内容作为
              思考文字回调 on_thought（Web 端进 status 灰色小字区）；
            - content 累积超过 buffer_threshold 仍未出现 tool_calls →
              判定为最终轮，先发出缓冲内容，之后每个增量实时回调 on_token
              （Web 端进 token 正式回答区，实现流式输出）。

        :param kwargs: 请求参数
        :param on_token: 最终回答内容片段回调（实时流式）
        :param on_thought: 工具轮思考文字回调
        :param buffer_threshold: 缓冲阈值（字符），超过即判定为最终轮开始流式
        :return: 统一响应结构
        """
        stream_obj = self._client.chat.completions.create(**kwargs, stream=True)

        content_parts: list[str] = []
        # 流式工具调用按 index 分块到达，需按 index 聚合
        tool_calls_acc: dict[int, dict[str, str]] = {}
        reasoning_parts: list[str] = []  # 模型思考链（reasoning_content）增量（用于回传）
        reasoning_flushed = False        # 思考链结束标记是否已发送
        reasoning_started = False        # 思考链是否已开始流式输出
        buffered = ""        # 尚未分发的 content 缓冲
        streaming = False    # 已判定为最终轮，正在实时流式输出
        thought_done = False  # 已把工具轮思考文字交给 on_thought
        usage = None         # 流末尾的 usage chunk（include_usage 开启后由 API 回传）

        def flush_reasoning() -> None:
            """思考链流式结束后发送收起标记（前端据此折叠思考块）。"""
            nonlocal reasoning_flushed
            if reasoning_flushed:
                return
            reasoning_flushed = True
            if on_thought and reasoning_started:
                on_thought("思考=")

        for chunk in stream_obj:
            # include_usage 时，流末尾的 chunk 携带本次请求的完整 usage（choices 为空）
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if not chunk.choices:
                continue  # 部分 chunk 仅含 usage/finish 信息，无 choices
            delta = chunk.choices[0].delta

            # 思考链增量：流式输出给 on_thought
            # （首块用"思考："建块，后续用"思考+"追加，结束由 flush_reasoning 发"思考="收起）
            # reasoning_parts 仍累积，用于工具轮回传 reasoning_content（API 要求）
            rc = getattr(delta, "reasoning_content", None)
            if rc:
                reasoning_parts.append(rc)
                if on_thought:
                    if not reasoning_started:
                        on_thought("思考：" + rc)
                        reasoning_started = True
                    else:
                        on_thought("思考+" + rc)

            if delta.content:
                # 思考链结束（开始输出正式内容），发送收起标记
                flush_reasoning()
                content_parts.append(delta.content)
                if thought_done:
                    # 工具轮思考文字之后的零星内容（罕见），仍作为思考输出
                    if on_thought:
                        on_thought(delta.content)
                elif streaming:
                    # 最终轮：实时流式
                    if on_token:
                        on_token(delta.content)
                else:
                    # 尚未判定：先缓冲
                    buffered += delta.content
                    if len(buffered) >= buffer_threshold:
                        # 内容已够长且未出现 tool_calls → 判定为最终轮，开始流式
                        if on_token:
                            on_token(buffered)
                        buffered = ""
                        streaming = True

            if getattr(delta, "tool_calls", None):
                flush_reasoning()  # 工具轮：先展示思考链，再处理工具调用
                # 首次出现 tool_calls → 判定为工具轮，缓冲内容转为思考文字
                if not thought_done and not streaming:
                    thought_done = True
                    if on_thought and buffered:
                        on_thought("思考：" + buffered)
                        on_thought("思考=")
                        buffered = ""
                for tc in delta.tool_calls:
                    acc = tool_calls_acc.setdefault(
                        tc.index, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.id:
                        acc["id"] = tc.id
                    if tc.function.name:
                        acc["name"] = tc.function.name
                    if tc.function.arguments:
                        acc["arguments"] += tc.function.arguments

        # 流结束：补发思考收起标记（若思考已开始但中途无 content/tool_calls）
        flush_reasoning()
        # 流结束：最终轮且内容未达阈值 → 一次性发出剩余缓冲
        if not tool_calls_acc and not streaming and buffered:
            if on_token:
                on_token(buffered)

        tool_calls = None
        if tool_calls_acc:
            tool_calls = [
                {
                    "id": acc["id"],
                    "function": {"name": acc["name"], "arguments": acc["arguments"]},
                }
                for acc in tool_calls_acc.values()
            ]
        _accumulate_usage(usage)
        return {
            "content": "".join(content_parts) or None,
            "tool_calls": tool_calls,
            "reasoning_content": "".join(reasoning_parts) or None,
            "usage": _usage_to_dict(usage),
        }
