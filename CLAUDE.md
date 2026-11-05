# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.


## 문서 역할 (중복해서 쓰지 말고 한 곳에만 쓴다)

| 문서 | 담는 내용 |
|---|---|
| `README.md` | 과제 제출용 요약. 패턴 적용 표(상태·코드 위치 포함), 아키텍처, 실행 방법, RAGAS 결과, 통과율, 회고 |
| `SERVICE.md` | 서비스 설계 명세. 도구, 에이전트 흐름, 정책, 성공 기준, `POST /query` 스펙, 미적용 패턴 사유 |
| `CLAUDE.md` | 이 파일. 작업 규칙, 명령어, 함정 |

- 설계가 바뀌면 SERVICE.md 를 먼저 고친다.
- **README.md 는 사용자가 요청할 때만, 실제 구현된 내용을 확인한 뒤 갱신한다.** 임의로 상태(✅/🔧)나 라인 번호를 고치지 않는다.
- `CLAUDE.md`·`TASK.md`·`.gitignore` 와 분석 대상 소스 `data/petclinic-legacy/` 도 커밋 대상이다. (`.env`·`_backup.env`·`data/index/`·`logs/` 만 제외) 그래서 이 문서들에는 키·계정 ID·IAM 사용자 이름 같은 비밀·식별 정보를 적지 않는다.

## Status

Korean-language "mini PJT": Java/Spring 소스 분석 에이전트(RAG + ReAct)와 자체 평가 세트를 만든다. 분석 대상은 `data/petclinic-legacy/`.

- 구현됨 (TASK.md 묶음 1·2): `src/tools.py` (검색을 제외한 도구 7종 + 계층 분류), `src/agent.py` (ReAct 에이전트, 도구 8종 결합(`search_code` + tools.py 6종 + `save_doc` 래퍼), `{answer, contexts, trace}` 모델, `run_query`), `src/api.py` (`POST /query`), `src/retriever.py` (청킹·하이브리드 검색·질의 재작성·`search_code`), `src/embeddings.py` (Titan 임베딩 + 캐시), `main.py` (Bedrock 연결 확인), `evaluation/test_queries.csv` (17건).
- 구현됨 (묶음 3): `src/guardrails.py` — `InputGuardMiddleware`(규칙 + Haiku 범위 분류), `MaskingMiddleware`(비밀·개인정보 마스킹). `agent.py` 의 `create_agent(middleware=[...])` 로 결합. 규칙·패턴은 파일 상단 "튜닝 상수" 블록.
- 구현됨 (묶음 4): HITL. `agent.py` 의 `save_doc` 래퍼(`approved` 숨김) + 내장 `HumanInTheLoopMiddleware` + `InMemorySaver`. `run_query(question, session_id)` 가 승인 대기 세션의 다음 입력을 승인/거절/수정 요청으로 해석하고, 승인 재개 동안에만 `_APPROVED` ContextVar 를 켠다. 수정 요청은 최대 3회. `POST /query` 요청에 선택 필드 `session_id` 가 있다(응답은 3키 그대로). 체크포인터는 메모리라 서버 재시작 시 대기 상태가 사라진다.
- 구현됨 (묶음 5): `OutputCheckMiddleware`(`guardrails.py`) — 답 속 Mermaid 를 `validate_diagram` 으로 코드 검증하고 실패 시 모델에 되돌려 수정(최대 2회), 한도 초과 시 "검증 불가 항목" 명시. `validate_diagram` 파서는 라벨 표기·체인 엣지를 처리하고 계층 도표에 `DB` 노드가 있다. `find_usages` 는 snake_case·getter/setter 변형도 찾고 주석 줄에 `is_comment` 를 붙인다. `SummarizationMiddleware`(Haiku)로 대화 이력 요약(`agent.py` 상단 `SUMMARY_*` 상수, 기본 30개 초과 시 요약·10개 유지). 질의 유형별 흐름은 `SYSTEM_PROMPT` 에 있다.
- 구현됨 (묶음 6): `src/tracing.py` — 요청마다 `logs/trace.jsonl` 에 한 줄 기록(상태·지연·토큰·마스킹된 trace). `run_query` 는 로깅 래퍼이고 본체는 `_run_query`. 응답 trace 는 결정 단계(`hitl_*`)를 해당 `save_doc` 직전에 넣고, 실행되지 않은 `save_doc` 은 output 앞에 "미실행:" 을 붙인다. 승인 여부는 `{step, input, output}` 형식을 유지한 채 단계 이름으로 표현한다. LangFuse(6-4)는 키가 없어 건너뜀.
- 구현됨 (묶음 7 중 7-1·7-2): 평가 러너 `evaluation/run_eval.py`, 1차 실행(15/17, 88%)과 `evaluation/round1_report.md`. 1차 실패는 #8·#10 (negative, 프롬프트 원인).
- 구현됨 (7-3a): `evaluation/ragas_reference.json` — RAGAS·검색 평가용 정답 목록(8건, 정답 위치 51곳 + decoy 3곳, 앵커로 라인 검증). 스키마: `reference`(정답 설명), `reference_locations`(`path`·`start`·`end`·`anchor`), `decoy_locations`. 소스가 바뀌면 앵커 검사로 어긋난 라인을 찾는다.
- 구현됨 (7-3b 코드): `run_eval.py` 의 `retrieval_metrics`(검색 recall·근거 recall·검색 precision·decoy 적중, 위치 겹침 기준)와 `--retrieval <TAG>` 명령. 라운드 실행에도 정답 케이스(8건)에 자동 포함. 평가 중에는 `agent.MAX_TEXT` 를 `EVAL_MAX_TEXT`(20000)로 올려 긴 출력의 위치를 끝까지 본다. 실측 완료(서울 리전, 근거 recall 80%, 결과 `results/retrieval1-seoul.json`, 해석은 `round1_report.md` 5-1절). 평가 결과 JSON 에 `region` 이 기록된다.
- 구현됨 (7-3c): `python -m evaluation.run_eval --ragas <TAG> [--reuse]` — 정답 8케이스를 에이전트로 실행해 샘플을 저장하고 RAGAS 4지표를 계산한다(`--reuse` 는 저장된 샘플로 지표만 재계산, 에이전트 호출 없음). 채점 LLM 은 `BEDROCK_RAGAS_MODEL_ID`(기본 `us.…haiku-4-5`, 서울에서는 `global.…haiku-4-5`), 임베딩은 Titan v2. 1차 결과: recall 0.90, precision 0.75, faithfulness 0.87, relevancy 0.53 (`results/ragas1.json`). RAGAS 컨텍스트에는 `doc_id` 머리말을 붙인다(없으면 `파일:라인` 인용이 확인되지 않아 faithfulness 가 부당하게 깎인다).
  - **함정:** `ragas 0.4.3` 은 `langchain-community 0.4.x` 에 없는 `langchain_community.chat_models.vertexai` 를 import 해서 그대로는 실패한다. `run_eval.py` 의 `_shim_langchain_community()` 가 빈 대체 모듈을 끼워 우회한다(설치 환경은 바꾸지 않는다). ragas 의 기존 지표 클래스는 deprecation 경고를 낸다.
