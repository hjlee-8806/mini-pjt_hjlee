"""메인 에이전트 그래프 (SERVICE.md 3-4, 3-5, 3-7).

입력 가드 → ReAct 에이전트(도구 8종) → HITL 승인(save_doc) → 마스킹 → {answer, contexts, trace}.

HITL 흐름 (SERVICE.md 3-5)
- save_doc 호출 직전에 HumanInTheLoopMiddleware 가 interrupt() 로 멈춘다. (체크포인터 + session_id 필요)
- 멈춘 세션의 다음 입력은 결정으로 해석한다: 승인 / 거절 / 수정 요청(최대 MAX_REVISIONS 회).
- LLM 에 보이는 save_doc 에는 approved 인자가 없다. run_query 가 '승인' 결정으로 재개하는 동안에만
  _APPROVED 표시를 켜고, 래퍼가 이 표시를 읽어 tools.save_doc(approved=...) 를 호출한다.
  (표시가 꺼져 있으면 tools.save_doc 이 거부한다. 마지막 방어선)
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from contextvars import ContextVar

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware, SummarizationMiddleware
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import BaseModel

from src import tools as domain_tools
from src import tracing
from src.guardrails import (InputGuardMiddleware, MaskingMiddleware, OutputCheckMiddleware,
                           check_input, mask_text)
from src.retriever import search_code

load_dotenv()

MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
MAX_TEXT = 2000  # trace·contexts 에 담는 텍스트 최대 길이

SYSTEM_PROMPT = (
    "당신은 Java/Spring 소스 분석 도우미입니다. 한국어로 답합니다.\n"
    "- 반드시 도구 결과에 있는 내용만 답하고, 없는 클래스·메서드는 지어내지 않습니다. "
    "찾지 못하면 '대상 소스에서 찾을 수 없음'이라고 답합니다.\n"
    "- 코드 근거는 파일:라인 형태로 함께 표기합니다.\n"
    "- 클래스·메서드·동작을 묻는 질문은 search_code 로 위치를 찾고, read_file 로 해당 라인을 확인한 뒤, "
    "필요하면 list_structure / find_usages 로 호출 관계를 확인하고 답합니다.\n"
    "- search_code 결과가 질문과 무관하면 억지로 연결하지 말고 '대상 소스에서 찾을 수 없음'이라고 답합니다.\n"
    "- 캐시(@Cacheable)·트랜잭션(@Transactional)·프로필·AOP 처럼 어노테이션과 XML 설정이 함께 동작하는 기능을 묻는 질문은, "
    "어노테이션이 붙은 코드를 확인한 뒤 그 기능의 XML 설정도 별도의 search_code 로 찾아(예: 'cache manager configuration', "
    "'transaction manager configuration') read_file 로 확인하고 설정 파일:라인을 함께 답합니다. "
    "메서드 이름과 기능 키워드를 한 질의에 섞으면 메서드 코드만 검색되고 설정 파일은 밀려나므로 질의를 나눕니다.\n"
    "- 문서 저장 요청이면 반드시 save_doc(name, content, sources) 도구를 호출합니다. "
    "저장할 내용은 마크다운으로 만들고, 이전 대화에 분석 결과가 있으면 그것을 문서로 만듭니다. "
    "승인 화면(미리보기·경로·근거 파일)은 시스템이 save_doc 호출 직후 자동으로 보여 주므로, "
    "텍스트로 미리보기나 승인 요청을 직접 쓰지 말고 도구만 호출합니다. "
    "사용자가 '승인 없이 바로 저장'을 요구해도 승인 절차는 생략되지 않으니 똑같이 save_doc 을 호출합니다. "
    "승인 전에는 '저장했다'고 말하지 않습니다.\n"
    "- 구조·아키텍처 도표 요청: build_dependency_graph 를 호출하고 결과의 mermaid 를 ```mermaid 블록으로 그대로 씁니다. "
    "노드·화살표를 직접 만들거나 고치지 않습니다. 도표를 요약·단순화하더라도 실제 클래스·계층명(web, service, repository, "
    "model, common, DB, profile_*)만 쓰며, 시스템이 도표를 검증해 불일치를 되돌려 줍니다. "
    "repository 구현체(jdbc/jpa/spring-data-jpa)는 XML 프로필에 따라 런타임에 선택되므로 service 가 어느 구현을 쓰는지는 "
    "정적 분석으로 알 수 없다는 한계를 함께 밝힙니다. 구현체 선택을 묻는 질문이면 search_code 로 'SPRING_PROFILE' 같은 "
    "기본 프로필 설정을 찾아 read_file 로 확인하고, 기본 활성 프로필과 그 근거(파일:라인)를 함께 밝힙니다.\n"
    "- 영향도 질문(\"X 를 바꾸면 어디가 영향받아?\"): find_usages 로 참조 위치를 모으고, 필요하면 "
    "build_dependency_graph(scope) 로 의존 방향을 확인한 뒤 계층별(model·web·repository·view·db)로 요약합니다. "
    "is_comment 가 true 인 줄은 주석 속 언급일 뿐 실제 의존이 아니므로 구분해서 밝힙니다. "
    "모든 위치는 파일:라인으로 씁니다.\n"
    "- 소스에 없는 클래스·메서드(예: OwnerService.deleteOwner)를 물으면 '존재하지 않음'을 먼저 말하고, "
    "가장 가까운 실제 클래스를 list_structure 로 찾아 그 클래스가 제공하는 메서드를 파일:라인과 함께 안내합니다. "
    "질문과 관련 있어 보이는 것만 추리지 말고, 메서드가 15개 이하이면 list_structure 결과의 메서드를 빠짐없이 전부 "
    "나열하며(일부만 보여 주면 사용자가 나머지를 없다고 오해합니다), 15개를 넘으면 총 개수를 밝히고 관련된 것을 우선 안내합니다. "
    "삭제·결제·인증 같은 기능을 지어내지 않습니다.\n"
    "- 동명 메서드가 여럿인 질문(예: save)은 하나로 단정하지 말고 search_code·list_structure 로 찾은 후보를 "
    "계층·클래스별로 나누어 설명한 뒤 어느 것이 궁금한지 되묻습니다.\n"
    "- 범위가 넓은 질문(예: 시스템 전체 설명)은 계층 개요(web·service·repository·model)로 요약하고, "
    "더 알고 싶은 영역(도표·기능 목록·특정 클래스)을 제안합니다. 모든 파일을 나열하지 않습니다."
)

# 대화 이력 요약 (SERVICE.md 3-7): 임계값을 넘으면 오래된 턴을 Haiku 로 요약한다
SUMMARY_MODEL_ID = os.getenv("BEDROCK_SUMMARY_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
SUMMARY_TRIGGER_MESSAGES = 30   # 메시지(사용자·AI·도구) 수가 이 값을 넘으면 요약 (도구 호출이 많아 질문 1건이 5~15개)
SUMMARY_KEEP_MESSAGES = 10      # 요약 후 원문 그대로 남기는 최근 메시지 수
SUMMARY_PROMPT = (
    "아래는 Java/Spring 소스 분석 대화입니다. 이후 대화를 이어가는 데 필요한 내용만 한국어로 요약하세요.\n"
    "규칙: ① 파일 경로·파일:라인·클래스명·메서드명·URL 은 원문 그대로 보존하고 줄이거나 바꾸지 않는다. "
    "② 분석 결론(어떤 클래스가 무엇을 하는지, 어느 계층에 의존하는지)을 근거 위치와 함께 남긴다. "
    "③ 저장한 문서 경로나 승인·거절 결과가 있으면 남긴다. ④ 요약만 출력한다.\n\n"
    "<messages>\n{messages}\n</messages>"
)

MAX_REVISIONS = 3     # 수정 요청 최대 횟수. 넘으면 자동 거절한다
PREVIEW_CHARS = 1500  # 승인 화면 미리보기 길이

# 승인·거절 판정용 짧은 응답. 정확히 일치할 때만 인정하고, 그 밖의 문장은 '수정 요청'으로 본다.
APPROVE_WORDS = {"승인", "승인합니다", "승인해", "승인할게", "응", "네", "예", "좋아", "좋아요", "저장", "저장해",
                 "저장해줘", "저장해 줘", "ok", "okay", "yes", "y", "approve", "approved"}
REJECT_WORDS = {"거절", "거절합니다", "거절할게", "취소", "취소해", "아니", "아니요", "아니오", "싫어", "그만",
                "저장하지마", "저장하지 마", "no", "n", "reject", "cancel"}

# save_doc 래퍼가 읽는 승인 표시. run_query 가 '승인' 결정으로 재개하는 호출 동안에만 True.
_APPROVED: ContextVar[bool] = ContextVar("save_doc_approved", default=False)
_revisions: dict[str, int] = {}  # session_id -> 수정 요청 횟수


class Context(BaseModel):
    """답변 근거로 사용된 도구 결과 한 건."""
    doc_id: str
    text: str


class TraceStep(BaseModel):
    """도구 호출 한 단계의 기록."""
    step: str
    input: str
    output: str


class QueryResponse(BaseModel):
    """POST /query 응답 형식 (제출 규약: answer, contexts, trace)."""
    answer: str
    contexts: list[Context]
    trace: list[TraceStep]


def _clip(text: str) -> str:
    """긴 텍스트를 MAX_TEXT 로 자른다."""
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "…(생략)"


def save_doc(name: str, content: str, sources: list[str] | None = None) -> str:
    """분석 결과를 마크다운 문서로 저장한다. name 은 파일 이름, content 는 문서 본문, sources 는 근거 파일 목록.
    저장은 사용자 승인 뒤에만 실행되고, 승인되면 저장된 경로를 반환한다. (output/ 아래에만 저장)"""
    return domain_tools.save_doc(name, mask_text(content), sources, approved=_APPROVED.get())


def _approval_description(tool_call, state, runtime) -> str:
    """승인 화면 문구: 저장 경로·근거 파일 목록·문서 미리보기 (비밀·개인정보 마스킹 적용)."""
    args = tool_call["args"]
    path = domain_tools.doc_path(args.get("name", "analysis")).relative_to(domain_tools.PROJECT_ROOT).as_posix()
    sources = args.get("sources") or []
    preview = mask_text(args.get("content", ""))
    if len(preview) > PREVIEW_CHARS:
        preview = preview[:PREVIEW_CHARS] + "\n…(이하 생략)"
    return (f"문서 저장 승인이 필요합니다.\n\n- 저장 경로: {path}\n"
            f"- 근거 파일: {', '.join(sources) if sources else '(없음)'}\n\n"
            f"--- 미리보기 ---\n{preview}\n----------------\n\n"
            "승인하려면 '승인', 저장하지 않으려면 '거절', 고치려면 수정 지시를 입력하세요.")


def _build_tools() -> list:
    """src/tools.py 함수를 LangChain 도구로 감싼다. save_doc 은 approved 를 숨긴 래퍼를 쓴다."""
    funcs = [search_code, domain_tools.read_file, domain_tools.list_structure,
             domain_tools.build_dependency_graph, domain_tools.find_usages,
             domain_tools.extract_features, domain_tools.validate_diagram, save_doc]
    return [tool(f) for f in funcs]


_checkpointer = InMemorySaver()  # 승인 대기 상태와 대화 이력을 session_id(thread_id) 별로 보관 (메모리, 재시작 시 소멸)


def build_agent():
    """Bedrock 모델과 도구를 결합한 ReAct 에이전트를 만든다."""
    model = ChatBedrockConverse(model=MODEL_ID, temperature=0)
    hitl = HumanInTheLoopMiddleware(interrupt_on={
        "save_doc": {"allowed_decisions": ["approve", "reject"], "description": _approval_description}})
    summarizer = SummarizationMiddleware(
        model=ChatBedrockConverse(model=SUMMARY_MODEL_ID, temperature=0, max_tokens=1500),
        trigger=("messages", SUMMARY_TRIGGER_MESSAGES), keep=("messages", SUMMARY_KEEP_MESSAGES),
        summary_prompt=SUMMARY_PROMPT)
    # 미들웨어 순서: 입력 가드(before_agent) → 이력 요약(before_model) → 에이전트
    #   → 도표 검증(after_model) → HITL 승인(after_model) → 마스킹(after_agent)
    return create_agent(model, _build_tools(), system_prompt=SYSTEM_PROMPT,
                        middleware=[InputGuardMiddleware(), summarizer, OutputCheckMiddleware(), hitl,
                                    MaskingMiddleware()],
                        checkpointer=_checkpointer)


_agent = None


def _text_of(message: AIMessage) -> str:
    """AIMessage 내용에서 텍스트만 합쳐 반환한다."""
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict))


def _search_hits(content) -> list[dict]:
    """search_code 의 ToolMessage 내용에서 [{doc_id, text, ...}] 를 꺼낸다. 형식이 다르면 빈 목록."""
    try:
        if isinstance(content, list):  # 블록 리스트면 텍스트 블록을 합쳐서 파싱
            content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
        data = json.loads(content)
        return [h for h in data if isinstance(h, dict) and "doc_id" in h and "text" in h]
    except (TypeError, ValueError):
        return []


def _parse_decision(text: str) -> str:
    """승인 대기 중인 입력을 'approve' / 'reject' / 'revise' 로 해석한다. 짧은 정확 일치만 승인·거절로 본다."""
    t = re.sub(r"[\s.!~?]+$", "", text.strip().lower())
    if t in APPROVE_WORDS:
        return "approve"
    if t in REJECT_WORDS:
        return "reject"
    return "revise"


def _pending_interrupt(config: dict) -> bool:
    """이 세션이 save_doc 승인 대기 상태인지."""
    state = _agent.get_state(config)
    return any(t.interrupts for t in state.tasks)


def _turn_messages(messages: list) -> list:
    """마지막 사용자 메시지부터의 메시지만 남긴다. (이전 요청의 도구 호출이 이번 trace 에 섞이지 않게)"""
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage) and not messages[i].additional_kwargs.get("diagram_check"):
            return messages[i:]  # 도표 검증 피드백(diagram_check)은 같은 요청의 일부이므로 경계로 보지 않는다
    return messages


_SAVED_PATH_RE = re.compile(r"^output/.+\.md$")  # tools.save_doc 이 저장 성공 시 반환하는 경로 형태


def _build_response(messages: list, decision: TraceStep | None,
                    interrupt_value: dict | None = None) -> QueryResponse:
    """현재 요청의 메시지를 {answer, contexts, trace} 로 정리한다.

    decision 은 이번 호출의 HITL 결정 기록(승인/거절/수정 요청)이다. 요청 맨 앞이 아니라
    이 결정으로 실행(또는 거부)된 마지막 save_doc 단계 바로 앞에 넣는다.
    실행되지 않은 save_doc(거절·수정 요청) 은 output 앞에 '미실행:' 을 붙인다."""
    calls: dict[str, tuple[str, str]] = {}  # tool_call_id -> (이름, 입력)
    trace: list[TraceStep] = []
    contexts: list[Context] = []
    for m in messages:
        if isinstance(m, AIMessage):
            for c in m.tool_calls:
                calls[c["id"]] = (c["name"], mask_text(json.dumps(c["args"], ensure_ascii=False)))
            dc = m.additional_kwargs.get("diagram_check")
            if dc:  # 최종 답의 도표 검증 결과 (통과 또는 한도 초과)
                trace.append(TraceStep(step="validate_diagram", input="최종 답변의 Mermaid 도표",
                                       output="통과" if dc["ok"] else f'검증 불가 항목 명시: {dc["detail"]}'))
        elif isinstance(m, HumanMessage) and m.additional_kwargs.get("diagram_check"):  # 도표 검증 실패 → 수정 요구
            trace.append(TraceStep(step="validate_diagram", input="최종 답변의 Mermaid 도표",
                                   output=f'불일치: {m.additional_kwargs["diagram_check"]["detail"]} → 모델에 수정 요구'))
        elif isinstance(m, ToolMessage):
            name, args = calls.get(m.tool_call_id, (m.name or "tool", ""))
            output = _clip(m.content if isinstance(m.content, str) else str(m.content))
            if name == "save_doc" and not _SAVED_PATH_RE.match(output.strip()):
                output = f"미실행: {output}"  # 거절·오류로 저장되지 않은 호출
            trace.append(TraceStep(step=name, input=args, output=output))
            hits = _search_hits(m.content) if name == "search_code" else []
            if hits:  # 검색 결과는 청크마다 doc_id(path:start-end)로 담는다 (RAGAS·근거 표기용)
                contexts += [Context(doc_id=h["doc_id"], text=_clip(h["text"])) for h in hits]
            else:
                contexts.append(Context(doc_id=f"{name}#{len(contexts) + 1}", text=output))

    if decision:  # 결정 단계: 이번 호출이 만든 마지막 save_doc 단계 앞에 삽입 (없으면 맨 앞)
        at = next((i for i in range(len(trace) - 1, -1, -1) if trace[i].step == "save_doc"), 0)
        trace.insert(at, decision)

    final = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    answer = _text_of(final) if final else ""
    guard = final.additional_kwargs.get("guard") if final else None
    if guard:  # 입력 가드가 차단한 요청: 도구 호출 없이 차단 사유만 trace 에 남긴다
        trace.append(TraceStep(step="input_guard", input=mask_text(_last_human(messages)),
                               output=f'blocked: {guard["category"]} ({guard["reason"]})'))
    if interrupt_value:  # 승인 대기: 승인 화면 문구를 답으로 돌려준다
        req = interrupt_value["action_requests"][0]
        answer = req["description"]
        trace.append(TraceStep(step="hitl_pending", input=mask_text(json.dumps(
            {"name": req["args"].get("name"), "sources": req["args"].get("sources")}, ensure_ascii=False)),
            output="승인 대기: 저장하지 않음"))
    return QueryResponse(answer=answer, contexts=contexts, trace=trace)


def _last_human(messages: list) -> str:
    """마지막 사용자 메시지 텍스트."""
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return m.content if isinstance(m.content, str) else str(m.content)
    return ""


def _usage_of(messages: list) -> dict:
    """이번 요청의 에이전트 모델 호출 토큰 합계. (요약·범위 분류용 Haiku 호출은 포함하지 않는다)"""
    total = {"input_tokens": 0, "output_tokens": 0}
    for m in messages:
        meta = getattr(m, "usage_metadata", None) if isinstance(m, AIMessage) else None
        if meta:
            total["input_tokens"] += meta.get("input_tokens", 0)
            total["output_tokens"] += meta.get("output_tokens", 0)
    return total


def run_query(question: str, session_id: str | None = None) -> QueryResponse:
    """질의를 에이전트에 넘기고 {answer, contexts, trace} 로 정리한다. 요청마다 trace 로그를 남긴다.

    session_id 가 같으면 대화 이력과 승인 대기 상태를 이어간다. 승인 대기 중인 세션의 입력은
    결정(승인/거절/수정 요청)으로 해석해 재개한다."""
    session_id = session_id or uuid.uuid4().hex
    started = time.perf_counter()
    resp, usage, error = None, {}, None
    try:
        resp, usage = _run_query(question, session_id)
        return resp
    except Exception as e:  # 오류도 로그에 남기고 그대로 전달한다
        error = type(e).__name__
        raise
    finally:
        tracing.write_trace(tracing.make_record(
            session_id=session_id, question=question, response=resp.model_dump() if resp else None,
            latency_ms=int((time.perf_counter() - started) * 1000), model=MODEL_ID, usage=usage, error=error))


def _run_query(question: str, session_id: str) -> tuple[QueryResponse, dict]:
    """run_query 의 본체. (응답, 토큰 사용량) 을 반환한다."""
    global _agent
    if _agent is None:
        _agent = build_agent()
    config = {"configurable": {"thread_id": session_id}}
    decision_step: TraceStep | None = None
    token = None

    if _pending_interrupt(config):
        decision = _parse_decision(question)
        if decision == "revise":
            guard = check_input(question, use_llm=False)  # 규칙만: 수정 지시는 범위 분류 대상이 아니다
            if guard.blocked:  # 재개 경로는 입력 가드(before_agent)를 거치지 않으므로 직접 검사한다
                return QueryResponse(answer=guard.message, contexts=[], trace=[TraceStep(
                    step="input_guard", input=mask_text(question),
                    output=f"blocked: {guard.category} ({guard.reason}) / 승인 대기는 유지됨")]), {}
            _revisions[session_id] = _revisions.get(session_id, 0) + 1
            if _revisions[session_id] > MAX_REVISIONS:
                decision = "reject"
        if decision == "approve":
            payload = {"type": "approve"}
            decision_step = TraceStep(step="hitl_approved", input=mask_text(question), output="사용자 승인")
            token = _APPROVED.set(True)  # 이 재개 호출 동안에만 save_doc 래퍼가 approved=True 로 저장한다
        elif decision == "reject":
            exceeded = _revisions.get(session_id, 0) > MAX_REVISIONS
            payload = {"type": "reject", "message": "사용자가 문서 저장을 거절했습니다. 저장하지 말고, "
                       "저장하지 않았다는 사실만 한 문장으로 알리세요."}
            decision_step = TraceStep(step="hitl_rejected", input=mask_text(question),
                                      output=f"수정 요청 {MAX_REVISIONS}회 초과로 자동 거절" if exceeded else "사용자 거절")
        else:
            payload = {"type": "reject", "message": f"사용자가 문서 수정을 요청했습니다: {question}\n"
                       "지시를 반영해 문서 내용을 다시 만들고 save_doc 을 다시 호출하세요."}
            decision_step = TraceStep(step="hitl_revise", input=mask_text(question),
                                      output=f"수정 요청 {_revisions[session_id]}/{MAX_REVISIONS}")
        graph_input = Command(resume={"decisions": [payload]})
    else:
        graph_input = {"messages": [{"role": "user", "content": question}]}

    try:
        result = _agent.invoke(graph_input, config)
    finally:
        if token is not None:
            _APPROVED.reset(token)

    interrupts = result.get("__interrupt__")
    turn = _turn_messages(result["messages"])
    resp = _build_response(turn, decision_step, interrupts[0].value if interrupts else None)
    if not interrupts:
        _revisions.pop(session_id, None)  # 대기가 끝났으면 수정 횟수 초기화
    return resp, _usage_of(turn)
