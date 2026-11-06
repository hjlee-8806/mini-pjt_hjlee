# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.


## 문서 역할 (중복해서 쓰지 말고 한 곳에만 쓴다)

| 문서 | 담는 내용 |
|---|---|
| `README.md` | 과제 제출용 요약. 패턴 적용 표(상태·코드 위치 포함), 아키텍처, 실행 방법, RAGAS 결과, 통과율, 회고 |
| `SERVICE.md` | 서비스 설계 명세. 도구, 에이전트 흐름, 정책, 성공 기준, `POST /query` 스펙, 미적용 패턴 사유 |
| `TASK.md` | 구현 계획(묶음별 작업·확인 방법)과 진행 기록 |
| `evaluation/round1_report.md`, `round2_report.md` | 평가 결과 리포트 (수치의 원본) |
| `CLAUDE.md` | 이 파일. 작업 규칙, 명령어, 함정 |

- 설계가 바뀌면 SERVICE.md 를 먼저 고친다.
- **README.md 는 사용자가 요청할 때만, 실제 구현된 내용을 확인한 뒤 갱신한다.** 임의로 상태(✅/🔧)나 라인 번호를 고치지 않는다.
- `CLAUDE.md`·`TASK.md`·`.gitignore` 와 분석 대상 소스 `data/petclinic-legacy/` 도 커밋 대상이다. (`.env`·`_backup.env`·`data/index/`·`logs/` 만 제외) 그래서 이 문서들에는 키·계정 ID·IAM 사용자 이름 같은 비밀·식별 정보를 적지 않는다.

## 현재 상태

Korean-language "mini PJT": Java/Spring 소스 분석 에이전트(RAG + ReAct)와 자체 평가 세트. 분석 대상은 `data/petclinic-legacy/`.

- **구현·평가 완료** (TASK.md 묶음 1~7): 에이전트, 도구 8종, 하이브리드 검색, 가드레일, HITL, 도표 검증, trace 로그, 평가 러너, 1·2차 평가와 리포트.
- 평가 수치는 README.md 와 `evaluation/round*_report.md` 에만 적는다. 여기에 복사하면 낡는다.
- 남은 과제는 `evaluation/round2_report.md` 7절. 단위 테스트(`tests/`)가 있고 린터·빌드 설정은 없다.

## Commands

```
venv\Scripts\activate
uvicorn src.api:app --reload                 # POST /query 서버 (프로젝트 루트에서 실행)
python -m src.settings                       # 분석 대상 설정 점검: 자동 감지한 소스 루트·기본 패키지·XML. TARGET_ROOT=다른 폴더 로 다른 프로젝트 확인
python -m src.retriever "질의"               # 검색 점검 (--chunks, --rebuild, --no-rewrite)
python -m pytest                             # 단위 테스트 (Bedrock·네트워크 불필요)
python scripts/try_query.py "질문"           # 수동 테스트: 질문 → 답변·trace 출력 (--samples 대표 6개, 인자 없으면 대화형). 서버가 떠 있어야 한다
python scripts/try_approval.py               # 수동 테스트: 문서 저장 승인 흐름 (--decision approve|reject --cleanup 으로 자동 점검)
python -m src.tracing --check                # trace 로그 요약 + HITL 준수 검사(승인 없는 저장 0건, 위반 시 exit 1). --tail N 으로 최근 N건
```
- 테스트로 로그를 남기고 싶지 않으면 `TRACE_LOG_PATH` 로 다른 파일을 지정한다. 테스트 중 만든 `output/` 파일은 직접 지운다.
- Windows Git Bash 의 `curl` 로 한글 JSON 을 보내면 400 이 날 수 있다. UTF-8 파일(`--data-binary @body.json`)이나 Python 으로 보낸다. Python 출력이 깨지면 `PYTHONUTF8=1`.

## Architecture

- `main.py`: Bedrock `converse` 연결 확인용 데모. README 에서는 안내하지 않는다.
- `src/settings.py`: 분석 대상 설정의 단일 출처. 환경변수 `TARGET_ROOT`(기본 `data/petclinic-legacy`)로 대상을 바꾸고(한 서버 = 한 대상), 자바 소스 루트·기본 패키지·인덱스 대상·Spring XML 을 **package 선언과 폴더 경로로 자동 감지**한다(`SOURCE_ROOTS`·`BASE_PKG`·`TARGET_NAME` 으로 덮어쓰기). 자기 프로젝트이거나 상위 폴더, 자바 소스가 없는 폴더는 `validate_target()` 이 거부한다(`api.py` 가 시작 시 호출). 인덱스는 대상별 폴더(`data/index/<이름>-<경로해시>/`)에 두고 임베딩 캐시(`data/index/embed_cache.json`)만 공유한다.
- `src/tools.py`: 도구 구현. 도구 입출력은 SERVICE.md 3-2, 함수 위치는 README.md 의 "활용한 패턴" 표를 참고한다. (라인 번호는 코드 수정 시 어긋나니 grep 으로 확인)
  - 모든 경로는 `_safe_path()` 로 `TARGET_ROOT` 안으로 제한한다. 이 검사를 우회하는 코드를 추가하지 않는다.
  - `save_doc` 은 HITL 승인(`approved=True`) 없이는 쓰지 않으며 `output/` 에만 쓴다.
  - 도표는 LLM 이 아니라 코드 파싱으로 만든다. (SERVICE.md 3-6)
