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
    # 같은 기관은 1건만 — 서로 다른 기관 5곳으로 구성 (다양성 확보)
    seen = set()
    picked = []
    for _, r in d.iterrows():
        org = str(r.get("기관명", "")).strip()
        if org in seen:
            continue
        seen.add(org)
        picked.append(r)
        if len(picked) == 5:
            break
    if picked:
        return pd.DataFrame(picked)
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


def _short_link(row) -> str:
    """공고별 짧은 지원 링크 (go.html 리다이렉트). 실패 시 사이트 주소."""
    try:
        import main as _crawler
        doc_id = _crawler.stable_doc_id(row)
        return f"{SITE_URL}/go.html?c={doc_id[5:13]}"
    except Exception:  # noqa: BLE001
        return SITE_URL


def _build_caption_ig(slot: str, df, rows) -> str:
    """인스타그램용(2200자): 기관명 | 제목 (~마감) + 즉시 지원 링크."""
    meta = SLOT_META[slot]
    lines = [meta["cap_title"].format(total=len(df)), ""]
    for _, r in rows.iterrows():
        org = str(r.get("기관명", "")).strip()
        title = str(r.get("채용공고제목", "")).strip()
        lines.append(f"▪ {org} | {title}{_end_suffix(r.get('공고종료일'))}")
        lines.append(f"   👉 즉시 지원: {_short_link(r)}")
    lines += ["", f"전체 공고와 채용달력은 올공에서: {SITE_URL}", "", meta["tags"]]
    return "\n".join(lines)


def _utf16_len(s: str) -> int:
    """메타(쓰레드)가 세는 방식(UTF-16 단위)의 글자 수 — 이모지는 2로 계산됨."""
    return len(s.encode("utf-16-le")) // 2


def _hard_trim_utf16(s: str, max_units: int) -> str:
    """UTF-16 단위 기준으로 안전하게 자르기 (이모지 중간에서 깨지지 않게)."""
    if _utf16_len(s) <= max_units:
        return s
    cut = s.encode("utf-16-le")[: (max_units - 1) * 2]
    return cut.decode("utf-16-le", errors="ignore").rstrip() + "…"


# 쓰레드 글자 수 한도는 500 (초과 시 'Invalid parameter' 400 오류로 게시 거부됨)
THREADS_BUDGET = 440  # 한도 대비 여유 마진
# 2025-12-22부터 게시물당 링크 수(본문+첨부 합산) 최대 5개 — 초과 시 400 거부.
# → 공고 링크는 4개까지만 넣고, 올공 유입 링크가 항상 5번째 링크로 들어가게 한다.
THREADS_JOB_LINK_MAX = 4
# 웹사이트 유입 유도 문구 (모든 슬롯 공통 · 모든 축약 단계에서도 유지)
THREADS_PROMO = "👉 전체 공고와 채용달력은 올공에서: https://www.allgongin.com"


