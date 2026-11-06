"""도메인 도구 (SERVICE.md 3-2).

정적 분석 도구는 LLM 없이 정규식·XML 파싱으로 동작한다.
- read_file, list_structure, build_dependency_graph, find_usages,
  extract_features, validate_diagram : 읽기 전용
- save_doc : 쓰기. 사용자 승인(HITL) 결과(approved=True)가 없으면 거부한다.
- search_code (RAG) 는 src/retriever.py 에서 제공한다.

분석 대상은 TARGET_ROOT(환경변수 TARGET_ROOT, 기본값은 src/settings.py) 하위로 제한한다. 경로·소스 루트·기본 패키지는 src/settings.py 가 정한다.
"""
from __future__ import annotations

import collections
import functools
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from src import settings

PROJECT_ROOT = settings.PROJECT_ROOT
TARGET_ROOT = settings.TARGET_ROOT      # 분석 대상 (환경변수 TARGET_ROOT, 기본값은 src/settings.py)
OUTPUT_DIR = PROJECT_ROOT / "output"
BASE_PKG = settings.BASE_PKG            # 자동 감지(모든 package 선언의 공통 접두사) 또는 환경변수 BASE_PKG

READABLE_EXT = {".java", ".xml", ".jsp", ".html", ".sql", ".properties", ".tag"}

# ---------------------------------------------------------------- 계층 분류 규칙
# 우선순위: ① 어노테이션 → ② 패키지 폴더 → ③ 공통/설정 규칙 → ④ 관례 추정(모델 어노테이션·슈퍼타입·이름 접미사) → ⑤ unclassified
LAYER_BY_ANNOTATION = {"Controller": "web", "RestController": "web",
                       "Service": "service", "Repository": "repository"}
LAYER_BY_PACKAGE = {"web": "web", "service": "service",
                    "repository": "repository", "model": "model", "util": "common",
                    # 패키지 별칭 (다른 프로젝트의 관례)
                    "controller": "web", "dao": "repository", "entity": "model", "domain": "model", "config": "common"}
COMMON_ANNOTATIONS = {"Aspect", "Configuration", "ControllerAdvice"}
COMMON_SUPERTYPE_SUFFIX = ("Initializer",)  # 예: AbstractDispatcherServletInitializer
# ④ 관례 추정: 계층 어노테이션·패키지가 없는 기능 단위 패키지(owner/, vet/ 등)용. 위 ①~③ 에서 못 정한 클래스에만 적용한다.
MODEL_ANNOTATIONS = {"Entity", "MappedSuperclass", "Embeddable", "Document", "XmlRootElement"}
LAYER_BY_SUPERTYPE_SUFFIX = (("Repository", "repository"), ("Formatter", "web"), ("Validator", "web"))
LAYER_BY_NAME_SUFFIX = (("Controller", "web"), ("Service", "service"), ("Repository", "repository"),
                        ("Dao", "repository"), ("Mapper", "repository"))
LAYER_ORDER = ["web", "service", "repository", "model", "common", "unclassified"]


# ---------------------------------------------------------------- 공통 유틸
def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _safe_path(path: str) -> Path:
    """TARGET_ROOT 밖(.env, ../ 우회, 절대경로) 접근을 차단한다."""
    p = Path(path)
    p = p if p.is_absolute() else TARGET_ROOT / p
    p = p.resolve()
    if p != TARGET_ROOT.resolve() and TARGET_ROOT.resolve() not in p.parents:
        raise PermissionError(f"분석 대상 경로 밖은 접근할 수 없습니다: {path}")
    if p.suffix.lower() not in READABLE_EXT and p.is_file():
        raise PermissionError(f"허용되지 않는 파일 형식입니다: {p.suffix}")
    return p


def _rel(p: Path) -> str:
    return p.resolve().relative_to(TARGET_ROOT.resolve()).as_posix()


# ---------------------------------------------------------------- 파싱
_CLASS_RE = re.compile(
    r"^(?:public\s+)?(?:abstract\s+|final\s+)*(class|interface|enum|record)\s+(\w+)([^{]*)\{",
    re.M)
