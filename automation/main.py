"""
ALLGONG 채용공고 자동 수집·게시 (Cloud Function, 2nd gen / HTTP trigger)
────────────────────────────────────────────────────────────────────
교수님의 두 주피터 노트북을 하나로 합쳐 서버리스로 동작하도록 포팅한 버전.

  ① 알리오 공공기관 API에서 채용공고 + 기관 마스터 수집
  ② 복합필터(보건·의료 제외 + 신입·학력 + 마감 ≤ N일) 적용 + 기관유형 병합
  ③ (엑셀 단계 생략) Firestore 'jobs' 컬렉션에 '고정 ID'로 upsert
     - 같은 공고는 새로 만들지 않고 갱신 (중복 누적 방지)
     - 이번 수집에 없는 자동등록 공고(source='alio-auto')는 삭제 (마감 공고 정리)
     - 관리자가 수동으로 올린 공고(source 없음)는 건드리지 않음
  ④ Storage 'logos/' 의 기관 로고를 매칭해 imageUrl 로 사용

환경변수
  ALIO_SERVICE_KEY  (필수)  : data.go.kr 서비스키  ← Secret Manager 로 주입
  STORAGE_BUCKET    (선택)  : 기본 'recruit-board.firebasestorage.app'
  COLLECTION_NAME   (선택)  : 기본 'jobs'
  MAX_DAYS          (선택)  : 복합필터 마감일 기준, 기본 20
  FILTER_MODE       (선택)  : 'composite'(기본,복합필터) | 'no_medical'(보건의료만 제외) | 'all'(전체)

인증
  Cloud Functions 런타임 서비스계정의 기본 자격증명(ADC)을 사용하므로
  serviceAccountKey.json 파일이 필요 없습니다. (같은 프로젝트 recruit-board)
"""

import hashlib
import math
import os
import re
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

import firebase_admin
from firebase_admin import firestore, storage

# ─────────────────────────── 설정 ───────────────────────────
SERVICE_KEY = os.environ.get("ALIO_SERVICE_KEY", "")
STORAGE_BUCKET = os.environ.get("STORAGE_BUCKET", "recruit-board.firebasestorage.app")
COLLECTION_NAME = os.environ.get("COLLECTION_NAME", "jobs")
MAX_DAYS = int(os.environ.get("MAX_DAYS", "20"))
FILTER_MODE = os.environ.get("FILTER_MODE", "composite")
# 마감된 공고 보존 일수: 사이트 '마감된 채용' 게시판 열람용으로 이 기간 동안 삭제하지 않고 보존
EXPIRED_RETENTION_DAYS = int(os.environ.get("EXPIRED_RETENTION_DAYS", "30"))

# 제목에 이 키워드들이 포함된 공고는 업로드에서 제외 (콤마로 구분, 환경변수로 변경 가능)
_DEFAULT_EXCLUDE = (
    "청원경찰,의사,간호,환경미화,운전,경비,"
    "단기노무원,프로젝트계약근로자,작업원,보강공사,단순정비,일용근로자,"
    "배전분야,일용원,배전자동화,촉탁"
)
EXCLUDE_KEYWORDS = [k.strip() for k in os.environ.get("EXCLUDE_KEYWORDS", _DEFAULT_EXCLUDE).split(",") if k.strip()]

# 첫 화면 '추천 채용정보' 배너 대상 고용유형
RECOMMENDED_EMP = {"정규직", "청년인턴(채용형)", "청년인턴(체험형)"}

PUBLIC_URL = "https://apis.data.go.kr/1051000/public_inst/list"
RECRUIT_URL = "http://apis.data.go.kr/1051000/recruitment/list"

# 기관유형(한글) → 사이트 카테고리 코드 (업로더 노트북과 동일)
CATEGORY_MAPPING = {
    "공기업(준시장형)": "market-public",
    "공기업(시장형)": "market-public",
    "준정부기관(기금관리형)": "fund-quasi",
    "준정부기관(위탁집행형)": "entrust-quasi",
    "기타공공기관": "other-public",
    "지방공기업": "local-public",
    "지방출자·출연기관": "local-investment",
}

