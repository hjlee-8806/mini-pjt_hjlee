"""pytest 공용 설정: 프로젝트 루트와 scripts/ 를 import 경로에 넣는다.

모든 테스트는 Bedrock·네트워크를 쓰지 않는다. (LLM 호출이 필요한 부분은 use_llm=False 나 가짜 객체로 대체)
실행: 프로젝트 루트에서  python -m pytest
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