_METHOD_RE = re.compile(
    r"^(?: {4}|\t)(?:(?:public|protected|private|static|final|abstract|synchronized)\s+)*"
    r"(?:<[^>]+>\s+)?[\w<>\[\],.? ]+?\s+(\w+)\s*\([^;{)]*(?:\)[^;{]*)?[{;]\s*$", re.M)
_NOT_METHOD = {"if", "for", "while", "switch", "catch", "return", "new", "else", "try"}


@functools.lru_cache(maxsize=1)
def parse_project() -> dict:
    """Java 소스를 한 번 파싱해 캐시한다. list_structure·그래프·영향도가 공유한다."""
    classes: dict[str, dict] = {}
    codes: dict[str, str] = {}
    for f in sorted(f for root in settings.SOURCE_ROOTS for f in root.rglob("*.java")):
        src = _read(f)
        pkg_m = re.search(r"^package\s+([\w.]+);", src, re.M)
        cls_m = _CLASS_RE.search(src)
        if not pkg_m or not cls_m:  # package-info.java 등
            continue
        kind, name, header = cls_m.group(1), cls_m.group(2), cls_m.group(3)
        before = src[:cls_m.start()]
        annotations = re.findall(r"^@(\w+)", before, re.M)
        line_of = lambda pos: src.count("\n", 0, pos) + 1  # noqa: E731

        methods = []
        for m in _METHOD_RE.finditer(src, cls_m.end()):
            if m.group(1) in _NOT_METHOD:
                continue
            methods.append({"name": m.group(1), "line": line_of(m.start())})

        supers = []
        for rel_kind, targets in re.findall(r"\b(implements|extends)\s+([\w<>, .]+)", header):
            for t in re.findall(r"[A-Za-z_]\w*", re.sub(r"<[^>]*>", "", targets)):
                supers.append((rel_kind, t))

        imports = re.findall(
            rf"^import\s+{re.escape(BASE_PKG)}\.[\w.]*?(\w+);", src, re.M)

        classes[name] = {
            "name": name, "kind": kind, "package": pkg_m.group(1),
            "file": _rel(f), "line": line_of(cls_m.start()),
            "annotations": annotations, "methods": methods,
            "supers": supers, "imports": imports,
        }
        codes[name] = _strip_java(src)
    for c in classes.values():
        c["layer"] = _classify(c)
        # 같은 패키지 클래스는 import 없이 쓰므로(기능 단위 패키지 구조) 코드 속 이름 사용으로 참조를 추정한다
        c["same_pkg_refs"] = [o["name"] for o in classes.values()
                              if o is not c and o["package"] == c["package"]
                              and o["name"] not in {t for _, t in c["supers"]}   # 상속·구현은 이미 따로 표시된다
                              and re.search(rf"\b{re.escape(o['name'])}\b", codes[c["name"]])]
    return classes


def _strip_java(src: str) -> str:
    """주석과 문자열 리터럴을 지운 코드. 같은 패키지 참조 추정에서 주석·문구 속 이름을 세지 않기 위해 쓴다."""
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"//[^\n]*", " ", src)
    return re.sub(r'"(?:\\.|[^"\\\n])*"', '""', src)


def _classify(c: dict) -> str:
    # ① 어노테이션
    for a in c["annotations"]:
        if a in LAYER_BY_ANNOTATION:
            return LAYER_BY_ANNOTATION[a]
    # ② 패키지 폴더 (base 패키지 바로 아래 첫 단계)
    sub = c["package"].replace(BASE_PKG, "").lstrip(".").split(".")[0]
    if sub in LAYER_BY_PACKAGE:
        return LAYER_BY_PACKAGE[sub]
    # ③ 공통/설정: 루트 패키지의 설정 클래스, @Aspect 등, *Initializer 상속
    if sub == "" or set(c["annotations"]) & COMMON_ANNOTATIONS:
        return "common"
    if any(t.endswith(COMMON_SUPERTYPE_SUFFIX) for _, t in c["supers"]):
        return "common"
    # ④ 관례 추정
    if set(c["annotations"]) & MODEL_ANNOTATIONS:
        return "model"
    for suffix, layer in LAYER_BY_SUPERTYPE_SUFFIX:
        if any(t.endswith(suffix) for _, t in c["supers"]):
            return layer
    for suffix, layer in LAYER_BY_NAME_SUFFIX:
        if c["name"].endswith(suffix):
            return layer
    # ⑤ 그 외
    return "unclassified"


