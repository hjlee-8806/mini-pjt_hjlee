"""LLM 토큰 사용량 집계. 요청(또는 평가 케이스) 하나가 쓴 모델 호출을 모델별로 합친다.

- `collect()` 로 집계 구간을 열면 그 안에서 `callbacks()` 를 붙인 LangChain 호출의 토큰이 `Ledger` 에 쌓인다.
- 에이전트 그래프에 콜백을 한 번 붙이면 그래프 안쪽의 모든 모델 호출(범위 분류·질의 재작성·대화 요약의 Haiku 포함)이
  같은 콜백으로 잡힌다. (LangChain 이 실행 설정을 안쪽 호출로 전달한다)
- 임베딩(Titan)은 LangChain 을 거치지 않아 `add_embedding()` 으로 직접 더한다.
- 집계 결과(`Ledger.summary()`)는 trace 로그와 평가 결과 JSON 에 저장된다. 비용 추정과 한도 관리의 근거로 쓴다.
- 포함하지 않는 것: RAGAS 채점 호출(ragas 가 자체 클라이언트를 쓴다).
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar

from langchain_core.callbacks import BaseCallbackHandler

_FIELDS = ("requests", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
_current: ContextVar["Ledger | None"] = ContextVar("usage_ledger", default=None)


class Ledger:
    """모델 호출 토큰 합계. 키는 모델 ID (평가 judge 는 '모델 [judge]'). 스레드 안전하다.
    구간이 중첩되면(평가 케이스 안에서 요청이 실행되는 경우) 안쪽에 더한 값이 바깥(parent)에도 더해진다."""

    def __init__(self, parent: "Ledger | None" = None) -> None:
        self._by_model: dict[str, dict[str, int]] = {}
        self._lock = threading.Lock()
        self.parent = parent

    def add(self, model: str, *, requests: int = 1, input_tokens: int = 0, output_tokens: int = 0,
            cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> None:
        """호출 한 번(또는 여러 번)의 토큰을 더한다."""
        vals = dict(requests=requests, input_tokens=input_tokens, output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens, cache_write_tokens=cache_write_tokens)
        with self._lock:
            row = self._by_model.setdefault(model, dict.fromkeys(_FIELDS, 0))
            for k, v in vals.items():
                row[k] += int(v or 0)
        if self.parent:
            self.parent.add(model, **vals)

    def callbacks(self, label: str = "") -> list:
        """LangChain 호출 설정(config={'callbacks': ...})에 넣을 콜백. label 은 같은 모델을 구분할 때 쓴다(예: 'judge')."""
        return [UsageCallback(self, label)]

    def summary(self) -> dict:
        """{requests, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, by_model}"""
        with self._lock:
            by_model = {m: dict(r) for m, r in self._by_model.items()}
        total = {k: sum(r[k] for r in by_model.values()) for k in _FIELDS}
        return {**total, "by_model": by_model}


class UsageCallback(BaseCallbackHandler):
    """채팅 모델 호출이 끝날 때 응답의 usage_metadata 를 Ledger 에 더한다."""

    def __init__(self, ledger: Ledger, label: str = "") -> None:
        self.ledger, self.label = ledger, label
        self._models: dict = {}

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        """모델 ID 는 시작 시점의 호출 파라미터에서 읽는다. (응답에는 들어 있지 않다)"""
        params = kwargs.get("invocation_params") or {}
        self._models[run_id] = params.get("model") or params.get("model_id") or "unknown"

    def on_llm_end(self, response, *, run_id, **kwargs):
        model = self._models.pop(run_id, "unknown")
        for gens in response.generations:
            for g in gens:
                meta = getattr(getattr(g, "message", None), "usage_metadata", None) or {}
                details = meta.get("input_token_details") or {}
                self.ledger.add(f"{model} [{self.label}]" if self.label else model,
                                input_tokens=meta.get("input_tokens", 0), output_tokens=meta.get("output_tokens", 0),
                                cache_read_tokens=details.get("cache_read", 0),
                                cache_write_tokens=details.get("cache_creation", 0))

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._models.pop(run_id, None)   # 실패한 호출은 토큰을 세지 않는다(한도에서 제외되는지는 확인되지 않았다)


@contextmanager
def collect():
    """집계 구간을 연다. 안쪽 코드는 `current()` 로 이 Ledger 에 접근한다."""
    ledger = Ledger(parent=_current.get())
    token = _current.set(ledger)
    try:
        yield ledger
    finally:
        _current.reset(token)


def current() -> Ledger | None:
    """지금 열려 있는 집계 구간의 Ledger. 없으면 None."""
    return _current.get()


def current_callbacks(label: str = "") -> list:
    """열려 있는 집계 구간이 있으면 그 콜백, 없으면 빈 리스트. (호출부가 구간 유무를 신경 쓰지 않게 한다)"""
    led = current()
    return led.callbacks(label) if led else []


def add_embedding(model: str, tokens: int) -> None:
    """임베딩 호출 한 번의 입력 토큰을 현재 구간에 더한다. 구간이 없으면 무시한다."""
    led = current()
    if led:
        led.add(model, input_tokens=tokens)


def format_summary(s: dict) -> str:
    """한 줄 요약. 예) '요청 4건 · 입력 18,420 · 출력 912 (캐시 읽기 0)'."""
    cache = s.get("cache_read_tokens", 0)
    return (f'요청 {s.get("requests", 0)}건 · 입력 {s.get("input_tokens", 0):,} · 출력 {s.get("output_tokens", 0):,}'
            + (f" (캐시 읽기 {cache:,})" if cache else ""))


def merge(summaries: list[dict]) -> dict:
    """여러 summary 를 합친다. (평가 라운드 합계용)"""
    led = Ledger()
    for s in summaries:
        for model, r in (s or {}).get("by_model", {}).items():
            led.add(model, **{k: r.get(k, 0) for k in _FIELDS})
    return led.summary()
