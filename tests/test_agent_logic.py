"""에이전트 로직 테스트: 승인 결정 해석, 요청 경계, 응답·trace 구성, 저장 래퍼의 승인 표시."""
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src import agent, tools
from src.agent import TraceStep


# ---------------------------------------------------------------- 승인 결정 해석
@pytest.mark.parametrize("text,expected", [
    ("승인", "approve"), ("  승인! ", "approve"), ("OK", "approve"), ("네.", "approve"),
    ("거절", "reject"), ("취소해.", "reject"), ("No", "reject"),
    ("표 대신 글머리 목록으로 바꿔줘", "revise"),
    ("승인하지 마", "revise"), ("승인 안 해", "revise"),   # 애매하면 승인으로 보지 않는다
    ("제목을 바꾸고 승인해줘", "revise"),
])
def test_승인_결정은_짧은_정확_일치만_승인_거절로_본다(text, expected):
    assert agent._parse_decision(text) == expected


# ---------------------------------------------------------------- 요청 경계
def test_마지막_사용자_메시지부터를_현재_요청으로_본다():
    old = [HumanMessage("이전 질문"), AIMessage(content="이전 답")]
    now = [HumanMessage("현재 질문"), AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "t"}])]
    assert agent._turn_messages(old + now) == now


def test_도표_검증_피드백은_요청_경계가_아니다():
    """검증 실패 피드백(HumanMessage)이 경계로 잡히면 앞선 도구 호출이 trace 에서 사라진다."""
    q = HumanMessage("도표 그려줘")
    tool_call = AIMessage(content="", tool_calls=[{"name": "build_dependency_graph", "args": {}, "id": "t1"}])
    feedback = HumanMessage("[도표 검증 실패]", additional_kwargs={"diagram_check": {"ok": False, "detail": "x"}})
    msgs = [HumanMessage("이전"), q, tool_call, feedback, AIMessage(content="수정본")]
    assert agent._turn_messages(msgs)[0] is q


# ---------------------------------------------------------------- 응답·trace 구성
def _save_flow(save_output: str):
    return [
        HumanMessage("기능 목록 뽑아서 저장해줘"),
        AIMessage(content="", tool_calls=[{"name": "extract_features", "args": {}, "id": "t1"}]),
        ToolMessage(content="[]", tool_call_id="t1", name="extract_features"),
        AIMessage(content="", tool_calls=[{"name": "save_doc", "args": {"name": "x", "content": "c"}, "id": "t2"}]),
        ToolMessage(content=save_output, tool_call_id="t2", name="save_doc"),
        AIMessage(content="처리했습니다"),
    ]


def test_결정_단계는_저장_단계_바로_앞에_들어간다():
    """회귀: 결정 기록이 요청 맨 앞에 붙어 '승인이 도구 호출보다 먼저'처럼 보이던 문제."""
    decision = TraceStep(step="hitl_approved", input="승인", output="사용자 승인")
    resp = agent._build_response(_save_flow("output/x.md"), decision)
    assert [t.step for t in resp.trace] == ["extract_features", "hitl_approved", "save_doc"]
    assert resp.trace[-1].output == "output/x.md"


def test_실행되지_않은_저장은_미실행으로_표시된다():
    decision = TraceStep(step="hitl_rejected", input="거절", output="사용자 거절")
    resp = agent._build_response(_save_flow("User rejected the tool call for `save_doc`"), decision)
    assert resp.trace[-1].step == "save_doc" and resp.trace[-1].output.startswith("미실행:")


def test_승인_대기는_안내문을_답으로_돌려주고_hitl_pending을_남긴다():
    msgs = _save_flow("")[:4]
    pending = {"action_requests": [{"name": "save_doc", "args": {"name": "x", "sources": ["a.java"]},
                                    "description": "문서 저장 승인이 필요합니다."}]}
    resp = agent._build_response(msgs, None, pending)
    assert resp.answer == "문서 저장 승인이 필요합니다."
    assert resp.trace[-1].step == "hitl_pending" and "저장하지 않음" in resp.trace[-1].output


def test_가드가_차단한_응답은_input_guard_단계를_남기고_도구_호출이_없다():
    blocked = AIMessage(content="접근할 수 없습니다", additional_kwargs={"guard": {"category": "path", "reason": "환경파일"}})
    resp = agent._build_response([HumanMessage(".env 보여줘"), blocked], None)
    assert [t.step for t in resp.trace] == ["input_guard"]
    assert "blocked: path" in resp.trace[0].output and resp.contexts == []


def test_search_code_결과는_청크마다_doc_id를_가진_컨텍스트가_된다():
    hits = '[{"doc_id": "a/B.java:1-5", "path": "a/B.java", "start": 1, "end": 5, "text": "class B {}"}]'
    msgs = [HumanMessage("q"),
            AIMessage(content="", tool_calls=[{"name": "search_code", "args": {"query": "B"}, "id": "t"}]),
            ToolMessage(content=hits, tool_call_id="t", name="search_code"), AIMessage(content="답")]
    resp = agent._build_response(msgs, None)
    assert [c.doc_id for c in resp.contexts] == ["a/B.java:1-5"]


def test_긴_출력은_자르고_응답_키는_규약_세_개다(monkeypatch):
    monkeypatch.setattr(agent, "MAX_TEXT", 10)
    assert agent._clip("가" * 50).endswith("…(생략)")
    assert set(agent.QueryResponse(answer="", contexts=[], trace=[]).model_dump()) == {"answer", "contexts", "trace"}


# ---------------------------------------------------------------- 저장 래퍼의 승인 표시
@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(tools, "OUTPUT_DIR", tmp_path / "output")
    return tmp_path


def test_LLM이_보는_저장_도구에는_approved_인자가_없다():
    schema = next(t for t in agent._build_tools() if t.name == "save_doc").args
    assert "approved" not in schema and {"name", "content"} <= set(schema)


def test_승인_표시가_꺼져_있으면_저장이_거부된다(sandbox):
    assert agent._APPROVED.get() is False
    with pytest.raises(PermissionError):
        agent.save_doc("doc", "내용")
    assert not (sandbox / "output").exists()


def test_승인_표시가_켜진_동안만_저장되고_저장_내용은_마스킹된다(sandbox):
    token = agent._APPROVED.set(True)
    try:
        rel = agent.save_doc("doc", "문의 010-1234-5678", ["a.java"])
    finally:
        agent._APPROVED.reset(token)
    assert rel == "output/doc.md"
    assert "[PHONE]" in (sandbox / rel).read_text(encoding="utf-8")
    assert agent._APPROVED.get() is False   # 호출이 끝나면 다시 꺼진다
