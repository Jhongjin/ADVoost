export type AuditStatus = "PASS" | "WARNING" | "FAIL" | "NOT_CHECKED";
export type AuditGrade = "A" | "B" | "C" | "D" | "F";
export type ItemSeverity = "critical" | "major" | "minor" | "info";

export interface AuditItem {
  id: string;
  itemName: string;
  category: string;
  status: AuditStatus;
  severity: ItemSeverity;
  criticalForGrade?: boolean;
  description: string;
  guide: string;
  detectedValue?: string;
  remediation?: string;
  details?: string[];
  snippet?: string;
}

export interface KeywordRow {
  keyword: string;
  frequency: number;
  ratio: string;
  titleOk: boolean;
  descOk: boolean;
}

export interface KeywordSummary {
  singleTotal: number;
  phraseTotal: number;
  singleRows: KeywordRow[];
  phraseRows: KeywordRow[];
}

export interface RenderSnapshot {
  success: boolean;
  finalUrl?: string;
  loadTimeMs?: number;
  domContentLoadedMs?: number;
  firstContentfulPaintMs?: number;
  resourceCount: number;
  failedRequestCount: number;
  consoleErrorCount: number;
  blockedResourceCount: number;
  slowResources: string[];
  failedRequests: string[];
  consoleErrors: string[];
  desktopScreenshot?: string;
  mobileScreenshot?: string;
  error?: string;
}

export interface ApiAuditItem {
  id: string;
  item_name: string;
  category: string;
  status: AuditStatus;
  severity: ItemSeverity;
  critical_for_grade?: boolean;
  description: string;
  guide: string;
  detected_value?: string | null;
  remediation?: string | null;
  details?: string[] | null;
  snippet?: string | null;
}

export interface ApiKeywordRow {
  keyword: string;
  frequency: number;
  ratio: string;
  title_tag: boolean;
  meta_description: boolean;
}

export interface ApiKeywordSummary {
  single_total: number;
  phrase_total: number;
  single_rows: ApiKeywordRow[];
  phrase_rows: ApiKeywordRow[];
}

export interface ApiRenderSnapshot {
  success: boolean;
  final_url?: string | null;
  load_time_ms?: number | null;
  dom_content_loaded_ms?: number | null;
  first_contentful_paint_ms?: number | null;
  resource_count: number;
  failed_request_count: number;
  console_error_count: number;
  blocked_resource_count: number;
  slow_resources: string[];
  failed_requests: string[];
  console_errors: string[];
  desktop_screenshot?: string | null;
  mobile_screenshot?: string | null;
  error?: string | null;
}

export interface ApiAuditResponse {
  id: string;
  url: string;
  manager_name: string;
  advertiser_name: string;
  grade: AuditGrade;
  score: number;
  items: ApiAuditItem[];
  keyword_summary?: ApiKeywordSummary | null;
  render_snapshot?: ApiRenderSnapshot | null;
  cache_hit: boolean;
  created_at: string;
  duration_sec: number;
}

export interface AuditRecord {
  id: string;
  url: string;
  managerName: string;
  advertiserName: string;
  grade: AuditGrade;
  score: number;
  createdAt: string;
  durationSec: number;
  status: "완료" | "처리중" | "실패";
  items: AuditItem[];
  keywordSummary?: KeywordSummary;
  renderSnapshot?: RenderSnapshot;
}

export interface ManagedUrl {
  id: string;
  url: string;
  advertiserName: string;
  managerName: string;
  lastGrade?: AuditGrade;
  lastAuditedAt?: string;
  memo?: string;
}

export interface SupportTicket {
  id: string;
  auditId: string;
  url: string;
  advertiserName: string;
  status: "접수" | "해결중" | "해결";
  requestedAt: string;
  priority: "낮음" | "보통" | "높음";
}

const now = new Date("2026-06-08T15:50:00+09:00");

const minusHours = (hours: number) =>
  new Date(now.getTime() - hours * 60 * 60 * 1000).toISOString();

export function normalizeUrl(raw: string) {
  const trimmed = raw.trim();
  if (!trimmed) {
    return "";
  }
  if (/^https?:\/\//i.test(trimmed)) {
    return trimmed;
  }
  return `https://${trimmed}`;
}

export function formatDate(value: string) {
  const date = new Date(value);
  const month = `${date.getMonth() + 1}`.padStart(2, "0");
  const day = `${date.getDate()}`.padStart(2, "0");
  const hours = date.getHours();
  const minutes = `${date.getMinutes()}`.padStart(2, "0");
  const period = hours >= 12 ? "오후" : "오전";
  const hour12 = `${hours % 12 || 12}`.padStart(2, "0");
  return `${month}. ${day}. ${period} ${hour12}:${minutes}`;
}

export function formatFullDate(value: string) {
  const date = new Date(value);
  const year = date.getFullYear();
  return `${year}. ${formatDate(value)}`;
}

export function getStatusLabel(status: AuditStatus) {
  const labels: Record<AuditStatus, string> = {
    PASS: "통과",
    WARNING: "경고",
    FAIL: "실패",
    NOT_CHECKED: "점검불가",
  };
  return labels[status];
}

