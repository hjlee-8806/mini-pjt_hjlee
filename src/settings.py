"""분석 대상 설정: 대상 경로, 자바 소스 루트, 기본 패키지, 인덱스 위치를 한곳에서 정한다.

환경변수로 대상을 바꾼다. (한 서버 프로세스 = 한 대상)
  TARGET_ROOT   분석할 프로젝트 폴더. 기본 data/petclinic-legacy. 상대 경로는 이 프로젝트 루트 기준
  TARGET_NAME   안내문·프롬프트에 쓸 이름. 기본은 폴더 이름
  SOURCE_ROOTS  자바 소스 루트를 직접 지정 (세미콜론 구분, 대상 폴더 기준 상대 경로). 기본은 자동 감지
  BASE_PKG      기본 패키지를 직접 지정. 기본은 모든 package 선언의 공통 접두사

자동 감지가 하는 일 (Maven·Gradle 표준 구조 가정에 기대지 않는다)
  - 소스 루트: .java 파일의 package 선언과 폴더 경로를 맞춰 보고 package 폴더 바로 위를 루트로 본다.
    test·build·target 등은 제외한다. (모듈이 여럿이면 루트도 여럿)
  - 기본 패키지: 소스의 모든 package 선언의 공통 접두사. (너무 짧으면 가장 흔한 앞 3단계)
  - 인덱스 대상: 'src/main' 폴더들. 없으면 대상 폴더 전체
  - Spring XML 설정: 루트 요소가 <beans> 인 .xml 파일

점검: python -m src.settings
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGET = PROJECT_ROOT / "data" / "petclinic-legacy"
CACHE_DIR = PROJECT_ROOT / "data" / "index"   # 임베딩 캐시는 대상과 무관하게 공유한다 (텍스트 해시가 키)

# 탐색에서 건너뛰는 폴더 이름. 패키지 폴더가 이 이름이면(예: com.acme.build) 놓칠 수 있다.
EXCLUDED_DIRS = {".git", ".svn", ".idea", ".vscode", ".gradle", ".mvn", "node_modules", "target", "build",
                 "out", "bin", "test", "tests", "__pycache__"}
PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
BEANS_RE = re.compile(r"<beans[\s>]")
HEAD_BYTES = 20000          # package 선언을 찾기 위해 읽는 앞부분 크기 (긴 라이선스 주석 대비)
MIN_PKG_SEGMENTS = 2        # 공통 접두사가 이보다 짧으면 흔한 앞 3단계로 대체


@dataclass(frozen=True)
class Settings:
    """분석 대상에 대한 설정과 감지 결과."""
    target_root: Path
    target_name: str
    source_roots: tuple[Path, ...]
    base_pkg: str
    index_roots: tuple[Path, ...]
    spring_xml_files: tuple[Path, ...]
    index_dir: Path


def _walk(root: Path):
    """제외 폴더를 건너뛰며 (폴더, 하위 폴더 목록, 파일 목록)을 돌려준다."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in EXCLUDED_DIRS)
        yield Path(dirpath), dirnames, sorted(filenames)


def _read_head(path: Path) -> str:
    with path.open("rb") as f:
        return f.read(HEAD_BYTES).decode("utf-8", errors="replace")


def detect_source_roots(target: Path) -> tuple[Path, ...]:
    """.java 파일의 package 선언과 폴더 경로를 맞춰 소스 루트를 찾는다."""
    roots: set[Path] = set()
    for folder, _, files in _walk(target):
        for name in files:
            if not name.endswith(".java"):
                continue
            m = PACKAGE_RE.search(_read_head(folder / name))
            parts = m.group(1).split(".") if m else []
            if parts and folder.parts[-len(parts):] == tuple(parts):
                roots.add(Path(*folder.parts[:-len(parts)]))
            else:
                roots.add(folder)       # 기본 패키지이거나 폴더와 package 가 맞지 않으면 그 폴더 자체
    # 다른 루트 안에 들어 있는 루트는 바깥 루트에 포함된다
    return tuple(sorted(r for r in roots if not any(o != r and o in r.parents for o in roots)))


def detect_base_package(source_roots: tuple[Path, ...]) -> str:
    """소스의 모든 package 선언의 공통 접두사. 너무 짧으면 가장 흔한 앞 3단계를 쓴다."""
    packages: list[list[str]] = []
    for root in source_roots:
        for f in root.rglob("*.java"):
            m = PACKAGE_RE.search(_read_head(f))
            if m:
                packages.append(m.group(1).split("."))
    if not packages:
        return ""
    common = packages[0]
    for p in packages[1:]:
        n = 0
        while n < min(len(common), len(p)) and common[n] == p[n]:
            n += 1
        common = common[:n]
    if len(common) >= MIN_PKG_SEGMENTS:
        return ".".join(common)
    top = Counter(".".join(p[:3]) for p in packages).most_common(1)
    return top[0][0] if top else ""


def detect_index_roots(target: Path) -> tuple[Path, ...]:
    """검색 인덱스·영향도 탐색 대상: 'src/main' 폴더들. 없으면 대상 폴더 전체."""
    found = [folder / "main" for folder, dirs, _ in _walk(target) if folder.name == "src" and "main" in dirs]
    return tuple(found) or (target,)


