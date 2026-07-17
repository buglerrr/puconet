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
| `EXCLUDE_KEYWORDS` | `청원경찰,의사,간호,환경미화,운전,경비,단기노무원,프로젝트계약근로자,작업원,보강공사,단순정비,일용근로자,배전분야,일용원,배전자동화,촉탁` | 제목에 포함되면 업로드 제외할 키워드(콤마 구분). 바꾸려면 `--set-env-vars=EXCLUDE_KEYWORDS="키워드1,키워드2"` |

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

---

## 🎬 쇼츠(마감임박 TOP 5) 자동 생성 — `shorts_video.py`

매일 크롤링 직후, **마감임박 TOP 5 쇼츠 영상**(1080×1920, 약 30초)을 자동 생성해
① 구글 드라이브 `[shorts]` 폴더 저장 ② 인스타그램 릴스 게시까지 수행합니다.
- MZ 톤 스크립트 자동 생성(매일 로테이션) + Google TTS 나레이션(생동감 있는 목소리)
- 장면마다 해당 기관 CI/공고 정보 화면 전환 + 줌 모션 + 배경음악
- BGM: `automation/assets/bgm.mp3` 를 넣으면 그 음원 사용(유튜브 오디오 라이브러리 등
  무료 음원 권장), 없으면 저작권 걱정 없는 자체 합성 루프 사용

### 설정 (한 번만)

1. **OAuth 동의화면**: <https://console.cloud.google.com/apis/credentials/consent?project=recruit-board>
   → User Type **외부** → 앱 이름/이메일 입력 후 저장 → **앱 게시**(프로덕션 전환).
   (drive.file 은 비민감 범위라 심사 불필요. '테스트' 상태로 두면 7일마다 인증 만료됨)
2. **OAuth 클라이언트**: <https://console.cloud.google.com/apis/credentials?project=recruit-board>
   → 사용자 인증 정보 만들기 → OAuth 클라이언트 ID → 유형 **'TV 및 제한된 입력 장치'**
   → 클라이언트 ID/보안 비밀 복사.
3. **1회 인증**: Cloud Shell 에서 `cd ~/puconet/automation && python3 drive_auth.py`
   → ID/비밀 붙여넣기 → 안내되는 URL 에서 코드 입력·허용. (Firestore 에 자동 저장됨)
   ※ 구글 정책상 서비스계정은 개인 드라이브에 파일을 소유할 수 없어(storageQuotaExceeded)
     소유자 본인 인증 방식을 사용합니다. 폴더 공유는 필요 없습니다.
4. **Firestore 설정 문서**: `_config` 컬렉션 → `shorts` 문서:
   ```
   enabled  : true
   ig_reels : true        ← 인스타 릴스 게시 (기존 social 토큰 재사용)
   ```
   (drive_folder_id 는 첫 실행 때 자동 설정됩니다. 첫 실행 후 드라이브에 생긴
   '올공 쇼츠' 폴더를 원하는 위치로 옮기면 이후에도 계속 그곳에 저장됩니다.)
5. **재배포**: `./deploy.sh` 재실행.

컴퓨터를 켜지 않아도 영상은 드라이브 클라우드에 저장되며, PC를 켜면
`G:\내 드라이브\[교원창업]\[공기업 브레인넷]\[shorts]` 경로로 자동 동기화됩니다.
비용: TTS는 무료 한도(월 100만 자) 내, 함수 실행 1~2분/일 수준입니다.

---

## 📰 공공기관 뉴스 자동 게시 — `news_sync.py`

매일 함수 실행 시각(07/12/18시)에 맞춰 '공공기관 채용' 관련 최신 뉴스를 검색하고,
원문을 **완전히 새 문장으로 재작성(패러프레이징)** 한 글을 사이트의
'공공기관 뉴스' 게시판에 **하루 3건**(아침·점심·저녁 각 1건) 자동 게시합니다.
저녁 슬롯은 '전망/전문가 의견' 키워드로 검색해 분석성 기사도 다룹니다.
모든 글 하단에는 원문 출처(매체명·링크)가 자동 표기됩니다.

### 설정 방법 (한 번만)

1. **네이버 검색 API 키 발급** (무료)
   - https://developers.naver.com/apps/#/register 접속 → 네이버 로그인
   - 애플리케이션 이름: `올공 뉴스` (아무거나) / 사용 API: **검색** 선택
   - 비로그인 오픈 API 서비스 환경: **WEB 설정** → URL에 `https://allgongin.com` 입력 → 등록
   - 발급된 **Client ID** 와 **Client Secret** 두 값을 복사
2. **Firestore 설정 문서에 저장**
   - Firebase 콘솔 → Firestore → `_config` 컬렉션 → `news` 문서(없으면 생성)
   - 필드 추가(모두 문자열):
     - `naver_client_id` = 발급받은 Client ID
     - `naver_client_secret` = 발급받은 Client Secret
3. **재배포**: Cloud Shell에서 `cd ~/puconet && git pull && cd automation && ./deploy.sh`
   (Vertex AI API 활성화와 서비스계정 권한이 자동으로 추가됩니다)

### 선택 설정 (같은 `_config/news` 문서)

| 필드 | 기본값 | 설명 |
|---|---|---|
| `enabled` | `true` | `false` 로 두면 기능 전체 정지 |
| `daily_limit` | `3` | 하루 게시 건수 |
| `queries` | 채용/공기업/전망 | 검색 키워드 배열 |
| `use_og_image` | `false` | `true` 면 원문 대표사진을 썸네일로 사용 (언론사 사진은 별도 저작권이 있어 기본은 미사용) |

### 비용

- 네이버 검색 API: 무료 (하루 25,000회 한도, 실제 사용은 하루 수 회)
- Vertex AI Gemini(재작성): 글 3건 기준 **월 수십 원 미만** 수준

### 저작권 관련 메모

기사 전문 복제는 저작권 침해이므로 이 모듈은 ① 사실관계만 유지한 전면 재작성,
② 출처·원문 링크 표기, ③ 언론사 사진 기본 미사용 3중 장치를 둡니다.
그래도 특정 언론사가 문제를 제기하면 해당 글을 삭제하고 `queries` 에서
관련 키워드를 조정하는 방식으로 대응하세요.
