# 브레인게시판(Static) — GitHub Issues 기반

이 게시판은 **GitHub Pages(정적 호스팅)** 환경에서 서버 없이 동작합니다. 글/댓글 데이터는 **GitHub Issues**에 저장합니다.

## 구성 개요
- 게시글 = GitHub Issue (라벨: `board`)
- 댓글 = 그 이슈의 코멘트 (GitHub UI에서 작성)
- 목록/검색 = GitHub Search API(JSON)를 프론트에서 호출
- 쓰기 = “새 글쓰기” 버튼 → GitHub의 New Issue 화면(라벨 자동 적용)

> 장점: 서버/DB 불필요, 무료, 스팸/차단/라벨링 등 관리가 쉬움  
> 제약: 작성하려면 GitHub 로그인 필요, 파일 첨부는 GH 이슈 첨부 UI 사용

---

## 1) 저장소 준비
1. GitHub에 사이트 저장소(예: `YOUR_GH_USER/YOUR_REPO`)가 있다고 가정합니다.
2. 저장소에 **Labels**에서 `board` 라벨을 하나 만들어 둡니다(색상 임의).

## 2) board.html 설치
1. 본 ZIP 안의 `board.html`을 저장소 루트(또는 `/board/board.html`)에 추가/커밋.
2. `board.html` 열어 아래 항목을 **본인 값**으로 변경합니다.
   ```js
   const OWNER = "YOUR_GH_USER";
   const REPO  = "YOUR_REPO";
   const LABEL = "board";
   ```

## 3) 메뉴 연결
- 기존 네비게이션의 ‘브레인게시판’ 링크를 `board.html`로 연결:
  ```html
  <a href="/board.html">브레인게시판</a>
  ```
  (하위 폴더에 넣었다면 경로를 맞춰주세요: `/board/board.html` 등)

## 4) 새 글쓰기(이슈 생성)
- “새 글쓰기” 버튼은 `https://github.com/OWNER/REPO/issues/new?labels=board`로 이동합니다.
- 템플릿을 쓰고 싶다면 저장소에 `.github/ISSUE_TEMPLATE/board.yml`을 만들고,
  `?template=board.yml&labels=board` 쿼리를 추가할 수 있습니다.

## 5) 검색/페이지
- 상단 검색창은 제목/본문 전체를 검색합니다.
- 페이지당 20개(코드 내 `PER_PAGE`)씩 불러오며, 다음/이전 버튼으로 이동합니다.

## 6) 권한/제한
- **읽기(목록/검색)**: 공개 저장소면 토큰 없이 가능(익명).  
- **쓰기(새 글쓰기)**: GitHub 로그인 필요. 비회원 글 허용이 필요하면 Firebase/Supabase 등 별도 백엔드 고려.

## 7) 고급 옵션
- 이슈에 카테고리 라벨을 추가(`공지`, `Q&A` 등)하고, 목록에서 `label:` 필터를 더 붙일 수 있습니다.
- 댓글을 페이지 안에서 보이게 하려면 **Giscus/Utterances**를 board.html 하단에 추가해도 됩니다(해당 디스커션/이슈와 연결).

## 8) 대안(서버리스/풀스택)
- **서버리스 DB**(Firebase/Supabase/Appwrite/PocketBase 등) + JS로 CRUD 구현 → 사용자 로그인/역할, 파일 업로드 가능
- **자체 백엔드**(Node/Express 등) → 자유도 최상, 서버 운영 필요

문의 시 Nginx/Cloudflare 페이지 라우팅, OAuth 연동, 스팸 방지(Recaptcha) 등도 지원 예제를 드릴 수 있습니다.