def detect_spring_xml(target: Path) -> tuple[Path, ...]:
    """루트 요소가 <beans> 인 Spring XML 설정 파일."""
    return tuple(folder / n for folder, _, files in _walk(target) for n in files
                 if n.endswith(".xml") and BEANS_RE.search(_read_head(folder / n)[:4000]))


def index_dir_for(target: Path) -> Path:
    """대상별 벡터 인덱스 폴더. 이름 + 경로 해시라 대상이 달라도 서로 덮어쓰지 않는다."""
    name = re.sub(r"[^\w.-]+", "_", target.name) or "target"
    return CACHE_DIR / f"{name}-{hashlib.sha1(str(target).encode('utf-8')).hexdigest()[:8]}"


def resolve_target(env: Mapping[str, str] | None = None) -> Path:
    """환경변수 TARGET_ROOT 를 절대 경로로. 없으면 기본 대상."""
    env = os.environ if env is None else env
    raw = env.get("TARGET_ROOT", "").strip()
    if not raw:
        return DEFAULT_TARGET.resolve()
    p = Path(raw).expanduser()
    return (p if p.is_absolute() else PROJECT_ROOT / p).resolve()


def detect(target: Path, env: Mapping[str, str] | None = None) -> Settings:
    """대상 폴더를 살펴 설정을 만든다. 환경변수(SOURCE_ROOTS, BASE_PKG, TARGET_NAME)가 있으면 그 값을 우선한다."""
    env = os.environ if env is None else env
    explicit = [s.strip() for s in env.get("SOURCE_ROOTS", "").split(";") if s.strip()]
    source_roots = tuple((target / s).resolve() for s in explicit) if explicit else detect_source_roots(target)
    return Settings(
        target_root=target,
        target_name=env.get("TARGET_NAME", "").strip() or target.name,
        source_roots=source_roots,
        base_pkg=env.get("BASE_PKG", "").strip() or detect_base_package(source_roots),
        index_roots=detect_index_roots(target),
        spring_xml_files=detect_spring_xml(target),
        index_dir=index_dir_for(target),
    )


def validate_target(s: Settings | None = None) -> None:
    """대상 설정이 쓸 수 있는지 검사한다. 문제가 있으면 원인과 해결 방법이 담긴 ValueError.

    - 대상은 존재하는 폴더여야 한다.
    - 이 프로젝트 자신이거나 이 프로젝트를 품은 상위 폴더면 거부한다. (인덱스에 output·logs·data 가 섞이고
      에이전트가 자기 코드를 분석 대상으로 읽게 된다)
    - 자바 소스를 하나도 못 찾으면 거부한다."""
    s = s or SETTINGS
    t = s.target_root
    if not t.is_dir():
        raise ValueError(f"분석 대상 폴더가 없습니다: {t}\n환경변수 TARGET_ROOT 를 확인하세요.")
    if t == PROJECT_ROOT or t in PROJECT_ROOT.parents:
        raise ValueError(f"분석 대상이 이 프로젝트 자신이거나 상위 폴더입니다: {t}\n"
                         "분석할 프로젝트의 폴더를 TARGET_ROOT 로 지정하세요.")
    if not s.source_roots:
        raise ValueError(f"자바 소스(.java)를 찾지 못했습니다: {t}\n"
                         "다른 폴더이거나 테스트·build 폴더에만 소스가 있을 수 있습니다. SOURCE_ROOTS 로 직접 지정할 수 있습니다.")
    if not s.base_pkg:
        raise ValueError("기본 패키지를 정하지 못했습니다. 환경변수 BASE_PKG 로 지정하세요.")


SETTINGS = detect(resolve_target())

TARGET_ROOT = SETTINGS.target_root
TARGET_NAME = SETTINGS.target_name
SOURCE_ROOTS = SETTINGS.source_roots
BASE_PKG = SETTINGS.base_pkg
INDEX_ROOTS = SETTINGS.index_roots
SPRING_XML_FILES = SETTINGS.spring_xml_files
INDEX_DIR = SETTINGS.index_dir


def describe(s: Settings | None = None) -> str:
    """감지 결과를 사람이 읽기 쉽게 출력한다."""
    s = s or SETTINGS
    rel = lambda p: p.relative_to(s.target_root).as_posix() if s.target_root in p.parents else str(p)  # noqa: E731
    lines = [f"분석 대상     : {s.target_root}", f"이름          : {s.target_name}",
             f"소스 루트     : {', '.join(rel(r) for r in s.source_roots) or '(없음)'}",
             f"기본 패키지   : {s.base_pkg or '(없음)'}",
             f"인덱스 대상   : {', '.join(rel(r) for r in s.index_roots)}",
             f"Spring XML    : {len(s.spring_xml_files)}개 " + ", ".join(p.name for p in s.spring_xml_files),
             f"인덱스 폴더   : {s.index_dir}"]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    print(describe())
    try:
        validate_target()
        print("\n검사: 사용할 수 있는 대상입니다.")
    except ValueError as e:
        print(f"\n검사 실패: {e}")
        sys.exit(1)
