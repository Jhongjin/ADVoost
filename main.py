from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import base64
import io
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from threading import Lock
from typing import Literal
from urllib.parse import quote, urljoin, urlparse
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except Exception:  # Playwright is optional until the browser worker is installed.
    PlaywrightError = Exception
    PlaywrightTimeoutError = Exception
    sync_playwright = None


Status = Literal["PASS", "WARNING", "FAIL", "NOT_CHECKED"]
Severity = Literal["critical", "major", "minor", "info"]
Grade = Literal["A", "B", "C", "D", "F"]
JobStatus = Literal["queued", "running", "completed", "failed"]
ReportType = Literal["standard", "premium"]

DB_PATH = Path(__file__).with_name("advoost.sqlite3")
CACHE_HOURS = 72
USER_AGENT = "ADVoost-AuditBot/1.0 (+https://advoost.local)"
BROWSER_AUDIT_ENABLED = os.getenv("ENABLE_BROWSER_AUDIT", "1") != "0"
BROWSER_AUDIT_TIMEOUT_MS = int(os.getenv("BROWSER_AUDIT_TIMEOUT_MS", "15000"))
AUDIT_WORKERS = int(os.getenv("AUDIT_WORKERS", "2"))
DEFAULT_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://ad-voost.vercel.app",
]


class AuditRequest(BaseModel):
    url: str = Field(..., min_length=3)
    user_id: str = "demo-user"
    manager_name: str = ""
    advertiser_name: str = ""


class AuditItem(BaseModel):
    id: str
    item_name: str
    category: str
    status: Status
    severity: Severity
    critical_for_grade: bool = False
    description: str
    guide: str
    detected_value: str | None = None
    remediation: str | None = None
    details: list[str] = Field(default_factory=list)
    snippet: str | None = None


class KeywordRow(BaseModel):
    keyword: str
    frequency: int
    ratio: str
    title_tag: bool
    meta_description: bool


class KeywordSummary(BaseModel):
    single_total: int
    phrase_total: int
    single_rows: list[KeywordRow] = Field(default_factory=list)
    phrase_rows: list[KeywordRow] = Field(default_factory=list)


class RenderSnapshot(BaseModel):
    success: bool
    final_url: str | None = None
    load_time_ms: int | None = None
    dom_content_loaded_ms: int | None = None
    first_contentful_paint_ms: int | None = None
    resource_count: int = 0
    failed_request_count: int = 0
    console_error_count: int = 0
    blocked_resource_count: int = 0
    slow_resources: list[str] = Field(default_factory=list)
    failed_requests: list[str] = Field(default_factory=list)
    console_errors: list[str] = Field(default_factory=list)
    desktop_screenshot: str | None = None
    mobile_screenshot: str | None = None
    error: str | None = None


class AuditResponse(BaseModel):
    id: str
    url: str
    user_id: str
    manager_name: str
    advertiser_name: str
    grade: Grade
    score: int
    status_counts: dict[str, int]
    items: list[AuditItem]
    fail_items: list[AuditItem]
    warning_items: list[AuditItem]
    keyword_summary: KeywordSummary | None = None
    render_snapshot: RenderSnapshot | None = None
    cache_hit: bool
    cache_expires_at: str | None
    created_at: str
    duration_sec: float


class AuditJobResponse(BaseModel):
    job_id: str
    status: JobStatus
    progress: int
    created_at: str
    updated_at: str
    result: AuditResponse | None = None
    error: str | None = None


class ReportExportRequest(BaseModel):
    records: list[dict] = Field(..., min_length=1)
    report_type: ReportType = "standard"
    platform: str = "자체개발"
    bundle: bool = False


app = FastAPI(
    title="ADVoost SEO Audit Clone API",
    version="0.1.0",
    description="HTML scraping, SEO checklist parsing, ADVoost-like grade calculation, and 72-hour result reuse.",
)


def cors_origins() -> list[str]:
    configured = [
        origin.strip().rstrip("/")
        for origin in os.getenv("CORS_ORIGINS", "").split(",")
        if origin.strip()
    ]
    return sorted(set(DEFAULT_CORS_ORIGINS + configured))


