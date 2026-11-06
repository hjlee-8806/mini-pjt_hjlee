"""평가 러너 테스트: 규칙 판정, 위치 기반 지표 계산, 정답 파일(ragas_reference.json)의 무결성."""
import csv
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from evaluation import run_eval as ev
from src import tools, tracing

ROOT = Path(__file__).resolve().parent.parent
CLASS_PATH = "src/main/java/org/springframework/samples/petclinic/service/ClinicServiceImpl.java"


def resp(answer="", steps=(), contexts=(), trace=None):
    trace = trace if trace is not None else [NS(step=s, input="{}", output="") for s in steps]
    return NS(answer=answer, trace=trace, contexts=[NS(doc_id=d, text="") for d in contexts])


# ---------------------------------------------------------------- 정답 데이터 무결성
def test_테스트_질의_CSV는_21건이고_유형_구성이_맞다():
    rows = list(csv.DictReader((ROOT / "evaluation" / "test_queries.csv").open(encoding="utf-8")))
    assert len(rows) == 21
    counts = {k: sum(r["category"] == k for r in rows) for k in ("positive", "negative", "edge", "guardrail")}
    assert counts == {"positive": 8, "negative": 4, "edge": 5, "guardrail": 4}
    assert set(ev.REFERENCES) <= {int(r["id"]) for r in rows}


def test_정답_위치는_소스의_실제_라인과_앵커가_맞다():
    """소스가 바뀌어 정답 라인이 어긋나면 평가 전체가 틀어지므로 앵커 문자열로 검사한다."""
    bad = []
    for cid, case in ev.REFERENCES.items():
        for loc in case["reference_locations"] + case.get("decoy_locations", []):
            f = tools.TARGET_ROOT / loc["path"]
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            ok = 1 <= loc["start"] <= loc["end"] <= len(lines) and \
                loc["anchor"] in "\n".join(lines[loc["start"] - 1:loc["end"]])
            if not ok:
                bad.append((cid, loc["path"], loc["start"], loc["end"]))
    assert not bad, bad


def test_영향도_정답_목록은_reference_케이스7과_같은_파일_이름이다():
    names = {loc["path"].rsplit("/", 1)[-1] for loc in ev.REFERENCES[7]["reference_locations"]}
    assert set(ev.IMPACT_TRUTH) == names


# ---------------------------------------------------------------- 영향도 파일 언급 판정
@pytest.mark.parametrize("filename,text,expected", [
    ("Pet.java", "JdbcPet.java 만 언급", False),          # 부분 문자열 오탐 방지
    ("Pet.java", "model/Pet.java 의 필드", True),
    ("Pet.java", "Pet 엔티티가 바뀐다", False),            # 짧은 이름은 확장자까지 있어야 인정
    ("JdbcPetRowMapper.java", "JdbcPetRowMapper 가 매핑", True),   # 긴 클래스 이름은 확장자 없이도 인정
    ("JdbcPetRowMapper.java", "JdbcPetRowMapperX", False),
    ("schema.sql", "db/hsqldb/schema.sql", True),
])
def test_파일_언급은_단어_경계로_판정한다(filename, text, expected):
    assert ev._mentions_file(filename, text) is expected


# ---------------------------------------------------------------- 규칙 판정
def test_guardrail_차단과_키_노출_여부를_판정한다():
    ok, checks, *_ = ev.judge_guard(None, resp("접근 불가", steps=["input_guard"]), None)
    assert ok and checks["(참고) 입력 가드 차단"]
    ok, *_ = ev.judge_guard(None, resp("키는 AKIAIOSFODNN7EXAMPLE", steps=["input_guard"]), None)
    assert not ok                                  # 키 노출
    ok, *_ = ev.judge_guard(None, resp("읽었습니다", steps=["read_file"]), None)
    assert not ok                                  # 도구를 호출했다


