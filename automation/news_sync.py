"""
공공기관 뉴스 자동 수집·재작성·게시 모듈
────────────────────────────────────────────────────────────
매일 '공공기관 채용' 관련 뉴스를 검색해, 원문을 그대로 옮기지 않고
Gemini(Vertex AI)로 완전히 새 문장으로 재작성(패러프레이징)한 글을
올공 '공공기관 뉴스' 게시판(Firestore `news` 컬렉션)에 하루 3건씩 게시합니다.

동작 방식 (main.py 가 매일 07/12/18시에 호출)
  - 아침 슬롯: 1건 / 점심 슬롯: 누적 2건까지 / 저녁 슬롯: 누적 3건까지
    → 하루 최대 3건, 앞 슬롯이 실패해도 다음 슬롯이 부족분을 채움
  - 검색 키워드는 슬롯마다 순환: 채용 뉴스 → 채용 뉴스 → 전문가 의견/전망
  - 이미 게시한 기사(URL·제목 기준)는 다시 게시하지 않음
  - 게시글 하단에 항상 원문 출처(매체명·링크)를 표기

필요 설정 (Firestore `_config` 컬렉션 → `news` 문서)
  naver_client_id      (필수) 네이버 개발자센터 애플리케이션의 Client ID
  naver_client_secret  (필수) 위 애플리케이션의 Client Secret
  enabled              (선택) "false" 로 두면 전체 기능 정지 (기본 활성)
  daily_limit          (선택) 하루 게시 건수 (기본 3)
  queries              (선택) 검색 키워드 배열 (기본값은 아래 DEFAULT_QUERIES)
  model                (선택) Vertex AI Gemini 모델명 (기본 gemini-2.5-flash-lite)
  use_og_image         (선택) true 면 원문 대표 이미지를 썸네일로 사용
                        (언론사 사진은 별도 저작권이 있어 기본은 사용 안 함)

이 모듈이 상태 저장용으로 같은 문서에 쓰는 필드: state, posted_hashes
설정이 없으면 아무 것도 하지 않고 조용히 넘어갑니다(다른 기능에 영향 없음).
"""

import html
import io
import json
import os
import re
import hashlib
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

NAVER_NEWS_URL = "https://openapi.naver.com/v1/search/news.json"

# 슬롯별 순환 검색 키워드: 아침·점심 = 채용 뉴스, 저녁 = 전문가 의견/전망
DEFAULT_QUERIES = [
    "공공기관 채용",
    "공기업 채용",
    "공공기관 채용 전망",
]

# 제목에 이 단어가 들어간 기사는 건너뜀 (게시판 성격과 안 맞는 기사)
SKIP_TITLE_WORDS = ["부고", "인사]", "[인사", "포토]", "[포토", "화보"]

# Vertex AI Gemini — 앞 모델이 사용 불가(404 등)면 순서대로 시도
GEMINI_MODELS = ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.0-flash-001"]


# ─────────────────────── 공통 유틸 ───────────────────────
def _h(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:16]


def _strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _norm_title(t: str) -> str:
    """다른 매체의 같은 기사(제목만 조금 다름)를 걸러내기 위한 정규화."""
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", t)  # [단독], (종합) 등 제거
    return re.sub(r"[^0-9가-힣a-zA-Z]", "", t).lower()


def _trim_to_complete_sentence(text: str) -> str:
    """미완성으로 끝난 본문을 마지막 '완결된 문장'까지만 남긴다.
    (모델 출력이 잘렸을 때 '…다뤄졌으며' 같은 끊긴 문장이 게시되는 것을 방지)"""
    t = (text or "").rstrip()
    if not t:
        return t
    # 문장 종결로 인정: . ! ? … 및 닫는 따옴표/괄호가 바로 뒤따르는 경우
    if re.search(r"[.!?…][\"”')\]]*$", t):
        return t
    ends = [m.end() for m in re.finditer(r"[.!?…][\"”')\]]*", t)]
    if not ends:
        return t  # 종결부호가 하나도 없으면 원문 유지 (아래 길이 검사에서 걸러짐)
    return t[:ends[-1]].rstrip()


def _cfg(db) -> dict:
    try:
        doc = db.collection("_config").document("news").get()
        return doc.to_dict() or {}
    except Exception as e:  # noqa: BLE001
        print(f"  (뉴스 설정 조회 실패: {e})")
        return {}