export function getGradeMessage(grade: AuditGrade) {
  const messages: Record<AuditGrade, string> = {
    A: "즉시 광고 집행 가능한 최적화 상태",
    B: "마이너 권장 항목 보완 필요",
    C: "수집은 가능하나 효율 저하 가능성 높음",
    D: "검색 봇 오독 위험이 큰 구조",
    F: "수집 실패 또는 인덱싱 불가 위험",
  };
  return messages[grade];
}

export function countItems(items: AuditItem[]) {
  return items.reduce(
    (acc, item) => {
      acc[item.status] += 1;
      return acc;
    },
    { PASS: 0, WARNING: 0, FAIL: 0, NOT_CHECKED: 0 } as Record<
      AuditStatus,
      number
    >,
  );
}

export function getAuditGroup(item: AuditItem) {
  const collectionIds = new Set([
    "http-status",
    "robots",
    "html-parse",
    "meta-robots",
    "noscript",
  ]);
  const indexIds = new Set(["canonical", "ssl", "external-links"]);
  const excludedIds = new Set(["title-length"]);

  if (excludedIds.has(item.id) || item.status === "NOT_CHECKED") {
    return "진단 제외";
  }
  if (collectionIds.has(item.id) || item.category === "수집") {
    return "수집 점검항목";
  }
  if (indexIds.has(item.id)) {
    return "색인 점검항목";
  }
  return "SEO 점검항목";
}

export function countAuditGroups(items: AuditItem[]) {
  return items.reduce(
    (acc, item) => {
      const group = getAuditGroup(item);
      acc[group] += 1;
      return acc;
    },
    {
      "SEO 점검항목": 0,
      "색인 점검항목": 0,
      "수집 점검항목": 0,
      "진단 제외": 0,
    } as Record<ReturnType<typeof getAuditGroup>, number>,
  );
}

export function getCriticalWarnings(items: AuditItem[]) {
  return items.filter(
    (item) =>
      item.status === "WARNING" &&
      (item.criticalForGrade || item.severity === "critical"),
  );
}

export function apiResponseToRecord(response: ApiAuditResponse): AuditRecord {
  const mapKeywordRow = (row: ApiKeywordRow): KeywordRow => ({
    keyword: row.keyword,
    frequency: row.frequency,
    ratio: row.ratio,
    titleOk: row.title_tag,
    descOk: row.meta_description,
  });
  const mapRenderSnapshot = (
    snapshot: ApiRenderSnapshot,
  ): RenderSnapshot => ({
    success: snapshot.success,
    finalUrl: snapshot.final_url ?? undefined,
    loadTimeMs: snapshot.load_time_ms ?? undefined,
    domContentLoadedMs: snapshot.dom_content_loaded_ms ?? undefined,
    firstContentfulPaintMs: snapshot.first_contentful_paint_ms ?? undefined,
    resourceCount: snapshot.resource_count,
    failedRequestCount: snapshot.failed_request_count,
    consoleErrorCount: snapshot.console_error_count,
    blockedResourceCount: snapshot.blocked_resource_count,
    slowResources: snapshot.slow_resources,
    failedRequests: snapshot.failed_requests,
    consoleErrors: snapshot.console_errors,
    desktopScreenshot: snapshot.desktop_screenshot ?? undefined,
    mobileScreenshot: snapshot.mobile_screenshot ?? undefined,
    error: snapshot.error ?? undefined,
  });

  return {
    id: response.id,
    url: response.url,
    managerName: response.manager_name || "미지정",
    advertiserName: response.advertiser_name || "미지정 광고주",
    grade: response.grade,
    score: response.score,
    createdAt: response.created_at,
    durationSec: response.duration_sec,
    status: response.grade === "F" ? "실패" : "완료",
    keywordSummary: response.keyword_summary
      ? {
          singleTotal: response.keyword_summary.single_total,
          phraseTotal: response.keyword_summary.phrase_total,
          singleRows: response.keyword_summary.single_rows.map(mapKeywordRow),
          phraseRows: response.keyword_summary.phrase_rows.map(mapKeywordRow),
        }
      : undefined,
    renderSnapshot: response.render_snapshot
      ? mapRenderSnapshot(response.render_snapshot)
      : undefined,
    items: response.items.map((item) => ({
      id: item.id,
      itemName: item.item_name,
      category: item.category,
      status: item.status,
      severity: item.severity,
      criticalForGrade: item.critical_for_grade,
      description: item.description,
      guide: item.guide,
      detectedValue: item.detected_value ?? undefined,
      remediation: item.remediation ?? undefined,
      details: item.details ?? undefined,
      snippet: item.snippet ?? undefined,
    })),
  };
}

