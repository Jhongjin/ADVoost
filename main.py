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
from functools import lru_cache
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

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
except Exception:  # ReportLab is a server-side PDF fallback for restricted hosts.
    colors = None
    A4 = None
    mm = 1
    pdfmetrics = None
    UnicodeCIDFont = None
    TTFont = None
    canvas = None
    ImageReader = None


Status = Literal["PASS", "WARNING", "FAIL", "NOT_CHECKED"]
Severity = Literal["critical", "major", "minor", "info"]
Grade = Literal["A", "B", "C", "D", "F"]
JobStatus = Literal["queued", "running", "completed", "failed"]
ReportType = Literal["standard", "premium"]

DB_PATH = Path(__file__).with_name("advoost.sqlite3")
FONT_DIR = Path(__file__).parent / "assets" / "fonts"
REPORT_FONT_REGULAR = FONT_DIR / "NanumGothic-Regular.ttf"
REPORT_FONT_BOLD = FONT_DIR / "NanumGothic-Bold.ttf"
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


def report_get(data: dict, *keys: str, fallback: object = None) -> object:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return fallback


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


def report_created_at(record: dict) -> str:
    return report_text(report_get(record, "createdAt", "created_at"), "")


def report_keyword_summary(record: dict) -> dict:
    summary = report_get(record, "keywordSummary", "keyword_summary", fallback={})
    return summary if isinstance(summary, dict) else {}


def report_render_snapshot(record: dict) -> dict:
    snapshot = report_get(record, "renderSnapshot", "render_snapshot", fallback={})
    return snapshot if isinstance(snapshot, dict) else {}


def item_name(item: dict) -> str:
    return report_text(report_get(item, "itemName", "item_name"))


def item_detected_value(item: dict) -> str:
    return report_text(report_get(item, "detectedValue", "detected_value") or item.get("description"))


def item_remediation(item: dict) -> str:
    return report_text(report_get(item, "remediation", "guide") or item.get("description"))


def item_critical_for_grade(item: dict) -> bool:
    return bool(report_get(item, "criticalForGrade", "critical_for_grade", fallback=False))


def keyword_title_ok(row: dict) -> bool:
    return bool(report_get(row, "titleOk", "title_tag", fallback=False))


def keyword_desc_ok(row: dict) -> bool:
    return bool(report_get(row, "descOk", "meta_description", fallback=False))


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


@lru_cache(maxsize=1)
def report_font_face_css() -> str:
    if not REPORT_FONT_REGULAR.exists() or not REPORT_FONT_BOLD.exists():
        return ""
    regular = base64.b64encode(REPORT_FONT_REGULAR.read_bytes()).decode("ascii")
    bold = base64.b64encode(REPORT_FONT_BOLD.read_bytes()).decode("ascii")
    return f"""
    @font-face {{
      font-family: "ADVoostReport";
      src: url("data:font/truetype;base64,{regular}") format("truetype");
      font-weight: 400;
      font-style: normal;
    }}
    @font-face {{
      font-family: "ADVoostReport";
      src: url("data:font/truetype;base64,{bold}") format("truetype");
      font-weight: 700;
      font-style: normal;
    }}
    """


REPORT_STATUS_SORT = {"FAIL": 0, "WARNING": 1, "NOT_CHECKED": 2, "PASS": 3}


def report_items(record: dict) -> list[dict]:
    return list(record.get("items") or [])


def report_snippet(item: dict, limit: int = 1300) -> str:
    snippet = report_text(item.get("snippet"), "")
    if not snippet:
        return ""
    return snippet[:limit] + ("..." if len(snippet) > limit else "")


def report_group_name(item: dict) -> str:
    item_id = report_text(item.get("id"), "")
    status = report_text(item.get("status"), "")
    category = report_text(item.get("category"), "")
    if status == "NOT_CHECKED":
        return "진단 제외"
    if item_id in {"html-parse", "meta-robots"} or "색인" in category:
        return "색인 점검항목"
    if category == "수집" or item_id in {"http-status", "robots", "noscript"}:
        return "수집 점검항목"
    return "SEO 점검 필요"


def report_grouped_items(record: dict) -> list[tuple[str, list[dict]]]:
    groups = {
        "SEO 점검 필요": [],
        "색인 점검항목": [],
        "수집 점검항목": [],
        "진단 제외": [],
    }
    for item in report_items(record):
        groups[report_group_name(item)].append(item)
    return [(label, items) for label, items in groups.items() if items]


def report_top_issues(record: dict, limit: int = 4) -> list[dict]:
    items = sorted(
        report_items(record),
        key=lambda item: (
            REPORT_STATUS_SORT.get(report_text(item.get("status"), ""), 9),
            0 if item_critical_for_grade(item) else 1,
            item_name(item),
        ),
    )
    return [item for item in items if report_text(item.get("status"), "") in {"FAIL", "WARNING"}][:limit]


def keyword_rows(record: dict, key: str) -> list[dict]:
    summary = report_keyword_summary(record)
    snake_key = "single_rows" if key == "singleRows" else "phrase_rows"
    return list(summary.get(key) or summary.get(snake_key) or [])


def keyword_bar_width(row: dict) -> int:
    raw = report_text(row.get("ratio"), "0").replace("%", "")
    try:
        value = float(raw)
    except ValueError:
        value = 0
    return min(100, max(6, int(value * 120)))


def keyword_table_html(record: dict, key: str, title: str, limit: int = 30) -> str:
    rows = keyword_rows(record, key)
    if not rows:
        return "<p class='empty'>키워드 데이터가 없습니다.</p>"
    body = []
    for index, row in enumerate(rows[:limit], start=1):
        title_ok = keyword_title_ok(row)
        desc_ok = keyword_desc_ok(row)
        title_mark = "O" if title_ok else "X"
        desc_mark = "O" if desc_ok else "X"
        body.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td><strong>{escape(report_text(row.get('keyword')))}</strong></td>"
            f"<td>{escape(report_text(row.get('frequency')))}</td>"
            f"<td><div class='bar-cell'><span style='width:{keyword_bar_width(row)}%'></span></div></td>"
            f"<td>{escape(report_text(row.get('ratio')))}</td>"
            f"<td class='mark {'ok' if title_ok else 'no'}'>{title_mark}</td>"
            f"<td class='mark {'ok' if desc_ok else 'no'}'>{desc_mark}</td>"
            "</tr>"
        )
    total_key = "singleTotal" if key == "singleRows" else "phraseTotal"
    total = report_keyword_summary(record).get(total_key) or report_keyword_summary(record).get(
        "single_total" if key == "singleRows" else "phrase_total"
    )
    suffix = f"<p class='table-note'>상위 {min(limit, len(rows))}개 표시 (전체 {escape(report_text(total or len(rows)))}개)</p>"
    return (
        f"<h3>{escape(title)}</h3>"
        "<table class='keyword-table'><thead><tr><th>#</th><th>키워드</th><th>빈도수</th><th>페이지 빈도율</th><th>비율</th><th>타이틀 태그</th><th>메타 디스크립션</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>{suffix}"
    )


def keyword_summary_html(record: dict, compact: bool = False) -> str:
    limit = 10 if compact else 30
    return (
        "<section class='page-break'>"
        "<h2>키워드 요약</h2>"
        f"{keyword_table_html(record, 'singleRows', '개별 키워드', limit)}"
        f"{keyword_table_html(record, 'phraseRows', '구문(Phrase) 키워드', limit)}"
        "</section>"
    )


def issue_item_html(item: dict, premium: bool) -> str:
    status = report_text(item.get("status"), "NOT_CHECKED")
    name = escape(item_name(item))
    detected = escape(item_detected_value(item))
    guide = escape(item_remediation(item))
    snippet = escape(report_snippet(item, 2200 if premium else 1100))
    details_html = f"<div class='finding-copy'><strong>점검 항목:</strong> {detected}</div>"
    if status in {"WARNING", "FAIL"}:
        details_html = f"<div class='finding-copy warn-copy'><strong>개선 방안:</strong> {guide}</div>"
    snippet_html = f"<pre>{snippet}</pre>" if snippet and (premium or status != "PASS") else ""
    return (
        f"<article class='issue-item issue-{status.lower().replace('_', '-')}'><div class='issue-head'>"
        f"<div><span class='issue-dot'></span><strong>{name}</strong></div>"
        f"<div class='issue-meta'><span>{detected}</span><span class='status status-{status.lower().replace('_', '-')}'>{report_status_label(status)}</span></div>"
        f"</div>{details_html}{snippet_html}</article>"
    )


def issue_sections_html(record: dict, premium: bool) -> str:
    sections = []
    for title, items in report_grouped_items(record):
        sorted_items = sorted(
            items,
            key=lambda item: (
                REPORT_STATUS_SORT.get(report_text(item.get("status"), ""), 9),
                item_name(item),
            ),
        )
        if not premium and title == "진단 제외":
            # Keep the exclusion section short in the standard PDF.
            sorted_items = sorted_items[:6]
        pass_count = sum(1 for item in sorted_items if report_text(item.get("status"), "") == "PASS")
        warning_count = sum(1 for item in sorted_items if report_text(item.get("status"), "") == "WARNING")
        fail_count = sum(1 for item in sorted_items if report_text(item.get("status"), "") == "FAIL")
        summary_bits = []
        if warning_count:
            summary_bits.append(f"경고 {warning_count}")
        if fail_count:
            summary_bits.append(f"실패 {fail_count}")
        summary_bits.append(f"통과 {pass_count}")
        sections.append(
            "<section class='page-break audit-section'>"
            f"<div class='section-title'><h2>{escape(title)}</h2><span>{escape(' · '.join(summary_bits))}</span></div>"
            f"{''.join(issue_item_html(item, premium) for item in sorted_items)}"
            "</section>"
        )
    return "".join(sections)