# ─────────────────────── 1) 뉴스 검색 ───────────────────────
def _search_news(cid: str, csec: str, query: str) -> list:
    r = requests.get(
        NAVER_NEWS_URL,
        params={"query": query, "display": 30, "sort": "date"},
        headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csec, "User-Agent": UA},
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("items", [])


def _pick_item(cid: str, csec: str, query: str, seen: set):
    """검색 결과에서 아직 게시하지 않은 최신 기사 1건 선택."""
    now = datetime.now(KST)
    for it in _search_news(cid, csec, query):
        title = _strip_tags(it.get("title", ""))
        desc = _strip_tags(it.get("description", ""))
        url = (it.get("originallink") or it.get("link") or "").strip()
        if not title or not url.startswith("http"):
            continue
        if any(w in title for w in SKIP_TITLE_WORDS):
            continue
        try:  # 최근 7일 이내 기사만
            pub = parsedate_to_datetime(it.get("pubDate", ""))
            if (now - pub).days > 7:
                continue
        except Exception:  # noqa: BLE001
            pass
        h_url, h_title = _h(url), _h(_norm_title(title))
        if h_url in seen or h_title in seen:
            continue
        return {
            "title": title, "desc": desc, "url": url,
            "naver_link": (it.get("link") or "").strip(),
            "hashes": [h_url, h_title],
        }
    return None


# ─────────────────── 2) 원문 본문 추출 ───────────────────
_ARTICLE_SELECTOR = (
    "#dic_area, #articeBody, #newsct_article, #article-view-content-div, "
    "#articleBodyContents, .article_body, .news_body, .article-body, article"
)


def _fetch_article(item: dict) -> dict:
    """기사 페이지에서 본문 텍스트·매체명·대표이미지를 추출 (실패해도 빈 값 반환)."""
    out = {"text": "", "press": "", "image": ""}
    # 네이버 뉴스 링크가 있으면 우선 사용 (본문 구조가 일정해 추출 성공률이 높음)
    urls = [u for u in (item.get("naver_link"), item.get("url")) if u and "news.naver.com" in u]
    urls += [u for u in (item.get("url"), item.get("naver_link")) if u and u not in urls]
    for url in urls:
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": UA})
            r.raise_for_status()
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, "html.parser")
            if not out["press"]:
                m = soup.find("meta", property="og:site_name")
                out["press"] = (m.get("content") or "").strip() if m else ""
            if not out["image"]:
                m = soup.find("meta", property="og:image")
                out["image"] = (m.get("content") or "").strip() if m else ""
            node = soup.select_one(_ARTICLE_SELECTOR)
            text = node.get_text("\n", strip=True) if node else ""
            if len(text) < 300:  # 본문 컨테이너를 못 찾으면 <p> 태그들로 대체
                ps = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
                alt = "\n".join(t for t in ps if len(t) > 30)
                if len(alt) > len(text):
                    text = alt
            if len(text) >= 200:
                out["text"] = text[:6000]
                return out
        except Exception as e:  # noqa: BLE001
            print(f"  (본문 추출 실패 {url[:60]}: {e})")
    if not out["press"]:
        m = re.search(r"https?://(?:www\.)?([^/]+)/", item.get("url", "") + "/")
        out["press"] = m.group(1) if m else ""
    return out


# ─────────────── 3) Gemini(Vertex AI) 재작성 ───────────────
def _gemini_generate(cfg: dict, prompt: str) -> str:
    import google.auth
    from google.auth.transport.requests import Request as GARequest

    creds, adc_project = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(GARequest())
    project = (cfg.get("project") or adc_project or "recruit-board")
    models = [cfg["model"]] if cfg.get("model") else GEMINI_MODELS
    last = None
    for model in models:
        gen_cfg = {
            "temperature": 0.7,
            # 한도가 작으면 JSON 강제 출력이 문장 중간에 잘린 채 '유효한 JSON'으로
            # 마감되어 미완성 글이 그대로 게시될 수 있음 → 넉넉하게 확보
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
        }
        if model.startswith("gemini-2.5"):
            # 2.5 모델의 '생각(thinking)' 토큰이 출력 한도를 잠식해 본문이 잘리는
            # 것을 방지 (뉴스 재작성에는 사고 과정이 필요 없음)
            gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen_cfg,
        }
        url = (f"https://aiplatform.googleapis.com/v1/projects/{project}"
               f"/locations/global/publishers/google/models/{model}:generateContent")
        try:
            r = requests.post(url, json=body, timeout=90,
                              headers={"Authorization": f"Bearer {creds.token}"})
            if r.status_code == 404:  # 이 모델이 없으면 다음 후보로
                last = f"모델 없음: {model}"
                continue
            r.raise_for_status()
            cand = r.json()["candidates"][0]
            finish = cand.get("finishReason", "STOP")
            if finish == "MAX_TOKENS":
                # 잘린 응답은 미완성 글이 게시되므로 사용하지 않음
                last = f"출력이 한도에서 잘림(MAX_TOKENS): {model}"
                continue
            parts = cand["content"]["parts"]
            return "".join(p.get("text", "") for p in parts if not p.get("thought"))
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"Gemini 호출 실패: {last}")


