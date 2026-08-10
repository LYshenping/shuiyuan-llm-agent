"""
cli.py — shuiyuan-agent 命令行（CLI）入口。

用法：
    python cli.py

流程：
    1. 加载配置（环境变量 / .env）；
    2. 确保论坛登录态可用（无登录态时自动弹出浏览器手动登录）；
    3. 进入交互式问答：输入问题回车即可，支持连续追问；
       Agent 的思考过程按完整段落显示（非逐字流式），最终回答流式逐字输出；
       输入 exit / quit / 退出 结束。

与 Web 版（web_app.py）、微信版（wechat_bot.py）并行，共享同一套 agent 核心。
"""

from agent import Session, ShuiyuanAgent
from auth import ensure_login
from config import Config
from forum_client import LoginExpiredError
from llm import LLMClient
from tools import ToolRegistry


def _print_token(token: str) -> None:
    """流式回调：逐字打印 LLM 生成内容，不换行。"""
    print(token, end="", flush=True)


class _ThoughtLogger:
    """把 LLM 逐 token 的思考流聚合为完整段落再打印，避免终端刷屏。

    llm.py 的 on_thought 回调是逐增量调用的（"思考：xxx"建块、"思考+yyy"追加、
    "思考="收起）。CLI 不需要逐字流式展示思考过程，这里把碎片累积起来，
    每满一段（约 200 字）打印一次，结束时打印剩余部分；阶段提示正常逐条打印。
    """

    def __init__(self) -> None:
        self._buf: list[str] = []

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
        print(msg, flush=True)

    def _emit_if_enough(self) -> None:
        """思考流累积超过阈值时打印一段。"""
        if sum(len(x) for x in self._buf) >= 200:
            self._flush()

    def _flush(self) -> None:
        """把已聚合的思考段落一次性打印并清空缓冲区。"""
        text = "".join(self._buf).strip()
        self._buf = []
        if text:
            print(f"思考：{text}", flush=True)


def main() -> None:
    """主流程：初始化组件并进入交互问答循环。"""
    cfg = Config.load()

    # 1. 校验 LLM 配置
    try:
        cfg.check_llm_ready()
    except RuntimeError as e:
        print(e)
        return

    # 2. 确保论坛登录态（无则弹出浏览器手动登录）
    try:
        forum = ensure_login(cfg.state_file, request_delay=cfg.request_delay,
                             on_wait=lambda msg: print(msg, flush=True))
    except RuntimeError as e:
        print(f"[错误] {e}")
        return

    # 3. 组装 Agent（on_event 用于打印 Agent 的阶段提示与思考过程，共用同一聚合器）
    llm = LLMClient(cfg)
    thought_logger = _ThoughtLogger()
    tools = ToolRegistry(forum, cfg, llm=llm, on_event=thought_logger)
    agent = ShuiyuanAgent(cfg, llm, tools, on_event=thought_logger)
    session = Session(history_window=cfg.history_window)

    # 4. 交互问答循环
    print("\n" + "=" * 56)
    print("  水源社区 Agent（CLI 版）已就绪，请输入你的问题")
    print("  输入 exit / quit / 退出 结束")
    print("=" * 56)

    while True:
        try:
            question = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[再见]")
            break

        if not question:
            continue
        if question.lower() in ("exit", "quit", "q") or question in ("退出",):
            print("[再见]")
            break

        try:
            # track_usage 统计本次任务内全部 LLM 调用（含工具内部深度阅读）的 token 消耗
            with llm.track_usage() as usage:
                answer = agent.ask(question, session, on_token=_print_token)
            total = usage["prompt_tokens"] + usage["completion_tokens"]
            print(f"\n[Token 消耗] 输入 {usage['prompt_tokens']} · 输出 {usage['completion_tokens']} · 总计 {total}")
        except LoginExpiredError as e:
            print(f"\n[错误] {e}")
            print("[提示] 请重新运行程序以触发重新登录。")
            break
        except KeyboardInterrupt:
            print("\n[中断] 已停止当前回答，请输入新问题。")
            continue
        except Exception as e:  # noqa: BLE001
            print(f"\n[错误] 回答失败: {e!r}")
            continue

        session.add_exchange(question, answer)  # 归档本轮，支持追问


if __name__ == "__main__":
    main()