def premium_cover_html(record: dict, device_html: str) -> str:
    url = report_text(record.get("url"))
    host = report_host(url)
    grade = report_text(record.get("grade"), "C")
    created_at = report_created_at(record)
    return f"""
    <section class="premium-cover">
      <div class="cover-brand">kt nasmedia</div>
      <div class="cover-content">
        <p class="cover-kicker">ADVoost Search Diagnostic Report</p>
        <h1>애드부스트 검색 진단 보고서</h1>
        <div class="cover-url">분석 대상: {escape(url)}</div>
      </div>
      <div class="cover-score">
        <div class="cover-grade">{escape(grade)}</div>
        <strong>랜딩페이지 분석 종합 점수</strong>
      </div>
      <div class="cover-bottom">
        <div class="cover-preview">{device_html}</div>
        <span>발행일: {escape(created_at[:10])} | {escape(host)} 진단보고서</span>
      </div>
    </section>
    """


def premium_summary_html(record: dict) -> str:
    counts = report_status_counts(record)
    grade = report_text(record.get("grade"), "C")
    top_issues = report_top_issues(record, 4)
    findings = "".join(
        f"<li><span>{index}</span><strong>{escape(item_name(item))}</strong><b>즉시 조치 필요</b></li>"
        for index, item in enumerate(top_issues, start=1)
    ) or "<li><span>1</span><strong>즉시 조치가 필요한 항목 없음</strong><b>유지 관리</b></li>"
    return f"""
    <section class="page-break premium-summary">
      <p class="eyebrow">EXECUTIVE SUMMARY</p>
      <h2>한눈에 보는 진단 결과</h2>
      <div class="summary-grid">
        <div class="summary-card">
          <div class="donut"><strong>{sum(counts.values())}</strong><span>총 점검 항목</span></div>
          <ul class="legend">
            <li><b class="green"></b>통과 {counts['PASS']}</li>
            <li><b class="orange"></b>경고 {counts['WARNING']}</li>
            <li><b class="red"></b>실패 {counts['FAIL']}</li>
            <li><b class="gray"></b>수집불가 {counts['NOT_CHECKED']}</li>
          </ul>
        </div>
        <div class="summary-card">
          <h3>종합 AEO(SEO) 등급</h3>
          <div class="grade-scale"><span>A</span><span>B</span><span class="active">{escape(grade)}</span><span>D</span><span>F</span></div>
          <p class="grade-band">현재 등급: {escape(grade)} ({escape(report_grade_message(grade))})</p>
        </div>
      </div>
      <h3>해결이 필요한 문제점</h3>
      <ul class="key-findings">{findings}</ul>
    </section>
    """


def readiness_categories(record: dict) -> list[tuple[str, float, str]]:
    items = report_items(record)
    buckets = [
        ("콘텐츠 연관성", {"콘텐츠", "SEO 점검항목", "소셜"}),
        ("수집 안정성", {"수집"}),
        ("색인 가능성", {"메타", "브랜드"}),
        ("성능·모바일", {"성능", "모바일"}),
        ("메타·공유·구조화", {"기술", "전환"}),
    ]
    result = []
    for label, categories in buckets:
        bucket_items = [item for item in items if report_text(item.get("category"), "") in categories]
        total = max(1, len(bucket_items))
        penalty = sum(
            2 if report_text(item.get("status"), "") == "FAIL" else 1
            for item in bucket_items
            if report_text(item.get("status"), "") in {"WARNING", "FAIL"}
        )
        score = max(0, min(100, round(((total * 2 - penalty) / (total * 2)) * 100, 1)))
        result.append((label, score, "#ef4444" if score < 60 else "#f97316" if score < 80 else "#22c55e"))
    return result


def premium_ars_html(record: dict) -> str:
    score = int(record.get("score") or 0)
    current = max(1.0, min(10.0, round(score / 10, 1)))
    after = min(10.0, round(current + len(report_top_issues(record, 6)) * 0.5, 1))
    rows = "".join(
        f"<tr><td>{escape(label)}</td><td>{score_value:.1f} / 100</td><td><div class='score-bar'><span style='width:{score_value}%; background:{color}'></span></div></td></tr>"
        for label, score_value, color in readiness_categories(record)
    )
    return f"""
    <section class="page-break ars-page">
      <p class="eyebrow">ADVoost Readiness Score</p>
      <h2>ARS Score — 광고연관지수 (프리미엄 지표)</h2>
      <div class="ars-grid">
        <div class="meter-card"><span>현재 ARS Score</span><strong>{current}</strong><small>/ 10</small></div>
        <div class="meter-arrow">BEFORE → AFTER</div>
        <div class="meter-card after"><span>경고 해결 후 예상 ARS</span><strong>{after}</strong><small>/ 10</small></div>
      </div>
      <h3>ARS 산출 근거 카테고리</h3>
      <table class="score-table"><thead><tr><th>검진 카테고리</th><th>현재 / 만점</th><th>점수 바</th></tr></thead><tbody>{rows}</tbody></table>
      <p class="table-note">ARS는 내부 분석 지표이며 실제 네이버 광고연관지수와 동일하지 않을 수 있습니다.</p>
    </section>
    """


def premium_overall_results_html(record: dict) -> str:
    total_count = len(report_items(record))
    colors_by_group = {
        "SEO 점검 필요": "#e9fbf1",
        "색인 점검항목": "#edf5ff",
        "수집 점검항목": "#fff4e8",
        "진단 제외": "#f5f7fb",
    }
    columns = []
    for title, items in report_grouped_items(record):
        if title == "진단 제외":
            continue
        rows = "".join(
            f"<li><span class='status-dot status-dot-{report_text(item.get('status'), '').lower().replace('_', '-')}'></span>{escape(item_name(item))}<b>{report_status_label(report_text(item.get('status'), ''))}</b></li>"
            for item in items[:14]
        )
        columns.append(
            f"<div class='result-column'><h3 style='background:{colors_by_group.get(title, '#f5f7fb')}'>{escape(title)} ({len(items)}개)</h3><ul>{rows}</ul></div>"
        )
    return f"""
    <section class="page-break">
      <p class="eyebrow">DETAILED AUDIT RESULTS</p>
      <h2>{total_count}개 점검 항목 전체 결과</h2>
      <div class="result-columns">{''.join(columns)}</div>
    </section>
    """


def premium_keyword_match_html(record: dict) -> str:
    rows = keyword_rows(record, "singleRows")[:10]
    cloud = "".join(
        f"<span style='font-size:{18 + min(22, int(row.get('frequency') or 1) * 3)}px; transform:rotate({(-18 + index * 7) % 34 - 17}deg)'>{escape(report_text(row.get('keyword')))}</span>"
        for index, row in enumerate(rows)
    )
    table = keyword_table_html(record, "singleRows", "키워드 - 메타 정보 매칭 현황", 10)
    missing = sum(1 for row in rows if not keyword_title_ok(row) or not keyword_desc_ok(row))
    return f"""
    <section class="page-break keyword-match">
      <p class="eyebrow">KEYWORD TO META TAG MATCHING</p>
      <h2>페이지 키워드 - 메타태그 매칭 분석</h2>
      <div class="keyword-match-grid">
        <div class="word-cloud">{cloud or '<span>키워드 없음</span>'}</div>
        <div>{table}</div>
      </div>
      <div class="alert-box">현재 페이지 내 고빈도 핵심 키워드 중 {missing}개가 title 또는 meta description에 충분히 반영되지 않았습니다.</div>
    </section>
    """


def premium_platform_guide_html(record: dict, platform: str) -> str:
    guide_items = "".join(
        f"<li><strong>{escape(title)}</strong><span>{escape(copy)}</span></li>"
        for title, copy in platform_guide(platform)
    )
    issues = "".join(
        f"<li><b>{index}</b><strong>{escape(item_name(item))}</strong><span>{escape(item_remediation(item))}</span></li>"
        for index, item in enumerate(report_top_issues(record, 4), start=1)
    )
    return f"""
    <section class="page-break platform-page">
      <p class="eyebrow">{escape(platform)} PLATFORM FIX GUIDE</p>
      <h2>발견된 항목 — {escape(platform)} 수정 가이드</h2>
      <div class="platform-grid">
        <aside><strong>{escape(platform)}</strong><p>선택한 쇼핑/CMS 환경 기준으로 우선 조치 항목을 정리했습니다.</p></aside>
        <div>
          <h3>우선 수정 항목</h3>
          <ol class="fix-list">{issues or '<li><b>1</b><strong>우선 수정 항목 없음</strong><span>현재 상태를 유지 관리하세요.</span></li>'}</ol>
          <h3>플랫폼 공통 가이드</h3>
          <ul class="guide-list">{guide_items}</ul>
        </div>
      </div>
    </section>
    """