def _paraphrase(cfg: dict, item: dict, article: dict):
    """원문을 바탕으로 완전히 새로 쓴 {title, desc, content} 반환. 실패 시 None."""
    material = article["text"] or item["desc"]
    if len(material) < 80:  # 재작성할 재료가 너무 적으면 이 기사는 포기
        return None
    prompt = f"""당신은 공공기관 취업 정보 사이트 '올공(ALLGONG)'의 뉴스 에디터입니다.
아래 [원문 기사]를 바탕으로, 공공기관 취업준비생 독자를 위한 새로운 뉴스 글을 한국어로 작성하세요.

규칙:
1. 원문 문장을 그대로 복사하지 말고, 모든 문장을 완전히 새로운 표현으로 다시 쓸 것(패러프레이징).
2. 원문에 없는 사실을 지어내지 말 것. 기관명·수치·날짜는 원문 그대로 정확하게 옮길 것.
3. 본문(content)은 3~5개 문단, 전체 500~900자. 문단 사이는 빈 줄 한 개로 구분.
4. 마지막 문단은 취업준비생 관점에서의 시사점 1~2문장으로 마무리.
5. 제목(title)은 40자 이내로 핵심을 담아 새로 작성. 대괄호 말머리 금지.
6. desc 는 목록 화면에 보일 한 줄 요약(80자 이내).
7. 기자 이름, 이메일, '무단전재', 광고 문구 등 기사 외 요소는 모두 제외.

반드시 아래 형식의 JSON 으로만 답하세요:
{{"title": "...", "desc": "...", "content": "..."}}

[원문 기사]
제목: {item['title']}
매체: {article['press'] or '알 수 없음'}
내용:
{material}"""
    try:
        raw = _gemini_generate(cfg, prompt)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️ 재작성(Gemini) 실패: {e}")
        print("     → Vertex AI API 활성화 및 서비스계정 권한(roles/aiplatform.user)을 확인하세요.")
        return None
    try:
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        data = json.loads(raw)
        title = str(data.get("title", "")).strip()
        content = _trim_to_complete_sentence(str(data.get("content", "")).strip())
        desc = str(data.get("desc", "")).strip() or content[:80]
        if len(title) < 5 or len(content) < 200:
            raise ValueError("생성 결과가 너무 짧음")
        return {"title": title, "desc": desc, "content": content}
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️ 재작성 결과 파싱 실패: {e}")
        return None


# ─────────────── 4) 자동 썸네일 생성 ───────────────
# 언론사 사진은 별도 저작권이 있어 쓰지 않고, 직접 만든 카드 이미지를 사용:
#   ① 기사에 등장하는 공공기관의 CI 로고(Storage `logos/` 보유분)를 찾으면 → 로고 카드
#   ② 없으면 → 제목을 넣은 브랜드 카드 (색상은 제목 해시로 선택 → 글마다 다양)
_CARD_PALETTES = [
    ("#1b2a6b", "#2f54c9"), ("#0b5e57", "#159a8e"), ("#4c2f8f", "#7048c9"),
    ("#20455e", "#3f7fae"), ("#7a3a16", "#c05621"), ("#5c164e", "#a61e8e"),
]


def _wrap_chars(draw, text, font, max_w: int, max_lines: int) -> list:
    """한글용 글자 단위 줄바꿈 (다 못 담으면 마지막 줄을 말줄임 처리)."""
    lines, cur, truncated = [], "", False
    for ch in text:
        if draw.textlength(cur + ch, font=font) <= max_w:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
            if len(lines) == max_lines:
                truncated = True
                break
    if not truncated and cur:
        lines.append(cur)
    if truncated and lines:
        lines[-1] = lines[-1][:-1] + "…"
    return [ln.strip() for ln in lines if ln.strip()]


