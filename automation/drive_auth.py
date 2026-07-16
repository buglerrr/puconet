"""
구글 드라이브 업로드용 1회 인증 스크립트 (Cloud Shell 에서 실행)
──────────────────────────────────────────────────────────
구글 정책상 서비스계정(로봇 계정)은 개인 '내 드라이브'에 파일을 소유할 수
없으므로, 쇼츠 영상은 소유자 본인 계정 명의로 업로드해야 합니다.
이 스크립트는 '기기 코드 방식'(URL + 코드 입력)으로 본인 계정의 인증을
한 번 받아 Firestore `_config/shorts` 에 저장합니다. 이후에는 전 과정 자동.

■ 사전 준비 (구글 클라우드 콘솔에서 한 번만)
  1) https://console.cloud.google.com/apis/credentials/consent?project=recruit-board
     → User Type: 외부(External) → 앱 이름/이메일만 채우고 저장
     → '앱 게시'(프로덕션 전환) 버튼 클릭
       (drive.file 은 민감하지 않은 범위라 별도 심사 불필요.
        '테스트' 상태로 두면 7일마다 인증이 만료되니 꼭 게시하세요)
  2) https://console.cloud.google.com/apis/credentials?project=recruit-board
     → 사용자 인증 정보 만들기 → OAuth 클라이언트 ID
     → 애플리케이션 유형: 'TV 및 제한된 입력 장치'
     → 만들어진 클라이언트 ID / 보안 비밀(secret)을 복사

■ 실행
  cd ~/puconet/automation && python3 drive_auth.py
"""

import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request

PROJECT = "recruit-board"
SCOPE = "https://www.googleapis.com/auth/drive.file"
DEVICE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def _post(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def main():
    print("── 올공 쇼츠: 드라이브 업로드 1회 인증 ──")
    client_id = input("OAuth 클라이언트 ID: ").strip()
    client_secret = input("OAuth 클라이언트 보안 비밀(secret): ").strip()
    if not client_id or not client_secret:
        print("클라이언트 ID/보안 비밀이 필요합니다. 스크립트 상단의 '사전 준비'를 참고하세요.")
        sys.exit(1)

    dev = _post(DEVICE_URL, {"client_id": client_id, "scope": SCOPE})
    if "device_code" not in dev:
        print(f"기기 코드 발급 실패: {dev}")
        print("→ OAuth 클라이언트 유형이 'TV 및 제한된 입력 장치'인지 확인하세요.")
        sys.exit(1)

    print()
    print("┌─────────────────────────────────────────────")
    print(f"│ 1) 브라우저에서 접속: {dev.get('verification_url', 'https://www.google.com/device')}")
    print(f"│ 2) 코드 입력:        {dev['user_code']}")
    print("│ 3) 본인 구글 계정으로 로그인 후 '허용' 클릭")
    print("└─────────────────────────────────────────────")
    print("(승인을 기다리는 중...)")

    interval = int(dev.get("interval", 5))
    token = None
    for _ in range(int(dev.get("expires_in", 1800) / interval)):
        time.sleep(interval)
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
            continue
        if err == "slow_down":
            interval += 2
            continue
        print(f"인증 실패: {t}")
        sys.exit(1)
    if not token:
        print("시간 초과 — 다시 실행해 주세요.")
        sys.exit(1)
    print("✅ 인증 성공! Firestore 에 저장합니다...")

    fields = {
        "drive_client_id": client_id,
        "drive_client_secret": client_secret,
        "drive_refresh_token": token["refresh_token"],
    }
    try:
        try:
            from google.cloud import firestore  # noqa: PLC0415
        except ImportError:
            print("(필요한 패키지를 설치합니다...)")
            subprocess.run([sys.executable, "-m", "pip", "install", "--user", "-q",
                            "google-cloud-firestore"], check=True)
            from google.cloud import firestore  # noqa: PLC0415
        db = firestore.Client(project=PROJECT)
        db.collection("_config").document("shorts").set(fields, merge=True)
        print("✅ 저장 완료! (_config/shorts)")
        print()
        print("다음 단계: Cloud Scheduler 에서 'job-sync-daily' 를 강제 실행하면")
        print("영상이 드라이브에 저장됩니다. 첫 실행 시 '올공 쇼츠' 폴더가 자동 생성되며,")
        print("이 폴더를 드라이브에서 원하는 위치([공기업 브레인넷] 등)로 옮겨두면")
        print("이후 영상도 계속 그곳에 저장됩니다.")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ Firestore 자동 저장 실패: {e}")
        print("Firebase 콘솔에서 _config → shorts 문서에 아래 3개 필드(string)를 직접 추가하세요:")
        for k, v in fields.items():
            print(f"  {k} = {v}")


if __name__ == "__main__":
    main()
