# 채용공고 자동 수집·게시 (서버리스)

교수님 PC를 켜두지 않아도, 구글 클라우드가 **매일 자동으로** 알리오 채용공고를
수집해 올공(`recruit-board`) Firestore `jobs` 컬렉션에 올립니다.

```
Cloud Scheduler(매일 정해진 시각)
  → Cloud Function(job_sync) 실행
     ① 알리오 API 수집 → ② 복합필터 + 기관유형 병합
     → ③ Firestore 'jobs' 에 고정 ID로 upsert (중복 방지)
     → ④ 만료된 자동공고 정리 (수동 등록 공고는 보존)
  → 올공 '채용정보' 게시판 자동 반영
```

기존 노트북 2개(`Alio 크롤러`, `Firebase 업로더`)를 합친 것이며,
**엑셀/구글드라이브 중간 단계는 제거**했습니다.

---

## 0. 사전 준비 (한 번만)

- gcloud CLI 설치 후 로그인: `gcloud auth login`
- 프로젝트 지정: `gcloud config set project recruit-board`
- API 활성화:
  ```bash
  gcloud services enable \
    cloudfunctions.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
    cloudscheduler.googleapis.com secretmanager.googleapis.com
  ```

## 1. 알리오 서비스키를 Secret Manager에 저장

> ⚠️ 서비스키를 코드에 넣지 않습니다. 비밀저장소에 넣고 함수에 주입합니다.
> (이전에 채팅/파일로 노출된 키는 data.go.kr에서 **재발급**받아 쓰시길 권장합니다.)

```bash
printf '발급받은_서비스키_원문' | gcloud secrets create ALIO_SERVICE_KEY --data-file=-
# 이미 있으면 새 버전 추가:
# printf '키' | gcloud secrets versions add ALIO_SERVICE_KEY --data-file=-
```

## 2. 함수 배포

`automation/` 폴더에서 실행:

```bash
gcloud functions deploy job-sync \
  --gen2 \
  --region=asia-northeast3 \
  --runtime=python312 \
  --source=. \
  --entry-point=job_sync \
  --trigger-http \
  --no-allow-unauthenticated \
  --memory=1Gi \
  --timeout=540s \
  --set-secrets=ALIO_SERVICE_KEY=ALIO_SERVICE_KEY:latest
```

배포 후 출력되는 **함수 URL** 을 메모해 둡니다. (예: `https://job-sync-xxxx.a.run.app`)

## 3. 런타임 서비스계정 권한 부여

함수가 Firestore에 쓰고 Storage 로고를 읽으려면 권한이 필요합니다.
2nd gen 함수의 기본 런타임 SA는 보통 `PROJECTNUMBER-compute@developer.gserviceaccount.com` 입니다.

```bash
# 프로젝트 번호 확인
PN=$(gcloud projects describe recruit-board --format='value(projectNumber)')
SA="$PN-compute@developer.gserviceaccount.com"

gcloud projects add-iam-policy-binding recruit-board \
  --member="serviceAccount:$SA" --role="roles/datastore.user"
gcloud projects add-iam-policy-binding recruit-board \
  --member="serviceAccount:$SA" --role="roles/storage.objectAdmin"
```

## 4. 스케줄러 등록 (매일 오전 7시 KST 예시)

함수가 인증 필요(`--no-allow-unauthenticated`)이므로 OIDC 토큰으로 호출합니다.

```bash
FUNC_URL="2단계에서_메모한_함수_URL"
PN=$(gcloud projects describe recruit-board --format='value(projectNumber)')
SA="$PN-compute@developer.gserviceaccount.com"

gcloud scheduler jobs create http job-sync-daily \
  --location=asia-northeast3 \
  --schedule="0 7 * * *" \
  --time-zone="Asia/Seoul" \
  --uri="$FUNC_URL" \
  --http-method=POST \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$FUNC_URL"
```

수동으로 즉시 한 번 돌려보기:
```bash
gcloud scheduler jobs run job-sync-daily --location=asia-northeast3
```

## 5. 동작 확인

- Cloud Functions 콘솔 → `job-sync` → **로그** 에서 수집/업로드 건수 확인
- 올공 사이트 '채용정보' 게시판에 공고가 갱신되었는지 확인

---

## 환경변수(선택) 조정

배포 시 `--set-env-vars` 로 바꿀 수 있습니다.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `MAX_DAYS` | `20` | 복합필터의 마감 임박 기준 일수 |
| `FILTER_MODE` | `composite` | `composite`(신입·학력·마감·보건제외) / `no_medical`(보건의료만 제외) / `all`(전체) |
| `COLLECTION_NAME` | `jobs` | 업로드 대상 Firestore 컬렉션 |
| `STORAGE_BUCKET` | `recruit-board.firebasestorage.app` | 로고 버킷 |
| `EXCLUDE_KEYWORDS` | `청원경찰,의사,간호,환경미화,운전,경비,단기노무원,프로젝트계약근로자,작업원,보강공사,단순정비,일용근로자` | 제목에 포함되면 업로드 제외할 키워드(콤마 구분). 바꾸려면 `--set-env-vars=EXCLUDE_KEYWORDS="키워드1,키워드2"` |

예: 전체 공고를 올리고 싶다면
`--set-env-vars=FILTER_MODE=all` 추가.

## 동작 원리 메모

- **중복 방지**: 각 공고는 원본공고URL(없으면 기관명+제목+마감일) 해시를 문서 ID로 사용 →
  매일 돌려도 같은 공고는 **갱신**될 뿐 중복 생성되지 않습니다.
- **만료 정리**: 이번 수집에 없는 `source='alio-auto'` 문서는 삭제됩니다(마감 공고 정리).
- **수동 공고 보존**: 관리자가 사이트에서 직접 올린 공고는 `source` 필드가 없으므로
  자동 정리에서 제외됩니다.
- **로고**: Firebase Storage `logos/` 의 파일명을 기관명과 매칭합니다.
  (로고가 없으면 `./logo.png`)

## 로컬 테스트 (선택)

```bash
pip install -r requirements.txt
# Firebase 인증: 서비스계정 키로 ADC 설정
export GOOGLE_APPLICATION_CREDENTIALS=/경로/serviceAccountKey.json
export ALIO_SERVICE_KEY='서비스키'
python main.py
```
