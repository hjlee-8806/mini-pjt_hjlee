"""POST /query 엔드포인트 (제출 규약: question 입력, answer·contexts·trace 출력)."""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src import settings
from src.agent import QueryResponse, run_query

settings.validate_target()  # 분석 대상이 잘못됐으면 서버를 시작하지 않는다 (원인과 해결 방법이 오류에 담긴다)

app = FastAPI(title="Source Code Analyst")


class QueryRequest(BaseModel):
    """요청 본문. session_id 는 선택: 같은 값이면 대화·승인 대기 상태를 이어간다."""
    question: str
    session_id: str | None = None


@app.post("/query", response_model=QueryResponse)
def query(req: QueryRequest) -> QueryResponse:
    """질의를 에이전트에 전달하고 구조화된 응답을 반환한다."""
    try:
        return run_query(req.question, req.session_id)
    except Exception as e:  # 내부 상세(.env 값 등)는 응답에 노출하지 않는다
        raise HTTPException(status_code=500, detail=f"처리 중 오류: {type(e).__name__}") from e
