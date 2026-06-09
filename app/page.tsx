"use client";

import type { FormEvent } from "react";
import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Box,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Coins,
  Download,
  ExternalLink,
  FileArchive,
  FileDown,
  History,
  LayoutDashboard,
  LifeBuoy,
  Link2,
  ListChecks,
  LogOut,
  MessageSquare,
  Plus,
  Search,
  Settings,
  Trash2,
  Upload,
} from "lucide-react";
import ReportDetail from "../ReportDetail";
import {
  apiResponseToRecord,
  countItems,
  createAuditRecord,
  formatDate,
  formatFullDate,
  isWithinReuseWindow,
  normalizeUrl,
  seedManagedUrls,
  seedRecords,
  seedTickets,
} from "../lib/auditData";
import type {
  ApiAuditResponse,
  AuditGrade,
  AuditRecord,
  ManagedUrl,
  SupportTicket,
} from "../lib/auditData";

type ViewKey = "dashboard" | "single" | "history" | "bulk" | "urls" | "detail";

type ToastState = {
  tone: "success" | "warning" | "info";
  title: string;
  message: string;
};

const API_BASE_URL =
  process.env.NEXT_PUBLIC_AUDIT_API_URL ?? "http://localhost:8000";

const navItems: Array<{
  key: ViewKey;
  label: string;
  icon: React.ReactNode;
}> = [
  { key: "dashboard", label: "대시보드", icon: <LayoutDashboard size={18} /> },
  { key: "single", label: "분석하기", icon: <Search size={18} /> },
  { key: "history", label: "분석 히스토리", icon: <History size={18} /> },
  { key: "bulk", label: "일괄 분석하기", icon: <ListChecks size={18} /> },
  { key: "urls", label: "URL 관리", icon: <Link2 size={18} /> },
];

const gradeClass: Record<AuditGrade, string> = {
  A: "grade-a",
  B: "grade-b",
  C: "grade-c",
  D: "grade-d",
  F: "grade-f",
};

function downloadTextFile(filename: string, content: string, type: string) {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

function GradeBadge({ grade }: { grade: AuditGrade }) {
  return <span className={`grade-badge ${gradeClass[grade]}`}>{grade}</span>;
}

function getInitialView(): ViewKey {
  if (typeof window === "undefined") {
    return "dashboard";
  }
  const view = new URLSearchParams(window.location.search).get("view") as ViewKey | null;
  return view && ["dashboard", "single", "history", "bulk", "urls", "detail"].includes(view)
    ? view
    : "dashboard";
}

function CreditPill() {
  return (
    <span className="credit-pill" title="원본 UI와 동일한 표시용 배지입니다">
      <Coins size={15} />
      <strong>9949</strong>
      <span>크레딧</span>
    </span>
  );
}

function PageTitle({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  return (
    <section className="page-title">
      <div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action ? <div className="page-title-action">{action}</div> : null}
    </section>
  );
}

function StatusBadge({ status }: { status: AuditRecord["status"] }) {
  return (
    <span className="complete-badge">
      <CheckCircle2 size={14} />
      {status}
    </span>
  );
}

function getDisplayHost(rawUrl: string) {
  try {
    return new URL(normalizeUrl(rawUrl)).host.replace(/^www\./, "");
  } catch {
    return rawUrl.replace(/^https?:\/\//, "").split("/")[0];
  }
}

function formatHistoryDate(value: string) {
  const date = new Date(value);
  const year = `${date.getFullYear()}`.slice(-2);
  const month = date.getMonth() + 1;
  const day = date.getDate();
  const hours = date.getHours();
  const minutes = `${date.getMinutes()}`.padStart(2, "0");
  const period = hours >= 12 ? "오후" : "오전";
  const hour12 = hours % 12 || 12;
  return `${year}. ${month}. ${day}. ${period} ${hour12}:${minutes}`;
}

function CountsInline({ record }: { record: AuditRecord }) {
  const counts = countItems(record.items);
  return (
    <span className="counts-inline">
      <b className="count-pass">{counts.PASS}</b>
      <i>/</i>
      <b className="count-warning">{counts.WARNING}</b>
      <i>/</i>
      <b className="count-fail">{counts.FAIL}</b>
    </span>
  );
}

async function requestAudit(recordInput: {
  url: string;
  managerName: string;
  advertiserName: string;
}) {
  const response = await fetch(`${API_BASE_URL}/api/audit`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      url: recordInput.url,
      user_id: "internal-platform",
      manager_name: recordInput.managerName,
      advertiser_name: recordInput.advertiserName,
    }),
  });

  if (!response.ok) {
    throw new Error(`Audit API failed: ${response.status}`);
  }

  const body = (await response.json()) as ApiAuditResponse;
  return apiResponseToRecord(body);
}