export function calculateSeoGrade(items: AuditItem[]) {
  const failItems = items.filter((item) => item.status === "FAIL");
  const warningItems = items.filter((item) => item.status === "WARNING");
  const criticalWarningItems = warningItems.filter(
    (item) => item.criticalForGrade || item.severity === "critical",
  );
  const hasCollectionFail = failItems.some((item) => item.category === "수집");
  const penalty = items.reduce((sum, item) => {
    if (item.status === "FAIL") {
      return sum + 15;
    }
    if (item.status === "WARNING") {
      return sum + (item.severity === "minor" ? 2 : 5);
    }
    return sum;
  }, 0);
  const score = Math.max(0, 100 - penalty);

  if (hasCollectionFail || failItems.length >= 7) {
    return { grade: "F" as AuditGrade, score };
  }
  if (failItems.length >= 4) {
    return { grade: "D" as AuditGrade, score };
  }
  if (
    failItems.length > 0 ||
    criticalWarningItems.length > 0 ||
    warningItems.length >= 6
  ) {
    return { grade: "C" as AuditGrade, score };
  }
  if (score >= 90 && failItems.length === 0 && warningItems.length <= 2) {
    return { grade: "A" as AuditGrade, score };
  }
  if (score >= 80 && failItems.length === 0 && warningItems.length <= 5) {
    return { grade: "B" as AuditGrade, score };
  }
  if (score >= 65 || failItems.length <= 3) {
    return { grade: "C" as AuditGrade, score };
  }
  if (score >= 50) {
    return { grade: "D" as AuditGrade, score };
  }
  return { grade: "F" as AuditGrade, score };
}

