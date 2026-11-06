"""API 계약 테스트: POST /query 의 입력(question, 선택 session_id)과 출력(answer·contexts·trace). 에이전트는 가짜로 대체."""
import pytest

pytest.importorskip("httpx")  # TestClient 가 쓴다 (fastapi 하위 의존성)
from fastapi.testclient import TestClient  # noqa: E402

from src import api  # noqa: E402
from src.agent import Context, QueryResponse, TraceStep  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    calls = []

    def fake_run_query(question, session_id=None):
        calls.append((question, session_id))
        return QueryResponse(answer="답", contexts=[Context(doc_id="d", text="t")],
                             trace=[TraceStep(step="search_code", input="{}", output="[]")])

    monkeypatch.setattr(api, "run_query", fake_run_query)
    c = TestClient(api.app)
    c.calls = calls
    return c


def test_응답은_규약대로_세_키를_가진다(client):
    r = client.post("/query", json={"question": "질문"})
    assert r.status_code == 200
    assert set(r.json()) == {"answer", "contexts", "trace"}
    assert r.json()["trace"][0]["step"] == "search_code"


def test_session_id는_선택이고_있으면_그대로_전달된다(client):
    client.post("/query", json={"question": "a"})
    client.post("/query", json={"question": "b", "session_id": "s1"})
    assert client.calls == [("a", None), ("b", "s1")]


def test_question이_없으면_422다(client):
    assert client.post("/query", json={}).status_code == 422


def test_에이전트_오류는_500이고_내부_메시지를_노출하지_않는다(monkeypatch):
    def boom(question, session_id=None):
        raise RuntimeError("비밀키 AKIA... 가 들어간 내부 메시지")

    monkeypatch.setattr(api, "run_query", boom)
    r = TestClient(api.app).post("/query", json={"question": "q"})
    assert r.status_code == 500
    assert "RuntimeError" in r.json()["detail"] and "비밀키" not in r.text
