"""
SNS(인스타그램/쓰레드) 자동 게시 모듈
────────────────────────────────────
매일 크롤링 완료 직후 '오늘의 채용공고 요약'을 인스타그램과 쓰레드에 자동 게시한다.

■ 설정 위치 (비밀값은 절대 코드/저장소에 넣지 않음)
  Firestore `_config` 컬렉션 → `social` 문서에서 읽는다. 필드:
    threads_user_id        : 쓰레드 사용자 ID
    threads_access_token   : 쓰레드 장기 액세스 토큰 (이 모듈이 7일마다 자동 연장 → 사실상 영구)
    ig_user_id             : 인스타그램 비즈니스 계정 ID
    ig_access_token        : 인스타그램(페이지) 액세스 토큰 (무기한 페이지 토큰 권장)
    ig_image_url           : 인스타그램 게시용 이미지 URL (정사각형 1080x1080 권장, 필수)
    enabled                : false 로 두면 전체 비활성 (기본 활성)
    last_posted_date       : (자동 기록) 마지막 게시일 — 하루 1회만 게시하도록 방지

■ 동작 원칙
  - 설정 문서가 없거나 토큰이 비어 있으면 조용히 건너뜀 (크롤링에 영향 없음)
  - 인스타/쓰레드 중 설정된 쪽만 게시
  - 같은 날 크롤러가 여러 번 돌아도 게시는 하루 1회만
"""

import requests
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
SITE_URL = "https://www.allgongin.com"
THREADS_HOST = "https://graph.threads.net"
THREADS_API = THREADS_HOST + "/v1.0"
# 인스타그램: '인스타그램 로그인' 방식(신규 API, 페이스북 페이지 불필요)
IG_HOST = "https://graph.instagram.com"
IG_API = IG_HOST + "/v21.0"


def _today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _cfg_ref(db):
    return db.collection("_config").document("social")


def _build_caption(df) -> str:
    """오늘의 공고 요약 캡션 (인스타/쓰레드 공용)."""
    total = len(df)
    lines = [f"📢 오늘의 공공기관 채용공고 업데이트 (총 {total}건)", ""]
    try:
        top = df.sort_values(by="공고시작일", ascending=False).head(5)
    except Exception:  # noqa: BLE001
        top = df.head(5)
    for _, r in top.iterrows():
        org = str(r.get("기관명", "")).strip()
        title = str(r.get("채용공고제목", "")).strip()
        end = r.get("공고종료일")
        end_s = ""
        try:
            if end is not None and str(end) != "NaT":
                end_s = f" (~{end.month}/{end.day})"
        except Exception:  # noqa: BLE001
            end_s = ""
        lines.append(f"▪ {org} | {title}{end_s}")
    lines += [
        "",
        f"👉 전체 공고와 채용달력은 올공에서: {SITE_URL}",
        "",
        "#공공기관채용 #공기업채용 #채용공고 #공채 #취업준비 #올공",
    ]
    return "\n".join(lines)


def _post_threads(cfg: dict, caption: str) -> bool:
    """쓰레드 텍스트 게시 (2단계: 컨테이너 생성 → 발행)."""
    uid, tok = cfg.get("threads_user_id"), cfg.get("threads_access_token")
    if not uid or not tok:
        print("  (쓰레드 설정 없음 → 건너뜀)")
        return False
    text = caption if len(caption) <= 480 else caption[:477] + "..."
    r = requests.post(
        f"{THREADS_API}/{uid}/threads",
        data={"media_type": "TEXT", "text": text, "access_token": tok},
        timeout=30,
    )
    r.raise_for_status()
    creation_id = r.json().get("id")
    r2 = requests.post(
        f"{THREADS_API}/{uid}/threads_publish",
        data={"creation_id": creation_id, "access_token": tok},
        timeout=30,
    )
    r2.raise_for_status()
    print(f"  ✅ 쓰레드 게시 완료: {r2.json().get('id')}")
    return True


