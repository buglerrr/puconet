"""
유튜브 쇼츠 자동 업로드용 1회 인증 스크립트 (Cloud Shell 에서 실행)
──────────────────────────────────────────────────────────
유튜브 '업로드'는 API 키로는 불가능하고(조회 전용), 채널 소유자 본인의
OAuth 인증이 필요합니다. 드라이브 때와 똑같은 '기기 코드 방식'
(URL 접속 + 코드 입력)으로 한 번만 인증하면 이후 전 과정이 자동입니다.

■ 사전 준비: 없음!
  드라이브 인증 때 만든 OAuth 클라이언트(TV 및 제한된 입력 장치)를 그대로
  재사용합니다. Firestore `_config/shorts` 에서 자동으로 읽어옵니다.

■ 실행
  cd ~/puconet/automation && python3 youtube_auth.py
  → 화면의 URL 접속 + 코드 입력 → '유튜브 채널 소유' 구글 계정으로 로그인 → 허용
  → "Google에서 확인하지 않은 앱" 경고가 나오면: '고급' → '(앱 이름)(으)로 이동' 클릭
"""

import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request

PROJECT = "recruit-board"
SCOPE = "https://www.googleapis.com/auth/youtube.upload"
DEVICE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DOC_URL = (f"https://firestore.googleapis.com/v1/projects/{PROJECT}"
           "/databases/(default)/documents/_config/shorts")


def _post(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def _gcloud_token() -> str:
    return subprocess.run(["gcloud", "auth", "print-access-token"],
                          capture_output=True, text=True, check=True).stdout.strip()


def _load_client_from_firestore():
    """드라이브 인증 때 저장한 OAuth 클라이언트 ID/비밀을 자동으로 읽어온다."""
    try:
        req = urllib.request.Request(DOC_URL, headers={"Authorization": f"Bearer {_gcloud_token()}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            fields = json.loads(r.read().decode()).get("fields", {})
        cid = fields.get("drive_client_id", {}).get("stringValue", "")
        csec = fields.get("drive_client_secret", {}).get("stringValue", "")
        return cid, csec
    except Exception as e:  # noqa: BLE001
        print(f"(Firestore 자동 조회 실패: {e} → 직접 입력으로 진행)")
        return "", ""


def main():
    print("── 올공 쇼츠: 유튜브 업로드 1회 인증 ──")
    client_id, client_secret = _load_client_from_firestore()
    if client_id and client_secret:
        print("✅ 드라이브 인증 때 저장한 OAuth 클라이언트를 재사용합니다.")
    else:
        client_id = input("OAuth 클라이언트 ID: ").strip()
        client_secret = input("OAuth 클라이언트 보안 비밀(secret): ").strip()
        if not client_id or not client_secret:
            print("클라이언트 ID/보안 비밀이 필요합니다. (drive_auth.py 상단 '사전 준비' 참고)")
            sys.exit(1)

    dev = _post(DEVICE_URL, {"client_id": client_id, "scope": SCOPE})
    if "device_code" not in dev:
        print(f"기기 코드 발급 실패: {dev}")
        sys.exit(1)

    print()
    print("┌─────────────────────────────────────────────")
    print(f"│ 1) 브라우저에서 접속: {dev.get('verification_url', 'https://www.google.com/device')}")
    print(f"│ 2) 코드 입력:        {dev['user_code']}")
    print("│ 3) '유튜브 채널이 있는' 구글 계정으로 로그인 후 '허용' 클릭")
    print("│    ※ '확인하지 않은 앱' 경고가 나오면: 고급 → '이동' 클릭")
    print("└─────────────────────────────────────────────")
    print("(승인을 기다리는 중입니다 — 점(.)이 계속 찍히는 것은 정상입니다."
          " 창을 닫거나 Ctrl+C 를 누르지 마세요!)")

    interval = int(dev.get("interval", 5))
    token = None
    waited = 0
    for _ in range(int(dev.get("expires_in", 1800) / interval)):
        time.sleep(interval)
        waited += interval
        t = _post(TOKEN_URL, {
            "client_id": client_id, "client_secret": client_secret,
            "device_code": dev["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        })
        if t.get("refresh_token"):
            token = t
            break
        err = t.get("error")
        if err == "authorization_pending":
            sys.stdout.write(f". ({waited}초 경과 — 승인 대기 중)\n" if waited % 30 == 0 else ".")
            sys.stdout.flush()
            continue
        if err == "slow_down":
            interval += 2
            continue
        print(f"\n인증 실패: {t}")
        sys.exit(1)
    print()
    if not token:
        print("시간 초과 — 다시 실행해 주세요.")
        sys.exit(1)
    print("✅ 인증 성공! Firestore 에 저장합니다...")

    fields = {"youtube_refresh_token": token["refresh_token"]}
    try:
        mask = "&".join(f"updateMask.fieldPaths={k}" for k in fields)
        body = json.dumps({"fields": {k: {"stringValue": v} for k, v in fields.items()}}).encode()
        req = urllib.request.Request(f"{DOC_URL}?{mask}", data=body, method="PATCH", headers={
            "Authorization": f"Bearer {_gcloud_token()}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        print("✅ 저장 완료! (_config/shorts → youtube_refresh_token)")
        print()
        print("다음 자동 실행부터 매일 쇼츠가 유튜브 채널에도 업로드됩니다.")
        print("(즉시 확인: Cloud Scheduler 에서 job-sync-daily 강제 실행)")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ Firestore 자동 저장 실패: {e}")
        print("Firebase 콘솔에서 _config → shorts 문서에 아래 필드(string)를 직접 추가하세요:")
        for k, v in fields.items():
            print(f"  {k} = {v}")


if __name__ == "__main__":
    main()