def _build_caption_threads(slot: str, df, rows) -> list:
    """쓰레드용 캡션 '후보 목록'(큰 것 → 작은 것 순) 생성.
    모든 후보가 두 제한(글자 500자·링크 5개)을 지키고, 올공 유입 문구를 항상 포함한다.
    게시가 거부되면 _post_threads 가 다음 후보로 재시도 — 문구가 잘려나가는 일 방지.
    공고 제목은 함께 첨부되는 카드 이미지에 표시되므로 본문에서는 생략."""
    meta = SLOT_META[slot]
    items = [r for _, r in rows.iterrows()]

    def compose(n_linked, n_named, with_tags=True):
        lines = [meta["cap_title"].format(total=len(df)), ""]
        for i, r in enumerate(items[:n_named]):
            org = str(r.get("기관명", "")).strip()
            lines.append(f"▪ {org}{_end_suffix(r.get('공고종료일'))}")
            if i < n_linked:
                lines.append(f"👉 {_short_link(r)}")
        lines += ["", THREADS_PROMO]
        if with_tags:
            lines += ["", meta["tags"]]
        return "\n".join(lines)

    n = min(THREADS_JOB_LINK_MAX, len(items))
    minimal = meta["cap_title"].format(total=len(df)) + "\n\n" + THREADS_PROMO
    candidates = [
        compose(n, n),                       # 공고 4곳 + 링크 4개 + 올공 링크
        compose(max(n - 1, 1), max(n - 1, 1)),  # 공고 3곳으로 축소
        compose(0, min(5, len(items))),      # 공고 링크 없이 기관명만 5곳 + 올공 링크
        minimal,                             # 최소: 제목 + 올공 문구
    ]
    out = []
    for c in candidates:
        if _utf16_len(c) <= THREADS_BUDGET and c not in out:
            out.append(c)
    if minimal not in out:
        out.append(minimal)
    return out


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
def _post_threads(cfg: dict, caption: str, image_url: str = None) -> bool:
    """쓰레드 게시. image_url 이 있으면 카드 이미지+텍스트, 없으면 텍스트만."""
    uid, tok = cfg.get("threads_user_id"), cfg.get("threads_access_token")
    if not uid or not tok:
        print("  (쓰레드 설정 없음 → 건너뜀)")
        return False
    # 파라미터 거부(400) 대비 단계적 재시도: 미리 만들어 둔 후보 캡션(큰 것→작은 것,
    # 모두 글자·링크 제한 준수 + 올공 유입 문구 포함)을 순서대로 시도하고,
    # 최후에는 텍스트 전용으로도 시도 — 어떤 이유로든 게시가 통째로 빠지지 않게.
    cands = [c for c in (caption if isinstance(caption, list) else [caption]) if str(c).strip()]
    attempts = [(_hard_trim_utf16(c, 480), image_url) for c in cands[:3]]
    attempts.append((_hard_trim_utf16(cands[-1], 480), None))  # 이미지가 문제인 경우 대비
    creation_id, last_err = None, ""
    for i, (text, img) in enumerate(attempts):
        payload = {"access_token": tok, "text": text}
        if img:
            payload["media_type"] = "IMAGE"
            payload["image_url"] = img
        else:
            payload["media_type"] = "TEXT"
        r = requests.post(f"{THREADS_API}/{uid}/threads", data=payload, timeout=30)
        if r.ok:
            creation_id = r.json().get("id")
            image_url = img  # 실제 사용된 형태 기준으로 이후 처리(이미지 대기 여부)
            if i:
                print(f"  (쓰레드: {i + 1}단계 축약 재시도로 컨테이너 생성 성공)")
            break
        last_err = f"{r.status_code} {r.text[:300]}"
        if r.status_code != 400:
            break  # 파라미터 문제(400)가 아니면(토큰 만료 등) 축약 재시도 무의미
        print(f"  (쓰레드 컨테이너 거부({i + 1}차) → 축약 재시도: {last_err[:120]})")
    if not creation_id:
        raise RuntimeError(f"컨테이너 생성 실패: {last_err}")
    # 이미지 게시는 처리 완료까지 대기 (텍스트는 즉시 발행 가능)
    if image_url:
        for _ in range(20):  # 최대 약 60초
            s = requests.get(
                f"{THREADS_API}/{creation_id}",
                params={"fields": "status", "access_token": tok},
                timeout=30,
            )
            if s.ok:
                st = str((s.json() or {}).get("status") or "")
                if st == "FINISHED":
                    break
                if st == "ERROR":
                    raise RuntimeError("이미지 처리 실패(status=ERROR) — 이미지 URL 접근 가능 여부 확인 필요")
            time.sleep(3)
    # 발행 (일시 오류 대비 재시도)
    last_err = ""
    for _ in range(3):
        r2 = requests.post(
            f"{THREADS_API}/{uid}/threads_publish",
            data={"creation_id": creation_id, "access_token": tok},
            timeout=30,
        )
        if r2.ok:
            tid = r2.json().get("id")
            print(f"  ✅ 쓰레드 게시 완료{'(이미지 포함)' if image_url else ''}: {tid}")
            return tid or True  # 게시글 ID 반환 (댓글 작성용)
        last_err = f"{r2.status_code} {r2.text[:300]}"
        time.sleep(5)
    raise RuntimeError(f"발행 실패: {last_err}")