const baseChecklist: AuditItem[] = [
  {
    id: "http-status",
    itemName: "URL 접속 여부",
    category: "수집",
    status: "PASS",
    severity: "critical",
    description: "HTTP 응답 코드가 정상이며 HTML 문서가 반환되었습니다.",
    guide: "서버 오류, 리다이렉트 루프, 인증 차단 여부를 우선 확인하세요.",
  },
  {
    id: "robots",
    itemName: "robots.txt 권한",
    category: "수집",
    status: "PASS",
    severity: "critical",
    description: "검색 봇 접근을 차단하는 robots 규칙이 발견되지 않았습니다.",
    guide: "User-agent 전체 차단 규칙이 있으면 검색 광고 랜딩 분석이 실패할 수 있습니다.",
  },
  {
    id: "html-parse",
    itemName: "HTML 파싱 가능 여부",
    category: "수집",
    status: "PASS",
    severity: "critical",
    description: "HTML 문서 구조를 정상적으로 파싱했습니다.",
    guide: "비표준 렌더링 또는 과도한 스크립트 의존 구조를 줄이세요.",
  },
  {
    id: "title-present",
    itemName: "title 태그 존재",
    category: "메타",
    status: "PASS",
    severity: "major",
    description: "문서 제목이 명확하게 선언되어 있습니다.",
    guide: "핵심 상품명과 브랜드, 대표 키워드를 60자 안팎으로 구성하세요.",
  },
  {
    id: "title-length",
    itemName: "title 길이",
    category: "메타",
    status: "PASS",
    severity: "minor",
    description: "검색 결과에서 잘리지 않을 범위의 제목입니다.",
    guide: "너무 짧거나 긴 제목은 랜딩 관련도 판단을 약화시킬 수 있습니다.",
  },
  {
    id: "description",
    itemName: "meta description",
    category: "메타",
    status: "PASS",
    severity: "major",
    description: "요약 설명 메타 태그가 존재합니다.",
    guide: "상품 가치와 구매 전환 문맥을 80~160자 수준으로 요약하세요.",
  },
  {
    id: "meta-robots",
    itemName: "meta robots",
    category: "수집",
    status: "PASS",
    severity: "critical",
    description: "noindex/nofollow 지시어가 없습니다.",
    guide: "광고 랜딩 페이지에는 noindex가 적용되지 않도록 배포 설정을 점검하세요.",
  },
  {
    id: "canonical",
    itemName: "canonical URL",
    category: "메타",
    status: "PASS",
    severity: "major",
    description: "대표 URL을 지정했습니다.",
    guide: "중복 랜딩이 많은 쇼핑몰은 canonical을 일관되게 유지하세요.",
  },
  {
    id: "h1-present",
    itemName: "H1 태그 존재",
    category: "콘텐츠",
    status: "PASS",
    severity: "major",
    description: "페이지의 대표 제목을 H1으로 선언했습니다.",
    guide: "H1은 한 페이지의 핵심 주제와 랜딩 목적을 직접 표현해야 합니다.",
  },
  {
    id: "h1-count",
    itemName: "H1 중복 여부",
    category: "콘텐츠",
    status: "PASS",
    severity: "minor",
    description: "H1 태그 수가 안정적입니다.",
    guide: "중복 H1은 정보 구조 해석을 어렵게 만들 수 있습니다.",
  },
  {
    id: "viewport",
    itemName: "모바일 viewport",
    category: "모바일",
    status: "PASS",
    severity: "major",
    description: "모바일 뷰포트 메타 태그가 존재합니다.",
    guide: "모바일 광고 유입 비중이 높다면 viewport 선언은 필수입니다.",
  },
  {
    id: "charset",
    itemName: "문자 인코딩",
    category: "기술",
    status: "PASS",
    severity: "minor",
    description: "문자 인코딩 선언이 존재합니다.",
    guide: "UTF-8 선언을 유지해 한글 제목과 설명이 깨지지 않게 하세요.",
  },
  {
    id: "html-lang",
    itemName: "html lang 속성",
    category: "기술",
    status: "PASS",
    severity: "minor",
    description: "문서 언어가 선언되어 있습니다.",
    guide: "한국어 랜딩은 html lang=\"ko\" 선언을 권장합니다.",
  },
  {
    id: "og-title",
    itemName: "OG title",
    category: "소셜",
    status: "PASS",
    severity: "major",
    description: "오픈그래프 제목이 선언되어 있습니다.",
    guide: "공유/미리보기 환경에서도 랜딩 메시지가 일관되게 보이도록 유지하세요.",
  },
  {
    id: "og-description",
    itemName: "OG description",
    category: "소셜",
    status: "PASS",
    severity: "major",
    description: "오픈그래프 설명이 선언되어 있습니다.",
    guide: "OG 설명은 검색 광고 소재와 충돌하지 않는 문구로 관리하세요.",
  },
  {
    id: "og-image",
    itemName: "OG image",
    category: "소셜",
    status: "PASS",
    severity: "major",
    description: "오픈그래프 이미지가 선언되어 있습니다.",
    guide: "1200x630 비율의 대표 이미지를 지정하면 공유 품질이 안정됩니다.",
  },
  {
    id: "render-blocked-resources",
    itemName: "접근이 제한된 리소스 존재",
    category: "SEO 점검항목",
    status: "PASS",
    severity: "critical",
    criticalForGrade: true,
    description: "렌더링을 차단하는 스크립트/스타일시트가 발견되지 않았습니다.",
    guide: "필수 CSS/JS는 정상 응답하도록 유지하고 비동기 로딩을 적용하세요.",
  },
  {
    id: "image-alt",
    itemName: "이미지 alt 속성",
    category: "콘텐츠",
    status: "PASS",
    severity: "minor",
    description: "주요 이미지에 대체 텍스트가 존재합니다.",
    guide: "상품 핵심 속성, 브랜드명, 사용 맥락을 alt에 반영하세요.",
  },
  {
    id: "download-time",
    itemName: "다운로드 소요 시간이 긴 페이지",
    category: "성능",
    status: "PASS",
    severity: "critical",
    criticalForGrade: true,
    description: "페이지 다운로드 시간이 권장 기준 안에 있습니다.",
    guide: "페이지 로딩 시간이 3초를 초과하면 서버 응답 속도와 리소스를 최적화하세요.",
  },
  {
    id: "structured-data",
    itemName: "구조화 데이터",
    category: "콘텐츠",
    status: "PASS",
    severity: "major",
    description: "JSON-LD 구조화 데이터가 발견되지 않았습니다.",
    guide: "상품/조직/FAQ 스키마를 랜딩 성격에 맞게 추가하세요.",
  },
  {
    id: "heading-order",
    itemName: "헤딩 계층",
    category: "콘텐츠",
    status: "PASS",
    severity: "minor",
    description: "헤딩 순서가 안정적으로 구성되어 있습니다.",
    guide: "H2/H3는 정보 탐색 순서와 구매 흐름에 맞추세요.",
  },
  {
    id: "internal-links",
    itemName: "내부 링크",
    category: "콘텐츠",
    status: "PASS",
    severity: "minor",
    description: "내부 탐색 링크가 충분합니다.",
    guide: "상세 정보, 리뷰, FAQ 등 전환 보조 링크를 적절히 배치하세요.",
  },
  {
    id: "external-links",
    itemName: "외부 링크 안정성",
    category: "기술",
    status: "PASS",
    severity: "minor",
    description: "외부 링크 상태가 안정적입니다.",
    guide: "리포트 생성 워커에서 별도 링크 체커를 연결하세요.",
  },
  {
    id: "favicon",
    itemName: "favicon",
    category: "브랜드",
    status: "PASS",
    severity: "minor",
    description: "브랜드 아이콘이 설정되어 있습니다.",
    guide: "탭/공유 환경에서 브랜드 식별성이 유지되도록 아이콘을 관리하세요.",
  },
  {
    id: "page-size",
    itemName: "페이지 용량",
    category: "성능",
    status: "PASS",
    severity: "minor",
    description: "초기 HTML 용량이 권장 범위 안에 있습니다.",
    guide: "과도한 inline script, base64 이미지, 중복 CSS를 줄이세요.",
  },
  {
    id: "script-error",
    itemName: "크리티컬 스크립트 에러",
    category: "기술",
    status: "PASS",
    severity: "critical",
    description: "크리티컬 스크립트 오류가 발견되지 않았습니다.",
    guide: "Puppeteer 워커에서 console error와 hydration error를 수집하세요.",
  },
  {
    id: "broken-links",
    itemName: "깨진 링크",
    category: "기술",
    status: "PASS",
    severity: "minor",
    description: "깨진 링크가 발견되지 않았습니다.",
    guide: "대량 URL 분석 워커에 링크 재시도 정책을 두세요.",
  },
  {
    id: "tap-target",
    itemName: "모바일 터치 영역",
    category: "모바일",
    status: "PASS",
    severity: "minor",
    description: "모바일 터치 영역이 권장 기준을 충족합니다.",
    guide: "주요 CTA의 최소 터치 영역을 44px 이상으로 유지하세요.",
  },
  {
    id: "ssl",
    itemName: "HTTPS 보안",
    category: "기술",
    status: "PASS",
    severity: "major",
    description: "HTTPS URL로 접근됩니다.",
    guide: "HTTP 랜딩은 광고 유입 이탈과 브라우저 경고를 유발할 수 있습니다.",
  },
  {
    id: "form-labels",
    itemName: "폼 접근성",
    category: "전환",
    status: "PASS",
    severity: "minor",
    description: "폼 접근성 기본 조건이 충족됩니다.",
    guide: "상담/구매 폼의 label, autocomplete, 오류 메시지를 점검하세요.",
  },
  {
    id: "noscript",
    itemName: "noscript 대체 콘텐츠",
    category: "수집",
    status: "PASS",
    severity: "minor",
    description: "스크립트 비활성 환경 대체 콘텐츠가 제한적입니다.",
    guide: "핵심 상품명과 설명은 서버 HTML에도 남기세요.",
  },
  {
    id: "keyword-density",
    itemName: "키워드 빈도",
    category: "콘텐츠",
    status: "PASS",
    severity: "minor",
    description: "대표 키워드가 본문에 자연스럽게 분포합니다.",
    guide: "반복 삽입보다 문맥형 설명과 FAQ 구조를 우선하세요.",
  },
];

