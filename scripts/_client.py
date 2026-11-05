"""수동 테스트 스크립트 공용 부분: POST /query 호출과 응답(답변·trace·contexts) 출력.

표준 라이브러리만 쓴다. 요청은 UTF-8 로 보내고 응답도 UTF-8 로 직접 풀기 때문에 Windows 에서도 한글이 깨지지 않는다.
서버 주소는 --url 또는 환경변수 QUERY_URL 로 바꾼다. (기본 http://127.0.0.1:8000/query)
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = os.getenv("QUERY_URL", "http://127.0.0.1:8000/query")
RESPONSE_KEYS = ("answer", "contexts", "trace")  # 제출 규약: 응답은 이 세 키

sys.stdout.reconfigure(errors="replace")  # 콘솔이 못 찍는 문자가 있어도 멈추지 않는다


def ask(question: str, session_id: str | None = None, url: str = DEFAULT_URL, timeout: int = 300) -> tuple[dict, float]:
    """질문을 보내고 (응답 dict, 걸린 초)를 반환한다. 실패하면 RuntimeError."""
    body = json.dumps({"question": question, "session_id": session_id}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json; charset=utf-8"})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            data = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"서버에 연결할 수 없습니다 ({url}): {e.reason}\n"
                           "먼저 다른 터미널에서 'uvicorn src.api:app --port 8000' 으로 서버를 실행하세요.") from None
    return data, time.perf_counter() - started


def status_of(trace: list[dict]) -> str:
    """trace 로 이번 응답의 상태를 한 단어로 판정한다: 차단 / 승인 대기 / 저장됨 / 거절됨 / 일반."""
    steps = [t["step"] for t in trace]
    if "input_guard" in steps:
        return "차단"
    if steps and steps[-1] == "hitl_pending":
        return "승인 대기"
    if any(t["step"] == "save_doc" and t["output"].strip().startswith("output/") for t in trace):
        return "저장됨"
    if "hitl_rejected" in steps:
        return "거절됨"
    return "일반"


def clip(text: str, n: int) -> str:
    """한 줄로 줄이고 n 자를 넘으면 자른다."""
    one = " ".join(str(text).split())
    return one if len(one) <= n else one[: n - 1] + "…"


def print_response(data: dict, elapsed: float, full: bool = False, answer_chars: int | None = None) -> None:
    """답변, trace 표, contexts 요약, 상태를 출력한다. full=True 면 trace 입력·출력을 자르지 않는다."""
    missing = [k for k in RESPONSE_KEYS if k not in data]
    if missing:
        print(f"[경고] 응답에 규약 키가 없습니다: {missing}")
    answer = data.get("answer", "")
    if answer_chars is not None and len(answer) > answer_chars:
        answer = answer[:answer_chars] + f"\n… (이하 {len(data['answer']) - answer_chars}자 생략, --full 로 전체 보기)"
    print(f"\n── 답변 ({elapsed:.1f}초) " + "─" * 40)
    print(answer)

    trace = data.get("trace", [])
    print(f"\n── trace ({len(trace)}단계) " + "─" * 38)
    if not trace:
        print("  (단계 없음)")
    for i, t in enumerate(trace, 1):
        if full:
            print(f"  {i:>2}. {t['step']}\n      입력: {t['input']}\n      출력: {t['output']}")
        else:
            print(f"  {i:>2}. {t['step']:<24} 입력 {clip(t['input'], 50):<52} → {clip(t['output'], 60)}")

    ctx = data.get("contexts", [])
    ids = [c["doc_id"].rsplit("/", 1)[-1] for c in ctx]
    shown = ", ".join(ids[:8]) + (f" 외 {len(ids) - 8}개" if len(ids) > 8 else "")
    print(f"\n── contexts {len(ctx)}개: {shown or '(없음)'}")
    print(f"── 상태: {status_of(trace)}")