# ─────────── 쿠팡파트너스 교재 댓글 (게시 직후 첫 답글로 자동 작성) ───────────
# 본문에 상업 링크를 넣으면 도달률이 떨어질 수 있어 '링크는 댓글에' 방식 사용.
# 게시 때마다 아래 카탈로그에서 무작위로 한 권 선택.
# ※ 쓰레드 댓글은 현재 '일시 중지' 상태(2026-07, 유입 감소 우려로 교수님 요청).
#    재개하려면 _config/social 문서에 coupang_reply: true (boolean) 필드 추가 — 재배포 불필요.
# 인스타그램 댓글은 계속 동작. 끄려면 coupang_reply_ig: false 추가.
COUPANG_CATALOG = [
    {"copy": "사무직 필기는 결국 NCS 싸움! 기출로 감 잡고 시작하는 게 국룰 ✍️",
     "url": "https://link.coupang.com/a/fsdNI9YCEn"},   # 경영·회계·사무 (PSAT형 기출예상)
    {"copy": "금융 공기업 노린다면 피셋형은 선택이 아니라 필수! 300제로 한 번에 정리 💰",
     "url": "https://link.coupang.com/a/fsedWljjtA"},   # 금융·보험
    {"copy": "전기직 전공필기, 통합기본서 한 권이면 세팅 끝 ⚡",
     "url": "https://link.coupang.com/a/fsef8D9lYq"},   # 전기·전자
    {"copy": "기계직도 결국 NCS부터! 2주 완성으로 단기 승부 🔧",
     "url": "https://link.coupang.com/a/fsehSaTM4a"},   # 기계
    {"copy": "토목직 필기는 기출동형 모의고사로 실전 감각부터 🏗️",
     "url": "https://link.coupang.com/a/fsel3gBPPg"},   # 건설(토목)
    {"copy": "모듈형+피듈형+PSAT형 한 권 정리가 정답 🏢",
     "url": "https://link.coupang.com/a/fseqg338TY"},   # 건설(건축)
    {"copy": "화학 직무능력평가, 개념부터 실전까지 이 책 하나로 🧪",
     "url": "https://link.coupang.com/a/fsevNaGgsS"},   # 화학·바이오 (=IT 동일 링크라 1회만 수록)
    {"copy": "보건·의료 계열 공공기관, 통합기본서로 필기 한 방에 🩺",
     "url": "https://link.coupang.com/a/fseAQKTpFQ"},   # 보건·의료
    {"copy": "6대 출제사 찐기출! 필기는 결국 기출이 답 ♻️",
     "url": "https://link.coupang.com/a/fseEVLUOdM"},   # 환경·에너지·안전
    {"copy": "벼락치기 타임 ⏰ 찐기출로 실전 감각 급속충전!",
     "url": "https://link.coupang.com/a/fseHuEQlci"},   # 운전·운송(물류)
    {"copy": "법무직 전공필기, 최단기 문제풀이로 효율 극대화 ⚖️",
     "url": "https://link.coupang.com/a/fseJjCIUuG"},   # 법률·법무
    {"copy": "정출연 노리는 연구자라면? 통합편 NCS로 필기 준비 완료 🔬",
     "url": "https://link.coupang.com/a/fseKIsQjIq"},   # 연구직
    {"copy": "어떤 직렬이든 NCS 기본기가 합격의 시작! 기본서로 탄탄하게 🚀",
     "url": "https://link.coupang.com/a/fseb3PYXym"},   # 종합 기본서(초록이)
]
COUPANG_DISCLOSURE = "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."


