import os

import boto3
from dotenv import load_dotenv

# .env 의 AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_DEFAULT_REGION 로드
load_dotenv()

# 사용할 Bedrock 모델 ID (Bedrock 콘솔의 모델 액세스에서 활성화된 ID로 .env 에 지정)
MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")

client = boto3.client("bedrock-runtime")


def ask_claude(prompt):
    try:
        response = client.converse(
            modelId=MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 1000, "temperature": 0.7},
        )
        return response["output"]["message"]["content"][0]["text"]
    except Exception as e:
        return f"에러가 발생했습니다: {e}"


if __name__ == "__main__":
    print(f"Bedrock 연결 테스트 (model: {MODEL_ID})")
    user_input = input("질문을 입력하세요")

    print("\n답변 생성 중...")
    print("\n--- 답변 ---")
    print(ask_claude(user_input))
