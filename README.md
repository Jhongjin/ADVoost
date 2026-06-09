# ADVoost 검색 분석 툴 클론

Next.js App Router 프론트엔드와 FastAPI 기반 SEO 진단 API로 구성한 B2B SaaS 프로토타입입니다.

## 실행

```bash
npm install
npm run dev
```

프론트엔드: http://localhost:3000

프론트엔드 단건/일괄 분석은 기본적으로 `http://localhost:8000/api/audit`를 호출합니다.
다른 API 서버를 사용할 때는 `NEXT_PUBLIC_AUDIT_API_URL`을 설정하세요.

```bash
C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pip install -r requirements.txt
C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m uvicorn main:app --reload --port 8000
```

API 문서: http://localhost:8000/docs

## Vercel 배포 메모

Vercel은 Next.js 프론트엔드를 자동으로 빌드할 수 있습니다. 운영에서 실제 진단 기능을 사용하려면 FastAPI 서버를 별도로 배포한 뒤 Vercel 프로젝트 환경변수에 아래 값을 설정하세요.

```bash
NEXT_PUBLIC_AUDIT_API_URL=https://your-audit-api.example.com
```

이 값을 설정하지 않으면 브라우저가 기본값인 `http://localhost:8000`으로 요청하므로, 배포 환경에서는 단건/일괄 분석 API 호출이 실패합니다.

현재 배포 예시는 아래와 같습니다.

```bash
NEXT_PUBLIC_AUDIT_API_URL=https://advoost.onrender.com
```

FastAPI는 기본적으로 로컬 개발 주소와 `https://ad-voost.vercel.app`을 CORS 허용 목록에 포함합니다. Vercel 도메인을 바꾸거나 preview 도메인에서도 API를 호출하려면 Render 환경변수에 쉼표로 구분한 origin을 추가하세요.

```bash
CORS_ORIGINS=https://ad-voost.vercel.app,https://your-preview.vercel.app
```

## 구현 범위

- 대시보드: 통과/경고/실패 지표, 평균 점수, 최근 분석, 기술 지원 현황
- 분석하기: URL 프로토콜 자동 보정, 72시간 동일 URL 결과 재사용, 단건 진단 흐름
- 분석 히스토리: 결과 그리드, PDF ZIP 비동기 상태, CSV Export, 상세 리포트 연결
- 결과 상세: A~F 등급, 32개 SEO 항목, HTML 스니펫 아코디언, 점검불가 감점 제외, 화이트리스트 팝업
- 일괄 분석: 선택 URL 큐 등록, 진행 상태, 실패 URL 상태 반영
- URL 관리: 개별 등록, CSV 대량 등록, 오류 행 스킵, 일괄 분석 화면으로 선택 상태 인계
- FastAPI `/api/audit`: HTML 수집, BeautifulSoup 파싱, SQLite 72시간 캐시, ADVoost식 핵심 경고 강등 기반 등급 산정
- 프론트/API 연결: API 응답을 리포트 모델로 변환해 단건 및 일괄 분석 결과에 반영