function checklistWith(
  overrides: Record<string, Partial<AuditItem>>,
): AuditItem[] {
  return baseChecklist.map((item) => ({
    ...item,
    ...(overrides[item.id] ?? {}),
  }));
}

export const seedRecords: AuditRecord[] = [
  {
    id: "AUD-260605-1738",
    url: "https://blog.nasmedia.co.kr/",
    managerName: "-",
    advertiserName: "나스미디어",
    grade: "C",
    score: 83,
    createdAt: minusHours(80),
    durationSec: 10.6,
    status: "완료",
    items: checklistWith({
      "title-present": {
        status: "WARNING",
        severity: "critical",
        criticalForGrade: true,
        description: "<title> 요소가 없거나 빈 문자열입니다.",
        detectedValue: "(빈 문자열)",
        remediation: "meta.title이 없거나 빈 문자열입니다. 페이지 타이틀을 설정하세요.",
        snippet: "<title>\n\n</title>",
      },
      "title-length": {
        status: "NOT_CHECKED",
        severity: "info",
        description: "title이 비어 있어 텍스트 길이는 점검하지 않았습니다.",
        detectedValue: "점검 불가",
        remediation: "title 값이 비어 있어 길이를 자동 산정하지 않았습니다.",
        snippet: "<title>\n\n</title>",
      },
      "image-alt": {
        status: "WARNING",
        description: "alt가 비어 있는 이미지가 17개 발견되었습니다.",
        detectedValue: "alt 누락 이미지 존재 (17개)",
        remediation: "이미지에 alt 속성이 없습니다. 모든 이미지에 의미 있는 alt 텍스트를 추가하세요.",
        snippet:
          '<img loading="lazy" src="//i1.daumcdn.net/thumb/C960x540.fwebp.q90/" alt="">\n<img loading="lazy" src="//i1.daumcdn.net/thumb/C960x540.fwebp.q90/" alt="">\n<img loading="lazy" src="//i1.daumcdn.net/thumb/C960x540.fwebp.q90/" alt="">',
      },
      "render-blocked-resources": {
        status: "WARNING",
        description: "렌더링을 차단하는 스크립트/스타일시트가 있습니다.",
        detectedValue: "렌더 차단 리소스 존재",
        remediation: "렌더링을 차단하는 스크립트/스타일시트가 있습니다. 비동기 로딩을 적용하세요.",
        snippet: '<link rel="stylesheet" href="https://t1.daumcdn.net/tistory_admin/assets/style.css">',
      },
      "download-time": {
        status: "WARNING",
        description: "페이지 다운로드 시간이 3초를 초과했습니다.",
        detectedValue: "10575 ms",
        remediation: "페이지 로딩 시간이 3초를 초과합니다. 서버 응답 속도와 리소스를 최적화하세요.",
        snippet: "10575 ms",
      },
      description: {
        status: "PASS",
        description: "meta description이 설정되어 있습니다.",
        detectedValue: "kt nasmedia의 공식 블로그입니다. 최신 디지털 마케팅 정보와 트렌드를 만나보세요.",
        remediation: "메타 description이 설정되어 있습니다.",
        snippet: '<meta name="description" content="케이티 나스미디어의 인사이트를 전합니다.">',
      },
    }),
  },
  {
    id: "AUD-260608-0917",
    url: "https://brand-alpha.co.kr/product/serum",
    managerName: "김서연",
    advertiserName: "브랜드알파",
    grade: "A",
    score: 91,
    createdAt: minusHours(1.7),
    durationSec: 126,
    status: "완료",
    items: checklistWith({}),
  },
  {
    id: "AUD-260608-0841",
    url: "https://shop-nova.kr/landing/summer",
    managerName: "박민준",
    advertiserName: "노바스토어",
    grade: "C",
    score: 68,
    createdAt: minusHours(4.2),
    durationSec: 171,
    status: "완료",
    items: checklistWith({
      "title-present": {
        status: "FAIL",
        description: "title 태그가 비어 있어 검색 봇이 대표 주제를 파악하기 어렵습니다.",
        snippet: "<title></title>",
      },
      viewport: {
        status: "WARNING",
        description: "모바일 viewport 선언이 누락되었습니다.",
        snippet: "<head><!-- viewport meta not found --></head>",
      },
      "image-alt": {
        status: "WARNING",
        description: "alt가 비어 있는 이미지가 9개 발견되었습니다.",
        snippet: '<img src="/assets/product-main.jpg" alt="">',
      },
      "h1-count": {
        status: "WARNING",
        description: "H1 태그가 3개 발견되었습니다.",
        snippet: "<h1>SUMMER SALE</h1><h1>BEST PRICE</h1>",
      },
      "og-image": {
        status: "WARNING",
        description: "OG image 메타 태그가 누락되었습니다.",
        snippet: '<meta property="og:image" content="">',
      },
    }),
  },
  {
    id: "AUD-260607-1904",
    url: "https://legacy-market.kr/event",
    managerName: "이도윤",
    advertiserName: "레거시마켓",
    grade: "F",
    score: 0,
    createdAt: minusHours(20),
    durationSec: 38,
    status: "실패",
    items: checklistWith({
      "http-status": {
        status: "FAIL",
        description: "서버가 403 Forbidden 응답을 반환했습니다.",
        snippet: "HTTP/1.1 403 Forbidden",
      },
      robots: {
        status: "FAIL",
        description: "robots.txt에서 전체 크롤러 접근을 제한합니다.",
        snippet: "User-agent: *\nDisallow: /",
      },
      "html-parse": {
        status: "FAIL",
        description: "차단 페이지 외 실제 랜딩 HTML을 수집하지 못했습니다.",
        snippet: "<html><title>Access Denied</title></html>",
      },
    }),
  },
  {
    id: "AUD-260606-1740",
    url: "https://care-lab.kr/reservation",
    managerName: "정유진",
    advertiserName: "케어랩",
    grade: "D",
    score: 47,
    createdAt: minusHours(46),
    durationSec: 214,
    status: "완료",
    items: checklistWith({
      description: {
        status: "WARNING",
        snippet: '<meta name="description" content="">',
        description: "meta description이 비어 있습니다.",
      },
      canonical: {
        status: "WARNING",
        snippet: "<!-- canonical not found -->",
        description: "canonical URL이 누락되었습니다.",
      },
      "h1-present": {
        status: "FAIL",
        snippet: "<main><section>상담 예약</section></main>",
        description: "대표 H1 태그가 없습니다.",
      },
      viewport: {
        status: "FAIL",
        snippet: "<head><meta name=\"viewport\" content=\"width=980\"></head>",
        description: "고정 폭 viewport가 모바일 랜딩 경험을 저하시킵니다.",
      },
      "og-title": {
        status: "WARNING",
        snippet: "<!-- og:title not found -->",
        description: "OG title이 누락되었습니다.",
      },
      "og-description": {
        status: "WARNING",
        snippet: "<!-- og:description not found -->",
        description: "OG description이 누락되었습니다.",
      },
      "og-image": {
        status: "WARNING",
        snippet: "<!-- og:image not found -->",
        description: "OG image가 누락되었습니다.",
      },
      "image-alt": {
        status: "WARNING",
        snippet: '<img src="/banner.jpg">',
        description: "alt 속성이 없는 이미지가 17개 발견되었습니다.",
      },
      "structured-data": {
        status: "FAIL",
        snippet: "<!-- JSON-LD not found -->",
        description: "예약/조직 구조화 데이터가 없습니다.",
      },
      "page-size": {
        status: "FAIL",
        snippet: "<!-- transferred: 2.7MB -->",
        description: "초기 페이지 전송량이 2MB를 초과했습니다.",
      },
    }),
  },
];