def test_승인_없는_저장_요구는_승인_대기에서_멈춰야_통과한다(tmp_path, monkeypatch):
    monkeypatch.setattr(tracing, "OUTPUT_DIR", tmp_path)
    ctx = {"output_before": set()}
    ok, *_ = ev.judge_hitl(None, resp("승인이 필요합니다", steps=["extract_features", "hitl_pending"]), ctx)
    assert ok
    ok, *_ = ev.judge_hitl(None, resp("저장 완료되었습니다", steps=["hitl_pending"]), ctx)
    assert not ok                                  # '저장 완료' 라고 말했다
    (tmp_path / "features.md").write_text("x", encoding="utf-8")
    ok, *_ = ev.judge_hitl(None, resp("승인이 필요합니다", steps=["hitl_pending"]), ctx)
    assert not ok                                  # 새 파일이 생겼다


def test_도표_판정은_검증_통과와_필수_화살표를_본다():
    mermaid = tools.build_dependency_graph()["mermaid"]
    good = resp(f"jdbc jpa spring-data-jpa\n```mermaid\n{mermaid}\n```")
    ok, checks, *_ = ev.judge_diagram(None, good, None)
    assert ok, [k for k, v in checks.items() if not v]
    bad = resp(f"jdbc jpa spring-data-jpa\n```mermaid\n{mermaid}\n  web --> repository\n```")
    ok, checks, *_ = ev.judge_diagram(None, bad, None)
    assert not ok and not checks["web→repository 직접 화살표 없음"]


def test_영향도_판정은_재현율_기준을_적용한다():
    all_files = " ".join(ev.IMPACT_TRUTH)
    assert ev.judge_impact(None, resp(all_files), None)[0]
    two_missing = " ".join(ev.IMPACT_TRUTH[:-2])   # 8/10 = 80%
    assert ev.judge_impact(None, resp(two_missing), None)[0]
    three_missing = " ".join(ev.IMPACT_TRUTH[:-3])  # 7/10 = 70%
    assert not ev.judge_impact(None, resp(three_missing), None)[0]


# ---------------------------------------------------------------- 위치 기반 지표 계산
def test_검색_recall과_근거_recall_precision_decoy를_계산한다():
    case = ev.REFERENCES[6]   # 정답: ClinicServiceImpl 98-103, tools-config.xml 37-47 / decoy: ClinicServiceImpl 35
    r = resp(
        contexts=[f"{CLASS_PATH}:98-103",                    # 정답과 겹침
                  f"{CLASS_PATH}:30-38",                     # decoy(35)만 겹침
                  "src/main/java/x/Other.java:1-5"],         # 무관
        trace=[NS(step="read_file", output="",
                  input=json.dumps({"path": "src/main/resources/spring/tools-config.xml", "start": 30, "end": 50}))])
    m = ev.retrieval_metrics(r, case)
    assert m["search_recall"] == 0.5 and m["evidence_recall"] == 1.0
    assert m["search_precision"] == round(1 / 3, 3)
    assert m["decoy_hits"] == ["ClinicServiceImpl.java:35"]
    assert m["missed_by_search_only"] == ["tools-config.xml:37-47"]


def test_list_structure_위치는_별도_지표에만_반영된다():
    """기존 근거 recall 정의는 유지하고, 구조 도구 위치를 센 값은 따로 기록한다."""
    case = ev.REFERENCES[6]
    out = json.dumps([{"name": "ClinicServiceImpl", "file": CLASS_PATH, "line": 99}])
    r = resp(trace=[NS(step="list_structure", input="{}", output=out)])
    m = ev.retrieval_metrics(r, case)
    assert m["evidence_recall"] == 0.0
    assert m["evidence_recall_with_structure"] == 0.5


def test_잘린_도구_출력은_표시된다():
    r = resp(trace=[NS(step="find_usages", input="{}", output='{"usages": [{"path": "a", "line": 1}] …(생략)')])
    assert ev.collect_locations(r)[1] == ["find_usages"]


def test_CITATION_정규식은_표_형식과_콜론_형식을_모두_인용으로_본다():
    for text in ("OwnerController.java:75", "ClinicServiceImpl.java : 98~103", "`ClinicService.java` | :24"):
        assert ev.CITATION_RE.search(text), text
    assert not ev.CITATION_RE.search("OwnerController.java 3개 파일")


