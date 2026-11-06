"""trace 로그 테스트: 상태 판정, 마스킹, 기록·읽기, HITL 준수 검사."""
import json

from src import tracing


def step(name, output="", inp=""):
    return {"step": name, "input": inp, "output": output}


def record(rid, sid, trace, status=None, question="q"):
    r = tracing.make_record(session_id=sid, question=question, response={"answer": "a", "contexts": [], "trace": trace},
                            latency_ms=1, model="m", usage={}, error=None)
    r["request_id"] = rid
    if status:
        r["status"] = status
    return r


def test_상태는_trace_단계로_판정한다():
    c = tracing.classify_status
    assert c([step("input_guard")], None) == "blocked"
    assert c([step("extract_features"), step("hitl_pending")], None) == "pending_approval"
    assert c([step("hitl_approved"), step("save_doc", "output/x.md")], None) == "saved"
    assert c([step("hitl_rejected"), step("save_doc", "미실행: User rejected")], None) == "rejected"
    assert c([step("search_code")], None) == "ok"
    assert c([], "RuntimeError") == "error"


def test_기록_직전에_질문과_trace의_비밀을_마스킹한다():
    r = tracing.make_record(session_id="s", question="전화 010-1234-5678 키 AKIAIOSFODNN7EXAMPLE",
                            response={"answer": "010-1234-5678", "contexts": [{"doc_id": "d", "text": "t"}],
                                      "trace": [step("x", "AKIAIOSFODNN7EXAMPLE")]},
                            latency_ms=5, model="m", usage={"input_tokens": 1}, error=None)
    blob = json.dumps(r, ensure_ascii=False)
    assert "010-1234-5678" not in blob and "AKIAIOSFODNN7EXAMPLE" not in blob
    assert r["context_ids"] == ["d"] and r["usage"] == {"input_tokens": 1}


def test_기록한_로그를_다시_읽을_수_있고_깨진_줄은_건너뛴다(tmp_path, monkeypatch):
    log = tmp_path / "trace.jsonl"
    monkeypatch.setattr(tracing, "LOG_PATH", log)
    tracing.write_trace(record("r1", "s", [step("search_code")]))
    with log.open("a", encoding="utf-8") as f:
        f.write("깨진 줄\n")
    tracing.write_trace(record("r2", "s", [step("input_guard")]))
    assert [r["request_id"] for r in tracing.read_traces()] == ["r1", "r2"]


def test_로그_쓰기가_실패해도_예외를_내지_않는다(tmp_path, monkeypatch):
    monkeypatch.setattr(tracing, "LOG_PATH", tmp_path)  # 디렉터리라 열 수 없다
    tracing.write_trace(record("r1", "s", []))          # 응답을 막으면 안 된다


def test_정상_승인_흐름은_위반이_없다():
    recs = [record("r1", "s", [step("hitl_pending")]),
            record("r2", "s", [step("hitl_approved"), step("save_doc", "output/x.md")])]
    assert tracing.hitl_violations(recs) == []


def test_승인_없는_저장을_위반으로_잡는다():
    v = tracing.hitl_violations([record("f1", "X", [step("save_doc", "output/evil.md")])])
    assert [x["type"] for x in v] == ["승인 없는 저장"]


def test_승인_대기_이력이_없는_승인을_위반으로_잡는다():
    v = tracing.hitl_violations([record("f2", "Y", [step("hitl_approved"), step("save_doc", "output/g.md")])])
    assert [x["type"] for x in v] == ["승인 대기 이력 없는 승인"]


def test_거절된_저장은_위반이_아니다():
    recs = [record("r1", "s", [step("hitl_pending")]),
            record("r2", "s", [step("hitl_rejected"), step("save_doc", "미실행: User rejected")])]
    assert tracing.hitl_violations(recs) == []


def test_로그에_근거_없는_output_파일을_찾아낸다(tmp_path):
    (tmp_path / "ok.md").write_text("x", encoding="utf-8")
    (tmp_path / "orphan.md").write_text("x", encoding="utf-8")
    recs = [record("r", "s", [step("hitl_approved"), step("save_doc", "output/ok.md")])]
    assert tracing.unaccounted_outputs(recs, tmp_path) == ["orphan.md"]


def test_요약은_상태_분포와_토큰_합계를_센다():
    a, b = record("a", "s", [step("search_code")]), record("b", "s", [step("input_guard")])
    a["usage"], a["latency_ms"] = {"input_tokens": 10, "output_tokens": 2}, 100
    b["latency_ms"] = 300
    s = tracing.summarize([a, b])
    assert s["requests"] == 2 and s["status"] == {"ok": 1, "blocked": 1}
    assert s["input_tokens"] == 10 and s["latency_ms_avg"] == 200 and s["latency_ms_max"] == 300
