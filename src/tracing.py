"""trace 기록·검사 (SERVICE.md 3-7, 5-2): 요청마다 JSON 한 줄을 남기고, 그 기록으로 HITL 준수를 검사한다.

- 쓰기: agent.run_query 가 요청이 끝날 때(차단·승인 대기·예외 포함) write_trace(make_record(...)) 를 호출한다.
- 로그 위치: logs/trace.jsonl (환경변수 TRACE_LOG_PATH 로 변경). 한 줄 = 한 요청. 응답의 trace 와 같은 단계 목록을 담는다.
- 검사:  python -m src.tracing --check     HITL 준수(승인 없는 저장 0건) + 요약
         python -m src.tracing --tail 5     최근 N건 보기
- 승인 여부는 trace 단계 이름으로 표현한다: hitl_pending → hitl_approved → save_doc(저장 경로). (응답 형식은 바꾸지 않는다)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from src.guardrails import mask_text

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = Path(os.getenv("TRACE_LOG_PATH", PROJECT_ROOT / "logs" / "trace.jsonl"))
OUTPUT_DIR = PROJECT_ROOT / "output"

QUESTION_CHARS = 1000   # 로그에 남기는 질문 최대 길이
ANSWER_CHARS = 500      # 로그에 남기는 답변 최대 길이
SAVED_PATH_RE = re.compile(r"^output/.+\.md$")  # save_doc 이 저장 성공 시 반환하는 경로 형태

_lock = threading.Lock()  # API 가 여러 스레드로 요청을 처리해도 한 줄이 섞이지 않게 한다
_log = logging.getLogger(__name__)


# ================================================================== 기록
def classify_status(trace: list[dict], error: str | None) -> str:
    """trace 단계로 요청 상태를 정한다: error / blocked / pending_approval / saved / rejected / ok."""
    if error:
        return "error"
    steps = [t["step"] for t in trace]
    if "input_guard" in steps:
        return "blocked"
    if steps and steps[-1] == "hitl_pending":
        return "pending_approval"
    if any(t["step"] == "save_doc" and SAVED_PATH_RE.match(t["output"].strip()) for t in trace):
        return "saved"
    if "hitl_rejected" in steps:
        return "rejected"
    return "ok"


def make_record(*, session_id: str, question: str, response: dict | None, latency_ms: int,
                model: str, usage: dict, error: str | None) -> dict:
    """로그 한 줄(dict)을 만든다. 질문·답변·trace 는 기록 직전에 한 번 더 마스킹한다."""
    trace = [{k: mask_text(str(v)) for k, v in step.items()} for step in (response or {}).get("trace", [])]
    return {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "request_id": uuid.uuid4().hex[:12],
        "session_id": session_id,
        "question": mask_text(question)[:QUESTION_CHARS],
        "status": classify_status(trace, error),
        "latency_ms": latency_ms,
        "model": model,
        "usage": usage,
        "answer": mask_text((response or {}).get("answer", ""))[:ANSWER_CHARS],
        "context_ids": [c["doc_id"] for c in (response or {}).get("contexts", [])],
        "trace": trace,
        "error": error,
    }


def write_trace(record: dict) -> None:
    """로그 파일에 한 줄 추가한다. 쓰기에 실패해도 응답을 막지 않는다."""
    try:
        line = json.dumps(record, ensure_ascii=False)
        with _lock:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:  # noqa: BLE001 - 관측 실패가 서비스 실패가 되면 안 된다
        _log.warning("trace 로그 기록 실패: %s", type(e).__name__)


def read_traces(path: Path | None = None) -> list[dict]:
    """로그를 읽어 기록 목록으로 반환한다. 깨진 줄은 건너뛴다."""
    path = path or LOG_PATH
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ================================================================== 검사
def _saved_steps(record: dict) -> list[int]:
    """기록 안에서 저장 경로를 반환한(= 실제로 저장된) save_doc 단계의 위치."""
    return [i for i, t in enumerate(record["trace"])
            if t["step"] == "save_doc" and SAVED_PATH_RE.match(t["output"].strip())]


def hitl_violations(records: list[dict]) -> list[dict]:
    """HITL 위반을 찾는다. (승인 없는 저장 0건이 목표 — SERVICE.md 5-2)
    ① 저장된 save_doc 앞에 같은 요청의 hitl_approved 가 없다.
    ② hitl_approved 인데 같은 세션에 직전의 승인 대기(pending_approval) 기록이 없다."""
    violations = []
    last_status: dict[str, str] = {}  # session_id -> 직전 요청의 상태
    for r in records:
        steps = [t["step"] for t in r["trace"]]
        for i in _saved_steps(r):
            if "hitl_approved" not in steps[:i]:
                violations.append({"request_id": r["request_id"], "session_id": r["session_id"],
                                   "type": "승인 없는 저장", "detail": r["trace"][i]["output"]})
        if "hitl_approved" in steps and last_status.get(r["session_id"]) != "pending_approval":
            violations.append({"request_id": r["request_id"], "session_id": r["session_id"],
                               "type": "승인 대기 이력 없는 승인", "detail": ""})
        last_status[r["session_id"]] = r["status"]
    return violations


def unaccounted_outputs(records: list[dict], output_dir: Path | None = None) -> list[str]:
    """output/ 에 있는데 로그의 '승인 후 저장' 기록에 없는 파일 이름 목록."""
    output_dir = output_dir or OUTPUT_DIR
    if not output_dir.exists():
        return []
    saved = {Path(r["trace"][i]["output"].strip()).name for r in records for i in _saved_steps(r)}
    return sorted(f.name for f in output_dir.glob("*.md") if f.name not in saved)


def summarize(records: list[dict]) -> dict:
    """요청 수, 상태 분포, 지연, 토큰, 도구 호출 분포를 요약한다."""
    lat = [r["latency_ms"] for r in records]
    tools = Counter(t["step"] for r in records for t in r["trace"])
    return {
        "requests": len(records),
        "status": dict(Counter(r["status"] for r in records)),
        "latency_ms_avg": int(sum(lat) / len(lat)) if lat else 0,
        "latency_ms_max": max(lat) if lat else 0,
        "input_tokens": sum(r.get("usage", {}).get("input_tokens", 0) for r in records),
        "output_tokens": sum(r.get("usage", {}).get("output_tokens", 0) for r in records),
        "steps": dict(tools.most_common()),
    }


def _main() -> int:
    ap = argparse.ArgumentParser(description="trace 로그 검사")
    ap.add_argument("--check", action="store_true", help="HITL 준수 검사 + 요약")
    ap.add_argument("--tail", type=int, metavar="N", help="최근 N건 보기")
    ap.add_argument("--log", type=Path, help="로그 파일 경로 (기본 logs/trace.jsonl)")
    a = ap.parse_args()
    records = read_traces(a.log)
    if a.tail:
        for r in records[-a.tail:]:
            steps = " → ".join(t["step"] for t in r["trace"]) or "(단계 없음)"
            print(f'{r["ts"]} {r["status"]:<16} {r["latency_ms"]:>6}ms  {r["question"][:40]!r}  [{steps}]')
        return 0
    print(f"로그: {a.log or LOG_PATH}  ({len(records)}건)")
    s = summarize(records)
    print(f'상태 분포: {s["status"]} | 평균 {s["latency_ms_avg"]}ms, 최대 {s["latency_ms_max"]}ms | '
          f'토큰 입력 {s["input_tokens"]:,} / 출력 {s["output_tokens"]:,}')
    print(f'단계 분포: {s["steps"]}')
    v = hitl_violations(records)
    extra = unaccounted_outputs(records)
    print(f"HITL 위반: {len(v)}건" + ("  ← 목표 0건 OK" if not v else ""))
    for x in v:
        print(f'  - [{x["type"]}] request={x["request_id"]} session={x["session_id"]} {x["detail"]}')
    if extra:
        print(f"로그에 승인 기록이 없는 output/ 파일: {extra}")
    return 1 if v else 0


if __name__ == "__main__":
    sys.exit(_main())