def _post_instagram(cfg: dict, caption: str) -> bool:
    """인스타그램 이미지+캡션 게시 (인스타그램 로그인 방식, 2단계: 컨테이너 생성 → 발행).
    ig_user_id 가 없으면 토큰으로 자동 조회."""
    tok, img = cfg.get("ig_access_token"), cfg.get("ig_image_url")
    if not tok or not img:
        print("  (인스타그램 설정 없음(토큰/이미지URL) → 건너뜀)")
        return False
    uid = cfg.get("ig_user_id")
    if not uid:
        r0 = requests.get(f"{IG_API}/me", params={"fields": "user_id,username", "access_token": tok}, timeout=30)
        r0.raise_for_status()
        j0 = r0.json()
        uid = j0.get("user_id") or j0.get("id")
        print(f"  (인스타그램 계정 자동 확인: @{j0.get('username')} / {uid})")
    r = requests.post(
        f"{IG_API}/{uid}/media",
        data={"image_url": img, "caption": caption[:2100], "access_token": tok},
        timeout=60,
    )
    r.raise_for_status()
    creation_id = r.json().get("id")
    r2 = requests.post(
        f"{IG_API}/{uid}/media_publish",
        data={"creation_id": creation_id, "access_token": tok},
        timeout=60,
    )
    r2.raise_for_status()
    print(f"  ✅ 인스타그램 게시 완료: {r2.json().get('id')}")
    return True


def _maybe_refresh_ig_token(db, cfg: dict) -> None:
    """인스타그램 장기 토큰(유효 60일)을 7일마다 자동 연장 → 별도 관리 없이 영구 유지."""
    tok = cfg.get("ig_access_token")
    if not tok:
        return
    last = str(cfg.get("ig_token_refreshed") or "")
    if last >= (datetime.now(KST) - timedelta(days=7)).strftime("%Y-%m-%d"):
        return
    try:
        r = requests.get(
            f"{IG_HOST}/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": tok},
            timeout=30,
        )
        r.raise_for_status()
        new_tok = r.json().get("access_token")
        if new_tok:
            _cfg_ref(db).set(
                {"ig_access_token": new_tok, "ig_token_refreshed": _today_kst()},
                merge=True,
            )
            print("  🔄 인스타그램 토큰 자동 연장 완료")
    except Exception as e:  # noqa: BLE001
        print(f"  (인스타그램 토큰 연장 실패 — 다음 실행 때 재시도: {e})")


def _maybe_refresh_threads_token(db, cfg: dict) -> None:
    """쓰레드 장기 토큰(유효 60일)을 7일마다 자동 연장 → 별도 관리 없이 영구 유지."""
    tok = cfg.get("threads_access_token")
    if not tok:
        return
    last = str(cfg.get("threads_token_refreshed") or "")
    if last >= (datetime.now(KST) - timedelta(days=7)).strftime("%Y-%m-%d"):
        return
    try:
        r = requests.get(
            f"{THREADS_HOST}/refresh_access_token",
            params={"grant_type": "th_refresh_token", "access_token": tok},
            timeout=30,
        )
        r.raise_for_status()
        new_tok = r.json().get("access_token")
        if new_tok:
            _cfg_ref(db).set(
                {"threads_access_token": new_tok, "threads_token_refreshed": _today_kst()},
                merge=True,
            )
            print("  🔄 쓰레드 토큰 자동 연장 완료")
    except Exception as e:  # noqa: BLE001
        print(f"  (쓰레드 토큰 연장 실패 — 다음 실행 때 재시도: {e})")


def post_daily(db, df) -> None:
    """크롤링 완료 후 호출되는 진입점. 설정 없으면 무동작, 하루 1회만 게시."""
    snap = _cfg_ref(db).get()
    cfg = snap.to_dict() if snap.exists else None
    if not cfg or cfg.get("enabled") is False:
        print("  (SNS 자동 게시 설정 없음/비활성 → 건너뜀)")
        return
    today = _today_kst()
    if str(cfg.get("last_posted_date") or "") == today:
        print("  (오늘 이미 SNS 게시함 → 건너뜀)")
        _maybe_refresh_threads_token(db, cfg)
        _maybe_refresh_ig_token(db, cfg)
        return
    if df is None or len(df) == 0:
        print("  (게시할 공고 없음 → 건너뜀)")
        return

    caption = _build_caption(df)
    ok_threads = ok_ig = False
    try:
        ok_threads = _post_threads(cfg, caption)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️ 쓰레드 게시 실패: {e}")
    try:
        ok_ig = _post_instagram(cfg, caption)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️ 인스타그램 게시 실패: {e}")

    if ok_threads or ok_ig:
        _cfg_ref(db).set({"last_posted_date": today}, merge=True)
    _maybe_refresh_threads_token(db, cfg)
    _maybe_refresh_ig_token(db, cfg)