- `src/agent.py`: `create_agent` 로 만든 에이전트. `tools.py` 함수를 `langchain_core.tools.tool` 로 감싸 쓰므로 `tools.py` 에 `@tool` 을 붙이지 않는다. 모델은 `BEDROCK_MODEL_ID`(기본 `us.anthropic.claude-sonnet-4-6`). 질의 유형별 도구 흐름은 `SYSTEM_PROMPT` 에 있다.
  - HITL: `save_doc` 래퍼(`approved` 인자 숨김) + `HumanInTheLoopMiddleware` + `InMemorySaver`. `run_query(question, session_id)` 가 승인 대기 세션의 다음 입력을 승인/거절/수정 요청으로 해석하고, 승인 재개 동안에만 `_APPROVED` ContextVar 를 켠다. 수정 요청은 최대 3회, 체크포인터가 메모리라 서버를 재시작하면 대기 상태가 사라진다.
  - `run_query` 는 trace 를 로그에 남기는 래퍼이고 본체는 `_run_query` 다. 대화 요약은 `SUMMARY_*` 상수(기본 30개 초과 시 요약, 10개 유지).
- `src/api.py`: FastAPI 앱. 요청 `{question, session_id(선택)}` 를 `run_query` 에 넘기기만 한다. 응답은 `answer`·`contexts`·`trace` 세 키.
- `src/guardrails.py`: 미들웨어 3종. `InputGuardMiddleware`(인젝션·경로 규칙 + Haiku 범위 분류), `OutputCheckMiddleware`(Mermaid 검증·재시도, '없는 대상' 답의 메서드 목록 완전성 검사), `MaskingMiddleware`(비밀·개인정보 마스킹). `agent.py` 의 `build_agent` 에서 `[InputGuard, Summarization, OutputCheck, HITL, Masking]` 순서로 결합한다. 규칙·패턴은 파일 상단 "튜닝 상수" 블록.
- `src/usage.py`: LLM 토큰 사용량 집계. `collect()` 구간 안에서 LangChain 콜백(`current_callbacks()`)이 에이전트 그래프의 모든 모델 호출(Haiku 보조 호출 포함)을 모델별로 합치고, 임베딩은 `embeddings.py` 가 직접 더한다. `run_query` 가 요청마다, 평가 러너가 케이스마다(judge 는 `[judge]` 라벨) 구간을 연다. 구간은 중첩되며 안쪽 값이 바깥에도 더해진다. RAGAS 호출은 제외.
- `src/tracing.py`: trace 로그 기록(`make_record`·`write_trace`)과 검사(`hitl_violations`·`unaccounted_outputs`·`summarize`). `agent.py` 를 import 하지 않는다(순환 방지). 쓰기 실패는 응답을 막지 않는다.
- `src/embeddings.py`: Bedrock Titan 임베딩 호출과 해시 캐시(`data/index/embed_cache.json`). 모델은 `BEDROCK_EMBED_MODEL_ID`(기본 `amazon.titan-embed-text-v2:0`).
- `src/retriever.py`: RAG. 자바는 `tools.parse_project()` 의 클래스·메서드 경계로 청킹하고, FAISS(벡터) + BM25 를 RRF 로 합친다. 검색 결과가 약하면 `BEDROCK_REWRITE_MODEL_ID`(기본 Haiku 4.5)로 질의를 영어 식별자로 바꿔 재검색한다. 조정값은 파일 상단 "튜닝 상수" 블록에 있다.
  - 인덱스(`data/index/`)는 청크·임베딩 설정의 해시가 바뀌면 자동 재생성된다. 수동 재생성은 폴더 삭제 또는 `--rebuild`.
  - 점검: `python -m src.retriever --chunks` (청크 통계), `python -m src.retriever "질의"` (벡터·BM25 순위와 점수).

## 평가 (`evaluation/`)

