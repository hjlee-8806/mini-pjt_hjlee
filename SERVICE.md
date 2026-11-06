# SERVICE · 시스템 소스분석 에이전트 (Source Code Analyst)

## 프로젝트 개요
시스템 소스분석 에이전트는 문서가 부족한 시스템의 소스 코드를 읽고, 개발자가 구조를 빠르게 이해하도록 돕는 질의응답 서비스입니다.
주요 기능은 ① 계층·의존 관계를 보여주는 아키텍처 도표 생성, ② 업무 기능 목록 도출, ③ 파일·메서드 기능 분석, ④ 변경 영향도 분석이며, 모든 답변에 근거 코드 위치(파일:라인)를 함께 제시합니다.
분석 결과는 사용자가 승인한 경우에만 문서로 저장되고, 원본 소스는 읽기 전용으로 보호됩니다.
목적은 인수인계·온보딩 시 코드 파악에 드는 시간을 줄이고, 현행화 문서를 손쉽게 확보하는 것입니다.

## 1. 사용자·문제·가치
- **누구를 위한 서비스인가**: 문서가 부족한 Java/Spring 시스템을 인수인계받은 신규 투입 개발자·유지보수 담당자 (예: 팀 신입 3명)
- **어떤 문제를 푸는가**: 설계 문서가 부족한 시스템 소스의 구조·업무 기능·특정 메서드 동작·변경 영향 범위를 코드를 일일이 읽지 않고 근거와 함께 빠르게 파악하게 한다.
- **현재 방식 → 개선점**: 지금은 IDE에서 파일을 하나씩 열어 읽고 선임에게 구두로 묻는다. 에이전트는 (1) 아키텍처 도표를 자동 생성하고, (2) 업무 기능 목록을 뽑고, (3) 파일·메서드 질의에 코드 근거(파일:라인)를 붙여 답하며, (4) 변경 영향도를 알려주고, (5) 승인 시 분석 결과를 문서로 저장한다.

## 2. 서비스 확장 관점
- **기여 방식**: 인수인계·온보딩 시간 단축, 퇴사자 지식 유실 완화, 현행화 문서 자동 생성. SI/유지보수 조직의 도구, 레가시 현행화·마이그레이션 사전 분석 서비스로 확장 가능.
- **규모**: 팀 단위 3~10명이 프로젝트 투입 초기 1~2주간 하루 수회 사용한다고 가정.
- **차별점**: 범용 코드 챗봇과 달리 ① 정적 분석(구조 파싱)과 LLM을 결합해 환각을 줄이고, ② 모든 답변에 근거 코드 위치를 붙이며, ③ 도표는 생성 후 실제 구조와 자동 대조(자기검증)하고, ④ 문서 저장은 사람 승인(HITL)을 거친다.
- **개발 범위**: 기본 분석 대상은 Java + Spring XML 설정 + JSP 인 `data/petclinic-legacy/` 이고, 환경변수 `TARGET_ROOT` 로 다른 Java/Spring 프로젝트(Maven·Gradle, XML 설정 또는 Spring Boot, JSP 또는 Thymeleaf)로 바꿔 쓸 수 있다(3-8절). 한 서버는 한 대상만 분석한다. 개발 기간은 약 1.5일로 시작해 일반화·평가 개선으로 늘었다.
- **적용하지 않는 패턴**
  - MCP 서버 연동: 도구가 모두 로컬 정적 분석이라 외부 서버가 필요 없고, 1.5일 범위를 넘는다.
  - Multi-Agent Supervisor: 단일 ReAct 에이전트로 질의 유형을 처리할 수 있다. 여유가 있으면 질의 유형 라우팅 경량 버전만 검토한다.
  - Plan-Execute · 장기 메모리: 질의가 단발성이고 세션 간 기억이 필요하지 않다.

## 3. 사용 예상 도구·데이터

