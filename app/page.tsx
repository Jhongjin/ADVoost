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
  RefreshCw,
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
  ApiAuditJobResponse,
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

type PdfMode = "standard" | "premium";
type PdfPlatform = "카페24" | "고도몰" | "아임웹" | "워드프레스" | "자체개발";
type PdfJobStatus = "building" | "ready" | "failed";

type PdfJob = {
  id: string;
  mode: PdfMode;
  platform: PdfPlatform;
  records: AuditRecord[];
  status: PdfJobStatus;
  createdAt: string;
  completedAt?: string;
  objectUrl?: string;
  filename?: string;
  error?: string;
};

type PdfDialogState = {
  mode: PdfMode;
  platform: PdfPlatform;
  records: AuditRecord[];
  bundle: boolean;
};

const API_BASE_URL =
  process.env.NEXT_PUBLIC_AUDIT_API_URL ?? "http://localhost:8000";
const UNLIMITED_CREDITS = "∞";
const JOB_POLL_INTERVAL_MS = 1500;
const JOB_MAX_POLLS = 120;
const PDF_PLATFORMS: PdfPlatform[] = [
  "카페24",
  "고도몰",
  "아임웹",
  "워드프레스",
  "자체개발",
];
const HISTORY_MIN_ROWS = 21;
const HISTORY_PAGE_SIZES = [10, 20, 50];

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
  document.body.appendChild(link);
  link.click();
  link.remove();
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
      <strong>{UNLIMITED_CREDITS}</strong>
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

const wait = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms));