- `test_queries.csv` 스키마: `id,category,input,expected_traits,forbidden,expected_tools,note`. `expected_traits`, `forbidden` 은 세미콜론 구분.
- 러너 `evaluation/run_eval.py`: 규칙(케이스 1·5·7·15·16·17·21) + LLM judge(나머지, Sonnet 4.6) 혼합 판정. 조정 지점은 파일 상단 `★` 주석(`TRAIT_PASS_RATIO` 기본 0.70 등). 결과는 `evaluation/results/`, 라운드별 trace 로그는 `logs/eval_*.jsonl`.
  - 명령: `--round N`(전체 21건, 약 4분) / `--dry-run`(대상·예상 토큰만, 호출 없음) / `--budget 토큰`(입력 토큰 예산에 닿으면 중단) / `--rescore N [--apply]`(저장된 결과를 현재 `TRAIT_PASS_RATIO` 등으로 다시 판정, 호출 없음) / `--only 8 10` / `--failed` / `--show N` / `--compare A B` / `--retrieval TAG`(정답 위치 기준 검색 지표) / `--ragas TAG [--reuse]`(RAGAS 4지표, `--reuse` 는 저장된 샘플로 지표만 재계산). 테스트용 라운드 결과는 직접 지운다.
  - 정답 목록 `evaluation/ragas_reference.json`: 케이스별 `reference`(정답 설명), `reference_locations`(`path`·`start`·`end`·`anchor`), `decoy_locations`. 소스가 바뀌면 `tests/test_evaluation.py` 의 앵커 검사가 어긋난 라인을 알려 준다.
  - 위치 지표: `evidence_recall`(1차와 같은 기준 유지)과 `list_structure` 위치까지 센 `evidence_recall_with_structure` 를 따로 기록한다. 평가 중에는 `agent.MAX_TEXT` 를 `EVAL_MAX_TEXT` 로 올려 긴 출력의 위치를 끝까지 본다(`run_case` 안에서 설정).
  - 영향도(케이스 7) 정답 `IMPACT_TRUTH` 는 파일 이름 10개이고 `ragas_reference.json` 케이스 7 과 같다. 한쪽을 고치면 다른 쪽도 고친다.
  - 17번은 같은 세션에서 앞선 분석 질문을 먼저 보낸다(`SETUP_QUESTIONS`).
  - RAGAS: 채점 LLM 은 `BEDROCK_RAGAS_MODEL_ID`(기본 `us.…haiku-4-5`), 임베딩은 Titan v2. 컨텍스트에는 `doc_id` 머리말을 붙인다(없으면 답변의 `파일:라인` 인용이 확인되지 않아 faithfulness 가 부당하게 깎인다).
- **토큰을 아끼는 실행 순서**: 코드·프롬프트를 고친 뒤에는 ① 단위 테스트와 저장된 답변으로 하는 오프라인 확인(`--rescore`, 규칙 검사 재생) → ② `--dry-run` 으로 대상·예상 토큰 확인 → ③ 바뀐 영향이 있는 케이스만 `--only` → ④ 마지막에 한 번만 전체. 판정 기준(★)만 바꿀 때는 모델을 다시 부르지 않고 `--rescore`. 일일 한도 오류가 나면 러너가 남은 케이스를 돌리지 않고 멈추며 기존 결과를 덮어쓰지 않는다. 반복 실행은 횟수를 정해 두고 한다.
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


**한도(Throttling) 우회:** 일일 토큰 한도는 리전·프로필별이다. 기본 리전 `us-east-1` 이 막히면 환경변수로 `ap-northeast-2` 를 쓴다. 서울에는 `us.` 프로필이 없으므로 모든 모델 ID 를 `global.` 로 바꿔야 한다. (셸 환경변수가 `.env` 보다 우선한다)
```
export AWS_DEFAULT_REGION=ap-northeast-2
export BEDROCK_MODEL_ID=global.anthropic.claude-sonnet-4-6
export BEDROCK_REWRITE_MODEL_ID=global.anthropic.claude-haiku-4-5-20251001-v1:0
export BEDROCK_GUARD_MODEL_ID=global.anthropic.claude-haiku-4-5-20251001-v1:0
export BEDROCK_SUMMARY_MODEL_ID=global.anthropic.claude-haiku-4-5-20251001-v1:0
export BEDROCK_RAGAS_MODEL_ID=global.anthropic.claude-haiku-4-5-20251001-v1:0   # --ragas 채점용
export BEDROCK_JUDGE_MODEL_ID=global.anthropic.claude-sonnet-4-6                  # --round 의 LLM judge (빠뜨리면 서울에서 ValidationException)
```
새 계정에서는 `us.anthropic.claude-sonnet-4-6` 의 일일 한도만 먼저 소진될 수 있다. 이때는 리전을 바꾸지 말고 같은 리전에서 `BEDROCK_MODEL_ID`·`BEDROCK_JUDGE_MODEL_ID` 만 `global.anthropic.claude-sonnet-4-6` 으로 지정해도 된다(Haiku 는 `us.` 그대로).
Haiku 변수를 빼먹으면 재작성·범위 분류가 실패해도 조용히 넘어가(재작성 생략, 분류 통과) 검색·가드 결과가 왜곡된다. 평가 결과 JSON 에는 `region` 이 기록된다.