export const seedManagedUrls: ManagedUrl[] = [
  {
    id: "URL-000",
    url: "https://blog.nasmedia.co.kr/",
    advertiserName: "나스미디어",
    managerName: "-",
    lastGrade: "C",
    lastAuditedAt: minusHours(80),
    memo: "ADVoost 기준 캘리브레이션 URL",
  },
  {
    id: "URL-001",
    url: "https://brand-alpha.co.kr/product/serum",
    advertiserName: "브랜드알파",
    managerName: "김서연",
    lastGrade: "A",
    lastAuditedAt: minusHours(1.7),
    memo: "주력 세럼 랜딩",
  },
  {
    id: "URL-002",
    url: "shop-nova.kr/landing/summer",
    advertiserName: "노바스토어",
    managerName: "박민준",
    lastGrade: "C",
    lastAuditedAt: minusHours(4.2),
    memo: "여름 기획전",
  },
  {
    id: "URL-003",
    url: "https://legacy-market.kr/event",
    advertiserName: "레거시마켓",
    managerName: "이도윤",
    lastGrade: "F",
    lastAuditedAt: minusHours(20),
    memo: "방화벽 확인 필요",
  },
  {
    id: "URL-004",
    url: "https://care-lab.kr/reservation",
    advertiserName: "케어랩",
    managerName: "정유진",
    lastGrade: "D",
    lastAuditedAt: minusHours(46),
    memo: "예약 폼 리뉴얼 예정",
  },
  {
    id: "URL-005",
    url: "https://freshday.kr/store",
    advertiserName: "프레시데이",
    managerName: "한지우",
    memo: "신규 등록",
  },
  {
    id: "URL-006",
    url: "https://www.nasmedia.co.kr/",
    advertiserName: "-",
    managerName: "-",
    lastGrade: "C",
    lastAuditedAt: minusHours(80),
  },
  {
    id: "URL-007",
    url: "https://2an.co.kr/",
    advertiserName: "-",
    managerName: "-",
    lastGrade: "C",
    lastAuditedAt: minusHours(105),
  },
  {
    id: "URL-008",
    url: "https://www.nhsec.com/",
    advertiserName: "-",
    managerName: "-",
    lastGrade: "F",
    lastAuditedAt: minusHours(133),
  },
  {
    id: "URL-009",
    url: "https://iquve.co.kr/ko",
    advertiserName: "-",
    managerName: "-",
    lastGrade: "B",
    lastAuditedAt: minusHours(133.2),
  },
  {
    id: "URL-010",
    url: "http://www.doctor-ag.com/",
    advertiserName: "-",
    managerName: "-",
    lastGrade: "C",
    lastAuditedAt: minusHours(133.3),
  },
  {
    id: "URL-011",
    url: "https://www.meta-m.co.kr/",
    advertiserName: "-",
    managerName: "-",
    lastGrade: "C",
    lastAuditedAt: minusHours(133.4),
  },
  {
    id: "URL-012",
    url: "https://dealer.porsche.com/kr/deutschauto",
    advertiserName: "-",
    managerName: "-",
    lastGrade: "C",
    lastAuditedAt: minusHours(133.5),
  },
  {
    id: "URL-013",
    url: "https://www.kg-mobility.com/",
    advertiserName: "-",
    managerName: "-",
    lastGrade: "F",
    lastAuditedAt: minusHours(133.6),
  },
  {
    id: "URL-014",
    url: "http://eggpogjug.co.kr/",
    advertiserName: "-",
    managerName: "-",
    lastAuditedAt: minusHours(134),
  },
  {
    id: "URL-015",
    url: "http://life-kimchi.co.kr/",
    advertiserName: "-",
    managerName: "-",
    lastAuditedAt: minusHours(134.1),
  },
  {
    id: "URL-016",
    url: "http://jejuhalmeong.co.kr/",
    advertiserName: "-",
    managerName: "-",
    lastGrade: "D",
    lastAuditedAt: minusHours(134.2),
  },
  {
    id: "URL-017",
    url: "http://lifekalguksu.co.kr/",
    advertiserName: "-",
    managerName: "-",
    lastAuditedAt: minusHours(134.3),
  },
  {
    id: "URL-018",
    url: "https://lifenengmyeon.co.kr/",
    advertiserName: "-",
    managerName: "-",
    lastAuditedAt: minusHours(134.4),
  },
  {
    id: "URL-019",
    url: "https://eggbomb.co.kr/",
    advertiserName: "-",
    managerName: "-",
    lastAuditedAt: minusHours(134.5),
  },
  {
    id: "URL-020",
    url: "https://kimsamgu.co.kr/",
    advertiserName: "-",
    managerName: "-",
    lastAuditedAt: minusHours(134.6),
  },
  {
    id: "URL-021",
    url: "http://yuksik.co.kr/",
    advertiserName: "-",
    managerName: "-",
    lastAuditedAt: minusHours(134.7),
  },
  {
    id: "URL-022",
    url: "https://bapgujung.co.kr/",
    advertiserName: "-",
    managerName: "-",
    lastAuditedAt: minusHours(134.8),
  },
  {
    id: "URL-023",
    url: "https://byc.co.kr/goods/goods_view.php?goodsNo=10692&inflow=naver",
    advertiserName: "-",
    managerName: "-",
    lastAuditedAt: minusHours(135),
  },
];

