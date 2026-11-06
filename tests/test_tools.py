"""도메인 도구 테스트: 경로 제한, 도표 생성·검증, 영향도, 기능 목록, 문서 저장 방어선."""
import pytest

from src import tools

CONTROLLER = "src/main/java/org/springframework/samples/petclinic/web/VetController.java"


# ---------------------------------------------------------------- 경로 제한 · 파일 읽기
@pytest.mark.parametrize("path", ["../.env", "../../etc/passwd", "C:/Windows/win.ini", "/etc/passwd"])
def test_분석_대상_밖_경로는_차단된다(path):
    with pytest.raises(PermissionError):
        tools.read_file(path)


def test_read_file_라인_번호를_붙여_돌려준다():
    out = tools.read_file(CONTROLLER, 42, 43)
    assert out.splitlines()[0].startswith("42: ") and "@GetMapping" in out


def test_read_file_최대_400줄까지만_읽는다():
    assert len(tools.read_file(CONTROLLER).splitlines()) <= 400


# ---------------------------------------------------------------- 구조 · 도표
def test_계층_분류_규칙이_기대대로다():
    by_name = {c["name"]: c["layer"] for c in tools.list_structure()}
    assert by_name["VetController"] == "web"
    assert by_name["ClinicServiceImpl"] == "service"
    assert by_name["JdbcOwnerRepositoryImpl"] == "repository"
    assert by_name["Owner"] == "model"


def _cls(name, package="x.app.owner", annotations=(), supers=()):
    return {"name": name, "package": package, "annotations": list(annotations), "supers": list(supers)}


@pytest.mark.parametrize("c, layer", [
    (_cls("Owner", annotations=["Entity", "Table"]), "model"),                          # 모델 어노테이션
    (_cls("OwnerRepository", supers=[("extends", "JpaRepository")]), "repository"),     # 슈퍼타입
    (_cls("VetRepository", supers=[("extends", "Repository")]), "repository"),
    (_cls("PetValidator", supers=[("implements", "Validator")]), "web"),
    (_cls("BillingService"), "service"),                                                # 이름 접미사
    (_cls("OrderDao"), "repository"),
    (_cls("Helper"), "unclassified"),                                                   # 추정 근거 없음
    (_cls("OrderEntity", package="x.app.entity"), "model"),                             # 패키지 별칭
    (_cls("OrderController", package="x.app.controller"), "web"),
    (_cls("OrderService", annotations=["Repository"]), "repository"),                   # ① 어노테이션이 이름보다 우선
])
def test_계층_분류는_관례를_추정하되_기존_우선순위를_지킨다(monkeypatch, c, layer):
    monkeypatch.setattr(tools, "BASE_PKG", "x.app")
    assert tools._classify(c) == layer


def test_record_선언도_클래스로_파싱한다():
    m = tools._CLASS_RE.search("package a;\n\npublic record Point(int x, int y) implements Shape {\n}\n")
    assert m and (m.group(1), m.group(2)) == ("record", "Point")


def test_계층_도표에_DB_노드가_있고_web에서_repository로_직접_가지_않는다():
    g = tools.build_dependency_graph()
    assert "DB" in g["nodes"] and "repository --> DB" in g["mermaid"]
    pairs = {(e["from"], e["to"]) for e in g["edges"]}
    assert ("web", "service") in pairs and ("service", "repository") in pairs
    assert ("web", "repository") not in pairs


def test_기계_생성_도표는_검증을_통과한다():
    for kwargs in ({}, {"level": "class"}, {"scope": "web"}):
        assert tools.validate_diagram(tools.build_dependency_graph(**kwargs)["mermaid"])["ok"], kwargs


@pytest.mark.parametrize("mermaid", [
    'graph TD\n  web["웹 계층"] -->|5| service["서비스"]\n  service --> repository',   # 노드 라벨
    "graph TD\n  web --> service --> repository --> DB",                              # 체인
    "graph TD\n  web --> service\n  style web fill:#f9f\n  classDef x fill:#fff",     # 스타일 줄
])
def test_정상_도표_문법은_통과한다(mermaid):
    assert tools.validate_diagram(mermaid)["ok"]


@pytest.mark.parametrize("mermaid,missing,wrong", [
    ('graph TD\n  web["웹"] --> PaymentService["결제"]', ["PaymentService"], []),     # 라벨이 있어도 가짜 노드를 잡는다
    ("graph TD\n  web --> service --> web", [], [("service", "web")]),                 # 체인의 둘째 엣지도 검사
    ("graph TD\n  repository --> web", [], [("repository", "web")]),                   # 역방향
    ("graph TD\n  web --> DB", [], [("web", "DB")]),                                    # DB 는 repository 아래만
])
def test_잘못된_도표는_원인과_함께_잡힌다(mermaid, missing, wrong):
    """회귀: 라벨이 붙은 노드와 체인은 예전에 검증 없이 통과했다."""
    r = tools.validate_diagram(mermaid)
    assert not r["ok"]
    assert r["missing_nodes"] == missing
    assert [(w["from"], w["to"]) for w in r["wrong_edges"]] == wrong


# ---------------------------------------------------------------- 영향도 · 기능 목록
def test_find_usages_snake_case와_접근자도_찾는다():
    u = tools.find_usages("birthDate")
    assert {"birth_date", "getBirthDate", "setBirthDate"} <= set(u["searched"])
    assert u["by_layer"].get("db", 0) >= 4          # schema.sql 4종
    assert any(x["path"].endswith("JdbcPetRowMapper.java") for x in u["usages"])


def test_find_usages_주석_줄을_구분한다():
    u = tools.find_usages("ClinicService")
    comments = [x for x in u["usages"] if x["is_comment"]]
    assert u["comment_hits"] == len(comments) >= 2
    assert sum(u["by_layer"].values()) == u["total"] - u["comment_hits"]  # 계층 요약에는 주석을 뺀다


def test_extract_features_실제_엔드포인트와_맞다():
    rows = tools.extract_features()
    assert len(rows) == 18
    vet = {r["url"] for r in tools.extract_features("VetController")}
    assert vet == {"/vets", "/vets.json", "/vets.xml"}


# ---------------------------------------------------------------- 문서 저장 방어선
@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """프로젝트 루트와 output/ 을 임시 폴더로 바꿔 실제 output/ 을 건드리지 않는다."""
    monkeypatch.setattr(tools, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(tools, "OUTPUT_DIR", tmp_path / "output")
    return tmp_path


def test_승인_없이는_저장되지_않는다(sandbox):
    with pytest.raises(PermissionError):
        tools.save_doc("doc", "내용")
    assert not (sandbox / "output").exists()


def test_이름이_경로를_벗어나려_해도_output_안에만_저장된다(sandbox):
    target = tools.doc_path(r"../../evil/..\x.md")
    assert target.parent == (sandbox / "output").resolve() and target.name == "x.md"
    rel = tools.save_doc("../../evil", "본문", ["a.java"], approved=True)
    assert rel == "output/evil.md"
    assert [p.name for p in (sandbox / "output").iterdir()] == ["evil.md"]
    assert "근거 파일" in (sandbox / "output" / "evil.md").read_text(encoding="utf-8")
