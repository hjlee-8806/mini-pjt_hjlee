"""Bedrock 임베딩 호출 + 디스크 캐시.

retriever.py 가 청크·질의를 벡터로 바꿀 때만 쓴다. (RAG 에서 '임베딩' 한 가지 역할만 맡는다)

조정 포인트 (아래 상수 / 환경변수)
- BEDROCK_EMBED_MODEL_ID : 임베딩 모델. 기본 Titan Text Embeddings v2.
  (cohere.embed-multilingual-v3 는 이 계정에서 AccessDenied 였다.)
- EMBED_DIM              : 벡터 차원. Titan v2 는 256 / 512 / 1024 지원. 클수록 정밀하지만 느리다.
- EMBED_MAX_CHARS        : 한 텍스트의 최대 길이. Titan v2 입력 한도(8k 토큰)를 넘지 않도록 자른다.
- 캐시: 텍스트 해시 → 벡터. 같은 청크는 다시 호출하지 않으므로 인덱스 재생성이 거의 공짜다.
        모델·차원을 바꾸면 키가 달라져 자동으로 새로 계산한다.
"""
from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor

import boto3
import numpy as np
from botocore.config import Config
from dotenv import load_dotenv

from src import settings, usage

load_dotenv()

CACHE_DIR = settings.CACHE_DIR                       # 임베딩 캐시는 대상과 무관하게 공유한다 (텍스트 해시가 키, .gitignore 대상)
CACHE_PATH = CACHE_DIR / "embed_cache.json"
INDEX_DIR = settings.INDEX_DIR                       # 벡터·메타는 대상별 폴더 (data/index/<이름>-<해시>/)

EMBED_MODEL_ID = os.getenv("BEDROCK_EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
EMBED_DIM = 1024
EMBED_MAX_CHARS = 6000
EMBED_WORKERS = 8                                     # 동시 호출 수. 스로틀링이 나면 줄인다.

_client = None


def _bedrock():
    """bedrock-runtime 클라이언트. 스로틀링 대비 재시도를 켠다."""
    global _client
    if _client is None:
        _client = boto3.client(
            "bedrock-runtime",
            config=Config(retries={"max_attempts": 8, "mode": "adaptive"}))
    return _client


def _key(text: str) -> str:
    """캐시 키. 모델·차원이 바뀌면 키도 바뀐다."""
    raw = f"{EMBED_MODEL_ID}|{EMBED_DIM}|{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _call(text: str, ledger: usage.Ledger | None = None) -> list[float]:
    """Titan 임베딩 API 를 한 번 호출한다. (정규화된 벡터 반환) ledger 는 토큰 집계용이다."""
    body = {"inputText": text[:EMBED_MAX_CHARS], "dimensions": EMBED_DIM, "normalize": True}
    resp = _bedrock().invoke_model(modelId=EMBED_MODEL_ID, body=json.dumps(body))
    out = json.loads(resp["body"].read())
    if ledger:   # 작업 스레드에는 ContextVar 가 전달되지 않아 호출 시점의 ledger 를 넘겨 받는다
        ledger.add(EMBED_MODEL_ID, input_tokens=out.get("inputTextTokenCount", 0))
    return out["embedding"]


def _load_cache() -> dict[str, list[float]]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:  # 깨진 캐시는 버리고 다시 만든다
            return {}
    return {}


def embed_texts(texts: list[str]) -> np.ndarray:
    """텍스트 목록을 (N, EMBED_DIM) float32 배열로 임베딩한다. 캐시에 있는 것은 호출하지 않는다."""
    cache = _load_cache()
    keys = [_key(t) for t in texts]
    todo = {k: t for k, t in zip(keys, texts) if k not in cache}  # 중복 텍스트도 한 번만 호출
    if todo:
        ledger = usage.current()   # 작업 스레드로 넘기기 위해 미리 잡아 둔다
        with ThreadPoolExecutor(max_workers=EMBED_WORKERS) as pool:
            for k, vec in zip(todo, pool.map(lambda t: _call(t, ledger), todo.values())):
                cache[k] = vec
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    return np.array([cache[k] for k in keys], dtype="float32")


def embed_query(text: str) -> np.ndarray:
    """질의 한 건을 (EMBED_DIM,) 벡터로 임베딩한다."""
    return embed_texts([text])[0]
