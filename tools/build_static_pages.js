#!/usr/bin/env node
/**
 * 공고·뉴스 정적 페이지 생성기 (GitHub Actions에서 주기 실행)
 *
 * Firestore(jobs, news 컬렉션)를 익명 REST로 읽어
 *   job/{문서ID}.html, job/index.html
 *   news/{문서ID}.html, news/index.html
 *   sitemap-pages.xml
 * 을 저장소 루트에 생성한다. 검색엔진이 공고·뉴스를 개별 페이지로
 * 색인할 수 있게 하기 위한 것(SPA 해시 주소는 색인 불가).
 *
 * 의존성 없음(Node 18+ 내장 fetch). 실행 위치: 저장소 루트.
 */
const fs = require('fs');
const path = require('path');

const FS_BASE = 'https://firestore.googleapis.com/v1/projects/recruit-board/databases/(default)/documents';
const SITE = 'https://www.allgongin.com';
const ROOT = process.cwd();

// ───────── Firestore REST 유틸 ─────────
function unwrap(v) {
  if (v == null || typeof v !== 'object') return '';
  if ('stringValue' in v) return v.stringValue;
  if ('integerValue' in v) return String(v.integerValue);
  if ('doubleValue' in v) return String(v.doubleValue);
  if ('booleanValue' in v) return v.booleanValue;
  if ('timestampValue' in v) return v.timestampValue;
  return '';
}

async function fetchAll(coll) {
  const docs = [];
  let pageToken = '';
  for (let i = 0; i < 50; i++) {
    const url = `${FS_BASE}/${coll}?pageSize=300${pageToken ? '&pageToken=' + encodeURIComponent(pageToken) : ''}`;
    const res = await fetch(url);
    if (!res.ok) throw new Error(`Firestore ${coll} 조회 실패: HTTP ${res.status}`);
    const data = await res.json();
    for (const d of data.documents || []) {
      const fields = {};
      for (const [k, v] of Object.entries(d.fields || {})) fields[k] = unwrap(v);
      docs.push({ id: d.name.split('/').pop(), ...fields });
    }
    pageToken = data.nextPageToken || '';
    if (!pageToken) break;
  }
  return docs;
}

// ───────── 공통 헬퍼 ─────────
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

const jsonLd = (obj) => JSON.stringify(obj).replace(/</g, '\\u003c');

function kstToday() {
  const d = new Date(Date.now() + 9 * 3600 * 1000);
  return d.toISOString().slice(0, 10);
}

function dday(deadline, today) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(deadline)) return '';
  const diff = Math.round((new Date(deadline + 'T00:00:00Z') - new Date(today + 'T00:00:00Z')) / 86400000);
  if (diff < 0) return '마감';
  if (diff === 0) return 'D-DAY';
  return `D-${diff}`;
}

