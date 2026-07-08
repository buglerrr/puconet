"""
SNS(인스타그램/쓰레드) 자동 게시 모듈
────────────────────────────────────
크롤링 완료 직후 채용공고 요약을 인스타그램과 쓰레드에 자동 게시한다.
하루 3회(슬롯별)로 각기 다른 내용을 게시한다:
  - morning (오전 ~10시 실행분) : 최신 등록 공고 5건
  - noon    (10~15시 실행분)    : 마감 임박 공고 5건
  - evening (15시~ 실행분)      : 정규직 공고 5건
인스타그램 이미지는 해당 게시물에 담긴 '채용기관 CI'가 들어간 카드를
매번 자동 생성해 Storage 에 올려 사용한다(실패 시 기본 카드 이미지 사용).

■ 설정 위치 (비밀값은 절대 코드/저장소에 넣지 않음)
  Firestore `_config` 컬렉션 → `social` 문서 필드:
    threads_user_id, threads_access_token : 쓰레드 (장기 토큰; 7일마다 자동 연장)
    ig_access_token                       : 인스타그램 (장기 토큰; 7일마다 자동 연장)
    ig_user_id                            : (선택) 없으면 토큰으로 자동 조회
    ig_image_url                          : 카드 생성 실패 시 사용할 기본 이미지(JPG)
    enabled                               : false 로 두면 전체 비활성
  게시 기록(자동 관리): last_threads_{슬롯}, last_ig_{슬롯} — 슬롯별 하루 1회만 게시
"""

import io
import os
import re
import time

import requests
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
SITE_URL = "https://www.allgongin.com"
THREADS_HOST = "https://graph.threads.net"
THREADS_API = THREADS_HOST + "/v1.0"
IG_HOST = "https://graph.instagram.com"
IG_API = IG_HOST + "/v21.0"
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

# 슬롯별 게시 콘셉트 (카드 머리말 텍스트에는 이모지 사용 금지 — 폰트에 이모지 글리프 없음)
SLOT_META = {
    "morning": {
        "band": "#1c7ed6",
        "card_title": ("오늘의 신규", "채용공고 업데이트"),
        "cap_title": "📢 오늘의 공공기관 채용공고 업데이트 (총 {total}건)",
        "tags": "#공공기관채용 #공기업채용 #채용공고 #공채 #취업준비 #올공",
    },
    "noon": {
        "band": "#e8590c",
        "card_title": ("마감 임박!", "놓치면 안 될 채용공고"),
        "cap_title": "⏰ 마감 임박! 놓치면 안 될 공공기관 채용공고",
        "tags": "#마감임박 #공공기관채용 #채용공고 #취업 #올공",
    },
    "evening": {
        "band": "#2b8a3e",
        "card_title": ("정규직만 모았다", "오늘의 채용공고"),
        "cap_title": "💼 정규직만 골라 담은 오늘의 공공기관 채용공고",
        "tags": "#정규직채용 #공공기관채용 #공기업 #신입채용 #올공",
    },
}


def _today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _slot_now() -> str:
    h = datetime.now(KST).hour
    if h < 10:
        return "morning"
    if h < 15:
        return "noon"
    return "evening"


def _cfg_ref(db):
    return db.collection("_config").document("social")


# ─────────────────────── 콘텐츠 선별/문구 ───────────────────────
def _is_alone_regular(v) -> bool:
    parts = [p.strip() for p in re.split(r"[,+/]", str(v or "")) if p.strip()]
    return len(parts) == 1 and parts[0] == "정규직"


def _select_rows(df, slot):
    """슬롯별로 게시할 공고 최대 5건 선별."""
    import pandas as pd
    d = df
    try:
        if slot == "noon":
            today = pd.Timestamp(datetime.now(KST).date())
            d = df[df["공고종료일"].notna() & (df["공고종료일"] >= today)].sort_values("공고종료일")
        elif slot == "evening":
            mask = df["고용유형"].apply(_is_alone_regular)
            d = df[mask]
            if len(d) == 0:
                d = df[df["고용유형"].astype(str).str.contains("정규직", na=False)]
            d = d.sort_values("공고시작일", ascending=False)
        else:  # morning
            d = df.sort_values("공고시작일", ascending=False)
    except Exception:  # noqa: BLE001
        d = df
    if len(d) == 0:
        d = df
    return d.head(5)