@functools.lru_cache(maxsize=1)
def parse_profiles() -> dict:
    """Spring XML 의 profile 별 component-scan/jpa:repositories 대상 패키지를 읽는다.
    반환: {"jdbc": ["...repository.jdbc"], "jpa": [...], "spring-data-jpa": [...]}"""
    result: dict[str, list[str]] = collections.defaultdict(list)
    for xml in settings.SPRING_XML_FILES:
        try:
            root = ET.fromstring(_read(xml))
        except ET.ParseError:
            continue

        def walk(el, profiles):
            tag = el.tag.split("}")[-1]
            if tag == "beans" and el.get("profile"):
                profiles = [p.strip() for p in re.split(r"[,; ]", el.get("profile")) if p.strip()]
            if tag in ("component-scan", "repositories") and el.get("base-package"):
                for p in profiles or ["(default)"]:
                    result[p].append(el.get("base-package"))
            for ch in el:
                walk(ch, profiles)

        walk(root, [])
    return dict(result)


def _profile_of(c: dict) -> str | None:
    for prof, pkgs in parse_profiles().items():
        if prof != "(default)" and any(c["package"] == p or c["package"].startswith(p + ".") for p in pkgs):
            return prof
    return None


# ---------------------------------------------------------------- 도구
def read_file(path: str, start: int = 1, end: int | None = None) -> str:
    """파일의 start~end 라인(1-base)을 '라인번호: 내용' 형태로 반환."""
    p = _safe_path(path)
    if not p.is_file():
        raise FileNotFoundError(path)
    lines = _read(p).splitlines()
    end = min(end or len(lines), len(lines), start + 399)  # 최대 400줄
    return "\n".join(f"{i}: {lines[i - 1]}" for i in range(max(start, 1), end + 1))


def list_structure(scope: str | None = None) -> list[dict]:
    """클래스·메서드·어노테이션·라인 목록. scope 는 클래스명 또는 패키지/계층명 일부."""
    out = []
    for c in parse_project().values():
        if scope and scope not in (c["name"], c["layer"]) and scope not in c["package"]:
            continue
        out.append({k: c[k] for k in ("name", "kind", "layer", "package", "file",
                                      "line", "annotations", "methods")})
    return out


def _class_edges(classes: dict) -> set[tuple[str, str, str]]:
    edges = set()
    for c in classes.values():
        for imp in c["imports"] + c.get("same_pkg_refs", []):
            edges.add((c["name"], imp, "uses"))
        for kind, t in c["supers"]:
            edges.add((c["name"], t, "impl" if kind == "implements" else "extends"))
    return {e for e in edges if e[0] in classes and e[1] in classes and e[0] != e[1]}


