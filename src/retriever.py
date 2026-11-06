"""RAG 파이프라인 (SERVICE.md 3-1, 3-2): 청킹 → 인덱스 → 하이브리드 검색 → 질의 재작성 → search_code.

임베딩 호출은 src/embeddings.py 가 맡고, 여기서는 청크를 만들고 검색 결과를 조합한다.

점검·조정 방법
- 청크와 검색 순위를 눈으로 확인:   python -m src.retriever "saveOwner 가 뭘 해?"
- 청크 통계·샘플만 보기:            python -m src.retriever --chunks
- 인덱스를 처음부터 다시 만들기:     python -m src.retriever --rebuild "질의"   (data/index/ 삭제와 동일)
- 조정 포인트는 아래 '튜닝 상수' 블록에 모여 있다. 바꾸면 청크 해시가 달라져 인덱스는 자동 재생성된다.
  (임베딩은 embeddings.py 의 캐시를 쓰므로 바뀐 청크만 새로 호출한다.)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re

import faiss
import numpy as np
from dotenv import load_dotenv
from rank_bm25 import BM25Okapi

from src import embeddings, settings
from src.tools import TARGET_ROOT, _read, parse_project

load_dotenv()

# ================================================================== 튜닝 상수
# --- 청킹 ---
JAVA_MAX_LINES = 120          # 자바 청크(메서드·클래스)가 이보다 길면 나눈다
NONJAVA_MAX_LINES = 60        # xml / jsp / sql 은 이 줄 수 단위로 나눈다
CHUNK_OVERLAP = 5             # 나눌 때 앞 청크와 겹치는 줄 수
# --- XML 설정 파일 청킹: 요소(bean·프로필 블록) 단위 ---
# 설정 파일을 60줄 창으로 통째로 임베딩하면 네임스페이스 선언·여러 설정이 섞여 '캐시'·'프로필' 같은 의미가 희석되어
# 검색에서 밀린다. (예: tools-config.xml 의 캐시 설정이 "findVets cache" 질의에서 벡터 20위 밖) 자바의 메서드 단위처럼
# 루트의 자식 요소 단위로 쪼개고, 요소 바로 위 주석을 함께 묶는다. 바꾸면 XML 청크 임베딩이 다시 만들어진다.
# [2026-11-05 A/B 결과] 기본값은 False. 설정 중심 질의("cache manager configuration")에서는 설정 청크가 1위로 잘 찾히지만
# 그것은 예전 방식도 마찬가지였고, 질문 8건 평균 recall 은 top_k=5 에서 58%→58%(변화 없음), top_k=10 에서 74%→68%(케이스 3 하락)였다.
# 케이스 6 의 실제 원인은 청크 크기가 아니라 에이전트가 메서드 이름과 'cache' 를 한 질의에 섞어 검색하는 것이다. (프롬프트로 해결)
XML_STRUCTURED = False        # True: 요소 단위 청킹. False: 예전 방식(NONJAVA_MAX_LINES 줄 고정 창)
XML_MIN_LINES = 6             # 이보다 짧은 요소는 다음 요소와 합친다 (너무 잘게 쪼개면 문맥이 사라진다)
XML_MAX_LINES = 40            # 요소가 이보다 길면 자식 요소 단위로 내려간다 (더 못 내려가면 고정 창)
CLASS_CHUNK_FROM_PACKAGE = True  # True: 클래스 청크에 package·import 포함 (의존 키워드가 BM25 에 걸린다)
INDEX_EXTS = {".java", ".xml", ".jsp", ".html", ".sql"}   # 인덱스 대상 확장자 (SERVICE.md 3-1)
INDEX_ROOTS = settings.INDEX_ROOTS               # 인덱스 대상 폴더들 (자동 감지: src/main). 테스트·빌드 산출물 제외

# --- 검색 ---
DEFAULT_TOP_K = 5
MAX_TOP_K = 20
CANDIDATES = 20               # 벡터·BM25 각각에서 뽑는 후보 수 (RRF 입력)
RRF_K = 60                    # RRF 상수. 작을수록 상위 순위에 가중이 쏠린다
VECTOR_WEIGHT = 1.0           # 하이브리드 가중치. 식별자 위주 질의가 많으면 BM25 를 올린다
BM25_WEIGHT = 1.0

# --- 질의 재작성 (결과가 약할 때만) ---
BM25_MIN_SCORE = 0.0          # BM25 최고점이 이 값 이하 = 질의 단어가 코드에 한 번도 안 나옴
VEC_MIN_SIM = 0.30            # 벡터 최고 유사도(코사인)가 이 값 미만 = 의미상으로도 먼 질의
REWRITE_MODEL_ID = os.getenv("BEDROCK_REWRITE_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
REWRITE_PROMPT = (
    "다음은 Java/Spring 소스 검색 질의입니다. 코드에서 실제로 쓰일 법한 "
    "영어 식별자(클래스·메서드·어노테이션·패키지명)와 핵심 영어 키워드만 공백으로 나열해 "
    "한 줄로 출력하세요. 설명·따옴표·번호는 쓰지 마세요. "
    "소스 코드와 무관한 질의면 NONE 만 출력하세요.\n\n질의: {query}"
)

# ================================================================== 청킹
_DECOR_RE = re.compile(r"^\s*(@|\*|/\*|//|\*/)")  # 메서드 바로 위의 어노테이션·주석 줄


def _split_range(start: int, end: int, max_lines: int) -> list[tuple[int, int]]:
    """[start, end] (1-base, 포함) 를 max_lines 단위로 나눈다. 앞 청크와 CHUNK_OVERLAP 줄 겹친다."""
    if end - start + 1 <= max_lines:
        return [(start, end)]
    out, s = [], start
    while s <= end:
        e = min(s + max_lines - 1, end)
        out.append((s, e))
        if e == end:
            break
        s = e - CHUNK_OVERLAP + 1
    return out


def _make_chunk(path: str, lines: list[str], start: int, end: int,
                symbol: str, layer: str) -> dict | None:
    """라인 범위로 청크 dict 를 만든다. 빈 범위면 None.
    embed_text 는 임베딩 전용(출처·심볼 머리말 포함), text 는 사용자에게 보이는 원문이다."""
    while end >= start and not lines[end - 1].strip():  # 뒤쪽 빈 줄 제거
        end -= 1
    while start <= end and not lines[start - 1].strip():  # 앞쪽 빈 줄 제거
        start += 1
    if end < start:
        return None
    text = "\n".join(lines[start - 1:end])
    return {
        "doc_id": f"{path}:{start}-{end}", "path": path, "start": start, "end": end,
        "symbol": symbol, "layer": layer, "text": text,
        # 머리말: 파일·심볼·계층을 붙여 "어느 코드인지"를 벡터에 반영한다
        "embed_text": f"# {path} | {layer} | {symbol}\n{text}",
    }


def _java_chunks() -> list[dict]:
    """자바: 클래스 머리(필드 포함) 1개 + 메서드마다 1개. 경계는 tools.parse_project() 를 재사용한다."""
    out = []
    for c in parse_project().values():
        path = c["file"]
        lines = _read(TARGET_ROOT / path).splitlines()
        n = len(lines)

        # 메서드 시작 라인을 위로 확장해 어노테이션·Javadoc 을 같은 청크에 넣는다
        starts, prev = [], c["line"]
        for m in c["methods"]:
            a = m["line"]
            while a - 1 > prev and _DECOR_RE.match(lines[a - 2]):
                a -= 1
            starts.append(a)
            prev = m["line"]

        head_start = 1
        if not CLASS_CHUNK_FROM_PACKAGE:
            head_start = c["line"]
        else:  # 라이선스 주석을 건너뛰고 package 줄부터
            head_start = next((i + 1 for i, ln in enumerate(lines) if ln.startswith("package ")), 1)
        head_end = (starts[0] - 1) if starts else n

        ranges = [(head_start, head_end, c["name"])]
        for i, m in enumerate(c["methods"]):
            end = (starts[i + 1] - 1) if i + 1 < len(starts) else n
            ranges.append((starts[i], end, f'{c["name"]}.{m["name"]}'))

        for s, e, symbol in ranges:
            for ss, ee in _split_range(s, e, JAVA_MAX_LINES):
                ch = _make_chunk(path, lines, ss, ee, symbol, c["layer"])
                if ch:
                    out.append(ch)
    return out


def _xml_spans(src: str) -> list[dict] | None:
    """XML 의 모든 요소를 문서 순서대로 [{depth, start, end, symbol}] 로 반환한다. (expat 으로 줄 번호를 얻는다)
    깨진 XML 이면 None. 네임스페이스는 처리하지 않고 태그 이름(접두사 포함)을 그대로 쓴다."""
    import xml.parsers.expat as expat
    spans: list[dict] = []
    stack: list[int] = []
    parser = expat.ParserCreate()

    def on_start(name, attrs):
        label = name
        for key in ("id", "name", "profile", "class", "resource", "base-package", "location"):  # 요소를 구분할 속성
            if key in attrs:
                val = attrs[key].rsplit(".", 1)[-1] if key in ("class", "base-package") else attrs[key]
                label = f"{name}[{key}={val}]"
                break
        spans.append({"depth": len(stack) + 1, "start": parser.CurrentLineNumber,
                      "end": parser.CurrentLineNumber, "symbol": label})
        stack.append(len(spans) - 1)

    def on_end(name):
        spans[stack.pop()]["end"] = parser.CurrentLineNumber

    parser.StartElementHandler, parser.EndElementHandler = on_start, on_end
    try:
        parser.Parse(src, True)
    except expat.ExpatError:
        return None
    return spans


def _xml_children(spans: list[dict], i: int) -> list[int]:
    """spans[i] 의 바로 아래 자식 요소 번호들."""
    kids, d = [], spans[i]["depth"]
    for j in range(i + 1, len(spans)):
        if spans[j]["depth"] <= d:
            break
        if spans[j]["depth"] == d + 1:
            kids.append(j)
    return kids


def _xml_units(spans: list[dict]) -> list[tuple[int, int, str]]:
    """루트의 자식 요소를 단위(start, end, symbol)로 만든다. XML_MAX_LINES 를 넘는 요소는 자식 단위로 내려간다."""
    out: list[tuple[int, int, str]] = []

    def visit(i: int) -> None:
        s = spans[i]
        kids = _xml_children(spans, i)
        if s["end"] - s["start"] + 1 > XML_MAX_LINES and kids:
            for k in kids:
                visit(k)
        else:
            out.append((s["start"], s["end"], s["symbol"]))

    if spans:
        for k in _xml_children(spans, 0):
            visit(k)
    return sorted(out)


def _extend_over_comments(lines: list[str], start: int, floor: int) -> int:
    """start 바로 위의 주석 블록(사이 빈 줄 1개까지 허용)을 포함하도록 시작 줄을 올린다. floor 아래로는 올라가지 않는다."""
    s = start
    while True:
        j = s - 1
        if j >= floor and not lines[j - 1].strip():
            j -= 1
        if j < floor or not lines[j - 1].strip().endswith("-->"):
            return s
        k = j
        while k >= floor and "<!--" not in lines[k - 1]:
            k -= 1
        if k < floor:
            return s
        s = k


def _xml_chunks(path: str, lines: list[str], src: str, name: str) -> list[dict] | None:
    """XML 을 요소 단위 청크로 나눈다. 깨진 XML 이거나 요소가 없으면 None(→ 고정 창으로 대체)."""
    spans = _xml_spans(src)
    units = _xml_units(spans) if spans else []
    if not units:
        return None
    # 1) 각 단위의 시작을 위로 올려 바로 위 주석을 묶는다 (이전 단위와 겹치지 않게)
    ext, floor = [], 1
    for s, e, sym in units:
        s2 = max(_extend_over_comments(lines, s, floor), floor)
        ext.append([s2, e, [sym]])
        floor = e + 1
    # 2) 짧은 단위는 다음 단위와 합친다 (합친 결과가 XML_MAX_LINES 를 넘지 않는 범위에서)
    merged: list[list] = []
    for u in ext:
        if merged and (merged[-1][1] - merged[-1][0] + 1) < XML_MIN_LINES \
                and (u[1] - merged[-1][0] + 1) <= XML_MAX_LINES:
            merged[-1][1] = u[1]
            merged[-1][2] += u[2]
        else:
            merged.append(u)
    if len(merged) > 1 and (merged[-1][1] - merged[-1][0] + 1) < XML_MIN_LINES \
            and (merged[-1][1] - merged[-2][0] + 1) <= XML_MAX_LINES:  # 마지막이 짧으면 앞과 합친다
        last = merged.pop()
        merged[-1][1] = last[1]
        merged[-1][2] += last[2]
    out = []
    # 3) 첫 요소 앞의 머리(XML 선언·루트 태그·파일 설명 주석)도 하나의 청크로 남긴다
    if merged[0][0] > 1:
        head = _make_chunk(path, lines, 1, merged[0][0] - 1, f"{name} 파일 머리(루트 태그·설명)", "config")
        if head:
            out.append(head)
    for s, e, syms in merged:
        symbol = f"{name}: " + " + ".join(syms[:3]) + (f" 외 {len(syms) - 3}개" if len(syms) > 3 else "")
        for ss, ee in _split_range(s, e, XML_MAX_LINES):
            ch = _make_chunk(path, lines, ss, ee, symbol, "config")
            if ch:
                out.append(ch)
    return out


def _other_chunks() -> list[dict]:
    """xml / jsp / sql. xml 은 요소 단위(XML_STRUCTURED), 나머지는 NONJAVA_MAX_LINES 줄 단위로 나눈다."""
    out = []
    for f in sorted({p for root in INDEX_ROOTS for p in root.rglob("*")}):
        if f.suffix.lower() not in INDEX_EXTS - {".java"}:
            continue
        path = f.resolve().relative_to(TARGET_ROOT.resolve()).as_posix()
        src = _read(f)
        lines = src.splitlines()
        layer = {".xml": "config", ".jsp": "view", ".html": "view", ".sql": "db"}[f.suffix.lower()]
        if XML_STRUCTURED and f.suffix.lower() == ".xml":
            xml_chunks = _xml_chunks(path, lines, src, f.name)
            if xml_chunks is not None:
                out += xml_chunks
                continue
        for s, e in _split_range(1, len(lines), NONJAVA_MAX_LINES):
            ch = _make_chunk(path, lines, s, e, f.name, layer)
            if ch:
                out.append(ch)
    return out


def make_chunks() -> list[dict]:
    """인덱스 대상 전체를 청크로 만든다. (자바 → 기타 순, 정렬 고정)"""
    return _java_chunks() + _other_chunks()


# ================================================================== 토큰화 (BM25)
_WORD_RE = re.compile(r"[A-Za-z0-9가-힣]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def tokenize(text: str) -> list[str]:
    """식별자를 통째와 camelCase 조각으로 모두 토큰화한다.
    예) 'saveOwner' → ['saveowner', 'save', 'owner']  (질의가 'save owner' 여도 걸리게)"""
    tokens = []
    for w in _WORD_RE.findall(text):
        tokens.append(w.lower())
        parts = _CAMEL_RE.split(w)
        if len(parts) > 1:
            tokens += [p.lower() for p in parts]
    return tokens


# ================================================================== 인덱스
class _Index:
    """청크 + 벡터(FAISS) + BM25 를 묶은 메모리 인덱스."""

    def __init__(self, chunks: list[dict], vectors: np.ndarray):
        self.chunks = chunks
        self.vectors = vectors
        self.faiss = faiss.IndexFlatIP(vectors.shape[1])  # 정규화된 벡터의 내적 = 코사인 유사도
        self.faiss.add(vectors)
        # BM25 문서: 경로·심볼도 넣어 'OwnerController' 같은 이름 질의가 걸리게 한다
        self.bm25 = BM25Okapi([tokenize(f'{c["path"]} {c["symbol"]} {c["text"]}') for c in chunks])


_index: _Index | None = None


def _fingerprint(chunks: list[dict]) -> str:
    """청크 내용 + 임베딩 설정의 해시. 달라지면 저장된 인덱스를 버린다."""
    h = hashlib.sha256(f"{embeddings.EMBED_MODEL_ID}|{embeddings.EMBED_DIM}".encode())
    for c in chunks:
        h.update(c["doc_id"].encode())
        h.update(c["embed_text"].encode())
    return h.hexdigest()


def get_index(rebuild: bool = False) -> _Index:
    """인덱스를 반환한다. 저장본이 최신이면 로드하고, 아니면 (캐시를 활용해) 다시 만든다."""
    global _index
    if _index is not None and not rebuild:
        return _index
    chunks = make_chunks()
    fp = _fingerprint(chunks)
    meta_p, vec_p = embeddings.INDEX_DIR / "meta.json", embeddings.INDEX_DIR / "vectors.npy"
    vectors = None
    if not rebuild and meta_p.exists() and vec_p.exists():
        if json.loads(meta_p.read_text(encoding="utf-8")).get("fingerprint") == fp:
            vectors = np.load(vec_p)
    if vectors is None:
        vectors = embeddings.embed_texts([c["embed_text"] for c in chunks])
        embeddings.INDEX_DIR.mkdir(parents=True, exist_ok=True)
        np.save(vec_p, vectors)
        meta_p.write_text(json.dumps({"fingerprint": fp, "chunks": len(chunks)}), encoding="utf-8")
    _index = _Index(chunks, vectors)
    return _index


# ================================================================== 검색
def _vector_rank(idx: _Index, query: str) -> list[tuple[int, float]]:
    """벡터 검색: [(청크 번호, 코사인 유사도)] 상위 CANDIDATES."""
    q = embeddings.embed_query(query).reshape(1, -1)
    sims, ids = idx.faiss.search(q, CANDIDATES)
    return [(int(i), float(s)) for i, s in zip(ids[0], sims[0]) if i >= 0]


def _bm25_rank(idx: _Index, query: str) -> list[tuple[int, float]]:
    """BM25 검색: [(청크 번호, 점수)] 상위 CANDIDATES. 점수 0 이하는 제외."""
    scores = idx.bm25.get_scores(tokenize(query))
    order = np.argsort(scores)[::-1][:CANDIDATES]
    return [(int(i), float(scores[i])) for i in order if scores[i] > BM25_MIN_SCORE]


def _rrf(ranked: dict[str, tuple[float, list[tuple[int, float]]]]) -> dict[int, dict]:
    """RRF(Reciprocal Rank Fusion): 점수 척도가 다른 두 검색의 '순위'만 합산한다.
    ranked = {라벨: (가중치, [(청크 번호, 원점수)])}. 반환 = {청크 번호: {rrf, ranks}}."""
    fused: dict[int, dict] = {}
    for label, (weight, lst) in ranked.items():
        for rank, (i, raw) in enumerate(lst, 1):
            d = fused.setdefault(i, {"rrf": 0.0, "ranks": {}})
            d["rrf"] += weight / (RRF_K + rank)
            d["ranks"][label] = (rank, round(raw, 3))
    return fused


def rewrite_query(query: str) -> str:
    """결과가 약할 때 질의를 영어 식별자 중심 키워드로 바꾼다 (Haiku, 1회). 실패하면 빈 문자열."""
    try:
        from langchain_aws import ChatBedrockConverse
        llm = ChatBedrockConverse(model=REWRITE_MODEL_ID, temperature=0, max_tokens=100)
        out = llm.invoke(REWRITE_PROMPT.format(query=query)).content
        text = out if isinstance(out, str) else "".join(b.get("text", "") for b in out if isinstance(b, dict))
        first = text.strip().splitlines()[0].strip() if text.strip() else ""
        # NONE(무관한 질의)이거나 한글이 섞였으면(= 키워드로 바뀌지 않음) 재작성을 버린다
        if first.upper() == "NONE" or re.search(r"[가-힣]", first):
            return ""
        return first
    except Exception:  # 재작성 실패가 검색 전체를 막지 않게 한다
        return ""


def search_debug(query: str, top_k: int = DEFAULT_TOP_K, rewrite: bool = True) -> dict:
    """검색 + 점검용 진단 정보. 반환: {hits, weak, rewritten, vec_max, bm25_max}.
    hits[i]['ranks'] 에 각 검색기(vec/bm25)의 순위·원점수가 담긴다."""
    idx = get_index()
    top_k = max(1, min(top_k, MAX_TOP_K))
    vec, bm = _vector_rank(idx, query), _bm25_rank(idx, query)
    ranked = {"vec": (VECTOR_WEIGHT, vec), "bm25": (BM25_WEIGHT, bm)}

    vec_max = vec[0][1] if vec else 0.0
    bm25_max = bm[0][1] if bm else 0.0
    weak = bm25_max <= BM25_MIN_SCORE or vec_max < VEC_MIN_SIM  # 재작성 조건 (튜닝 상수 참고)
    rewritten = ""
    if weak and rewrite:
        rewritten = rewrite_query(query)
        if rewritten:  # 원 질의 결과에 재작성 질의 결과를 합쳐 RRF
            ranked["vec(rw)"] = (VECTOR_WEIGHT, _vector_rank(idx, rewritten))
            ranked["bm25(rw)"] = (BM25_WEIGHT, _bm25_rank(idx, rewritten))

    fused = _rrf(ranked)
    top = sorted(fused.items(), key=lambda kv: kv[1]["rrf"], reverse=True)[:top_k]
    hits = [{**idx.chunks[i], "rrf": round(d["rrf"], 4), "ranks": d["ranks"]} for i, d in top]
    return {"hits": hits, "weak": weak, "rewritten": rewritten,
            "vec_max": round(vec_max, 3), "bm25_max": round(bm25_max, 3)}


def search_code(query: str, top_k: int = DEFAULT_TOP_K) -> list[dict]:
    """소스 코드를 의미(벡터)와 키워드(BM25)로 함께 검색한다. 클래스·메서드 설명이나 위치를 찾을 때
    가장 먼저 쓴다. 결과가 약하면 질의를 영어 식별자로 바꿔 한 번 더 찾는다.
    반환: [{doc_id, path, start, end, text}] (doc_id 는 'path:start-end')."""
    hits = search_debug(query, top_k)["hits"]
    return [{k: h[k] for k in ("doc_id", "path", "start", "end", "text")} for h in hits]


# ================================================================== 점검용 CLI
def _print_chunk_stats() -> None:
    chunks = make_chunks()
    lens = [c["end"] - c["start"] + 1 for c in chunks]
    print(f"청크 {len(chunks)}개 | 줄 수 평균 {sum(lens) / len(lens):.1f}, 최대 {max(lens)}, 최소 {min(lens)}")
    by_layer: dict[str, int] = {}
    for c in chunks:
        by_layer[c["layer"]] = by_layer.get(c["layer"], 0) + 1
    print("계층별:", by_layer)
    for c in chunks[:: max(1, len(chunks) // 8)]:  # 균등 샘플 8개
        print(f'  {c["doc_id"]:<95} {c["symbol"]}')


def _main() -> None:
    ap = argparse.ArgumentParser(description="검색 점검 도구")
    ap.add_argument("query", nargs="?", help="검색 질의")
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    ap.add_argument("--rebuild", action="store_true", help="인덱스 강제 재생성")
    ap.add_argument("--chunks", action="store_true", help="청크 통계·샘플만 출력")
    ap.add_argument("--no-rewrite", action="store_true", help="질의 재작성 끄기")
    a = ap.parse_args()
    if a.chunks or not a.query:
        _print_chunk_stats()
        return
    if a.rebuild:
        get_index(rebuild=True)
    r = search_debug(a.query, a.top_k, rewrite=not a.no_rewrite)
    print(f'vec_max={r["vec_max"]} bm25_max={r["bm25_max"]} weak={r["weak"]} rewritten={r["rewritten"]!r}')
    for h in r["hits"]:
        print(f'{h["rrf"]:.4f}  {h["doc_id"]}  [{h["symbol"]}]  {h["ranks"]}')


if __name__ == "__main__":
    _main()
