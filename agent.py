"""
agent.py — Agent 核心：Function Calling 循环 + 多轮会话记忆 + 流式输出。

流程（单次提问）：
    messages = [system] + [会话历史] + [用户问题]
    循环（最多 max_agent_rounds 轮）：
        LLM(messages, tools) —— 流式调用，生成内容实时通过 on_token 打印
        ├─ 若返回 tool_calls → 执行工具（输出过程提示），结果回填 messages，继续循环
        └─ 若返回纯文本 → 即最终回答（已实时流式打印），结束
    每轮结束后将 (问题, 回答) 归档进 Session，供下一轮追问引用。
"""

import datetime
import json
from typing import Callable

from config import Config
from llm import LLMClient, TaskCancelled
from tools import ToolRegistry

SYSTEM_PROMPT = """\
你是"水源社区助手"，帮助用户在上海交通大学水源社区论坛（shuiyuan.sjtu.edu.cn）中查找信息、阅读帖子并总结回答。

工作方式：
1. 用户提问后，可以先用 search_forum 搜索相关帖子。关键词不够精准时可尝试多个不同组合（例如同时搜索"毕业 去向"、"就业"、"读研"等）。也可以添加上不同的tag参数，筛选出更符合需求的帖子。
2. 如果用户想按标签（tag）筛选内容——例如只看某个标签（如"转专业"）的帖子，或屏蔽某些标签（如"日记"）——可先调用 list_tags 了解论坛有哪些标签，再用 search_forum 的 tags 参数或 browse_latest 的 tags/exclude_tags 参数筛选。
3. 如果用户想按版块浏览内容（如"看看求职招聘版块有什么"），可先调用 list_categories 了解论坛有哪些版块，再用 browse_latest 的 category 参数筛选。
4. 如果用户想知道某个具体用户发过什么（如"看看某位学长/学姐发过什么""某人的保研/求职经验"），用 get_user_topics 查看该用户发起的主题或回复过的帖子；如果想了解用户的身份资历（注册时间、等级、发帖数、获赞数等），用 get_user_profile。
5. 根据搜索结果，用 read_topic 阅读最相关帖子的正文内容，必要时多读几个帖子交叉印证。若用户只想看某个人在某帖中的发言（如"这个帖子里 X 说了什么"），给 read_topic 传 username 参数，结果只会包含该用户的楼层。
6. 如果用户想了解论坛热度（"最近有什么热门话题""这周最火的帖子"），用 browse_top 按日/周/月/年查看热门帖；只想看某个帖子的浏览量/点赞数时，用 read_topic_likes 获取热度统计即可，无需读全文。
7. 如果检索有时间范围要求（"近半年关于转专业的帖子""2024 年的保研经验"），用 search_time_range 按主题创建时间过滤；想浏览某个标签下的内容（"看看转专业标签下有什么"）时，用 get_tag_topics。
8. 如果想找"还有没有类似的帖子/相关讨论"（如"跟这个求职帖类似的帖子""这事有没有其他说法"），用 find_similar_topics 查找相关帖子。
9. 综合多个帖子的信息给出结论，并在回答中注明信息来源帖子链接。
10. 如果用户给出了论坛之外的网页链接（如校园手册、官方文档、学长博客、通知公告页等），
    用 read_url 阅读该链接内容并总结。若链接是文档站首页，read_url 会返回全站章节索引，
    可根据索引用 read_url 继续阅读具体章节，再综合给出回答。
11. 如果用户想了解某个校园常用网站的内容（如"生存手册讲了什么""教务处有什么通知""图书馆怎么查馆藏"），
    或不确定该用哪个外部网站获取信息，可先调用 list_web_sites 查看预置网站库，
    找到对应网站后再用 read_url 阅读。

选课与课程评价（course.sjtu.plus 选课社区）：
- 用户问「XX 课怎么样」「XX 老师课好不好」「这学期选什么课」「XX 课难不难」「有什么水课推荐」「XX 课给分如何」
  → 先 search_courses(query=...) 搜索候选课程，再 get_course_detail(course_id=...) 看详情与真实评价。
- 选课社区有真实学生评价（评分、给分、学分、感受），比在论坛/网页上搜公开内容更准更细。
- 展示时：先用 search_courses 列出候选（课名+老师+评分+评价数），让用户挑选；选定后 get_course_detail 拉详情和前若干条评价。
- 严禁编造评价内容：用户问课程口碑必须先 get_course_detail 拿真实数据，不得凭空猜测。
- 若提示「选课社区未配置」/「凭证已过期」→ 调用 setup_course_community 重新登录（会打开浏览器，用户手动登录后自动保存登录态）。

课表与成绩（教学信息服务网 i.sjtu.edu.cn，需 jAccount 登录）：
- 用户问「今天/本周/下周有什么课」「这学期课表」→ 调用 get_schedule 查询个人课表（结构化数据，自动判断当前学期）。
- 用户问其他/历史学期的课表（如「大一上学期的课表」「2024-2025 课表」）→ 调用 get_schedule 并传对应的 year（学年起始年）和 term（1=秋季，2=春季）参数；该学期无课表（如入学前）时如实说明。
- 用户问下学期/未来学期的课表（如「下学期的课表」）→ 同样传对应 year/term 参数查询；若该学期尚未选课或排课，接口会返回空，如实说明即可。
- 用户问「我的成绩」「上学期成绩」「GPA 多少」「绩点多少」→ 调用 query_grades 查询成绩明细并自动计算加权 GPA（默认返回全部学期，也可传 year/semester 限定）。
- 用户问「我的 GPA 排名」「绩点排名」「在专业排第几」「学积分排名」→ 调用 query_gpa_rank 查询个人排名（统计范围为同年级同专业，返回绩点排名与学积分排名）。
- 用户问「培养计划」「培养方案」「专业要修什么课」「毕业要求多少学分」→ 调用 query_training_plan 查询某专业培养计划（传入专业名，如 '工业工程'；可加 year 指定年级、college 指定学院）。培养方案为 PDF 自动提取文本，包含各类课程要求学分与课程列表。
- 用户没说清专业名、或想先了解有哪些可选专业（如「2026级有哪些专业」「机械学院有什么专业」「计算机相关的专业」）→ 先调用 list_training_majors 查看真实存在的专业列表（可传 keyword/year 过滤），再拿准确专业名调用 query_training_plan。禁止凭空猜测专业名。
- 若提示「教学信息服务网未配置」/「登录态已过期」→ 调用 setup_jwxt 重新登录（优先自动登录，
  失败时打开浏览器引导手动登录，登录成功后自动保存登录态）。

外部网页检索策略（list_web_sites / read_url / search_web）：
- 需要查找外部网站上的特定内容、但不知道具体网址时，用 search_web 搜索（支持 site: 限定网站，
  如 site:www.cs.sjtu.edu.cn 转专业），再从结果里挑相关链接用 read_url 阅读。
- 阅读网站首页后，先提炼页面中的栏目导航链接（如"通知公告""本科生通知""学生事务"），
  再定向阅读对应栏目列表页，不要凭空猜测 URL。
- 若栏目列表页返回的文本中没有具体条目（列表由脚本动态加载，工具会给出提示），
  应改用 read_url 的 render=true 参数重新抓取该页（启用浏览器渲染）；不要在同一网站上反复猜测其他列表 URL 浪费轮次。
- 按时间找历史通知时不要逐页翻分页：通知按时间倒序排列，一般读 1~2 页即可；
  翻页超过 3 页仍未找到就停止，向用户如实说明。
- 页面正文为空或只有导航（疑似脚本渲染）时，同样优先用 render=true 重试，无效则如实告知用户。
- 想找 GitHub 上的开源项目/仓库时，直接用 search_github（走 GitHub 官方 API，返回仓库名/Star/链接），
  不要用 search_web 加 site:github.com（通用搜索引擎对 GitHub 收录差），也不要 read_url 抓 GitHub 的
  搜索页/话题页（JS 动态渲染，抓不到有效列表）。

效率要求：
- 通常 2~4 轮工具调用内即可完成信息收集并给出结论。
- 搜索到足够支撑结论的帖子后，立即用 read_topic 读取并总结，避免反复搜索相同内容。
- 不要无谓地扩大搜索范围；信息已足够时就停止调用工具，直接输出最终回答。

任务规模感知（容量自适应，模型上下文约 100 万 token）：
- 常规任务（单帖即可回答，100~500 层楼）：用 search_forum 找到帖子后，直接 read_topic 一次读完并总结（无需指定 max_posts，字符预算内会完整读完整楼），不要调用深层工具。
- 大任务（几千层的超长楼，或 read_topic 返回截断提示）：使用 deep_summarize_topic 做整楼总结；该工具会自行判断是直接读全文还是分块总结。
- 跨帖统计/穷举类大规模任务（如"统计 CS 专业同学的保研去向"）：进入系统性研究模式——
  1) 用多组不同关键词搜索，找出所有相关帖子；
  2) 对每个重要帖子逐一 read_topic（常规楼层）或 deep_summarize_topic（超长楼）；
  3) 最后跨帖综合汇总，给出整体结论，并注明依据了哪些帖子。
  切忌只搜一两组关键词、看一两个帖子就下结论。

必须遵守：
- 回答必须基于真实搜索结果和帖子内容，严禁编造帖子、链接或内容。
- 阅读外部链接时以 read_url 抓取到的正文为准，严禁编造链接指向的网页内容；并区分网页信息与论坛帖子的个人经验。
- search_web 的搜索结果来自公开搜索引擎，可能包含商业推广或未经核实的内容：
  优先采信学校官网、政府机构、权威媒体等可信来源；对个人博客、商业网站或来源不明的信息，要标注"来源待核实"，
  涉及缴费、转账、个人信息等敏感操作时明确提醒用户谨慎甄别，不要照搬推广话术。
- 要区分"帖子里的个人经验/说法"和"客观事实"，但为了保持回答的可读性，回答时不强制区分。
- 引用帖子时附上链接（https://shuiyuan.sjtu.edu.cn/t/<帖子id>）。
- 如果搜索不到相关或有用信息，明确告诉用户，不要硬凑答案。
- 全部使用中文回答。
- 建议在回答的末尾用分隔线隔开，简要写上你自己的观点或建议。
"""