def build_dependency_graph(scope: str | None = None, level: str = "layer") -> dict:
    """의존성 그래프. level='layer'(기본) 는 계층 단위, 'class' 는 클래스 단위.
    반환: {"nodes": [...], "edges": [...], "mermaid": str, "profiles": {...}}"""
    classes = parse_project()
    if scope:
        keep = {x["name"] for x in list_structure(scope)}
        classes = {k: v for k, v in classes.items() if k in keep}
    edges = _class_edges(classes)
    profiles = parse_profiles()

    if level == "class":
        nodes = sorted(classes)
        lines = ["graph TD"]
        for layer in LAYER_ORDER:
            members = sorted(n for n in nodes if classes[n]["layer"] == layer)
            if members:
                lines.append(f"  subgraph {layer}")
                lines += [f"    {m}" for m in members]
                lines.append("  end")
        arrow = {"uses": "-->", "impl": "-.->|impl|", "extends": "-->|extends|"}
        lines += [f"  {a} {arrow[k]} {b}" for a, b, k in sorted(edges)]
        return {"nodes": nodes,
                "edges": [{"from": a, "to": b, "type": k} for a, b, k in sorted(edges)],
                "mermaid": "\n".join(lines), "profiles": profiles}

    agg: collections.Counter = collections.Counter()
    for a, b, _ in edges:
        la, lb = classes[a]["layer"], classes[b]["layer"]
        if la != lb:
            agg[(la, lb)] += 1
    layers = sorted({l for pair in agg for l in pair}, key=LAYER_ORDER.index)
    lines = ["graph TD"] + [f"  {a} -->|{n}| {b}" for (a, b), n in sorted(agg.items())]
    has_db = "repository" in layers
    if has_db:  # 영속 계층 아래의 DB 노드 (SERVICE.md 3-6: web → service → repository → DB)
        lines.append("  repository --> DB")
    # repository 구현체는 프로필별 선택 노드로 별도 표현
    prof_classes: dict[str, list[str]] = collections.defaultdict(list)
    for c in classes.values():
        pr = _profile_of(c)
        if pr and c["layer"] == "repository":
            prof_classes[pr].append(c["name"])
    for pr in sorted(prof_classes):
        node = "profile_" + pr.replace("-", "_")
        lines.append(f"  repository -.->|profile| {node}[\"{pr}\"]")
    return {"nodes": layers + (["DB"] if has_db else []) + [f"profile:{p}" for p in sorted(prof_classes)],
            "edges": [{"from": a, "to": b, "count": n} for (a, b), n in sorted(agg.items())],
            "mermaid": "\n".join(lines), "profiles": dict(prof_classes)}


_COMMENT_PREFIXES = ("*", "//", "/*", "<!--", "--", "#")  # 주석으로 보는 줄 시작 (java·xml·jsp·sql·properties)


def _symbol_variants(symbol: str) -> list[str]:
    """심볼이 코드·DB·화면에서 쓰이는 다른 표기들. 영향도에서 놓치기 쉬운 표기를 함께 찾는다.
    - snake_case : birthDate → birth_date  (DB 컬럼, JDBC 매핑)
    - 접근자      : birthDate → getBirthDate / setBirthDate / isBirthDate  (소문자로 시작하는 이름만)"""
    variants = [symbol]
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", symbol).lower()
    if snake != symbol.lower():
        variants.append(snake)
    if symbol[:1].islower() and "_" not in symbol:
        cap = symbol[0].upper() + symbol[1:]
        variants += [f"get{cap}", f"set{cap}", f"is{cap}"]
    return variants


def find_usages(symbol: str) -> dict:
    """심볼(클래스·메서드·필드명)이 등장하는 위치와 계층별 요약. 단어 경계 기준.
    snake_case(DB 컬럼)·getter/setter 표기도 함께 찾고(searched 참고), 주석 줄은 is_comment=true 로 표시한다.
    by_layer 는 주석을 뺀 실제 참조의 계층별 개수, comment_hits 는 주석 속 언급 수이다."""
    variants = _symbol_variants(symbol)
    pat = re.compile(r"\b(?:" + "|".join(re.escape(v) for v in variants) + r")\b")
    classes = parse_project()
    file_layer = {c["file"]: c["layer"] for c in classes.values()}
    hits = []
    for f in sorted({p for root in settings.INDEX_ROOTS for p in root.rglob("*")}):
        if f.suffix.lower() not in {".java", ".xml", ".jsp", ".html", ".tag", ".sql"}:
            continue
        for i, line in enumerate(_read(f).splitlines(), 1):
            if pat.search(line):
                rel = _rel(f)
                layer = file_layer.get(rel) or {".jsp": "view", ".tag": "view", ".html": "view",
                                                ".xml": "config", ".sql": "db"}.get(f.suffix.lower(), "unclassified")
                hits.append({"path": rel, "line": i, "layer": layer, "text": line.strip()[:160],
                             "is_comment": line.strip().startswith(_COMMENT_PREFIXES)})
    summary = collections.Counter(h["layer"] for h in hits if not h["is_comment"])
    return {"symbol": symbol, "searched": variants, "total": len(hits),
            "comment_hits": sum(h["is_comment"] for h in hits),
            "by_layer": dict(summary), "usages": hits}


