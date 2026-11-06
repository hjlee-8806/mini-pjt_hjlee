"""가드레일 (SERVICE.md 4장): 입력 차단 → (에이전트) → 출력 마스킹.

LangChain AgentMiddleware 세 개로 에이전트에 결합한다. (agent.py 의 create_agent(middleware=[...]))
- InputGuardMiddleware  : before_agent. 차단이면 모델·도구 호출 없이 안내문으로 바로 끝낸다.
- OutputCheckMiddleware : after_model.  답변 속 Mermaid 도표를 validate_diagram 으로 검증하고, 실패하면 모델에 되돌려
                          수정시킨다(최대 MAX_DIAGRAM_RETRIES 회). 계속 실패하면 '검증 불가 항목'을 답에 덧붙인다. (SERVICE.md 3-6)
- MaskingMiddleware     : after_agent.  답변·도구 결과 메시지의 비밀·개인정보를 가린다.

입력 검사 순서: ① 규칙(인젝션) → ② 규칙(경로·비밀 요청) → ③ 범위 분류(Haiku). 앞에서 걸리면 뒤는 실행하지 않는다.
규칙은 LLM 없이 동작하고, 단어 하나가 아니라 '동작 + 대상' 조합으로 건다. (단어 하나로 걸면 정상 질의가 막힌다)

점검·조정 방법
- 규칙·패턴은 아래 '튜닝 상수' 블록에 모여 있다. 정상 질의가 막히면 해당 규칙의 조합을 좁히고,
  공격이 통과하면 규칙을 추가한다. 둘 다 같은 세트로 다시 확인한다.
- 빠른 점검:  python -c "from src.guardrails import check_input, mask_text; print(check_input('...')); print(mask_text('...'))"
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass

from dotenv import load_dotenv
from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage

from src.settings import TARGET_NAME
from src.tools import parse_project, validate_diagram

load_dotenv()


# ================================================================== 튜닝 상수
MAX_INPUT_CHARS = 5000        # 검사하는 입력 최대 길이 (너무 긴 입력은 앞부분만 본다)
GUARD_MODEL_ID = os.getenv("BEDROCK_GUARD_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
MAX_DIAGRAM_RETRIES = 2       # 도표 검증 실패 시 모델에 되돌려 수정시키는 최대 횟수 (SERVICE.md 3-6)
MIN_METHOD_NAME_LEN = 4       # 범위 분류 단축 경로: 이 길이 이상 + camelCase 인 메서드명만 '코드에 있는 이름'으로 본다

SCOPE_PROMPT = (
    "당신은 질의 분류기입니다. 이 서비스는 Java/Spring '{target}' 소스 코드(컨트롤러·서비스·저장소·모델·"
    "XML 설정·JSP·SQL)의 구조·기능·영향도·도표를 분석합니다.\n"
    "아래 질의가 이 소스 분석과 관련 있으면 IN, 일반 상식·잡담·다른 프로젝트·코드와 무관한 질문이면 OUT 만 출력하세요.\n"
    "소스에 없을 수도 있는 기능(예: 결제, 로그인)을 묻는 것, 범위가 넓거나 모호한 질문, 후속 질문은 IN 입니다.\n"
    "'이 결과/방금 분석한 내용을 문서로 저장·요약·정리해 줘' 같이 이전 분석 결과를 가리키는 후속 요청도 IN 입니다.\n\n"
    "질의: {query}"
)

BLOCK_MESSAGES = {
    "injection": "요청에 시스템 지시를 바꾸거나 내부 정보를 요구하는 내용이 있어 처리할 수 없습니다. "
                 f"분석 대상 소스({TARGET_NAME})의 구조·기능·영향도에 대해 질문해 주세요.",
    "path": f"분석 대상 소스({TARGET_NAME}) 폴더 밖의 파일이나 비밀 정보는 접근할 수 없습니다. "
            "대신 대상 소스의 구조·기능·영향도는 분석해 드릴 수 있습니다.",
    "scope": f"분석 대상 소스({TARGET_NAME})와 관련 없는 질문이라 답할 수 없습니다. "
             "소스의 구조, 업무 기능, 메서드 동작, 변경 영향도를 질문해 주세요.",
}


def _pair(a: str, b: str) -> str:
    """a, b 두 표현이 30자 이내에서 (순서 무관) 함께 나오는 정규식."""
    return rf"(?:{a}).{{0,30}}(?:{b})|(?:{b}).{{0,30}}(?:{a})"


# (규칙 이름, 정규식). 입력은 정규화(소문자·공백 압축) 후 검사한다. 하나라도 맞으면 차단.
INJECTION_RULES: list[tuple[str, str]] = [
    # 지시 덮어쓰기: '무시/잊어' + '이전·기존 지시/규칙'
    ("지시 덮어쓰기", _pair(
        r"무시|잊어|잊고|덮어|ignore|forget|disregard",
        r"(?:이전|기존|앞선|위의|지금까지|모든|previous|prior|above|all).{0,12}"
        r"(?:지시|명령|지침|규칙|프롬프트|instruction|rule|prompt)")),
    # 시스템 프롬프트·내부 지침 노출 요구
    ("내부 정보 노출", _pair(
        r"system prompt|시스템 ?프롬프트|시스템 ?지침|내부 ?지침|your instructions",
        r"출력|알려|보여|공개|말해|reveal|show|print|repeat|tell")),
    # 역할 탈취
    ("역할 탈취", _pair(
        r"지금부터|이제부터|you are now|from now on",
        r"너는|넌 |네 역할|너의 역할|당신은|act as|you are")),
    # 권한 모드 전환
    ("권한 모드 전환", _pair(
        r"관리자 ?모드|admin mode|developer mode|디버그 ?모드|jailbreak",
        r"전환|활성|켜|진입|enable|switch")),
    # 구분자·주석 속 SYSTEM 지시 주입 (평가 케이스 16)
    ("구분자 주입", r"#{3,}|new instruction|<\|im_start\|>|</?system>|(?://|/\*|#|<!--)\s*system\s*:"),
]

PATH_RULES: list[tuple[str, str]] = [
    ("환경파일", r"(?<![\w])\.env(?:\.\w+)?(?![\w])"),                 # .env, .env.local (application.env 는 제외)
    ("상위 경로", r"\.\./|\.\.\\"),
    ("시스템 경로", r"(?:^|[\s\"'`(])(?:/etc/|/proc/|/root/|~/|[a-z]:\\)"),
    ("비밀 파일", r"id_rsa|\.pem(?![\w])|\.ssh/|aws/credentials"),
    # 자격증명 출력 요구. 'password' 는 일부러 뺐다 (설정 위치를 묻는 정상 질의가 막힘)
    ("자격증명 요구", _pair(
        r"자격증명|credential|시크릿|secret ?key|비밀 ?키|access ?key|api ?키|api ?key",
        r"출력|보여|dump|reveal|print|show|그대로")),
]

# 마스킹 패턴. 위에서부터 순서대로 적용한다. (긴 토큰 먼저. 치환어는 어느 패턴에도 다시 걸리지 않는다)
_KV_KEYS = r"[\w.:-]*?(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|token)[\w.-]*"
MASK_PATTERNS: list[tuple[str, str, int]] = [
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----", "[PRIVATE_KEY]", 0),
    (r"eyJ[\w-]+\.[\w-]+\.[\w-]*", "[JWT]", 0),
    (r"(?:AKIA|ASIA)[0-9A-Z]{16}", "[AWS_KEY]", 0),
    (r"(?i)bearer\s+[A-Za-z0-9._~+/-]{20,}", "Bearer [TOKEN]", 0),
    # key=값 / key: 값 / "key": "값" → 값만 가린다. ${...} 자리표시자·이미 가린 값·함수 호출은 제외
    # 주의: 도구 결과는 JSON 문자열(따옴표가 \" 로 이스케이프됨)로 들어온다. 따옴표 앞의 역슬래시를 허용하고 값에서는 역슬래시를
    # 제외해야 마스킹이 JSON 을 깨뜨리지 않는다. (예전에는 역슬래시를 값으로 삼켜 contexts 의 doc_id 파싱이 실패했다)
    (rf"(?i)(\b{_KV_KEYS}(?:\\*[\"'])?\s*[=:]\s*(?:\\*[\"'])?)(?!\$\{{|\[|\s)(?![^\s\"',;\\]*\()([^\s\"',;<>\\]+)", r"\1[SECRET]", 0),
    (r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "[EMAIL]", 0),
    (r"(?<!\d)\d{6}-[1-4]\d{6}(?!\d)", "[RRN]", 0),                                              # 주민등록번호
    (r"(?<!\d)(?:\+82[ -]?\d{1,2}|0\d{1,2})[-. ]\d{3,4}[-. ]\d{4}(?!\d)", "[PHONE]", 0),       # 한국 전화 (구분자 필수)
    (r"(?<!\d)(?:\(\d{3}\)\s?|\d{3}[-. ])\d{3}[-. ]\d{4}(?!\d)", "[PHONE]", 0),                 # 미국식 전화
    (r"(?<![\w.:-])\d{10,11}(?![\w.-])", "[PHONE]", 0),                                         # 구분자 없는 10~11자리 (샘플 데이터)
]


# ================================================================== 입력 검사
@dataclass
class GuardResult:
    """입력 검사 결과. blocked 면 category 와 message 가 채워진다."""
    blocked: bool
    category: str = ""    # injection | path | scope
    reason: str = ""      # 어떤 규칙에 걸렸는지 (trace 용)
    message: str = ""     # 사용자에게 보여줄 안내문


def _normalize(text: str) -> str:
    """전각 통일, 제로폭 문자 제거, 소문자화, 공백 압축, 길이 제한."""
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"[​-‏⁠﻿]", "", text).lower()
    return re.sub(r"\s+", " ", text).strip()[:MAX_INPUT_CHARS]


def _match_rules(text: str, rules: list[tuple[str, str]]) -> str | None:
    """규칙 목록에서 처음 맞는 규칙 이름을 반환한다. 없으면 None."""
    for name, pattern in rules:
        if re.search(pattern, text):
            return name
    return None


_known_names: tuple[set[str], set[str]] | None = None


def _mentions_known_code(original: str) -> bool:
    """질의에 코드에 실제 있는 클래스명 또는 camelCase 메서드명이 있으면 True (범위 분류 LLM 호출 생략)."""
    global _known_names
    if _known_names is None:
        classes = parse_project()
        methods = {m["name"] for c in classes.values() for m in c["methods"]
                   if len(m["name"]) >= MIN_METHOD_NAME_LEN and re.search(r"[a-z][A-Z]", m["name"])}
        _known_names = (set(classes), methods)
    classes, methods = _known_names
    return any(t in classes or t in methods for t in re.findall(r"[A-Za-z_]\w*", original))


def classify_scope(query: str) -> bool:
    """Haiku 로 질의가 소스 분석 범위인지 판별한다. True=범위 안(IN). 애매하거나 호출 실패면 True(fail-open)."""
    try:
        from langchain_aws import ChatBedrockConverse
        llm = ChatBedrockConverse(model=GUARD_MODEL_ID, temperature=0, max_tokens=5)
        out = llm.invoke(SCOPE_PROMPT.format(target=TARGET_NAME, query=query[:1000])).content
        text = out if isinstance(out, str) else "".join(b.get("text", "") for b in out if isinstance(b, dict))
        return not text.strip().upper().startswith("OUT")
    except Exception:
        return True


def check_input(text: str, use_llm: bool = True) -> GuardResult:
    """사용자 입력을 검사한다. 규칙(인젝션 → 경로·비밀) → 범위 분류(Haiku) 순서."""
    norm = _normalize(text)
    if not norm:
        return GuardResult(False)
    rule = _match_rules(norm, INJECTION_RULES)
    if rule:
        return GuardResult(True, "injection", rule, BLOCK_MESSAGES["injection"])
    rule = _match_rules(norm, PATH_RULES)
    if rule:
        return GuardResult(True, "path", rule, BLOCK_MESSAGES["path"])
    if use_llm and not _mentions_known_code(text) and not classify_scope(text):
        return GuardResult(True, "scope", "범위 밖 질문", BLOCK_MESSAGES["scope"])
    return GuardResult(False)


# ================================================================== 출력 마스킹
def mask_text(text: str) -> str:
    """비밀·개인정보를 마스킹한다. 같은 입력에 여러 번 적용해도 결과가 같다."""
    for pattern, repl, flags in MASK_PATTERNS:
        text = re.sub(pattern, repl, text, flags=flags)
    return text


def _mask_content(content):
    """메시지 content(문자열 또는 블록 리스트)의 텍스트를 마스킹한다."""
    if isinstance(content, str):
        return mask_text(content)
    return [{**b, "text": mask_text(b["text"])} if isinstance(b, dict) and isinstance(b.get("text"), str) else b
            for b in content]


# ================================================================== 미들웨어
def _last_user_text(messages) -> str:
    """대화에서 가장 마지막 사용자 메시지 텍스트."""
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            c = m.content
            return c if isinstance(c, str) else "".join(b.get("text", "") for b in c if isinstance(b, dict))
    return ""


class InputGuardMiddleware(AgentMiddleware):
    """에이전트 시작 전 입력을 검사한다. 차단이면 모델·도구를 호출하지 않고 안내문으로 종료한다."""

    @hook_config(can_jump_to=["end"])
    def before_agent(self, state, runtime):
        """차단 시 안내 AIMessage 를 남기고 end 로 점프한다. (additional_kwargs['guard'] 에 사유를 기록)"""
        result = check_input(_last_user_text(state["messages"]))
        if not result.blocked:
            return None
        msg = AIMessage(content=result.message,
                        additional_kwargs={"guard": {"category": result.category, "reason": result.reason}})
        return {"messages": [msg], "jump_to": "end"}


class MaskingMiddleware(AgentMiddleware):
    """에이전트 종료 후 AI·도구 메시지의 비밀·개인정보를 마스킹한다."""

    def after_agent(self, state, runtime):
        """내용이 바뀐 메시지만 같은 id 로 교체한다. (add_messages 가 id 기준으로 덮어쓴다)"""
        updated = []
        for m in state["messages"]:
            if m.type in ("ai", "tool") and m.content:
                new = _mask_content(m.content)
                if new != m.content:
                    updated.append(m.model_copy(update={"content": new}))
        return {"messages": updated} if updated else None


# ================================================================== 도표 검증 미들웨어
_MERMAID_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.S)


def _text_of_content(content) -> str:
    """메시지 content(문자열 또는 블록 리스트)에서 텍스트만 합친다."""
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict))


def check_diagrams(text: str) -> dict | None:
    """답변 텍스트의 Mermaid 블록을 모두 검증한다. 도표가 없으면 None.
    반환: {ok, missing_nodes, wrong_edges, count}"""
    blocks = _MERMAID_RE.findall(text)
    if not blocks:
        return None
    missing, wrong = set(), []
    for b in blocks:
        r = validate_diagram(b)
        missing.update(r["missing_nodes"])
        wrong += [w for w in r["wrong_edges"] if w not in wrong]
    return {"ok": not missing and not wrong, "missing_nodes": sorted(missing),
            "wrong_edges": wrong, "count": len(blocks)}


def _describe(result: dict) -> str:
    """검증 실패 내용을 한 줄로 요약한다."""
    parts = []
    if result["missing_nodes"]:
        parts.append("실제 소스에 없는 노드: " + ", ".join(result["missing_nodes"]))
    if result["wrong_edges"]:
        parts.append("추출된 의존에 없는 화살표: " + ", ".join(f'{w["from"]} → {w["to"]}' for w in result["wrong_edges"]))
    return "; ".join(parts)


_FEEDBACK_KEYS = ("diagram_check", "list_check")   # 검사 미들웨어가 되돌려 보낸 HumanMessage 의 표식


def _retries(messages, key: str) -> int:
    """이번 요청(마지막 실제 사용자 메시지 이후)에서 key 검사가 수정을 요구한 횟수."""
    n = 0
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            if m.additional_kwargs.get(key):
                n += 1
            elif not any(m.additional_kwargs.get(k) for k in _FEEDBACK_KEYS):
                break
    return n


def _diagram_retries(messages) -> int:
    """이번 요청에서 도표 수정을 요구한 횟수."""
    return _retries(messages, "diagram_check")


# ---------------------------------------------------------------- 메서드 목록 완전성 검사
# 프롬프트의 "메서드가 15개 이하이면 전부 나열" 규칙을 코드로 보장한다. 모델은 질문과 관련 있어 보이는 것만 추려 쓰려는
# 경향이 있어, 프롬프트 지시만으로는 문구에 따라 지켜지다 말다 한다. (TASK.md 9-3 케이스 10 분석)
MAX_LIST_RETRIES = 1          # 누락 목록을 알려 주고 다시 쓰게 하는 최대 횟수. 그래도 빠지면 답 끝에 누락 목록을 덧붙인다
MAX_LISTED_METHODS = 15       # 이 개수 이하일 때만 '전부 나열'을 요구한다 (SYSTEM_PROMPT 와 같은 값)
MAX_LISTED_CLASSES = 2        # list_structure 결과가 이 개수 이하의 클래스일 때만 검사 (인터페이스+구현체 정도). 계층 전체 조회는 제외
_NOT_FOUND_RE = re.compile(r"존재하지\s*않|찾을\s*수\s*없")   # '없는 대상을 물은 질문'에 대한 답에만 적용한다


def _turn_tool_outputs(messages, tool_name: str):
    """이번 요청(마지막 실제 사용자 메시지 이후)의 tool_name 도구 결과를 JSON 으로 읽어 돌려준다."""
    out = []
    for m in reversed(messages):
        if isinstance(m, HumanMessage) and not any(m.additional_kwargs.get(k) for k in _FEEDBACK_KEYS):
            break
        if getattr(m, "type", "") == "tool" and getattr(m, "name", "") == tool_name:
            try:
                out.append(json.loads(_text_of_content(m.content)))
            except ValueError:
                continue
    return out


def _turn_read_java_paths(messages) -> list[str]:
    """이번 요청에서 모델이 read_file 로 읽은 자바 파일 경로."""
    paths = []
    for m in reversed(messages):
        if isinstance(m, HumanMessage) and not any(m.additional_kwargs.get(k) for k in _FEEDBACK_KEYS):
            break
        for call in getattr(m, "tool_calls", None) or []:
            p = str((call.get("args") or {}).get("path", "")).replace("\\", "/")
            if call.get("name") == "read_file" and p.endswith(".java"):
                paths.append(p)
    return paths


def _examined_classes(messages) -> list[list[dict]]:
    """모델이 이번 요청에서 직접 확인한 클래스 묶음. list_structure 결과(1~MAX_LISTED_CLASSES 개짜리) 하나가 한 묶음이고,
    read_file 로 읽은 자바 파일의 클래스는 파일마다 한 묶음이다. (모델이 list_structure 가 빈 결과일 때 파일을 직접 읽는 경로가 있다)"""
    groups = [d for d in _turn_tool_outputs(messages, "list_structure")
              if isinstance(d, list) and 1 <= len(d) <= MAX_LISTED_CLASSES]
    paths = _turn_read_java_paths(messages)
    if paths:
        by_file: dict[str, list[dict]] = {}
        for c in parse_project().values():
            by_file.setdefault(c["file"], []).append(c)
        for p in dict.fromkeys(paths):
            hit = next((cs for f, cs in by_file.items() if p == f or p.endswith("/" + f) or f.endswith("/" + p)), None)
            if hit:
                groups.append(hit)
    return groups


def check_method_listing(messages, answer: str) -> dict | None:
    """'존재하지 않음' 답변이 모델이 확인한 클래스(list_structure 결과 또는 read_file 로 읽은 자바 파일)의 메서드를
    빠뜨렸는지 검사한다. 답변에 이름이 나온 클래스의 메서드가 MAX_LISTED_METHODS 개 이하인데 답에 없는 이름이 있으면
    {이름: 파일:라인} 을 돌려준다. 해당 없으면 None."""
    if not _NOT_FOUND_RE.search(answer):
        return None
    mentioned = lambda name: bool(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", answer))  # noqa: E731
    missing: dict[str, str] = {}
    for group in _examined_classes(messages):
        methods: dict[str, str] = {}
        for c in group:
            if isinstance(c, dict) and mentioned(str(c.get("name"))):
                for mt in c.get("methods", []):
                    methods.setdefault(mt["name"], f'{c["file"].rsplit("/", 1)[-1]}:{mt["line"]}')
        if not methods or len(methods) > MAX_LISTED_METHODS:
            continue
        missing.update({n: loc for n, loc in methods.items() if not mentioned(n)})
    return missing or None


class OutputCheckMiddleware(AgentMiddleware):
    """모델이 도구 호출 없이 최종 답을 낼 때, 답 속 Mermaid 도표와 메서드 목록 완전성을 코드로 검증한다."""

    def _check_listing(self, state, last):
        """도표가 없는 답에 대한 메서드 목록 검사. 누락이 있으면 한 번 되돌리고, 그래도 빠지면 누락 목록을 덧붙인다."""
        text = _text_of_content(last.content)
        missing = check_method_listing(state["messages"][:-1], text)
        if not missing:
            return None
        desc = ", ".join(f"{n}({loc})" for n, loc in missing.items())
        if _retries(state["messages"][:-1], "list_check") < MAX_LIST_RETRIES:
            feedback = HumanMessage(
                content=f"[목록 완전성 검사] list_structure 로 확인한 메서드 중 답변에 없는 것: {desc}. "
                        f"메서드가 {MAX_LISTED_METHODS}개 이하이므로 질문과 관련 없어 보여도 빠짐없이 전부 파일:라인과 함께 나열해 다시 답하세요.",
                additional_kwargs={"list_check": {"ok": False, "missing": sorted(missing)}})
            return {"messages": [feedback], "jump_to": "model"}
        kw = {**last.additional_kwargs, "list_check": {"ok": False, "missing": sorted(missing), "exhausted": True}}
        note = f"\n\n> ⚠ 답변에서 빠진 메서드 (list_structure 결과): {desc}"
        return {"messages": [last.model_copy(update={"content": text + note, "additional_kwargs": kw})]}

    @hook_config(can_jump_to=["model"])
    def after_model(self, state, runtime):
        """통과: 검증 결과를 메시지에 기록. 실패: 수정 요구 후 model 로 점프(최대 MAX_DIAGRAM_RETRIES 회).
        한도 초과: 답 끝에 '검증 불가 항목'을 덧붙인다. (같은 id 로 교체하므로 add_messages 가 덮어쓴다)
        도표가 없는 답에는 메서드 목록 완전성 검사를 한다."""
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or last.tool_calls:
            return None
        result = check_diagrams(_text_of_content(last.content))
        if result is None:
            return self._check_listing(state, last)
        if result["ok"]:
            kw = {**last.additional_kwargs, "diagram_check": {"ok": True, "count": result["count"]}}
            return {"messages": [last.model_copy(update={"additional_kwargs": kw})]}
        retries = _diagram_retries(state["messages"][:-1])
        desc = _describe(result)
        if retries < MAX_DIAGRAM_RETRIES:
            feedback = HumanMessage(
                content=f"[도표 검증 실패 {retries + 1}/{MAX_DIAGRAM_RETRIES}] {desc}. "
                        "build_dependency_graph 결과의 노드·화살표만 사용해 도표를 수정해 다시 답하세요.",
                additional_kwargs={"diagram_check": {"ok": False, "detail": desc}})
            return {"messages": [feedback], "jump_to": "model"}
        note = f"\n\n> ⚠ 검증 불가 항목 (도표 검증 {MAX_DIAGRAM_RETRIES}회 재시도 후에도 불일치): {desc}"
        kw = {**last.additional_kwargs, "diagram_check": {"ok": False, "detail": desc, "exhausted": True}}
        return {"messages": [last.model_copy(update={"content": _text_of_content(last.content) + note,
                                                     "additional_kwargs": kw})]}