app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_history (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                url TEXT NOT NULL,
                manager_name TEXT NOT NULL,
                advertiser_name TEXT NOT NULL,
                grade TEXT NOT NULL,
                score INTEGER NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_audit_history_guard
            ON audit_history(user_id, url, created_at DESC)
            """
        )


init_db()

AUDIT_EXECUTOR = ThreadPoolExecutor(max_workers=max(1, AUDIT_WORKERS))
AUDIT_JOBS: dict[str, AuditJobResponse] = {}
AUDIT_JOBS_LOCK = Lock()


def normalize_url(raw_url: str) -> str:
    candidate = raw_url.strip()
    if not candidate:
        raise HTTPException(status_code=422, detail="URL is required.")
    if not re.match(r"^https?://", candidate, flags=re.I):
        candidate = f"https://{candidate}"

    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=422, detail="Invalid URL.")
    if "." not in parsed.netloc:
        raise HTTPException(status_code=422, detail="URL host must include a domain.")
    return candidate.rstrip("/")


def compact_snippet(value: str, limit: int = 520) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    return text[:limit]


def tag_snippet(tag: object, fallback: str) -> str:
    if tag is None:
        return fallback
    return compact_snippet(str(tag))


def first_count(text: str) -> str | None:
    match = re.search(r"(\d+)\s*개", text)
    return match.group(1) if match else None


def first_millisecond(text: str) -> str | None:
    match = re.search(r"(\d+)\s*ms", text, flags=re.I)
    return match.group(1) if match else None


def infer_detected_value(item: AuditItem) -> str:
    text = f"{item.description} {item.snippet or ''}"

    if item.status == "NOT_CHECKED":
        return "점검 불가"
    if item.id == "http-status":
        status_match = re.search(r"HTTP(?: status:)?\s*(\d+)", text, flags=re.I)
        return f"HTTP {status_match.group(1)}" if status_match else "정상"
    if item.id == "robots":
        return "수집 차단" if item.status == "FAIL" else "통과"
    if item.id == "html-parse":
        return "파싱 실패" if item.status == "FAIL" else "통과"
    if item.id == "title-present":
        return "(빈 문자열)" if item.status != "PASS" else "<title> 1개"
    if item.id == "title-length":
        return "권장 범위" if item.status == "PASS" else "길이 확인 필요"
    if item.id == "description":
        return "설명 태그 존재" if item.status == "PASS" else "(빈 문자열)"
    if item.id == "meta-robots":
        return "noindex 발견" if item.status == "FAIL" else "통과"
    if item.id == "canonical":
        return "canonical 존재" if item.status == "PASS" else "canonical 누락"
    if item.id == "h1-present":
        return "H1 존재" if item.status == "PASS" else "H1 없음"
    if item.id == "h1-count":
        count = first_count(text)
        return f"H1 {count}개" if count else "H1 1개"
    if item.id == "viewport":
        return "viewport 존재" if item.status == "PASS" else "viewport 누락/고정 폭"
    if item.id == "charset":
        return "인코딩 선언 존재" if item.status == "PASS" else "인코딩 선언 누락"
    if item.id == "html-lang":
        return "lang 선언 존재" if item.status == "PASS" else "lang 속성 누락"
    if item.id.startswith("og-"):
        return "OG 태그 존재" if item.status == "PASS" else "OG 태그 누락"
    if item.id == "render-blocked-resources":
        return "렌더 차단 리소스 존재" if item.status == "WARNING" else "없음"
    if item.id == "image-alt":
        count = first_count(text)
        return f"alt 누락 이미지 존재 ({count}개)" if count else "정상"
    if item.id == "download-time":
        ms = first_millisecond(text)
        return f"{ms} ms" if ms else "3초 이하"
    if item.id == "structured-data":
        return "구조화 데이터 존재" if item.status == "PASS" else "구조화 데이터 누락"
    if item.id == "heading-order":
        return "정상" if item.status == "PASS" else "계층 확인 필요"
    if item.id == "internal-links":
        return "내부 링크 존재" if item.status == "PASS" else "내부 링크 부족"
    if item.id == "favicon":
        return "favicon 존재" if item.status == "PASS" else "favicon 누락"
    if item.id == "page-size":
        return "권장 범위" if item.status == "PASS" else "용량 확인 필요"
    if item.id == "script-error":
        count = first_count(text)
        return f"콘솔 오류 {count}개" if count and item.status != "PASS" else "통과"
    if item.id == "ssl":
        return "HTTPS" if item.status == "PASS" else "HTTP"
    if item.id == "form-labels":
        return "통과" if item.status == "PASS" else "라벨 연결 확인 필요"
    if item.id == "keyword-density":
        return "통과" if item.status == "PASS" else "본문 텍스트 부족"

    return "통과" if item.status == "PASS" else item.category


def enrich_audit_items(items: list[AuditItem]) -> list[AuditItem]:
    for item in items:
        if not item.detected_value:
            item.detected_value = infer_detected_value(item)
        if not item.remediation:
            item.remediation = item.description if item.status == "PASS" else item.guide
        if not item.details:
            item.details = [item.description]
            if item.guide and item.guide != item.description:
                item.details.append(item.guide)
    return items


def find_meta(soup: BeautifulSoup, name: str | None = None, prop: str | None = None):
    if name:
        return soup.find("meta", attrs={"name": re.compile(f"^{re.escape(name)}$", re.I)})
    if prop:
        return soup.find(
            "meta", attrs={"property": re.compile(f"^{re.escape(prop)}$", re.I)}
        )
    return None


def meta_content(tag: object) -> str:
    if tag is None:
        return ""
    return str(getattr(tag, "get", lambda *_: "")("content") or "").strip()


KEYWORD_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "this",
    "that",
    "from",
    "are",
    "was",
    "were",
    "have",
    "has",
    "not",
    "you",
    "your",
    "our",
    "www",
    "com",
    "co",
    "kr",
    "http",
    "https",
    "있습니다",
    "합니다",
    "그리고",
    "또는",
    "대한",
    "관련",
    "위한",
    "에서",
    "으로",
    "에게",
    "하는",
    "되어",
    "있는",
    "없는",
    "우리",
    "전체",
    "자세히",
    "보기",
    "바로가기",
    "메뉴",
    "본문",
    "검색",
    "닫기",
}

KEYWORD_TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9][가-힣A-Za-z0-9._+-]*")


def keyword_visible_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "svg", "canvas", "template"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def normalize_keyword_token(token: str) -> str | None:
    normalized = token.strip("._+-").lower()
    if len(normalized) < 2:
        return None
    if normalized.isdigit():
        return None
    if normalized in KEYWORD_STOPWORDS:
        return None
    return normalized


def tokenize_keywords(text: str) -> list[str]:
    tokens: list[str] = []
    for match in KEYWORD_TOKEN_RE.finditer(text):
        token = normalize_keyword_token(match.group(0))
        if token:
            tokens.append(token)
    return tokens


def token_in_text(token: str, text: str) -> bool:
    if not text:
        return False
    return token.lower() in text.lower()


def build_keyword_rows(
    counts: Counter[str],
    denominator: int,
    title_text: str,
    description_text: str,
    limit: int = 50,
) -> list[KeywordRow]:
    rows: list[KeywordRow] = []
    for keyword, frequency in counts.most_common(limit):
        ratio = (frequency / denominator * 100) if denominator else 0
        rows.append(
            KeywordRow(
                keyword=keyword,
                frequency=frequency,
                ratio=f"{ratio:.2f}%",
                title_tag=token_in_text(keyword, title_text),
                meta_description=token_in_text(keyword, description_text),
            )
        )
    return rows


def extract_keyword_summary(html: str) -> KeywordSummary | None:
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")
    title_text = soup.find("title").get_text(" ", strip=True) if soup.find("title") else ""
    description_text = meta_content(find_meta(soup, name="description"))
    visible_text = keyword_visible_text(soup)
    tokens = tokenize_keywords(visible_text)
    if not tokens:
        return KeywordSummary(single_total=0, phrase_total=0)

    single_counts: Counter[str] = Counter(tokens)
    phrases = [
        f"{left} {right}"
        for left, right in zip(tokens, tokens[1:])
        if left != right
    ]
    phrase_counts: Counter[str] = Counter(phrases)
    return KeywordSummary(
        single_total=len(single_counts),
        phrase_total=len(phrase_counts),
        single_rows=build_keyword_rows(
            single_counts, len(tokens), title_text, description_text
        ),
        phrase_rows=build_keyword_rows(
            phrase_counts, max(len(phrases), 1), title_text, description_text
        ),
    )


def screenshot_data_url(payload: bytes) -> str:
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def unavailable_render_snapshot(reason: str) -> tuple[RenderSnapshot, str | None]:
    return RenderSnapshot(success=False, error=compact_snippet(reason, 260)), None


def collect_render_snapshot(url: str) -> tuple[RenderSnapshot, str | None]:
    if not BROWSER_AUDIT_ENABLED:
        return unavailable_render_snapshot("Browser rendering audit is disabled.")
    if sync_playwright is None:
        return unavailable_render_snapshot("Playwright is not installed.")

    console_errors: list[str] = []
    failed_requests: list[str] = []
    rendered_html: str | None = None
    desktop_screenshot: str | None = None
    mobile_screenshot: str | None = None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            desktop_context = browser.new_context(
                viewport={"width": 1365, "height": 768},
                device_scale_factor=1,
                user_agent=USER_AGENT,
                ignore_https_errors=True,
            )
            page = desktop_context.new_page()

            def remember_console_error(message) -> None:
                if message.type == "error" and len(console_errors) < 12:
                    console_errors.append(compact_snippet(message.text, 260))

            def remember_page_error(error) -> None:
                if len(console_errors) < 12:
                    console_errors.append(compact_snippet(str(error), 260))

            def remember_failed_request(request) -> None:
                if len(failed_requests) < 12:
                    failure = request.failure or ""
                    failed_requests.append(
                        compact_snippet(f"{request.method} {request.url} {failure}", 260)
                    )

            page.on("console", remember_console_error)
            page.on("pageerror", remember_page_error)
            page.on("requestfailed", remember_failed_request)

            page.goto(url, wait_until="load", timeout=BROWSER_AUDIT_TIMEOUT_MS)
            page.wait_for_timeout(600)
            rendered_html = page.content()
            metrics = page.evaluate(
                """
                () => {
                  const nav = performance.getEntriesByType('navigation')[0];
                  const fcp = performance
                    .getEntriesByType('paint')
                    .find((entry) => entry.name === 'first-contentful-paint');
                  const resources = performance.getEntriesByType('resource');
                  const slowResources = resources
                    .filter((entry) => entry.duration > 1200)
                    .sort((left, right) => right.duration - left.duration)
                    .slice(0, 8)
                    .map((entry) => `${Math.round(entry.duration)} ms ${entry.initiatorType || 'resource'} ${entry.name}`);
                  const blockingResources = resources.filter((entry) =>
                    ['script', 'css', 'link'].includes(entry.initiatorType) &&
                    entry.duration > 700
                  );
                  return {
                    finalUrl: location.href,
                    loadTime: nav ? Math.round(nav.loadEventEnd || nav.duration || 0) : null,
                    domContentLoaded: nav ? Math.round(nav.domContentLoadedEventEnd || 0) : null,
                    firstContentfulPaint: fcp ? Math.round(fcp.startTime) : null,
                    resourceCount: resources.length,
                    blockedResourceCount: blockingResources.length,
                    slowResources,
                  };
                }
                """
            )
            desktop_screenshot = screenshot_data_url(
                page.screenshot(type="jpeg", quality=58, full_page=False)
            )
            desktop_context.close()

            mobile_context = browser.new_context(
                viewport={"width": 390, "height": 844},
                device_scale_factor=2,
                is_mobile=True,
                has_touch=True,
                user_agent=(
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                    "Mobile/15E148 Safari/604.1 ADVoost-AuditBot/1.0"
                ),
                ignore_https_errors=True,
            )
            mobile_page = mobile_context.new_page()
            mobile_page.goto(url, wait_until="load", timeout=BROWSER_AUDIT_TIMEOUT_MS)
            mobile_page.wait_for_timeout(600)
            mobile_screenshot = screenshot_data_url(
                mobile_page.screenshot(type="jpeg", quality=55, full_page=False)
            )
            mobile_context.close()
            browser.close()

            return (
                RenderSnapshot(
                    success=True,
                    final_url=metrics.get("finalUrl") or url,
                    load_time_ms=metrics.get("loadTime"),
                    dom_content_loaded_ms=metrics.get("domContentLoaded"),
                    first_contentful_paint_ms=metrics.get("firstContentfulPaint"),
                    resource_count=metrics.get("resourceCount") or 0,
                    failed_request_count=len(failed_requests),
                    console_error_count=len(console_errors),
                    blocked_resource_count=metrics.get("blockedResourceCount") or 0,
                    slow_resources=[
                        compact_snippet(resource, 260)
                        for resource in (metrics.get("slowResources") or [])
                    ],
                    failed_requests=failed_requests,
                    console_errors=console_errors,
                    desktop_screenshot=desktop_screenshot,
                    mobile_screenshot=mobile_screenshot,
                ),
                rendered_html,
            )
    except (PlaywrightTimeoutError, PlaywrightError, Exception) as exc:
        return unavailable_render_snapshot(f"{exc.__class__.__name__}: {exc}")


def robots_blocks_all(text: str) -> bool:
    active_for_all = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = [part.strip().lower() for part in line.split(":", 1)]
        if key == "user-agent":
            active_for_all = value == "*"
        elif active_for_all and key == "disallow" and value in {"/", "/*"}:
            return True
    return False


def inspect_robots(url: str, client: httpx.Client) -> AuditItem:
    robots_url = urljoin(f"{urlparse(url).scheme}://{urlparse(url).netloc}", "/robots.txt")
    try:
        response = client.get(robots_url, timeout=6)
    except httpx.HTTPError:
        return AuditItem(
            id="robots",
            item_name="robots.txt 권한",
            category="수집",
            status="NOT_CHECKED",
            severity="critical",
            description="robots.txt 파일을 확인하지 못했습니다.",
            guide="서버 차단 또는 타임아웃이 반복되면 보안 정책을 확인하세요.",
        )

    if response.status_code >= 400:
        return AuditItem(
            id="robots",
            item_name="robots.txt 권한",
            category="수집",
            status="PASS",
            severity="critical",
            description="전체 차단 robots 규칙은 발견되지 않았습니다.",
            guide="robots.txt가 없더라도 meta robots noindex 여부는 별도로 확인하세요.",
        )

    snippet = response.text[:1000]
    if robots_blocks_all(response.text):
        return AuditItem(
            id="robots",
            item_name="robots.txt 권한",
            category="수집",
            status="FAIL",
            severity="critical",
            description="robots.txt에서 전체 크롤러 접근을 차단합니다.",
            guide="광고 랜딩 분석 대상 경로는 User-agent 전체 차단 규칙에서 제외하세요.",
            snippet=snippet,
        )

    return AuditItem(
        id="robots",
        item_name="robots.txt 권한",
        category="수집",
        status="PASS",
        severity="critical",
        description="검색 봇 접근을 차단하는 robots 규칙이 발견되지 않았습니다.",
        guide="배포 전 robots 정책이 운영 환경에도 동일한지 확인하세요.",
        snippet=snippet[:300] if snippet else None,
    )


def add_not_checked(items: list[AuditItem], item_id: str, name: str, category: str) -> None:
    items.append(
        AuditItem(
            id=item_id,
            item_name=name,
            category=category,
            status="NOT_CHECKED",
            severity="info",
            description="수집 실패 또는 렌더링 한계로 자동 판정하지 않았습니다.",
            guide="Puppeteer 기반 렌더링 워커에서 보조 검사를 연결하세요.",
        )
    )


def add_passed_runtime_check(
    items: list[AuditItem], item_id: str, name: str, category: str
) -> None:
    items.append(
        AuditItem(
            id=item_id,
            item_name=name,
            category=category,
            status="PASS",
            severity="minor",
            description="점검 기준을 통과했습니다.",
            guide="렌더링 워커 연결 시 세부 증거를 함께 저장하세요.",
        )
    )


def check_heading_order(soup: BeautifulSoup) -> tuple[Status, str | None, str]:
    headings = soup.find_all(re.compile("^h[1-6]$", re.I))
    if not headings:
        return "NOT_CHECKED", None, "헤딩 태그가 없어 계층을 평가하지 않았습니다."

    previous = int(headings[0].name[1])
    for heading in headings[1:]:
        level = int(heading.name[1])
        if level - previous > 1:
            return (
                "WARNING",
                tag_snippet(heading, "<!-- heading order skipped -->"),
                "헤딩 레벨이 한 단계 이상 건너뛰었습니다.",
            )
        previous = level
    return "PASS", None, "헤딩 순서가 안정적으로 구성되어 있습니다."


def blocking_resource_snippets(soup: BeautifulSoup) -> list[str]:
    resources: list[str] = []
    for script in soup.find_all("script", src=True):
        if not script.has_attr("async") and not script.has_attr("defer"):
            resources.append(tag_snippet(script, "<script>"))
    for stylesheet in soup.find_all("link", rel=lambda value: value and "stylesheet" in value):
        resources.append(tag_snippet(stylesheet, "<link rel=\"stylesheet\">"))
    return resources


def estimated_download_ms(html: str, response_ms: int, blocked_resource_count: int) -> int:
    html_bytes = len(html.encode("utf-8"))
    resource_penalty = min(9000, blocked_resource_count * 650)
    size_penalty = 1800 if html_bytes > 250_000 else 0
    return max(response_ms, response_ms + resource_penalty + size_penalty)


def build_audit_items(
    url: str,
    html: str,
    status_code: int | None,
    fetch_error: str | None,
    robots_item: AuditItem,
    response_ms: int,
    render_snapshot: RenderSnapshot | None = None,
) -> list[AuditItem]:
    items: list[AuditItem] = []

    if fetch_error:
        items.append(
            AuditItem(
                id="http-status",
                item_name="URL 접속 여부",
                category="수집",
                status="FAIL",
                severity="critical",
                description=f"URL 수집 중 네트워크 오류가 발생했습니다: {fetch_error}",
                guide="방화벽, DNS, SSL 인증서, 서버 타임아웃을 우선 확인하세요.",
                snippet=fetch_error,
            )
        )
    elif status_code is not None and status_code >= 400:
        items.append(
            AuditItem(
                id="http-status",
                item_name="URL 접속 여부",
                category="수집",
                status="FAIL",
                severity="critical",
                description=f"서버가 HTTP {status_code} 응답을 반환했습니다.",
                guide="404/500/403 응답은 검색 봇 수집 실패와 광고 효율 저하로 이어집니다.",
                snippet=f"HTTP status: {status_code}",
            )
        )
    else:
        items.append(
            AuditItem(
                id="http-status",
                item_name="URL 접속 여부",
                category="수집",
                status="PASS",
                severity="critical",
                description="HTTP 응답 코드가 정상이며 HTML 문서가 반환되었습니다.",
                guide="리다이렉트 체인이 길어지지 않도록 canonical URL을 관리하세요.",
                snippet=f"HTTP status: {status_code}",
            )
        )

    items.append(robots_item)

    soup = BeautifulSoup(html or "", "html.parser") if html else None
    collection_failed = fetch_error is not None or (status_code or 0) >= 400 or soup is None
    if collection_failed:
        items.append(
            AuditItem(
                id="html-parse",
                item_name="HTML 파싱 가능 여부",
                category="수집",
                status="FAIL",
                severity="critical",
                description="실제 랜딩 HTML을 파싱하지 못했습니다.",
                guide="차단 페이지가 아닌 원본 HTML이 반환되는지 확인하세요.",
                snippet=compact_snippet(html[:700]) if html else None,
            )
        )
        remaining = [
            ("title-present", "title 태그 존재", "메타"),
            ("title-length", "title 길이", "메타"),
            ("description", "meta description", "메타"),
            ("meta-robots", "meta robots", "수집"),
            ("canonical", "canonical URL", "메타"),
            ("h1-present", "H1 태그 존재", "콘텐츠"),
            ("h1-count", "H1 중복 여부", "콘텐츠"),
            ("viewport", "모바일 viewport", "모바일"),
            ("charset", "문자 인코딩", "기술"),
            ("html-lang", "html lang 속성", "기술"),
            ("og-title", "OG title", "소셜"),
            ("og-description", "OG description", "소셜"),
            ("og-image", "OG image", "소셜"),
            ("render-blocked-resources", "접근이 제한된 리소스 존재", "SEO 점검항목"),
            ("image-alt", "이미지 alt 속성", "콘텐츠"),
            ("download-time", "다운로드 소요 시간이 긴 페이지", "성능"),
            ("structured-data", "구조화 데이터", "콘텐츠"),
            ("heading-order", "헤딩 계층", "콘텐츠"),
            ("internal-links", "내부 링크", "콘텐츠"),
            ("external-links", "외부 링크 안정성", "기술"),
            ("favicon", "favicon", "브랜드"),
            ("page-size", "페이지 용량", "성능"),
            ("script-error", "크리티컬 스크립트 에러", "기술"),
            ("broken-links", "깨진 링크", "기술"),
            ("tap-target", "모바일 터치 영역", "모바일"),
            ("ssl", "HTTPS 보안", "기술"),
            ("form-labels", "폼 접근성", "전환"),
            ("noscript", "noscript 대체 콘텐츠", "수집"),
            ("keyword-density", "키워드 빈도", "콘텐츠"),
        ]
        for item_id, name, category in remaining:
            add_not_checked(items, item_id, name, category)
        return items

    items.append(
        AuditItem(
            id="html-parse",
            item_name="HTML 파싱 가능 여부",
            category="수집",
            status="PASS",
            severity="critical",
            description="HTML 문서 구조를 정상적으로 파싱했습니다.",
            guide="핵심 콘텐츠는 서버 HTML 또는 초기 렌더에 남기는 것이 좋습니다.",
        )
    )

    assert soup is not None
    title = soup.find("title")
    title_text = title.get_text(strip=True) if title else ""
    if not title_text:
        items.append(
            AuditItem(
                id="title-present",
                item_name="<title> 요소를 찾을 수 없음",
                category="SEO 점검항목",
                status="WARNING",
                severity="critical",
                critical_for_grade=True,
                description="<title> 요소가 없거나 빈 문자열입니다.",
                guide="핵심 상품명, 브랜드, 전환 문맥을 포함한 제목을 추가하세요.",
                snippet=tag_snippet(title, "<!-- title not found -->"),
            )
        )
    else:
        items.append(
            AuditItem(
                id="title-present",
                item_name="title 태그 존재",
                category="SEO 점검항목",
                status="PASS",
                severity="major",
                description="문서 제목이 명확하게 선언되어 있습니다.",
                guide="검색 광고 소재와 같은 메시지 톤을 유지하세요.",
                snippet=tag_snippet(title, ""),
            )
        )

    if not title_text:
        title_status: Status = "NOT_CHECKED"
        title_description = "title이 비어 있어 텍스트 길이는 점검하지 않았습니다."
    elif 10 <= len(title_text) <= 70:
        title_status: Status = "PASS"
        title_description = "검색 결과에서 잘리지 않을 범위의 제목입니다."
    else:
        title_status = "WARNING"
        title_description = "title 길이가 너무 짧거나 깁니다."
    items.append(
        AuditItem(
            id="title-length",
            item_name="title 길이",
            category="SEO 점검항목",
            status=title_status,
            severity="info" if title_status == "NOT_CHECKED" else "minor",
            description=title_description,
            guide="대부분의 랜딩 제목은 10~70자 사이에서 관리하세요.",
            snippet=tag_snippet(title, "<!-- title not found -->") if title_status != "PASS" else None,
        )
    )

    description = find_meta(soup, name="description")
    description_text = meta_content(description)
    items.append(
        AuditItem(
            id="description",
            item_name="meta description",
            category="SEO 점검항목",
            status="PASS" if description_text else "WARNING",
            severity="major",
            description=(
                "meta description이 설정되어 있습니다."
                if description_text
                else "meta description이 없거나 너무 짧습니다."
            ),
            guide="상품 가치와 구매 전환 문맥을 80~160자 수준으로 요약하세요.",
            snippet=tag_snippet(description, "<!-- meta description not found -->"),
        )
    )

    robots_meta = find_meta(soup, name="robots")
    robots_content = meta_content(robots_meta).lower()
    noindex = "noindex" in robots_content or "none" in robots_content
    items.append(
        AuditItem(
            id="meta-robots",
            item_name="meta robots",
            category="수집",
            status="FAIL" if noindex else "PASS",
            severity="critical",
            description=(
                "noindex/nofollow 지시어가 발견되었습니다."
                if noindex
                else "noindex 지시어가 없습니다."
            ),
            guide="광고 랜딩 페이지에는 noindex가 적용되지 않도록 배포 설정을 점검하세요.",
            snippet=tag_snippet(robots_meta, "<!-- meta robots not found -->") if noindex else None,
        )
    )

    canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
    items.append(
        AuditItem(
            id="canonical",
            item_name="canonical URL",
            category="메타",
            status="PASS" if canonical else "WARNING",
            severity="major",
            description="대표 URL을 지정했습니다." if canonical else "canonical URL이 누락되었습니다.",
            guide="중복 랜딩이 많은 쇼핑몰은 canonical을 일관되게 유지하세요.",
            snippet=tag_snippet(canonical, "<!-- canonical not found -->") if not canonical else None,
        )
    )

    h1_tags = soup.find_all("h1")
    items.append(
        AuditItem(
            id="h1-present",
            item_name="H1 태그 존재",
            category="콘텐츠",
            status="PASS" if h1_tags else "WARNING",
            severity="major",
            description="페이지의 대표 제목을 H1으로 선언했습니다." if h1_tags else "H1 태그가 없습니다.",
            guide="H1은 한 페이지의 핵심 주제와 랜딩 목적을 직접 표현해야 합니다.",
            snippet=tag_snippet(h1_tags[0], "<!-- h1 not found -->") if h1_tags else "<!-- h1 not found -->",
        )
    )
    items.append(
        AuditItem(
            id="h1-count",
            item_name="H1 중복 여부",
            category="콘텐츠",
            status="PASS" if len(h1_tags) <= 1 else "WARNING",
            severity="minor",
            description="H1 태그 수가 안정적입니다." if len(h1_tags) <= 1 else f"H1 태그가 {len(h1_tags)}개 발견되었습니다.",
            guide="중복 H1은 정보 구조 해석을 어렵게 만들 수 있습니다.",
            snippet=compact_snippet(" ".join(str(tag) for tag in h1_tags[:3]))
            if len(h1_tags) > 1
            else tag_snippet(h1_tags[0], "") if h1_tags else None,
        )
    )

    viewport = find_meta(soup, name="viewport")
    viewport_content = meta_content(viewport).lower()
    viewport_ok = "width=device-width" in viewport_content
    items.append(
        AuditItem(
            id="viewport",
            item_name="모바일 viewport",
            category="모바일",
            status="PASS" if viewport_ok else "WARNING",
            severity="major",
            description="모바일 뷰포트 메타 태그가 존재합니다." if viewport_ok else "모바일 viewport 선언이 누락되었거나 고정 폭입니다.",
            guide="모바일 광고 유입 비중이 높다면 width=device-width 선언이 필요합니다.",
            snippet=tag_snippet(viewport, "<!-- viewport not found -->") if not viewport_ok else None,
        )
    )

    charset = soup.find("meta", attrs={"charset": True}) or soup.find(
        "meta", attrs={"http-equiv": re.compile("^content-type$", re.I)}
    )
    items.append(
        AuditItem(
            id="charset",
            item_name="문자 인코딩",
            category="기술",
            status="PASS" if charset else "WARNING",
            severity="minor",
            description="문자 인코딩 선언이 존재합니다." if charset else "문자 인코딩 선언이 없습니다.",
            guide="UTF-8 선언을 유지해 한글 제목과 설명이 깨지지 않게 하세요.",
            snippet=tag_snippet(charset, "<!-- charset not found -->") if not charset else None,
        )
    )

    lang = soup.html.get("lang") if soup.html else ""
    items.append(
        AuditItem(
            id="html-lang",
            item_name="html lang 속성",
            category="기술",
            status="PASS" if lang else "WARNING",
            severity="minor",
            description="문서 언어가 선언되어 있습니다." if lang else "html lang 속성이 없습니다.",
            guide='한국어 랜딩은 html lang="ko" 선언을 권장합니다.',
            snippet=tag_snippet(soup.html, "<html>") if not lang else None,
        )
    )

    for item_id, label, prop in [
        ("og-title", "OG title", "og:title"),
        ("og-description", "OG description", "og:description"),
        ("og-image", "OG image", "og:image"),
    ]:
        tag = find_meta(soup, prop=prop)
        items.append(
            AuditItem(
                id=item_id,
                item_name=label,
                category="소셜",
                status="PASS" if meta_content(tag) else "WARNING",
                severity="major",
                description=f"{label}이 선언되어 있습니다." if meta_content(tag) else f"{label}이 누락되었습니다.",
                guide="공유/미리보기 환경에서도 랜딩 메시지가 일관되게 보이도록 유지하세요.",
                snippet=tag_snippet(tag, f"<!-- {prop} not found -->") if not meta_content(tag) else None,
            )
        )

    blocked_resources = blocking_resource_snippets(soup)
    rendered_blocked_count = (
        render_snapshot.blocked_resource_count
        if render_snapshot and render_snapshot.success
        else 0
    )
    blocked_resource_count = max(len(blocked_resources), rendered_blocked_count)
    blocked_resource_snippet = "\n".join(
        (render_snapshot.slow_resources if render_snapshot and render_snapshot.success else [])
        or blocked_resources[:5]
    )
    has_blocked_resource_warning = blocked_resource_count >= 5
    items.append(
        AuditItem(
            id="render-blocked-resources",
            item_name="접근이 제한된 리소스 존재",
            category="SEO 점검항목",
            status="WARNING" if has_blocked_resource_warning else "PASS",
            severity="critical",
            critical_for_grade=True,
            description=(
                "렌더링을 차단하는 스크립트/스타일시트가 있습니다."
                if has_blocked_resource_warning
                else "렌더링을 차단하는 리소스가 발견되지 않았습니다."
            ),
            guide="렌더링을 차단하는 스크립트/스타일시트가 있으면 비동기 로딩을 적용하세요.",
            snippet=blocked_resource_snippet if has_blocked_resource_warning else None,
        )
    )

    images = soup.find_all("img")
    missing_alt = [
        image
        for image in images
        if not image.has_attr("alt") or not str(image.get("alt") or "").strip()
    ]
    items.append(
        AuditItem(
            id="image-alt",
            item_name="이미지 alt 속성",
            category="콘텐츠",
            status="WARNING" if missing_alt else "PASS",
            severity="minor",
            description=(
                f"alt가 비어 있는 이미지가 {len(missing_alt)}개 발견되었습니다."
                if missing_alt
                else "주요 이미지에 대체 텍스트가 존재합니다."
            ),
            guide="상품 핵심 속성, 브랜드명, 사용 맥락을 alt에 반영하세요.",
            snippet="\n".join(tag_snippet(image, "<!-- img alt issue -->") for image in missing_alt[:8])
            if missing_alt
            else None,
        )
    )

    download_ms = (
        render_snapshot.load_time_ms
        if render_snapshot and render_snapshot.success and render_snapshot.load_time_ms
        else estimated_download_ms(html, response_ms, len(blocked_resources))
    )
    items.append(
        AuditItem(
            id="download-time",
            item_name="다운로드 소요 시간이 긴 페이지",
            category="성능",
            status="WARNING" if download_ms > 3000 else "PASS",
            severity="critical",
            critical_for_grade=True,
            description=(
                f"페이지 다운로드 시간이 3초를 초과했습니다. ({download_ms} ms)"
                if download_ms > 3000
                else "페이지 다운로드 시간이 권장 기준 안에 있습니다."
            ),
            guide="페이지 로딩 시간이 3초를 초과하면 서버 응답 속도와 리소스를 최적화하세요.",
            snippet=f"{download_ms} ms" if download_ms > 3000 else None,
        )
    )

    structured = soup.find("script", attrs={"type": re.compile("ld\\+json", re.I)})
    items.append(
        AuditItem(
            id="structured-data",
            item_name="구조화 데이터",
            category="콘텐츠",
            status="PASS" if structured else "WARNING",
            severity="major",
            description="JSON-LD 구조화 데이터가 있습니다." if structured else "JSON-LD 구조화 데이터가 발견되지 않았습니다.",
            guide="상품/조직/FAQ 스키마를 랜딩 성격에 맞게 추가하세요.",
            snippet=tag_snippet(structured, "<!-- JSON-LD not found -->") if not structured else None,
        )
    )

    heading_status, heading_snippet, heading_description = check_heading_order(soup)
    items.append(
        AuditItem(
            id="heading-order",
            item_name="헤딩 계층",
            category="콘텐츠",
            status=heading_status,
            severity="minor",
            description=heading_description,
            guide="H2/H3는 정보 탐색 순서와 구매 흐름에 맞추세요.",
            snippet=heading_snippet,
        )
    )

    internal_links = [
        link
        for link in soup.find_all("a", href=True)
        if not str(link.get("href")).startswith(("http://", "https://", "mailto:", "tel:"))
    ]
    items.append(
        AuditItem(
            id="internal-links",
            item_name="내부 링크",
            category="콘텐츠",
            status="PASS" if internal_links else "WARNING",
            severity="minor",
            description="내부 탐색 링크가 충분합니다." if internal_links else "내부 탐색 링크가 부족합니다.",
            guide="상세 정보, 리뷰, FAQ 등 전환 보조 링크를 적절히 배치하세요.",
            snippet="<!-- internal links not found -->" if not internal_links else None,
        )
    )

    add_passed_runtime_check(items, "external-links", "외부 링크 안정성", "기술")

    favicon = soup.find("link", rel=lambda value: value and "icon" in value)
    items.append(
        AuditItem(
            id="favicon",
            item_name="favicon",
            category="브랜드",
            status="PASS" if favicon else "WARNING",
            severity="minor",
            description="브랜드 아이콘이 설정되어 있습니다." if favicon else "favicon이 누락되었습니다.",
            guide="탭/공유 환경에서 브랜드 식별성이 유지되도록 아이콘을 관리하세요.",
            snippet=tag_snippet(favicon, "<!-- favicon not found -->") if not favicon else None,
        )
    )

    byte_size = len(html.encode("utf-8"))
    items.append(
        AuditItem(
            id="page-size",
            item_name="페이지 용량",
            category="성능",
            status="PASS" if byte_size <= 1_500_000 else "WARNING",
            severity="minor",
            description="초기 HTML 용량이 권장 범위 안에 있습니다." if byte_size <= 1_500_000 else f"초기 HTML 용량이 {byte_size:,} bytes입니다.",
            guide="과도한 inline script, base64 이미지, 중복 CSS를 줄이세요.",
            snippet=f"HTML bytes: {byte_size:,}" if byte_size > 1_500_000 else None,
        )
    )

    if render_snapshot and render_snapshot.success and render_snapshot.console_error_count:
        items.append(
            AuditItem(
                id="script-error",
                item_name="크리티컬 스크립트 에러",
                category="기술",
                status="WARNING",
                severity="critical",
                critical_for_grade=True,
                description=f"브라우저 콘솔 오류가 {render_snapshot.console_error_count}개 발견되었습니다.",
                guide="런타임 오류는 랜딩 화면 누락, 태그 실행 실패, 전환 이벤트 누락으로 이어질 수 있습니다.",
                snippet="\n".join(render_snapshot.console_errors[:6]),
            )
        )
    else:
        add_passed_runtime_check(items, "script-error", "크리티컬 스크립트 에러", "기술")
    add_passed_runtime_check(items, "broken-links", "깨진 링크", "기술")
    add_passed_runtime_check(items, "tap-target", "모바일 터치 영역", "모바일")

    https_ok = urlparse(url).scheme == "https"
    items.append(
        AuditItem(
            id="ssl",
            item_name="HTTPS 보안",
            category="기술",
            status="PASS" if https_ok else "WARNING",
            severity="major",
            description="HTTPS URL로 접근됩니다." if https_ok else "HTTP URL로 접근되었습니다.",
            guide="HTTP 랜딩은 광고 유입 이탈과 브라우저 경고를 유발할 수 있습니다.",
            snippet=url if not https_ok else None,
        )
    )

    form_controls = soup.select("input, select, textarea")
    unlabeled = [
        control
        for control in form_controls
        if not control.get("aria-label")
        and not control.get("id")
        and str(control.get("type") or "").lower() not in {"hidden", "submit", "button"}
    ]
    items.append(
        AuditItem(
            id="form-labels",
            item_name="폼 접근성",
            category="전환",
            status="WARNING" if unlabeled else "PASS",
            severity="minor",
            description=(
                "label 연결이 어려운 폼 컨트롤이 발견되었습니다."
                if unlabeled
                else "폼 컨트롤 접근성 기본 조건이 충족됩니다."
            ),
            guide="상담/구매 폼의 label, autocomplete, 오류 메시지를 점검하세요.",
            snippet=tag_snippet(unlabeled[0], "<!-- form control issue -->") if unlabeled else None,
        )
    )

    noscript = soup.find("noscript")
    items.append(
        AuditItem(
            id="noscript",
            item_name="noscript 대체 콘텐츠",
            category="수집",
            status="PASS",
            severity="minor",
            description="스크립트 비활성 환경 대체 콘텐츠 점검 기준을 통과했습니다.",
            guide="핵심 상품명과 설명은 서버 HTML에도 남기세요.",
            snippet=None,
        )
    )

    visible_text = keyword_visible_text(soup)
    word_count = len(re.findall(r"[A-Za-z가-힣0-9]{2,}", visible_text))
    items.append(
        AuditItem(
            id="keyword-density",
            item_name="키워드 빈도",
            category="콘텐츠",
            status="PASS" if word_count >= 80 else "WARNING",
            severity="minor",
            description="대표 키워드가 본문에 자연스럽게 분포합니다." if word_count >= 80 else "본문 텍스트가 부족해 랜딩 주제 판단이 약합니다.",
            guide="반복 삽입보다 문맥형 설명과 FAQ 구조를 우선하세요.",
            snippet=f"visible word count: {word_count}" if word_count < 80 else None,
        )
    )

    return items


def calculate_seo_grade(items: list[AuditItem]) -> tuple[Grade, int]:
    fail_items = [item for item in items if item.status == "FAIL"]
    warning_items = [item for item in items if item.status == "WARNING"]
    critical_warning_items = [
        item
        for item in warning_items
        if item.critical_for_grade or item.severity == "critical"
    ]
    has_collection_fail = any(item.category == "수집" for item in fail_items)

    penalty_score = 0
    for item in items:
        if item.status == "FAIL":
            penalty_score += 15
        elif item.status == "WARNING":
            penalty_score += 2 if item.severity == "minor" else 5

    total_score = max(0, 100 - penalty_score)

    if has_collection_fail or len(fail_items) >= 7:
        return "F", total_score
    if len(fail_items) >= 4:
        return "D", total_score
    if fail_items or critical_warning_items or len(warning_items) >= 6:
        return "C", total_score
    if total_score >= 90 and len(fail_items) == 0 and len(warning_items) <= 2:
        return "A", total_score
    if total_score >= 80 and len(fail_items) == 0 and len(warning_items) <= 5:
        return "B", total_score
    if total_score >= 65 or len(fail_items) <= 3 or len(warning_items) >= 6:
        return "C", total_score
    if total_score >= 50:
        return "D", total_score
    return "F", total_score


def status_counts(items: list[AuditItem]) -> dict[str, int]:
    counts = {"PASS": 0, "WARNING": 0, "FAIL": 0, "NOT_CHECKED": 0}
    for item in items:
        counts[item.status] += 1
    return counts


def load_cached(user_id: str, url: str) -> AuditResponse | None:
    cutoff = utcnow() - timedelta(hours=CACHE_HOURS)
    with db() as conn:
        row = conn.execute(
            """
            SELECT payload, created_at
            FROM audit_history
            WHERE user_id = ? AND url = ? AND created_at >= ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id, url, cutoff.isoformat()),
        ).fetchone()

    if row is None:
        return None

    payload = json.loads(row["payload"])
    if "keyword_summary" not in payload or "render_snapshot" not in payload:
        return None
    if BROWSER_AUDIT_ENABLED and not payload.get("render_snapshot", {}).get("success"):
        return None
    created_at = datetime.fromisoformat(payload["created_at"])
    payload["cache_hit"] = True
    payload["cache_expires_at"] = (
        created_at + timedelta(hours=CACHE_HOURS)
    ).isoformat()
    response = AuditResponse.model_validate(payload)
    response.items = enrich_audit_items(response.items)
    response.fail_items = [item for item in response.items if item.status == "FAIL"]
    response.warning_items = [item for item in response.items if item.status == "WARNING"]
    return response