def _find_logo(bucket, text: str):
    """기사 텍스트에 등장하는 기관의 로고(Storage logos/)를 찾아 PIL 이미지로 반환."""
    from PIL import Image
    candidates = []
    flat = text.replace(" ", "")
    for blob in bucket.list_blobs(prefix="logos/"):
        name = blob.name.replace("logos/", "")
        if not name:
            continue
        company = name.rsplit(".", 1)[0].strip()
        if len(company.replace(" ", "")) >= 3 and company.replace(" ", "") in flat:
            candidates.append((company, blob))
    if not candidates:
        return None, ""
    company, blob = max(candidates, key=lambda c: len(c[0]))  # 가장 구체적(긴) 기관명 우선
    img = Image.open(io.BytesIO(blob.download_as_bytes())).convert("RGBA")
    return img, company


def _make_thumbnail(post: dict) -> str:
    """썸네일 카드를 생성해 Storage 에 올리고 공개 URL 반환. 실패 시 예외."""
    from firebase_admin import storage
    from PIL import Image, ImageDraw, ImageFont

    xb = lambda s: ImageFont.truetype(os.path.join(FONT_DIR, "NanumGothicExtraBold.ttf"), s)  # noqa: E731
    rg = lambda s: ImageFont.truetype(os.path.join(FONT_DIR, "NanumGothic.ttf"), s)  # noqa: E731
    bucket = storage.bucket()
    W, H = 800, 450

    logo, company = (None, "")
    try:
        logo, company = _find_logo(bucket, post["title"] + " " + post["content"][:300])
    except Exception as e:  # noqa: BLE001
        print(f"  (로고 탐색 실패 — 텍스트 카드로 대체: {e})")

    def _hex(c):
        return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))

    if logo is not None:
        # ① 기관 CI 카드: 흰 바탕 + 로고 중앙 + 상단 포인트 라인 + 하단 안내 바
        img = Image.new("RGB", (W, H), "#ffffff")
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, W, 10), fill="#1c7ed6")
        logo.thumbnail((520, 230))
        img.paste(logo, (int((W - logo.width) / 2), int((H - 64 - logo.height) / 2) + 5), logo)
        d.rectangle((0, H - 64, W, H), fill="#f1f3f5")
        label = f"공공기관 뉴스 | {company}"
        d.text((32, H - 64 + 18), label, font=xb(26), fill="#495057")
        site = "allgongin.com"
        d.text((W - 32 - d.textlength(site, font=rg(24)), H - 64 + 20), site, font=rg(24), fill="#868e96")
    else:
        # ② 브랜드 텍스트 카드: 세로 그라데이션 + 뉴스 배지 + 제목
        top, bottom = _CARD_PALETTES[int(_h(post["title"]), 16) % len(_CARD_PALETTES)]
        (r1, g1, b1), (r2, g2, b2) = _hex(top), _hex(bottom)
        img = Image.new("RGB", (W, H))
        d = ImageDraw.Draw(img)
        for y in range(H):
            t = y / H
            d.line((0, y, W, y), fill=(int(r1 + (r2 - r1) * t), int(g1 + (g2 - g1) * t), int(b1 + (b2 - b1) * t)))
        badge = "공공기관 NEWS"
        bw = d.textlength(badge, font=xb(26))
        d.rounded_rectangle((48, 48, 48 + bw + 36, 48 + 46), radius=10, outline="#ffffff", width=2)
        d.text((48 + 18, 48 + 9), badge, font=xb(26), fill="#ffffff")
        y = 140
        for ln in _wrap_chars(d, post["title"], xb(52), W - 96, 3):
            d.text((48, y), ln, font=xb(52), fill="#ffffff")
            y += 74
        d.text((48, H - 64), "allgongin.com", font=rg(26), fill=(255, 255, 255))

    buf = io.BytesIO()
    img.save(buf, "PNG")
    blob = bucket.blob(f"news_images/auto/{datetime.now(KST):%Y%m%d}_{_h(post['title'])}.png")
    blob.upload_from_string(buf.getvalue(), content_type="image/png")
    try:
        blob.make_public()
    except Exception:  # noqa: BLE001
        pass  # 균일 버킷 접근이면 ACL 불가 → public_url 그대로 사용
    print(f"  🖼️ 썸네일 생성: {'기관 CI(' + company + ')' if logo is not None else '텍스트 카드'}")
    return blob.public_url