class Session:
    """多轮会话记忆：保存最近若干轮的问答对。"""

    def __init__(self, history_window: int = 5):
        """
        初始化会话。

        :param history_window: 保留的最近完整轮次数
        """
        self.history_window = history_window
        self._history: list[dict] = []  # [{"role":"user"|"assistant", "content": ...}]

    def add_exchange(self, question: str, answer: str) -> None:
        """
        归档一轮问答。

        :param question: 用户问题
        :param answer: 助手回答
        """
        self._history.append({"role": "user", "content": question})
        self._history.append({"role": "assistant", "content": answer})

    def context_messages(self) -> list[dict]:
        """
        生成用于 LLM 的历史消息（最近 history_window 轮）。

        :return: OpenAI 消息格式的列表
        """
        n = self.history_window * 2  # 每轮占用 2 条消息
        return self._history[-n:] if n > 0 else []

    def load_history(self, items: list[dict]) -> None:
        """
        从持久化记录重建会话历史（Web 端重启后恢复对话记忆用）。

        :param items: 按时间顺序的 [{"role": "user"|"assistant", "content": ...}]
        """
        self._history = [
            {"role": it["role"], "content": it["content"]}
            for it in items
            if it.get("role") in ("user", "assistant")
        ]


class ShuiyuanAgent:
    """水源社区问答 Agent。"""

    def __init__(self, cfg: Config, llm: LLMClient, tools: ToolRegistry,
                 on_event: Callable[[str], None] | None = None,
                 is_cancelled: Callable[[], bool] | None = None):
        """
        初始化 Agent。

        :param cfg: 全局配置
        :param llm: LLM 客户端
        :param tools: 工具注册表
        :param on_event: 可选回调，Agent 执行过程中输出阶段提示（如"第N轮分析"）
        :param is_cancelled: 可选回调，返回是否收到用户取消信号；为 True 时尽早中断
        """
        self.cfg = cfg
        self.llm = llm
        self.tools = tools
        self._on_event = on_event or (lambda msg: print(msg, flush=True))
        self._is_cancelled = is_cancelled or (lambda: False)

    def _emit(self, msg: str) -> None:
        """输出阶段提示信息。"""
        self._on_event(msg)

    def _check_cancel(self) -> None:
        """若收到用户取消信号则抛出 TaskCancelled，中断 Agent 运行。"""
        if self._is_cancelled():
            raise TaskCancelled()

    @staticmethod
    def _format_args(args_json: str) -> str:
        """
        把工具参数 JSON 转为可读的紧凑形式（key=value）。

        :param args_json: LLM 返回的工具参数 JSON 字符串
        :return: 可读的参数文本，如 '(query="经验", tags=["转专业"])'
        """
        try:
            args = json.loads(args_json) if args_json else {}
        except json.JSONDecodeError:
            return f"({args_json})"
        if not args:
            return "()"
        pairs = [
            f'{k}="{v}"' if isinstance(v, str) else f"{k}={v}"
            for k, v in args.items()
        ]
        return "(" + ", ".join(pairs) + ")"

    def ask(self, question: str, session: Session,
            on_token: Callable[[str], None] | None = None,
            memory: str | None = None,
            disabled_tools: set[str] | None = None) -> str:
        """
        回答用户问题（Function Calling 循环，流式输出）。

        :param question: 用户问题
        :param session: 会话（携带历史）
        :param on_token: 流式回调，LLM 每生成一段内容即调用（用于逐字打印）
        :param memory: 可选：用户全局长期记忆文本，注入为附加 system 消息
        :param disabled_tools: 可选：本次禁用的工具名集合（如联网关闭时禁用
            search_web/read_url）。这些工具不会出现在传给 LLM 的 schema 中，
            即便模型仍尝试调用也会被拦截返回禁用提示，而非真正执行。
        :return: 最终回答文本
        """
        disabled_tools = disabled_tools or set()
        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        # 注入当前日期时间（本地时区），使 Agent 能回答"今天几号/星期几"及
        # 按时间判断信息时效性（如"最近/今天的新帖"）；随每次提问刷新
        now = datetime.datetime.now()
        weekday_names = ["一", "二", "三", "四", "五", "六", "日"]
        messages.append({
            "role": "system",
            "content": (
                "当前日期时间（本地时区 Asia/Shanghai）："
                f"{now.strftime('%Y-%m-%d %H:%M:%S')} 星期{weekday_names[now.weekday()]}"
            ),
        })
        if memory and memory.strip():
            # 长期记忆作为附加 system 消息注入；与当前对话矛盾时以对话为准
            messages.append({
                "role": "system",
                "content": (
                    "以下是你已了解的用户长期记忆（跨对话记住的个人信息与偏好，"
                    "供参考；若与当前对话内容矛盾，以当前对话为准）：\n"
                    f"{memory.strip()}"
                ),
            })
        messages.extend(session.context_messages())
        messages.append({"role": "user", "content": question})

        self._emit(f"提问：\"{question}\"")

        for round_no in range(1, self.cfg.max_agent_rounds + 1):
            self._check_cancel()  # 每轮开始检查用户取消信号

            # 流式调用：思考文字走 on_thought（Web 端 status 灰色小字区），
            # 最终回答走 on_token（Web 端 token 正式回答区，实时流式）。
            # LLMClient 内部通过缓冲阈值区分本轮归属（详见 llm._chat_stream）。
            # 过滤掉本次禁用的工具，使其不出现在传给 LLM 的 schema 中
            tools_schema = [
                t for t in self.tools.schema
                if t["function"]["name"] not in disabled_tools
            ]
            resp = self.llm.chat(
                messages, tools=tools_schema,
                stream=True,
                on_token=on_token,
                on_thought=self._emit,
            )
            content = resp["content"]
            tool_calls = resp["tool_calls"]

            # 模型要求调用工具：执行并回填结果，进入下一轮
            if tool_calls:
                # 工具调用声明已并入各工具的结果行（方案A），此处不再单独输出；
                # 思考文字已在流式期间通过 on_thought 输出。
                assistant_msg: dict = {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {
                                "name": c["function"]["name"],
                                "arguments": c["function"]["arguments"],
                            },
                        }
                        for c in tool_calls
                    ],
                }
                # 思考模式下，发生了工具调用的轮次必须回传 reasoning_content，
                # 否则 API 会返回 400（详见 DeepSeek 思考模式文档）。
                rc = resp.get("reasoning_content")
                if rc:
                    assistant_msg["reasoning_content"] = rc
                messages.append(assistant_msg)
                for call in tool_calls:
                    name = call["function"]["name"]
                    if name in disabled_tools:
                        # 防御性拦截：禁用工具即便被模型调用也不真正执行
                        result = f"[该工具已被禁用：{name}]"
                    else:
                        result = self.tools.run(name, call["function"]["arguments"])
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": result,
                    })
                self._check_cancel()  # 工具执行完成后、进入下一轮前再次检查取消
                continue

            # 无工具调用：content 即为最终回答（已在流式期间通过 on_token 实时输出）
            self._emit("完成")
            return (content or "").strip()

        self._emit("已达最大工具调用轮数")
        return "（已达到最大工具调用轮数仍未完成查询，请换一种问法再试。）"
