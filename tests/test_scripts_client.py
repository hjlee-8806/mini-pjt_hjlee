"""수동 시험 스크립트의 공용 부분 테스트: 상태 판정과 출력 도우미. (서버 호출은 하지 않는다)"""
import _client


def test_응답_상태를_trace로_판정한다():
    s = lambda name, out="": {"step": name, "input": "", "output": out}  # noqa: E731
    assert _client.status_of([s("input_guard")]) == "차단"
    assert _client.status_of([s("search_code"), s("hitl_pending")]) == "승인 대기"
    assert _client.status_of([s("hitl_approved"), s("save_doc", "output/x.md")]) == "저장됨"
    assert _client.status_of([s("hitl_rejected"), s("save_doc", "미실행: 거절")]) == "거절됨"
    assert _client.status_of([s("search_code")]) == "일반"


def test_긴_문장은_한_줄로_줄인다():
    assert _client.clip("가\n나   다", 10) == "가 나 다"
    assert len(_client.clip("가" * 100, 10)) == 10 and _client.clip("가" * 100, 10).endswith("…")


def test_서버가_없으면_안내가_담긴_오류를_낸다():
    import pytest
    with pytest.raises(RuntimeError, match="uvicorn"):
        _client.ask("질문", url="http://127.0.0.1:9/query", timeout=2)  # 9번 포트는 아무도 쓰지 않는다