function pageShell({ title, description, canonical, ogImage, body, ld }) {
  return `<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${esc(title)}</title>
  <meta name="description" content="${esc(description)}">
  <link rel="canonical" href="${esc(canonical)}">
  <meta property="og:type" content="article">
  <meta property="og:title" content="${esc(title)}">
  <meta property="og:description" content="${esc(description)}">
  <meta property="og:url" content="${esc(canonical)}">
  ${ogImage ? `<meta property="og:image" content="${esc(ogImage)}">` : ''}
  <link rel="icon" href="${SITE}/ALLGONG%20CI.png">
  ${ld ? `<script type="application/ld+json">${ld}</script>` : ''}
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Malgun Gothic', 'Apple SD Gothic Neo', sans-serif; color: #212529; line-height: 1.7; background: #f8f9fa; }
    .top { background: #fff; border-bottom: 2px solid #1971c2; }
    .top-in { max-width: 860px; margin: 0 auto; padding: 14px 18px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
    .brand { font-size: 22px; font-weight: 800; color: #1971c2; text-decoration: none; }
    .brand small { font-size: 12px; font-weight: 400; color: #868e96; margin-left: 8px; }
    .top-nav a { color: #495057; text-decoration: none; font-size: 14px; margin-left: 14px; }
    .top-nav a:hover { color: #1971c2; }
    main { max-width: 860px; margin: 24px auto 40px; padding: 0 18px; }
    .card { background: #fff; border: 1px solid #dee2e6; border-radius: 10px; padding: 26px 28px; }
    .crumb { font-size: 13px; color: #868e96; margin-bottom: 14px; }
    .crumb a { color: #868e96; text-decoration: none; }
    h1 { font-size: 24px; line-height: 1.4; margin-bottom: 8px; }
    .byline { color: #495057; font-size: 15px; margin-bottom: 18px; }
    .byline .badge { display: inline-block; background: #e7f5ff; color: #1971c2; border-radius: 4px; font-size: 12px; padding: 2px 8px; margin-left: 6px; }
    table.info { width: 100%; border-collapse: collapse; margin: 14px 0 20px; font-size: 14.5px; }
    table.info th { width: 120px; text-align: left; background: #f8f9fa; color: #495057; font-weight: 600; padding: 9px 12px; border: 1px solid #e9ecef; white-space: nowrap; }
    table.info td { padding: 9px 12px; border: 1px solid #e9ecef; }
    .dday { color: #e03131; font-weight: 700; }
    h2 { font-size: 17px; margin: 22px 0 8px; padding-left: 10px; border-left: 4px solid #1971c2; }
    .content-box { background: #f8f9fa; border-radius: 8px; padding: 14px 16px; font-size: 14.5px; white-space: pre-wrap; word-break: break-word; }
    .cta { margin: 26px 0 6px; display: flex; gap: 10px; flex-wrap: wrap; }
    .btn { display: inline-block; padding: 12px 22px; border-radius: 8px; font-size: 15px; font-weight: 700; text-decoration: none; text-align: center; }
    .btn-primary { background: #1971c2; color: #fff; }
    .btn-outline { background: #fff; color: #1971c2; border: 2px solid #1971c2; }
    article.news p { margin: 0 0 14px; font-size: 15.5px; }
    article.news img.thumb { max-width: 100%; border-radius: 8px; margin: 6px 0 18px; }
    .src { font-size: 13px; color: #868e96; border-top: 1px dashed #dee2e6; margin-top: 18px; padding-top: 12px; word-break: break-all; }
    ul.listing { list-style: none; }
    ul.listing li { border-bottom: 1px solid #f1f3f5; padding: 12px 4px; display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }
    ul.listing a { color: #212529; text-decoration: none; font-size: 15px; }
    ul.listing a:hover { color: #1971c2; text-decoration: underline; }
    ul.listing .co { color: #1971c2; font-weight: 600; margin-right: 8px; }
    ul.listing .dl { color: #868e96; font-size: 13px; white-space: nowrap; }
    footer { border-top: 1px solid #dee2e6; background: #fff; margin-top: 20px; }
    .foot-in { max-width: 860px; margin: 0 auto; padding: 20px 18px; font-size: 12.5px; color: #868e96; line-height: 1.8; }
    .foot-in a { color: #495057; text-decoration: none; }
    @media (max-width: 600px) { .card { padding: 18px 16px; } h1 { font-size: 20px; } table.info th { width: 92px; } }
  </style>
</head>
<body>
  <div class="top"><div class="top-in">
    <a class="brand" href="${SITE}/">올공 <small>공공기관 채용의 모든 것</small></a>
    <nav class="top-nav"><a href="${SITE}/#board">채용정보</a><a href="${SITE}/job/">공고 목록</a><a href="${SITE}/news/">공공기관 뉴스</a></nav>
  </div></div>
  <main>${body}</main>
  <footer><div class="foot-in">
    올공(ALLGONG) — 공공기관 경영평가위원 출신 교수가 운영하는 공공기관·공기업 채용정보 포털<br>
    (주)한국퍼블릭잡컨설팅 · <a href="${SITE}/">www.allgongin.com</a>
  </div></footer>
</body>
</html>
`;
}