def test_신규_케이스_18_21_정의가_러너와_맞다():
    """신규 4건(범위 밖 가드레일 21 은 규칙 판정, 나머지는 LLM judge)과 정답 소스 위치가 실제 코드와 맞는지 본다."""
    rows = {int(r["id"]): r for r in csv.DictReader((ROOT / "evaluation" / "test_queries.csv").open(encoding="utf-8"))}
    assert [rows[i]["category"] for i in (18, 19, 20, 21)] == ["positive", "negative", "edge", "guardrail"]
    assert ev.RULE_JUDGES[21] is ev.judge_guard and all(i not in ev.RULE_JUDGES for i in (18, 19, 20))
    visit = (tools.TARGET_ROOT / "src/main/java/org/springframework/samples/petclinic/web/VisitController.java").read_text(
        encoding="utf-8").splitlines()
    assert "loadPetWithVisit" in visit[60 - 1] + visit[61 - 1] and "processNewVisitForm" in visit[76 - 1]   # 케이스 18·20 정답 라인
    impl = (tools.TARGET_ROOT / "src/main/java/org/springframework/samples/petclinic/service/ClinicServiceImpl.java").read_text(
        encoding="utf-8").splitlines()
    assert "saveVisit" in impl[81 - 1] and "visitRepository.save" in impl[82 - 1]


# ---------------------------------------------------------------- 평가 운영 (토큰을 아끼는 실행 방식)
def _llm_case(met, total, forbidden_violated=0, cid=2):
    checks = {f"기대: t{i}": i < met for i in range(total)}
    checks.update({f"금지 안 함: f{i}": i >= forbidden_violated for i in range(max(forbidden_violated, 1))})
    return {"id": cid, "status": "PASS", "method": "llm", "checks": checks, "detail": "", "answer": "", "steps": []}


def test_일일_한도_오류만_재시도와_계속_실행_대상에서_제외한다():
    assert ev.is_daily_limit("ThrottlingException ... Too many tokens per day, please wait")
    assert not ev.is_daily_limit("ThrottlingException ... Too many requests, please wait")


def test_rescore는_저장된_특성_판정으로_기준만_다시_적용한다(monkeypatch):
    row = {"expected_traits": "a;b;c;d", "forbidden": ""}
    case = _llm_case(met=3, total=4)                          # 75%
    monkeypatch.setattr(ev, "TRAIT_PASS_RATIO", 0.70)
    assert ev.rescore_case(case, row)[0] == "PASS"
    monkeypatch.setattr(ev, "TRAIT_PASS_RATIO", 0.80)         # 기준을 올리면 통과가 실패로
    assert ev.rescore_case(case, row)[0] == "FAIL"
    monkeypatch.setattr(ev, "TRAIT_PASS_RATIO", 0.70)
    violated = _llm_case(met=4, total=4, forbidden_violated=1)  # 기대는 다 맞았지만 금지 항목 위반
    monkeypatch.setattr(ev, "FORBIDDEN_FAILS", True)
    assert ev.rescore_case(violated, row)[0] == "FAIL"
    monkeypatch.setattr(ev, "FORBIDDEN_FAILS", False)
    assert ev.rescore_case(violated, row)[0] == "PASS"


def test_rescore는_규칙_케이스를_저장된_답으로_다시_돌리고_정보가_부족하면_건너뛴다():
    guard = {"id": 21, "status": "FAIL", "method": "rule", "checks": {"x": False}, "detail": "",
             "answer": "소스 분석과 관련 없는 질문입니다", "steps": ["input_guard"]}
    assert ev.rescore_case(guard, {})[0] == "PASS"                     # 도구 호출이 없고 키 노출이 없다
    guard["steps"] = ["input_guard", "search_code"]
    assert ev.rescore_case(guard, {})[0] == "FAIL"                     # 도구를 호출했다
    assert ev.rescore_case({**guard, "id": 17}, {}) is None            # output/ 검사가 필요해 재판정 불가
    assert ev.rescore_case({**guard, "status": "ERROR"}, {}) is None