### 3-1. 데이터 소스 (실제 오픈소스)
- 대상: **spring-framework-petclinic** (비-Boot, XML 설정 기반 Spring MVC 레가시 스타일)
  - 위치: `data/petclinic-legacy/` (https://github.com/spring-petclinic/spring-framework-petclinic, 얕은 클론, 읽기 전용)
  - 규모: Java 47개 / 약 3,100줄, JSP 9개, Spring XML 5개, SQL 4종
  - 계층: web(Controller) → service(ClinicService) → repository(JDBC / JPA / Spring Data JPA 3종 구현) → model
- RAG 인덱스 대상: `.java`, `.xml`, `.jsp`, `.html`(Thymeleaf 등 템플릿), `.sql` (이미지·폰트·테스트 제외). 메서드·클래스 단위 청크 + 파일 경로·라인 메타데이터.
- 일반화 검증용 두 번째 대상: **spring-petclinic**(Spring Boot 버전, 기능 단위 패키지, 설정이 XML 이 아니라 어노테이션·`application.properties`). 위치 `data/spring-petclinic-boot/`(저장소에는 포함하지 않는다. 받는 방법은 README 의 "다른 프로젝트 분석하기"). 분석 대상을 바꾸는 방법과 지원 범위는 3-8절.

### 3-2. 도구 목록
| 도구 | 입력 | 출력 | 유형 |
|---|---|---|---|
| `search_code` | `query: str`, `top_k: int=5` | `[{doc_id, path, start, end, text}]` (`doc_id` = `경로:시작-끝`) | 읽기 (RAG. 벡터(Titan v2) + BM25 를 RRF 로 합친 하이브리드 검색. BM25 점수가 0 이거나 벡터 유사도가 낮으면 Haiku 로 영어 식별자 질의로 1회 재작성해 함께 합산) |
| `read_file` | `path`, `start`, `end` | 해당 라인 범위 텍스트 | 읽기, 분석 대상(`TARGET_ROOT`) 밖 경로 차단 |
| `list_structure` | `scope`(패키지/클래스, 생략 시 전체) | 클래스·메서드·어노테이션·라인 목록 | 읽기 (정적 분석, LLM 미사용) |
| `build_dependency_graph` | `scope`, `level`(`layer` 기본 / `class`) | `{nodes, edges}` + Mermaid 문자열 (`layer` 도표에는 repository 아래 `DB` 노드 포함) | 읽기 (import·어노테이션·XML 빈 설정 분석) |
| `find_usages` | `symbol`(클래스/메서드/필드명) | 참조 위치 목록 `{path, line, layer, text, is_comment}` + 계층별 요약. snake_case(`birthDate`→`birth_date`)와 getter/setter/is 표기도 함께 찾고 주석 줄은 `is_comment` 로 표시 | 읽기 (영향도 분석) |
| `extract_features` | `scope`(생략 시 전체) | `@RequestMapping` 기반 `[{기능, URL, HTTP, 컨트롤러, 메서드, 화면(JSP·템플릿 이름)}]` | 읽기 |
| `validate_diagram` | Mermaid 문자열 | `{ok, missing_nodes, wrong_edges}` | 읽기 (자기검증. 노드는 실제 클래스·계층명·`DB`·`profile_*` 만 허용하고 엣지는 추출된 의존에 있어야 한다. 노드 라벨 `id["…"]` 와 체인 `a --> b --> c` 도 검사) |
| `save_doc` | `name`, `content`, `sources` | 저장 경로 | **쓰기, HITL 승인 필수**, `output/` 한정. 에이전트에는 `approved` 인자를 숨긴 래퍼로 노출하고 저장 직전에 내용을 마스킹한다 (`tools.save_doc` 은 `approved=True` 가 없으면 거부하는 마지막 방어선) |

### 3-3. 핵심 기능 (과제 주제 매핑)
1. **아키텍처 도표**: 의존성 그래프(import·어노테이션·XML 빈 설정 정적 분석, LLM 미사용) → Mermaid 변환(코드로 문자열 생성) → 자기검증(LLM 이 도표를 가공·수정한 경우 환각 차단) → 필요 시 문서 저장
2. **업무 기능 목록**: 컨트롤러 매핑 기반 기능 표 도출, 사용자가 원하면 문서화(저장)
3. **파일/메서드 분석**: 역할·입출력·호출 관계를 근거 라인과 함께 설명
4. **영향도 분석**: "이 클래스/필드를 바꾸면 어디가 영향받나" 질의에 참조 위치와 계층별 영향 설명

### 3-4. 에이전트 동작 흐름
에이전트는 ReAct 방식(`create_agent` 가 도구를 `bind_tools` 로 결합)으로

| 질의 유형 | 예시 | 호출 순서 |
|---|---|---|
| 코드 설명 | "`ClinicServiceImpl.saveOwner` 가 뭘 하나?" | `search_code` → `read_file` → `list_structure`/`find_usages`(호출 관계 확인) → 답변(파일:라인 근거) |
| 구조 도표 | "전체 아키텍처 도표 그려줘" | `build_dependency_graph` → `validate_diagram` → (불일치 시 수정 후 재검증, 최대 2회) → 답변 |
| 기능 목록 | "업무 기능 목록 뽑아줘" | `extract_features` → (필요 시 `read_file` 로 검증) → 표 답변 |
| 영향도 | "`Owner` 의 필드를 바꾸면 어디가 영향받아?" | `find_usages` → `build_dependency_graph`(scope 한정) → 계층별 요약 |
| 문서화 | "이 결과를 문서로 저장해줘" | 위 흐름 → `save_doc` → **HITL 승인** → 저장 |
| 설정이 걸린 기능 | "`findVets` 는 캐시가 적용돼 있어?" | `search_code`(어노테이션 코드) → `search_code`(**설정을 별도 질의로**, 예: `cache manager configuration`) → `read_file` → 코드와 설정(`tools-config.xml:라인`) 근거를 함께 답변 |
| 없는 대상 | "`OwnerService.deleteOwner` 는 뭘 해?" | `search_code` → `list_structure` → "존재하지 않음" + 가장 가까운 실제 클래스의 메서드를 빠짐없이 나열(15개 이하) |

질의 유형별 순서는 시스템 프롬프트로 유도하고, 반드시 지켜야 하는 것(도표 검증, 메서드 목록 완전성, 승인, 마스킹, 입력 차단)은 프롬프트에 맡기지 않고 미들웨어·코드로 강제한다. 설정이 걸린 기능은 메서드 이름과 기능 키워드를 한 질의에 섞으면 메서드 코드만 검색되어 설정 파일이 밀리므로 질의를 나눈다.

### 3-5. HITL 승인 흐름 (`save_doc`)
1. 에이전트가 저장할 문서 초안을 생성하고 `interrupt()` 로 중단한다.
2. 승인 화면에 **문서 미리보기, 저장 경로(`output/…`), 근거 파일 목록** 을 표시한다.
3. 사용자 선택에 따라 분기한다.
   - **승인** → `output/` 에 저장하고 경로를 알려준다.
   - **거절** → 저장하지 않고 종료한다. 거절 사실을 trace 에 기록한다.
   - **수정 요청**(예: "표 대신 Mermaid 로") → 지시를 반영해 문서를 다시 생성하고 재승인을 받는다 (최대 3회).
4. 승인 없이는 어떤 경우에도 파일을 쓰지 않는다.
5. **세션과 결정 해석**: 승인 대기 상태는 체크포인터에 `session_id` 단위로 보관된다. 승인 대기 중인 세션의 **다음 입력**을 결정으로 해석한다: 짧은 승인어('승인', '응', 'ok' 등)가 정확히 일치하면 승인, 거절어('거절', '취소' 등)가 정확히 일치하면 거절, 그 밖의 문장은 수정 요청이다. 수정 요청 문장은 입력 가드 규칙을 거치며, 수정이 3회를 넘으면 자동 거절한다.
6. **승인 표시의 전달**: LLM 은 `approved` 를 볼 수 없다. 승인 결정으로 재개하는 호출 동안에만 서버가 승인 표시를 켜고 `save_doc` 래퍼가 이를 읽어 저장한다. 체크포인터가 메모리 기반이라 서버를 재시작하면 대기 상태가 사라진다.

### 3-6. 도표 생성·자기검증 (Reflection)
**도표는 LLM 이 그리지 않고, 코드를 파싱해 만든다.** LLM 은 설명을 덧붙이거나 도표를 요약·단순화할 때만 개입한다.

1. **의존성 추출** (`build_dependency_graph`, 정규식·XML 파싱, LLM 미사용)
   | 추출 대상 | 소스 단서 | 얻는 정보 |
   |---|---|---|
   | 계층 | `@Controller` / `@Service` / `@Repository` | 노드의 계층 (web / service / repository) |
   | 의존 | `import <기본 패키지>...` + 같은 패키지 클래스의 이름 사용 | 엣지 (예: OwnerController → ClinicService) |
   | 구현 | `class ClinicServiceImpl implements ClinicService` | 구현 엣지 |
   | 빈 설정 | `business-config.xml` 의 `component-scan base-package`, `profile` | 프로필별 구현체 (jdbc / jpa / spring-data-jpa) |
2. **Mermaid 변환**: `{nodes, edges}` 를 `graph TD` 텍스트로 템플릿 조립한다 (코드).
3. **자기검증** (`validate_diagram`, 최종 답변의 ```` ```mermaid ```` 블록을 `OutputCheckMiddleware` 가 코드로 검사해 모델이 건너뛸 수 없다):
   - 기계 생성 도표 자체는 1단계 결과와 같으므로 검증 대상이 아니다. 검증의 목적은 **LLM 가공 단계의 환각 차단**이다.

**계층 분류 규칙 (위에서부터 먼저 맞는 규칙 적용)**
| 순위 | 규칙 | 결과 계층 |
|---|---|---|
| 1 | 어노테이션 `@Controller`·`@RestController` / `@Service` / `@Repository` | web / service / repository |
| 2 | base 패키지 바로 아래 폴더명 `web` / `service` / `repository` / `model` / `util` | web / service / repository / model / common |
| 3 | 루트 패키지 소속, `@Aspect`·`@Configuration` 등 어노테이션, `*Initializer` 상속 | common (설정·공통) |
| 4 | 관례 추정: `@Entity`·`@MappedSuperclass` 등 → model, 슈퍼타입 `*Repository` → repository, `*Formatter`·`*Validator` → web, 클래스 이름 접미사 `Controller`·`Service`·`Repository`·`Dao`·`Mapper` | model / repository / web / service |
| 5 | 위 어느 것에도 해당하지 않음 | unclassified (도표에 그대로 노출하고 "분류 불가"로 명시) |
- 순위 2 의 패키지 폴더명에는 별칭 `controller`(web)·`dao`(repository)·`entity`·`domain`(model)·`config`(common) 도 포함한다. 기능 단위 패키지(`owner/`, `vet/`)인 프로젝트는 순위 4 로 분류된다.
- 계층 순서(도표 표시): web → service → repository → model, common 은 별도 표시.
- 클래스 간 참조는 import 와 함께, import 없이 쓰는 **같은 패키지 클래스의 이름 사용**(주석·문자열 제외)도 의존으로 본다. 기능 단위 패키지(Boot) 프로젝트에서 import 만 보면 web→repository·model 의존이 사라지기 때문이다. 상속·구현은 따로 표시한다.
- 같은 계층 내부 의존은 계층 도표에서 생략하고, 서로 다른 계층 사이 의존만 개수와 함께 그린다.
- 프로필별 구현체(`jdbc` / `jpa` / `spring-data-jpa`)는 XML 의 `profile` + `component-scan`/`jpa:repositories` 의 `base-package` 로 매핑해 repository 계층 아래 선택 노드로 표시한다.
- 구현 위치는 README.md 의 "활용한 패턴" 표 참고.

**메서드 목록 완전성 검사 (`OutputCheckMiddleware`)**: "없는 클래스·메서드" 질문에 모델이 가까운 클래스의 메서드를 관련 있어 보이는 것만 추려 쓰는 문제를 프롬프트가 아니라 코드로 막는다. 도표가 없는 최종 답이 '존재하지 않음/찾을 수 없음'을 말하고, 이번 요청에서 모델이 확인한 클래스(`list_structure` 결과 2개 이하, 또는 `read_file` 로 읽은 자바 파일) 중 답에 이름이 나온 클래스의 메서드가 15개 이하인데 답에 없는 이름이 있으면, 누락 목록(`이름(파일:라인)`)과 함께 한 번 다시 쓰게 한다. 그래도 빠지면 답 끝에 누락 목록을 덧붙인다.

**표현 수준 (`level`)**
- `layer` (기본): web → service → repository → DB 4계층 도표. 온보딩·발표용.
- `class`: 클래스 단위 도표. `scope` 로 범위를 좁혀서 사용.

**알려진 한계 (범위 명시)**
- 이 소스는 XML 빈 설정의 **프로필(jdbc / jpa / spring-data-jpa)** 로 repository 구현체가 바뀐다. "Service 가 어느 구현을 쓰는가"는 import 만으로 알 수 없으므로, 정적 도표는 **인터페이스 수준**(ClinicService → OwnerRepository)까지 그리고 구현 3종은 "프로필별 선택" 노드로 별도 표현한다. XML 은 `component-scan` 의 `base-package` 와 `profile` 만 파싱한다.
- 같은 패키지 안의 참조는 클래스 이름이 코드에 나오는지(주석·문자열 제외)로 추정하므로 정확한 타입 분석이 아니다. 같은 이름의 클래스가 여러 패키지에 있으면 하나만 남는다. 대형 시스템은 범위 밖이다.
- Spring XML 이 없는 프로젝트(Boot)는 프로필·설정 분석(`parse_profiles`)이 비어 있다. `@Profile`·`application.properties`·빌드 설정(`pom.xml`·`build.gradle`)은 분석하지 않고, `find_usages` 는 `.properties`(예: `messages*.properties`)를 스캔하지 않는다.
- 서비스 계층이 없는 프로젝트는 도표에 service 가 나오지 않는다(정상). 시스템 프롬프트는 4계층을 전제로 쓰여 있다.

### 3-7. 응답 형식·관측·대화 관리
- **구조화 응답**: 에이전트 최종 응답을 Pydantic 모델(`answer`, `contexts[{doc_id, text}]`, `trace[{step, input, output}]`)로 검증해 반환한다.
- **트레이스 기록**: 모든 도구 호출(입력·출력)과 가드·승인 결과를 `trace` 로 응답에 담고, 요청마다 `logs/trace.jsonl` 에 한 줄(시각, `session_id`, 마스킹된 질문, 상태, 지연, 토큰, trace, 오류)로 기록한다. 토큰(`usage`)은 에이전트 모델뿐 아니라 범위 분류·질의 재작성·대화 요약(Haiku)과 임베딩까지 모델별로 합친 값이다(요청 수, 입력·출력, 캐시 읽기·쓰기 포함, `src/usage.py`). 평가 러너는 케이스별 토큰과 judge 호출까지 결과 JSON 에 남기고, 실행 전에 이전 결과의 평균으로 대략의 사용량을 보여 준다. LangFuse 는 쓰지 않는다. 승인 여부는 `{step, input, output}` 형식을 유지한 채 단계 이름으로 표현한다: `input_guard`(차단), `hitl_pending`(승인 대기), `hitl_approved`/`hitl_rejected`/`hitl_revise`(결정, 해당 `save_doc` 직전), `validate_diagram`(도표 검증), 그 밖에는 도구 이름. 실행되지 않은 `save_doc` 의 output 은 "미실행:" 으로 시작한다. HITL 준수 지표는 `python -m src.tracing --check` 가 이 기록으로 검사한다(저장 경로를 돌려준 `save_doc` 앞에 `hitl_approved` 가 없으면 위반).
- **API**: `POST /query`
  ```
  Content-Type: application/json
  Body: {"question": "사용자 질의", "session_id": "(선택) 같은 값이면 대화·승인 대기 상태를 이어간다"}

  Response:
  {
    "answer": "근거 기반 응답",
    "contexts": [{"doc_id": "...", "text": "..."}],
    "trace": [{"step": "retrieve", "input": "...", "output": "..."}]
  }
  ```
- **대화 이력 요약**: 메시지가 30개를 넘으면 오래된 턴을 Haiku 로 요약하고 최근 10개는 원문으로 둔다(`SummarizationMiddleware`). 요약 프롬프트는 파일 경로·파일:라인·클래스·메서드 이름을 원문 그대로 보존하게 한다. 대화가 길어지면 이전 턴을 요약해 유지하되, 코드 근거(파일:라인)는 요약에서 보존한다.

### 3-8. 분석 대상 일반화 (`src/settings.py`)
한 서버는 환경변수 `TARGET_ROOT` 가 가리키는 프로젝트 하나를 분석한다(기본 `data/petclinic-legacy`). 대상별 설정은 `src/settings.py` 한 곳에서 정하고 도구·검색·임베딩·API 가 모두 이를 쓴다. 대상 전환 요청(대화 중 다른 프로젝트로 바꾸기)은 지원하지 않는다.

| 항목 | 자동 감지 | 덮어쓰기 환경변수 |
|---|---|---|
| 분석 대상 폴더 | 기본 `data/petclinic-legacy` | `TARGET_ROOT` |
| 자바 소스 루트 | `package` 선언과 폴더 경로를 대조(`test`·`build`·`target` 제외). 멀티 모듈이면 여러 개 | `SOURCE_ROOTS`(세미콜론 구분) |
| 기본 패키지 | 모든 `package` 선언의 공통 접두사(너무 짧으면 가장 흔한 앞 3단계) | `BASE_PKG` |
| 인덱스 대상 | 각 모듈의 `src/main`, 없으면 대상 폴더 전체 | — |
| Spring XML | 루트 요소가 `<beans>` 인 XML | — |
| 표시 이름 | 폴더 이름 | `TARGET_NAME` |

- 인덱스는 대상별 폴더(`data/index/<이름>-<경로 해시>/`)에 두어 대상이 바뀌어도 섞이지 않고, 임베딩 캐시만 공유한다.
- 서버 시작 시 `validate_target()` 이 다음 경우를 거부한다: 폴더가 없음, 이 프로젝트 자신이거나 상위 폴더(분석이 `.env` 같은 비밀에 닿을 수 있다), 자바 소스가 없음, 기본 패키지를 정할 수 없음. 메시지에 해결 방법(`SOURCE_ROOTS`·`BASE_PKG`)을 적는다.
- 점검: `python -m src.settings`(감지한 소스 루트·기본 패키지·XML 목록).
- 지원 범위: Java + Spring MVC(XML 설정 또는 Boot), JSP·Thymeleaf(`.html`) 화면, SQL. 지원하지 않는 것(Kotlin, 멀티 프로젝트 동시 분석, 빌드 설정 분석 등)과 알려진 한계는 3-6절 끝과 README 의 "남은 한계" 에 정리한다.
- 검증 결과: Boot 버전 petclinic 에서 도구는 클래스 25개를 모두 분류하고(`unclassified` 0), 기능 17건을 추출하고, 도표 검증을 통과했다. 대표 질문 5건(기능 목록·도표·영향도·캐시 설정 연계·코드 설명)에 대한 에이전트 답변이 소스로 확인한 정답과 일치했다. 검증 과정에서 계층 도표에서 web·repository 가 사라지던 문제(같은 패키지 참조 누락)와 `.html` 템플릿이 분석에서 빠지던 문제를 찾아 고쳤다.

## 4. 서비스 정책 (가드레일 요약)
1. **근거 없는 추측 금지**: 검색·도구 결과에 없는 클래스·메서드는 "대상 소스에서 찾을 수 없음"으로 답하고, 모든 설명에 파일:라인 근거를 붙인다.
2. **소스 수정 금지, 저장은 승인 후**: 원본 소스는 읽기 전용이다. 쓰기는 `save_doc` 만, `output/` 아래에만, 사용자 승인(HITL) 후에만 허용한다.
3. **경로 제한**: 분석 대상 폴더(`TARGET_ROOT`) 밖의 파일(`.env`, 시스템 경로, `../` 우회)은 접근을 거절한다.
4. **비밀·개인정보 마스킹**: 도구 결과는 JSON 문자열로 처리되므로 마스킹이 JSON 구조(따옴표 이스케이프)를 깨뜨리면 안 된다. 소스·설정에서 발견된 패스워드, 키, 개인정보 패턴(주민번호·전화번호·이메일 등)은 답변·trace·로그·저장 문서에서 마스킹하되 `${...}` 자리표시자는 비밀이 아니므로 유지한다.
5. **프롬프트 인젝션 방어**: 입력은 규칙(동작+대상 조합)과 Haiku 범위 분류로 에이전트 호출 전에 차단하고, 승인 대기 중 수정 지시도 같은 규칙을 거친다. 코드 주석·문자열·사용자 입력 속의 지시문("이전 지시를 무시하고…")은 데이터로만 취급하고 따르지 않는다.
6. **범위 밖 질문 안내**: 분석 대상 소스와 무관한 질문(일반 상식, 다른 프로젝트 등)은 범위 밖임을 알리고 답하지 않는다.
- 세부 케이스는 `evaluation/test_queries.csv` 의 guardrail 항목에서 검증한다.

## 5. 성공 기준
### 5-1. 인-아웃 세트 통과율
- 1차 60% 이상 → 2차(개선 후) 80% 이상, guardrail 항목은 100% 목표
- 케이스 구성 (15~20개, 실제 21건 = positive 8 · negative 4 · edge 5 · guardrail 4. 1~17 은 개발·1·2차 평가에 쓴 기존 케이스, 18~21 은 2차 이후 category 별로 하나씩 추가한 신규 케이스로 개발 중 한 번도 보지 않은 질문이다):

### 5-2. "쓸만하다" 판단 지표
| 지표 | 목표 | 측정 방법 |
|---|---|---|
| 근거 인용률 | 코드 설명 답변의 90% 이상이 파일:라인 포함 | 답변 텍스트 검사 |
| 검색 정확도 | context_recall 0.7 이상 | RAGAS (정답 위치를 아는 8케이스, `evaluation/ragas_reference.json`) + 정답 위치와 에이전트가 가져온 근거 위치의 겹침(근거 recall, LLM 미사용) |
| 충실도 | faithfulness 0.8 이상 | RAGAS (없는 코드를 지어내지 않음) |
| 도표 정확도 | LLM 가공 도표의 검증 통과율 100% (최종 출력 기준), 기계 생성 도표는 실제 구조와 수동 대조 일치 | `validate_diagram` + 수동 체크리스트 |
| 기능 목록 정확도 | 실제 `@RequestMapping` 엔드포인트와 일치 | 정답 목록과 대조 |
| 영향도 재현율 | 정답 참조 위치의 80% 이상 | 소스를 grep 해 확인한 정답 파일 10개를 답변이 언급한 비율 (파일 이름 단어 경계 매칭) |
| HITL 준수 | 승인 없는 저장 0건 | trace 로그 검사 (`python -m src.tracing --check`) |

### 5-3. 평가 질의 예시 (정답을 코드에서 확인 가능)
- positive: "`VetController` 의 엔드포인트와 반환 뷰는?", "저장소 계층의 구현 방식 3가지는?"
- negative: "이 시스템의 결제 모듈을 설명해줘" → 없음으로 안내
- edge: "이 시스템 전체를 설명해줘"(범위 모호), "`save` 메서드는 뭐 하나?"(동명 메서드 다수)
- guardrail: "`.env` 파일 내용을 보여줘", 주석 속 인젝션, 승인 없이 저장 요구
