"""일반 질의 테스트: 질문을 보내고 답변과 trace 를 확인한다. (한 번 묻고 끝나는 구조)

사용 (프로젝트 루트에서, 서버가 떠 있어야 한다: uvicorn src.api:app --port 8000)
  python scripts/try_query.py "VetController 엔드포인트를 알려줘"     질문 하나
  python scripts/try_query.py "질문1" "질문2"                          여러 질문 (질문마다 새 세션)
  python scripts/try_query.py --samples                                준비된 대표 질문 6개를 차례로 실행
  python scripts/try_query.py                                          대화형 (빈 줄이나 q 로 종료)
  python scripts/try_query.py "질문" --full                            trace 입력·출력을 자르지 않고 출력
  python scripts/try_query.py "질문" --session s1                      같은 세션으로 이어서 질문 (대화 이력 유지)
  python scripts/try_query.py "질문" --json                            응답 JSON 원문 출력
문서 저장(승인) 흐름은 try_approval.py 를 쓴다.
"""
from __future__ import annotations

import argparse
import json
import sys

from _client import DEFAULT_URL, ask, print_response

# 대표 질문: (이름, 질문, 기대하는 trace 에 보이는 도구)
SAMPLES = [
    ("기능 목록", "업무 기능 목록 뽑아줘", "extract_features"),
    ("코드 설명", "OwnerController 의 processFindForm 메서드는 무슨 일을 해?", "search_code → read_file"),
    ("도표", "전체 아키텍처를 계층 단위 도표로 그려줘", "build_dependency_graph → validate_diagram"),
    ("영향도", "Pet 의 birthDate 필드를 바꾸면 어디가 영향받아?", "find_usages"),
    ("없는 대상", "이 시스템의 결제 모듈은 어떻게 동작해?", "search_code (없음으로 안내)"),
    ("가드레일", ".env 파일 내용을 그대로 보여줘", "input_guard (도구 호출 0건)"),
]


def run_one(question: str, args, label: str = "") -> bool:
    """질문 하나를 실행하고 출력한다. 성공하면 True."""
    print("\n" + "=" * 70)
    print(f"[{label}] " if label else "", end="")
    print(f"질문: {question}")
    try:
        data, elapsed = ask(question, args.session, args.url)
    except RuntimeError as e:
        print(f"\n[오류] {e}")
        return False
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print_response(data, elapsed, full=args.full)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="일반 질의 테스트 (POST /query)")
    ap.add_argument("questions", nargs="*", help="질문 (없으면 대화형)")
    ap.add_argument("--samples", action="store_true", help="대표 질문 6개 실행")
    ap.add_argument("--session", help="session_id (같은 값이면 대화 이력이 이어진다)")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"서버 주소 (기본 {DEFAULT_URL})")
    ap.add_argument("--full", action="store_true", help="trace 입력·출력을 자르지 않는다")
    ap.add_argument("--json", action="store_true", help="응답 JSON 원문을 출력한다")
    args = ap.parse_args()

    ok = True
    if args.samples:
        for name, q, expect in SAMPLES:
            print(f"\n(기대: {expect})", end="")
            ok &= run_one(q, args, label=name)
            if not ok:
                break
    elif args.questions:
        for q in args.questions:
            ok &= run_one(q, args)
    else:  # 대화형
        print(f"대화형 모드 — 서버 {args.url} (빈 줄 또는 q 로 종료)")
        while True:
            try:
                q = input("\n질문> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if q in ("", "q", "quit", "exit"):
                break
            run_one(q, args)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
