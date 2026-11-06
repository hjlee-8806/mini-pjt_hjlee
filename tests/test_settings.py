"""분석 대상 설정 테스트: 소스 루트·기본 패키지·Spring XML 자동 감지, 환경변수 덮어쓰기, 위험한 대상 거부.

합성 프로젝트(임시 폴더에 만든 작은 자바 프로젝트)로 petclinic 이 아닌 구조에서도 감지가 되는지 본다.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src import settings, tools

ROOT = Path(__file__).resolve().parent.parent


def make(tmp_path: Path, files: dict[str, str]) -> Path:
    """{상대 경로: 내용} 으로 합성 프로젝트를 만든다."""
    for rel, text in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    return tmp_path


def java(pkg: str, name: str, body: str = "", imports: str = "") -> str:
    return f"package {pkg};\n{imports}\npublic class {name} {{\n{body}\n}}\n"


# ---------------------------------------------------------------- 기존 동작(petclinic)과 같은 값
def test_기본_대상은_예전_하드코딩_값과_같다():
    s = settings.detect(settings.DEFAULT_TARGET.resolve(), env={})
    t = s.target_root
    assert s.base_pkg == "org.springframework.samples.petclinic"
    assert s.source_roots == ((t / "src" / "main" / "java").resolve(),)
    assert s.index_roots == (t / "src" / "main",)
    assert {p.name for p in s.spring_xml_files} == {
        "business-config.xml", "datasource-config.xml", "mvc-core-config.xml", "mvc-view-config.xml", "tools-config.xml"}
    settings.validate_target(s)  # 예외 없음


def test_tools는_설정_모듈의_값을_그대로_쓴다():
    assert tools.TARGET_ROOT == settings.TARGET_ROOT and tools.BASE_PKG == settings.BASE_PKG
    assert tools.parse_project()["VetController"]["layer"] == "web"


# ---------------------------------------------------------------- 자동 감지 (petclinic 이 아닌 구조)
def test_Boot식_구조에서_소스_루트와_기본_패키지를_찾고_테스트_빌드는_제외한다(tmp_path):
    make(tmp_path, {
        "src/main/java/com/acme/shop/ShopApplication.java": java("com.acme.shop", "ShopApplication"),
        "src/main/java/com/acme/shop/order/Order.java": java("com.acme.shop.order", "Order"),
        "src/test/java/com/acme/shop/OrderTest.java": java("com.acme.shop", "OrderTest"),
        "build/generated/Gen.java": java("gen", "Gen"),
        "target/classes/Old.java": java("old", "Old"),
    })
    s = settings.detect(tmp_path, env={})
    assert s.source_roots == (tmp_path / "src" / "main" / "java",)
    assert s.base_pkg == "com.acme.shop"
    assert s.index_roots == (tmp_path / "src" / "main",)
    assert s.spring_xml_files == ()


def test_멀티_모듈은_소스_루트가_여러_개다(tmp_path):
    make(tmp_path, {
        "a/src/main/java/com/acme/a/A.java": java("com.acme.a", "A"),
        "b/src/main/java/com/acme/b/B.java": java("com.acme.b", "B"),
    })
    s = settings.detect(tmp_path, env={})
    assert s.source_roots == (tmp_path / "a/src/main/java", tmp_path / "b/src/main/java")
    assert s.base_pkg == "com.acme"
    assert s.index_roots == (tmp_path / "a/src/main", tmp_path / "b/src/main")


def test_표준이_아닌_폴더_구조도_package_선언으로_루트를_찾는다(tmp_path):
    make(tmp_path, {"src/com/acme/App.java": java("com.acme", "App"),
                    "src/com/acme/util/U.java": java("com.acme.util", "U")})
    s = settings.detect(tmp_path, env={})
    assert s.source_roots == (tmp_path / "src",)
    assert s.base_pkg == "com.acme"
    assert s.index_roots == (tmp_path,)   # src/main 이 없으면 대상 폴더 전체


def test_공통_접두사가_너무_짧으면_가장_흔한_앞_3단계를_쓴다(tmp_path):
    make(tmp_path, {"src/org/a/x/One.java": java("org.a.x", "One"), "src/org/a/x/sub/Two.java": java("org.a.x.sub", "Two"),
                    "src/org/b/y/Three.java": java("org.b.y", "Three")})
    assert settings.detect(tmp_path, env={}).base_pkg == "org.a.x"


def test_Spring_XML은_루트가_beans인_파일만_고른다(tmp_path):
    make(tmp_path, {
        "src/main/java/a/A.java": java("a", "A"),
        "src/main/resources/ctx.xml": '<?xml version="1.0"?>\n<beans xmlns="http://www.springframework.org/schema/beans"></beans>',
        "src/main/resources/logback.xml": "<configuration></configuration>",
        "pom.xml": "<project></project>",
        "target/classes/old-ctx.xml": "<beans></beans>",
    })
    assert [p.name for p in settings.detect(tmp_path, env={}).spring_xml_files] == ["ctx.xml"]


def test_환경변수가_자동_감지보다_우선한다(tmp_path):
    make(tmp_path, {"src/main/java/com/acme/A.java": java("com.acme", "A"), "alt/java/org/x/B.java": java("org.x", "B")})
    s = settings.detect(tmp_path, env={"BASE_PKG": "my.pkg", "TARGET_NAME": "내 프로젝트", "SOURCE_ROOTS": "alt/java"})
    assert (s.base_pkg, s.target_name) == ("my.pkg", "내 프로젝트")
    assert s.source_roots == ((tmp_path / "alt/java").resolve(),)


# ---------------------------------------------------------------- 대상 경로 해석과 인덱스 분리
def test_TARGET_ROOT_해석(tmp_path):
    assert settings.resolve_target({}) == settings.DEFAULT_TARGET.resolve()
    assert settings.resolve_target({"TARGET_ROOT": str(tmp_path)}) == tmp_path.resolve()
    assert settings.resolve_target({"TARGET_ROOT": "data/x"}) == (settings.PROJECT_ROOT / "data" / "x").resolve()


def test_대상이_다르면_인덱스_폴더도_다르다(tmp_path):
    a, b = tmp_path / "proj", tmp_path / "other" / "proj"   # 이름이 같아도 경로가 다르면 달라야 한다
    assert settings.index_dir_for(a) != settings.index_dir_for(b)
    assert settings.index_dir_for(a) == settings.index_dir_for(a)
    assert settings.index_dir_for(a).parent == settings.CACHE_DIR and "proj" in settings.index_dir_for(a).name


# ---------------------------------------------------------------- 위험한 대상 거부
def _fake(target: Path, **kw) -> settings.Settings:
    base = dict(target_root=target, target_name="x", source_roots=(target,), base_pkg="a.b",
                index_roots=(target,), spring_xml_files=(), index_dir=target)
    return settings.Settings(**{**base, **kw})


@pytest.mark.parametrize("target", [settings.PROJECT_ROOT, settings.PROJECT_ROOT.parent])
def test_이_프로젝트_자신이거나_상위_폴더는_대상으로_거부한다(target):
    with pytest.raises(ValueError, match="자신이거나 상위"):
        settings.validate_target(_fake(target))


def test_없는_폴더는_거부한다(tmp_path):
    with pytest.raises(ValueError, match="TARGET_ROOT"):
        settings.validate_target(_fake(tmp_path / "없음"))


def test_자바_소스가_없으면_해결_방법과_함께_거부한다(tmp_path):
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="SOURCE_ROOTS"):
        settings.validate_target(settings.detect(tmp_path, env={}))


def test_기본_패키지를_못_정하면_거부한다(tmp_path):
    with pytest.raises(ValueError, match="BASE_PKG"):
        settings.validate_target(_fake(tmp_path, base_pkg=""))


# ---------------------------------------------------------------- 환경변수로 대상을 바꿔 도구까지 동작하는지 (통합)
def test_환경변수_TARGET_ROOT만_바꿔도_도구가_그_프로젝트를_분석한다(tmp_path):
    make(tmp_path, {
        "src/main/java/com/acme/shop/web/OrderController.java": java(
            "com.acme.shop.web", "OrderController", imports="import com.acme.shop.service.OrderService;",
            body='  @GetMapping("/orders")\n  public String list() { return "orders"; }').replace("public class", "@Controller\npublic class"),
        "src/main/java/com/acme/shop/service/OrderService.java": java("com.acme.shop.service", "OrderService").replace("public class", "@Service\npublic class"),
    })
    code = ("import json; from src import tools, settings; g = tools.build_dependency_graph(); "
            "print(json.dumps({'pkg': tools.BASE_PKG, 'name': settings.TARGET_NAME, "
            "'layers': {c['name']: c['layer'] for c in tools.list_structure()}, "
            "'edges': [[e['from'], e['to']] for e in g['edges']], 'urls': [r['url'] for r in tools.extract_features()]}))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                         env={**os.environ, "TARGET_ROOT": str(tmp_path), "PYTHONUTF8": "1"})
    assert out.returncode == 0, out.stderr[-500:]
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert r["pkg"] == "com.acme.shop" and r["name"] == tmp_path.name
    assert r["layers"] == {"OrderController": "web", "OrderService": "service"}
    assert ["web", "service"] in r["edges"] and r["urls"] == ["/orders"]


def test_주석과_문자열_속_이름은_참조로_세지_않는다():
    code = tools._strip_java('class A { /* Order */ // Order\n String s = "Order \\" Order"; Foo f; }')
    assert "Order" not in code and "Foo" in code


def test_기능_단위_패키지는_import_없이도_같은_패키지_참조를_계층_간선으로_잡는다(tmp_path):
    """Spring Boot 식(owner/ 한 패키지에 Controller·Repository·Entity)은 import 가 없어 예전에는 간선이 사라졌다."""
    pkg = "com.acme.shop.order"
    make(tmp_path, {
        "src/main/java/com/acme/shop/order/OrderController.java": java(
            pkg, "OrderController", body="  private OrderRepository repo; private Order order; // Ghost 는 주석",
        ).replace("public class", "@Controller\npublic class"),
        "src/main/java/com/acme/shop/order/OrderRepository.java": java(pkg, "OrderRepository").replace(
            "public class", "public interface").replace("OrderRepository {", "OrderRepository extends JpaRepository {"),
        "src/main/java/com/acme/shop/order/Order.java": java(pkg, "Order").replace("public class", "@Entity\npublic class"),
        "src/main/java/com/acme/shop/order/Ghost.java": java(pkg, "Ghost"),
        "src/main/java/com/acme/shop/ShopApplication.java": java("com.acme.shop", "ShopApplication"),  # 루트 패키지가 base 가 되게
        "src/main/resources/templates/orders.html": "<td th:text=\"${order.total}\">total</td>",
    })
    code = ("import json; from src import tools; g = tools.build_dependency_graph(); "
            "print(json.dumps({'layers': {c['name']: c['layer'] for c in tools.list_structure()}, "
            "'edges': sorted([e['from'], e['to']] for e in g['edges']), "
            "'usage_layers': tools.find_usages('total')['by_layer']}))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                         env={**os.environ, "TARGET_ROOT": str(tmp_path), "PYTHONUTF8": "1"})
    assert out.returncode == 0, out.stderr[-500:]
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert r["layers"] == {"OrderController": "web", "OrderRepository": "repository", "Order": "model",
                           "Ghost": "unclassified", "ShopApplication": "common"}
    assert ["web", "repository"] in r["edges"] and ["web", "model"] in r["edges"] and ["repository", "model"] not in r["edges"]
    assert not any("unclassified" in e for e in r["edges"])        # 주석 속 Ghost 는 참조가 아니다
    assert r["usage_layers"] == {"view": 1}                         # .html 템플릿도 영향도에 잡힌다
