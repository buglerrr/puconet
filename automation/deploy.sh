#!/usr/bin/env bash
#
# 채용공고 자동화 - 원클릭 배포 스크립트
# ─────────────────────────────────────────────
# 사용법 (Google Cloud Shell 권장: https://shell.cloud.google.com):
#   1) 저장소를 받습니다:   git clone https://github.com/buglerrr/puconet && cd puconet/automation
#   2) 실행 권한:           chmod +x deploy.sh
#   3) 실행:                ./deploy.sh
#   → 알리오 서비스키는 실행 중에 '가려진 입력'으로 물어봅니다(화면/기록에 안 남음).
#
# 다시 실행해도 안전합니다(이미 있는 자원은 갱신/건너뜀).

set -euo pipefail

# ── 설정 (필요시 수정) ──
PROJECT="recruit-board"
REGION="asia-northeast3"            # 서울 리전
FUNC_NAME="job-sync"
SCHEDULE="0 7 * * *"                # 매일 오전 7시
TZ="Asia/Seoul"
RUNTIME="python312"

echo "▶ 프로젝트: $PROJECT / 리전: $REGION"
gcloud config set project "$PROJECT"

# ── 1) 필요한 API 활성화 ──
echo "▶ [1/6] API 활성화..."
gcloud services enable \
  cloudfunctions.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
  cloudscheduler.googleapis.com secretmanager.googleapis.com \
  texttospeech.googleapis.com drive.googleapis.com aiplatform.googleapis.com

# ── 2) 알리오 서비스키를 Secret Manager에 저장 ──
echo "▶ [2/6] 알리오 서비스키 입력 (입력 내용은 화면에 표시되지 않습니다)"
read -rsp "   ALIO 서비스키: " ALIO_KEY; echo
if gcloud secrets describe ALIO_SERVICE_KEY >/dev/null 2>&1; then
  printf '%s' "$ALIO_KEY" | gcloud secrets versions add ALIO_SERVICE_KEY --data-file=-
  echo "   (기존 시크릿에 새 버전 추가)"
else
  printf '%s' "$ALIO_KEY" | gcloud secrets create ALIO_SERVICE_KEY --data-file=-
  echo "   (시크릿 신규 생성)"
fi
unset ALIO_KEY

# ── 3) 런타임 서비스계정 권한 ──
echo "▶ [3/6] 서비스계정 권한 부여..."
PN=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
SA="$PN-compute@developer.gserviceaccount.com"
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$SA" --role="roles/datastore.user" >/dev/null
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$SA" --role="roles/storage.objectAdmin" >/dev/null
# 뉴스 패러프레이징(Vertex AI Gemini) 호출 권한
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$SA" --role="roles/aiplatform.user" >/dev/null
# 시크릿 접근 권한
gcloud secrets add-iam-policy-binding ALIO_SERVICE_KEY \
  --member="serviceAccount:$SA" --role="roles/secretmanager.secretAccessor" >/dev/null || true
echo "   서비스계정: $SA"

# ── 4) 함수 배포 ──
echo "▶ [4/6] Cloud Function 배포 (수 분 소요)..."
gcloud functions deploy "$FUNC_NAME" \
  --gen2 --region="$REGION" --runtime="$RUNTIME" \
  --source=. --entry-point=job_sync \
  --trigger-http --no-allow-unauthenticated \
  --memory=2Gi --timeout=900s \
  --set-secrets=ALIO_SERVICE_KEY=ALIO_SERVICE_KEY:latest

FUNC_URL=$(gcloud functions describe "$FUNC_NAME" --region="$REGION" --gen2 --format='value(serviceConfig.uri)')
echo "   함수 URL: $FUNC_URL"

# ── 5) 스케줄러 등록(매일 자동 실행) ──
echo "▶ [5/6] Cloud Scheduler 등록..."
SCHED_ARGS=(
  --location="$REGION" --schedule="$SCHEDULE" --time-zone="$TZ"
  --uri="$FUNC_URL" --http-method=POST
  --oidc-service-account-email="$SA" --oidc-token-audience="$FUNC_URL"
  --attempt-deadline=900s   # 함수 타임아웃과 동일하게 — 쇼츠·뉴스 등 작업 증가에 여유 확보
)
if gcloud scheduler jobs describe "${FUNC_NAME}-daily" --location="$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "${FUNC_NAME}-daily" "${SCHED_ARGS[@]}"
else
  gcloud scheduler jobs create http "${FUNC_NAME}-daily" "${SCHED_ARGS[@]}"
fi
# 정오/저녁 SNS 슬롯 작업이 별도로 있으면 같은 설정(기한 540초 포함)으로 갱신
for _slot in noon evening; do
  if gcloud scheduler jobs describe "${FUNC_NAME}-${_slot}" --location="$REGION" >/dev/null 2>&1; then
    _sched="0 12 * * *"; [ "$_slot" = "evening" ] && _sched="0 18 * * *"
    gcloud scheduler jobs update http "${FUNC_NAME}-${_slot}" \
      --location="$REGION" --schedule="$_sched" --time-zone="$TZ" \
      --uri="$FUNC_URL" --http-method=POST \
      --oidc-service-account-email="$SA" --oidc-token-audience="$FUNC_URL" \
      --attempt-deadline=900s || true
  fi
done

# 함수를 호출할 수 있도록 스케줄러 SA에 Invoker 권한
gcloud run services add-iam-policy-binding "$FUNC_NAME" \
  --region="$REGION" \
  --member="serviceAccount:$SA" --role="roles/run.invoker" >/dev/null 2>&1 || true

# ── 6) 즉시 1회 실행 (테스트) ──
echo "▶ [6/6] 지금 한 번 실행해 테스트합니다..."
gcloud scheduler jobs run "${FUNC_NAME}-daily" --location="$REGION" || true

echo
echo "✅ 배포 완료!"
echo "   - 매일 ${SCHEDULE} (${TZ}) 자동 실행됩니다."
echo "   - 로그: gcloud functions logs read $FUNC_NAME --region=$REGION --gen2 --limit=50"
echo "   - 올공 '채용정보' 게시판에서 결과를 확인하세요."

# ── 7) 쇼츠용: 함수가 실제 사용하는 서비스계정(로봇 계정) 이메일 안내 ──
RUNTIME_SA=$(gcloud functions describe "$FUNC_NAME" --gen2 --region="$REGION" \
  --format='value(serviceConfig.serviceAccountEmail)' 2>/dev/null || true)
if [ -n "$RUNTIME_SA" ]; then
  echo
  echo "🎬 [쇼츠 사용 시] 구글 드라이브 '[shorts]' 폴더를 아래 이메일에 '편집자'로 공유하세요:"
  echo "   👉 $RUNTIME_SA"
  echo "   (drive.google.com 웹에서 폴더 우클릭 → 공유 → 위 이메일 입력)"
  echo "   공유를 마친 뒤 다음 명령으로 즉시 다시 실행해 확인할 수 있습니다:"
  echo "   gcloud scheduler jobs run ${FUNC_NAME}-daily --location=$REGION"
fi