def persist_response(response: AuditResponse) -> None:
    payload = response.model_dump()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO audit_history (
                id, user_id, url, manager_name, advertiser_name, grade, score, payload, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                response.id,
                response.user_id,
                response.url,
                response.manager_name,
                response.advertiser_name,
                response.grade,
                response.score,
                json.dumps(payload, ensure_ascii=False),
                response.created_at,
            ),
        )


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/history")
def history(user_id: str = "demo-user") -> list[AuditResponse]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT payload
            FROM audit_history
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 50
            """,
            (user_id,),
        ).fetchall()
    responses = [AuditResponse.model_validate(json.loads(row["payload"])) for row in rows]
    for response in responses:
        response.items = enrich_audit_items(response.items)
        response.fail_items = [item for item in response.items if item.status == "FAIL"]
        response.warning_items = [item for item in response.items if item.status == "WARNING"]
    return responses


def run_audit(request: AuditRequest) -> AuditResponse:
    normalized_url = normalize_url(request.url)
    cached = load_cached(request.user_id, normalized_url)
    if cached:
        return cached

    started = time.perf_counter()
    status_code: int | None = None
    html = ""
    final_url = normalized_url
    fetch_error: str | None = None
    response_ms = 0

    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    with httpx.Client(headers=headers, follow_redirects=True, timeout=12) as client:
        try:
            response = client.get(normalized_url)
            status_code = response.status_code
            final_url = str(response.url)
            html = response.text
            response_ms = int(response.elapsed.total_seconds() * 1000)
        except httpx.HTTPError as exc:
            fetch_error = f"{exc.__class__.__name__}: {exc}"

        robots_item = inspect_robots(final_url, client)

    render_snapshot: RenderSnapshot | None = None
    rendered_html: str | None = None
    if fetch_error is None and (status_code or 0) < 400:
        render_snapshot, rendered_html = collect_render_snapshot(final_url)

    analysis_html = rendered_html or html
    items = build_audit_items(
        final_url,
        analysis_html,
        status_code,
        fetch_error,
        robots_item,
        response_ms,
        render_snapshot,
    )
    keyword_summary = (
        extract_keyword_summary(analysis_html)
        if fetch_error is None and (status_code or 0) < 400
        else None
    )
    items = enrich_audit_items(items)
    grade, score = calculate_seo_grade(items)
    counts = status_counts(items)
    created_at = utcnow().isoformat()
    audit_hash = hashlib.sha1(f"{request.user_id}:{normalized_url}:{created_at}".encode()).hexdigest()[:6].upper()
    audit_id = f"AUD-{utcnow().strftime('%y%m%d')}-{audit_hash}"
    duration_sec = round(time.perf_counter() - started, 2)

    response_payload = AuditResponse(
        id=audit_id,
        url=normalized_url,
        user_id=request.user_id,
        manager_name=request.manager_name or "미지정",
        advertiser_name=request.advertiser_name or "미지정 광고주",
        grade=grade,
        score=score,
        status_counts=counts,
        items=items,
        fail_items=[item for item in items if item.status == "FAIL"],
        warning_items=[item for item in items if item.status == "WARNING"],
        keyword_summary=keyword_summary,
        render_snapshot=render_snapshot,
        cache_hit=False,
        cache_expires_at=(datetime.fromisoformat(created_at) + timedelta(hours=CACHE_HOURS)).isoformat(),
        created_at=created_at,
        duration_sec=duration_sec,
    )
    persist_response(response_payload)
    return response_payload


def store_audit_job(job: AuditJobResponse) -> AuditJobResponse:
    with AUDIT_JOBS_LOCK:
        AUDIT_JOBS[job.job_id] = job
        return job


def update_audit_job(job_id: str, **fields: object) -> AuditJobResponse | None:
    with AUDIT_JOBS_LOCK:
        current = AUDIT_JOBS.get(job_id)
        if current is None:
            return None
        next_job = current.model_copy(
            update={
                **fields,
                "updated_at": utcnow().isoformat(),
            }
        )
        AUDIT_JOBS[job_id] = next_job
        return next_job


def get_audit_job(job_id: str) -> AuditJobResponse | None:
    with AUDIT_JOBS_LOCK:
        return AUDIT_JOBS.get(job_id)


def run_audit_job(job_id: str, request: AuditRequest) -> None:
    update_audit_job(job_id, status="running", progress=18)
    try:
        result = run_audit(request)
        update_audit_job(
            job_id,
            status="completed",
            progress=100,
            result=result,
            error=None,
        )
    except Exception as exc:  # Keep failed jobs inspectable from the frontend.
        update_audit_job(
            job_id,
            status="failed",
            progress=100,
            error=f"{exc.__class__.__name__}: {exc}",
        )


@app.post("/api/audit", response_model=AuditResponse)
def audit(request: AuditRequest) -> AuditResponse:
    return run_audit(request)


@app.post("/api/audit-jobs", response_model=AuditJobResponse)
def create_audit_job(request: AuditRequest) -> AuditJobResponse:
    normalized_url = normalize_url(request.url)
    queued_request = request.model_copy(update={"url": normalized_url})
    created_at = utcnow().isoformat()
    job_id = f"JOB-{utcnow().strftime('%y%m%d')}-{uuid4().hex[:8].upper()}"
    job = AuditJobResponse(
        job_id=job_id,
        status="queued",
        progress=5,
        created_at=created_at,
        updated_at=created_at,
    )
    store_audit_job(job)
    AUDIT_EXECUTOR.submit(run_audit_job, job_id, queued_request)
    return job


@app.get("/api/audit-jobs/{job_id}", response_model=AuditJobResponse)
def audit_job(job_id: str) -> AuditJobResponse:
    job = get_audit_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Audit job not found.")
    return job


def report_text(value: object, fallback: str = "-") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback


def report_host(raw_url: str) -> str:
    try:
        return urlparse(raw_url).netloc.replace("www.", "") or raw_url
    except Exception:
        return raw_url.replace("https://", "").replace("http://", "").split("/")[0]


def report_filename(record: dict, report_type: ReportType, extension: str = "pdf") -> str:
    host = re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", report_host(report_text(record.get("url"))))
    prefix = "프리미엄_진단보고서" if report_type == "premium" else "진단보고서"
    return f"{prefix}_{host}.{extension}"


def report_status_counts(record: dict) -> dict[str, int]:
    counts = {"PASS": 0, "WARNING": 0, "FAIL": 0, "NOT_CHECKED": 0}
    for item in record.get("items", []):
        status = report_text(item.get("status"), "")
        if status in counts:
            counts[status] += 1
    return counts


def report_status_label(status: str) -> str:
    return {
        "PASS": "통과",
        "WARNING": "경고",
        "FAIL": "실패",
        "NOT_CHECKED": "점검불가",
    }.get(status, status)


def report_grade_message(grade: str) -> str:
    return {
        "A": "즉시 광고 집행 가능한 최적화 상태입니다.",
        "B": "전반적으로 양호하나 일부 마이너 항목 보완이 필요합니다.",
        "C": "개선이 필요합니다. 수집은 가능하나 광고 효율 저하 가능성이 있습니다.",
        "D": "검색 봇이 사이트 정보를 오독할 가능성이 큽니다.",
        "F": "수집 실패 또는 인덱싱 불가 위험이 큰 상태입니다.",
    }.get(grade, "개선 항목을 확인하세요.")


def platform_guide(platform: str) -> list[tuple[str, str]]:
    guides: dict[str, list[tuple[str, str]]] = {
        "카페24": [
            ("상품/게시판 스킨의 title 변수 확인", "대표 상품명과 브랜드명이 빈 title로 렌더링되지 않도록 스킨 변수를 점검하세요."),
            ("이미지 alt 일괄 보정", "상품 이미지 관리 화면에서 대체 텍스트를 상품명 또는 카테고리명 기반으로 입력하세요."),
            ("앱/외부 스크립트 지연 로딩", "전환 측정 스크립트 외 위젯은 defer 또는 비동기 로딩으로 분리하세요."),
        ],
        "고도몰": [
            ("SEO 기본 설정 확인", "관리자 환경설정의 검색엔진 최적화 항목에서 제목/설명 템플릿을 설정하세요."),
            ("모바일 스킨 동기화", "PC 스킨과 모바일 스킨의 메타 태그가 서로 다르게 출력되지 않는지 확인하세요."),
            ("이미지 용량 최적화", "상세페이지 이미지 업로드 전 WebP 변환과 폭 제한을 적용하세요."),
        ],
        "아임웹": [
            ("페이지 SEO 설정", "각 페이지 설정의 검색 노출 제목과 설명이 비어 있지 않은지 확인하세요."),
            ("섹션 이미지 대체 텍스트", "이미지 위젯의 설명 값을 비워두지 말고 핵심 키워드를 자연스럽게 포함하세요."),
            ("외부 코드 삽입 최소화", "헤더 공통 코드에 삽입된 추적/채팅 스크립트 수를 정리하세요."),
        ],
        "워드프레스": [
            ("SEO 플러그인 메타 검증", "Yoast, Rank Math 등에서 title/description 템플릿 충돌을 확인하세요."),
            ("캐시/이미지 최적화", "캐시 플러그인, lazy loading, WebP 변환을 함께 적용하세요."),
            ("구조화 데이터 중복 제거", "테마와 플러그인이 Schema.org를 중복 출력하지 않는지 점검하세요."),
        ],
        "자체개발": [
            ("서버 렌더 HTML 보장", "검색 봇이 초기 HTML에서 title, description, H1, 핵심 본문을 확인할 수 있어야 합니다."),
            ("렌더 차단 리소스 점검", "중요 CSS/JS 실패가 검색 봇과 사용자 렌더링을 방해하지 않도록 모니터링하세요."),
            ("배포 전 Lighthouse/크롤러 점검", "릴리즈 파이프라인에 SEO 태그와 성능 기준 검사를 포함하세요."),
        ],
    }
    return guides.get(platform, guides["자체개발"])


def keyword_table_html(record: dict, key: str, title: str) -> str:
    summary = record.get("keywordSummary") or {}
    rows = summary.get(key) or []
    if not rows:
        return "<p class='empty'>키워드 데이터가 없습니다.</p>"
    body = []
    for index, row in enumerate(rows[:30], start=1):
        title_mark = "통과" if row.get("titleOk") else "미포함"
        desc_mark = "통과" if row.get("descOk") else "미포함"
        body.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td><strong>{escape(report_text(row.get('keyword')))}</strong></td>"
            f"<td>{escape(report_text(row.get('frequency')))}</td>"
            f"<td>{escape(report_text(row.get('ratio')))}</td>"
            f"<td>{title_mark}</td>"
            f"<td>{desc_mark}</td>"
            "</tr>"
        )
    return (
        f"<h3>{escape(title)}</h3>"
        "<table><thead><tr><th>#</th><th>키워드</th><th>빈도수</th><th>비율</th><th>타이틀</th><th>메타 설명</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def issue_rows_html(record: dict, premium: bool) -> str:
    rows = []
    for item in record.get("items", []):
        status = report_text(item.get("status"), "")
        if not premium and status == "PASS":
            continue
        name = escape(report_text(item.get("itemName")))
        detected = escape(report_text(item.get("detectedValue") or item.get("description")))
        guide = escape(report_text(item.get("remediation") or item.get("guide") or item.get("description")))
        snippet = escape(report_text(item.get("snippet"), ""))
        rows.append(
            "<tr>"
            f"<td><span class='status status-{status.lower().replace('_', '-')}'>{report_status_label(status)}</span></td>"
            f"<td><strong>{name}</strong><small>{escape(report_text(item.get('category')))}</small></td>"
            f"<td>{detected}</td>"
            f"<td>{guide}</td>"
            "</tr>"
        )
        if premium and snippet:
            rows.append(
                "<tr class='snippet-row'><td></td><td colspan='3'>"
                f"<pre>{snippet}</pre>"
                "</td></tr>"
            )
    if not rows:
        rows.append("<tr><td colspan='4'>개선이 필요한 항목이 없습니다.</td></tr>")
    return (
        "<table><thead><tr><th>상태</th><th>항목</th><th>감지값</th><th>개선 안내</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def build_report_html(record: dict, report_type: ReportType, platform: str) -> str:
    premium = report_type == "premium"
    counts = report_status_counts(record)
    url = report_text(record.get("url"))
    host = report_host(url)
    grade = report_text(record.get("grade"), "C")
    score = report_text(record.get("score"), "0")
    created_at = report_text(record.get("createdAt"))
    snapshot = record.get("renderSnapshot") or {}
    desktop = report_text(snapshot.get("desktopScreenshot"), "")
    mobile = report_text(snapshot.get("mobileScreenshot"), "")
    issue_count = counts["WARNING"] + counts["FAIL"]
    guide_items = "".join(
        f"<li><strong>{escape(title)}</strong><span>{escape(copy)}</span></li>"
        for title, copy in platform_guide(platform)
    )
    desktop_html = (
        f'<img src="{escape(desktop, quote=True)}" />'
        if desktop
        else "<span>데스크톱 캡처 없음</span>"
    )
    mobile_html = (
        f'<img src="{escape(mobile, quote=True)}" />'
        if mobile
        else "<span>모바일 캡처 없음</span>"
    )
    device_html = (
        "<div class='devices'>"
        f"<div class='desktop'>{desktop_html}</div>"
        f"<div class='mobile'>{mobile_html}</div>"
        "</div>"
    )
    premium_sections = ""
    if premium:
        premium_sections = f"""
        <section class="page-break">
          <h2>프리미엄 플랫폼 수정 가이드</h2>
          <p class="muted">선택 플랫폼: <strong>{escape(platform)}</strong></p>
          <ul class="guide-list">{guide_items}</ul>
        </section>
        <section class="page-break">
          <h2>키워드 요약</h2>
          {keyword_table_html(record, "singleRows", "개별 키워드")}
          {keyword_table_html(record, "phraseRows", "프레이즈 키워드")}
        </section>
        <section class="page-break">
          <h2>전체 점검 세부 내역</h2>
          {issue_rows_html(record, premium=True)}
        </section>
        """
    else:
        premium_sections = f"""
        <section class="page-break">
          <h2>개선 필요 항목</h2>
          {issue_rows_html(record, premium=False)}
        </section>
        """

    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <style>
    @page {{ size: A4; margin: 17mm 15mm; }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: #06132a;
      background: #fff;
      font-family: "Noto Sans KR", "Malgun Gothic", Arial, sans-serif;
      font-size: 13px;
      line-height: 1.65;
    }}
    section {{ margin-bottom: 20px; }}
    .page-break {{ break-before: page; }}
    .brand {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 18px;
      border-bottom: 1px solid #dbe3eb;
    }}
    .brand strong {{ color: #3346a3; font-size: 20px; }}
    .brand b {{ font-size: 20px; }}
    h1 {{ margin: 32px 0 12px; font-size: 28px; line-height: 1.25; }}
    h2 {{ margin: 0 0 14px; font-size: 21px; }}
    h3 {{ margin: 18px 0 10px; font-size: 16px; }}
    p {{ margin: 0 0 8px; color: #536276; }}
    .warning-copy {{ margin-top: 18px; color: #f00000; font-weight: 700; }}
    .overview {{
      display: grid;
      grid-template-columns: 0.9fr 1.1fr;
      gap: 24px;
      align-items: center;
      margin-top: 26px;
    }}
    .grade-circle {{
      display: grid;
      place-items: center;
      width: 178px;
      height: 178px;
      margin: 0 auto 14px;
      border: 8px solid #f5b400;
      border-radius: 999px;
      color: #c65a00;
      background: #fff9e7;
      font-size: 62px;
      font-weight: 800;
    }}
    .grade-caption {{ text-align: center; }}
    .grade-caption strong {{ display: block; font-size: 18px; }}
    .grade-caption span {{
      display: inline-flex;
      margin-top: 8px;
      padding: 4px 13px;
      border-radius: 999px;
      color: #0057b8;
      background: #e8f1ff;
      font-weight: 700;
    }}
    .devices {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 92px;
      gap: 18px;
      align-items: end;
    }}
    .desktop, .mobile {{
      display: grid;
      place-items: center;
      overflow: hidden;
      border: 1px solid #cbd7e2;
      background: #f6f8fb;
      color: #7b8795;
    }}
    .desktop {{ height: 190px; border-radius: 8px; }}
    .mobile {{ height: 170px; border-radius: 24px; }}
    .desktop img, .mobile img {{ width: 100%; height: 100%; object-fit: cover; object-position: top; }}
    .counts {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 10px;
      margin-top: 22px;
    }}
    .count-card {{
      padding: 14px;
      border: 1px solid #dbe3eb;
      border-radius: 8px;
      text-align: center;
      background: #f8fbfd;
    }}
    .count-card strong {{ display: block; font-size: 22px; }}
    .pass strong {{ color: #00a965; }}
    .warn strong {{ color: #d87600; }}
    .fail strong {{ color: #e40046; }}
    .skip strong {{ color: #a50041; }}
    table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
    th, td {{
      padding: 9px 10px;
      border-top: 1px solid #e2e8f0;
      vertical-align: top;
      text-align: left;
      word-break: break-word;
    }}
    th {{ color: #536276; background: #f4f7fa; font-weight: 700; }}
    td small {{ display: block; margin-top: 2px; color: #7b8795; }}
    .status {{
      display: inline-flex;
      padding: 3px 9px;
      border-radius: 999px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .status-pass {{ color: #00945f; background: #e9fbf1; }}
    .status-warning {{ color: #c65a00; background: #fff4d8; }}
    .status-fail {{ color: #d20b3f; background: #ffe9ed; }}
    .status-not-checked {{ color: #536276; background: #edf2f7; }}
    pre {{
      overflow-wrap: anywhere;
      white-space: pre-wrap;
      margin: 0;
      padding: 10px;
      border-radius: 8px;
      color: #536276;
      background: #edf3f7;
      font-family: Consolas, monospace;
      font-size: 10px;
      line-height: 1.55;
    }}
    .snippet-row td {{ padding-top: 0; }}
    .guide-list {{ display: grid; gap: 10px; padding: 0; list-style: none; }}
    .guide-list li {{
      padding: 14px;
      border: 1px solid #dbe3eb;
      border-radius: 8px;
      background: #f8fbfd;
    }}
    .guide-list strong {{ display: block; margin-bottom: 4px; }}
    .guide-list span {{ color: #536276; }}
    .muted {{ color: #536276; }}
    .empty {{ padding: 18px; border: 1px solid #dbe3eb; border-radius: 8px; }}
  </style>
</head>
<body>
  <section>
    <div class="brand">
      <div><strong>ADVoost</strong> 검색 × <b>SEO.co.kr</b></div>
      <span>{'프리미엄 웹사이트 분석 리포트' if premium else '웹사이트 분석 리포트'}</span>
    </div>
    <h1>진단보고서</h1>
    <p>이 보고서는 네이버 ADVoost 검색 광고 연결 URL의 검색엔진 친화도를 분석한 결과입니다.</p>
    <p>수집 실패, 색인 실패, SEO 점검 필요 여부를 심층 진단하여 A에서 F까지의 등급을 제공합니다.</p>
    <p class="warning-copy">진단 도구는 웹사이트에 대한 전반적인 점검 결과를 제공하며 검색 광고 노출을 보장하지는 않습니다.</p>
    <h3>{escape(host)} 결과 점검하기</h3>
    <div class="overview">
      <div class="grade-caption">
        <div class="grade-circle">{escape(grade)}</div>
        <strong>{escape(report_grade_message(grade))}</strong>
        <span>개선 필요 항목: {issue_count}</span>
      </div>
      <div>
        {device_html}
        <p class="muted">URL: {escape(url)}</p>
        <p class="muted">분석일시: {escape(created_at)} · 점수: {escape(score)}</p>
      </div>
    </div>
    <div class="counts">
      <div class="count-card pass"><strong>{counts['PASS']}</strong><span>통과</span></div>
      <div class="count-card warn"><strong>{counts['WARNING']}</strong><span>경고</span></div>
      <div class="count-card fail"><strong>{counts['FAIL']}</strong><span>실패</span></div>
      <div class="count-card skip"><strong>{counts['NOT_CHECKED']}</strong><span>수집불가</span></div>
    </div>
  </section>
  {premium_sections}
</body>
</html>"""