// ───────── 공고 페이지 ─────────
const EMP_TYPE_MAP = [
  ['정규직', 'FULL_TIME'], ['무기계약직', 'FULL_TIME'], ['청년인턴', 'INTERN'],
  ['인턴', 'INTERN'], ['비정규직', 'TEMPORARY'], ['계약직', 'CONTRACTOR'],
];

function jobPage(job, today) {
  const url = `${SITE}/job/${encodeURIComponent(job.id)}.html`;
  const dd = dday(job.deadline, today);
  const rows = [
    ['기관명', job.company], ['기관유형', job.instType], ['고용유형', job.employmentType],
    ['채용구분', job.careerType], ['채용인원', job.recruitmentCount && job.recruitmentCount !== '0' ? job.recruitmentCount + '명' : ''],
    ['근무지역', job.location], ['학력조건', job.education], ['우대조건', job.preference],
    ['NCS 직무', job.ncsCode],
    ['접수 마감', job.deadline ? `${job.deadline}${dd ? ` <span class="dday">(${dd})</span>` : ''}` : '공고 참조'],
  ].filter(([, v]) => v);

  const desc = `${job.company} ${job.title} — 접수 마감 ${job.deadline || '공고 참조'}. ` +
    `고용유형 ${job.employmentType || '공고 참조'}, 근무지 ${job.location || '공고 참조'}. 공공기관 채용정보 포털 올공.`;

  const ldObj = {
    '@context': 'https://schema.org', '@type': 'JobPosting',
    title: job.title, description: `${job.company} ${job.title}. ${job.content || ''}`.trim(),
    hiringOrganization: { '@type': 'Organization', name: job.company },
    datePosted: (job.createdAt || '').slice(0, 10) || today,
    employmentType: (EMP_TYPE_MAP.find(([k]) => (job.employmentType || '').includes(k)) || [])[1],
    jobLocation: { '@type': 'Place', address: { '@type': 'PostalAddress', addressLocality: job.location || '대한민국', addressCountry: 'KR' } },
    directApply: false,
  };
  if (/^\d{4}-\d{2}-\d{2}$/.test(job.deadline)) ldObj.validThrough = job.deadline + 'T23:59:59+09:00';
  if (!ldObj.employmentType) delete ldObj.employmentType;

  const body = `
  <div class="card">
    <div class="crumb"><a href="${SITE}/">올공 홈</a> › <a href="${SITE}/job/">공공기관 채용공고</a> › ${esc(job.company)}</div>
    <h1>${esc(job.title)}</h1>
    <div class="byline">${esc(job.company)}${job.instType ? `<span class="badge">${esc(job.instType)}</span>` : ''}</div>
    <table class="info">${rows.map(([k, v]) => `<tr><th>${k}</th><td>${k === '접수 마감' ? v.replace(/&(?!amp;|lt;|gt;|quot;)/g, '&amp;') : esc(v)}</td></tr>`).join('\n')}</table>
    ${job.content ? `<h2>전형 절차</h2><div class="content-box">${esc(job.content)}</div>` : ''}
    <div class="cta">
      <a class="btn btn-primary" href="${SITE}/#job-detail-${encodeURIComponent(job.id)}">올공에서 이 공고 보기</a>
      ${job.detailUrl && job.detailUrl !== '#' ? `<a class="btn btn-outline" href="${esc(job.detailUrl)}" target="_blank" rel="noopener nofollow">원문 공고 · 지원하기</a>` : ''}
    </div>
    <p style="font-size:13px;color:#868e96;margin-top:10px;">전체 공공기관 채용공고와 채용달력은 <a href="${SITE}/">올공(allgongin.com)</a>에서 확인할 수 있습니다.</p>
  </div>`;

  return pageShell({ title: `${job.company} ${job.title} | 올공`, description: desc, canonical: url, ogImage: job.imageUrl, body, ld: jsonLd(ldObj) });
}