_MAPPING_RE = re.compile(
    r"@(RequestMapping|GetMapping|PostMapping|PutMapping|DeleteMapping)\s*"
    r"(?:\(([^)]*)\))?")


def extract_features(scope: str | None = None) -> list[dict]:
    """컨트롤러의 매핑 어노테이션에서 기능 목록(URL·HTTP·메서드·뷰)을 뽑는다."""
    rows = []
    for c in parse_project().values():
        if c["layer"] != "web" or (scope and scope not in (c["name"], c["package"])):
            continue
        lines = _read(TARGET_ROOT / c["file"]).splitlines()
        # 클래스 수준 prefix
        prefix = ""
        for ln in lines[:c["line"]]:
            m = _MAPPING_RE.search(ln)
            if m:
                prefix = _paths(m.group(2))[0] if _paths(m.group(2)) else ""
        for i in range(c["line"], len(lines)):
            if lines[i].lstrip().startswith(("*", "//", "/*")):  # 주석 속 어노테이션 제외
                continue
            m = _MAPPING_RE.search(lines[i])
            if not m:
                continue
            http = {"GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
                    "DeleteMapping": "DELETE"}.get(m.group(1))
            if http is None:
                hm = re.search(r"RequestMethod\.(\w+)", m.group(2) or "")
                http = hm.group(1) if hm else "ANY"
            # 다음 메서드 선언과 그 본문에서 view 반환값 탐색
            method, view = None, []
            for j in range(i + 1, min(i + 60, len(lines))):
                if _MAPPING_RE.search(lines[j]) and not lines[j].lstrip().startswith(("*", "//")):
                    break
                decl = lines[j] + (" " + lines[j + 1] if lines[j].strip() in ("public", "protected") and j + 1 < len(lines) else "")
                mm = re.match(r"\s+(?:public|protected)\s+(?:@\w+\s+)*[\w<>\[\], ?]+\s+(\w+)\s*\(", decl)
                if mm and method is None:
                    method = mm.group(1)
                view += re.findall(r'return\s+"([^"]+)"', lines[j])
                view += re.findall(r'ModelAndView\(\s*"([^"]+)"', lines[j])
            for path in _paths(m.group(2)) or [""]:
                rows.append({"controller": c["name"], "method": method, "http": http,
                             "url": prefix + path, "views": sorted(set(view)),
                             "file": c["file"], "line": i + 1})
    return rows


def _paths(args: str | None) -> list[str]:
    if not args:
        return []
    m = re.search(r"(?:value|path)\s*=\s*\{([^}]*)\}", args) or \
        re.search(r"(?:value|path)\s*=\s*(\"[^\"]*\")", args) or \
        re.match(r'\s*(\{[^}]*\}|"[^"]*")', args)
    return re.findall(r'"([^"]*)"', m.group(1)) if m else []


# 노드 모양(라벨) 표기 제거용: id["라벨"], id[라벨], id(라벨), id{라벨}
_SHAPE_RE = re.compile(r"\[\[?[^\]]*\]?\]|\(\(?[^)]*\)?\)|\{[^}]*\}")
# 엣지 구분: 화살표(+ 선택 라벨). re.split 결과는 [노드, 화살표, 라벨, 노드, 화살표, 라벨, 노드 ...]
_ARROW_SPLIT_RE = re.compile(r"(-->|-\.->|==>|---)(?:\|([^|]*)\|)?")
_NON_EDGE_LINE_RE = re.compile(r"^\s*(?:graph|flowchart|subgraph|end\b|style|classDef|class\b|click|linkStyle|%%)")
EXTRA_DIAGRAM_NODES = {"DB"}                 # 계층 도표의 DB 노드 (repository 아래)
EXTRA_DIAGRAM_EDGES = {("repository", "DB")}  # 계층 도표에서 허용하는 DB 엣지


