# 설치 및 실행 방법 요약
- Python 3.9+ 설치
- 프로젝트 폴더 생성, 위 파일들(.env, collector.py, requirements.txt) 복사
- 가상환경 생성 및 활성화
```
python -m venv venv
```
- Windows는 다음 파일을 실행해 가상 환경 진입. => .\venv\Scripts\Ativate.ps1
- 의존성 리스트 작성 : requirements.txt 에 정리
- 의존성 설치
```
pip install -r requirements.txt
```
- .env 파일에 ORG_ID, API_TOKEN 등 값 채움
- 수동 실행으로 동작 확인
```
python collect-audit.py
```

# Rate limit 관련 문서에 Retry-After 가 header로 값이 넘어온다고 기술되어 있음
- https://developer.atlassian.com/cloud/jira/platform/rate-limiting/#rate-limit-detection