def _end_suffix(end) -> str:
    try:
        if end is not None and str(end) != "NaT":
            return f" (~{end.month}/{end.day})"
    except Exception:  # noqa: BLE001
        pass
    return ""


def _dday_text(end) -> str:
    try:
        if end is None or str(end) == "NaT":
            return ""
        days = (end.date() - datetime.now(KST).date()).days
        if days < 0:
            return "마감"
        if days == 0:
            return "D-DAY"
        return f"D-{days}"
    except Exception:  # noqa: BLE001
        return ""


def _build_caption(slot: str, df, rows) -> str:
    meta = SLOT_META[slot]
    lines = [meta["cap_title"].format(total=len(df)), ""]
    for _, r in rows.iterrows():
        org = str(r.get("기관명", "")).strip()
        title = str(r.get("채용공고제목", "")).strip()
        lines.append(f"▪ {org} | {title}{_end_suffix(r.get('공고종료일'))}")
    lines += ["", f"👉 전체 공고와 채용달력은 올공에서: {SITE_URL}", "", meta["tags"]]
    return "\n".join(lines)


# ─────────────────────── 카드 이미지 생성 ───────────────────────
def _fetch_logo(url):
    try:
        if not url or str(url).startswith("./"):
            return None
        r = requests.get(url, timeout=10)
        if not r.ok:
            return None
        from PIL import Image
        return Image.open(io.BytesIO(r.content)).convert("RGBA")
    except Exception:  # noqa: BLE001
        return None


