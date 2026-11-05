"""문서 저장 승인(HITL) 흐름 테스트: 같은 세션에서 여러 턴으로 이어지는 구조.

  1) 분석 질문  →  2) 저장 요청  →  3) 승인 대기(저장 안 됨)  →  4) 승인 / 거절 / 수정 지시(최대 3회)

사용 (프로젝트 루트에서, 서버가 떠 있어야 한다: uvicorn src.api:app --port 8000)
  python scripts/try_approval.py                         대화형: 승인 화면을 보고 직접 승인·거절·수정 지시를 입력
  python scripts/try_approval.py --decision reject       자동: 거절 → 파일이 안 생기는지 확인
  python scripts/try_approval.py --decision approve --cleanup
                                                         자동: 승인 → 파일이 생기는지 확인한 뒤 그 파일을 지운다
  python scripts/try_approval.py --revise "표 대신 글머리 목록으로 바꿔줘" --decision approve --cleanup
                                                         수정 요청 후 승인까지
  옵션: --topic "분석 질문"  --name 파일이름  --full  --url http://127.0.0.1:8000/query

점검하는 것: ① 승인 전에는 파일이 없다 ② 승인하면 hitl_approved 뒤에 save_doc 이 저장 경로를 돌려주고 파일이 생긴다
③ 거절하면 파일이 없다 ④ 승인 없이 저장된 기록(save_doc 앞에 hitl_approved 가 없음)이 없다.
서버는 이 프로젝트 폴더에서 실행해야 output/ 확인이 맞는다.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from _client import DEFAULT_URL, ask, clip, print_response, status_of

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
MAX_ROUNDS = 5  # 수정 요청 3회 + 최종 결정 여유


def step_title(n: int, text: str) -> None:
    print("\n" + "#" * 70 + f"\n# {n}단계: {text}\n" + "#" * 70)


def saved_before_approval(trace: list[dict]) -> bool:
    """HITL 위반: 저장 경로를 돌려준 save_doc 앞에 hitl_approved 가 없으면 True."""
    steps = [t["step"] for t in trace]
    for i, t in enumerate(trace):
        if t["step"] == "save_doc" and t["output"].strip().startswith("output/") and "hitl_approved" not in steps[:i]:
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="문서 저장 승인 흐름 테스트")
    ap.add_argument("--topic", default="VetController 엔드포인트만 정리해줘", help="먼저 보낼 분석 질문")
    ap.add_argument("--name", default="try_approval_demo", help="저장할 문서 이름")
    ap.add_argument("--decision", choices=["ask", "approve", "reject"], default="ask",
                    help="ask: 직접 입력(기본), approve/reject: 자동")
    ap.add_argument("--revise", help="승인·거절 전에 먼저 보낼 수정 지시 (한 번)")
    ap.add_argument("--cleanup", action="store_true", help="승인으로 새로 만들어진 문서를 시험 뒤 지운다")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--full", action="store_true", help="trace 입력·출력과 답변을 자르지 않는다")
    args = ap.parse_args()

    sid = f"try-approval-{int(time.time())}"
    problems: list[str] = []
    all_trace: list[dict] = []
    limit = None if args.full else 400
    print(f"세션: {sid} / 서버: {args.url}")

    try:
        step_title(1, "분석 질문 (저장할 내용을 만든다)")
        print(f"질문: {args.topic}")
        data, el = ask(args.topic, sid, args.url)
        print_response(data, el, full=args.full, answer_chars=limit)
        all_trace += data["trace"]

        step_title(2, "저장 요청 → 승인 대기에서 멈춰야 한다")
        req = f"이 결과를 {args.name} 로 저장해줘"
        print(f"질문: {req}")
        data, el = ask(req, sid, args.url)
        print_response(data, el, full=args.full)
        all_trace += data["trace"]
    except RuntimeError as e:
        print(f"\n[오류] {e}")
        return 1

    if status_of(data["trace"]) != "승인 대기":
        print("\n[문제] 승인 대기 상태가 아닙니다. (모델이 save_doc 을 호출하지 않았을 수 있습니다. 다시 실행해 보세요)")
        return 1
    m = re.search(r"저장 경로:\s*(\S+)", data["answer"])
    rel = m.group(1) if m else f"output/{args.name}.md"
    target = OUTPUT_DIR.parent / rel
    existed = target.exists()
    print(f"\n[확인] 승인 전 저장 예정 경로 {rel}: " + ("이미 있던 파일입니다(--cleanup 으로 지우지 않습니다)" if existed else "없음 (정상)"))
    if existed:
        problems.append(f"{rel} 가 이미 있었습니다. 시험 결과가 섞일 수 있으니 이름을 바꾸거나 지우고 다시 하세요.")

    step_title(3, "결정: 승인 / 거절 / 수정 지시")
    pending = [args.revise] if args.revise else []
    final = None
    for round_no in range(1, MAX_ROUNDS + 1):
        if pending:
            decision = pending.pop(0)
        elif args.decision == "approve":
            decision = "승인"
        elif args.decision == "reject":
            decision = "거절"
        else:
            decision = input("\n결정 입력 ('승인' / '거절' / 수정 지시 문장, 그냥 Enter 는 거절)> ").strip() or "거절"
        print(f"\n[{round_no}회] 입력: {decision}")
        try:
            data, el = ask(decision, sid, args.url)
        except RuntimeError as e:
            print(f"\n[오류] {e}")
            return 1
        print_response(data, el, full=args.full, answer_chars=limit)
        all_trace += data["trace"]
        status = status_of(data["trace"])
        if status != "승인 대기":  # 저장됨 / 거절됨 / 차단 → 끝
            final = status
            break
        print("\n→ 다시 승인 대기입니다. (수정 요청이 반영된 새 미리보기)")

    step_title(4, "결과 점검")
    exists = target.exists()
    if final == "저장됨":
        ok_order = any(t["step"] == "hitl_approved" for t in data["trace"])
        print(f"- 상태 저장됨 / trace 에 hitl_approved 있음: {ok_order} / 파일 {rel} 존재: {exists}")
        if not ok_order:
            problems.append("저장됐는데 trace 에 hitl_approved 가 없습니다.")
        if not exists:
            problems.append(f"저장됐다고 했지만 {rel} 파일이 없습니다. 서버를 이 프로젝트 폴더에서 실행했는지 확인하세요.")
    elif final == "거절됨":
        print(f"- 상태 거절됨 / 파일 {rel} 존재: {exists} (없어야 정상)")
        if exists and not existed:
            problems.append("거절했는데 파일이 만들어졌습니다.")
    else:
        problems.append(f"결정 뒤 최종 상태가 '{final}' 입니다. (저장됨 또는 거절됨이어야 합니다)")
    if saved_before_approval(all_trace):
        problems.append("승인 없는 저장 기록이 있습니다(HITL 위반).")
    print(f"- 승인 없는 저장 기록: {'있음 (위반)' if saved_before_approval(all_trace) else '없음 (정상)'}")
    print("- 전체 trace: " + " → ".join(t["step"] for t in all_trace))

    if args.cleanup and exists and not existed:
        target.unlink()
        print(f"- 시험 파일 삭제: {rel}")
    elif exists and not existed:
        print(f"- 시험 파일이 남아 있습니다: {rel} (지우려면 --cleanup)")

    print("\n" + ("결과: 정상" if not problems else "결과: 문제 있음"))
    for p in problems:
        print(f"  ! {clip(p, 150)}")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