// ───────── 뉴스 페이지 ─────────
function newsPage(post) {
  const url = `${SITE}/news/${encodeURIComponent(post.id)}.html`;
  const paras = String(post.content || '').split(/\n{2,}/).map((p) => p.trim()).filter(Boolean);
  const srcIdx = paras.findIndex((p) => p.startsWith('※'));
  const bodyParas = srcIdx >= 0 ? paras.slice(0, srcIdx) : paras;
  const srcParas = srcIdx >= 0 ? paras.slice(srcIdx) : [];

  const ldObj = {
    '@context': 'https://schema.org', '@type': 'Article',
    headline: post.title, description: post.desc || '',
    datePublished: (post.createdAt || '').slice(0, 10) || post.date || '',
    author: { '@type': 'Organization', name: '올공(ALLGONG)' },
    publisher: { '@type': 'Organization', name: '올공(ALLGONG)', url: SITE },
    mainEntityOfPage: url,
  };
  if (post.imageUrl) ldObj.image = post.imageUrl;

  const body = `
  <div class="card">
    <div class="crumb"><a href="${SITE}/">올공 홈</a> › <a href="${SITE}/news/">공공기관 뉴스</a></div>
    <article class="news">
      <h1>${esc(post.title)}</h1>
      <div class="byline">올공 공공기관 뉴스 · ${esc(post.date || (post.createdAt || '').slice(0, 10))}</div>
      ${post.imageUrl ? `<img class="thumb" src="${esc(post.imageUrl)}" alt="${esc(post.title)}">` : ''}
      ${bodyParas.map((p) => `<p>${esc(p).replace(/\n/g, '<br>')}</p>`).join('\n')}
      ${srcParas.length ? `<div class="src">${srcParas.map((p) => esc(p).replace(/\n/g, '<br>')).join('<br>')}</div>` : ''}
    </article>
    <div class="cta">
      <a class="btn btn-primary" href="${SITE}/#news">공공기관 뉴스 더 보기</a>
      <a class="btn btn-outline" href="${SITE}/">오늘의 채용공고 보기</a>
    </div>
  </div>`;

  return pageShell({ title: `${post.title} | 올공 공공기관 뉴스`, description: post.desc || post.title, canonical: url, ogImage: post.imageUrl, body, ld: jsonLd(ldObj) });
}

// ───────── 목록 페이지 ─────────
function jobIndexPage(jobs, today) {
  const items = jobs.map((j) => {
    const dd = dday(j.deadline, today);
    return `<li><span><span class="co">${esc(j.company)}</span><a href="${SITE}/job/${encodeURIComponent(j.id)}.html">${esc(j.title)}</a></span><span class="dl">${esc(j.deadline || '공고 참조')}${dd ? ` · <span class="dday">${dd}</span>` : ''}</span></li>`;
  }).join('\n');
  const body = `
  <div class="card">
    <div class="crumb"><a href="${SITE}/">올공 홈</a> › 공공기관 채용공고</div>
    <h1>공공기관 채용공고 (진행 중 ${jobs.length}건)</h1>
    <div class="byline">매일 3회 자동 갱신 · 기준일 ${today}</div>
    <ul class="listing">${items}</ul>
    <div class="cta"><a class="btn btn-primary" href="${SITE}/">올공에서 채용달력과 함께 보기</a></div>
  </div>`;
  return pageShell({ title: `공공기관 채용공고 목록 | 올공`, description: `현재 진행 중인 공공기관·공기업 채용공고 ${jobs.length}건 — 기관명, 마감일, 전형절차를 한눈에. 공공기관 채용정보 포털 올공.`, canonical: `${SITE}/job/`, body });
}

