"use client";

import { useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowLeft,
  BookOpen,
  CheckCircle2,
  ChevronDown,
  Download,
  FileText,
  HelpCircle,
  Monitor,
  RefreshCw,
  Search,
  ShieldAlert,
  Smartphone,
  Wrench,
  XCircle,
} from "lucide-react";
import type {
  AuditItem,
  AuditRecord,
  AuditStatus,
  KeywordRow,
  KeywordSummary,
} from "./lib/auditData";
import {
  countItems,
  formatFullDate,
  getAuditGroup,
  getStatusLabel,
  normalizeUrl,
} from "./lib/auditData";

interface ReportDetailProps {
  record: AuditRecord;
  onBack: () => void;
  onSupport: (record: AuditRecord) => void;
  onCreatePdf: (record: AuditRecord, mode: "standard" | "premium") => void;
}

const gradeClass: Record<AuditRecord["grade"], string> = {
  A: "grade-a",
  B: "grade-b",
  C: "grade-c",
  D: "grade-d",
  F: "grade-f",
};

const statusIcon: Record<AuditStatus, React.ReactNode> = {
  PASS: <CheckCircle2 size={16} />,
  WARNING: <AlertTriangle size={16} />,
  FAIL: <XCircle size={16} />,
  NOT_CHECKED: <HelpCircle size={16} />,
};

type AuditGroupName =
  | "SEO 점검항목"
  | "색인 점검항목"
  | "수집 점검항목"
  | "진단 제외";

const groupOrder: AuditGroupName[] = [
  "SEO 점검항목",
  "색인 점검항목",
  "수집 점검항목",
  "진단 제외",
];

const groupTone: Record<AuditGroupName, string> = {
  "SEO 점검항목": "seo",
  "색인 점검항목": "index",
  "수집 점검항목": "crawl",
  "진단 제외": "excluded",
};

const itemTitleOverrides: Record<string, string> = {
  "title-present": "<title> 요소를 찾을 수 없음",
  "title-length": "<title> 요소 텍스트 길이 확인 필요",
  description: '<meta name="description"> 설명 누락',
  "h1-count": "<H1> 요소가 2개 이상 발견",
  "render-blocked-resources": "접근이 제한된 리소스가 존재",
  "image-alt": "Alt 속성 누락",
  "download-time": "다운로드 소요 시간이 긴 페이지",
  "page-size": "다운로드 크기가 큰 페이지",
  viewport: "모바일 뷰포트 누락 또는 모바일 최적화 미흡",
  "structured-data": "구조화된 데이터(Schema.org) 마크업 누락",
  canonical: "프로토콜이 다른 내부 링크 존재",
  "meta-robots": "robots.txt에 의해 수집 차단된 페이지",
  "http-status": "페이지 접속 실패",
  "html-parse": "HTML 내용이 없는 페이지",
};

type KeywordMode = "single" | "phrase";