def _make_card(slot: str, rows) -> bytes:
    """게시 대상 기관들의 CI 로고가 들어간 1080x1080 카드(JPG bytes) 생성."""
    from PIL import Image, ImageDraw, ImageFont

    meta = SLOT_META[slot]
    band = meta["band"]
    xb = lambda s: ImageFont.truetype(os.path.join(FONT_DIR, "NanumGothicExtraBold.ttf"), s)  # noqa: E731
    rg = lambda s: ImageFont.truetype(os.path.join(FONT_DIR, "NanumGothic.ttf"), s)  # noqa: E731

    W = H = 1080
    img = Image.new("RGB", (W, H), "#ffffff")
    d = ImageDraw.Draw(img)

    def center(t, f, y, fill):
        w = d.textlength(t, font=f)
        d.text(((W - w) / 2, y), t, font=f, fill=fill)

    # 상단 밴드 (슬롯별 색)
    d.rectangle([0, 0, W, 240], fill=band)
    center(meta["card_title"][0], xb(70), 34, "#ffffff")
    center(meta["card_title"][1], xb(70), 128, "#ffffff")
    d.text((70, 262), datetime.now(KST).strftime("%Y년 %m월 %d일"), font=rg(30), fill="#868e96")

    # 기관 로고 URL 맵 (크롤러 유틸 재사용)
    logo_map = {}
    try:
        import main as _crawler
        logo_map = _crawler.load_logo_files(_crawler.storage.bucket())
    except Exception as e:  # noqa: BLE001
        print(f"  (로고 목록 로드 실패 — 로고 없이 카드 생성: {e})")

    y = 322
    row_h = 124
    row_list = list(rows.iterrows())
    for idx, (_, r) in enumerate(row_list):
        org = str(r.get("기관명", "")).strip()
        title = str(r.get("채용공고제목", "")).strip()
        end = r.get("공고종료일")

        # 로고 박스
        bx, by, bw, bh = 70, y, 170, 96
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=12, outline="#e9ecef", width=2, fill="#ffffff")
        logo = None
        if logo_map:
            try:
                import main as _crawler
                logo = _fetch_logo(_crawler.get_logo_url(org, logo_map))
            except Exception:  # noqa: BLE001
                logo = None
        if logo is not None:
            logo.thumbnail((bw - 16, bh - 16))
            img.paste(logo, (bx + (bw - logo.width) // 2, by + (bh - logo.height) // 2), logo)
        else:
            ini = org[:1] if org else "?"
            f = xb(44)
            w = d.textlength(ini, font=f)
            d.text((bx + (bw - w) / 2, by + 22), ini, font=f, fill=band)

        # 기관명 + 공고 제목
        tx = bx + bw + 28
        org_t = org if len(org) <= 13 else org[:12] + "…"
        d.text((tx, y + 4), org_t, font=xb(40), fill="#1d2939")
        tt = title if len(title) <= 26 else title[:25] + "…"
        d.text((tx, y + 58), tt, font=rg(28), fill="#495057")

        # 우측 D-day / 마감일
        dd = _dday_text(end)
        if dd:
            f = xb(30)
            w = d.textlength(dd, font=f)
            d.text((W - 70 - w, y + 10), dd, font=f, fill="#e03131")
            try:
                es = f"~{end.month}/{end.day}"
                f2 = rg(24)
                w2 = d.textlength(es, font=f2)
                d.text((W - 70 - w2, y + 52), es, font=f2, fill="#868e96")
            except Exception:  # noqa: BLE001
                pass

        if idx < len(row_list) - 1:
            d.line([70, y + row_h - 14, W - 70, y + row_h - 14], fill="#f1f3f5", width=2)
        y += row_h

    # 하단 밴드
    d.rectangle([0, 960, W, H], fill="#f8f9fa")
    d.rectangle([0, 960, W, 963], fill="#e9ecef")
    center("공공기관 채용과 교육의 모든 것, 올공", rg(28), 980, "#868e96")
    center("www.allgongin.com", xb(48), 1016, band)

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90, optimize=True)
    return buf.getvalue()


def _upload_card(jpg_bytes: bytes, slot: str):
    """생성한 카드를 Storage 에 올리고 공개 URL 반환 (실패 시 None)."""
    try:
        import main as _crawler
        bucket = _crawler.storage.bucket()
        blob = bucket.blob(f"social/card-{_today_kst()}-{slot}.jpg")
        blob.upload_from_string(jpg_bytes, content_type="image/jpeg")
        blob.make_public()
        return blob.public_url
    except Exception as e:  # noqa: BLE001
        print(f"  (카드 업로드 실패 → 기본 이미지 사용: {e})")
        return None


# ─────────────────────── 플랫폼별 게시 ───────────────────────
def _post_threads(cfg: dict, caption: str) -> bool:
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
    if not r.ok:
        raise RuntimeError(f"컨테이너 생성 실패: {r.status_code} {r.text[:300]}")
    creation_id = r.json().get("id")
    r2 = requests.post(
        f"{THREADS_API}/{uid}/threads_publish",
        data={"creation_id": creation_id, "access_token": tok},
        timeout=30,
    )
    if not r2.ok:
        raise RuntimeError(f"발행 실패: {r2.status_code} {r2.text[:300]}")
    print(f"  ✅ 쓰레드 게시 완료: {r2.json().get('id')}")
    return True


def _post_instagram(cfg: dict, caption: str, image_url: str) -> bool:
    tok = cfg.get("ig_access_token")
    if not tok or not image_url:
        print("  (인스타그램 설정 없음(토큰/이미지URL) → 건너뜀)")
        return False
    uid = cfg.get("ig_user_id")
    if not uid:
        r0 = requests.get(f"{IG_API}/me", params={"fields": "user_id,username", "access_token": tok}, timeout=30)
        if not r0.ok:
            raise RuntimeError(f"계정 조회 실패: {r0.status_code} {r0.text[:300]}")
        j0 = r0.json()
        uid = j0.get("user_id") or j0.get("id")
        print(f"  (인스타그램 계정 자동 확인: @{j0.get('username')} / {uid})")
    # 1) 미디어 컨테이너 생성
    r = requests.post(
        f"{IG_API}/{uid}/media",
        data={"image_url": image_url, "caption": caption[:2100], "access_token": tok},
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"컨테이너 생성 실패: {r.status_code} {r.text[:300]}")
    creation_id = r.json().get("id")
    # 2) 컨테이너 처리 완료 대기 (인스타그램이 이미지를 내려받아 검증하는 데 수 초 걸림)
    status = ""
    for _ in range(20):  # 최대 약 60초
        s = requests.get(f"{IG_API}/{creation_id}", params={"fields": "status_code", "access_token": tok}, timeout=30)
        if s.ok:
            status = str((s.json() or {}).get("status_code") or "")
            if status == "FINISHED":
                break
            if status == "ERROR":
                raise RuntimeError("이미지 처리 실패(status=ERROR) — 이미지 URL 접근 가능 여부 확인 필요")
        time.sleep(3)
    print(f"  (컨테이너 상태: {status or '확인불가'} → 발행 시도)")
    # 3) 발행 (일시 오류 대비 재시도)
    last_err = ""
    for _ in range(3):
        r2 = requests.post(
            f"{IG_API}/{uid}/media_publish",
            data={"creation_id": creation_id, "access_token": tok},
            timeout=60,
        )
        if r2.ok:
            print(f"  ✅ 인스타그램 게시 완료: {r2.json().get('id')}")
            return True
        last_err = f"{r2.status_code} {r2.text[:300]}"
        time.sleep(5)
    raise RuntimeError(f"발행 실패: {last_err}")


# ─────────────────────── 토큰 자동 연장 ───────────────────────
def _maybe_refresh_threads_token(db, cfg: dict) -> None:
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
            _cfg_ref(db).set({"threads_access_token": new_tok, "threads_token_refreshed": _today_kst()}, merge=True)
            print("  🔄 쓰레드 토큰 자동 연장 완료")
    except Exception as e:  # noqa: BLE001
        print(f"  (쓰레드 토큰 연장 실패 — 다음 실행 때 재시도: {e})")


def _maybe_refresh_ig_token(db, cfg: dict) -> None:
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
            _cfg_ref(db).set({"ig_access_token": new_tok, "ig_token_refreshed": _today_kst()}, merge=True)
            print("  🔄 인스타그램 토큰 자동 연장 완료")
    except Exception as e:  # noqa: BLE001
        print(f"  (인스타그램 토큰 연장 실패 — 다음 실행 때 재시도: {e})")


# ─────────────────────── 진입점 ───────────────────────
def post_daily(db, df) -> None:
    """크롤링 완료 후 호출. 실행 시각에 따라 슬롯(아침/점심/저녁)을 정해
    슬롯별로 하루 1회만, 각기 다른 내용으로 게시한다."""
    snap = _cfg_ref(db).get()
    cfg = snap.to_dict() if snap.exists else None
    if not cfg or cfg.get("enabled") is False:
        print("  (SNS 자동 게시 설정 없음/비활성 → 건너뜀)")
        return
    if df is None or len(df) == 0:
        print("  (게시할 공고 없음 → 건너뜀)")
        return

    slot = _slot_now()
    today = _today_kst()
    print(f"  (게시 슬롯: {slot})")

    # 슬롯별 하루 1회 (구버전 기록 필드는 morning 기록으로 인정)
    legacy = str(cfg.get("last_posted_date") or "")
    threads_done = str(cfg.get(f"last_threads_{slot}") or "") == today or (
        slot == "morning" and (str(cfg.get("last_posted_threads") or "") or legacy) == today
    )
    ig_done = str(cfg.get(f"last_ig_{slot}") or "") == today or (
        slot == "morning" and str(cfg.get("last_posted_ig") or "") == today
    )

    rows = _select_rows(df, slot)
    caption = _build_caption(slot, df, rows)

    if threads_done:
        print("  (쓰레드: 이 슬롯은 오늘 이미 게시함 → 건너뜀)")
    else:
        try:
            if _post_threads(cfg, caption):
                _cfg_ref(db).set({f"last_threads_{slot}": today}, merge=True)
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ 쓰레드 게시 실패: {e}")

    if ig_done:
        print("  (인스타그램: 이 슬롯은 오늘 이미 게시함 → 건너뜀)")
    else:
        # 게시 기관 CI가 들어간 카드 이미지 생성 (실패 시 기본 이미지로 폴백)
        image_url = None
        try:
            image_url = _upload_card(_make_card(slot, rows), slot)
            if image_url:
                print("  (기관 CI 카드 이미지 생성 완료)")
        except Exception as e:  # noqa: BLE001
            print(f"  (카드 생성 실패 → 기본 이미지 사용: {e})")
        image_url = image_url or cfg.get("ig_image_url")
        try:
            if _post_instagram(cfg, caption, image_url):
                _cfg_ref(db).set({f"last_ig_{slot}": today}, merge=True)
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ 인스타그램 게시 실패: {e}")

    _maybe_refresh_threads_token(db, cfg)
    _maybe_refresh_ig_token(db, cfg)