def premium_glossary_html() -> str:
    rows = [
        ("Meta Description", "검색결과 하단에 표시되는 페이지 요약문", "누락 시 본문 임의 추출로 페이지 콘텐츠 파악 불가"),
        ("<title> 태그", "브라우저 탭 및 검색결과 제목에 표시되는 텍스트", "검색엔진 주제 파악 및 광고연관지수 핵심 신호"),
        ("<H1> 태그", "페이지 본문 내 최상위 주제 제목", "문서 구조 파악의 핵심, 연관도 평가 주요 지표"),
        ("Alt 속성", "이미지 내용을 텍스트로 설명하는 대체 정보", "이미지 검색 노출 및 접근성 필수"),
        ("Open Graph", "SNS 링크 공유 시 표시되는 썸네일/제목 미리보기 정보", "카카오톡/페이스북 등 외부 채널 바이럴 효율 저하 방지"),
        ("페이지 로딩 시간", "브라우저에서 전체 페이지가 안전 로드되는 시간", "3초 초과 시 이탈률 증가, 광고 효율 영향"),
        ("Schema.org", "검색엔진 이해도를 높이는 구조화 마크업", "리뷰/가격 등 리치 스니펫 노출로 클릭률 강화"),
    ]
    body = "".join(f"<tr><td><strong>{escape(a)}</strong></td><td>{escape(b)}</td><td>{escape(c)}</td></tr>" for a, b, c in rows)
    return f"""
    <section class="page-break">
      <p class="eyebrow">GLOSSARY OF AEO(SEO) AUDIT FACTORS</p>
      <h2>필수 점검 항목 정의</h2>
      <table><thead><tr><th>항목</th><th>정의 (What)</th><th>비즈니스 임팩트 (Why)</th></tr></thead><tbody>{body}</tbody></table>
      <div class="method-box">방법론: Google Search Quality Evaluator Guidelines, Search Central, Schema.org, W3C 접근성 표준을 참고해 내부 기준으로 정성 추정합니다.</div>
    </section>
    """


def premium_action_plan_html() -> str:
    return """
    <section class="page-break action-page">
      <p class="eyebrow">SEO TO AD PERFORMANCE MAPPING</p>
      <h2>애드부스트 광고 운영 연계 액션플랜</h2>
      <div class="action-grid">
        <div class="action-card"><h3>광고 연관지수 개선</h3><ul><li>메타 타이틀·디스크립션 및 페이지 핵심 문구 개선</li><li>광고 키워드와 랜딩페이지 본문/태그 간 연관도 강화</li></ul></div>
        <div class="action-card green"><h3>클릭 기대지수 개선</h3><ul><li>광고 소재 이미지와 확장소재 품질 개선</li><li>검색결과에 노출되는 공급 문구와 이미지 매력도 강화</li></ul></div>
        <div class="action-card"><h3>활용법 4가지</h3><ol><li>동일 URL에 등록된 광고 키워드 연결도 점검</li><li>후보 랜딩 비교 후 저연관 조합 제외</li><li>랜딩 콘텐츠 보강 방향 도출</li><li>재진단으로 개선 여부 추적</li></ol></div>
        <div class="action-card green"><h3>클릭 기대지수 활용법</h3><ol><li>소재 제목·설명·이미지 개선</li><li>가격/혜택/신뢰 요소 강화</li><li>확장소재 조합 테스트</li><li>검색 의도별 문구 실험</li></ol></div>
      </div>
    </section>
    """


def premium_appendix_html() -> str:
    rows = [
        ("SEO", "검색엔진 최적화", "Alt 속성", "이미지 대체 텍스트"),
        ("SERP", "검색 결과 페이지", "Viewport", "모바일 화면 표시 영역 메타"),
        ("CTR", "클릭률", "robots.txt", "크롤러 수집 지시 파일"),
        ("CPC", "클릭 비용", "Content-Type", "HTTP 응답 콘텐츠 유형"),
        ("ROAS", "광고 투자 수익률", "Soft 404", "정상 응답이지만 오류 페이지"),
        ("OG", "SNS 공유 미리보기 메타", "ADVoost", "네이버 AI 광고 시스템"),
    ]
    body = "".join(f"<tr><td><strong>{a}</strong></td><td>{b}</td><td><strong>{c}</strong></td><td>{d}</td></tr>" for a, b, c, d in rows)
    return f"""
    <section class="page-break appendix-page">
      <p class="eyebrow">GLOSSARY & REFERENCES</p>
      <h2>부록 (Appendix & References)</h2>
      <table><tbody>{body}</tbody></table>
    </section>
    <section class="page-break end-page">
      <span>EOD</span>
      <h2>지금 개선하면, 광고 효율이 달라집니다.</h2>
      <div class="contact-card"><strong>문의</strong><p>mc2@nasmedia.co.kr</p></div>
      <footer>kt nasmedia | Confidential</footer>
    </section>
    """