def _coupang_comment_text() -> str:
    """카탈로그에서 무작위 교재를 뽑아 댓글 문구 생성 (쓰레드/인스타 공용)."""
    import random
    book = random.choice(COUPANG_CATALOG)
    return (f"{book['copy']}\n"
            f"🎓 공공기관 경영평가위원 현직 교수 PICK\n"
            f"👉 {book['url']}\n\n{COUPANG_DISCLOSURE}")


def _post_coupang_reply(cfg: dict, thread_id: str, slot: str) -> bool:
    """방금 올린 쓰레드 게시글에 교재 추천 댓글(첫 답글)을 단다. 실패해도 본 게시에는 영향 없음.
    ※ 기본 '일시 중지' — _config/social 에 coupang_reply: true 를 넣어야만 동작."""
    if cfg.get("coupang_reply") is not True:
        return False
    uid, tok = cfg.get("threads_user_id"), cfg.get("threads_access_token")
    if not uid or not tok or not thread_id or thread_id is True:
        return False
    text = _coupang_comment_text()
    r = requests.post(f"{THREADS_API}/{uid}/threads", data={
        "access_token": tok, "media_type": "TEXT",
        "text": text, "reply_to_id": thread_id,
    }, timeout=30)
    if not r.ok:
        msg = r.text[:300]
        if "permission" in msg.lower() or "scope" in msg.lower() or r.status_code == 403:
            print("  ⚠️ 교재 댓글 실패: 쓰레드 토큰에 답글 권한(threads_manage_replies)이 없습니다."
                  " 토큰 재발급 시 이 권한을 포함해 주세요.")
        else:
            print(f"  ⚠️ 교재 댓글 컨테이너 실패: {r.status_code} {msg}")
        return False
    creation_id = r.json().get("id")
    r2 = requests.post(f"{THREADS_API}/{uid}/threads_publish",
                       data={"creation_id": creation_id, "access_token": tok}, timeout=30)
    if r2.ok:
        print(f"  ✅ 교재 추천 댓글 작성 완료: {r2.json().get('id')}")
        return True
    print(f"  ⚠️ 교재 댓글 발행 실패: {r2.status_code} {r2.text[:300]}")
    return False


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
            mid = r2.json().get("id")
            print(f"  ✅ 인스타그램 게시 완료: {mid}")
            return mid or True  # 미디어 ID 반환 (댓글 작성용)
        last_err = f"{r2.status_code} {r2.text[:300]}"
        time.sleep(5)
    raise RuntimeError(f"발행 실패: {last_err}")