- 7-4 진행: 케이스 10 프롬프트 보완(부분 개선), 케이스 6 프롬프트 보완(5회 비교로 확인: 근거 recall 50%→100%, 판정 충족률 0.75→1.00), 마스킹 JSON 깨짐 버그 수정. 중간 점검 전체 실행(태그 `6fix`) 17/17, 1차 대비 fixed [8, 10], regressed 없음. 기록은 TASK.md 7-4 진행 기록.
- 7-4 케이스 2: 위치 지표에 `evidence_recall_with_structure`(= 기존 근거 recall + `list_structure` 클래스 선언 위치)를 **별도 지표로 추가**했다(기존 `evidence_recall` 정의는 1차와 비교하려고 유지). 케이스 6 프롬프트가 케이스 2 근거 recall 도 올렸다(5회 A/B: 60%→94%, 구조 포함 74%→100%). `run_case` 는 안에서 `agent.MAX_TEXT=EVAL_MAX_TEXT` 를 설정한다(단독 호출 시 trace 출력이 2000자에서 잘려 위치를 놓치던 결함).
- 구현됨 (7-5): 정식 2차 실행·`evaluation/round2_report.md`. 2차 17/17 (1차 15/17), fixed [8, 10], regressed 없음, RAGAS recall 1.00·faithfulness 0.87, 근거 recall 97~99%. 결과 `results/round2.json`·`retrieval2.json`·`ragas2.json`.
- 미구현: README 의 남은 항목 없음. 남은 과제(케이스 12 precision 등)는 `round2_report.md` 7절. 계획은 TASK.md.
- 테스트, 린터, 빌드 설정은 없다. git 커밋은 아직 없다.


## Commands