# 한글 컬럼 매핑 (크롤러 노트북과 동일)
COL_MAP = {
    "instNm": "기관명",
    "ncsCdNmLst": "NCS코드명",
    "hireTypeNmLst": "고용유형",
    "workRgnNmLst": "근무지역",
    "recrutSeNm": "채용구분",
    "prefCondCn": "우대조건",
    "recrutNope": "채용인원",
    "pbancBgngYmd": "공고시작일",
    "pbancEndYmd": "공고종료일",
    "recrutPbancTtl": "채용공고제목",
    "srcUrl": "원본공고URL",
    "replmprYn": "대체인력여부",
    "aplyQlfcCn": "지원자격요건",
    "disqlfcRsn": "결격사유",
    "scrnprcdrMthdExpln": "전형절차",
    "prefCn": "우대사항",
    "acbgCondNmLst": "학력조건",
    "nonatchRsn": "미채용사유",
    "ongoingYn": "진행여부",
    "decimalDay": "마감일까지남은일",
    "pbadmsStdInstCd": "표준기관코드",
}


# ─────────────────────── 공통 수집 함수 ───────────────────────
def _request_json(url: str, params: dict, retries: int = 4, timeout: int = 30) -> dict:
    """requests.get + JSON 파싱에 지수 백오프 재시도(1·2·4·8초).
    일시적 네트워크 오류/5xx/파싱 실패에 견디도록 하여 크롤링 전체가 한 번에 실패하지 않게 한다."""
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries - 1:
                wait = 2 ** attempt  # 1, 2, 4, 8초
                print(f"  ⚠️ 요청 실패({attempt + 1}/{retries}): {e} → {wait}초 후 재시도")
                time.sleep(wait)
    raise RuntimeError(f"요청 {retries}회 모두 실패: {url} :: {last}")


def _collect_all_pages(url: str, params: dict, page_size: int = 500) -> list:
    """주어진 URL의 모든 페이지를 수집하여 items 리스트로 반환."""
    params = {**params, "pageNo": 1, "numOfRows": page_size}
    j = _request_json(url, params)
    if j.get("resultCode") != 200:
        raise RuntimeError(f"API 오류: {j}")

    total = int(j.get("totalCount", 0))
    items = j.get("result", [])
    pages = max(1, math.ceil(total / page_size))
    print(f"  → 총 {total}건, {pages} 페이지 수집 중...")

    for p in range(2, pages + 1):
        params["pageNo"] = p
        jj = _request_json(url, params)
        if jj.get("resultCode") != 200:
            break
        items.extend(jj.get("result", []))
    return items


def fetch_institutions() -> pd.DataFrame:
    """공공기관 마스터 → 기관유형 등 핵심 컬럼만 반환."""
    print("[1/2] 공공기관 마스터 수집")
    items = _collect_all_pages(PUBLIC_URL, {"serviceKey": SERVICE_KEY, "resultType": "json"})
    df = pd.DataFrame(items)
    keep = ["pbadmsStdInstCd", "instTypeNm", "instClsfNm", "sprvsnInstNm", "ctpvNm", "sggNm"]
    df_core = df[[c for c in keep if c in df.columns]].copy()
    if "pbadmsStdInstCd" in df_core.columns:
        df_core.drop_duplicates(subset=["pbadmsStdInstCd"], inplace=True)
    print(f"  → 마스터 {len(df_core)}개 기관 로드")
    return df_core


def fetch_recruitments() -> pd.DataFrame:
    """현재 진행중(ongoingYn=Y) 채용공고 → 한글 컬럼 변환."""
    print("[2/2] 현재진행중 채용공고 수집")
    items = _collect_all_pages(
        RECRUIT_URL, {"serviceKey": SERVICE_KEY, "resultType": "json", "ongoingYn": "Y"}
    )
    df = pd.json_normalize(items)
    df_core = df.loc[:, [c for c in COL_MAP if c in df.columns]].copy()
    df_core.rename(columns=COL_MAP, inplace=True)
    for c in ("공고시작일", "공고종료일"):
        if c in df_core.columns:
            df_core[c] = pd.to_datetime(df_core[c], format="%Y%m%d", errors="coerce")
    print(f"  → 채용공고 {len(df_core)}건 로드")
    return df_core


def merge_inst_type(df: pd.DataFrame, df_inst: pd.DataFrame) -> pd.DataFrame:
    """표준기관코드 기준으로 기관유형 등 마스터 정보를 병합."""
    if "표준기관코드" not in df.columns or "pbadmsStdInstCd" not in df_inst.columns:
        print("  ⚠️ 표준기관코드 컬럼이 없어 기관유형 병합 생략")
        return df
    merged = df.merge(
        df_inst, left_on="표준기관코드", right_on="pbadmsStdInstCd", how="left"
    ).drop(columns=["pbadmsStdInstCd"], errors="ignore")
    merged.rename(
        columns={
            "instTypeNm": "기관유형",
            "instClsfNm": "기관구분",
            "sprvsnInstNm": "소관부처",
            "ctpvNm": "시도",
            "sggNm": "시군구",
        },
        inplace=True,
    )
    return merged