def _diagram_edges(mermaid: str) -> tuple[list[tuple[str, str, str]], set[str]]:
    """Mermaid 에서 (from, to, 라벨) 엣지 목록과 엣지에 쓰인 노드 id 집합을 뽑는다.
    라벨 표기와 체인(a --> b --> c), 'A & B' 묶음을 처리한다."""
    edges, nodes = [], set()
    for raw in mermaid.splitlines():
        if _NON_EDGE_LINE_RE.match(raw):
            continue
        parts = _ARROW_SPLIT_RE.split(_SHAPE_RE.sub("", raw))
        if len(parts) < 4:
            continue
        sides = [re.findall(r"[A-Za-z_]\w*", parts[i]) for i in range(0, len(parts), 3)]
        labels = [parts[i] or "" for i in range(2, len(parts), 3)]
        for k, label in enumerate(labels):
            for a in sides[k]:
                for b in sides[k + 1]:
                    edges.append((a, b, label.strip()))
        nodes.update(n for s in sides for n in s)
    return edges, nodes


def validate_diagram(mermaid: str) -> dict:
    """Mermaid 의 노드·엣지가 실제 구조에 근거하는지 검사한다.
    노드는 실제 클래스·계층명(또는 DB·profile_*)이어야 하고, 엣지는 추출된 의존에 있어야 한다.
    반환: {ok, missing_nodes, wrong_edges}"""
    classes = parse_project()
    class_edges = {(a, b) for a, b, _ in _class_edges(classes)}
    layer_edges = {(classes[a]["layer"], classes[b]["layer"]) for a, b in class_edges
                   if classes[a]["layer"] != classes[b]["layer"]} | EXTRA_DIAGRAM_EDGES
    known_nodes = set(classes) | set(LAYER_ORDER) | EXTRA_DIAGRAM_NODES | {
        "profile_" + p.replace("-", "_") for p in parse_profiles()}
    keywords = {"graph", "TD", "LR", "subgraph", "end", "TB", "BT", "RL"}

    edges, edge_nodes = _diagram_edges(mermaid)
    missing = {n for n in edge_nodes if n not in known_nodes and n not in keywords}
    wrong = []
    for a, b, label in edges:
        if a in known_nodes and b in known_nodes and not (
                (a, b) in class_edges or (a, b) in layer_edges
                or label == "profile" or b.startswith("profile_")):
            if {"from": a, "to": b} not in wrong:
                wrong.append({"from": a, "to": b})
    for line in mermaid.splitlines():  # 엣지가 없는 단독 노드 선언 (모양 표기 제거 후)
        s = _SHAPE_RE.sub("", line).strip()
        if re.fullmatch(r"[A-Za-z_]\w*", s) and s not in known_nodes and s not in keywords:
            missing.add(s)
    return {"ok": not missing and not wrong,
            "missing_nodes": sorted(missing), "wrong_edges": wrong}


def doc_path(name: str) -> Path:
    """저장될 문서의 절대 경로. 이름에서 디렉터리·확장자를 버리고 output/<안전한 이름>.md 로 만든다."""
    stem = re.sub(r"[^\w\-가-힣]", "_", Path(name).stem)[:60] or "analysis"
    target = (OUTPUT_DIR / f"{stem}.md").resolve()
    if OUTPUT_DIR.resolve() not in target.parents:
        raise PermissionError("output/ 밖에는 저장할 수 없습니다.")
    return target


def save_doc(name: str, content: str, sources: list[str] | None = None,
             approved: bool = False) -> str:
    """분석 문서를 output/ 에 저장한다. 사용자 승인(approved=True) 없이는 쓰지 않는다.
    승인 화면(interrupt)은 에이전트 그래프에서 처리하고, 이 함수는 마지막 방어선이다."""
    if not approved:
        raise PermissionError("사용자 승인(HITL) 없이는 문서를 저장할 수 없습니다.")
    OUTPUT_DIR.mkdir(exist_ok=True)
    target = doc_path(name)
    body = content.rstrip() + "\n"
    if sources:
        body += "\n---\n근거 파일:\n" + "\n".join(f"- {s}" for s in sources) + "\n"
    target.write_text(body, encoding="utf-8")
    return str(target.relative_to(PROJECT_ROOT).as_posix())