```
venv\Scripts\activate
uvicorn src.api:app --reload                 # POST /query 서버 (프로젝트 루트에서 실행)
python -m src.retriever "질의"               # 검색 점검 (--chunks, --rebuild, --no-rewrite)
python scripts/try_query.py "질문"           # 수동 테스트: 질문 → 답변·trace 출력 (--samples 대표 6개, 인자 없으면 대화형). 서버가 떠 있어야 한다
python scripts/try_approval.py               # 수동 테스트: 문서 저장 승인 흐름 (--decision approve|reject --cleanup 으로 자동 점검)
python -m src.tracing --check                # trace 로그 요약 + HITL 준수 검사(승인 없는 저장 0건, 위반 시 exit 1). --tail N 으로 최근 N건
```
- 테스트로 로그를 남기고 싶지 않으면 `TRACE_LOG_PATH` 로 다른 파일을 지정한다. 테스트 중 만든 `output/` 파일은 직접 지운다.
- Windows Git Bash 의 `curl` 로 한글 JSON 을 보내면 400 이 날 수 있다. UTF-8 파일(`--data-binary @body.json`)이나 Python 으로 보낸다. Python 출력이 깨지면 `PYTHONUTF8=1`.

## Architecture

- `main.py`: `boto3` Bedrock `converse` 호출 데모. 모델은 `BEDROCK_MODEL_ID` 환경변수(기본 `us.anthropic.claude-sonnet-4-6`. 4.5 는 일일 토큰 한도 소진으로 4.6 으로 교체).
- `src/tools.py`: 도구 구현. 함수 위치는 README.md 의 "활용한 패턴" 표를 참고한다. (라인 번호는 코드 수정 시 어긋날 수 있으니 grep 으로 확인)
  - 모든 경로는 `_safe_path()` 로 `TARGET_ROOT` 안으로 제한한다. 이 검사를 우회하는 코드를 추가하지 않는다.
  - `save_doc` 은 HITL 승인(`approved=True`) 없이는 쓰지 않으며 `output/` 에만 쓴다.
  - 도표는 LLM 이 아니라 코드 파싱으로 만든다. (SERVICE.md 3-6)
- `src/agent.py`: `create_agent` 로 만든 에이전트. `tools.py` 함수를 `langchain_core.tools.tool` 로 감싸 쓰므로 `tools.py` 에 `@tool` 을 붙이지 않는다. 모델은 `BEDROCK_MODEL_ID`.
- `src/api.py`: FastAPI 앱. `run_query` 호출만 한다.
- `src/guardrails.py`: 미들웨어 3종. `InputGuardMiddleware`(인젝션·경로 규칙 + Haiku 범위 분류), `OutputCheckMiddleware`(Mermaid 검증·재시도), `MaskingMiddleware`(비밀·개인정보 마스킹). `agent.py` 의 `build_agent` 에서 `[InputGuard, Summarization, OutputCheck, HITL, Masking]` 순서로 결합한다. 규칙·패턴은 파일 상단 "튜닝 상수" 블록.
- `src/tracing.py`: trace 로그 기록(`make_record`·`write_trace`)과 검사(`hitl_violations`·`unaccounted_outputs`·`summarize`). `agent.py` 를 import 하지 않는다(순환 방지). 쓰기 실패는 응답을 막지 않는다.
- `src/embeddings.py`: Bedrock Titan 임베딩 호출과 해시 캐시(`data/index/embed_cache.json`). 모델은 `BEDROCK_EMBED_MODEL_ID`(기본 `amazon.titan-embed-text-v2:0`).
- `src/retriever.py`: RAG. 자바는 `tools.parse_project()` 의 클래스·메서드 경계로 청킹하고, FAISS(벡터) + BM25 를 RRF 로 합친다. 검색 결과가 약하면 `BEDROCK_REWRITE_MODEL_ID`(기본 Haiku 4.5)로 질의를 영어 식별자로 바꿔 재검색한다. 조정값은 파일 상단 "튜닝 상수" 블록에 있다.
  - 인덱스(`data/index/`)는 청크·임베딩 설정의 해시가 바뀌면 자동 재생성된다. 수동 재생성은 폴더 삭제 또는 `--rebuild`.
  - 점검: `python -m src.retriever --chunks` (청크 통계), `python -m src.retriever "질의"` (벡터·BM25 순위와 점수).

## 평가 (`evaluation/`)