const singleKeywordRows: KeywordRow[] = [
  { keyword: "네이버", frequency: 6, ratio: "0.58%", titleOk: false, descOk: false },
  { keyword: "모바일", frequency: 5, ratio: "0.48%", titleOk: false, descOk: false },
  { keyword: "nas", frequency: 5, ratio: "0.48%", titleOk: false, descOk: true },
  { keyword: "ott", frequency: 5, ratio: "0.48%", titleOk: false, descOk: false },
  { keyword: "나스리포트", frequency: 4, ratio: "0.39%", titleOk: false, descOk: false },
  { keyword: "구글", frequency: 3, ratio: "0.29%", titleOk: false, descOk: false },
  { keyword: "메타", frequency: 3, ratio: "0.29%", titleOk: false, descOk: false },
  { keyword: "숏폼", frequency: 3, ratio: "0.29%", titleOk: false, descOk: false },
  { keyword: "이코노미", frequency: 2, ratio: "0.19%", titleOk: false, descOk: false },
  { keyword: "티스토리", frequency: 2, ratio: "0.19%", titleOk: false, descOk: false },
  { keyword: "디지털", frequency: 2, ratio: "0.19%", titleOk: false, descOk: false },
  { keyword: "마케팅", frequency: 2, ratio: "0.19%", titleOk: false, descOk: false },
  { keyword: "트렌드", frequency: 2, ratio: "0.19%", titleOk: false, descOk: false },
  { keyword: "광고", frequency: 2, ratio: "0.19%", titleOk: false, descOk: false },
  { keyword: "콘텐츠", frequency: 2, ratio: "0.19%", titleOk: false, descOk: false },
  { keyword: "미디어", frequency: 2, ratio: "0.19%", titleOk: false, descOk: false },
  { keyword: "브랜드", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "리포트", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "소비자", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "캠페인", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "시장", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "분석", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "인사이트", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "검색", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "커머스", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "영상", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "플랫폼", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "전략", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "데이터", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "광고주", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
];

const phraseKeywordRows: KeywordRow[] = [
  { keyword: "디지털 마케팅", frequency: 3, ratio: "0.29%", titleOk: false, descOk: false },
  { keyword: "모바일 광고", frequency: 3, ratio: "0.29%", titleOk: false, descOk: false },
  { keyword: "네이버 검색", frequency: 2, ratio: "0.19%", titleOk: false, descOk: false },
  { keyword: "나스 리포트", frequency: 2, ratio: "0.19%", titleOk: false, descOk: false },
  { keyword: "ott 트렌드", frequency: 2, ratio: "0.19%", titleOk: false, descOk: false },
  { keyword: "숏폼 콘텐츠", frequency: 2, ratio: "0.19%", titleOk: false, descOk: false },
  { keyword: "검색 광고", frequency: 2, ratio: "0.19%", titleOk: false, descOk: false },
  { keyword: "브랜드 캠페인", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "메타 플랫폼", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "소비자 인사이트", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "마케팅 전략", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "광고 효율", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "콘텐츠 소비", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "미디어 트렌드", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "커머스 시장", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "영상 광고", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "데이터 분석", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "광고주 리포트", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "시장 변화", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
  { keyword: "캠페인 성과", frequency: 1, ratio: "0.10%", titleOk: false, descOk: false },
];

const fallbackKeywordSummary: KeywordSummary = {
  singleTotal: 484,
  phraseTotal: 513,
  singleRows: singleKeywordRows,
  phraseRows: phraseKeywordRows,
};

function toneClass(status: AuditStatus) {
  return status.toLowerCase().replace("_", "-");
}

function getReportHost(url: string) {
  try {
    return new URL(normalizeUrl(url)).host.replace(/^www\./, "");
  } catch {
    return url.replace(/^https?:\/\//, "").split("/")[0];
  }
}

function getIssueTitle(item: AuditItem) {
  if (item.id === "title-present" && item.status === "PASS") {
    return "<title> 요소가 2개 이상 발견";
  }
  return itemTitleOverrides[item.id] ?? item.itemName;
}

function getGroupLabel(group: AuditGroupName, attentionCount: number) {
  if (group === "SEO 점검항목" && attentionCount > 0) {
    return "SEO 점검 필요";
  }
  return group;
}

function downloadKeywordCsv(rows: KeywordRow[], mode: KeywordMode) {
  const header = "keyword,frequency,ratio,title_tag,meta_description";
  const body = rows
    .map((row) =>
      [
        `"${row.keyword.replace(/"/g, '""')}"`,
        row.frequency,
        row.ratio,
        row.titleOk ? "PASS" : "FAIL",
        row.descOk ? "PASS" : "FAIL",
      ].join(","),
    )
    .join("\n");
  const blob = new Blob([`${header}\n${body}`], {
    type: "text/csv;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `keyword-${mode}.csv`;
  link.click();
  URL.revokeObjectURL(url);
}

function IssueAccordion({
  item,
  open,
  onToggle,
  onSupport,
}: {
  item: AuditItem;
  open: boolean;
  onToggle: () => void;
  onSupport: () => void;
}) {
  const tone = toneClass(item.status);
  const detailTitle =
    item.status === "PASS"
      ? "점검 항목 :"
      : item.status === "NOT_CHECKED"
        ? "점검 안내 :"
        : "개선 방안 :";
  const detailCopy =
    item.status === "PASS"
      ? item.description
      : item.remediation ?? item.guide ?? item.description;
  const detectedValue = item.detectedValue ?? item.description;

  return (
    <div className={`audit-row audit-row-${tone}${open ? " is-open" : ""}`}>
      <div className="audit-row-head">
        <button className="audit-row-toggle" type="button" onClick={onToggle}>
          <span className={`audit-row-icon audit-row-icon-${tone}`}>
            {statusIcon[item.status]}
          </span>
          <span className="audit-row-title">{getIssueTitle(item)}</span>
          <span className="audit-row-value">{detectedValue}</span>
          <span className={`audit-status-badge audit-status-${tone}`}>
            {getStatusLabel(item.status)}
          </span>
          <ChevronDown className={open ? "chevron open" : "chevron"} size={16} />
        </button>
        <div className="audit-row-tools">
          <button className="audit-guide-button" type="button" onClick={onToggle}>
            <BookOpen size={17} />
            가이드보기
          </button>
          {item.status !== "PASS" && item.status !== "NOT_CHECKED" ? (
            <button
              className="audit-help-button"
              type="button"
              onClick={onSupport}
            >
              <Wrench size={16} />
              도와주세요
            </button>
          ) : null}
        </div>
      </div>
      {open ? (
        <div className="audit-row-body">
          <div className={`audit-detail-box audit-detail-${tone}`}>
            <p>
              <strong>{detailTitle}</strong> {detailCopy}
            </p>
            {detectedValue ? (
              <p>
                <strong>감지된 값 :</strong> {detectedValue}
              </p>
            ) : null}
          </div>
          {item.snippet ? (
            <div className="html-snippet-wrap">
              <span>HTML</span>
              <pre className="snippet-block"><code>{item.snippet}</code></pre>
            </div>
          ) : (
            <div className="not-checked-tip">
              HTML 스니펫은 별도 렌더링 워커 연결 시 함께 저장됩니다.
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}

export default function ReportDetail({
  record,
  onBack,
  onSupport,
  onCreatePdf,
}: ReportDetailProps) {
  const [openIssueId, setOpenIssueId] = useState<string>(() => {
    const firstIssue = record.items.find((item) => item.status !== "PASS");
    return firstIssue?.id ?? record.items[0]?.id ?? "";
  });
  const [showFirewallGuide, setShowFirewallGuide] = useState(
    record.grade === "F",
  );
  const [keywordMode, setKeywordMode] = useState<KeywordMode>("single");
  const [keywordSearch, setKeywordSearch] = useState("");
  const [visibleKeywordCount, setVisibleKeywordCount] = useState(10);
  const counts = useMemo(() => countItems(record.items), [record.items]);
  const issues = record.items.filter((item) => item.status !== "PASS");
  const groupedItems = useMemo<Array<{ group: AuditGroupName; items: AuditItem[] }>>(
    () =>
      groupOrder
        .map((group) => ({
          group,
          items: record.items.filter((item) => getAuditGroup(item) === group),
        }))
        .filter(({ items }) => items.length > 0),
    [record.items],
  );
  const collectionFail = record.items.some(
    (item) => item.category === "수집" && item.status === "FAIL",
  );
  const reportHost = getReportHost(record.url);
  const renderSnapshot = record.renderSnapshot;
  const keywordSummary = record.keywordSummary ?? fallbackKeywordSummary;
  const activeKeywordRows =
    keywordMode === "single"
      ? keywordSummary.singleRows
      : keywordSummary.phraseRows;
  const filteredKeywordRows = useMemo(() => {
    const query = keywordSearch.trim().toLowerCase();
    if (!query) {
      return activeKeywordRows;
    }
    return activeKeywordRows.filter((row) =>
      row.keyword.toLowerCase().includes(query),
    );
  }, [activeKeywordRows, keywordSearch]);
  const visibleKeywordRows = filteredKeywordRows.slice(0, visibleKeywordCount);
  const maxKeywordFrequency = Math.max(
    1,
    ...filteredKeywordRows.map((row) => row.frequency),
  );

  return (
    <div className="view-stack detail-stack">
      <section className="detail-page-title">
        <button className="text-button subdued" type="button" onClick={onBack}>
          <ArrowLeft size={16} />
          분석 내역으로
        </button>
        <div className="detail-title-row">
          <div>
            <h1>분석 결과 상세</h1>
            <p>
              {record.url} · {formatFullDate(record.createdAt)}
            </p>
          </div>
          <div className="detail-actions">
            <button
              className="secondary-button"
              type="button"
              title="PDF 다운로드"
              onClick={() => onCreatePdf(record, "standard")}
            >
              <FileText size={16} />
              PDF 다운로드
            </button>
            <button
              className="secondary-button premium-action"
              type="button"
              title="프리미엄 PDF"
              onClick={() => onCreatePdf(record, "premium")}
            >
              <FileText size={16} />
              프리미엄 PDF
            </button>
          </div>
        </div>
      </section>

      {collectionFail ? (
        <section className="alert-band">
          <div>
            <p className="alert-title">수집 차단 가능성 감지</p>
            <p>
              방화벽, WAF, robots 정책으로 검색 봇이 실제 랜딩 HTML을 가져오지
              못했습니다.
            </p>
          </div>
          <button
            className="secondary-button"
            type="button"
            onClick={() => setShowFirewallGuide(true)}
          >
            <ShieldAlert size={16} />
            화이트리스트
          </button>
        </section>
      ) : null}

      <section className="report-card">
        <div className="report-brand-row">
          <div className="report-brand">
            <strong>ADVoost</strong>
            <span>검색</span>
            <i>×</i>
            <b>SEO.co.kr</b>
          </div>
          <span>웹사이트 분석 리포트</span>
        </div>
        <div className="report-intro">
          <h2>진단보고서</h2>
          <p>
            이 보고서는 네이버 애드부스트(ADVoost) 검색 광고 효율을 극대화하기
            위해 웹사이트의 검색엔진 친화도를 분석한 결과입니다.
          </p>
          <p>
            수집 실패, 색인 실패, SEO 점검 필요 여부를 심층 진단하여 A+에서
            F까지의 등급을 제공합니다.
          </p>
          <strong>
            진단 도구는 웹사이트에 대한 전반적인 점검 결과를 제공하며 검색 광고
            노출을 보장하지는 않습니다.
          </strong>
        </div>
        <h3>{reportHost} 결과 점검하기</h3>
        <div className="report-overview">
          <div className="grade-visual">
            <div className={`grade-circle ${gradeClass[record.grade]}`}>
              {record.grade}
            </div>
            <strong>개선이 필요합니다.</strong>
            <span>개선 필요 항목: {issues.length}</span>
            <div className="grade-mini-status">
              <div className="mini-status fail">
                <b>FAIL</b>
                <small>SEO 점검항목</small>
              </div>
              <div className="mini-status pass">
                <b>PASS</b>
                <small>색인 점검항목</small>
              </div>
              <div className="mini-status pass">
                <b>PASS</b>
                <small>수집 점검항목</small>
              </div>
            </div>
          </div>
          <div className="device-report">
            <div className="device-preview-row">
              <div className="desktop-device">
                {renderSnapshot?.desktopScreenshot ? (
                  <img src={renderSnapshot.desktopScreenshot} alt="데스크톱 캡처" />
                ) : (
                  <>
                    <div className="device-top-dots" />
                    <div className="mock-hero" />
                    <div className="mock-grid">
                      <span />
                      <span />
                      <span />
                    </div>
                  </>
                )}
              </div>
              <div className="mobile-device">
                {renderSnapshot?.mobileScreenshot ? (
                  <img src={renderSnapshot.mobileScreenshot} alt="모바일 캡처" />
                ) : (
                  <>
                    <div />
                    <span />
                    <span />
                    <span />
                  </>
                )}
              </div>
            </div>
            <div className="device-labels">
              <span>
                <Monitor size={15} />
                데스크탑
              </span>
              <span>
                <Smartphone size={15} />
                모바일
              </span>
            </div>
            {renderSnapshot?.success && renderSnapshot.loadTimeMs ? (
              <p>
                렌더링 {renderSnapshot.loadTimeMs} ms · 리소스{" "}
                {renderSnapshot.resourceCount}개 · 콘솔 오류{" "}
                {renderSnapshot.consoleErrorCount}개
              </p>
            ) : null}
            <p>*미리보기 화면이 제대로 표시되지 않나요?</p>
            <p>
              분석툴 IP가 화이트리스트에 등록되지 않으면 방화벽에 의해 사이트
              접근이 차단될 수 있습니다.
            </p>
            <button className="text-button dark" type="button">
              IP 화이트리스트 설정하기
            </button>
          </div>
        </div>
      </section>

      <section className="result-count-card">
        <div className="result-count-item pass">
          <CheckCircle2 size={22} />
          <strong>{counts.PASS}</strong>
          <span>통과</span>
        </div>
        <div className="result-count-item warning">
          <AlertTriangle size={22} />
          <strong>{counts.WARNING}</strong>
          <span>경고</span>
        </div>
        <div className="result-count-item fail">
          <XCircle size={22} />
          <strong>{counts.FAIL}</strong>
          <span>실패</span>
        </div>
        <div className="result-count-item blocked">
          <ShieldAlert size={22} />
          <strong>{counts.NOT_CHECKED}</strong>
          <span>수집불가</span>
        </div>
      </section>

      <section className="keyword-card">
        <div className="keyword-card-header">
          <h2>키워드 요약</h2>
          <span>{formatFullDate(record.createdAt)} 분석</span>
          <div>
            <button
              className="icon-only"
              type="button"
              title="다운로드"
              onClick={() => downloadKeywordCsv(filteredKeywordRows, keywordMode)}
            >
              <Download size={16} />
            </button>
            <button
              className="icon-only"
              type="button"
              title="필터 초기화"
              onClick={() => {
                setKeywordSearch("");
                setVisibleKeywordCount(10);
              }}
            >
              <RefreshCw size={16} />
            </button>
          </div>
        </div>
        <div className="keyword-tabs">
          <button
            className={keywordMode === "single" ? "keyword-tab-button active" : "keyword-tab-button"}
            type="button"
            onClick={() => {
              setKeywordMode("single");
              setVisibleKeywordCount(10);
            }}
          >
            개별 키워드 <span>({keywordSummary.singleTotal})</span>
          </button>
          <button
            className={keywordMode === "phrase" ? "keyword-tab-button active" : "keyword-tab-button"}
            type="button"
            onClick={() => {
              setKeywordMode("phrase");
              setVisibleKeywordCount(10);
            }}
          >
            프레이즈 (2어절) <span>({keywordSummary.phraseTotal})</span>
          </button>
          <div className="search-field compact">
            <Search size={15} />
            <input
              placeholder="키워드 검색..."
              value={keywordSearch}
              onChange={(event) => {
                setKeywordSearch(event.target.value);
                setVisibleKeywordCount(10);
              }}
            />
          </div>
        </div>
        <div className="keyword-table">
          <div className="keyword-row keyword-head">
            <span>#</span>
            <span>키워드</span>
            <span>빈도수</span>
            <span>페이지 빈도율</span>
            <span>타이틀 태그</span>
            <span>메타 디스크립션 태그</span>
          </div>
          {visibleKeywordRows.map((row, index) => (
            <div className="keyword-row" key={`${keywordMode}-${row.keyword}`}>
              <span>{index + 1}</span>
              <strong>{row.keyword}</strong>
              <span>{row.frequency}</span>
              <span className="keyword-ratio-cell">
                <span className="keyword-ratio-track">
                  <i style={{ width: `${(row.frequency / maxKeywordFrequency) * 100}%` }} />
                </span>
                <em>{row.ratio}</em>
              </span>
              <span className={row.titleOk ? "ok" : "bad"}>
                {row.titleOk ? <CheckCircle2 size={16} /> : <XCircle size={16} />}
              </span>
              <span className={row.descOk ? "ok" : "bad"}>
                {row.descOk ? <CheckCircle2 size={16} /> : <XCircle size={16} />}
              </span>
            </div>
          ))}
          {visibleKeywordRows.length === 0 ? (
            <div className="keyword-empty">검색 결과가 없습니다.</div>
          ) : null}
        </div>
        {visibleKeywordCount < filteredKeywordRows.length ? (
          <button
            className="keyword-more"
            type="button"
            onClick={() => setVisibleKeywordCount((count) => count + 20)}
          >
            더보기 ({Math.min(20, filteredKeywordRows.length - visibleKeywordCount)}개 더)
            <ChevronDown size={16} />
          </button>
        ) : (
          <div className="keyword-more keyword-more-static">
            전체 {filteredKeywordRows.length}개 표시
          </div>
        )}
      </section>

      <section className="issues-section">
        <div className="section-heading">
          <div>
            <h2>SEO 권장 항목 32개</h2>
          </div>
          <div className="issue-summary">
            <AlertTriangle size={15} />
            {counts.WARNING}
            <CheckCircle2 size={15} />
            {counts.PASS}
          </div>
        </div>
        <div className="audit-groups-list">
          {groupedItems.map(({ group, items }) => {
            const statusCounts = countItems(items);
            const attentionCount = statusCounts.WARNING + statusCounts.FAIL;

            return (
              <div className="audit-group-card" key={group}>
                <div className="audit-group-header">
                  <span className={`audit-group-chip audit-group-${groupTone[group]}`}>
                    {getGroupLabel(group, attentionCount)}
                  </span>
                  <div className="audit-group-stats">
                    {statusCounts.WARNING ? (
                      <span className="stat-warning">
                        <AlertTriangle size={15} />
                        {statusCounts.WARNING}
                      </span>
                    ) : null}
                    {statusCounts.FAIL ? (
                      <span className="stat-fail">
                        <XCircle size={15} />
                        {statusCounts.FAIL}
                      </span>
                    ) : null}
                    {statusCounts.PASS ? (
                      <span className="stat-pass">
                        <CheckCircle2 size={15} />
                        {statusCounts.PASS}
                      </span>
                    ) : null}
                    {statusCounts.NOT_CHECKED ? (
                      <span className="stat-not-checked">
                        <HelpCircle size={15} />
                        {statusCounts.NOT_CHECKED}
                      </span>
                    ) : null}
                    <ChevronDown size={17} />
                  </div>
                </div>
                <div className="audit-row-list">
                  {items.map((item) => (
                    <IssueAccordion
                      key={item.id}
                      item={item}
                      open={openIssueId === item.id}
                      onToggle={() =>
                        setOpenIssueId((current) =>
                          current === item.id ? "" : item.id,
                        )
                      }
                      onSupport={() => onSupport(record)}
                    />
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      </section>

      {showFirewallGuide ? (
        <div className="modal-scrim" role="dialog" aria-modal="true">
          <div className="modal-panel">
            <div className="modal-icon">
              <ShieldAlert size={22} />
            </div>
            <h2>크롤러 화이트리스트 등록</h2>
            <p>
              보안 장비에서 진단 워커를 차단하면 F 등급으로 산정됩니다. 운영
              환경에서는 고정 outbound IP, User-Agent, 요청 빈도 제한 값을
              고객사 보안팀에 전달해 허용 정책을 등록하세요.
            </p>
            <div className="modal-code">
              User-Agent: ADVoost-AuditBot/1.0
              <br />
              Timeout: 12s · Retry: 2 · Rate: 1 URL/min
            </div>
            <div className="modal-actions">
              <button
                className="secondary-button"
                type="button"
                onClick={() => setShowFirewallGuide(false)}
              >
                닫기
              </button>
              <button
                className="primary-button"
                type="button"
                onClick={() => {
                  setShowFirewallGuide(false);
                  onSupport(record);
                }}
              >
                기술 지원 접수
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