def build_report_html(record: dict, report_type: ReportType, platform: str) -> str:
    premium = report_type == "premium"
    counts = report_status_counts(record)
    url = report_text(record.get("url"))
    host = report_host(url)
    grade = report_text(record.get("grade"), "C")
    score = report_text(record.get("score"), "0")
    created_at = report_created_at(record)
    snapshot = report_render_snapshot(record)
    desktop = report_text(report_get(snapshot, "desktopScreenshot", "desktop_screenshot"), "")
    mobile = report_text(report_get(snapshot, "mobileScreenshot", "mobile_screenshot"), "")
    issue_count = counts["WARNING"] + counts["FAIL"]
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
    intro_title = "한눈에 보는 진단 결과" if premium else "진단보고서"
    intro_copy = (
        "프리미엄 리포트는 진단 결과를 광고 운영 관점의 개선 우선순위와 플랫폼 수정 가이드로 재구성합니다."
        if premium
        else "이 보고서는 네이버 ADVoost 검색 광고 연결 URL의 검색엔진 친화도를 분석한 결과입니다."
    )
    if premium:
        report_sections = (
            premium_summary_html(record)
            + premium_ars_html(record)
            + premium_overall_results_html(record)
            + keyword_summary_html(record, compact=True)
            + premium_keyword_match_html(record)
            + issue_sections_html(record, premium=True)
            + premium_platform_guide_html(record, platform)
            + premium_glossary_html()
            + premium_action_plan_html()
            + premium_appendix_html()
        )
        leading_section = premium_cover_html(record, device_html)
    else:
        report_sections = keyword_summary_html(record, compact=False) + issue_sections_html(record, premium=False)
        leading_section = ""

    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <style>
    {report_font_face_css()}
    @page {{ size: A4; margin: 17mm 15mm; }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: #06132a;
      background: #fff;
      font-family: "ADVoostReport", "Noto Sans KR", "Malgun Gothic", Arial, sans-serif;
      font-size: 12.5px;
      line-height: 1.58;
    }}
    section {{ margin-bottom: 20px; }}
    .page-break {{ break-before: page; }}
    .eyebrow {{
      margin: 0 0 8px;
      color: #7a8797;
      font-size: 10px;
      font-weight: 700;
      letter-spacing: .08em;
      text-transform: uppercase;
    }}
    .brand {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 18px;
      border-bottom: 1px solid #dbe3eb;
    }}
    .brand strong {{ color: #3346a3; font-size: 20px; }}
    .brand b {{ font-size: 20px; }}
    h1 {{ margin: 30px 0 12px; font-size: 27px; line-height: 1.24; }}
    h2 {{ margin: 0 0 14px; font-size: 20px; line-height: 1.25; }}
    h3 {{ margin: 17px 0 10px; font-size: 15px; }}
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
    .keyword-table th:nth-child(1), .keyword-table td:nth-child(1) {{ width: 46px; text-align: center; white-space: nowrap; }}
    .keyword-table th:nth-child(2), .keyword-table td:nth-child(2) {{ width: 31%; }}
    .keyword-table th:nth-child(3), .keyword-table td:nth-child(3) {{ width: 70px; text-align: right; }}
    .keyword-table th:nth-child(5), .keyword-table td:nth-child(5) {{ width: 70px; }}
    .keyword-table th:nth-child(6), .keyword-table td:nth-child(6),
    .keyword-table th:nth-child(7), .keyword-table td:nth-child(7) {{ width: 92px; text-align: center; }}
    .bar-cell {{ width: 100%; height: 8px; border-radius: 99px; background: #e8eef3; overflow: hidden; }}
    .bar-cell span {{ display: block; height: 100%; border-radius: inherit; background: #10c98b; }}
    .mark {{ font-weight: 800; }}
    .mark.ok {{ color: #00945f; }}
    .mark.no {{ color: #cf103d; }}
    .table-note {{ margin: 8px 0 18px; text-align: right; color: #8b97a7; font-size: 11px; }}
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
    .section-title {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 16px 18px;
      border: 1px solid #dbe3eb;
      border-bottom: 0;
      border-radius: 8px 8px 0 0;
      background: #f7f9fc;
    }}
    .section-title h2 {{ margin: 0; }}
    .section-title span {{ color: #00945f; font-weight: 700; }}
    .issue-item {{
      break-inside: avoid;
      padding: 14px 18px;
      border: 1px solid #dbe3eb;
      border-top: 0;
      background: #fff;
    }}
    .issue-item:last-child {{ border-radius: 0 0 8px 8px; }}
    .issue-head {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(180px, 42%);
      gap: 14px;
      align-items: center;
      margin-bottom: 10px;
    }}
    .issue-head > div:first-child {{
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
      font-size: 15px;
    }}
    .issue-dot {{
      width: 13px;
      height: 13px;
      border-radius: 99px;
      border: 2px solid #00b967;
      flex: 0 0 auto;
    }}
    .issue-warning .issue-dot {{ border-color: #ff8a00; }}
    .issue-fail .issue-dot {{ border-color: #ff3347; }}
    .issue-not-checked .issue-dot {{ border-color: #9aa7b5; }}
    .issue-meta {{
      display: flex;
      justify-content: flex-end;
      align-items: center;
      gap: 10px;
      color: #536276;
      text-align: right;
    }}
    .finding-copy {{
      margin: 0 0 10px 24px;
      padding: 9px 12px;
      border-radius: 7px;
      color: #087141;
      background: #edfbf3;
    }}
    .warn-copy {{ color: #536276; background: #fff8e7; }}
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
    .premium-cover {{
      position: relative;
      min-height: 260mm;
      margin: -17mm -15mm 0;
      padding: 24mm 20mm;
      color: #fff;
      background: #1b2e4d;
      break-after: page;
    }}
    .cover-brand {{ color: #ff2434; font-size: 20px; font-weight: 800; }}
    .cover-content {{ margin-top: 52mm; }}
    .cover-kicker {{ color: #29d978; font-weight: 800; }}
    .premium-cover h1 {{ max-width: 540px; color: #fff; font-size: 36px; }}
    .cover-url {{
      width: 60%;
      margin-top: 18px;
      padding: 12px 16px;
      border-left: 4px solid #2de078;
      border-radius: 4px;
      background: rgba(82, 111, 169, .35);
      font-weight: 700;
    }}
    .cover-score {{ position: absolute; right: 70px; top: 250px; text-align: center; }}
    .cover-grade {{
      display: grid;
      place-items: center;
      width: 140px;
      height: 140px;
      margin-bottom: 10px;
      border: 7px solid #f5b400;
      border-radius: 999px;
      color: #ff8b1a;
      font-size: 54px;
      font-weight: 800;
    }}
    .cover-bottom {{
      position: absolute;
      left: 20mm;
      right: 20mm;
      bottom: 24mm;
      display: flex;
      align-items: end;
      justify-content: space-between;
      border-top: 1px solid rgba(255,255,255,.18);
      padding-top: 18px;
      color: #b9c8dc;
    }}
    .cover-preview .devices {{ width: 190px; grid-template-columns: 120px 46px; gap: 10px; }}
    .cover-preview .desktop {{ height: 78px; }}
    .cover-preview .mobile {{ height: 82px; border-radius: 12px; }}
    .summary-grid, .ars-grid, .keyword-match-grid, .platform-grid, .action-grid {{
      display: grid;
      gap: 16px;
    }}
    .summary-grid {{ grid-template-columns: 1fr 1fr; }}
    .summary-card, .meter-card, .platform-grid aside, .action-card, .method-box, .alert-box, .contact-card {{
      border: 1px solid #dbe3eb;
      border-radius: 8px;
      background: #fff;
      padding: 18px;
    }}
    .summary-card {{ display: flex; gap: 22px; align-items: center; }}
    .donut {{
      display: grid;
      place-items: center;
      width: 126px;
      height: 126px;
      border-radius: 999px;
      border: 18px solid #22c55e;
      text-align: center;
    }}
    .donut strong {{ display: block; font-size: 28px; }}
    .donut span {{ display: block; color: #536276; font-size: 10px; }}
    .legend {{ margin: 0; padding: 0; list-style: none; color: #536276; }}
    .legend li {{ margin: 4px 0; }}
    .legend b {{ display: inline-block; width: 10px; height: 10px; margin-right: 6px; border-radius: 99px; }}
    .green {{ background: #22c55e; }}
    .orange {{ background: #f97316; }}
    .red {{ background: #ef4444; }}
    .gray {{ background: #cbd5e1; }}
    .grade-scale {{
      display: grid;
      grid-template-columns: repeat(5, 1fr);
      gap: 0;
      width: 100%;
      margin: 12px 0;
      overflow: hidden;
      border-radius: 999px;
      background: linear-gradient(90deg,#22c55e,#5dde84,#f6c945,#f97316,#ef4444);
    }}
    .grade-scale span {{ padding: 10px 0; color: #17304d; text-align: center; font-weight: 700; }}
    .grade-scale .active {{ outline: 2px solid #ff7a1a; outline-offset: -4px; border-radius: 999px; background: rgba(255,255,255,.72); }}
    .grade-band {{ padding: 9px 12px; border-radius: 8px; color: #f97316; background: #fff3e5; text-align: center; font-weight: 700; }}
    .key-findings {{
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 10px;
      padding: 0;
      list-style: none;
    }}
    .key-findings li {{
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 12px;
      border: 1px solid #ffd0b3;
      border-left: 4px solid #f97316;
      border-radius: 8px;
    }}
    .key-findings span {{
      display: grid;
      place-items: center;
      width: 26px;
      height: 26px;
      border-radius: 99px;
      color: #f97316;
      background: #fff3e5;
      font-weight: 800;
    }}
    .key-findings strong {{ flex: 1; }}
    .key-findings b {{ color: #ff6b37; font-size: 11px; }}
    .ars-grid {{ grid-template-columns: 1fr 120px 1fr; align-items: center; }}
    .meter-card {{ text-align: center; }}
    .meter-card strong {{ color: #f97316; font-size: 38px; }}
    .meter-card.after strong {{ color: #22c55e; }}
    .meter-arrow {{ color: #22c55e; text-align: center; font-weight: 800; }}
    .score-bar {{ height: 9px; overflow: hidden; border-radius: 99px; background: #edf2f7; }}
    .score-bar span {{ display: block; height: 100%; border-radius: inherit; }}
    .score-table th:nth-child(1), .score-table td:nth-child(1) {{ width: 34%; }}
    .score-table th:nth-child(2), .score-table td:nth-child(2) {{ width: 110px; text-align: center; }}
    .result-columns {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }}
    .result-column {{ border: 1px solid #dbe3eb; border-radius: 8px; overflow: hidden; }}
    .result-column h3 {{ margin: 0; padding: 12px; }}
    .result-column ul {{ margin: 0; padding: 8px 10px 12px; list-style: none; }}
    .result-column li {{
      display: grid;
      grid-template-columns: 14px 1fr auto;
      gap: 6px;
      align-items: center;
      padding: 7px 0;
      border-bottom: 1px solid #edf2f7;
      font-size: 11px;
    }}
    .result-column li:last-child {{ border-bottom: 0; }}
    .result-column b {{ padding: 2px 7px; border-radius: 99px; color: #00945f; background: #e9fbf1; }}
    .status-dot {{ width: 10px; height: 10px; border: 2px solid #22c55e; border-radius: 99px; }}
    .status-dot-warning {{ border-color: #f97316; }}
    .status-dot-fail {{ border-color: #ef4444; }}
    .keyword-match-grid {{ grid-template-columns: .9fr 1.1fr; }}
    .word-cloud {{
      display: flex;
      min-height: 290px;
      align-items: center;
      justify-content: center;
      gap: 16px;
      flex-wrap: wrap;
      border: 1px solid #dbe3eb;
      border-radius: 8px;
      color: #06132a;
      background: #fff;
      font-weight: 800;
    }}
    .word-cloud span:nth-child(3n) {{ color: #16a34a; }}
    .alert-box {{ margin-top: 14px; color: #d65a00; background: #fff8ef; }}
    .platform-grid {{ grid-template-columns: 260px 1fr; }}
    .platform-grid aside {{ background: #f8fbfd; }}
    .platform-grid aside strong {{
      display: inline-flex;
      margin-bottom: 12px;
      padding: 8px 14px;
      border-radius: 8px;
      color: #fff;
      background: #ff7a1a;
      font-size: 16px;
    }}
    .fix-list {{ display: grid; gap: 10px; padding-left: 0; list-style: none; }}
    .fix-list li {{ display: grid; grid-template-columns: 24px 180px 1fr; gap: 10px; padding: 12px; border-bottom: 1px solid #edf2f7; }}
    .fix-list b {{ color: #ff7a1a; }}
    .fix-list span {{ color: #536276; }}
    .action-grid {{ grid-template-columns: repeat(2, 1fr); }}
    .action-card {{ background: #f8fbfd; }}
    .action-card.green {{ background: #eefbf4; }}
    .action-card h3 {{ margin-top: 0; }}
    .action-card li {{ margin-bottom: 7px; }}
    .method-box {{ margin-top: 16px; color: #536276; background: #f8fbfd; }}
    .appendix-page table td {{ width: 25%; }}
    .end-page {{
      display: grid;
      min-height: 250mm;
      place-items: center;
      text-align: center;
    }}
    .end-page > span {{
      padding: 8px 26px;
      border-radius: 999px;
      color: #00945f;
      background: #e9fbf1;
      font-weight: 800;
      letter-spacing: .18em;
    }}
    .end-page h2 {{ font-size: 30px; }}
    .contact-card {{ width: 280px; }}
    .end-page footer {{ position: absolute; bottom: 18mm; color: #8b97a7; }}
  </style>
</head>
<body>
  {leading_section}
  <section>
    <div class="brand">
      <div><strong>ADVoost</strong> 검색 × <b>SEO.co.kr</b></div>
      <span>{'프리미엄 웹사이트 분석 리포트' if premium else '웹사이트 분석 리포트'}</span>
    </div>
    <h1>{escape(intro_title)}</h1>
    <p>{escape(intro_copy)}</p>
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
  {report_sections}
</body>
</html>"""


REPORTLAB_REGULAR_FONT = "HYSMyeongJo-Medium"
REPORTLAB_BOLD_FONT = "HYGothic-Medium"
REPORTLAB_FALLBACK_REGULAR = "Helvetica"
REPORTLAB_FALLBACK_BOLD = "Helvetica-Bold"


def render_report_pdfs_with_browser(
    records: list[dict],
    report_type: ReportType,
    platform: str,
) -> list[tuple[str, bytes]]:
    if sync_playwright is None:
        raise RuntimeError("Playwright is not installed.")
    results: list[tuple[str, bytes]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
            ],
        )
        try:
            for record in records:
                page = browser.new_page(viewport={"width": 1240, "height": 1754})
                try:
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
                finally:
                    page.close()
                results.append((report_filename(record, report_type), pdf))
        finally:
            browser.close()
    return results


def reportlab_is_available() -> bool:
    return bool(canvas and colors and A4 and pdfmetrics and UnicodeCIDFont and ImageReader)


@lru_cache(maxsize=1)
def ensure_reportlab_fonts() -> tuple[str, str]:
    if not reportlab_is_available():
        raise RuntimeError("ReportLab is not installed.")
    if TTFont and REPORT_FONT_REGULAR.exists() and REPORT_FONT_BOLD.exists():
        pdfmetrics.registerFont(TTFont("ADVoostReport", str(REPORT_FONT_REGULAR)))
        pdfmetrics.registerFont(TTFont("ADVoostReport-Bold", str(REPORT_FONT_BOLD)))
        return "ADVoostReport", "ADVoostReport-Bold"
    try:
        pdfmetrics.registerFont(UnicodeCIDFont(REPORTLAB_REGULAR_FONT))
        pdfmetrics.registerFont(UnicodeCIDFont(REPORTLAB_BOLD_FONT))
        return REPORTLAB_REGULAR_FONT, REPORTLAB_BOLD_FONT
    except Exception:
        return REPORTLAB_FALLBACK_REGULAR, REPORTLAB_FALLBACK_BOLD


def pdf_hex(value: str):
    return colors.HexColor(value)


def wrap_pdf_text(text: object, font_name: str, font_size: int, max_width: float) -> list[str]:
    source = report_text(text, "")
    if not source:
        return []
    paragraphs = source.splitlines() or [source]
    wrapped: list[str] = []
    for paragraph in paragraphs:
        normalized = re.sub(r"\s+", " ", paragraph).strip()
        if not normalized:
            wrapped.append("")
            continue
        current = ""
        for char in normalized:
            candidate = f"{current}{char}"
            if current and pdfmetrics.stringWidth(candidate, font_name, font_size) > max_width:
                wrapped.append(current.rstrip())
                current = char.lstrip()
            else:
                current = candidate
        if current:
            wrapped.append(current.rstrip())
    return wrapped


def image_reader_from_data_url(data_url: object):
    source = report_text(data_url, "")
    if not source.startswith("data:image/") or "," not in source:
        return None
    try:
        payload = source.split(",", 1)[1]
        return ImageReader(io.BytesIO(base64.b64decode(payload)))
    except Exception:
        return None


class ReportLabPdf:
    def __init__(self, title: str):
        self.buffer = io.BytesIO()
        self.width, self.height = A4
        self.margin_x = 18 * mm
        self.margin_top = 18 * mm
        self.margin_bottom = 16 * mm
        self.y = self.height - self.margin_top
        self.regular_font, self.bold_font = ensure_reportlab_fonts()
        self.canvas = canvas.Canvas(self.buffer, pagesize=A4)
        self.canvas.setTitle(title)

    @property
    def content_width(self) -> float:
        return self.width - (self.margin_x * 2)

    def finish(self) -> bytes:
        self.canvas.save()
        return self.buffer.getvalue()

    def ensure_space(self, needed: float) -> None:
        if self.y - needed < self.margin_bottom:
            self.canvas.showPage()
            self.y = self.height - self.margin_top

    def text(
        self,
        value: object,
        *,
        x: float | None = None,
        size: int = 10,
        font: str | None = None,
        color: str = "#06132a",
        leading: float | None = None,
        max_width: float | None = None,
    ) -> None:
        font_name = font or self.regular_font
        line_height = leading or (size + 5)
        x_pos = self.margin_x if x is None else x
        width = max_width or (self.width - self.margin_x - x_pos)
        lines = wrap_pdf_text(value, font_name, size, width)
        self.ensure_space(max(line_height * max(len(lines), 1), line_height))
        self.canvas.setFont(font_name, size)
        self.canvas.setFillColor(pdf_hex(color))
        for line in lines:
            self.canvas.drawString(x_pos, self.y, line)
            self.y -= line_height

    def heading(self, value: str, size: int = 18) -> None:
        self.ensure_space(size + 18)
        self.canvas.setFont(self.bold_font, size)
        self.canvas.setFillColor(pdf_hex("#06132a"))
        self.canvas.drawString(self.margin_x, self.y, value)
        self.y -= size + 12

    def divider(self, gap: float = 10) -> None:
        self.ensure_space(gap + 2)
        self.y -= gap / 2
        self.canvas.setStrokeColor(pdf_hex("#dbe3eb"))
        self.canvas.line(self.margin_x, self.y, self.width - self.margin_x, self.y)
        self.y -= gap

    def badge(self, value: str, x: float, y: float, fill: str, stroke: str, color: str) -> None:
        self.canvas.setFillColor(pdf_hex(fill))
        self.canvas.setStrokeColor(pdf_hex(stroke))
        self.canvas.roundRect(x, y - 4, 42, 18, 7, fill=1, stroke=1)
        self.canvas.setFont(self.bold_font, 9)
        self.canvas.setFillColor(pdf_hex(color))
        self.canvas.drawCentredString(x + 21, y + 1, value)

    def status_badge(self, status: str, x: float, y: float) -> None:
        colors_by_status = {
            "PASS": ("#e8fbf1", "#b7efcf", "#00945f"),
            "WARNING": ("#fff5de", "#ffd58a", "#c65a00"),
            "FAIL": ("#ffe9ee", "#ffb9c8", "#d20b3f"),
            "NOT_CHECKED": ("#edf2f7", "#dbe3eb", "#536276"),
        }
        fill, stroke, text_color = colors_by_status.get(status, colors_by_status["NOT_CHECKED"])
        self.badge(report_status_label(status), x, y, fill, stroke, text_color)

    def draw_image_frame(
        self,
        data_url: object,
        x: float,
        top: float,
        width: float,
        height: float,
        label: str,
    ) -> None:
        self.canvas.setFillColor(pdf_hex("#f6f8fb"))
        self.canvas.setStrokeColor(pdf_hex("#cbd7e2"))
        self.canvas.roundRect(x, top - height, width, height, 5, fill=1, stroke=1)
        reader = image_reader_from_data_url(data_url)
        if reader is None:
            self.canvas.setFont(self.regular_font, 8)
            self.canvas.setFillColor(pdf_hex("#7b8795"))
            self.canvas.drawCentredString(x + width / 2, top - height / 2, f"{label} 캡처 없음")
            return
        self.canvas.saveState()
        path = self.canvas.beginPath()
        path.roundRect(x, top - height, width, height, 5)
        self.canvas.clipPath(path, stroke=0, fill=0)
        self.canvas.drawImage(
            reader,
            x,
            top - height,
            width=width,
            height=height,
            preserveAspectRatio=True,
            anchor="n",
            mask="auto",
        )
        self.canvas.restoreState()

    def header(self, premium: bool) -> None:
        self.canvas.setFont(self.bold_font, 15)
        self.canvas.setFillColor(pdf_hex("#3346a3"))
        self.canvas.drawString(self.margin_x, self.y, "ADVoost")
        self.canvas.setFillColor(pdf_hex("#06132a"))
        self.canvas.drawString(self.margin_x + 68, self.y, "검색 × SEO.co.kr")
        self.canvas.setFont(self.regular_font, 9)
        self.canvas.setFillColor(pdf_hex("#6b7685"))
        label = "프리미엄 웹사이트 분석 리포트" if premium else "웹사이트 분석 리포트"
        self.canvas.drawRightString(self.width - self.margin_x, self.y, label)
        self.y -= 18
        self.divider(10)

    def count_cards(self, counts: dict[str, int]) -> None:
        card_gap = 6
        card_width = (self.content_width - card_gap * 3) / 4
        card_height = 46
        labels = [
            ("PASS", "통과", "#e9fbf1", "#00a965"),
            ("WARNING", "경고", "#fff7df", "#d87600"),
            ("FAIL", "실패", "#ffecee", "#e40046"),
            ("NOT_CHECKED", "수집불가", "#fff0f5", "#a50041"),
        ]
        self.ensure_space(card_height + 16)
        top = self.y
        for index, (key, label, fill, color) in enumerate(labels):
            x = self.margin_x + index * (card_width + card_gap)
            self.canvas.setFillColor(pdf_hex(fill))
            self.canvas.setStrokeColor(pdf_hex("#dbe3eb"))
            self.canvas.roundRect(x, top - card_height, card_width, card_height, 5, fill=1, stroke=1)
            self.canvas.setFont(self.bold_font, 16)
            self.canvas.setFillColor(pdf_hex(color))
            self.canvas.drawCentredString(x + card_width / 2, top - 22, str(counts[key]))
            self.canvas.setFont(self.regular_font, 8)
            self.canvas.drawCentredString(x + card_width / 2, top - 35, label)
        self.y -= card_height + 18

    def item_block(self, item: dict, premium: bool) -> None:
        status = report_text(item.get("status"), "NOT_CHECKED")
        name = item_name(item)
        category = report_text(item.get("category"))
        detected = item_detected_value(item)
        guide = item_remediation(item)
        snippet = report_text(item.get("snippet"), "")
        self.ensure_space(70)
        block_top = self.y
        self.canvas.setStrokeColor(pdf_hex("#e2e8f0"))
        self.canvas.line(self.margin_x, block_top + 7, self.width - self.margin_x, block_top + 7)
        self.status_badge(status, self.margin_x, block_top - 10)
        self.canvas.setFont(self.bold_font, 10)
        self.canvas.setFillColor(pdf_hex("#06132a"))
        self.canvas.drawString(self.margin_x + 54, block_top - 5, name[:70])
        self.canvas.setFont(self.regular_font, 8)
        self.canvas.setFillColor(pdf_hex("#6b7685"))
        self.canvas.drawRightString(self.width - self.margin_x, block_top - 5, category)
        self.y -= 28
        self.text(f"감지값: {detected}", x=self.margin_x + 54, size=8, color="#536276", max_width=self.content_width - 54)
        self.text(f"개선 방안: {guide}", x=self.margin_x + 54, size=8, color="#536276", max_width=self.content_width - 54)
        if premium and snippet:
            snippet_text = snippet[:1800] + ("..." if len(snippet) > 1800 else "")
            self.ensure_space(42)
            self.canvas.setFillColor(pdf_hex("#edf3f7"))
            snippet_height = min(90, max(42, len(wrap_pdf_text(snippet_text, self.regular_font, 7, self.content_width - 64)) * 10 + 14))
            self.canvas.roundRect(self.margin_x + 54, self.y - snippet_height + 7, self.content_width - 54, snippet_height, 5, fill=1, stroke=0)
            self.text(snippet_text, x=self.margin_x + 62, size=7, color="#536276", leading=9, max_width=self.content_width - 70)
        self.y -= 8


def draw_keyword_rows(pdf: ReportLabPdf, rows: list[dict], title: str) -> None:
    pdf.heading(title, 13)
    if not rows:
        pdf.text("키워드 데이터가 없습니다.", size=9, color="#6b7685")
        return
    header_y = pdf.y
    pdf.canvas.setFillColor(pdf_hex("#f4f7fa"))
    pdf.canvas.roundRect(pdf.margin_x, header_y - 18, pdf.content_width, 22, 4, fill=1, stroke=0)
    pdf.canvas.setFont(pdf.bold_font, 8)
    pdf.canvas.setFillColor(pdf_hex("#536276"))
    columns = [0, 34, 250, 310, 380, 455]
    labels = ["#", "키워드", "빈도수", "비율", "타이틀", "설명"]
    for offset, label in zip(columns, labels):
        pdf.canvas.drawString(pdf.margin_x + offset, header_y - 9, label)
    pdf.y -= 26
    for index, row in enumerate(rows[:30], start=1):
        pdf.ensure_space(22)
        y = pdf.y
        pdf.canvas.setStrokeColor(pdf_hex("#e2e8f0"))
        pdf.canvas.line(pdf.margin_x, y + 7, pdf.width - pdf.margin_x, y + 7)
        pdf.canvas.setFont(pdf.regular_font, 8)
        pdf.canvas.setFillColor(pdf_hex("#06132a"))
        values = [
            str(index),
            report_text(row.get("keyword")),
            report_text(row.get("frequency")),
            report_text(row.get("ratio")),
            "통과" if keyword_title_ok(row) else "미포함",
            "통과" if keyword_desc_ok(row) else "미포함",
        ]
        for offset, value in zip(columns, values):
            pdf.canvas.drawString(pdf.margin_x + offset, y - 6, value[:24])
        pdf.y -= 18
    pdf.y -= 6


def reportlab_new_page(pdf: ReportLabPdf, premium: bool) -> None:
    pdf.canvas.showPage()
    pdf.y = pdf.height - pdf.margin_top
    pdf.header(premium)


def draw_reportlab_cover(pdf: ReportLabPdf, record: dict, platform: str) -> None:
    url = report_text(record.get("url"))
    grade = report_text(record.get("grade"), "C")
    created_at = report_created_at(record)
    snapshot = report_render_snapshot(record)
    pdf.canvas.setFillColor(pdf_hex("#1b2e4d"))
    pdf.canvas.rect(0, 0, pdf.width, pdf.height, fill=1, stroke=0)
    pdf.canvas.setFont(pdf.bold_font, 19)
    pdf.canvas.setFillColor(pdf_hex("#ff2434"))
    pdf.canvas.drawString(pdf.margin_x, pdf.height - 46, "kt nasmedia")
    pdf.canvas.setFont(pdf.bold_font, 10)
    pdf.canvas.setFillColor(pdf_hex("#29d978"))
    pdf.canvas.drawString(pdf.margin_x, pdf.height - 205, "ADVoost Search Diagnostic Report")
    pdf.canvas.setFont(pdf.bold_font, 30)
    pdf.canvas.setFillColor(pdf_hex("#ffffff"))
    pdf.canvas.drawString(pdf.margin_x, pdf.height - 240, "애드부스트 검색 진단 보고서")
    pdf.canvas.setFillColor(pdf_hex("#2b4070"))
    pdf.canvas.roundRect(pdf.margin_x, pdf.height - 288, 360, 28, 4, fill=1, stroke=0)
    pdf.canvas.setFillColor(pdf_hex("#2de078"))
    pdf.canvas.rect(pdf.margin_x, pdf.height - 288, 3, 28, fill=1, stroke=0)
    pdf.canvas.setFont(pdf.bold_font, 10)
    pdf.canvas.setFillColor(pdf_hex("#ffffff"))
    pdf.canvas.drawString(pdf.margin_x + 14, pdf.height - 279, f"분석 대상: {url[:60]}")
    center_x = pdf.width - pdf.margin_x - 110
    center_y = pdf.height - 270
    pdf.canvas.setStrokeColor(pdf_hex("#f5b400"))
    pdf.canvas.setFillColor(pdf_hex("#1b2e4d"))
    pdf.canvas.setLineWidth(5)
    pdf.canvas.circle(center_x, center_y, 48, stroke=1, fill=1)
    pdf.canvas.setFont(pdf.bold_font, 30)
    pdf.canvas.setFillColor(pdf_hex("#ff8b1a"))
    pdf.canvas.drawCentredString(center_x, center_y - 10, grade)
    pdf.canvas.setLineWidth(1)
    bottom_y = 150
    pdf.canvas.setStrokeColor(pdf_hex("#40577b"))
    pdf.canvas.line(pdf.margin_x, bottom_y + 90, pdf.width - pdf.margin_x, bottom_y + 90)
    pdf.draw_image_frame(report_get(snapshot, "desktopScreenshot", "desktop_screenshot"), pdf.margin_x, bottom_y + 72, 80, 52, "데스크톱")
    pdf.draw_image_frame(report_get(snapshot, "mobileScreenshot", "mobile_screenshot"), pdf.margin_x + 92, bottom_y + 72, 34, 52, "모바일")
    pdf.canvas.setFont(pdf.bold_font, 9)
    pdf.canvas.setFillColor(pdf_hex("#c3d0e3"))
    pdf.canvas.drawRightString(pdf.width - pdf.margin_x, bottom_y + 30, f"발행일: {created_at[:10]} | 플랫폼: {platform}")
    pdf.canvas.showPage()
    pdf.y = pdf.height - pdf.margin_top


def draw_reportlab_summary(pdf: ReportLabPdf, record: dict, premium: bool) -> None:
    counts = report_status_counts(record)
    grade = report_text(record.get("grade"), "C")
    pdf.heading("한눈에 보는 진단 결과", 18)
    top = pdf.y
    pdf.ensure_space(150)
    card_w = (pdf.content_width - 12) / 2
    for index, title in enumerate(["총 점검 항목", "종합 AEO(SEO) 등급"]):
        x = pdf.margin_x + index * (card_w + 12)
        pdf.canvas.setFillColor(pdf_hex("#ffffff"))
        pdf.canvas.setStrokeColor(pdf_hex("#dbe3eb"))
        pdf.canvas.roundRect(x, top - 112, card_w, 112, 6, fill=1, stroke=1)
        pdf.canvas.setFont(pdf.bold_font, 11)
        pdf.canvas.setFillColor(pdf_hex("#06132a"))
        pdf.canvas.drawString(x + 12, top - 22, title)
        if index == 0:
            pdf.canvas.setStrokeColor(pdf_hex("#22c55e"))
            pdf.canvas.setLineWidth(12)
            pdf.canvas.circle(x + 72, top - 66, 34, stroke=1, fill=0)
            pdf.canvas.setLineWidth(1)
            pdf.canvas.setFont(pdf.bold_font, 20)
            pdf.canvas.drawCentredString(x + 72, top - 73, str(sum(counts.values())))
            pdf.canvas.setFont(pdf.regular_font, 8)
            pdf.canvas.setFillColor(pdf_hex("#536276"))
            pdf.canvas.drawString(x + 132, top - 48, f"통과 {counts['PASS']}")
            pdf.canvas.drawString(x + 132, top - 66, f"경고 {counts['WARNING']}")
            pdf.canvas.drawString(x + 132, top - 84, f"실패 {counts['FAIL']}")
        else:
            pdf.canvas.setFont(pdf.bold_font, 28)
            pdf.canvas.setFillColor(pdf_hex("#ff7a1a"))
            pdf.canvas.drawCentredString(x + card_w / 2, top - 70, grade)
            pdf.canvas.setFont(pdf.regular_font, 8)
            pdf.canvas.setFillColor(pdf_hex("#536276"))
            pdf.canvas.drawCentredString(x + card_w / 2, top - 92, report_grade_message(grade)[:38])
    pdf.y = top - 132
    pdf.heading("해결이 필요한 문제점", 13)
    for index, item in enumerate(report_top_issues(record, 4), start=1):
        pdf.ensure_space(28)
        y = pdf.y
        pdf.canvas.setFillColor(pdf_hex("#fff7ef"))
        pdf.canvas.setStrokeColor(pdf_hex("#ffd0b3"))
        pdf.canvas.roundRect(pdf.margin_x, y - 21, pdf.content_width, 25, 4, fill=1, stroke=1)
        pdf.canvas.setFont(pdf.bold_font, 9)
        pdf.canvas.setFillColor(pdf_hex("#ff7a1a"))
        pdf.canvas.drawString(pdf.margin_x + 10, y - 12, str(index))
        pdf.canvas.setFillColor(pdf_hex("#06132a"))
        pdf.canvas.drawString(pdf.margin_x + 32, y - 12, item_name(item)[:70])
        pdf.canvas.setFillColor(pdf_hex("#ff6b37"))
        pdf.canvas.drawRightString(pdf.width - pdf.margin_x - 10, y - 12, "즉시 조치 필요")
        pdf.y -= 30


def draw_reportlab_ars(pdf: ReportLabPdf, record: dict, premium: bool) -> None:
    reportlab_new_page(pdf, premium)
    score = int(record.get("score") or 0)
    current = max(1.0, min(10.0, round(score / 10, 1)))
    after = min(10.0, round(current + len(report_top_issues(record, 6)) * 0.5, 1))
    pdf.heading("ARS Score - 광고연관지수", 17)
    top = pdf.y
    pdf.ensure_space(105)
    for index, (label, value, color) in enumerate([("현재 ARS Score", current, "#f97316"), ("경고 해결 후 예상 ARS", after, "#22c55e")]):
        x = pdf.margin_x + index * ((pdf.content_width - 16) / 2 + 16)
        w = (pdf.content_width - 16) / 2
        pdf.canvas.setFillColor(pdf_hex("#ffffff"))
        pdf.canvas.setStrokeColor(pdf_hex("#dbe3eb"))
        pdf.canvas.roundRect(x, top - 80, w, 80, 6, fill=1, stroke=1)
        pdf.canvas.setFont(pdf.regular_font, 9)
        pdf.canvas.setFillColor(pdf_hex("#536276"))
        pdf.canvas.drawCentredString(x + w / 2, top - 25, label)
        pdf.canvas.setFont(pdf.bold_font, 27)
        pdf.canvas.setFillColor(pdf_hex(color))
        pdf.canvas.drawCentredString(x + w / 2, top - 56, f"{value} / 10")
    pdf.y = top - 105
    pdf.heading("ARS 산출 근거 카테고리", 13)
    for label, value, color in readiness_categories(record):
        pdf.ensure_space(24)
        y = pdf.y
        pdf.canvas.setFont(pdf.regular_font, 8)
        pdf.canvas.setFillColor(pdf_hex("#06132a"))
        pdf.canvas.drawString(pdf.margin_x, y - 6, label)
        pdf.canvas.drawRightString(pdf.margin_x + 175, y - 6, f"{value:.1f} / 100")
        pdf.canvas.setFillColor(pdf_hex("#edf2f7"))
        pdf.canvas.roundRect(pdf.margin_x + 190, y - 10, pdf.content_width - 190, 7, 3, fill=1, stroke=0)
        pdf.canvas.setFillColor(pdf_hex(color))
        pdf.canvas.roundRect(pdf.margin_x + 190, y - 10, (pdf.content_width - 190) * value / 100, 7, 3, fill=1, stroke=0)
        pdf.y -= 22


def draw_reportlab_grouped_results(pdf: ReportLabPdf, record: dict, premium: bool) -> None:
    reportlab_new_page(pdf, premium)
    pdf.heading(f"{len(report_items(record))}개 점검 항목 전체 결과", 17)
    for title, items in report_grouped_items(record):
        if title == "진단 제외":
            continue
        pdf.ensure_space(36)
        pdf.canvas.setFillColor(pdf_hex("#f4f7fa"))
        pdf.canvas.roundRect(pdf.margin_x, pdf.y - 22, pdf.content_width, 26, 4, fill=1, stroke=0)
        pdf.canvas.setFont(pdf.bold_font, 10)
        pdf.canvas.setFillColor(pdf_hex("#06132a"))
        pdf.canvas.drawString(pdf.margin_x + 10, pdf.y - 12, f"{title} ({len(items)}개)")
        pdf.y -= 30
        for item in items[:12]:
            pdf.ensure_space(18)
            pdf.canvas.setFont(pdf.regular_font, 8)
            pdf.canvas.setFillColor(pdf_hex("#06132a"))
            pdf.canvas.drawString(pdf.margin_x + 8, pdf.y - 5, item_name(item)[:76])
            pdf.status_badge(report_text(item.get("status"), ""), pdf.width - pdf.margin_x - 44, pdf.y - 4)
            pdf.y -= 17
        pdf.y -= 6


def draw_reportlab_glossary_action(pdf: ReportLabPdf, premium: bool) -> None:
    reportlab_new_page(pdf, premium)
    pdf.heading("필수 점검 항목 정의", 17)
    rows = [
        ("Meta Description", "검색결과 페이지 요약문", "페이지 콘텐츠 파악 가능"),
        ("title 태그", "검색결과 제목", "광고연관지수 핵심 신호"),
        ("H1 태그", "본문 최상위 제목", "문서 구조 파악"),
        ("Alt 속성", "이미지 대체 정보", "이미지 노출 및 접근성"),
        ("Open Graph", "SNS 공유 메타", "외부 채널 미리보기"),
        ("Schema.org", "구조화 마크업", "리치 스니펫 클릭률 강화"),
    ]
    for name, what, why in rows:
        pdf.ensure_space(24)
        pdf.canvas.setFont(pdf.bold_font, 8)
        pdf.canvas.setFillColor(pdf_hex("#06132a"))
        pdf.canvas.drawString(pdf.margin_x, pdf.y - 6, name)
        pdf.canvas.setFont(pdf.regular_font, 8)
        pdf.canvas.setFillColor(pdf_hex("#536276"))
        pdf.canvas.drawString(pdf.margin_x + 130, pdf.y - 6, what)
        pdf.canvas.drawString(pdf.margin_x + 310, pdf.y - 6, why)
        pdf.y -= 20
    reportlab_new_page(pdf, premium)
    pdf.heading("애드부스트 광고 운영 연계 액션플랜", 17)
    for title, copy in [
        ("광고 연관지수 개선", "메타 타이틀, 설명, 페이지 핵심 문구를 광고 키워드와 맞춥니다."),
        ("클릭 기대지수 개선", "소재 이미지와 확장소재의 품질을 개선합니다."),
        ("운영 활용", "동일 URL 키워드 연결도를 비교하고 저연관 조합을 제외합니다."),
        ("재진단", "개선 후 동일 URL을 재분석해 등급 및 경고 변화를 추적합니다."),
    ]:
        pdf.ensure_space(40)
        pdf.canvas.setFillColor(pdf_hex("#f8fbfd"))
        pdf.canvas.setStrokeColor(pdf_hex("#dbe3eb"))
        pdf.canvas.roundRect(pdf.margin_x, pdf.y - 34, pdf.content_width, 38, 5, fill=1, stroke=1)
        pdf.text(title, x=pdf.margin_x + 10, size=9, font=pdf.bold_font, max_width=pdf.content_width - 20)
        pdf.text(copy, x=pdf.margin_x + 10, size=8, color="#536276", max_width=pdf.content_width - 20)
        pdf.y -= 8


def render_report_pdf_with_reportlab(record: dict, report_type: ReportType, platform: str) -> bytes:
    premium = report_type == "premium"
    title = f"{'프리미엄 ' if premium else ''}진단보고서 - {report_host(report_text(record.get('url')))}"
    pdf = ReportLabPdf(title)
    counts = report_status_counts(record)
    url = report_text(record.get("url"))
    host = report_host(url)
    grade = report_text(record.get("grade"), "C")
    score = report_text(record.get("score"), "0")
    created_at = report_created_at(record)
    snapshot = report_render_snapshot(record)
    issue_count = counts["WARNING"] + counts["FAIL"]

    if premium:
        draw_reportlab_cover(pdf, record, platform)

    pdf.header(premium)
    pdf.heading("한눈에 보는 진단 결과" if premium else "진단보고서", 22)
    pdf.text(
        "프리미엄 리포트는 진단 결과를 광고 운영 관점의 개선 우선순위와 플랫폼 수정 가이드로 재구성합니다."
        if premium
        else "이 보고서는 네이버 ADVoost 검색 광고 연결 URL의 검색엔진 친화도를 분석한 결과입니다.",
        size=10,
        color="#536276",
    )
    pdf.text("수집 실패, 색인 실패, SEO 점검 필요 여부를 심층 진단하여 A에서 F까지의 등급을 제공합니다.", size=10, color="#536276")
    pdf.text("진단 도구는 웹사이트에 대한 전반적인 점검 결과를 제공하며 검색 광고 노출을 보장하지는 않습니다.", size=10, font=pdf.bold_font, color="#f00000")
    pdf.y -= 8
    pdf.heading(f"{host} 결과 점검하기", 15)

    overview_top = pdf.y
    center_x = pdf.margin_x + 92
    center_y = overview_top - 66
    pdf.ensure_space(190)
    pdf.canvas.setFillColor(pdf_hex("#fff9e7"))
    pdf.canvas.setStrokeColor(pdf_hex("#f5b400"))
    pdf.canvas.setLineWidth(5)
    pdf.canvas.circle(center_x, center_y, 58, stroke=1, fill=1)
    pdf.canvas.setFont(pdf.bold_font, 34)
    pdf.canvas.setFillColor(pdf_hex("#c65a00"))
    pdf.canvas.drawCentredString(center_x, center_y - 11, grade)
    pdf.canvas.setLineWidth(1)
    pdf.draw_image_frame(
        report_get(snapshot, "desktopScreenshot", "desktop_screenshot"),
        pdf.margin_x + 240,
        overview_top - 2,
        210,
        118,
        "데스크톱",
    )
    pdf.draw_image_frame(
        report_get(snapshot, "mobileScreenshot", "mobile_screenshot"),
        pdf.margin_x + 462,
        overview_top - 2,
        58,
        118,
        "모바일",
    )
    pdf.y = overview_top - 142
    pdf.text(report_grade_message(grade), x=pdf.margin_x + 8, size=11, font=pdf.bold_font, max_width=190)
    pdf.text(f"개선 필요 항목: {issue_count}", x=pdf.margin_x + 8, size=9, color="#0057b8", max_width=190)
    meta_y = overview_top - 136
    pdf.y = meta_y
    pdf.text(f"URL: {url}", x=pdf.margin_x + 240, size=8, color="#536276", max_width=pdf.content_width - 240)
    pdf.text(f"분석일시: {created_at}", x=pdf.margin_x + 240, size=8, color="#536276", max_width=pdf.content_width - 240)
    pdf.text(f"점수: {score} / 등급: {grade}", x=pdf.margin_x + 240, size=8, color="#536276", max_width=pdf.content_width - 240)
    pdf.y = overview_top - 190
    pdf.count_cards(counts)

    if premium:
        reportlab_new_page(pdf, premium)
        draw_reportlab_summary(pdf, record, premium)
        draw_reportlab_ars(pdf, record, premium)
        draw_reportlab_grouped_results(pdf, record, premium)

        reportlab_new_page(pdf, premium)
        pdf.heading("키워드 요약", 17)
        summary = report_keyword_summary(record)
        draw_keyword_rows(pdf, summary.get("singleRows") or [], "개별 키워드")
        draw_keyword_rows(pdf, summary.get("phraseRows") or [], "구문(Phrase) 키워드")

        reportlab_new_page(pdf, premium)
        pdf.heading("페이지 키워드 - 메타태그 매칭 분석", 17)
        rows = (summary.get("singleRows") or [])[:10]
        if not rows:
            pdf.text("키워드 데이터가 없습니다.", size=9, color="#6b7685")
        else:
            missing = sum(1 for row in rows if not keyword_title_ok(row) or not keyword_desc_ok(row))
            pdf.text(f"고빈도 키워드 중 {missing}개가 title 또는 meta description에 충분히 반영되지 않았습니다.", size=9, color="#d65a00")
            draw_keyword_rows(pdf, rows, "키워드 - 메타 정보 매칭 현황")

        reportlab_new_page(pdf, premium)
        pdf.heading("전체 점검 세부 내역", 17)
        for title_text, items in report_grouped_items(record):
            pdf.ensure_space(32)
            pdf.text(title_text, size=11, font=pdf.bold_font, color="#06132a")
            for item in sorted(items, key=lambda value: (REPORT_STATUS_SORT.get(report_text(value.get("status"), ""), 9), item_name(value))):
                pdf.item_block(item, premium=True)

        reportlab_new_page(pdf, premium)
        pdf.heading("프리미엄 플랫폼 수정 가이드", 17)
        pdf.text(f"선택 플랫폼: {platform}", size=10, font=pdf.bold_font)
        for item in report_top_issues(record, 4):
            pdf.item_block(item, premium=False)
        for guide_title, copy in platform_guide(platform):
            pdf.ensure_space(42)
            pdf.canvas.setFillColor(pdf_hex("#f8fbfd"))
            pdf.canvas.setStrokeColor(pdf_hex("#dbe3eb"))
            pdf.canvas.roundRect(pdf.margin_x, pdf.y - 36, pdf.content_width, 40, 5, fill=1, stroke=1)
            pdf.text(guide_title, x=pdf.margin_x + 10, size=9, font=pdf.bold_font, max_width=pdf.content_width - 20)
            pdf.text(copy, x=pdf.margin_x + 10, size=8, color="#536276", max_width=pdf.content_width - 20)
            pdf.y -= 8

        draw_reportlab_glossary_action(pdf, premium)
        reportlab_new_page(pdf, premium)
        pdf.heading("부록 (Appendix & References)", 17)
        pdf.text("SEO, SERP, CTR, CPC, ROAS, OG, JSON-LD, Schema.org 등 주요 용어를 내부 광고 운영 기준과 함께 해석합니다.", size=9, color="#536276")
        pdf.y -= 120
        pdf.heading("지금 개선하면, 광고 효율이 달라집니다.", 16)
        pdf.text("문의: mc2@nasmedia.co.kr", size=10, color="#536276")
    else:
        summary = report_keyword_summary(record)
        reportlab_new_page(pdf, premium)
        pdf.heading("키워드 요약", 17)
        draw_keyword_rows(pdf, summary.get("singleRows") or [], "개별 키워드")
        draw_keyword_rows(pdf, summary.get("phraseRows") or [], "구문(Phrase) 키워드")

        reportlab_new_page(pdf, premium)
        pdf.heading("전체 점검 세부 내역", 17)
        items = report_items(record)
        if not items:
            pdf.text("점검 항목이 없습니다.", size=10, color="#536276")
        for title_text, grouped in report_grouped_items(record):
            pdf.ensure_space(32)
            pdf.text(title_text, size=11, font=pdf.bold_font, color="#06132a")
            for item in sorted(grouped, key=lambda value: (REPORT_STATUS_SORT.get(report_text(value.get("status"), ""), 9), item_name(value))):
                pdf.item_block(item, premium=False)

    return pdf.finish()


def render_report_pdfs_with_reportlab(
    records: list[dict],
    report_type: ReportType,
    platform: str,
) -> list[tuple[str, bytes]]:
    if not reportlab_is_available():
        raise RuntimeError("ReportLab is not installed.")
    return [
        (report_filename(record, report_type), render_report_pdf_with_reportlab(record, report_type, platform))
        for record in records
    ]


def render_report_pdfs(records: list[dict], report_type: ReportType, platform: str) -> list[tuple[str, bytes]]:
    browser_error: Exception | None = None
    if sync_playwright is not None:
        try:
            return render_report_pdfs_with_browser(records, report_type, platform)
        except Exception as exc:
            browser_error = exc

    try:
        return render_report_pdfs_with_reportlab(records, report_type, platform)
    except Exception as fallback_error:
        detail = "PDF renderer failed."
        if browser_error is not None:
            detail += f" Browser: {browser_error.__class__.__name__}: {browser_error}"
        detail += f" Fallback: {fallback_error.__class__.__name__}: {fallback_error}"
        raise HTTPException(status_code=503, detail=detail) from fallback_error

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