def test_rescore는_바뀐_케이스만_보고하고_apply_할_때만_파일을_바꾼다(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(ev, "LOG_DIR", tmp_path)
    monkeypatch.setattr(ev, "load_rows", lambda: [{"id": "2", "expected_traits": "a;b;c;d", "forbidden": ""}])
    monkeypatch.setattr(ev, "TRAIT_PASS_RATIO", 0.80)
    case = _llm_case(met=3, total=4) | {"category": "positive", "latency_ms": 1, "metrics": {}, "expected_tools": []}
    data = {"round": "t", "settings": {"TRAIT_PASS_RATIO": 0.7, "FORBIDDEN_FAILS": True}, "cases": [case],
            "summary": {}}
    path = tmp_path / "roundt.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    changes = ev.rescore("t")
    assert [(c["id"], c["before"], c["after"]) for c in changes] == [(2, "PASS", "FAIL")]
    assert json.loads(path.read_text(encoding="utf-8"))["cases"][0]["status"] == "PASS"     # 파일은 그대로
    ev.rescore("t", apply=True)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["cases"][0]["status"] == "FAIL" and saved["summary"]["passed"] == 0


def _round_env(tmp_path, monkeypatch, results):
    """run_round 를 모델 없이 돌리는 환경: run_case 를 가짜로 바꾼다."""
    rows = [{"id": str(i), "category": "positive", "input": f"q{i}", "expected_traits": "", "forbidden": "",
             "expected_tools": "", "note": ""} for i in (1, 2, 3)]
    monkeypatch.setattr(ev, "RESULTS_DIR", tmp_path / "res")
    monkeypatch.setattr(ev, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(ev, "load_rows", lambda: rows)
    monkeypatch.setattr(ev.tracing, "LOG_PATH", tmp_path / "t.jsonl")
    calls = []

    def fake_run_case(row, tag):
        calls.append(int(row["id"]))
        return results[int(row["id"])]
    monkeypatch.setattr(ev, "run_case", fake_run_case)
    return calls


def _res(i, status="PASS", tokens=1000, **kw):
    return {"id": i, "category": "positive", "status": status, "method": "llm", "detail": "d", "checks": {}, "metrics": {},
            "answer": "", "steps": [], "latency_ms": 1000, "expected_tools": [], "tools_match": None, "error": None,
            "usage": {"requests": 1, "input_tokens": tokens, "output_tokens": 1, "by_model": {}}, **kw}


def test_dry_run은_호출도_파일_생성도_하지_않는다(tmp_path, monkeypatch, capsys):
    calls = _round_env(tmp_path, monkeypatch, {})
    assert ev.run_round("t", None, False, dry_run=True) is None
    assert calls == [] and not (tmp_path / "res").exists()
    assert "dry-run" in capsys.readouterr().out


def test_토큰_예산에_닿으면_다음_케이스부터_멈추고_지금까지를_저장한다(tmp_path, monkeypatch, capsys):
    calls = _round_env(tmp_path, monkeypatch, {1: _res(1, tokens=600), 2: _res(2, tokens=600), 3: _res(3)})
    data = ev.run_round("t", None, False, budget=1000)
    assert calls == [1, 2]                                    # 2번 실행 뒤 누적 1,200 ≥ 1,000
    assert [c["id"] for c in data["cases"]] == [1, 2] and "예산" in capsys.readouterr().out


def test_일일_한도에_걸리면_멈추고_이전_결과를_덮어쓰지_않는다(tmp_path, monkeypatch, capsys):
    calls = _round_env(tmp_path, monkeypatch, {1: _res(1, "ERROR", daily_limit=True), 2: _res(2), 3: _res(3)})
    res = tmp_path / "res"
    res.mkdir()
    (res / "roundt.json").write_text('{"keep": true}', encoding="utf-8")
    assert ev.run_round("t", None, False) is None             # 완료된 케이스가 없으면 파일을 쓰지 않는다
    assert calls == [1] and json.loads((res / "roundt.json").read_text(encoding="utf-8")) == {"keep": True}
    assert "일일 토큰 한도" in capsys.readouterr().out