def _post_coupang_comment_ig(cfg: dict, media_id: str) -> bool:
    """방금 올린 인스타그램 게시물에 교재 추천 댓글을 단다. 실패해도 본 게시에는 영향 없음.
    ※ 인스타그램 댓글의 URL은 앱에서 클릭이 안 되고 복사해야 함(플랫폼 제한)."""
    if cfg.get("coupang_reply_ig") is False:
        return False
    tok = cfg.get("ig_access_token")
    if not tok or not media_id or media_id is True:
        return False
    text = _coupang_comment_text()
    r = requests.post(f"{IG_API}/{media_id}/comments",
                      data={"message": text, "access_token": tok}, timeout=30)
    if r.ok:
        print(f"  ✅ 인스타 교재 댓글 작성 완료: {r.json().get('id')}")
        return True
    msg = r.text[:300]
    if "permission" in msg.lower() or "scope" in msg.lower() or r.status_code == 403:
        print("  ⚠️ 인스타 교재 댓글 실패: 토큰에 댓글 권한(instagram_business_manage_comments)이 "
              "없습니다. 토큰 재발급 시 이 권한을 포함해 주세요.")
    else:
        print(f"  ⚠️ 인스타 교재 댓글 실패: {r.status_code} {msg}")
    return False


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
def post_daily(db, df) -> dict:
    """크롤링 완료 후 호출. 실행 시각에 따라 슬롯(아침/점심/저녁)을 정해
    슬롯별로 하루 1회만, 각기 다른 내용으로 게시한다.
    반환값: 플랫폼별 결과 요약(dict) — main 이 _config/status 에 기록."""
    report = {}
    snap = _cfg_ref(db).get()
    cfg = snap.to_dict() if snap.exists else None
    if not cfg or cfg.get("enabled") is False:
        print("  (SNS 자동 게시 설정 없음/비활성 → 건너뜀)")
        return {"skip": "설정 없음/비활성"}
    if df is None or len(df) == 0:
        print("  (게시할 공고 없음 → 건너뜀)")
        return {"skip": "실패: 게시할 공고 데이터 없음"}

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
    caption_threads = _build_caption_threads(slot, df, rows)
    caption_ig = _build_caption_ig(slot, df, rows)

    # 게시 기관 CI가 들어간 카드 이미지 1회 생성 → 쓰레드/인스타 공용
    card_url = None
    if not threads_done or not ig_done:
        try:
            card_url = _upload_card(_make_card(slot, rows), slot)
            if card_url:
                print("  (기관 CI 카드 이미지 생성 완료)")
        except Exception as e:  # noqa: BLE001
            print(f"  (카드 생성 실패 → 쓰레드는 텍스트만, 인스타는 기본 이미지 사용: {e})")

    report["slot"] = slot
    if threads_done:
        print("  (쓰레드: 이 슬롯은 오늘 이미 게시함 → 건너뜀)")
        report["threads"] = "오늘 이미 게시(스킵)"
    else:
        try:
            _tid = _post_threads(cfg, caption_threads, card_url)
            if _tid:
                _cfg_ref(db).set({f"last_threads_{slot}": today}, merge=True)
                report["threads"] = "게시 완료"
                # 게시 직후 첫 댓글로 쿠팡 교재 추천 자동 작성 (실패해도 게시에는 영향 없음)
                if cfg.get("coupang_reply") is not True:
                    # 일시 중지 상태(기본) — 재개: _config/social 에 coupang_reply: true
                    print("  (쓰레드 교재 댓글: 일시 중지 상태 → 건너뜀)")
                    report["threads_comment"] = "일시 중지(설정)"
                else:
                    try:
                        _ok = _post_coupang_reply(cfg, _tid, slot)
                        report["threads_comment"] = "완료" if _ok else "실패/비활성 — 함수 로그 확인"
                    except Exception as e:  # noqa: BLE001
                        print(f"  ⚠️ 교재 댓글 오류(무시): {e}")
                        report["threads_comment"] = f"실패: {str(e)[:150]}"
            else:
                report["threads"] = "실패: 설정 없음(threads_user_id/threads_access_token)"
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ 쓰레드 게시 실패: {e}")
            report["threads"] = f"실패: {str(e)[:200]}"

    if ig_done:
        print("  (인스타그램: 이 슬롯은 오늘 이미 게시함 → 건너뜀)")
        report["instagram"] = "오늘 이미 게시(스킵)"
    else:
        image_url = card_url or cfg.get("ig_image_url")
        try:
            _mid = _post_instagram(cfg, caption_ig, image_url)
            if _mid:
                _cfg_ref(db).set({f"last_ig_{slot}": today}, merge=True)
                report["instagram"] = "게시 완료"
                # 게시 직후 교재 추천 댓글 자동 작성 (실패해도 게시에는 영향 없음)
                try:
                    _ok = _post_coupang_comment_ig(cfg, _mid)
                    report["ig_comment"] = "완료" if _ok else "실패/비활성 — 함수 로그 확인"
                except Exception as e:  # noqa: BLE001
                    print(f"  ⚠️ 인스타 교재 댓글 오류(무시): {e}")
                    report["ig_comment"] = f"실패: {str(e)[:150]}"
            else:
                report["instagram"] = "실패: 설정 없음(ig_access_token/이미지)"
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ 인스타그램 게시 실패: {e}")
            report["instagram"] = f"실패: {str(e)[:200]}"

    _maybe_refresh_threads_token(db, cfg)
    _maybe_refresh_ig_token(db, cfg)
    return report