- `test_queries.csv` 스키마: `id,category,input,expected_traits,forbidden,expected_tools,note`. `expected_traits`, `forbidden` 은 세미콜론 구분.
- 러너: `evaluation/run_eval.py` (7-1 구현됨). 규칙(케이스 1·5·7·15·16·17) + LLM judge(나머지, Sonnet 4.6) 혼합 판정. 조정 지점은 파일 상단 `★` 주석 (`TRAIT_PASS_RATIO` 기본 0.70 등). 결과는 `evaluation/results/round<N>.json`, 라운드별 trace 로그는 `logs/eval_round<N>.jsonl`.
  - `python -m evaluation.run_eval --round 1` (전체) / `--only 8 10` / `--failed` / `--show 1` / `--compare 1 2`. 전체 17건에 약 5~10분.
  - 영향도(케이스 7) 정답 `IMPACT_TRUTH` 는 파일 이름 10개(`ragas_reference.json` 케이스 7 과 같음)이고 단어 경계로 매칭한다. 한쪽을 고치면 다른 쪽도 고친다.
  - 17번은 같은 세션에서 앞선 분석 질문을 먼저 보낸다(`SETUP_QUESTIONS`). 테스트용 라운드 결과는 직접 지운다.
- 흐름: 1차 실행 → `round1_report.md` → 개선 → 2차 실행 → `round2_report.md`. 케이스 구성과 목표 수치는 SERVICE.md 5절.

## Bedrock 사용가능 모델
[us-east-1]
global.anthropic.claude-sonnet-4-6
us.anthropic.claude-sonnet-4-6
us.anthropic.claude-sonnet-4-5-20250929-v1:0
global.anthropic.claude-sonnet-4-5-20250929-v1:0
us.anthropic.claude-haiku-4-5-20251001-v1:0
global.anthropic.claude-haiku-4-5-20251001-v1:0
us.amazon.nova-pro-v1:0
us.amazon.nova-lite-v1:0
us.amazon.nova-micro-v1:0
us.amazon.nova-2-lite-v1:0
openai.gpt-oss-120b-1:0
openai.gpt-oss-20b-1:0

[us-west-2]
global.anthropic.claude-sonnet-4-6
us.anthropic.claude-sonnet-4-6
us.anthropic.claude-sonnet-4-5-20250929-v1:0
global.anthropic.claude-sonnet-4-5-20250929-v1:0
us.anthropic.claude-haiku-4-5-20251001-v1:0
global.anthropic.claude-haiku-4-5-20251001-v1:0
us.amazon.nova-pro-v1:0
us.amazon.nova-lite-v1:0
us.amazon.nova-micro-v1:0
us.amazon.nova-2-lite-v1:0
global.amazon.nova-2-lite-v1:0

[ap-northeast-2]
global.anthropic.claude-sonnet-4-6
global.anthropic.claude-sonnet-4-5-20250929-v1:0
global.anthropic.claude-haiku-4-5-20251001-v1:0
apac.amazon.nova-pro-v1:0
apac.amazon.nova-lite-v1:0
apac.amazon.nova-micro-v1:0
apac.amazon.nova-2-lite-v1:0


**한도(Throttling) 우회:** 일일 토큰 한도는 리전·프로필별이다. 기본 리전 `us-east-1` 이 막히면 환경변수로 `ap-northeast-2` 를 쓴다. 서울에는 `us.` 프로필이 없으므로 모든 모델 ID 를 `global.` 로 바꿔야 한다.
```
export AWS_DEFAULT_REGION=ap-northeast-2
export BEDROCK_MODEL_ID=global.anthropic.claude-sonnet-4-6
export BEDROCK_REWRITE_MODEL_ID=global.anthropic.claude-haiku-4-5-20251001-v1:0
export BEDROCK_GUARD_MODEL_ID=global.anthropic.claude-haiku-4-5-20251001-v1:0
export BEDROCK_SUMMARY_MODEL_ID=global.anthropic.claude-haiku-4-5-20251001-v1:0
export BEDROCK_RAGAS_MODEL_ID=global.anthropic.claude-haiku-4-5-20251001-v1:0   # --ragas 채점용
export BEDROCK_JUDGE_MODEL_ID=global.anthropic.claude-sonnet-4-6                  # --round 의 LLM judge (빠뜨리면 서울에서 ValidationException: 모델 ID 무효)
```
셸 환경변수가 `.env` 보다 우선한다. Haiku 변수를 빼먹으면 `us.` 프로필이 서울에서 실패하는데, 재작성·범위 분류는 실패해도 조용히 넘어가므로(재작성 생략, 분류 통과) 검색·가드 결과가 왜곡된다. 평가 결과 JSON 에는 `region` 이 기록된다. 서울에서 Titan 임베딩 동작 확인(1024차원). 2026-11-05 기준 `us-west-2` 는 들쭉날쭉 막혔다.