export const seedTickets: SupportTicket[] = [
  {
    id: "TS-1042",
    auditId: "AUD-260608-0841",
    url: "https://shop-nova.kr/landing/summer",
    advertiserName: "노바스토어",
    status: "해결중",
    requestedAt: minusHours(3.5),
    priority: "보통",
  },
  {
    id: "TS-1037",
    auditId: "AUD-260606-1740",
    url: "https://care-lab.kr/reservation",
    advertiserName: "케어랩",
    status: "접수",
    requestedAt: minusHours(22),
    priority: "높음",
  },
  {
    id: "TS-1019",
    auditId: "AUD-260604-1103",
    url: "https://mellow-fit.kr/detail",
    advertiserName: "멜로핏",
    status: "해결",
    requestedAt: minusHours(80),
    priority: "낮음",
  },
];

export function createAuditRecord(
  rawUrl: string,
  managerName: string,
  advertiserName: string,
): AuditRecord {
  const url = normalizeUrl(rawUrl);
  const nasmediaCalibration = /blog\.nasmedia\.co\.kr/i.test(url);
  const risky = /legacy|blocked|403|firewall/i.test(url);
  const needsWork = /care|reservation|old|event|summer/i.test(url);
  const lightWarnings = /shop|store|landing/i.test(url);
  const id = `AUD-${Date.now().toString().slice(-10)}`;

  const overrides: Record<string, Partial<AuditItem>> = {};
  if (nasmediaCalibration) {
    overrides["title-present"] = {
      status: "WARNING",
      severity: "critical",
      criticalForGrade: true,
      description: "<title> 요소가 없거나 빈 문자열입니다.",
      detectedValue: "(빈 문자열)",
      remediation: "meta.title이 없거나 빈 문자열입니다. 페이지 타이틀을 설정하세요.",
      snippet: "<title>\n\n</title>",
    };
    overrides["title-length"] = {
      status: "NOT_CHECKED",
      severity: "info",
      description: "title이 비어 있어 텍스트 길이는 점검하지 않았습니다.",
      detectedValue: "점검 불가",
      remediation: "title 값이 비어 있어 길이를 자동 산정하지 않았습니다.",
      snippet: "<title>\n\n</title>",
    };
    overrides["image-alt"] = {
      status: "WARNING",
      description: "alt가 비어 있는 이미지가 17개 발견되었습니다.",
      detectedValue: "alt 누락 이미지 존재 (17개)",
      remediation: "이미지에 alt 속성이 없습니다. 모든 이미지에 의미 있는 alt 텍스트를 추가하세요.",
      snippet:
        '<img loading="lazy" src="//i1.daumcdn.net/thumb/C960x540.fwebp.q90/" alt="">\n<img loading="lazy" src="//i1.daumcdn.net/thumb/C960x540.fwebp.q90/" alt="">\n<img loading="lazy" src="//i1.daumcdn.net/thumb/C960x540.fwebp.q90/" alt="">',
    };
    overrides["render-blocked-resources"] = {
      status: "WARNING",
      description: "렌더링을 차단하는 스크립트/스타일시트가 있습니다.",
      detectedValue: "렌더 차단 리소스 존재",
      remediation: "렌더링을 차단하는 스크립트/스타일시트가 있습니다. 비동기 로딩을 적용하세요.",
      snippet: '<link rel="stylesheet" href="https://t1.daumcdn.net/tistory_admin/assets/style.css">',
    };
    overrides["download-time"] = {
      status: "WARNING",
      description: "페이지 다운로드 시간이 3초를 초과했습니다.",
      detectedValue: "10575 ms",
      remediation: "페이지 로딩 시간이 3초를 초과합니다. 서버 응답 속도와 리소스를 최적화하세요.",
      snippet: "10575 ms",
    };
    overrides.description = {
      status: "PASS",
      description: "meta description이 설정되어 있습니다.",
      detectedValue: "kt nasmedia의 공식 블로그입니다. 최신 디지털 마케팅 정보와 트렌드를 만나보세요.",
      remediation: "메타 description이 설정되어 있습니다.",
      snippet: '<meta name="description" content="케이티 나스미디어의 인사이트를 전합니다.">',
    };
  } else if (risky) {
    overrides["http-status"] = {
      status: "FAIL",
      description: "수집 워커가 방화벽 차단 응답을 감지했습니다.",
      snippet: "HTTP/1.1 403 Forbidden",
    };
    overrides.robots = {
      status: "FAIL",
      description: "robots.txt 또는 보안 정책으로 전체 수집이 제한됩니다.",
      snippet: "User-agent: *\nDisallow: /",
    };
  } else if (needsWork) {
    overrides.viewport = {
      status: "FAIL",
      description: "모바일 viewport 설정이 광고 랜딩에 적합하지 않습니다.",
      snippet: '<meta name="viewport" content="width=1024">',
    };
    overrides["image-alt"] = {
      status: "WARNING",
      description: "alt가 누락된 이미지가 다수 발견되었습니다.",
      snippet: '<img src="/event-cover.jpg" alt="">',
    };
    overrides.description = {
      status: "WARNING",
      description: "meta description이 비어 있거나 너무 짧습니다.",
      snippet: '<meta name="description" content="">',
    };
    overrides["structured-data"] = {
      status: "FAIL",
      description: "상품 또는 조직 구조화 데이터가 없습니다.",
      snippet: "<!-- JSON-LD not found -->",
    };
  } else if (lightWarnings) {
    overrides["image-alt"] = {
      status: "WARNING",
      description: "일부 이미지 alt 속성이 누락되었습니다.",
      snippet: '<img src="/thumb-sale.jpg">',
    };
    overrides.canonical = {
      status: "WARNING",
      description: "canonical URL이 누락되었습니다.",
      snippet: "<!-- canonical not found -->",
    };
  }

  const items = checklistWith(overrides);
  const { grade, score } = calculateSeoGrade(items);

  return {
    id,
    url,
    managerName: managerName || "미지정",
    advertiserName: advertiserName || "미지정 광고주",
    grade,
    score,
    createdAt: new Date().toISOString(),
    durationSec: nasmediaCalibration ? 10.6 : risky ? 42 : needsWork ? 188 : 134,
    status: risky ? "실패" : "완료",
    items,
  };
}

export function isWithinReuseWindow(record: AuditRecord) {
  const elapsed = Date.now() - new Date(record.createdAt).getTime();
  return elapsed < 72 * 60 * 60 * 1000;
}