function newsIndexPage(posts) {
  const items = posts.map((p) => `<li><a href="${SITE}/news/${encodeURIComponent(p.id)}.html">${esc(p.title)}</a><span class="dl">${esc(p.date || (p.createdAt || '').slice(0, 10))}</span></li>`).join('\n');
  const body = `
  <div class="card">
    <div class="crumb"><a href="${SITE}/">올공 홈</a> › 공공기관 뉴스</div>
    <h1>공공기관 뉴스 (${posts.length}건)</h1>
    <div class="byline">공공기관 채용·경영평가 관련 소식을 재구성해 전합니다.</div>
    <ul class="listing">${items}</ul>
    <div class="cta"><a class="btn btn-primary" href="${SITE}/#news">올공에서 뉴스 게시판 보기</a></div>
  </div>`;
  return pageShell({ title: `공공기관 뉴스 목록 | 올공`, description: `공공기관·공기업 채용과 경영평가 관련 최신 뉴스 ${posts.length}건. 공공기관 채용정보 포털 올공.`, canonical: `${SITE}/news/`, body });
}

// ───────── sitemap ─────────
function sitemapPages(jobs, posts, today) {
  const urls = [
    { loc: `${SITE}/job/`, lastmod: today, freq: 'daily', pri: '0.8' },
    { loc: `${SITE}/news/`, lastmod: today, freq: 'daily', pri: '0.8' },
    ...jobs.map((j) => ({ loc: `${SITE}/job/${encodeURIComponent(j.id)}.html`, lastmod: today, freq: 'daily', pri: '0.7' })),
    ...posts.map((p) => ({ loc: `${SITE}/news/${encodeURIComponent(p.id)}.html`, lastmod: (p.date || (p.createdAt || '').slice(0, 10) || today), freq: 'monthly', pri: '0.6' })),
  ];
  return `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n` +
    urls.map((u) => `  <url><loc>${esc(u.loc)}</loc><lastmod>${u.lastmod}</lastmod><changefreq>${u.freq}</changefreq><priority>${u.pri}</priority></url>`).join('\n') +
    `\n</urlset>\n`;
}

// ───────── 메인 ─────────
(async () => {
  const today = kstToday();
  console.log(`기준일(KST): ${today}`);

  const [jobsRaw, newsRaw] = await Promise.all([fetchAll('jobs'), fetchAll('news')]);
  console.log(`Firestore 조회: 공고 ${jobsRaw.length}건, 뉴스 ${newsRaw.length}건`);

  // 진행 중 공고만 (마감일이 오늘 이후이거나, 날짜 형식이 아니면 포함)
  const jobs = jobsRaw
    .filter((j) => j.title && j.company)
    .filter((j) => !/^\d{4}-\d{2}-\d{2}$/.test(j.deadline) || j.deadline >= today)
    .sort((a, b) => String(a.deadline || '9999').localeCompare(String(b.deadline || '9999')) || String(a.company).localeCompare(String(b.company)));

  const posts = newsRaw
    .filter((p) => p.title && p.content)
    .sort((a, b) => String(b.createdAt || b.date || '').localeCompare(String(a.createdAt || a.date || '')));

  // 디렉터리 재생성 (만료 공고 페이지 자동 정리)
  for (const dir of ['job', 'news']) {
    fs.rmSync(path.join(ROOT, dir), { recursive: true, force: true });
    fs.mkdirSync(path.join(ROOT, dir), { recursive: true });
  }

  for (const j of jobs) fs.writeFileSync(path.join(ROOT, 'job', `${j.id}.html`), jobPage(j, today));
  fs.writeFileSync(path.join(ROOT, 'job', 'index.html'), jobIndexPage(jobs, today));
  for (const p of posts) fs.writeFileSync(path.join(ROOT, 'news', `${p.id}.html`), newsPage(p));
  fs.writeFileSync(path.join(ROOT, 'news', 'index.html'), newsIndexPage(posts));
  fs.writeFileSync(path.join(ROOT, 'sitemap-pages.xml'), sitemapPages(jobs, posts, today));

  console.log(`생성 완료: 공고 페이지 ${jobs.length} + 목록 1, 뉴스 페이지 ${posts.length} + 목록 1, sitemap-pages.xml`);
})().catch((e) => { console.error(e); process.exit(1); });