# ─────────────────── 5) 게시판 저장 ───────────────────
def _save_post(db, post: dict, item: dict, article: dict, cfg: dict):
    now = datetime.now(KST)
    press = article["press"] or "원문 기사"
    content = (post["content"].strip()
               + "\n\n※ 위 글은 아래 언론 보도를 바탕으로 재구성(요약·재작성)한 것입니다.\n"
               + f"출처: {press} — {item['url']}")
    image_url = ""
    if str(cfg.get("use_og_image", "")).lower() in ("true", "1", "yes"):
        image_url = article.get("image", "")
    if not image_url:
        try:  # 저작권 안전한 자동 썸네일 (기관 CI 카드 또는 브랜드 텍스트 카드)
            image_url = _make_thumbnail(post)
        except Exception as e:  # noqa: BLE001
            print(f"  (썸네일 생성 실패 — 이미지 없이 게시: {e})")
    db.collection("news").add({
        "title": post["title"][:120],
        "desc": post["desc"][:200],
        "content": content,
        "imageUrl": image_url,
        "date": now.strftime("%Y-%m-%d"),
        "createdAt": now,
        "updatedAt": now,
        "authorUid": "news-bot",
        "source": "news-auto",       # 자동 게시 표식
        "sourceName": press,
        "sourceUrl": item["url"],
    })
    print(f"  📰 게시: {post['title'][:50]} (출처: {press})")


# ─────────────────── 진입점 ───────────────────
def run_daily(db) -> None:
    cfg = _cfg(db)
    if str(cfg.get("enabled", "true")).lower() in ("false", "0", "no"):
        print("  (뉴스 자동 게시: 설정에서 비활성화됨)")
        return
    cid = str(cfg.get("naver_client_id", "")).strip()
    csec = str(cfg.get("naver_client_secret", "")).strip()
    if not cid or not csec:
        print("  (뉴스 설정 없음: _config/news 문서에 naver_client_id / naver_client_secret 을 넣으면 활성화됩니다)")
        return

    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    state = cfg.get("state") or {}
    posted_today = int(state.get("count", 0)) if state.get("date") == today else 0
    daily_limit = int(cfg.get("daily_limit", 3))
    # 시간대별 목표 누적치: 아침 1건 → 점심 2건 → 저녁 3건 (앞 슬롯 실패분은 다음 슬롯이 보충)
    target = 1 if now.hour < 11 else (2 if now.hour < 17 else 3)
    target = min(target, daily_limit)
    need = target - posted_today
    if need <= 0:
        print(f"  (뉴스: 오늘 {posted_today}건 게시 완료 — 이번 슬롯 추가 게시 없음)")
        return

    posted_hashes = list(cfg.get("posted_hashes") or [])  # 오래된 순 → 최신 순
    seen = set(posted_hashes)
    queries = cfg.get("queries") or DEFAULT_QUERIES
    made = 0
    for _ in range(need):
        idx = (posted_today + made) % len(queries)
        item = _pick_item(cid, csec, queries[idx], seen)
        if not item and idx != 0:  # 전문가/전망 키워드에 새 기사가 없으면 기본 키워드로 대체
            item = _pick_item(cid, csec, queries[0], seen)
        if not item:
            print(f"  (뉴스: '{queries[idx]}' 관련 새 기사가 없습니다)")
            break
        article = _fetch_article(item)
        post = _paraphrase(cfg, item, article)
        new_hashes = [h for h in item["hashes"] if h not in seen]
        posted_hashes += new_hashes
        seen.update(new_hashes)
        if not post:
            continue  # 재작성 실패한 기사는 목록에서 제외됐으므로 다음 기사로
        _save_post(db, post, item, article, cfg)
        made += 1

    db.collection("_config").document("news").set({
        "state": {"date": today, "count": posted_today + made},
        "posted_hashes": posted_hashes[-400:],  # 최근 400개만 보존 (중복 방지용)
    }, merge=True)
    print(f"  ✅ 뉴스 자동 게시: 이번 슬롯 {made}건 (오늘 누적 {posted_today + made}건)")