**계정 교체(2026-11-05):** 기존 `.env` 는 `_backup.env`(gitignore)로 옮기고 새 계정을 썼다. 새 계정은 `us-east-1` 에서 `us.`·`global.` Sonnet 4.6 과 Haiku 4.5 가 동작한다(서울 우회 불필요). 처음에는 IAM 사용자에 Titan 임베딩 `bedrock:InvokeModel` 권한이 없어 `AccessDeniedException` 이었으나 이후 부여되어 Titan v2 가 동작한다(Cohere 임베딩은 여전히 거부). 벡터 검색은 질의를 임베딩해야 하므로 이 권한이 없으면 `search_code` 가 새 질의에서 실패한다.
**XML 청킹:** `retriever.py` 의 `XML_STRUCTURED` 는 기본 `False`(60줄 고정 창)다. `True` 면 `.xml` 을 루트 자식 요소 단위로 청킹하는데(청크 270→288), 같은 임베딩 캐시로 A/B 한 결과 recall 이 오르지 않아(top_k=5 58%→58%, top_k=10 74%→68%) 채택하지 않았다. 케이스 6 의 원인은 청크 크기가 아니라 에이전트가 메서드 이름과 'cache' 를 한 질의에 섞는 것이었다(프롬프트로 해결). 바꾸면 인덱스가 자동 재생성된다.
**함정(마스킹):** 도구 결과는 JSON 문자열로 `MaskingMiddleware` 를 통과하므로 비밀값 정규식(`MASK_PATTERNS`)이 따옴표 이스케이프(`\"`)의 역슬래시를 삼키면 JSON 이 깨져 `contexts` 의 `doc_id` 파싱이 조용히 실패한다(오류 없이 `search_code#N` 으로 뭉침). 정규식을 고칠 때는 모든 청크를 `json.dumps` → `mask_text` → `json.loads` 로 왕복 검사한다.

임베딩: `amazon.titan-embed-text-v2:0` (이전 계정에서 사용 가능, 1024차원). `cohere.embed-multilingual-v3` 는 AccessDenied.

## 구조 (`src/`, `data/`, `evaluation/` 배치는 제출 규약이라 바꾸지 않는다)
```
mini-pjt_hjlee/
├── src/                     # agent.py 메인 에이전트 그래프, tools.py 도메인 도구, retriever.py RAG 파이프라인 (제출 규약 3종)
│                            # + api.py (POST /query), embeddings.py (임베딩), guardrails.py (가드레일), tracing.py (trace 로그)
├── scripts/                 # 수동 테스트: try_query.py(일반 질의), try_approval.py(문서 승인 흐름), _client.py(공용 HTTP·출력)
├── logs/                    # trace.jsonl (생성물, git 제외)
├── data/petclinic-legacy/   # 사용한 문서와 데이터. 분석 대상 소스 (읽기 전용, 커밋 대상. 빌드 산출물 target/ 은 폴더 자체 .gitignore 가 제외)
├── data/index/              # RAG 인덱스·임베딩 캐시 (생성물, git 제외)
├── evaluation/              # test_queries.csv, round1_report.md, round2_report.md
├── output/                  # save_doc 결과 (승인 후에만 생성)
├── README.md  SERVICE.md  CLAUDE.md
├── main.py  requirements.txt
```

## 기술 스택
- Python, LangChain, LangGraph
- 모델은 Amazon Bedrock (ChatBedrockConverse)
- Agent 생성은 가능하면 langchain.agents 의 create_agent, LangGraph 기반 agent 생성을 쓴다

## 주고받는 형식 (제출 규약)
- `POST /query` 로 받고 `question` 필드를 읽는다
- 답은 `answer`, `contexts`, `trace` 세 키로 돌려준다 (상세 스펙은 SERVICE.md)

## 코드 규칙
- 파일 하나에 한 가지 역할만 둔다
- 함수와 도구에는 한국어 docstring 을 쓴다
- 비밀 값은 `.env` 에서 읽고 코드에 적지 않는다. `.env` 의 AWS 자격증명은 절대 커밋하거나 출력하지 않는다. (`.gitignore` 에 등록됨)

## 하지 말 것
- 요청하지 않은 파일을 새로 만들지 않는다
- 기존 파일을 통째로 다시 쓰지 않는다. 바뀐 부분만 고친다