"""검색 테스트: 청킹 경계, 토큰화, 순위 융합. (임베딩·Bedrock 호출 없음)"""
from src import retriever
from src.tools import TARGET_ROOT


def test_토큰화는_식별자_통째와_camelCase_조각을_모두_만든다():
    assert retriever.tokenize("saveOwner") == ["saveowner", "save", "owner"]
    assert "owner" in retriever.tokenize("OwnerController")  # 이름 질의가 걸리게


def test_분할은_최대_줄수를_지키고_앞_청크와_겹친다():
    parts = retriever._split_range(1, 130, 60)
    assert parts[0] == (1, 60)
    assert all(e - s + 1 <= 60 for s, e in parts)
    assert parts[1][0] == 60 - retriever.CHUNK_OVERLAP + 1   # 겹침
    assert parts[-1][1] == 130                               # 끝까지 덮는다
    assert retriever._split_range(5, 10, 60) == [(5, 10)]


def test_청크_범위가_원본_파일의_실제_줄과_같다():
    """doc_id 의 경로:시작-끝 이 원본과 어긋나면 근거 표시가 모두 틀어진다."""
    chunks = retriever.make_chunks()
    assert len(chunks) > 200
    for c in chunks[:: max(1, len(chunks) // 40)]:  # 균등 표본 40개
        lines = (TARGET_ROOT / c["path"]).read_text(encoding="utf-8", errors="replace").splitlines()
        assert c["text"] == "\n".join(lines[c["start"] - 1:c["end"]]), c["doc_id"]
        assert c["doc_id"] == f'{c["path"]}:{c["start"]}-{c["end"]}'
        assert c["text"].strip()


def test_자바는_메서드_단위로_잘린다():
    by_symbol = {c["symbol"]: c for c in retriever.make_chunks()}
    save_owner = by_symbol["ClinicServiceImpl.saveOwner"]
    assert (save_owner["start"], save_owner["end"]) == (72, 76)   # @Override·@Transactional 포함
    assert "ownerRepository.save(owner)" in save_owner["text"]


def test_임베딩용_머리말은_출처와_계층을_담지만_원문은_그대로다():
    c = next(c for c in retriever.make_chunks() if c["symbol"] == "ClinicServiceImpl.saveOwner")
    assert c["embed_text"].startswith("# ") and "service" in c["embed_text"].splitlines()[0]
    assert c["embed_text"].endswith(c["text"])


def test_XML_청킹_기본값은_고정_창이고_옵션을_켜면_설정_블록_단위가_된다(monkeypatch):
    def tools_config_ranges():
        return {(c["start"], c["end"]) for c in retriever.make_chunks() if c["path"].endswith("tools-config.xml")}

    assert retriever.XML_STRUCTURED is False
    assert tools_config_ranges() == {(1, 49)}
    monkeypatch.setattr(retriever, "XML_STRUCTURED", True)
    assert (37, 47) in tools_config_ranges()   # cache:annotation-driven + cacheManager 블록


def test_RRF는_두_검색에_모두_나온_청크를_위로_올린다():
    fused = retriever._rrf({"vec": (1.0, [(1, 0.9), (2, 0.8)]), "bm25": (1.0, [(2, 5.0), (3, 4.0)])})
    order = sorted(fused, key=lambda i: -fused[i]["rrf"])
    assert order[0] == 2
    assert set(fused[2]["ranks"]) == {"vec", "bm25"}
