#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
올공(allgongin.com) Cloudflare Email Routing 자동 설정 스크립트.

하는 일 (모두 멱등 — 여러 번 실행해도 안전):
  1. Cloudflare 에서 allgongin.com 존(zone) 확인 (없으면 생성 후 네임서버 안내)
  2. 웹사이트용 DNS 레코드 보장 (GitHub Pages A x4 + www CNAME)
  3. Email Routing 활성화 (MX/SPF 레코드 자동 생성)
  4. 수신 대상 주소 allgongin@gmail.com 등록 (확인 메일 발송됨)
  5. 라우팅 규칙 생성: contact@allgongin.com -> allgongin@gmail.com
  6. SPF 레코드에 Gmail 발신용 include:_spf.google.com 병합
  7. DMARC 레코드(_dmarc, p=none) 없으면 추가

사용법:
  export CLOUDFLARE_API_TOKEN="발급받은 토큰"
  python3 automation/email/cloudflare_email_setup.py

토큰에 필요한 권한은 automation/email/README.md 참고.
비밀값은 환경변수로만 전달하며 코드/저장소에 저장하지 않는다.
"""

import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.cloudflare.com/client/v4"

ZONE_NAME = os.environ.get("ZONE_NAME", "allgongin.com")
CONTACT_ADDR = os.environ.get("CONTACT_ADDR", "contact@allgongin.com")
DEST_ADDR = os.environ.get("DEST_ADDR", "allgongin@gmail.com")

GITHUB_PAGES_IPS = [
    "185.199.108.153",
    "185.199.109.153",
    "185.199.110.153",
    "185.199.111.153",
]
GITHUB_PAGES_CNAME = "buglerrr.github.io"

SPF_WANT = "v=spf1 include:_spf.mx.cloudflare.net include:_spf.google.com ~all"
DMARC_WANT = "v=DMARC1; p=none; rua=mailto:" + DEST_ADDR


def die(msg):
    print("\n[오류] " + msg)
    sys.exit(1)


def api(method, path, body=None, ok_codes=(1000000,)):
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if not token:
        die("환경변수 CLOUDFLARE_API_TOKEN 이 없습니다. README.md 의 토큰 발급 절차를 따라 주세요.")
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode())
        except Exception:
            die("HTTP %s: %s %s" % (e.code, method, path))
    except urllib.error.URLError as e:
        die("Cloudflare API 접속 실패: %s (인터넷/프록시 환경 확인)" % e.reason)
    if not data.get("success"):
        errs = data.get("errors") or []
        codes = {e.get("code") for e in errs}
        if codes & set(ok_codes):
            return data
        die("%s %s 실패: %s" % (method, path, json.dumps(errs, ensure_ascii=False)))
    return data


def get_all(path):
    out, page = [], 1
    while True:
        sep = "&" if "?" in path else "?"
        d = api("GET", "%s%spage=%d&per_page=50" % (path, sep, page))
        out.extend(d.get("result") or [])
        info = d.get("result_info") or {}
        if page >= (info.get("total_pages") or 1):
            return out
        page += 1


def main():
    print("== 0. 토큰 확인 ==")
    api("GET", "/user/tokens/verify")
    print("  토큰 유효함")

    print("== 1. 존(zone) 확인: %s ==" % ZONE_NAME)
    zones = api("GET", "/zones?name=" + ZONE_NAME).get("result") or []
    if zones:
        zone = zones[0]
        print("  존 존재: id=%s, 상태=%s" % (zone["id"], zone["status"]))
    else:
        accounts = api("GET", "/accounts").get("result") or []
        if not accounts:
            die("존이 없고 계정 목록도 조회할 수 없습니다. 토큰에 Account Settings:Read 권한을 추가하거나, "
                "Cloudflare 대시보드에서 먼저 사이트(%s)를 추가해 주세요." % ZONE_NAME)
        zone = api("POST", "/zones", {
            "name": ZONE_NAME,
            "account": {"id": accounts[0]["id"]},
            "type": "full",
        })["result"]
        print("  존 새로 생성됨: id=%s" % zone["id"])
    zid = zone["id"]
    acct_id = zone["account"]["id"]

    if zone["status"] != "active":
        print("\n  ★ 존이 아직 활성화되지 않았습니다 (status=%s)." % zone["status"])
        print("  ★ 도메인 등록기관(hosting.co.kr)에서 네임서버를 아래로 변경해야 이메일이 동작합니다:")
        for ns in zone.get("name_servers") or []:
            print("      - " + ns)
        print("  ★ 네임서버 변경 후에도 이 스크립트가 만든 설정은 그대로 유지됩니다.\n")

    print("== 2. 웹사이트 DNS 레코드 보장 (GitHub Pages) ==")
    records = get_all("/zones/%s/dns_records" % zid)
    apex_a = {r["content"] for r in records if r["type"] == "A" and r["name"] == ZONE_NAME}
    for ip in GITHUB_PAGES_IPS:
        if ip in apex_a:
            print("  A %s -> %s (있음)" % (ZONE_NAME, ip))
        else:
            api("POST", "/zones/%s/dns_records" % zid, {
                "type": "A", "name": "@", "content": ip, "ttl": 1, "proxied": False,
            })
            print("  A %s -> %s (추가)" % (ZONE_NAME, ip))
    www = [r for r in records if r["name"] == "www." + ZONE_NAME and r["type"] == "CNAME"]
    if www:
        print("  CNAME www -> %s (있음)" % www[0]["content"])
    else:
        api("POST", "/zones/%s/dns_records" % zid, {
            "type": "CNAME", "name": "www", "content": GITHUB_PAGES_CNAME,
            "ttl": 1, "proxied": False,
        })
        print("  CNAME www -> %s (추가)" % GITHUB_PAGES_CNAME)

    print("== 3. Email Routing 활성화 ==")
    settings = api("GET", "/zones/%s/email/routing" % zid)["result"]
    if settings.get("enabled"):
        print("  이미 활성화됨")
    else:
        # 신형 엔드포인트(/dns) 우선, 실패 시 구형(/enable) 시도
        try:
            api("POST", "/zones/%s/email/routing/dns" % zid, {"name": ZONE_NAME})
        except SystemExit:
            api("POST", "/zones/%s/email/routing/enable" % zid, {})
        print("  활성화 완료 (MX/SPF 레코드 자동 생성됨)")

    print("== 4. 수신 대상 주소 등록: %s ==" % DEST_ADDR)
    addrs = get_all("/accounts/%s/email/routing/addresses" % acct_id)
    mine = [a for a in addrs if a.get("email", "").lower() == DEST_ADDR.lower()]
    if mine and mine[0].get("verified"):
        print("  이미 등록·인증 완료")
    elif mine:
        print("  등록됨, 인증 대기중 — %s 받은편지함에서 Cloudflare 확인 메일의 버튼을 눌러주세요." % DEST_ADDR)
    else:
        api("POST", "/accounts/%s/email/routing/addresses" % acct_id, {"email": DEST_ADDR})
        print("  등록 완료 — %s 로 확인 메일이 발송되었습니다. 메일 안의 인증 버튼을 꼭 눌러주세요." % DEST_ADDR)

    print("== 5. 라우팅 규칙: %s -> %s ==" % (CONTACT_ADDR, DEST_ADDR))
    rules = get_all("/zones/%s/email/routing/rules" % zid)
    have = False
    for r in rules:
        for m in r.get("matchers") or []:
            if m.get("type") == "literal" and m.get("value", "").lower() == CONTACT_ADDR.lower():
                have = True
    if have:
        print("  규칙 이미 존재")
    else:
        api("POST", "/zones/%s/email/routing/rules" % zid, {
            "name": "contact forward",
            "enabled": True,
            "matchers": [{"type": "literal", "field": "to", "value": CONTACT_ADDR}],
            "actions": [{"type": "forward", "value": [DEST_ADDR]}],
        })
        print("  규칙 생성 완료")

    print("== 6. SPF 레코드에 Gmail 발신 허용 병합 ==")
    records = get_all("/zones/%s/dns_records?type=TXT" % zid)
    spf = [r for r in records if r["name"] == ZONE_NAME and "v=spf1" in r["content"]]
    if spf:
        cur = spf[0]["content"].strip('"')
        if "_spf.google.com" in cur:
            print("  이미 포함됨: %s" % cur)
        else:
            api("PUT", "/zones/%s/dns_records/%s" % (zid, spf[0]["id"]), {
                "type": "TXT", "name": "@", "content": SPF_WANT, "ttl": 1,
            })
            print("  갱신: %s" % SPF_WANT)
    else:
        api("POST", "/zones/%s/dns_records" % zid, {
            "type": "TXT", "name": "@", "content": SPF_WANT, "ttl": 1,
        })
        print("  생성: %s" % SPF_WANT)

    print("== 7. DMARC 레코드 확인 ==")
    dmarc = [r for r in records if r["name"] == "_dmarc." + ZONE_NAME]
    if dmarc:
        print("  이미 존재: %s" % dmarc[0]["content"])
    else:
        api("POST", "/zones/%s/dns_records" % zid, {
            "type": "TXT", "name": "_dmarc", "content": DMARC_WANT, "ttl": 1,
        })
        print("  생성: %s" % DMARC_WANT)

    print("\n========== 완료 ==========")
    print("남은 수동 단계 (README.md 상세 안내):")
    if zone["status"] != "active":
        print("  1) hosting.co.kr 에서 네임서버를 위에 표시된 Cloudflare 네임서버로 변경")
    print("  2) %s 받은편지함에서 Cloudflare 대상 주소 확인 메일 인증" % DEST_ADDR)
    print("  3) Gmail '다른 주소에서 메일 보내기'로 %s 발신 설정 (앱 비밀번호 사용)" % CONTACT_ADDR)


if __name__ == "__main__":
    main()
