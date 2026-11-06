"""토큰 사용량 집계 테스트: Ledger, LangChain 콜백(중첩·에이전트 그래프), 임베딩 스레드, 평가 추정."""
import io
import json

import numpy as np
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool

from evaluation import run_eval as ev
from src import embeddings, usage


def ai(text="x", inp=100, out=10, cache=0, **kw):
    meta = {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}
    if cache:
        meta["input_token_details"] = {"cache_read": cache}
    return AIMessage(content=text, usage_metadata=meta, **kw)


class FakeModel(GenericFakeChatModel):
    """모델 ID 를 알리고 도구 바인딩을 허용하는 가짜 채팅 모델."""

    @property
    def _identifying_params(self):
        return {"model": "fake-model"}

    def bind_tools(self, tools, **kwargs):
        return self


# ---------------------------------------------------------------- Ledger
def test_ledger는_모델별로_합치고_상위_구간에도_더한다():
    with usage.collect() as outer:
        with usage.collect() as inner:
            inner.add("m1", input_tokens=100, output_tokens=10)
            inner.add("m1", input_tokens=50, output_tokens=5, cache_read_tokens=30)
            inner.add("m2", input_tokens=7)
    s = outer.summary()
    assert (s["requests"], s["input_tokens"], s["output_tokens"], s["cache_read_tokens"]) == (3, 157, 15, 30)
    assert s["by_model"]["m1"]["requests"] == 2 and s["by_model"]["m2"]["input_tokens"] == 7
    assert usage.current() is None                       # 구간이 끝나면 닫힌다


def test_merge와_format_summary():
    a, b = usage.Ledger(), usage.Ledger()
    a.add("m", input_tokens=10, output_tokens=1)
    b.add("m", input_tokens=5, output_tokens=2, cache_read_tokens=4)
    s = usage.merge([a.summary(), b.summary(), None])
    assert s["requests"] == 2 and s["input_tokens"] == 15 and s["cache_read_tokens"] == 4
    assert "요청 2건" in usage.format_summary(s) and "캐시 읽기 4" in usage.format_summary(s)
    assert "캐시" not in usage.format_summary(a.summary())


def test_집계_구간_밖에서는_아무것도_하지_않는다():
    assert usage.current_callbacks() == []
    usage.add_embedding("m", 5)   # 예외 없이 무시


# ---------------------------------------------------------------- 콜백
def test_콜백은_모델_ID와_토큰_캐시를_기록한다():
    led = usage.Ledger()
    FakeModel(messages=iter([ai(inp=120, out=8, cache=40)])).invoke("q", config={"callbacks": led.callbacks()})
    r = led.summary()["by_model"]["fake-model"]
    assert (r["requests"], r["input_tokens"], r["output_tokens"], r["cache_read_tokens"]) == (1, 120, 8, 40)


def test_label을_주면_같은_모델도_따로_센다():
    led = usage.Ledger()
    FakeModel(messages=iter([ai(), ai()])).invoke("a", config={"callbacks": led.callbacks("judge")})
    assert set(led.summary()["by_model"]) == {"fake-model [judge]"}


def test_바깥_실행의_콜백이_안쪽_모델_호출까지_잡는다():
    """범위 분류·재작성·요약처럼 그래프 안에서 config 없이 .invoke 하는 모델 호출도 집계되어야 한다."""
    inner = FakeModel(messages=iter([ai(inp=11, out=1)]))
    led = usage.Ledger()
    RunnableLambda(lambda x: inner.invoke(x)).invoke("q", config={"callbacks": led.callbacks()})
    assert led.summary()["input_tokens"] == 11


def test_에이전트_그래프의_모델_호출은_모두_집계된다():
    @tool
    def ping(x: str) -> str:
        """ping"""
        return "pong"

    model = FakeModel(messages=iter([
        AIMessage(content="", tool_calls=[{"name": "ping", "args": {"x": "a"}, "id": "t1"}],
                  usage_metadata={"input_tokens": 200, "output_tokens": 20, "total_tokens": 220}),
        ai("끝", inp=260, out=12)]))
    agent = create_agent(model, [ping])
    with usage.collect() as led:
        agent.invoke({"messages": [{"role": "user", "content": "hi"}]}, {"callbacks": usage.current_callbacks()})
    s = led.summary()
    assert (s["requests"], s["input_tokens"], s["output_tokens"]) == (2, 460, 32)


def test_실패한_호출은_토큰으로_세지_않는다():
    class Boom(FakeModel):
        def _generate(self, *a, **k):
            raise RuntimeError("throttled")

    led = usage.Ledger()
    try:
        Boom(messages=iter([])).invoke("q", config={"callbacks": led.callbacks()})
    except RuntimeError:
        pass
    assert led.summary()["requests"] == 0


# ---------------------------------------------------------------- 임베딩 (작업 스레드)
def test_임베딩_토큰은_작업_스레드에서도_집계된다(tmp_path, monkeypatch):
    class Client:
        def invoke_model(self, modelId, body):
            n = len(json.loads(body)["inputText"])
            return {"body": io.BytesIO(json.dumps({"embedding": [0.0] * embeddings.EMBED_DIM, "inputTextTokenCount": n}).encode())}

    monkeypatch.setattr(embeddings, "_bedrock", lambda: Client())
    monkeypatch.setattr(embeddings, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(embeddings, "CACHE_PATH", tmp_path / "cache.json")
    with usage.collect() as led:
        vecs = embeddings.embed_texts(["aaa", "bbbbb", "aaa"])   # 중복은 한 번만 호출
    assert vecs.shape == (3, embeddings.EMBED_DIM) and isinstance(vecs, np.ndarray)
    s = led.summary()
    assert s["requests"] == 2 and s["input_tokens"] == 8
    with usage.collect() as led2:
        embeddings.embed_texts(["aaa"])                           # 캐시 적중이면 호출도 토큰도 없다
    assert led2.summary()["requests"] == 0


# ---------------------------------------------------------------- 평가 러너
def test_평가_사용량_추정은_기록이_없으면_추정_불가를_알린다(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "RESULTS_DIR", tmp_path)
    assert "추정 불가" in ev.estimate_usage(5)
    (tmp_path / "round1.json").write_text(json.dumps({"cases": [
        {"id": 1, "usage": {"requests": 4, "input_tokens": 20000}},
        {"id": 2, "usage": {"requests": 6, "input_tokens": 30000}},
        {"id": 3}]}), encoding="utf-8")                           # 토큰 기록이 없는 옛 케이스는 평균에서 뺀다
    msg = ev.estimate_usage(4)
    assert "100,000" in msg and "모델 요청 20건" in msg and "이전 2건" in msg


def test_평가_요약은_케이스별_토큰을_합친다():
    def case(i, tokens):
        led = usage.Ledger()
        led.add("m", input_tokens=tokens, output_tokens=1)
        return {"id": i, "category": "positive", "status": "PASS", "detail": "", "metrics": {}, "expected_tools": [],
                "latency_ms": 1, "usage": led.summary()}

    s = ev.summarize([case(1, 100), case(2, 50)], ev.LOG_DIR / "없는-로그.jsonl")
    assert s["usage"]["input_tokens"] == 150 and s["usage"]["requests"] == 2