# ──────────────────── 필터 함수들 ─────────────────────────
def filter_no_medical(df: pd.DataFrame) -> pd.DataFrame:
    if "NCS코드명" not in df.columns:
        return df
    return df[~df["NCS코드명"].str.contains("보건.의료", na=False, regex=False)].copy()


def filter_entry_education(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "채용구분" in out.columns:
        out = out[out["채용구분"].isin(["신입", "신입+경력"])]
    if "학력조건" in out.columns:
        out = out[
            out["학력조건"].str.contains(r"학력무관|대졸\(2~3년\)|대졸\(4년\)", na=False)
        ]
    return out


def filter_deadline(df: pd.DataFrame, max_days: int) -> pd.DataFrame:
    if "마감일까지남은일" not in df.columns:
        return df
    dd = pd.to_numeric(df["마감일까지남은일"], errors="coerce")
    return df[dd.notna() & (dd <= max_days)].copy()


def filter_exclude_keywords(df: pd.DataFrame) -> pd.DataFrame:
    """채용공고제목에 제외 키워드가 포함된 공고를 제거."""
    if "채용공고제목" not in df.columns or not EXCLUDE_KEYWORDS:
        return df
    before = len(df)
    pattern = "|".join(re.escape(k) for k in EXCLUDE_KEYWORDS)
    out = df[~df["채용공고제목"].astype(str).str.contains(pattern, na=False, regex=True)].copy()
    print(f"  → 제외 키워드 필터: {before - len(out)}건 제거 → {len(out)}건")
    return out


def filter_not_expired(df: pd.DataFrame) -> pd.DataFrame:
    """마감일(공고종료일)이 오늘보다 이전인 공고 제거 (만료 공고는 게시 안 함)."""
    if "공고종료일" not in df.columns:
        return df
    before = len(df)
    today = pd.Timestamp(datetime.now().date())
    end = pd.to_datetime(df["공고종료일"], errors="coerce")
    out = df[end.isna() | (end >= today)].copy()
    print(f"  → 마감 지난 공고 제외: {before - len(out)}건 제거 → {len(out)}건")
    return out


def build_dataset() -> pd.DataFrame:
    """FILTER_MODE 에 맞춰 최종 업로드용 DataFrame 생성."""
    df_inst = fetch_institutions()
    df_raw = fetch_recruitments()

    if FILTER_MODE == "all":
        df = df_raw
    elif FILTER_MODE == "no_medical":
        df = filter_no_medical(df_raw)
    else:  # composite (기본)
        df = filter_no_medical(df_raw)
        df = filter_entry_education(df)
        df = filter_deadline(df, MAX_DAYS)

    # 제목 키워드 제외 + 마감 지난 공고 제외 (항상 적용)
    df = filter_exclude_keywords(df)
    df = filter_not_expired(df)

    df = merge_inst_type(df, df_inst)
    if "기관명" in df.columns:
        df = df.sort_values(by="기관명", ascending=True)
    print(f"  → 최종 업로드 대상: {len(df)}건 (FILTER_MODE={FILTER_MODE})")
    return df


# ──────────────────── 가공 함수 (업로더 노트북과 동일) ───────────
def clean_preference(text):
    if not text:
        return ""
    s = str(text)
    for phrase in [
        "※ 자세한 사항은 첨부파일의 공고문 참조",
        "※ 상세 내용은 첨부파일의 채용공고문 참조",
        "※ 보다 자세한 사항은 첨부파일의 채용공고문을 참조",
        "※ 상세 내용은\n첨부파일의 채용공고문 참조",
        "※ 보다 자세한 사항은\n첨부파일의 채용공고문을 참조",
    ]:
        s = s.replace(phrase, "")
    return s.strip()


def format_deadline(date_value):
    if date_value is None or date_value == "":
        return ""
    if isinstance(date_value, pd.Timestamp) or isinstance(date_value, datetime):
        if pd.isna(date_value):
            return ""
        return date_value.strftime("%Y-%m-%d")
    s = str(date_value).strip()
    if " " in s:
        s = s.split(" ")[0]
    return re.sub(r"[./]", "-", s)


def clean_url(url):
    if not url:
        return "#"
    s = str(url).strip().replace("'", "").replace('"', "").strip()
    if not s or not s.startswith(("http://", "https://")):
        return "#"
    return s


def clean_content(text):
    if not text or text == "":
        return ""
    return str(text).replace("_x000D_", "").replace("\r", "").strip()


# 말미 법인격 표기 ( (주) / (재) / (사) / (유) / (합) ) 제거용
_CORP_SUFFIX_RE = re.compile(r"\s*\((?:주|재|사|유|합)\)\s*$")

# 기관명이 감싸질 수 있는 괄호쌍 (여는 괄호, 닫는 괄호)
_BRACKET_PAIRS = [
    (r"\[", r"\]"), (r"\(", r"\)"), (r"\{", r"\}"),
    ("（", "）"), ("【", "】"), ("〔", "〕"),
    ("<", ">"), ("〈", "〉"), ("『", "』"), ("「", "」"),
]


def _company_variants(company):
    """기관명 제거에 사용할 후보 문자열들 (길이 2 이상만)."""
    c = str(company or "").strip()
    variants = set()
    if not c:
        return variants
    variants.add(c)
    base = _CORP_SUFFIX_RE.sub("", c).strip()  # 말미 (주)/(재) 등 제거 버전
    if base:
        variants.add(base)
    return {v for v in variants if len(v) >= 2}


def clean_title(title, company):
    """
    '제목'에서 '기관명'을 제거해 제목이 불필요하게 길어지지 않게 한다.
    - 기관명이 괄호( [], (), {} 등 )로 묶여 있으면 괄호까지 포함해 제거.
    - 제거 후 앞뒤 공백은 한 칸으로 정리하여 자연스럽게 이어지게 한다.
      예) '[한국지식재산보호원] 2026년도 제6차 인력채용' → '2026년도 제6차 인력채용'
          '2026년도 한국임업진흥원 인재채용(기간제 3차) 공고' → '2026년도 인재채용(기간제 3차) 공고'
    """
    raw = "" if title is None else str(title)
    if not raw.strip():
        return "제목없음"
    t = raw
    # 긴 후보부터 제거 ( '한국동서발전(주)' 를 '한국동서발전' 보다 먼저 )
    for v in sorted(_company_variants(company), key=len, reverse=True):
        vp = re.escape(v)
        # 1) 괄호로 감싼 기관명 → 괄호 포함 제거
        for lb, rb in _BRACKET_PAIRS:
            t = re.sub(lb + r"\s*" + vp + r"\s*" + rb, " ", t)
        # 2) 괄호 없이 노출된 기관명 제거
        t = re.sub(vp, " ", t)
    # 공백 정리: 연속 공백 → 한 칸, 양끝 트림
    t = re.sub(r"\s+", " ", t).strip()
    # 맨 앞에 홀로 남은 구분기호 제거 (예: '- 2026 …', ': 2026 …')
    t = re.sub(r"^[\-–—:·,]\s*", "", t).strip()
    return t if t else raw.strip()


def get_logo_url(company_name, logo_files):
    if company_name in logo_files:
        return logo_files[company_name]
    clean_name = str(company_name).replace(" ", "")
    for key, url in logo_files.items():
        if key.replace(" ", "") == clean_name:
            return url
    for key, url in logo_files.items():
        if company_name and (company_name in key or key in company_name):
            return url
    return "./logo.png"


def stable_doc_id(row) -> str:
    """공고를 고유하게 식별하는 결정적 문서 ID (중복 방지/갱신용)."""
    src = clean_url(row.get("원본공고URL", ""))
    if src != "#":
        basis = src
    else:
        basis = f"{row.get('기관명','')}|{row.get('채용공고제목','')}|{format_deadline(row.get('공고종료일',''))}"
    return "alio_" + hashlib.md5(basis.encode("utf-8")).hexdigest()


# ──────────────────── Firestore 동기화 ───────────────────────
def load_logo_files(bucket):
    print("📂 Storage logos/ 에서 로고 목록 로드 중...")
    logo_files = {}
    try:
        for blob in bucket.list_blobs(prefix="logos/"):
            filename = blob.name.replace("logos/", "")
            if not filename:
                continue
            company = filename.rsplit(".", 1)[0]
            try:
                blob.make_public()
            except Exception:
                pass  # 균일 버킷 접근(uniform access)이면 ACL 불가 → public_url 그대로 사용
            logo_files[company] = blob.public_url
        print(f"  ✅ 로고 {len(logo_files)}개")
    except Exception as e:
        print(f"  ⚠️ 로고 목록 로드 실패: {e}")
    return logo_files


def sync_to_firestore(df: pd.DataFrame) -> dict:
    db = firestore.client()
    bucket = storage.bucket()
    logo_files = load_logo_files(bucket)

    base_time = datetime.now()
    current_ids = set()
    ops = 0
    batch = db.batch()
    col = db.collection(COLLECTION_NAME)

    def commit():
        nonlocal batch, ops
        if ops:
            batch.commit()
        batch = db.batch()
        ops = 0

    # 기존 자동등록 공고 스냅샷 1회 조회 (관리자수정 잠금 / 등록일 보존 / 만료 정리에 공용)
    existing_docs = {}
    try:
        for d in col.where("source", "==", "alio-auto").stream():
            existing_docs[d.id] = d.to_dict() or {}
    except Exception as e:  # noqa: BLE001
        print(f"  (기존 공고 조회 생략: {e})")
    locked_ids = {k for k, v in existing_docs.items() if v.get("adminEdited")}
    if locked_ids:
        print(f"  🔒 관리자 수정 공고 {len(locked_ids)}건 → 덮어쓰기/삭제 제외")

    i = 0
    for _, row in df.iterrows():
        doc_id = stable_doc_id(row)
        if doc_id in current_ids:
            continue  # 같은 공고 중복 행 스킵
        current_ids.add(doc_id)
        if doc_id in locked_ids:
            continue  # 관리자 수정본은 그대로 보존(덮어쓰기 안 함). current_ids에 넣었으니 삭제도 안 됨

        company = row.get("기관명", "")
        category = CATEGORY_MAPPING.get(str(row.get("기관유형", "")).strip(), "public")
        deadline = format_deadline(row.get("공고종료일", ""))
        # 등록일(createdAt) = 실제 공고 게시일(공고시작일).
        # 문서ID 해시로 같은 날짜 안의 순서를 고정 → 매일 재수집해도 값이 흔들리지 않음(결정적).
        # 공고시작일이 없으면: 기존 문서는 원래 등록일 보존, 신규 문서만 수집 시각 사용.
        _start = row.get("공고시작일")
        _has_start = _start is not None and not pd.isna(_start)
        if _has_start:
            _base = _start.to_pydatetime() if hasattr(_start, "to_pydatetime") else _start
            _off = int(hashlib.md5(doc_id.encode("utf-8")).hexdigest()[:6], 16) % 86400
            timestamp = _base + timedelta(seconds=_off)
        else:
            timestamp = base_time + timedelta(seconds=i)
        i += 1

        # 첫 화면 '추천 채용정보' 배너 대상 표시 (정규직/청년인턴(채용·체험형))
        _emp_parts = {p.strip() for p in re.split(r"[,+/]", str(row.get("고용유형", "")))}
        is_recommended = bool(_emp_parts & RECOMMENDED_EMP)
        # 첫 화면 '프리미엄 채용정보' 배너 / '프리미엄' 게시판 대상 = 고용유형이 '정규직' 단독
        _emp_list = [p.strip() for p in re.split(r"[,+/]", str(row.get("고용유형", ""))) if p.strip()]
        is_premium = (len(_emp_list) == 1 and _emp_list[0] == "정규직")
        # 첫 화면 '주요 채용정보' 배너 / '주요' 게시판 대상 = 고용유형이 '비정규직' 또는 '무기계약직' 단독
        is_featured = (len(_emp_list) == 1 and _emp_list[0] in ("비정규직", "무기계약직"))

        doc_data = {
            "title": clean_title(row.get("채용공고제목", "제목없음"), company),
            "company": company,
            "category": category,
            "instType": str(row.get("기관유형", "")).strip(),  # 기관유형 원문 (예: 공기업(시장형)) — 상세 화면 표시용
            "employmentType": row.get("고용유형", ""),
            "jobType": row.get("고용유형", ""),
            "location": row.get("근무지역", ""),
            "deadline": deadline,
            "closingDate": deadline,
            "ncsCode": row.get("NCS코드명", ""),
            "careerType": row.get("채용구분", ""),
            "recruitmentCount": str(row.get("채용인원", "0")),
            "education": row.get("학력조건", ""),
            "preference": clean_preference(row.get("우대조건", "")),
            "detailUrl": clean_url(row.get("원본공고URL", "")),
            "imageUrl": get_logo_url(company, logo_files),
            "content": clean_content(row.get("전형절차", "")),
            "createdAt": timestamp,
            "created_at": timestamp,
            "source": "alio-auto",  # 자동 등록 표시 (정리 대상 식별용)
            "recommended": is_recommended,  # 추천 배너 대상 여부 (홈 빠른 조회용)
            "premium": is_premium,  # 프리미엄 배너/게시판 대상 여부 (고용유형 '정규직' 단독)
            "featured": is_featured,  # 주요 배너/게시판 대상 여부 (고용유형 '비정규직'/'무기계약직' 단독)
        }
        if not _has_start and doc_id in existing_docs:
            # 공고시작일이 없는 기존 문서는 원래 등록일을 덮어쓰지 않음
            doc_data.pop("createdAt", None)
            doc_data.pop("created_at", None)
        batch.set(col.document(doc_id), doc_data, merge=True)
        ops += 1
        if ops >= 450:
            commit()
    commit()
    print(f"  ✅ upsert 완료: {len(current_ids)}건")

    # ── 마감/사라진 자동등록 공고 정리 (수동 등록·관리자 수정 공고는 보존) ──
    #   마감된 공고는 EXPIRED_RETENTION_DAYS(기본 30일) 동안 보존 → 사이트 '마감된 채용' 게시판에서 열람.
    #   보존기간이 지났거나 마감일 정보가 없는 공고만 실제 삭제.
    deleted = 0
    kept_expired = 0
    cutoff = (datetime.now() - timedelta(days=EXPIRED_RETENTION_DAYS)).strftime("%Y-%m-%d")
    for did, data in existing_docs.items():
        if did in current_ids or did in locked_ids:
            continue  # 이번 수집분 또는 관리자 수정본은 보존
        dl = str(data.get("deadline") or data.get("closingDate") or "").strip()
        if dl and dl >= cutoff:
            # 보존: 최초 1회 배너 플래그를 꺼서(archived) 홈 배너 조회 대상에서 제외
            if not data.get("archived"):
                batch.update(col.document(did), {
                    "archived": True,
                    "recommended": False,
                    "premium": False,
                    "featured": False,
                })
                ops += 1
                if ops >= 450:
                    commit()
            kept_expired += 1
            continue
        batch.delete(col.document(did))
        ops += 1
        deleted += 1
        if ops >= 450:
            commit()
    commit()
    print(f"  🗑️ 정리(삭제)된 만료 공고: {deleted}건 / 🗂️ 마감 보존({EXPIRED_RETENTION_DAYS}일): {kept_expired}건")

    return {"upserted": len(current_ids), "deleted": deleted, "kept_expired": kept_expired}


def _notify_failure(message: str) -> None:
    """실패 알림(선택): 환경변수 ALERT_WEBHOOK_URL 이 설정된 경우에만 웹훅으로 전송.
    Slack/Discord/Google Chat 등 어떤 수신 웹훅이든 가능. 미설정 시 로그만 남기고 조용히 통과."""
    print(f"❌ {message}")
    url = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
    if not url:
        return
    try:
        requests.post(url, json={"text": f"[allgongin 크롤러] {message}"}, timeout=10)
    except Exception as e:  # noqa: BLE001
        print(f"  (알림 전송 실패: {e})")


def run() -> dict:
    try:
        if not SERVICE_KEY:
            raise RuntimeError("ALIO_SERVICE_KEY 환경변수가 설정되지 않았습니다.")
        if not firebase_admin._apps:
            firebase_admin.initialize_app(options={"storageBucket": STORAGE_BUCKET})
        df = build_dataset()
        result = sync_to_firestore(df)
        print(f"🎉 완료: {result}")
        return result
    except Exception as e:  # noqa: BLE001
        # 실패 시: 알림 전송 후 재-raise → Cloud Functions 실행이 '실패'로 기록되어 모니터링/스케줄러가 감지
        _notify_failure(f"채용공고 크롤링 실패: {e}")
        raise


# ──────────────────── 진입점 ───────────────────────
# Cloud Functions(2nd gen) HTTP 트리거
try:
    import functions_framework

    @functions_framework.http
    def job_sync(request):  # noqa: ARG001
        result = run()
        return (f"OK {result}", 200)
except ImportError:
    pass

# 로컬 실행용
if __name__ == "__main__":
    run()