## 함정
- **분석 대상 일반화(9-1~9-4 완료, 9-5 문서 정리만 남음):** 설정 자동 감지, 계층 분류 관례 추정, 문구 중립화, 두 번째 프로젝트 검증까지 끝났다. 알려진 한계는 TASK.md 9-4. 계층 분류 규칙은 `tools.py` 상단 상수이고 우선순위(어노테이션 → 패키지 → 공통 → 관례 추정)를 바꾸면 petclinic 결과가 달라질 수 있다. 안내문·분류·재작성 프롬프트는 중립화했고, `agent.py` `SYSTEM_PROMPT` 에 petclinic 고유 예시(`OwnerService.deleteOwner`, `SPRING_PROFILE`)가 남아 있다(9-3 2단계, 케이스 3·8·10 회귀 확인 필요). 계획은 TASK.md 묶음 9.
- **마스킹과 JSON:** 도구 결과는 JSON 문자열로 `MaskingMiddleware` 를 통과한다. 비밀값 정규식(`MASK_PATTERNS`)이 따옴표 이스케이프(`\"`)의 역슬래시를 삼키면 JSON 이 깨져 `contexts` 의 `doc_id` 파싱이 오류 없이 실패한다(`search_code#N` 으로 뭉침). 정규식을 고치면 `tests/test_guardrails.py` 의 JSON 왕복 테스트로 확인한다.
- **XML 청킹:** `retriever.py` 의 `XML_STRUCTURED` 는 기본 `False`(60줄 고정 창)다. `True`(요소 단위)는 같은 임베딩으로 A/B 했을 때 recall 이 오르지 않아 채택하지 않았다. 설정이 걸린 기능이 검색에서 밀리는 원인은 청크 크기가 아니라 메서드 이름과 기능 키워드를 한 질의에 섞는 것이었고, 프롬프트로 질의를 나누게 해 해결했다. 바꾸면 인덱스가 자동 재생성된다.
- **ragas 호환:** `ragas 0.4.3` 은 `langchain-community 0.4.x` 에 없는 모듈을 import 해서 그대로는 실패한다. `run_eval.py` 의 `_shim_langchain_community()` 가 빈 대체 모듈로 우회한다(설치 환경은 바꾸지 않는다). 기존 지표 클래스는 deprecation 경고를 낸다.

## 구조 (`src/`, `data/`, `evaluation/` 배치는 제출 규약이라 바꾸지 않는다)
```
mini-pjt_hjlee/
├── src/                     # agent.py 메인 에이전트 그래프, tools.py 도메인 도구, retriever.py RAG 파이프라인 (제출 규약 3종)
│                            # + api.py (POST /query), settings.py (분석 대상 설정), embeddings.py (임베딩), guardrails.py (가드레일), tracing.py (trace 로그)
├── tests/                   # 단위 테스트 (가드레일·도구·검색·에이전트 로직·trace·API 계약·평가 지표). 모두 오프라인
├── scripts/                 # 수동 테스트: try_query.py(일반 질의), try_approval.py(문서 승인 흐름), _client.py(공용 HTTP·출력)
├── logs/                    # trace.jsonl (생성물, git 제외)
├── data/petclinic-legacy/   # 사용한 문서와 데이터. 분석 대상 소스 (읽기 전용, 커밋 대상. 빌드 산출물 target/ 은 폴더 자체 .gitignore 가 제외)
├── data/spring-petclinic-boot/  # 일반화 검증용 두 번째 프로젝트 (git 제외). 받기: git clone --depth 1 https://github.com/spring-projects/spring-petclinic.git data/spring-petclinic-boot
├── data/index/              # RAG 인덱스(대상별 폴더)·임베딩 캐시 (생성물, git 제외)
├── evaluation/              # test_queries.csv(21건: 1~17 기존 + 18~21 신규), run_eval.py(러너), ragas_reference.json(정답 위치), round1·2_report.md, results/(결과 JSON)
├── output/                  # save_doc 결과 (승인 후에만 생성)
├── README.md  SERVICE.md  TASK.md  CLAUDE.md
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