function MetricCard({
  icon,
  label,
  value,
  detail,
  tone = "neutral",
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  detail: string;
  tone?: "neutral" | "green" | "amber" | "red" | "blue";
}) {
  return (
    <div className={`metric-card tone-${tone}`}>
      <div className="metric-icon">{icon}</div>
      <span>{label}</span>
      <strong>{value}</strong>
      <p>{detail}</p>
    </div>
  );
}

export default function Home() {
  const [activeView, setActiveView] = useState<ViewKey>("dashboard");
  const [records, setRecords] = useState<AuditRecord[]>(seedRecords);
  const [managedUrls, setManagedUrls] =
    useState<ManagedUrl[]>(seedManagedUrls);
  const [tickets, setTickets] = useState<SupportTicket[]>(seedTickets);
  const [selectedUrlIds, setSelectedUrlIds] = useState<string[]>([]);
  const [detailRecord, setDetailRecord] = useState<AuditRecord>(seedRecords[0]);
  const [singleRunning, setSingleRunning] = useState(false);
  const [toast, setToast] = useState<ToastState | null>(null);
  const [pdfBanner, setPdfBanner] = useState<{
    status: "building" | "ready";
    ids: string[];
  } | null>(null);
  const [singleForm, setSingleForm] = useState({
    url: "",
    managerName: "",
    advertiserName: "",
  });
  const [urlForm, setUrlForm] = useState({
    url: "",
    advertiserName: "",
    managerName: "",
    memo: "",
  });
  const [csvText, setCsvText] = useState(
    "freshday.kr/store,프레시데이,한지우\nbad-url,오류행,담당자\nhttps://mellow-fit.kr/detail,멜로핏,송하린",
  );
  const [bulkState, setBulkState] = useState({
    running: false,
    progress: 0,
    queued: selectedUrlIds.length,
    completed: 0,
    failed: 0,
  });

  useEffect(() => {
    setActiveView(getInitialView());
  }, []);

  const dashboardStats = useMemo(() => {
    const allItems = records.flatMap((record) => record.items);
    const issueCount = allItems.filter(
      (item) => item.status === "WARNING" || item.status === "FAIL",
    ).length;
    const warningCount = allItems.filter((item) => item.status === "WARNING").length;
    const failCount = allItems.filter((item) => item.status === "FAIL").length;
    const stable = allItems.filter((item) => item.status === "PASS").length;
    const completed = records.filter((record) => record.status === "완료");
    const avgScore = completed.length
      ? Math.round(
          completed.reduce((sum, record) => sum + record.score, 0) /
            completed.length,
        )
      : 0;
    return {
      issueCount,
      warningCount,
      failCount,
      stable,
      inProgress: tickets.filter((ticket) => ticket.status === "해결중").length,
      completedCount: completed.length,
      avgScore,
    };
  }, [records, tickets]);

  const selectedManagedUrls = useMemo(
    () => managedUrls.filter((url) => selectedUrlIds.includes(url.id)),
    [managedUrls, selectedUrlIds],
  );
  const visibleManagedUrls = useMemo(() => managedUrls.slice(0, 19), [managedUrls]);
  const historyRows = useMemo(() => {
    const directRows = records.slice(0, 10);
    if (directRows.length >= 10) {
      return directRows;
    }
    const existing = new Set(directRows.map((record) => normalizeUrl(record.url)));
    const fillers = visibleManagedUrls
      .filter((url) => !existing.has(normalizeUrl(url.url)))
      .slice(0, 10 - directRows.length)
      .map((url, index) => {
        const base = seedRecords[index % seedRecords.length];
        return {
          ...base,
          id: `HISTORY-${url.id}`,
          url: normalizeUrl(url.url),
          advertiserName: url.advertiserName,
          managerName: url.managerName,
          grade: url.lastGrade ?? base.grade,
          createdAt: url.lastAuditedAt ?? base.createdAt,
        } satisfies AuditRecord;
      });
    return [...directRows, ...fillers];
  }, [records, visibleManagedUrls]);

  function showToast(nextToast: ToastState) {
    setToast(nextToast);
    window.setTimeout(() => setToast(null), 3600);
  }

  function openDetail(record: AuditRecord) {
    setDetailRecord(record);
    setActiveView("detail");
  }

  function handleCreatePdf(targetRecords: AuditRecord | AuditRecord[]) {
    const targets = Array.isArray(targetRecords) ? targetRecords : [targetRecords];
    setPdfBanner({ status: "building", ids: targets.map((record) => record.id) });
    window.setTimeout(() => {
      setPdfBanner({ status: "ready", ids: targets.map((record) => record.id) });
      showToast({
        tone: "success",
        title: "PDF ZIP 준비 완료",
        message: `${targets.length}개 리포트를 다운로드할 수 있습니다.`,
      });
    }, 1100);
  }

  function downloadPdfArchive() {
    const ids = pdfBanner?.ids ?? [];
    downloadTextFile(
      "advoost-report-archive.txt",
      `ADVoost PDF ZIP mock\n${ids.join("\n")}`,
      "text/plain;charset=utf-8",
    );
    setPdfBanner(null);
  }

  function handleSupport(record: AuditRecord) {
    const priority =
      record.grade === "F" || record.grade === "D"
        ? "높음"
        : record.grade === "C"
          ? "보통"
          : "낮음";
    const ticket: SupportTicket = {
      id: `TS-${Math.floor(1000 + Math.random() * 9000)}`,
      auditId: record.id,
      url: record.url,
      advertiserName: record.advertiserName,
      status: "접수",
      requestedAt: new Date().toISOString(),
      priority,
    };
    setTickets((current) => [ticket, ...current]);
    showToast({
      tone: "success",
      title: "기술 지원 접수",
      message: `${record.advertiserName} 리포트가 지원 큐에 등록되었습니다.`,
    });
  }

  async function handleSingleAudit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalized = normalizeUrl(singleForm.url);
    if (!normalized || !normalized.includes(".")) {
      showToast({
        tone: "warning",
        title: "URL 확인 필요",
        message: "진단 가능한 랜딩 URL을 입력하세요.",
      });
      return;
    }

    const cachedRecord = records.find(
      (record) => normalizeUrl(record.url) === normalized && isWithinReuseWindow(record),
    );
    if (cachedRecord) {
      openDetail(cachedRecord);
      showToast({
        tone: "info",
        title: "72시간 캐시 적용",
        message: "동일 URL의 기존 분석 결과를 재사용했습니다.",
      });
      return;
    }

    setSingleRunning(true);
    showToast({
      tone: "info",
      title: "분석 큐 등록",
      message: "HTML 수집과 32개 권장 항목 검사를 시작했습니다.",
    });

    try {
      const nextRecord = await requestAudit({
        url: normalized,
        managerName: singleForm.managerName,
        advertiserName: singleForm.advertiserName,
      });
      setRecords((current) => [nextRecord, ...current]);
      setManagedUrls((current) =>
        current.map((url) =>
          normalizeUrl(url.url) === normalized
            ? {
                ...url,
                lastGrade: nextRecord.grade,
                lastAuditedAt: nextRecord.createdAt,
              }
            : url,
        ),
      );
      openDetail(nextRecord);
      showToast({
        tone: "success",
        title: "분석 완료",
        message: `${nextRecord.grade} 등급으로 산정되었습니다.`,
      });
    } catch {
      const nextRecord = createAuditRecord(
        normalized,
        singleForm.managerName,
        singleForm.advertiserName,
      );
      setRecords((current) => [nextRecord, ...current]);
      setManagedUrls((current) =>
        current.map((url) =>
          normalizeUrl(url.url) === normalized
            ? {
                ...url,
                lastGrade: nextRecord.grade,
                lastAuditedAt: nextRecord.createdAt,
              }
            : url,
        ),
      );
      openDetail(nextRecord);
      showToast({
        tone: "warning",
        title: "로컬 엔진 사용",
        message: "API 서버 연결 실패로 내장 규칙 엔진 결과를 표시했습니다.",
      });
    } finally {
      setSingleRunning(false);
    }
  }

  async function handleBulkStart() {
    if (selectedManagedUrls.length === 0) {
      showToast({
        tone: "warning",
        title: "선택 URL 없음",
        message: "일괄 분석할 URL을 먼저 선택하세요.",
      });
      return;
    }
    const queueSize = selectedManagedUrls.length;
    setBulkState({
      running: true,
      progress: 22,
      queued: queueSize,
      completed: 0,
      failed: 0,
    });
    showToast({
      tone: "info",
      title: "큐 등록 완료",
      message: `${queueSize}개 URL이 워커 큐에 등록되었습니다.`,
    });

    const newRecords: AuditRecord[] = [];

    for (const [index, target] of selectedManagedUrls.entries()) {
      try {
        newRecords.push(
          await requestAudit({
            url: normalizeUrl(target.url),
            managerName: target.managerName,
            advertiserName: target.advertiserName,
          }),
        );
      } catch {
        newRecords.push(
          createAuditRecord(
            target.url,
            target.managerName,
            target.advertiserName,
          ),
        );
      }

      setBulkState((current) => ({
        ...current,
        progress: Math.round(((index + 1) / queueSize) * 100),
        completed: index + 1,
      }));
    }

    const failed = newRecords.filter((record) => record.status === "실패").length;
    setRecords((current) => [...newRecords, ...current]);
    setManagedUrls((current) =>
      current.map((url) => {
        const nextRecord = newRecords.find(
          (record) => normalizeUrl(record.url) === normalizeUrl(url.url),
        );
        return nextRecord
          ? {
              ...url,
              lastGrade: nextRecord.grade,
              lastAuditedAt: nextRecord.createdAt,
            }
          : url;
      }),
    );
    setBulkState({
      running: false,
      progress: 100,
      queued: queueSize,
      completed: queueSize,
      failed,
    });
    showToast({
      tone: failed ? "warning" : "success",
      title: "일괄 분석 완료",
      message: failed
        ? `${failed}개 URL은 수집 실패로 표시되었습니다.`
        : "선택한 URL 진단이 모두 완료되었습니다.",
    });
  }

  function handleExcelExport() {
    const rows = [
      "id,url,advertiser,manager,grade,score,status,created_at",
      ...records.map((record) =>
        [
          record.id,
          record.url,
          record.advertiserName,
          record.managerName,
          record.grade,
          record.score,
          record.status,
          record.createdAt,
        ].join(","),
      ),
    ];
    downloadTextFile(
      "advoost-audit-history.csv",
      rows.join("\n"),
      "text/csv;charset=utf-8",
    );
  }

  function handleTemplateDownload() {
    downloadTextFile(
      "advoost-url-template.csv",
      "url,advertiser_name,manager_name,memo\nhttps://example.co.kr/landing,광고주명,담당자명,메모",
      "text/csv;charset=utf-8",
    );
  }

  function handleAddUrl(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalized = normalizeUrl(urlForm.url);
    if (!normalized || !normalized.includes(".")) {
      showToast({
        tone: "warning",
        title: "등록 실패",
        message: "URL 형식을 확인하세요.",
      });
      return;
    }
    const nextUrl: ManagedUrl = {
      id: `URL-${Date.now().toString().slice(-6)}`,
      url: normalized,
      advertiserName: urlForm.advertiserName || "미지정 광고주",
      managerName: urlForm.managerName || "미지정",
      memo: urlForm.memo,
    };
    setManagedUrls((current) => [nextUrl, ...current]);
    setSelectedUrlIds((current) => [nextUrl.id, ...current]);
    setUrlForm({ url: "", advertiserName: "", managerName: "", memo: "" });
    showToast({
      tone: "success",
      title: "URL 등록",
      message: "신규 URL이 목록에 추가되었습니다.",
    });
  }

  function handleCsvApply() {
    const lines = csvText
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean);
    const added: ManagedUrl[] = [];
    let skipped = 0;

    lines.forEach((line, index) => {
      const [url, advertiserName, managerName, memo] = line
        .split(",")
        .map((cell) => cell.trim());
      const normalized = normalizeUrl(url ?? "");
      if (!normalized.includes(".") || normalized.includes("bad-url")) {
        skipped += 1;
        return;
      }
      added.push({
        id: `URL-BULK-${Date.now()}-${index}`,
        url: normalized,
        advertiserName: advertiserName || "미지정 광고주",
        managerName: managerName || "미지정",
        memo: memo || "CSV 등록",
      });
    });

    if (added.length) {
      setManagedUrls((current) => [...added, ...current]);
      setSelectedUrlIds((current) => [
        ...added.map((url) => url.id),
        ...current,
      ]);
    }
    setCsvText("");
    showToast({
      tone: skipped ? "warning" : "success",
      title: "CSV 처리 완료",
      message: `${added.length}개 등록, ${skipped}개 오류 행 스킵`,
    });
  }

  function toggleSelectedUrl(id: string) {
    setSelectedUrlIds((current) =>
      current.includes(id)
        ? current.filter((selectedId) => selectedId !== id)
        : [...current, id],
    );
  }

  function bridgeToBulk() {
    setActiveView("bulk");
    showToast({
      tone: "info",
      title: "선택 상태 인계",
      message: `${selectedUrlIds.length}개 URL이 일괄 분석 화면으로 전달되었습니다.`,
    });
  }

  const content = (() => {
    if (activeView === "detail") {
      return (
        <ReportDetail
          record={detailRecord}
          onBack={() => setActiveView("history")}
          onSupport={handleSupport}
          onCreatePdf={handleCreatePdf}
        />
      );
    }

    if (activeView === "single") {
      return (
        <div className="view-stack">
          <PageTitle
            title="ADVoost 검색 연결 URL 분석도구"
            description="SEO 32가지 권장 항목을 자동 분석합니다"
            action={<CreditPill />}
          />

          <section className="single-audit-card">
            <form className="single-audit-form" onSubmit={handleSingleAudit}>
              <div className="single-meta-row">
                <input
                  value={singleForm.managerName}
                  onChange={(event) =>
                    setSingleForm((current) => ({
                      ...current,
                      managerName: event.target.value,
                    }))
                  }
                  placeholder="담당자명 (선택)"
                />
                <input
                  value={singleForm.advertiserName}
                  onChange={(event) =>
                    setSingleForm((current) => ({
                      ...current,
                      advertiserName: event.target.value,
                    }))
                  }
                  placeholder="광고주명 (선택)"
                />
              </div>
              <div className="single-url-row">
                <div className="url-input-shell">
                  <Link2 size={17} />
                  <input
                    value={singleForm.url}
                    onChange={(event) =>
                      setSingleForm((current) => ({
                        ...current,
                        url: event.target.value,
                      }))
                    }
                    placeholder="https://yoursite.com"
                  />
                </div>
                <button
                  className="primary-button"
                  type="submit"
                  disabled={singleRunning}
                >
                  <Search size={17} />
                  {singleRunning ? "분석 중" : "분석 시작"}
                </button>
              </div>
              <ul className="audit-notes">
                <li>연결URL 입력 후 분석 시작 버튼 클릭 시 분석 큐에 등록됩니다.</li>
                <li>동일한 랜딩페이지 기준 72시간 내 분석 결과를 재사용합니다.</li>
                <li>
                  해당 분석 도구는 SEO로직에 맞춰 개발한 웹사이트 분석 솔루션이며
                  내부 표준 기준으로 분석 결과를 제공합니다.
                </li>
              </ul>
            </form>
          </section>
        </div>
      );
    }

    if (activeView === "history") {
      return (
        <div className="view-stack">
          <PageTitle
            title="분석 히스토리"
            description="지금까지 분석한 URL 목록과 결과를 확인하세요"
            action={
              <button
                className="primary-button"
                type="button"
                onClick={() => setActiveView("single")}
              >
                <Search size={17} />새 분석
              </button>
            }
          />

          <section className="history-card">
            <div className="history-card-top">
              <strong>전체 21건</strong>
              <div className="history-tools">
                <div className="search-field">
                  <Search size={16} />
                  <input placeholder="검색" />
                </div>
                <span className="rows-label">페이지당</span>
                <button className="select-button" type="button">
                  10개 <ChevronDown size={15} />
                </button>
              </div>
            </div>
            <div className="history-download-row">
              <button className="text-button dark" type="button">
                <FileDown size={16} />
                분석 다운로드
              </button>
              <span className="ready-chip">2건 준비됨</span>
              <ChevronDown size={15} />
              <div className="download-spacer" />
              <button
                className="secondary-button muted-action"
                type="button"
                onClick={() => handleCreatePdf(historyRows.slice(0, 2))}
              >
                <FileArchive size={16} />
                PDF보고서 일괄 다운로드
              </button>
              <button
                className="secondary-button premium-action"
                type="button"
                onClick={() => handleCreatePdf(historyRows.slice(0, 2))}
              >
                <FileArchive size={16} />
                프리미엄 PDF 일괄 다운로드
              </button>
              <button
                className="text-button subdued"
                type="button"
                onClick={handleExcelExport}
              >
                선택 해제
              </button>
            </div>
            <div className="premium-row">
              <FileArchive size={16} />
              프리미엄 PDF 다운로드
              <ChevronDown size={15} />
            </div>
            <div className="history-table">
              <div className="history-table-row history-table-head">
                <span />
                <span>URL</span>
                <span>상태</span>
                <span>등급</span>
                <span>통과/경고/실패</span>
                <span>분석일시</span>
                <span>담당자</span>
                <span>광고주</span>
                <span>액션</span>
              </div>
              {historyRows.map((record, index) => (
                <div className="history-table-row" key={`${record.id}-${index}`}>
                  <span>
                    <input type="checkbox" />
                  </span>
                  <span className="history-url-cell">
                    <strong>{record.url}</strong>
                    <ExternalLink size={14} />
                  </span>
                  <span>
                    <StatusBadge status={record.status} />
                  </span>
                  <span>
                    <GradeBadge grade={record.grade} />
                  </span>
                  <CountsInline record={record} />
                  <span>{formatHistoryDate(record.createdAt)}</span>
                  <span>{record.managerName}</span>
                  <span>{record.advertiserName}</span>
                  <span className="history-actions">
                    <button type="button" onClick={() => openDetail(record)}>
                      상세 보기
                    </button>
                    {record.grade === "F" ? (
                      <button type="button" onClick={() => handleSupport(record)}>
                        도와주세요
                      </button>
                    ) : null}
                  </span>
                </div>
              ))}
            </div>
            <div className="table-footer">
              <span>전체 21개 중 1-10번째</span>
              <div className="pagination">
                <ChevronRight className="prev" size={18} />
                <strong>1</strong>
                <span>2</span>
                <span>3</span>
                <ChevronRight size={18} />
              </div>
            </div>
          </section>
        </div>
      );
    }

    if (activeView === "bulk") {
      return (
        <div className="view-stack">
          <PageTitle
            title="일괄 분석하기"
            description="등록된 광고 URL을 선택 후 한 번에 진단합니다. URL당 분석 요청 1개가 등록됩니다."
            action={<CreditPill />}
          />

          <section className="bulk-summary-card">
            <strong>{selectedUrlIds.length}개</strong>
            <span>URL 선택됨</span>
            <button
              className="primary-button"
              type="button"
              onClick={handleBulkStart}
              disabled={bulkState.running || selectedUrlIds.length === 0}
            >
              <Box size={17} />
              {bulkState.running ? "진단 중" : "일괄 진단 시작"}
            </button>
          </section>

          <section className="bulk-url-card">
            <div className="card-toolbar">
              <h2>진단할 URL 선택</h2>
              <div className="toolbar-right">
                <div className="search-field compact">
                  <Search size={16} />
                  <input placeholder="검색" />
                </div>
                <button className="danger-button" type="button">
                  <Trash2 size={16} />
                  URL 삭제
                </button>
              </div>
            </div>
            <label className="select-all-row">
              <input type="checkbox" />
              페이지 전체 선택/해제 (0/19)
            </label>
            <div className="url-select-list">
              {visibleManagedUrls.map((url) => (
                <label className="url-select-row" key={url.id}>
                  <input
                    type="checkbox"
                    checked={selectedUrlIds.includes(url.id)}
                    onChange={() => toggleSelectedUrl(url.id)}
                  />
                  <span className="url-title-stack">
                    <strong>{getDisplayHost(url.url)}</strong>
                    <small>{normalizeUrl(url.url)}</small>
                  </span>
                  <ExternalLink size={16} />
                </label>
              ))}
            </div>
          </section>
        </div>
      );
    }

    if (activeView === "urls") {
      return (
        <div className="view-stack">
          <PageTitle
            title="URL 관리"
            description="광고에 사용하는 랜딩 페이지 URL을 등록하고 일괄 진단하세요"
            action={
              <button className="primary-button" type="button" onClick={bridgeToBulk}>
                <Box size={17} />
                일괄 진단
              </button>
            }
          />

          <section className="url-add-card">
            <div className="card-toolbar">
              <h2>
                <Plus size={18} />
                URL 추가
              </h2>
              <div className="toolbar-right">
                <button
                  className="text-button dark"
                  type="button"
                  onClick={handleTemplateDownload}
                >
                  <Download size={16} />
                  CSV 양식
                </button>
                <button
                  className="secondary-button"
                  type="button"
                  onClick={handleCsvApply}
                >
                  <Upload size={16} />
                  CSV 업로드
                </button>
              </div>
            </div>
            <form className="url-add-form" onSubmit={handleAddUrl}>
              <input
                value={urlForm.advertiserName}
                onChange={(event) =>
                  setUrlForm((current) => ({
                    ...current,
                    advertiserName: event.target.value,
                  }))
                }
                placeholder="이름 (예: 메인 랜딩)"
              />
              <div className="url-input-shell">
                <Link2 size={16} />
                <input
                  value={urlForm.url}
                  onChange={(event) =>
                    setUrlForm((current) => ({
                      ...current,
                      url: event.target.value,
                    }))
                  }
                  placeholder="https://yoursite.com/landing"
                />
              </div>
              <button className="primary-button" type="submit">
                <Plus size={17} />
                추가
              </button>
              <input
                value={urlForm.managerName}
                onChange={(event) =>
                  setUrlForm((current) => ({
                    ...current,
                    managerName: event.target.value,
                  }))
                }
                placeholder="담당자명 (선택)"
              />
              <input
                value={urlForm.memo}
                onChange={(event) =>
                  setUrlForm((current) => ({
                    ...current,
                    memo: event.target.value,
                  }))
                }
                placeholder="광고주명 (선택)"
              />
            </form>
          </section>

          <section className="registered-url-card">
            <div className="card-toolbar">
              <h2>
                등록된 URL <span>{visibleManagedUrls.length}개</span>
              </h2>
              <div className="search-field compact">
                <Search size={16} />
                <input placeholder="검색" />
              </div>
            </div>
            <div className="registered-url-table">
              <div className="registered-url-row registered-url-head">
                <span>
                  <input type="checkbox" />
                </span>
                <span>URL / 이름</span>
                <span>담당자</span>
                <span>광고주</span>
                <span>등록일</span>
              </div>
              {visibleManagedUrls.map((url) => (
                <label className="registered-url-row" key={url.id}>
                  <span>
                    <input
                      type="checkbox"
                      checked={selectedUrlIds.includes(url.id)}
                      onChange={() => toggleSelectedUrl(url.id)}
                    />
                  </span>
                  <span className="url-title-stack">
                    <strong>{getDisplayHost(url.url)}</strong>
                    <small>
                      {normalizeUrl(url.url)}
                      <ExternalLink size={13} />
                    </small>
                  </span>
                  <span>{url.managerName}</span>
                  <span>{url.advertiserName}</span>
                  <span>
                    {url.lastAuditedAt ? formatFullDate(url.lastAuditedAt) : "-"}
                  </span>
                </label>
              ))}
            </div>
          </section>
        </div>
      );
    }

    return (
      <div className="view-stack">
        <PageTitle
          title="안녕하세요, (주)나스미디어님"
          description="네이버 ADVoost 검색 광고 연결 URL을 분석하고 개선하세요."
          action={
            <button
              className="primary-button"
              type="button"
              onClick={() => setActiveView("single")}
            >
              <Search size={17} />
              URL 분석하기
            </button>
          }
        />

        <section className="summary-strip-card">
          <h2>SEO 분석 요약</h2>
          <div className="summary-strip">
            <div className="summary-cell active">
              <span>사용 가능한 크레딧</span>
              <strong>9,949</strong>
            </div>
            <div className="summary-cell">
              <span>소진한 크레딧</span>
              <strong>69</strong>
            </div>
            <div className="summary-cell danger">
              <span>발견된 문제점</span>
              <strong>{dashboardStats.issueCount}개</strong>
            </div>
            <div className="summary-cell success">
              <span>해결된 문제점</span>
              <strong>0개</strong>
            </div>
            <div className="summary-cell warning">
              <span>해결중인 문제점</span>
              <strong>{dashboardStats.issueCount}개</strong>
            </div>
          </div>
        </section>

        <section className="dashboard-mini-cards">
          <MetricCard
            icon={<History size={19} />}
            label="총 분석 횟수"
            value={records.length.toString()}
            detail=""
            tone="blue"
          />
          <MetricCard
            icon={<LifeBuoy size={19} />}
            label="처리중 지원"
            value={dashboardStats.inProgress.toString()}
            detail=""
            tone="amber"
          />
          <MetricCard
            icon={<CheckCircle2 size={19} />}
            label="완료된 분석"
            value={dashboardStats.completedCount.toString()}
            detail=""
            tone="blue"
          />
        </section>

        <section className="dashboard-panels">
          <div className="dashboard-panel">
            <div className="panel-title-row">
              <h2>최근 분석 내역</h2>
              <button type="button" onClick={() => setActiveView("history")}>
                전체보기
              </button>
            </div>
            <div className="recent-list">
              {historyRows.slice(0, 5).map((record) => (
                <button
                  className="recent-row"
                  key={record.id}
                  type="button"
                  onClick={() => openDetail(record)}
                >
                  <span>
                    <strong>{record.url}</strong>
                    <small>{formatDate(record.createdAt)}</small>
                  </span>
                  <StatusBadge status={record.status} />
                </button>
              ))}
            </div>
          </div>
          <div className="dashboard-panel support-empty">
            <div className="panel-title-row">
              <h2>기술 지원 현황</h2>
              <button type="button">전체보기</button>
            </div>
            <div className="empty-support">
              <p>기술 지원 요청 내역이 없습니다.</p>
              <strong>분석 후 지원을 요청하세요</strong>
            </div>
          </div>
        </section>
      </div>
    );
  })();

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand-lockup">
          <strong>
            <span>ADVoost</span> 검색
          </strong>
          <i>×</i>
          <em>SEO.co.kr</em>
        </div>
        <div className="sidebar-credit">
          <Coins size={15} />
          <span>크레딧</span>
          <strong>9949</strong>
        </div>
        <nav>
          <button
            className={activeView === "dashboard" ? "nav-button active" : "nav-button"}
            type="button"
            onClick={() => setActiveView("dashboard")}
          >
            <LayoutDashboard size={18} />
            대시보드
            <ChevronRight size={16} />
          </button>
          <span className="nav-section-label">진단</span>
          {navItems
            .filter((item) => ["single", "history"].includes(item.key))
            .map((item) => (
              <button
                className={
                  activeView === item.key || (activeView === "detail" && item.key === "history")
                    ? "nav-button active"
                    : "nav-button"
                }
                key={item.key}
                type="button"
                onClick={() => setActiveView(item.key)}
              >
                {item.icon}
                {item.label}
                <ChevronRight size={16} />
              </button>
            ))}
          <span className="nav-section-label">일괄 진단</span>
          {navItems
            .filter((item) => ["bulk", "urls"].includes(item.key))
            .map((item) => (
              <button
                className={activeView === item.key ? "nav-button active" : "nav-button"}
                key={item.key}
                type="button"
                onClick={() => setActiveView(item.key)}
              >
                {item.icon}
                {item.label}
                <ChevronRight size={16} />
              </button>
            ))}
          <span className="nav-section-label">지원</span>
          <button className="nav-button" type="button">
            <LifeBuoy size={18} />
            기술 지원
          </button>
          <button className="nav-button" type="button">
            <MessageSquare size={18} />
            문의하기
          </button>
          <button className="nav-button" type="button">
            <Activity size={18} />
            FAQ
          </button>
        </nav>
        <div className="sidebar-footer">
          <button className="nav-button" type="button">
            <Settings size={18} />
            설정
          </button>
          <button className="nav-button" type="button">
            <LogOut size={18} />
            로그아웃
          </button>
          <div className="sidebar-account">
            <strong>(주)나스미디어</strong>
            <span>nasmedia@seo.co.kr</span>
          </div>
        </div>
      </aside>

      <div className="workspace">
        <div className="workspace-inner">
          {pdfBanner ? (
            <div className={`async-banner banner-${pdfBanner.status}`}>
              <div>
                <FileArchive size={18} />
                <span>
                  {pdfBanner.status === "building"
                    ? `${pdfBanner.ids.length}개 PDF 생성 중`
                    : `${pdfBanner.ids.length}개 PDF ZIP 다운로드 준비 완료`}
                </span>
              </div>
              {pdfBanner.status === "ready" ? (
                <button type="button" onClick={downloadPdfArchive}>
                  다운로드
                </button>
              ) : null}
            </div>
          ) : null}

          {content}
        </div>
      </div>

      {toast ? (
        <div className={`toast toast-${toast.tone}`}>
          <strong>{toast.title}</strong>
          <span>{toast.message}</span>
        </div>
      ) : null}
    </main>
  );
}
