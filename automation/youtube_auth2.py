"""
유튜브 업로드+댓글용 1회 인증 스크립트 — 웹 방식 (Cloud Shell 에서 실행)
──────────────────────────────────────────────────────────
유튜브 '댓글 작성' 권한은 TV 기기 코드 방식으로는 받을 수 없어(구글 제한),
웹 방식으로 한 번 인증합니다. 이 인증 하나로 업로드+댓글이 모두 됩니다.

■ 사전 준비 (구글 클라우드 콘솔에서 한 번만)
  1) https://console.cloud.google.com/apis/credentials?project=recruit-board
  2) '+ 사용자 인증 정보 만들기' → 'OAuth 클라이언트 ID'
  3) 애플리케이션 유형: '웹 애플리케이션' / 이름: 올공 유튜브 (아무거나)
  4) '승인된 리디렉션 URI' → 'URI 추가' 클릭 후 아래 주소를 정확히 입력:
       https://www.allgongin.com/yt_auth.html
  5) '만들기' → 표시되는 클라이언트 ID / 보안 비밀(secret) 복사

■ 실행
  cd ~/puconet/automation && python3 youtube_auth2.py
  → 안내되는 URL 접속 → 유튜브 채널 계정으로 로그인·허용
  → 올공 페이지에 표시되는 코드를 복사해 붙여넣기
"""

import json
import subprocess
import sys
import urllib.parse
import urllib.request

PROJECT = "recruit-board"
REDIRECT = "https://www.allgongin.com/yt_auth.html"
SCOPES = ("https://www.googleapis.com/auth/youtube.upload "
          "https://www.googleapis.com/auth/youtube.force-ssl")
TOKEN_URL = "https://oauth2.googleapis.com/token"
DOC_URL = (f"https://firestore.googleapis.com/v1/projects/{PROJECT}"
           "/databases/(default)/documents/_config/shorts")


def _gcloud_token() -> str:
    return subprocess.run(["gcloud", "auth", "print-access-token"],
                          capture_output=True, text=True, check=True).stdout.strip()


def _load_saved_client():
    try:
        req = urllib.request.Request(DOC_URL, headers={"Authorization": f"Bearer {_gcloud_token()}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            fields = json.loads(r.read().decode()).get("fields", {})
        return (fields.get("yt_web_client_id", {}).get("stringValue", ""),
                fields.get("yt_web_client_secret", {}).get("stringValue", ""))
    except Exception:  # noqa: BLE001
        return "", ""


def _save_fields(fields: dict):
    mask = "&".join(f"updateMask.fieldPaths={k}" for k in fields)
    body = json.dumps({"fields": {k: {"stringValue": v} for k, v in fields.items()}}).encode()
    req = urllib.request.Request(f"{DOC_URL}?{mask}", data=body, method="PATCH", headers={
        "Authorization": f"Bearer {_gcloud_token()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def main():
    print("── 올공 쇼츠: 유튜브(업로드+댓글) 1회 인증 — 웹 방식 ──")
    client_id, client_secret = _load_saved_client()
    if client_id and client_secret:
        print("✅ 저장된 웹 OAuth 클라이언트를 재사용합니다.")
    else:
        print("(스크립트 상단 '사전 준비'대로 만든 '웹 애플리케이션' 클라이언트 정보를 입력하세요)")
        client_id = input("웹 클라이언트 ID: ").strip()
        client_secret = input("웹 클라이언트 보안 비밀(secret): ").strip()
        if not client_id or not client_secret:
            print("클라이언트 ID/보안 비밀이 필요합니다.")
            sys.exit(1)

    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code",
        "scope": SCOPES, "access_type": "offline", "prompt": "consent",
    })
    print()
    print("┌─────────────────────────────────────────────")
    print("│ 1) 아래 URL 을 브라우저에서 열어 주세요 (전체 복사):")
    print(f"│ {auth_url}")
    print("│ 2) '유튜브 채널이 있는' 구글 계정으로 로그인 → 허용")
    print("│    ('확인하지 않은 앱' 경고가 나오면: 고급 → 이동 클릭)")
    print("│ 3) 올공 인증 페이지가 뜨면 '코드 복사하기' 클릭")
    print("└─────────────────────────────────────────────")
    code = input("복사한 인증 코드 붙여넣기: ").strip()
    if not code:
        print("코드가 비어 있습니다. 다시 실행해 주세요.")
        sys.exit(1)

    body = urllib.parse.urlencode({
        "code": code, "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": REDIRECT, "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            token = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"토큰 교환 실패: {e.read().decode()[:300]}")
        print("→ 리디렉션 URI(https://www.allgongin.com/yt_auth.html)가 클라이언트에 등록됐는지,")
        print("  코드를 발급 후 바로(수 분 내) 사용했는지 확인하고 다시 실행해 주세요.")
        sys.exit(1)
    rtok = token.get("refresh_token")
    if not rtok:
        print(f"리프레시 토큰이 없습니다: {token}")
        sys.exit(1)
    print("✅ 인증 성공! Firestore 에 저장합니다...")

    fields = {"youtube_refresh_token": rtok,
              "yt_web_client_id": client_id, "yt_web_client_secret": client_secret}
    try:
        _save_fields(fields)
        print("✅ 저장 완료! 이제 매일 쇼츠가 유튜브에 업로드되고 설명 요약 댓글도 자동 작성됩니다.")
        print("(즉시 확인: Cloud Scheduler 에서 job-sync-daily 강제 실행)")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ Firestore 자동 저장 실패: {e}")
        print("Firebase 콘솔에서 _config → shorts 문서에 아래 필드(string)를 직접 추가/수정하세요:")
        for k, v in fields.items():
            print(f"  {k} = {v}")


if __name__ == "__main__":
    main()