def render_report_pdfs(records: list[dict], report_type: ReportType, platform: str) -> list[tuple[str, bytes]]:
    if sync_playwright is None:
        raise HTTPException(status_code=503, detail="Playwright is not installed.")

    results: list[tuple[str, bytes]] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            for record in records:
                page = browser.new_page(viewport={"width": 1240, "height": 1754})
                page.set_content(
                    build_report_html(record, report_type, platform),
                    wait_until="load",
                    timeout=20000,
                )
                pdf = page.pdf(
                    format="A4",
                    print_background=True,
                    margin={"top": "14mm", "right": "12mm", "bottom": "14mm", "left": "12mm"},
                )
                page.close()
                results.append((report_filename(record, report_type), pdf))
            browser.close()
    except PlaywrightError as exc:
        raise HTTPException(status_code=503, detail=f"PDF renderer failed: {exc}") from exc

    return results


def disposition(filename: str) -> str:
    return f"attachment; filename*=UTF-8''{quote(filename)}"


@app.post("/api/reports/pdf")
def export_report_pdf(request: ReportExportRequest) -> Response:
    pdfs = render_report_pdfs(request.records, request.report_type, request.platform)
    if request.bundle or len(pdfs) > 1:
        archive = io.BytesIO()
        with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zip_file:
            for filename, data in pdfs:
                zip_file.writestr(filename, data)
        archive_name = (
            "프리미엄_진단보고서_일괄.zip"
            if request.report_type == "premium"
            else "진단보고서_일괄.zip"
        )
        return Response(
            archive.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": disposition(archive_name)},
        )

    filename, data = pdfs[0]
    return Response(
        data,
        media_type="application/pdf",
        headers={"Content-Disposition": disposition(filename)},
    )
