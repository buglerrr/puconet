# contact@allgongin.com 회사 이메일 설정 가이드 (Cloudflare Email Routing)

목표:

- **수신**: 고객이 `contact@allgongin.com` 으로 보낸 메일이 `allgongin@gmail.com` 받은편지함에 도착
- **발신**: Gmail 에서 보내는 사람 주소를 `contact@allgongin.com` 으로 선택해서 답장/발송

비용은 전부 무료입니다 (Cloudflare Free 플랜 + Gmail).

현재 상태(2026-08 확인): `allgongin.com` 은 hosting.co.kr 네임서버를 쓰고 있고
MX 레코드가 없어 **이메일 수신이 전혀 안 되는 상태**입니다. Cloudflare Email
Routing 을 쓰려면 도메인 DNS 를 Cloudflare 로 옮겨야 합니다 (아래 1단계).
웹사이트(GitHub Pages)는 스크립트가 동일한 DNS 레코드를 미리 만들어 두므로
네임서버를 바꿔도 **중단 없이 그대로 동작**합니다.

---

## 1단계. Cloudflare 에 도메인 추가 + 네임서버 변경 (1회, 수동)

1. https://dash.cloudflare.com 가입/로그인 (allgongin@gmail.com 계정 권장)
2. **Add a site** → `allgongin.com` 입력 → **Free** 플랜 선택
3. Cloudflare 가 기존 DNS 레코드를 자동으로 불러옵니다. 그대로 진행하면
   Cloudflare 전용 네임서버 2개를 알려줍니다 (예: `xxx.ns.cloudflare.com`).
4. **hosting.co.kr(도메인 등록기관) 관리 페이지**에서 `allgongin.com` 의
   네임서버를 기존 `ns1~ns4.hosting.co.kr` 에서 Cloudflare 가 알려준 2개로 변경
5. 반영까지 보통 수 분~수 시간 (최대 24시간). Cloudflare 대시보드에서
   존 상태가 **Active** 가 되면 완료

> 3단계 자동 스크립트를 먼저 실행해도 됩니다 — 존이 없으면 스크립트가
> 만들어 주고 네임서버 2개를 출력해 줍니다.

## 2단계. API 토큰 발급 (1회, 수동)

https://dash.cloudflare.com/profile/api-tokens → **Create Token** →
**Create Custom Token** 으로 아래 권한을 준 토큰을 만듭니다:

| 구분 | 권한 | 수준 |
|---|---|---|
| Account | Email Routing Addresses | Edit |
| Account | Account Settings | Read |
| Zone | Email Routing Rules | Edit |
| Zone | DNS | Edit |
| Zone | Zone | Edit |

Zone Resources 는 **All zones** 또는 `allgongin.com` 지정.
토큰 문자열은 **절대 저장소에 커밋하지 말고** 환경변수로만 사용합니다.

## 3단계. 자동 설정 스크립트 실행

인터넷이 되는 아무 컴퓨터에서 (Python 3 만 있으면 됨, 추가 패키지 불필요):

```bash
export CLOUDFLARE_API_TOKEN="2단계에서 발급한 토큰"
python3 automation/email/cloudflare_email_setup.py
```

스크립트가 자동으로 처리하는 것:

- 존 확인/생성, GitHub Pages 용 A/CNAME 레코드 보장 (웹사이트 무중단)
- Email Routing 활성화 (MX + SPF 레코드 자동 생성)
- 대상 주소 `allgongin@gmail.com` 등록 → **확인 메일 발송됨**
- 라우팅 규칙 `contact@allgongin.com → allgongin@gmail.com` 생성
- SPF 에 Gmail 발신 허용(`include:_spf.google.com`) 병합, DMARC 추가

여러 번 실행해도 안전합니다(이미 된 항목은 건너뜀).

## 4단계. 대상 주소 인증 (1회, 수동)

`allgongin@gmail.com` 받은편지함에서 Cloudflare 가 보낸
**"Verify your email address"** 메일을 열고 확인 버튼을 누릅니다.
이걸 눌러야 실제 전달이 시작됩니다.

여기까지 하면 **수신 완료** — 아무 메일 계정에서 `contact@allgongin.com` 으로
테스트 메일을 보내 `allgongin@gmail.com` 에 도착하는지 확인하세요.

## 5단계. 발신 설정 — Gmail "다른 주소에서 메일 보내기" (1회, 수동)

Cloudflare Email Routing 은 수신 전용이라, 발신은 Gmail 의 별칭 발신 기능을
사용합니다 (업계 표준 조합이며 무료).

1. **앱 비밀번호 만들기**
   - Google 계정에 2단계 인증이 켜져 있어야 합니다 (https://myaccount.google.com/security)
   - https://myaccount.google.com/apppasswords → 앱 이름 아무거나(예: `allgong-mail`) → 생성된 16자리 비밀번호 복사
2. **Gmail 설정**
   - Gmail(allgongin@gmail.com) → ⚙️ → **모든 설정 보기** → **계정 및 가져오기** 탭
   - **다른 주소에서 메일 보내기** → **다른 이메일 주소 추가**
   - 이름: `올공(ALLGONG)` / 이메일: `contact@allgongin.com` / **"별칭으로 취급" 체크 유지** → 다음
   - SMTP 서버: `smtp.gmail.com` / 포트: `587` (TLS)
   - 사용자 이름: `allgongin@gmail.com` / 비밀번호: **위에서 만든 16자리 앱 비밀번호** → 계정 추가
3. **확인 코드 입력**
   - Gmail 이 `contact@allgongin.com` 으로 확인 메일을 보냅니다.
   - 4단계까지 완료했다면 이 메일이 다시 `allgongin@gmail.com` 받은편지함으로 들어옵니다 → 코드 입력
4. (권장) 계정 및 가져오기 → 기본 주소를 `contact@allgongin.com` 으로 지정하거나,
   "메일을 받은 주소로 답장" 을 선택하면 고객 문의에 자동으로 회사 주소로 답장됩니다.

이제 메일 작성 시 **보낸사람** 을 눌러 `contact@allgongin.com` 을 선택해
발송할 수 있습니다.

## 문제 해결

- **메일이 안 들어와요**: Cloudflare 대시보드 → Email → Email Routing 에서
  존 상태 Active 인지, 대상 주소가 Verified 인지, MX 레코드 3개
  (`route1~3.mx.cloudflare.net`)가 있는지 확인.
- **보낸 메일이 스팸으로 가요**: SPF 레코드가
  `v=spf1 include:_spf.mx.cloudflare.net include:_spf.google.com ~all`
  인지 확인 (스크립트가 자동 설정). DMARC 는 `p=none` 으로 관찰만 합니다.
- **웹사이트가 안 열려요**: Cloudflare DNS 에 A 레코드 4개
  (185.199.108~111.153, DNS only)와 `www → buglerrr.github.io` CNAME 이
  있는지 확인. 스크립트를 다시 실행하면 자동 복구됩니다.