async function requestAuditDirect(recordInput: {
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

async function requestHistoryRecords() {
  const response = await fetch(`${API_BASE_URL}/api/history?user_id=internal-platform`);

  if (!response.ok) {
    throw new Error(`History API failed: ${response.status}`);
  }

  const body = (await response.json()) as ApiAuditResponse[];
  return body.map(apiResponseToRecord);
}

async function requestAudit(recordInput: {
  url: string;
  managerName: string;
  advertiserName: string;
}, onProgress?: (progress: number, status: ApiAuditJobResponse["status"]) => void) {
  const response = await fetch(`${API_BASE_URL}/api/audit-jobs`, {
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

  if (response.status === 404 || response.status === 405) {
    onProgress?.(35, "running");
    return requestAuditDirect(recordInput);
  }

  if (!response.ok) {
    throw new Error(`Audit job API failed: ${response.status}`);
  }

  let job = (await response.json()) as ApiAuditJobResponse;
  onProgress?.(job.progress, job.status);

  for (let attempt = 0; attempt < JOB_MAX_POLLS; attempt += 1) {
    if (job.status === "completed" && job.result) {
      onProgress?.(100, "completed");
      return apiResponseToRecord(job.result);
    }
    if (job.status === "failed") {
      throw new Error(job.error ?? "Audit job failed");
    }

    await wait(JOB_POLL_INTERVAL_MS);
    const jobResponse = await fetch(`${API_BASE_URL}/api/audit-jobs/${job.job_id}`);
    if (!jobResponse.ok) {
      throw new Error(`Audit job polling failed: ${jobResponse.status}`);
    }
    job = (await jobResponse.json()) as ApiAuditJobResponse;
    onProgress?.(job.progress, job.status);
  }

  throw new Error("Audit job timed out.");
}

function parseDownloadFilename(header: string | null) {
  if (!header) {
    return null;
  }
  const encoded = header.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  if (encoded) {
    return decodeURIComponent(encoded);
  }
  return header.match(/filename="?([^";]+)"?/i)?.[1] ?? null;
}

function defaultPdfFilename(records: AuditRecord[], mode: PdfMode, bundle: boolean) {
  const firstHost = getDisplayHost(records[0]?.url ?? "report").replace(/[\\/:*?"<>|]/g, "_");
  if (bundle || records.length > 1) {
    return mode === "premium" ? "프리미엄_진단보고서_일괄.zip" : "진단보고서_일괄.zip";
  }
  return `${mode === "premium" ? "프리미엄_진단보고서" : "진단보고서"}_${firstHost}.pdf`;
}

function serializeRecordForReport(record: AuditRecord) {
  return {
    id: record.id,
    url: record.url,
    managerName: record.managerName,
    advertiserName: record.advertiserName,
    grade: record.grade,
    score: record.score,
    createdAt: record.createdAt,
    durationSec: record.durationSec,
    status: record.status,
    items: record.items,
    keywordSummary: record.keywordSummary,
    renderSnapshot: record.renderSnapshot,
  };
}

function recordHasReportData(record: AuditRecord) {
  const hasKeywords = Boolean(
    record.keywordSummary?.singleRows.length || record.keywordSummary?.phraseRows.length,
  );
  const hasSnapshot = Boolean(
    record.renderSnapshot?.desktopScreenshot || record.renderSnapshot?.mobileScreenshot,
  );
  return record.items.length > 0 && hasKeywords && hasSnapshot;
}

function estimatePdfMinutes(count: number, mode: PdfMode) {
  return Math.max(1, Math.ceil(count * (mode === "premium" ? 2.5 : 1.2)));
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
  const [singleProgress, setSingleProgress] = useState(0);
  const [toast, setToast] = useState<ToastState | null>(null);
  const [selectedHistoryIds, setSelectedHistoryIds] = useState<string[]>([]);
  const [historySearch, setHistorySearch] = useState("");
  const [historyPageSize, setHistoryPageSize] = useState<number>(
    HISTORY_PAGE_SIZES[0],
  );
  const [historyPage, setHistoryPage] = useState(1);
  const [historyPageSizeOpen, setHistoryPageSizeOpen] = useState(false);
  const [pdfJobs, setPdfJobs] = useState<PdfJob[]>([]);
  const [pdfDialog, setPdfDialog] = useState<PdfDialogState | null>(null);
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

  useEffect(() => {
    let cancelled = false;

    async function loadHistory() {
      try {
        const historyRecords = await requestHistoryRecords();
        if (!cancelled && historyRecords.length > 0) {
          setRecords(historyRecords);
          setDetailRecord(historyRecords[0]);
        }
      } catch {
        // Keep the bundled demo data available when the backend is sleeping or empty.
      }
    }

    void loadHistory();
    return () => {
      cancelled = true;
    };
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
  const historySourceRows = useMemo(() => {
    const directRows = records;
    const existing = new Set(directRows.map((record) => normalizeUrl(record.url)));
    const targetLength = Math.max(HISTORY_MIN_ROWS, directRows.length);
    const fillers = managedUrls
      .filter((url) => !existing.has(normalizeUrl(url.url)))
      .slice(0, Math.max(0, targetLength - directRows.length))
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
  }, [records, managedUrls]);
  const filteredHistoryRows = useMemo(() => {
    const query = historySearch.trim().toLowerCase();
    if (!query) {
      return historySourceRows;
    }

    return historySourceRows.filter((record) => {
      const counts = countItems(record.items);
      return [
        record.url,
        getDisplayHost(record.url),
        record.status,
        record.grade,
        record.managerName,
        record.advertiserName,
        formatHistoryDate(record.createdAt),
        `${counts.PASS}/${counts.WARNING}/${counts.FAIL}`,
      ].some((value) => value.toLowerCase().includes(query));
    });
  }, [historySearch, historySourceRows]);
  const historyTotalPages = Math.max(
    1,
    Math.ceil(filteredHistoryRows.length / historyPageSize),
  );
  const historyCurrentPage = Math.min(historyPage, historyTotalPages);
  const historyRows = useMemo(() => {
    const start = (historyCurrentPage - 1) * historyPageSize;
    return filteredHistoryRows.slice(start, start + historyPageSize);
  }, [filteredHistoryRows, historyCurrentPage, historyPageSize]);
  const historyPageNumbers = useMemo(() => {
    const maxButtons = 5;
    if (historyTotalPages <= maxButtons) {
      return Array.from({ length: historyTotalPages }, (_, index) => index + 1);
    }
    const start = Math.max(
      1,
      Math.min(
        historyCurrentPage - Math.floor(maxButtons / 2),
        historyTotalPages - maxButtons + 1,
      ),
    );
    return Array.from({ length: maxButtons }, (_, index) => start + index);
  }, [historyCurrentPage, historyTotalPages]);
  const historyStartIndex =
    filteredHistoryRows.length > 0
      ? (historyCurrentPage - 1) * historyPageSize + 1
      : 0;
  const historyEndIndex = Math.min(
    historyCurrentPage * historyPageSize,
    filteredHistoryRows.length,
  );
  const selectedHistoryRows = useMemo(
    () =>
      historySourceRows.filter((record) => selectedHistoryIds.includes(record.id)),
    [historySourceRows, selectedHistoryIds],
  );
  const readyPdfJobs = useMemo(
    () => pdfJobs.filter((job) => job.status === "ready"),
    [pdfJobs],
  );

  useEffect(() => {
    setHistoryPage(1);
  }, [historySearch, historyPageSize]);

  useEffect(() => {
    setHistoryPage((current) => Math.min(current, historyTotalPages));
  }, [historyTotalPages]);

  useEffect(() => {
    const availableIds = new Set(historySourceRows.map((record) => record.id));
    setSelectedHistoryIds((current) => {
      const next = current.filter((id) => availableIds.has(id));
      return next.length === current.length ? current : next;
    });
  }, [historySourceRows]);

  function showToast(nextToast: ToastState) {
    setToast(nextToast);
    window.setTimeout(() => setToast(null), 3600);
  }

  function openDetail(record: AuditRecord) {
    setDetailRecord(record);
    setActiveView("detail");
  }

  async function prepareRecordsForPdf(targets: AuditRecord[]) {
    const prepared: AuditRecord[] = [];
    const refreshed: AuditRecord[] = [];

    for (const record of targets) {
      if (recordHasReportData(record)) {
        prepared.push(record);
        continue;
      }

      const nextRecord = await requestAudit({
        url: normalizeUrl(record.url),
        managerName: record.managerName,
        advertiserName: record.advertiserName,
      });
      prepared.push(nextRecord);
      refreshed.push(nextRecord);
    }

    if (refreshed.length > 0) {
      setRecords((current) => {
        const refreshedUrls = new Set(
          refreshed.map((record) => normalizeUrl(record.url)),
        );
        return [
          ...refreshed,
          ...current.filter(
            (record) => !refreshedUrls.has(normalizeUrl(record.url)),
          ),
        ];
      });
      setDetailRecord((current) => {
        const replacement = refreshed.find(
          (record) => normalizeUrl(record.url) === normalizeUrl(current.url),
        );
        return replacement ?? current;
      });
    }

    return prepared;
  }

  function openPdfDialog(
    targetRecords: AuditRecord | AuditRecord[],
    mode: PdfMode,
    bundle = false,
  ) {
    const targets = Array.isArray(targetRecords) ? targetRecords : [targetRecords];
    if (targets.length === 0) {
      showToast({
        tone: "warning",
        title: "선택 URL 없음",
        message: "PDF로 내보낼 분석 내역을 먼저 선택하세요.",
      });
      return;
    }
    setPdfDialog({
      mode,
      platform: "카페24",
      records: targets,
      bundle,
    });
  }

  function openHistoryPdfDialog(mode: PdfMode) {
    openPdfDialog(selectedHistoryRows, mode, true);
  }

  async function submitPdfDialog() {
    if (!pdfDialog) {
      return;
    }
    const dialogState = pdfDialog;
    const jobId = `PDF-${Date.now()}`;
    const nextJob: PdfJob = {
      id: jobId,
      mode: dialogState.mode,
      platform: dialogState.platform,
      records: dialogState.records,
      status: "building",
      createdAt: new Date().toISOString(),
    };
    setPdfJobs((current) => [nextJob, ...current]);
    setPdfDialog(null);
    showToast({
      tone: "info",
      title: "PDF 생성 요청",
      message: `${dialogState.records.length}개 리포트 생성을 시작했습니다.`,
    });

    try {
      const missingDataCount = dialogState.records.filter(
        (record) => !recordHasReportData(record),
      ).length;
      const reportRecords =
        missingDataCount > 0
          ? await prepareRecordsForPdf(dialogState.records)
          : dialogState.records;

      if (missingDataCount > 0) {
        setPdfJobs((current) =>
          current.map((job) =>
            job.id === jobId ? { ...job, records: reportRecords } : job,
          ),
        );
      }

      const response = await fetch(`${API_BASE_URL}/api/reports/pdf`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          records: reportRecords.map(serializeRecordForReport),
          report_type: dialogState.mode,
          platform: dialogState.platform,
          bundle: dialogState.bundle,
        }),
      });

      if (!response.ok) {
        let detail = "";
        try {
          const payload = (await response.clone().json()) as { detail?: unknown };
          detail = typeof payload.detail === "string" ? payload.detail : "";
        } catch {
          detail = "";
        }
        const reason = detail ? ` (${detail})` : "";
        throw new Error(`PDF 생성 실패: ${response.status}${reason}`);
      }

      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const filename =
        parseDownloadFilename(response.headers.get("Content-Disposition")) ??
        defaultPdfFilename(reportRecords, dialogState.mode, dialogState.bundle);
      setPdfJobs((current) =>
        current.map((job) =>
          job.id === jobId
            ? {
                ...job,
                status: "ready",
                completedAt: new Date().toISOString(),
                objectUrl,
                filename,
              }
            : job,
        ),
      );
      showToast({
        tone: "success",
        title: "PDF 준비 완료",
        message: `${filename} 파일을 다운로드할 수 있습니다.`,
      });
    } catch (error) {
      setPdfJobs((current) =>
        current.map((job) =>
          job.id === jobId
            ? {
                ...job,
                status: "failed",
                completedAt: new Date().toISOString(),
                error: error instanceof Error ? error.message : "PDF 생성 실패",
              }
            : job,
        ),
      );
      showToast({
        tone: "warning",
        title: "PDF 생성 실패",
        message: "백엔드 PDF 렌더러 상태를 확인하세요.",
      });
    }
  }

  function downloadPdfJob(job: PdfJob) {
    if (!job.objectUrl) {
      showToast({
        tone: "warning",
        title: "파일 준비 중",
        message: "PDF 생성이 완료된 뒤 다시 시도하세요.",
      });
      return;
    }
    const link = document.createElement("a");
    link.href = job.objectUrl;
    link.download = job.filename ?? defaultPdfFilename(job.records, job.mode, job.records.length > 1);
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  function toggleHistorySelection(recordId: string) {
    setSelectedHistoryIds((current) =>
      current.includes(recordId)
        ? current.filter((id) => id !== recordId)
        : [...current, recordId],
    );
  }

  function toggleAllHistoryRows() {
    const rowIds = historyRows.map((record) => record.id);
    if (rowIds.length === 0) {
      return;
    }
    const rowIdSet = new Set(rowIds);
    const allSelected = rowIds.every((id) => selectedHistoryIds.includes(id));
    setSelectedHistoryIds((current) =>
      allSelected
        ? current.filter((id) => !rowIdSet.has(id))
        : Array.from(new Set([...current, ...rowIds])),
    );
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
    setSingleProgress(5);
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
      }, (progress) => setSingleProgress(progress));
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
      setSingleProgress(0);
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
      progress: 5,
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
          }, (progress) =>
            setBulkState((current) => ({
              ...current,
              progress: Math.min(
                99,
                Math.round(((index + progress / 100) / queueSize) * 100),
              ),
            })),
          ),
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
          onCreatePdf={(record, mode) => openPdfDialog(record, mode, false)}
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
              {singleRunning ? (
                <div className="analysis-progress" aria-label={`분석 진행률 ${singleProgress}%`}>
                  <span>
                    <i style={{ width: `${Math.max(singleProgress, 8)}%` }} />
                  </span>
                  <em>{singleProgress}%</em>
                </div>
              ) : null}
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
              <strong>
                {historySearch.trim()
                  ? `검색 ${filteredHistoryRows.length}건`
                  : `전체 ${historySourceRows.length}건`}
              </strong>
              <div className="history-tools">
                <div className="search-field">
                  <Search size={16} />
                  <input
                    value={historySearch}
                    onChange={(event) => setHistorySearch(event.target.value)}
                    placeholder="검색"
                  />
                </div>
                <span className="rows-label">페이지당</span>
                <div className="page-size-menu">
                  <button
                    className="select-button"
                    type="button"
                    aria-expanded={historyPageSizeOpen}
                    onClick={() => setHistoryPageSizeOpen((current) => !current)}
                  >
                    {historyPageSize}개 <ChevronDown size={15} />
                  </button>
                  {historyPageSizeOpen ? (
                    <div className="page-size-options" role="menu">
                      {HISTORY_PAGE_SIZES.map((size) => (
                        <button
                          className={historyPageSize === size ? "active" : ""}
                          type="button"
                          role="menuitem"
                          key={size}
                          onClick={() => {
                            setHistoryPageSize(size);
                            setHistoryPageSizeOpen(false);
                          }}
                        >
                          {size}개
                        </button>
                      ))}
                    </div>
                  ) : null}
                </div>
              </div>
            </div>
            <div className="history-download-row">
              <button className="text-button dark" type="button">
                <FileDown size={16} />
                분석 다운로드
              </button>
              {selectedHistoryRows.length > 0 ? (
                <span className="selected-chip">{selectedHistoryRows.length}개 선택됨</span>
              ) : null}
              {readyPdfJobs.length > 0 ? (
                <span className="ready-chip">{readyPdfJobs.length}건 준비됨</span>
              ) : null}
              <ChevronDown size={15} />
              <div className="download-spacer" />
              <button
                className="secondary-button muted-action"
                type="button"
                disabled={selectedHistoryRows.length === 0}
                onClick={() => openHistoryPdfDialog("standard")}
              >
                <FileArchive size={16} />
                PDF보고서 일괄 다운로드
              </button>
              <button
                className="secondary-button premium-action"
                type="button"
                disabled={selectedHistoryRows.length === 0}
                onClick={() => openHistoryPdfDialog("premium")}
              >
                <FileArchive size={16} />
                프리미엄 PDF 일괄 다운로드
              </button>
              <button
                className="text-button subdued"
                type="button"
                onClick={() => setSelectedHistoryIds([])}
              >
                선택 해제
              </button>
            </div>
            <div className="premium-row">
              <div className="premium-row-title">
                <FileArchive size={16} />
                프리미엄 PDF 다운로드
                {readyPdfJobs.length > 0 ? (
                  <span className="ready-chip">{readyPdfJobs.length}건 준비됨</span>
                ) : null}
              </div>
              <button
                className="text-button dark"
                type="button"
                onClick={() =>
                  showToast({
                    tone: "info",
                    title: "목록 새로고침",
                    message: "현재 브라우저 세션의 PDF 작업 목록을 표시 중입니다.",
                  })
                }
              >
                <RefreshCw size={16} />
                새로고침
              </button>
            </div>
            <div className="pdf-job-list">
              {pdfJobs.length === 0 ? (
                <div className="pdf-job-empty">완료 후 3일간 보관</div>
              ) : (
                pdfJobs.map((job) => (
                  <div className={`pdf-job-row job-${job.status}`} key={job.id}>
                    <span className={`pdf-job-status status-${job.status}`}>
                      {job.status === "building"
                        ? "대기 중"
                        : job.status === "ready"
                          ? "완료"
                          : "실패"}
                    </span>
                    <span>{formatHistoryDate(job.createdAt)}</span>
                    <strong>{job.records.length}건</strong>
                    <span>{job.mode === "premium" ? job.platform : "일반 PDF"}</span>
                    <span>
                      {job.status === "ready" && job.completedAt
                        ? `완료: ${formatHistoryDate(job.completedAt)}`
                        : job.status === "failed"
                          ? job.error ?? "생성 실패"
                          : "처리 중"}
                    </span>
                    {job.status === "ready" ? (
                      <button type="button" onClick={() => downloadPdfJob(job)}>
                        <Download size={15} />
                        다운로드
                      </button>
                    ) : job.status === "failed" ? (
                      <span className="pdf-job-failed">실패</span>
                    ) : (
                      <span className="pdf-job-spinner">처리 중</span>
                    )}
                  </div>
                ))
              )}
            </div>
            <div className="history-table">
              <div className="history-table-row history-table-head">
                <span>
                  <input
                    type="checkbox"
                    checked={
                      historyRows.length > 0 &&
                      historyRows.every((record) =>
                        selectedHistoryIds.includes(record.id),
                      )
                    }
                    disabled={historyRows.length === 0}
                    onChange={toggleAllHistoryRows}
                  />
                </span>
                <span>URL</span>
                <span>상태</span>
                <span>등급</span>
                <span>통과/경고/실패</span>
                <span>분석일시</span>
                <span>담당자</span>
                <span>광고주</span>
                <span>액션</span>
              </div>
              {historyRows.length > 0 ? (
                historyRows.map((record, index) => (
                  <div className="history-table-row" key={`${record.id}-${index}`}>
                    <span>
                      <input
                        type="checkbox"
                        checked={selectedHistoryIds.includes(record.id)}
                        onChange={() => toggleHistorySelection(record.id)}
                      />
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
                ))
              ) : (
                <div className="history-table-empty">검색 결과가 없습니다.</div>
              )}
            </div>
            <div className="table-footer">
              <span>
                {filteredHistoryRows.length > 0
                  ? `전체 ${filteredHistoryRows.length}개 중 ${historyStartIndex}-${historyEndIndex}번째`
                  : `전체 ${historySourceRows.length}개 중 0번째`}
              </span>
              <div className="pagination">
                <button
                  type="button"
                  aria-label="이전 페이지"
                  disabled={historyCurrentPage <= 1}
                  onClick={() =>
                    setHistoryPage((current) => Math.max(1, current - 1))
                  }
                >
                  <ChevronRight className="prev" size={18} />
                </button>
                {historyPageNumbers.map((page) => (
                  <button
                    className={historyCurrentPage === page ? "active" : ""}
                    type="button"
                    key={page}
                    aria-current={historyCurrentPage === page ? "page" : undefined}
                    onClick={() => setHistoryPage(page)}
                  >
                    {page}
                  </button>
                ))}
                <button
                  type="button"
                  aria-label="다음 페이지"
                  disabled={historyCurrentPage >= historyTotalPages}
                  onClick={() =>
                    setHistoryPage((current) =>
                      Math.min(historyTotalPages, current + 1),
                    )
                  }
                >
                  <ChevronRight size={18} />
                </button>
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
            {bulkState.running ? (
              <div className="bulk-progress">
                <span>
                  <i style={{ width: `${bulkState.progress}%` }} />
                </span>
                <em>
                  {bulkState.completed}/{bulkState.queued} 완료 · {bulkState.progress}%
                </em>
              </div>
            ) : null}
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
              <strong>{UNLIMITED_CREDITS}</strong>
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
              {historySourceRows.slice(0, 5).map((record) => (
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
          <strong>{UNLIMITED_CREDITS}</strong>
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
          {content}
        </div>
      </div>

      {pdfDialog ? (
        <div className="modal-scrim pdf-modal-scrim" role="dialog" aria-modal="true">
          <div className="pdf-modal-panel">
            <button
              className="pdf-modal-close"
              type="button"
              aria-label="닫기"
              onClick={() => setPdfDialog(null)}
            >
              ×
            </button>
            {pdfDialog.mode === "premium" ? (
              <>
                <h2>플랫폼 선택 — 프리미엄 PDF {pdfDialog.bundle ? "일괄 다운로드" : "다운로드"}</h2>
                <p>
                  사용 중인 쇼핑몰/CMS 플랫폼을 선택해 주세요.
                  <br />
                  선택한 플랫폼의 수정 가이드가 PDF에 포함됩니다.
                </p>
                <div className="platform-grid">
                  {PDF_PLATFORMS.map((platform) => (
                    <button
                      className={pdfDialog.platform === platform ? "active" : ""}
                      type="button"
                      key={platform}
                      onClick={() =>
                        setPdfDialog((current) =>
                          current ? { ...current, platform } : current,
                        )
                      }
                    >
                      {platform}
                    </button>
                  ))}
                </div>
              </>
            ) : (
              <>
                <h2>PDF 보고서 {pdfDialog.bundle ? "일괄 다운로드" : "다운로드"}</h2>
                <p>
                  선택한 분석 결과를 PDF 보고서로 생성합니다. 완료 후 다운로드
                  목록에서 파일을 받을 수 있습니다.
                </p>
              </>
            )}
            <div className="pdf-modal-summary">
              <span>
                대상 URL: <strong>{pdfDialog.records.length}개</strong>
              </span>
              <span>
                예상 처리시간:{" "}
                <strong>
                  {estimatePdfMinutes(pdfDialog.records.length, pdfDialog.mode)}분
                </strong>
              </span>
              <span>
                내부 플랫폼: <strong>크레딧 차감 없음</strong>
              </span>
            </div>
            <label className="pdf-modal-checkbox">
              <input type="checkbox" />
              다시 열지 않기
            </label>
            <div className="pdf-modal-actions">
              <button
                className="secondary-button"
                type="button"
                onClick={() => setPdfDialog(null)}
              >
                취소
              </button>
              <button className="primary-button" type="button" onClick={submitPdfDialog}>
                <FileArchive size={17} />
                {pdfDialog.mode === "premium"
                  ? pdfDialog.bundle
                    ? "일괄 다운로드 요청"
                    : "다운로드 요청"
                  : "확인"}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {toast ? (
        <div className={`toast toast-${toast.tone}`}>
          <strong>{toast.title}</strong>
          <span>{toast.message}</span>
        </div>
      ) : null}
    </main>
  );
}
