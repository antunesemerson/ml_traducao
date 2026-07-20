import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { createPortal } from 'react-dom';
import './styles.css';
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  BarChart3,
  BrainCircuit,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Cpu,
  Database,
  ExternalLink,
  FileWarning,
  GitBranch,
  Home,
  Layers3,
  LayoutDashboard,
  Lock,
  Moon,
  PackageSearch,
  Play,
  RotateCcw,
  Route,
  Scale,
  Rocket,
  SearchCheck,
  ShieldAlert,
  ShieldCheck,
  Star,
  Sun,
  TerminalSquare,
  Unlock,
  Workflow,
  XCircle,
} from 'lucide-react';
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip as RechartsTooltip,
  XAxis,
  YAxis,
} from 'recharts';

const API_BASE = import.meta.env.VITE_DASHBOARD_API ?? 'http://127.0.0.1:8765/api';

const fmt = (value) => Number(value ?? 0).toLocaleString('pt-BR');
const compact = (value) => Intl.NumberFormat('pt-BR', { notation: 'compact', maximumFractionDigits: 1 }).format(Number(value ?? 0));
const pct = (value) => `${Number(value ?? 0).toLocaleString('pt-BR', { maximumFractionDigits: 2 })}%`;
const metric = (value) => Number(value ?? 0).toLocaleString('pt-BR', { maximumFractionDigits: 4 });
const pctMetric = (value) => pct(Number(value ?? 0) * 100);
const fmtBytes = (value) => {
  const bytes = Number(value);
  if (!Number.isFinite(bytes)) return 'nao medido';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let size = Math.abs(bytes);
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  const sign = bytes < 0 ? '-' : '';
  return `${sign}${size.toLocaleString('pt-BR', { maximumFractionDigits: unitIndex === 0 ? 0 : 1 })} ${units[unitIndex]}`;
};
const shortDateTime = (value) => {
  if (!value) return 'pending_instrumentation';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'short' });
};
const compactDateTime = (value) => {
  if (!value) return 'pendente';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString('pt-BR', {
    day: '2-digit',
    month: '2-digit',
    year: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
};
const parseRunIdDate = (value) => {
  const match = String(value ?? '').match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/);
  if (!match) return null;
  const [, year, month, day, hour, minute, second] = match;
  const date = new Date(
    Number(year),
    Number(month) - 1,
    Number(day),
    Number(hour),
    Number(minute),
    Number(second)
  );
  return Number.isNaN(date.getTime()) ? null : date;
};
const runDateTimeLabel = (runId, fallbackIso) => {
  const fallback = fallbackIso ? new Date(fallbackIso) : null;
  const date = parseRunIdDate(runId) ?? (fallback && !Number.isNaN(fallback.getTime()) ? fallback : null);
  if (!date) return runId ? String(runId) : '-';
  return date.toLocaleString('pt-BR', {
    day: '2-digit',
    month: '2-digit',
    year: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
};
const ageLabel = (value) => {
  if (!value) return 'pending_instrumentation';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'pending_instrumentation';
  const diffMs = Date.now() - date.getTime();
  if (diffMs < 0) return 'agora';
  const minutes = Math.floor(diffMs / 60000);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} h`;
  return `${Math.floor(hours / 24)} dias`;
};
const timestampMs = (value) => {
  if (!value) return null;
  const parsed = new Date(value).getTime();
  return Number.isFinite(parsed) ? parsed : null;
};
const durationLabel = (ms) => {
  const value = Number(ms);
  if (!Number.isFinite(value) || value < 0) return 'nao medido';
  const totalSeconds = Math.floor(value / 1000);
  const seconds = totalSeconds % 60;
  const totalMinutes = Math.floor(totalSeconds / 60);
  const minutes = totalMinutes % 60;
  const hours = Math.floor(totalMinutes / 60);
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, '0')}min`;
  if (minutes > 0) return `${minutes}min ${String(seconds).padStart(2, '0')}s`;
  return `${seconds}s`;
};
const clampNumber = (value, min = 0, max = 100) => {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return min;
  return Math.max(min, Math.min(max, parsed));
};
const logLineText = (line) => {
  if (line === null || line === undefined) return '';
  if (typeof line === 'string') return line;
  return line.message ?? line.line ?? line.event ?? JSON.stringify(line);
};

const colorClasses = {
  blue: 'bg-blue-50 dark:bg-blue-900/20 text-blue-600 dark:text-blue-400',
  emerald: 'bg-emerald-50 dark:bg-emerald-900/20 text-emerald-600 dark:text-emerald-400',
  amber: 'bg-amber-50 dark:bg-amber-900/20 text-amber-600 dark:text-amber-400',
  red: 'bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400',
  violet: 'bg-violet-50 dark:bg-violet-900/20 text-violet-600 dark:text-violet-400',
  slate: 'bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300',
};

const chartText = { fontSize: 12, fill: 'currentColor', opacity: 0.65 };

const cn = (...classes) => classes.filter(Boolean).join(' ');

const GLOBAL_TOOLTIP_ID = 'global-dashboard-tooltip';
const GLOBAL_TOOLTIP_SELECTOR = '[data-tooltip], [data-tooltip-description], [data-tooltip-title]';

const tooltipPayloadFor = (anchor) => {
  let title = String(anchor.dataset.tooltipTitle ?? '').trim();
  let description = String(anchor.dataset.tooltipDescription ?? anchor.dataset.tooltip ?? '').trim();
  let meta = String(anchor.dataset.tooltipMeta ?? '').trim();

  const descriptionLines = description.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  if (!title && descriptionLines.length > 1) {
    title = descriptionLines.shift();
    const statusIndex = descriptionLines.findIndex((line) => /^Status:/i.test(line));
    if (!meta && statusIndex >= 0) meta = descriptionLines.splice(statusIndex, 1)[0];
    description = descriptionLines.join('\n');
  }

  if (!meta && description) {
    const statusMatch = description.match(/(?:^|\s)(Status:\s*[^.]+)\.?$/i);
    if (statusMatch?.index > 0) {
      meta = statusMatch[1].trim();
      description = description.slice(0, statusMatch.index).trim();
    }
  }

  if (!title && description) {
    const colonIndex = description.indexOf(':');
    const candidate = colonIndex > 0 ? description.slice(0, colonIndex).trim() : '';
    if (candidate && candidate.length <= 42 && !/[\\/]/.test(candidate)) {
      title = candidate;
      description = description.slice(colonIndex + 1).trim();
    } else if (description.length <= 72 && !description.includes('\n')) {
      title = description;
      description = '';
    }
  }

  return title || description || meta ? { title, description, meta } : null;
};

function GlobalTooltipLayer() {
  const [tooltip, setTooltip] = useState(null);
  const activeAnchorRef = useRef(null);

  useEffect(() => {
    const migrateTitle = (element) => {
      if (!(element instanceof Element)) return;
      const nativeTitle = String(element.getAttribute('title') ?? '').trim();
      if (!nativeTitle) {
        if (element.hasAttribute('title')) element.removeAttribute('title');
        return;
      }
      const hasExplicitTooltip = element.hasAttribute('data-tooltip-title') || element.hasAttribute('data-tooltip-description');
      const isMigratedTooltip = element.dataset.tooltipSource === 'native-title';
      if (!hasExplicitTooltip && (!element.hasAttribute('data-tooltip') || isMigratedTooltip)) {
        element.dataset.tooltip = nativeTitle;
        element.dataset.tooltipSource = 'native-title';
      }
      element.removeAttribute('title');
    };

    const migrateTree = (root) => {
      if (!(root instanceof Element)) return;
      migrateTitle(root);
      root.querySelectorAll('[title]').forEach(migrateTitle);
    };

    const restoreDescription = (anchor) => {
      if (!(anchor instanceof Element) || !anchor.hasAttribute('data-tooltip-previous-describedby')) return;
      const previous = anchor.getAttribute('data-tooltip-previous-describedby') ?? '';
      if (previous) anchor.setAttribute('aria-describedby', previous);
      else anchor.removeAttribute('aria-describedby');
      anchor.removeAttribute('data-tooltip-previous-describedby');
    };

    const hideTooltip = () => {
      restoreDescription(activeAnchorRef.current);
      activeAnchorRef.current = null;
      setTooltip(null);
    };

    const showTooltip = (anchor) => {
      const payload = tooltipPayloadFor(anchor);
      if (!payload) return;
      const rect = anchor.getBoundingClientRect();
      if (!rect.width && !rect.height) return;

      if (activeAnchorRef.current && activeAnchorRef.current !== anchor) {
        restoreDescription(activeAnchorRef.current);
      }
      activeAnchorRef.current = anchor;

      if (!anchor.hasAttribute('data-tooltip-previous-describedby')) {
        const previous = anchor.getAttribute('aria-describedby') ?? '';
        anchor.setAttribute('data-tooltip-previous-describedby', previous);
        const describedBy = previous.split(/\s+/).filter(Boolean);
        if (!describedBy.includes(GLOBAL_TOOLTIP_ID)) describedBy.push(GLOBAL_TOOLTIP_ID);
        anchor.setAttribute('aria-describedby', describedBy.join(' '));
      }

      const viewportPadding = 12;
      const maxWidth = Math.max(180, Math.min(380, window.innerWidth - viewportPadding * 2));
      const halfWidth = maxWidth / 2;
      const anchorCenter = rect.left + rect.width / 2;
      const left = Math.min(
        Math.max(anchorCenter, viewportPadding + halfWidth),
        window.innerWidth - viewportPadding - halfWidth
      );
      const belowSpace = window.innerHeight - rect.bottom;
      const placement = belowSpace >= 140 || belowSpace >= rect.top ? 'bottom' : 'top';

      setTooltip({
        ...payload,
        left,
        maxWidth,
        placement,
        top: placement === 'bottom' ? rect.bottom + 10 : Math.max(viewportPadding, rect.top - 10),
      });
    };

    const findAnchor = (target) => target instanceof Element ? target.closest(GLOBAL_TOOLTIP_SELECTOR) : null;
    const onPointerOver = (event) => {
      const anchor = findAnchor(event.target);
      if (!anchor || anchor === activeAnchorRef.current) return;
      showTooltip(anchor);
    };
    const onPointerOut = (event) => {
      const anchor = activeAnchorRef.current;
      if (!anchor) return;
      if (event.relatedTarget instanceof Node && anchor.contains(event.relatedTarget)) return;
      hideTooltip();
    };
    const onFocusIn = (event) => {
      const anchor = findAnchor(event.target);
      if (anchor) showTooltip(anchor);
    };
    const onFocusOut = (event) => {
      const anchor = activeAnchorRef.current;
      if (!anchor) return;
      if (event.relatedTarget instanceof Node && anchor.contains(event.relatedTarget)) return;
      hideTooltip();
    };
    const onKeyDown = (event) => {
      if (event.key === 'Escape') hideTooltip();
    };

    migrateTree(document.body);
    const observer = new MutationObserver((mutations) => {
      mutations.forEach((mutation) => {
        if (mutation.type === 'attributes') migrateTitle(mutation.target);
        mutation.addedNodes.forEach((node) => migrateTree(node));
      });
    });
    observer.observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['title'] });

    document.addEventListener('pointerover', onPointerOver, true);
    document.addEventListener('pointerout', onPointerOut, true);
    document.addEventListener('focusin', onFocusIn, true);
    document.addEventListener('focusout', onFocusOut, true);
    document.addEventListener('keydown', onKeyDown, true);
    document.addEventListener('scroll', hideTooltip, true);
    window.addEventListener('resize', hideTooltip);

    return () => {
      observer.disconnect();
      hideTooltip();
      document.removeEventListener('pointerover', onPointerOver, true);
      document.removeEventListener('pointerout', onPointerOut, true);
      document.removeEventListener('focusin', onFocusIn, true);
      document.removeEventListener('focusout', onFocusOut, true);
      document.removeEventListener('keydown', onKeyDown, true);
      document.removeEventListener('scroll', hideTooltip, true);
      window.removeEventListener('resize', hideTooltip);
    };
  }, []);

  if (!tooltip || typeof document === 'undefined') return null;
  return createPortal(
    <div
      id={GLOBAL_TOOLTIP_ID}
      role="tooltip"
      className="dashboard-tooltip-surface global-dashboard-tooltip pointer-events-none fixed z-[100000] rounded-xl border border-slate-500/45 bg-slate-950/[0.98] px-3.5 py-3 text-left text-xs leading-relaxed text-slate-200 shadow-[0_18px_55px_rgba(0,0,0,0.55)]"
      style={{
        left: tooltip.left,
        top: tooltip.top,
        width: 'max-content',
        minWidth: Math.min(220, tooltip.maxWidth),
        maxWidth: tooltip.maxWidth,
        transform: tooltip.placement === 'top' ? 'translate(-50%, -100%)' : 'translateX(-50%)',
        whiteSpace: 'pre-line',
      }}
    >
      {tooltip.title && <p className="font-bold text-white">{tooltip.title}</p>}
      {tooltip.description && (
        <p className={cn(tooltip.title && 'mt-1.5', 'text-[11px] leading-[1.55] text-slate-300')}>
          {tooltip.description}
        </p>
      )}
      {tooltip.meta && (
        <p className={cn((tooltip.title || tooltip.description) && 'mt-2', 'border-t border-slate-700/70 pt-2 text-[10px] font-bold text-cyan-300')}>
          {tooltip.meta}
        </p>
      )}
    </div>,
    document.body
  );
}

const chartTooltipContentStyle = {
  borderRadius: 12,
  border: '1px solid rgba(100, 116, 139, 0.45)',
  background: 'rgba(2, 6, 23, 0.96)',
  boxShadow: '0 18px 55px rgba(0, 0, 0, 0.5)',
  color: '#e2e8f0',
  padding: '12px 14px',
  fontSize: 12,
  fontSynthesis: 'none',
  lineHeight: 1.5,
};

const Tooltip = ({ contentStyle, labelStyle, itemStyle, wrapperStyle, allowEscapeViewBox, ...props }) => (
  <RechartsTooltip
    {...props}
    allowEscapeViewBox={allowEscapeViewBox ?? { x: true, y: true }}
    contentStyle={{ ...chartTooltipContentStyle, ...contentStyle }}
    labelStyle={{ color: '#f8fafc', fontWeight: 700, marginBottom: 6, ...labelStyle }}
    itemStyle={{ color: '#cbd5e1', paddingTop: 2, paddingBottom: 2, ...itemStyle }}
    wrapperStyle={{ zIndex: 10000, outline: 'none', ...wrapperStyle }}
  />
);

const ModelTooltip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null;
  const source = payload[0]?.payload ?? {};
  return (
    <div className="dashboard-tooltip-surface rounded-xl border border-slate-500/45 bg-slate-950/[0.98] px-3.5 py-3 text-xs leading-relaxed text-slate-200 shadow-[0_18px_55px_rgba(0,0,0,0.5)]">
      <p className="mb-2 font-bold text-white">{source.modelVersion ?? `Run ${label}`}</p>
      {payload.map((item) => (
        <p key={item.dataKey} style={{ color: item.color }}>
          {item.name}: {['falseSafe', 'predictedSafe', 'risk'].includes(item.dataKey) ? fmt(item.value) : item.dataKey === 'holdoutCoverage' || item.dataKey === 'safePrecision' ? pct(item.value) : pct(Number(item.value) * 100)}
        </p>
      ))}
    </div>
  );
};

const CockpitTooltip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null;
  const source = payload[0]?.payload ?? {};
  return (
    <div className="dashboard-tooltip-surface rounded-xl border border-slate-500/45 bg-slate-950/[0.98] px-3.5 py-3 text-xs leading-relaxed text-slate-200 shadow-[0_18px_55px_rgba(0,0,0,0.5)]">
      <p className="mb-2 font-bold text-white">
        {source.modelVersion ? `${source.runLabel} · ${source.modelVersion}` : source.runLabel ?? label}
      </p>
      {payload.map((item) => (
        <p key={item.dataKey} style={{ color: item.color }}>
          {item.name}: {item.dataKey === 'pending' ? `${fmt(item.value)} segmentos` : pct(item.value)}
        </p>
      ))}
    </div>
  );
};

const Card = ({ children, className = '' }) => (
  <div className={`dashboard-surface border ${className}`}>
    {children}
  </div>
);

const StatCard = ({ title, value, detail, trend, icon: Icon, color = 'blue', danger = false }) => (
  <Card className="flex min-h-[156px] flex-col justify-between p-4">
    <div className="flex items-start justify-between gap-3">
      <div>
        <h3 className="text-sm font-medium text-[var(--dash-muted)]">{title}</h3>
        <p className="mt-3 text-3xl font-semibold tracking-tight text-[var(--dash-text)] xl:text-[1.85rem]">{value}</p>
      </div>
      <div className={`grid h-10 w-10 shrink-0 place-items-center rounded-xl border border-current/25 ${colorClasses[color] ?? colorClasses.blue}`}>
        <Icon size={18} />
      </div>
    </div>
    <div>
      {detail && <p className="text-xs text-[var(--dash-soft)]">{detail}</p>}
      {trend !== undefined && trend !== null && (
        <span className={`mt-2 inline-flex items-center gap-1 rounded-full border px-2 py-1 text-xs font-bold ${danger ? 'border-red-400/25 bg-red-400/10 text-red-300' : 'border-emerald-400/25 bg-emerald-400/10 text-emerald-300'}`}>
          {danger ? <ArrowDownRight size={14} /> : <ArrowUpRight size={14} />}
          {trend}
        </span>
      )}
    </div>
  </Card>
);

const SplitStatCard = ({ left, right }) => (
  <Card className="grid min-h-[156px] grid-cols-[1fr_auto_1fr] items-center gap-4 p-4">
    <MiniStat {...left} />
    <div className="h-[90%] min-h-20 w-px self-center bg-[var(--dash-border)]" />
    <MiniStat {...right} />
  </Card>
);

const MiniStat = ({ title, value, detail, icon: Icon, color = 'blue' }) => (
  <div className="min-w-0">
    <div className={`mb-3 inline-flex rounded-lg p-2 ${colorClasses[color] ?? colorClasses.blue}`}>
      <Icon size={17} />
    </div>
    <h3 className="truncate text-sm font-medium text-[var(--dash-muted)]">{title}</h3>
    <p className="mt-1 truncate text-2xl font-bold text-[var(--dash-text)] xl:text-[1.55rem]">{value}</p>
    {detail && <p className="mt-1 truncate text-xs text-[var(--dash-soft)]">{detail}</p>}
  </div>
);

const ModelSnapshotColumn = ({ model }) => (
  <div className="rounded-lg bg-[var(--dash-subtle)] p-4">
    <div className="mb-4">
      <p className="text-xs font-semibold uppercase tracking-wide text-slate-400">{model.label}</p>
      <h4 className="mt-1 truncate text-lg font-black text-[var(--dash-text)]">Run {model.runId}</h4>
      <p className="truncate text-xs text-[var(--dash-muted)]">{model.version}</p>
    </div>
    <div className="grid grid-cols-2 gap-3">
      <MetricTile title="Accuracy" value={pctMetric(model.accuracy)} />
      <MetricTile title="Macro F1" value={pctMetric(model.macroF1)} />
      <MetricTile title="Safe Precision" value={pctMetric(model.safePrecision)} tone="emerald" />
      <MetricTile title="Holdout Coverage" value={pctMetric(model.holdoutCoverage)} tone="blue" />
      <MetricTile title="Safe Recall" value={pctMetric(model.safeRecall)} tone="amber" />
      <MetricTile title="Negative Coverage" value={pctMetric(model.negativeCoverage)} tone="red" />
    </div>
  </div>
);

const MetricTile = ({ title, value, tone = 'slate', className = '' }) => (
  <div className={`rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-2.5 ${className}`}>
    <p className="text-[0.68rem] font-semibold uppercase tracking-wide text-[var(--dash-muted)]">{title}</p>
    <p className={`mt-0.5 text-base font-black ${tone === 'emerald' ? 'text-emerald-400' : tone === 'red' ? 'text-red-400' : tone === 'blue' ? 'text-blue-400' : tone === 'amber' ? 'text-amber-400' : tone === 'violet' ? 'text-violet-400' : 'text-[var(--dash-text)]'}`}>{value}</p>
  </div>
);

const actionLabels = {
  auto_safe: 'Auto-safe',
  needs_human: 'Human',
  needs_autofix: 'Autofix',
  blocked_structure: 'Blocked',
};

const parseReasons = (value) => {
  try {
    const parsed = JSON.parse(value || '[]');
    return Array.isArray(parsed) ? parsed.slice(0, 3) : [];
  } catch {
    return [];
  }
};

const ChartCard = ({ title, subtitle, children, className = '' }) => (
  <Card className={`flex flex-col p-5 ${className}`}>
    <div className="mb-3">
      <h3 className="text-sm font-semibold text-[var(--dash-text)]">{title}</h3>
      {subtitle && <p className="mt-1 text-xs text-[var(--dash-muted)]">{subtitle}</p>}
    </div>
    <div className="min-h-0 flex-1">{children}</div>
  </Card>
);

const Badge = ({ children, tone = 'emerald' }) => (
  <span className={`dashboard-badge px-3 py-1 text-xs font-bold ${colorClasses[tone] ?? colorClasses.emerald}`}>{children}</span>
);

const ViewToggle = ({ options, value, onChange }) => (
  <div className="dashboard-segmented">
    {options.map((item) => (
      <button
        key={item}
        onClick={() => onChange(item)}
        className={cn('dashboard-segmented-button px-3 text-sm font-medium', value === item && 'is-active')}
      >
        {item}
      </button>
    ))}
  </div>
);

const ViewHeader = ({ title, subtitle, children }) => (
  <div className="flex items-center justify-between gap-4">
    <div>
      <h3 className="text-sm font-bold text-[var(--dash-text)]">{title}</h3>
      <p className="text-xs text-[var(--dash-muted)]">{subtitle}</p>
    </div>
    {children}
  </div>
);

const ChartBox = ({ children, className = 'h-[375px]' }) => <div className={`${className} min-h-0`}>{children}</div>;

function Cockpit({ data }) {
  const [axisMode, setAxisMode] = useState('Run');
  const { kpis, qualityTrend, qualityTrendByModel, segmentDistribution, status } = data.cockpit;
  const chartTrend = axisMode === 'Modelo' ? qualityTrendByModel : qualityTrend;
  return (
    <div className="flex h-full min-h-0 flex-col gap-4 pb-3">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        <StatCard title="Total de Segmentos" value={compact(kpis.activeSegments)} detail="source_segments ativos" icon={LayoutDashboard} />
        <StatCard title="Cobertura com Output" value={pct(kpis.outputCoverage)} detail="segmentos com output_text" icon={CheckCircle2} color="emerald" />
        <StatCard title="Eficiência Auto-Safe" value={pct(kpis.autoSafeEfficiency)} trend={`${kpis.autoSafeDelta >= 0 ? '+' : ''}${pct(kpis.autoSafeDelta)}`} detail="final_auto_safe / scored" icon={ShieldCheck} color="emerald" />
        <StatCard title="Revisão Pendente" value={compact(kpis.pendingReview)} detail="no score operacional: humano + autofix + bloqueios" icon={AlertCircle} color="amber" danger />
      </div>

      <ViewHeader title="Visão da Confiança" subtitle="Alterna o eixo do gráfico entre execuções de score e última execução por modelo.">
        <ViewToggle options={['Run', 'Modelo']} value={axisMode} onChange={setAxisMode} />
      </ViewHeader>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <ChartCard title="Evolução da Confiança Geral" subtitle="Qualidade ML = Macro F1 do modelo usado no score; pendências são segmentos, não percentual" className="xl:col-span-2">
          <ChartBox>
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={chartTrend} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="currentColor" opacity={0.12} />
                <XAxis dataKey="runLabel" axisLine={false} tickLine={false} tick={chartText} />
                <YAxis yAxisId="left" domain={[0, 100]} tickFormatter={(v) => `${v}%`} axisLine={false} tickLine={false} tick={chartText} />
                <YAxis yAxisId="right" orientation="right" tickFormatter={(v) => compact(v)} axisLine={false} tickLine={false} tick={chartText} />
                <Tooltip content={<CockpitTooltip />} />
                <Legend />
                <Bar yAxisId="right" dataKey="pending" name="Pendências" fill="#f59e0b" radius={[8, 8, 0, 0]} opacity={0.75} />
                <Area yAxisId="left" type="monotone" dataKey="qualityIndex" name="Qualidade ML" stroke="#2563eb" fill="#2563eb" fillOpacity={0.08} strokeWidth={3} dot={{ r: 4, strokeWidth: 2 }} activeDot={{ r: 6 }} />
                <Line yAxisId="left" type="monotone" dataKey="autoSafe" name="Auto-Safe" stroke="#10b981" strokeWidth={3} dot={{ r: 4, strokeWidth: 2 }} activeDot={{ r: 6 }} />
              </ComposedChart>
            </ResponsiveContainer>
          </ChartBox>
        </ChartCard>

        <ChartCard title="Distribuição Atual" subtitle="Status dos segmentos no score operacional">
          <ChartBox>
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie data={segmentDistribution} dataKey="value" nameKey="name" innerRadius={64} outerRadius={104} paddingAngle={3}>
                  {segmentDistribution.map((entry) => <Cell key={entry.name} fill={entry.color} />)}
                </Pie>
                <Tooltip formatter={(value) => fmt(value)} />
                <Legend />
              </PieChart>
            </ResponsiveContainer>
          </ChartBox>
        </ChartCard>
      </div>

      <Card className="p-4">
        <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
          <div>
            <h3 className="font-bold text-[var(--dash-text)]">Status operacional</h3>
            <p className="text-sm text-[var(--dash-muted)]">Modelo ativo: {status.activeModel ?? 'n/a'} · Score #{status.latestScoreRunId ?? 'n/a'} · Dataset #{status.latestDatasetRunId ?? 'n/a'}</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Badge>Modelo ativo</Badge>
            <Badge tone="blue">SQLite conectado</Badge>
            <Badge tone="amber">{compact(kpis.pendingReview)} pendências</Badge>
          </div>
        </div>
      </Card>
    </div>
  );
}

function MLPerformance({ data }) {
  const [viewMode, setViewMode] = useState('Modelo');
  const [axisMode, setAxisMode] = useState('Run');
  const { kpis, mlTrend, datasetComposition, modelComparison, candidateDecision } = data.mlPerformance;
  const chartTrend = axisMode === 'Modelo' ? data.mlPerformance.mlTrendByModel : mlTrend;
  const isModelView = viewMode === 'Modelo';

  return (
    <div className="flex h-full min-h-0 flex-col gap-4 pb-3">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-6">
        <StatCard title="Modelo Ativo" value={kpis.activeModelShort} detail={kpis.activeModel} icon={Rocket} color="blue" />
        <StatCard title="Macro F1" value={metric(kpis.macroF1)} detail="equilíbrio entre classes" icon={BarChart3} color="emerald" />
        <StatCard title="Safe Precision" value={pct(kpis.safePrecision * 100)} detail="métrica pós-trava de segurança" icon={ShieldCheck} color="emerald" />
        <StatCard title="Holdout Coverage" value={pct(kpis.holdoutCoverage * 100)} detail="safe recall pós-trava" icon={ShieldAlert} color="emerald" />
        <StatCard title="Negative Coverage" value={pct(kpis.negativeCoverage)} detail="negativos no dataset" icon={Database} color="violet" />
      </div>

      <ViewHeader title={isModelView ? 'Visão do Modelo' : 'Visão do Dataset'} subtitle={isModelView ? 'Qualidade, risco e decisão por versão.' : 'Composição do dataset e comparação entre modelo atual e candidato.'}>
        <div className="flex flex-wrap items-center justify-end gap-3">
          {isModelView && <ViewToggle options={['Run', 'Modelo']} value={axisMode} onChange={setAxisMode} />}
          <ViewToggle options={['Modelo', 'Dataset']} value={viewMode} onChange={setViewMode} />
        </div>
      </ViewHeader>

      {isModelView ? (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <ChartCard title="Evolução por Modelo" subtitle="Macro F1, Safe Precision e Safe Recall">
            <ChartBox className="h-[445px]">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={chartTrend} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="currentColor" opacity={0.12} />
                  <XAxis dataKey="model" axisLine={false} tickLine={false} tick={chartText} />
                  <YAxis domain={[0.2, 1]} tickFormatter={(v) => `${Math.round(v * 100)}%`} axisLine={false} tickLine={false} tick={chartText} />
                  <Tooltip content={<ModelTooltip />} />
                  <Legend />
                  <Line type="monotone" dataKey="macroF1" name="Macro F1" stroke="#2563eb" strokeWidth={3} />
                  <Line type="monotone" dataKey="safePrecision" name="Safe Precision" stroke="#10b981" strokeWidth={3} />
                  <Line type="monotone" dataKey="safeRecall" name="Safe Recall" stroke="#f59e0b" strokeWidth={3} />
                </LineChart>
              </ResponsiveContainer>
            </ChartBox>
          </ChartCard>

          <ChartCard title="False Safe por Modelo" subtitle="O modelo só passa se não errar com confiança">
            <ChartBox className="h-[445px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={chartTrend} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="currentColor" opacity={0.12} />
                  <XAxis dataKey="model" axisLine={false} tickLine={false} tick={chartText} />
                  <YAxis axisLine={false} tickLine={false} tick={chartText} />
                  <Tooltip content={<ModelTooltip />} />
                  <Bar dataKey="falseSafe" name="False Safe" fill="#ef4444" radius={[8, 8, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </ChartBox>
          </ChartCard>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <ChartCard title="Composição do Dataset" subtitle="Classes usadas no treino">
            <ChartBox className="h-[445px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={datasetComposition} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="currentColor" opacity={0.12} />
                  <XAxis dataKey="label" axisLine={false} tickLine={false} tick={chartText} />
                  <YAxis axisLine={false} tickLine={false} tick={chartText} />
                  <Tooltip formatter={(value) => fmt(value)} />
                  <Bar dataKey="value" name="Registros" fill="#8b5cf6" radius={[8, 8, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </ChartBox>
          </ChartCard>

          <Card className="p-6">
            <h3 className="text-lg font-bold text-[var(--dash-text)]">Atual vs Candidato</h3>
            <p className="mt-1 text-sm text-[var(--dash-muted)]">Decisão de promoção do próximo modelo</p>
            <div className="mt-6 grid gap-4 md:grid-cols-2">
              {modelComparison.map((item) => (
                <div key={item.metric} className="rounded-lg bg-[var(--dash-subtle)] p-4">
                  <span className="text-sm text-[var(--dash-muted)]">{item.metric}</span>
                  <p className="mt-1 text-2xl font-black text-[var(--dash-text)]">{metric(item.current)} → {metric(item.candidate)}</p>
                </div>
              ))}
            </div>
            <div className="mt-6 rounded-lg border border-blue-500/30 bg-[var(--dash-subtle)] p-4 text-sm font-semibold text-[var(--dash-text)]">
              Decisão atual: <strong>{candidateDecision === 'promote' ? 'modelo ativo/promovido' : 'em revisão'}</strong>.
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}

function Pipeline({ data }) {
  const [viewMode, setViewMode] = useState('Produção');
  const { kpis, pipelineStatus, funnelData, packageBacklog, humanReviews } = data.pipeline;
  const isProductionView = viewMode === 'Produção';

  return (
    <div className="flex h-full min-h-0 flex-col gap-5">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        <SplitStatCard
          left={{ title: 'Segmentos Totais', value: compact(kpis.segmentsTotal), icon: PackageSearch }}
          right={{ title: 'Com Output', value: compact(kpis.withOutput), icon: CheckCircle2, color: 'emerald' }}
        />
        <SplitStatCard
          left={{ title: 'Sem Output', value: fmt(kpis.withoutOutput), icon: FileWarning, color: 'amber' }}
          right={{ title: 'Locked Humanos', value: compact(kpis.lockedHuman), icon: Lock, color: 'violet' }}
        />
        <SplitStatCard
          left={{ title: 'Confirmados', value: compact(kpis.confirmed), icon: ShieldCheck, color: 'emerald' }}
          right={{ title: 'Pendentes Revisão', value: compact(kpis.pendingReview), icon: AlertCircle, color: 'amber' }}
        />
        <SplitStatCard
          left={{ title: 'Issues Estruturais', value: fmt(kpis.structuralIssues), icon: ShieldAlert, color: 'red' }}
          right={{ title: 'Autofix', value: compact(kpis.autofix), icon: Workflow, color: 'blue' }}
        />
      </div>

      <ViewHeader title={isProductionView ? 'Visão de Produção' : 'Visão de Gargalos'} subtitle={isProductionView ? 'Status geral dos segmentos e fluxo da esteira.' : 'Pacotes que seguram o avanço e ritmo das revisões humanas.'}>
        <ViewToggle options={['Produção', 'Gargalos']} value={viewMode} onChange={setViewMode} />
      </ViewHeader>

      {isProductionView ? (
        <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
          <ChartCard title="Status dos Segmentos" subtitle="A esteira de produção em blocos">
            <ChartBox className="h-[445px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={pipelineStatus} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="currentColor" opacity={0.12} />
                  <XAxis dataKey="status" axisLine={false} tickLine={false} tick={chartText} interval={0} angle={-12} textAnchor="end" height={70} />
                  <YAxis tickFormatter={(v) => compact(v)} axisLine={false} tickLine={false} tick={chartText} />
                  <Tooltip formatter={(value) => fmt(value)} />
                  <Bar dataKey="count" name="Segmentos" fill="#2563eb" radius={[8, 8, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </ChartBox>
          </ChartCard>

          <ChartCard title="Fluxo do Segmento" subtitle="Do source indexado até locked">
            <ChartBox className="h-[445px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={funnelData} layout="vertical" margin={{ top: 8, right: 24, left: 12, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke="currentColor" opacity={0.12} />
                  <XAxis type="number" tickFormatter={(v) => compact(v)} axisLine={false} tickLine={false} tick={chartText} />
                  <YAxis type="category" dataKey="step" width={90} axisLine={false} tickLine={false} tick={chartText} />
                  <Tooltip formatter={(value) => fmt(value)} />
                  <Bar dataKey="value" name="Segmentos" fill="#10b981" radius={[0, 8, 8, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </ChartBox>
          </ChartCard>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
          <ChartCard title="Pacotes com Mais Pendência" subtitle="Top arquivos segurando o avanço">
            <ChartBox className="h-[445px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={packageBacklog} layout="vertical" margin={{ top: 8, right: 24, left: 80, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke="currentColor" opacity={0.12} />
                  <XAxis type="number" axisLine={false} tickLine={false} tick={chartText} />
                  <YAxis type="category" dataKey="file" width={150} axisLine={false} tickLine={false} tick={chartText} />
                  <Tooltip formatter={(value) => fmt(value)} />
                  <Bar dataKey="pending" name="Pendências" fill="#f59e0b" radius={[0, 8, 8, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </ChartBox>
          </ChartCard>

          <ChartCard title="Revisões Humanas por Dia" subtitle="Correções e validações recentes">
            <ChartBox className="h-[445px]">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={humanReviews} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="currentColor" opacity={0.12} />
                  <XAxis dataKey="day" axisLine={false} tickLine={false} tick={chartText} />
                  <YAxis axisLine={false} tickLine={false} tick={chartText} />
                  <Tooltip formatter={(value) => fmt(value)} />
                  <Legend />
                  <Area type="monotone" dataKey="correct" name="Correct" stackId="1" stroke="#10b981" fill="#10b981" fillOpacity={0.45} />
                  <Area type="monotone" dataKey="minorFix" name="Minor fix" stackId="1" stroke="#3b82f6" fill="#3b82f6" fillOpacity={0.45} />
                  <Area type="monotone" dataKey="semanticError" name="Semantic error" stackId="1" stroke="#f59e0b" fill="#f59e0b" fillOpacity={0.45} />
                  <Area type="monotone" dataKey="residualSpanish" name="Residual Spanish" stackId="1" stroke="#ef4444" fill="#ef4444" fillOpacity={0.45} />
                </AreaChart>
              </ResponsiveContainer>
            </ChartBox>
          </ChartCard>
        </div>
      )}
    </div>
  );
}

function Lifecycle({ data }) {
  const [viewMode, setViewMode] = useState('Overview');
  const lifecycle = data.lifecycle ?? {};
  const summary = lifecycle.summary ?? {};
  const taxonomy = lifecycle.taxonomy ?? {};
  const lifecycleSelectCString = taxonomy.select_cstring ?? {};
  const stateDistribution = lifecycle.stateDistribution ?? [];
  const groupDistribution = lifecycle.groupDistribution ?? [];
  const outputApplication = lifecycle.outputApplication ?? [];
  const packageBacklog = lifecycle.packageBacklog ?? [];
  const applyQueue = lifecycle.applyQueue ?? [];
  const reopenQueue = lifecycle.reopenQueue ?? [];
  const outputApply = lifecycle.outputApply ?? {};
  const outputApplySummary = outputApply.summary ?? {};
  const outputApplyRuns = outputApply.runs ?? [];
  const outputApplyEvolution = outputApply.evolution ?? [];
  const outputApplyPackages = outputApply.packageItems ?? [];
  const outputApplyTokenBlocks = outputApply.tokenBlocks ?? [];
  const tokenPolicy = lifecycle.tokenPolicy ?? {};
  const tokenPolicySummary = tokenPolicy.summary ?? {};
  const tokenPolicyRuns = tokenPolicy.runs ?? [];
  const tokenPolicyBuckets = tokenPolicy.bucketDistribution ?? [];
  const tokenPolicyPackages = tokenPolicy.packageBuckets ?? [];
  const tokenPolicyQueue = tokenPolicy.reviewQueue ?? [];
  const tokenPolicyDecisions = tokenPolicy.decisions ?? {};
  const tokenDecisionSummary = tokenPolicyDecisions.summary ?? {};
  const tokenDecisionBuckets = tokenPolicyDecisions.byBucket ?? [];
  const tokenDecisionCoverage = tokenPolicyDecisions.coverage ?? [];
  const tokenDecisionRuns = tokenPolicyDecisions.runs ?? [];

  const lifecycleTitle = {
    Overview: 'Lifecycle Overview',
    Output: 'Output & Aplicacao',
    Apply: 'Aplicacao Real',
    'Token Policy': 'Token Policy',
    Packages: 'Pacotes com Pendencia',
    Queues: 'Filas Operacionais',
  }[viewMode];
  const lifecycleSubtitle = {
    Overview: 'Estado final materializado dos segmentos ativos.',
    Output: 'Blanks validos, mismatch de confirmacao e output faltante.',
    Apply: 'Historico do segment-apply e arquivos realmente reescritos.',
    'Token Policy': 'Gate estrutural para token mismatch antes de aplicar output.',
    Packages: 'Pacotes que concentram pendencias, aplicacao e reabertura.',
    Queues: 'Candidatos para aplicar confirmacao ou reabrir revisao.',
  }[viewMode];

  const stateColor = (state) => {
    if (state?.includes('reopen')) return 'text-red-400';
    if (state?.includes('blank')) return 'text-blue-400';
    if (state?.includes('apply')) return 'text-amber-400';
    if (state?.includes('closed')) return 'text-emerald-400';
    return 'text-[var(--dash-text)]';
  };
  const riskTone = (risk) => {
    if (risk === 'critical') return 'red';
    if (risk === 'high') return 'amber';
    if (risk === 'medium') return 'blue';
    return 'emerald';
  };
  const riskColor = (risk) => {
    if (risk === 'critical') return '#ef4444';
    if (risk === 'high') return '#f59e0b';
    if (risk === 'medium') return '#3b82f6';
    return '#10b981';
  };

  return (
    <div className="flex h-full min-h-0 flex-col gap-4 pb-3">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5">
        <StatCard title="Segmentos Consolidados" value={compact(summary.closed_count)} detail={pct(summary.closed_pct)} icon={ShieldCheck} color="emerald" />
        <StatCard title="Pendencia Acionavel" value={compact(taxonomy.actionable_pending)} detail={`bruto ${compact(taxonomy.raw_pending ?? summary.pending_count)}`} icon={AlertCircle} color={taxonomy.actionable_pending ? 'amber' : 'emerald'} />
        <StatCard title="Suspeita ML / Watch" value={compact(taxonomy.model_suspicion_watch)} detail="conservador, nao manual" icon={BrainCircuit} color="violet" />
        <StatCard title="Ponte Select_CString" value={`${fmt(lifecycleSelectCString.closed ?? 0)}/${fmt(lifecycleSelectCString.total ?? 0)}`} detail={`${fmt(lifecycleSelectCString.pending ?? 0)} pendentes`} icon={SearchCheck} color={(lifecycleSelectCString.pending ?? 0) ? 'amber' : 'emerald'} />
        <StatCard title="Aplicar Output Confirmado" value={fmt(summary.output_apply_pending_count)} detail="confirmation mismatch" icon={Workflow} color="blue" />
        <StatCard title="Blanks Válidos" value={fmt(summary.blank_valid_count)} detail="valid/intencional" icon={CheckCircle2} color="blue" />
        <StatCard title="Reabrir por ML Atual" value={fmt(summary.reopen_count)} detail={`run ${summary.run_id ?? '-'}`} icon={ShieldAlert} color={summary.reopen_count ? 'red' : 'emerald'} />
      </div>

      <ViewHeader
        title={lifecycleTitle}
        subtitle={lifecycleSubtitle}
      >
        <ViewToggle options={['Overview', 'Output', 'Apply', 'Token Policy', 'Packages', 'Queues']} value={viewMode} onChange={setViewMode} />
      </ViewHeader>

      {viewMode === 'Overview' ? (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
          <ChartCard title="Closed vs Pending" subtitle="Consolidado contra pendência operacional" className="xl:col-span-4">
            <ChartBox className="h-[445px]">
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Tooltip formatter={(value) => fmt(value)} />
                  <Legend />
                  <Pie data={groupDistribution} dataKey="value" nameKey="name" innerRadius={72} outerRadius={118} paddingAngle={3}>
                    {groupDistribution.map((entry) => (
                      <Cell key={entry.name} fill={entry.color ?? (entry.group === 'closed' ? '#10b981' : '#f59e0b')} />
                    ))}
                  </Pie>
                </PieChart>
              </ResponsiveContainer>
            </ChartBox>
          </ChartCard>

          <ChartCard title="Distribuição por Estado" subtitle="Final state granular do snapshot" className="xl:col-span-8">
            <ChartBox className="h-[445px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={stateDistribution} layout="vertical" margin={{ top: 8, right: 18, left: 120, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke="currentColor" opacity={0.12} />
                  <XAxis type="number" tickFormatter={(v) => compact(v)} axisLine={false} tickLine={false} tick={chartText} />
                  <YAxis type="category" dataKey="final_state" width={230} axisLine={false} tickLine={false} tick={chartText} />
                  <Tooltip formatter={(value) => fmt(value)} />
                  <Bar dataKey="total" name="Segmentos" fill="#3b82f6" radius={[0, 8, 8, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </ChartBox>
          </ChartCard>

          <Card className="p-5 xl:col-span-12">
            <h3 className="text-sm font-semibold text-[var(--dash-text)]">Tabela de Estados</h3>
            <p className="mt-1 text-xs text-[var(--dash-muted)]">`blank_valid` e `blank_intentional` são estados corretos, não erros.</p>
            <div className="mt-4 max-h-[260px] overflow-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase text-[var(--dash-muted)]">
                  <tr><th className="py-2">Grupo</th><th className="py-2">Estado final</th><th className="py-2 text-right">Total</th><th className="py-2 text-right">%</th></tr>
                </thead>
                <tbody>
                  {stateDistribution.map((row) => (
                    <tr key={`${row.state_group}-${row.final_state}`} className="border-t border-[var(--dash-border)]">
                      <td className="py-2"><Badge tone={row.state_group === 'closed' ? 'emerald' : 'amber'}>{row.state_group}</Badge></td>
                      <td className={`py-2 font-semibold ${stateColor(row.final_state)}`}>{row.final_state}</td>
                      <td className="py-2 text-right">{fmt(row.total)}</td>
                      <td className="py-2 text-right">{pct(row.pct_total)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </div>
      ) : viewMode === 'Output' ? (
        <Card className="p-5">
          <h3 className="text-sm font-semibold text-[var(--dash-text)]">Output, Aplicação e Revisão</h3>
          <p className="mt-1 text-xs text-[var(--dash-muted)]">Estados combinados para diferenciar blanks válidos de erro real.</p>
          <div className="mt-4 h-[560px] overflow-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs uppercase text-[var(--dash-muted)]">
                <tr><th className="py-2">Output</th><th className="py-2">Aplicação</th><th className="py-2">Revisão</th><th className="py-2 text-right">Total</th></tr>
              </thead>
              <tbody>
                {outputApplication.map((row) => (
                  <tr key={`${row.output_state}-${row.apply_state}-${row.review_state}`} className="border-t border-[var(--dash-border)]">
                    <td className={`py-2 font-semibold ${stateColor(row.output_state)}`}>{row.output_state}</td>
                    <td className="py-2">{row.apply_state}</td>
                    <td className="py-2">{row.review_state}</td>
                    <td className="py-2 text-right">{fmt(row.total)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ) : viewMode === 'Apply' ? (
        <div className="flex flex-col gap-4">
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5">
            <StatCard title="Outputs Reescritos" value={fmt(outputApplySummary.total_applied)} detail="apply real" icon={CheckCircle2} color="emerald" />
            <StatCard title="Ultimo Lote Aplicado" value={fmt(outputApplySummary.latest_applied_count)} detail={`run ${outputApplySummary.latest_apply_run_id ?? '-'}`} icon={Workflow} color="blue" />
            <StatCard title="Token Bloqueado" value={fmt(outputApplySummary.token_mismatch_count)} detail="trava estrutural" icon={FileWarning} color={outputApplySummary.token_mismatch_count ? 'amber' : 'emerald'} />
            <StatCard title="Arquivos Tocados" value={fmt(outputApplySummary.files_touched_count)} detail="com backup" icon={PackageSearch} color="violet" />
            <StatCard title="Dry-runs" value={fmt(outputApplySummary.dry_run_count)} detail="sem escrita" icon={SearchCheck} color="blue" />
          </div>

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
            <ChartCard title="Aplicacao vs Pendencia" subtitle="Pendencia do snapshot, aplicado real e bloqueios por token" className="xl:col-span-7">
              <ChartBox className="h-[380px]">
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart data={outputApplyEvolution} margin={{ top: 8, right: 18, left: 0, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="currentColor" opacity={0.12} />
                    <XAxis dataKey="state_run_id" axisLine={false} tickLine={false} tick={chartText} />
                    <YAxis yAxisId="left" tickFormatter={(v) => compact(v)} axisLine={false} tickLine={false} tick={chartText} />
                    <YAxis yAxisId="right" orientation="right" tickFormatter={(v) => fmt(v)} axisLine={false} tickLine={false} tick={chartText} />
                    <Tooltip formatter={(value) => fmt(value)} labelFormatter={(label) => `State run ${label}`} />
                    <Legend />
                    <Bar yAxisId="right" dataKey="applied_from_this_state" name="Aplicado" fill="#10b981" radius={[8, 8, 0, 0]} />
                    <Bar yAxisId="right" dataKey="token_mismatch_from_this_state" name="Token bloqueado" fill="#f59e0b" radius={[8, 8, 0, 0]} />
                    <Line yAxisId="left" type="monotone" dataKey="output_apply_pending_count" name="Pendente aplicar" stroke="#3b82f6" strokeWidth={3} dot={{ r: 4 }} />
                  </ComposedChart>
                </ResponsiveContainer>
              </ChartBox>
            </ChartCard>

            <Card className="p-5 xl:col-span-5">
              <h3 className="text-sm font-semibold text-[var(--dash-text)]">Runs de Aplicacao</h3>
              <p className="mt-1 text-xs text-[var(--dash-muted)]">`applied_backfill` conta como aplicado real.</p>
              <div className="mt-4 h-[380px] overflow-auto">
                <table className="w-full text-left text-sm">
                  <thead className="text-xs uppercase text-[var(--dash-muted)]">
                    <tr><th className="py-2">Run</th><th className="py-2">Modo</th><th className="py-2 text-right">Inspec.</th><th className="py-2 text-right">Pronto</th><th className="py-2 text-right">Aplicado</th><th className="py-2 text-right">Token</th></tr>
                  </thead>
                  <tbody>
                    {outputApplyRuns.map((row) => (
                      <tr key={row.apply_run_id} className="border-t border-[var(--dash-border)]">
                        <td className="py-2 font-semibold text-[var(--dash-text)]">{row.apply_run_id}</td>
                        <td className="py-2"><Badge tone={row.apply ? 'emerald' : 'blue'}>{row.apply ? 'Aplicado' : 'Dry-run'}</Badge></td>
                        <td className="py-2 text-right">{fmt(row.candidates_inspected)}</td>
                        <td className="py-2 text-right text-blue-400">{fmt(row.ready_count)}</td>
                        <td className="py-2 text-right text-emerald-400">{fmt(row.applied_count)}</td>
                        <td className="py-2 text-right text-amber-400">{fmt(row.token_mismatch_count)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>

            <Card className="p-5 xl:col-span-6">
              <h3 className="text-sm font-semibold text-[var(--dash-text)]">Itens Aplicados por Pacote</h3>
              <p className="mt-1 text-xs text-[var(--dash-muted)]">Agrupado por run, pacote e origem da revisao.</p>
              <div className="mt-4 h-[320px] overflow-auto">
                <table className="w-full text-left text-sm">
                  <thead className="text-xs uppercase text-[var(--dash-muted)]">
                    <tr><th className="py-2">Run</th><th className="py-2">Pacote</th><th className="py-2">Revisao</th><th className="py-2 text-right">Inspec.</th><th className="py-2 text-right">Aplicado</th><th className="py-2 text-right">Token</th></tr>
                  </thead>
                  <tbody>
                    {outputApplyPackages.map((row) => (
                      <tr key={`${row.apply_run_id}-${row.package_name}-${row.review_state}`} className="border-t border-[var(--dash-border)]">
                        <td className="py-2">{row.apply_run_id}</td>
                        <td className="max-w-[200px] truncate py-2 font-semibold text-[var(--dash-text)]" title={row.package_name}>{row.package_name}</td>
                        <td className="py-2">{row.review_state}</td>
                        <td className="py-2 text-right">{fmt(row.inspected_count)}</td>
                        <td className="py-2 text-right text-emerald-400">{fmt(row.applied_count)}</td>
                        <td className="py-2 text-right text-amber-400">{fmt(row.token_mismatch_count)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>

            <Card className="p-5 xl:col-span-6">
              <h3 className="text-sm font-semibold text-[var(--dash-text)]">Fila de Bloqueios por Token</h3>
              <p className="mt-1 text-xs text-[var(--dash-muted)]">Nada aqui deve ser aplicado automaticamente.</p>
              <div className="mt-4 h-[320px] overflow-auto">
                <table className="w-full text-left text-sm">
                  <thead className="text-xs uppercase text-[var(--dash-muted)]">
                    <tr><th className="py-2">Run</th><th className="py-2">Segmento</th><th className="py-2">Arquivo</th><th className="py-2">Chave</th><th className="py-2">Status</th></tr>
                  </thead>
                  <tbody>
                    {outputApplyTokenBlocks.map((row) => (
                      <tr key={`${row.apply_run_id}-${row.segment_id}`} className="border-t border-[var(--dash-border)]">
                        <td className="py-2">{row.apply_run_id}</td>
                        <td className="py-2 text-[var(--dash-muted)]">{row.segment_id}</td>
                        <td className="max-w-[180px] truncate py-2" title={row.relative_path}>{row.relative_path}</td>
                        <td className="max-w-[220px] truncate py-2 font-semibold text-[var(--dash-text)]" title={row.source_key}>{row.source_key}</td>
                        <td className="py-2 text-amber-400">{row.result_status}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          </div>
        </div>
      ) : viewMode === 'Token Policy' ? (
        <div className="flex flex-col gap-4">
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-6">
            <StatCard title="Gate de Tokens" value={fmt(tokenPolicySummary.total_candidates)} detail={`run ${tokenPolicySummary.token_policy_run_id ?? '-'}`} icon={ShieldAlert} color="violet" />
            <StatCard title="Critico" value={fmt(tokenPolicySummary.critical_count)} detail="nao aplicar" icon={XCircle} color={tokenPolicySummary.critical_count ? 'red' : 'emerald'} />
            <StatCard title="Alto Risco" value={fmt(tokenPolicySummary.high_count)} detail="revisao forte" icon={FileWarning} color={tokenPolicySummary.high_count ? 'amber' : 'emerald'} />
            <StatCard title="Revisao Humana" value={fmt(tokenPolicySummary.manual_review_count)} detail="amostra necessaria" icon={SearchCheck} color="blue" />
            <StatCard title="Bloqueados" value={fmt(tokenPolicySummary.blocked_count)} detail="blocked_*" icon={Lock} color={tokenPolicySummary.blocked_count ? 'red' : 'emerald'} />
            <StatCard title="Candidatos Politica" value={fmt(tokenPolicySummary.policy_candidate_count)} detail="policy_candidate_*" icon={Scale} color="emerald" />
          </div>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5">
            <StatCard title="Decisoes do Gate" value={fmt(tokenDecisionSummary.total_decisions)} detail={`policy run ${tokenDecisionSummary.policy_run_id ?? '-'}`} icon={CheckCircle2} color="blue" />
            <StatCard title="Aprovadas Aplicar" value={fmt(tokenDecisionSummary.approved_for_apply)} detail="apos revisao" icon={ShieldCheck} color="emerald" />
            <StatCard title="Precisam Correcao" value={fmt(tokenDecisionSummary.needs_fix)} detail="texto/token" icon={FileWarning} color={tokenDecisionSummary.needs_fix ? 'amber' : 'emerald'} />
            <StatCard title="Precisam Subpolitica" value={fmt(tokenDecisionSummary.needs_subpolicy)} detail="novo escopo" icon={Workflow} color="violet" />
            <StatCard title="Critico Aprovado" value={fmt(tokenDecisionSummary.critical_approved_for_apply)} detail="deve ser zero" icon={ShieldAlert} color={tokenDecisionSummary.critical_approved_for_apply ? 'red' : 'emerald'} />
          </div>

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
            <ChartCard title="Distribuicao por Bucket" subtitle="Token mismatch classificado por risco e estado de revisao" className="xl:col-span-7">
              <ChartBox className="h-[390px]">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={tokenPolicyBuckets} layout="vertical" margin={{ top: 8, right: 18, left: 155, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke="currentColor" opacity={0.12} />
                    <XAxis type="number" tickFormatter={(v) => compact(v)} axisLine={false} tickLine={false} tick={chartText} />
                    <YAxis type="category" dataKey="policy_bucket" width={250} axisLine={false} tickLine={false} tick={chartText} />
                    <Tooltip formatter={(value) => fmt(value)} />
                    <Bar dataKey="total" name="Total" radius={[0, 8, 8, 0]}>
                      {tokenPolicyBuckets.map((row) => <Cell key={`${row.policy_bucket}-${row.risk_level}-${row.review_state}`} fill={riskColor(row.risk_level)} />)}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </ChartBox>
            </ChartCard>

            <Card className="p-5 xl:col-span-5">
              <h3 className="text-sm font-semibold text-[var(--dash-text)]">Ultimas Politicas</h3>
              <p className="mt-1 text-xs text-[var(--dash-muted)]">Historico do segment-token-policy.</p>
              <div className="mt-4 h-[390px] overflow-auto">
                <table className="w-full text-left text-sm">
                  <thead className="text-xs uppercase text-[var(--dash-muted)]">
                    <tr><th className="py-2">Run</th><th className="py-2 text-right">Total</th><th className="py-2 text-right">Critico</th><th className="py-2 text-right">Alto</th><th className="py-2 text-right">Politica</th><th className="py-2 text-right">Bloq.</th></tr>
                  </thead>
                  <tbody>
                    {tokenPolicyRuns.map((row) => (
                      <tr key={row.token_policy_run_id} className="border-t border-[var(--dash-border)]">
                        <td className="py-2 font-semibold text-[var(--dash-text)]">{row.token_policy_run_id}</td>
                        <td className="py-2 text-right">{fmt(row.total_candidates)}</td>
                        <td className="py-2 text-right text-red-400">{fmt(row.critical_count)}</td>
                        <td className="py-2 text-right text-amber-400">{fmt(row.high_count)}</td>
                        <td className="py-2 text-right text-emerald-400">{fmt(row.policy_candidate_count)}</td>
                        <td className="py-2 text-right text-red-400">{fmt(row.blocked_count)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>

            <Card className="p-5 xl:col-span-5">
              <h3 className="text-sm font-semibold text-[var(--dash-text)]">Buckets por Pacote</h3>
              <p className="mt-1 text-xs text-[var(--dash-muted)]">Onde os gates de token estao concentrados.</p>
              <div className="mt-4 h-[340px] overflow-auto">
                <table className="w-full text-left text-sm">
                  <thead className="text-xs uppercase text-[var(--dash-muted)]">
                    <tr><th className="py-2">Pacote</th><th className="py-2">Bucket</th><th className="py-2">Risco</th><th className="py-2 text-right">Total</th></tr>
                  </thead>
                  <tbody>
                    {tokenPolicyPackages.map((row) => (
                      <tr key={`${row.package_name}-${row.policy_bucket}-${row.risk_level}`} className="border-t border-[var(--dash-border)]">
                        <td className="max-w-[150px] truncate py-2 font-semibold text-[var(--dash-text)]" title={row.package_name}>{row.package_name}</td>
                        <td className="max-w-[240px] truncate py-2" title={row.policy_bucket}>{row.policy_bucket}</td>
                        <td className="py-2"><Badge tone={riskTone(row.risk_level)}>{row.risk_level}</Badge></td>
                        <td className="py-2 text-right">{fmt(row.total)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>

            <Card className="p-5 xl:col-span-7">
              <h3 className="text-sm font-semibold text-[var(--dash-text)]">Fila de Revisao Token Gate</h3>
              <p className="mt-1 text-xs text-[var(--dash-muted)]">Candidatos para excecao manual ou politica futura. Nao aplica output.</p>
              <div className="mt-4 h-[340px] overflow-auto">
                <table className="w-full text-left text-sm">
                  <thead className="text-xs uppercase text-[var(--dash-muted)]">
                    <tr><th className="py-2">Segmento</th><th className="py-2">Arquivo</th><th className="py-2">Chave</th><th className="py-2">Bucket</th><th className="py-2">Risco</th><th className="py-2">Recomendacao</th></tr>
                  </thead>
                  <tbody>
                    {tokenPolicyQueue.map((row) => (
                      <tr key={row.policy_item_id} className="border-t border-[var(--dash-border)]">
                        <td className="py-2 text-[var(--dash-muted)]">{row.segment_id}</td>
                        <td className="max-w-[160px] truncate py-2" title={row.relative_path}>{row.relative_path}</td>
                        <td className="max-w-[180px] truncate py-2 font-semibold text-[var(--dash-text)]" title={row.source_key}>{row.source_key}</td>
                        <td className="max-w-[220px] truncate py-2" title={row.policy_bucket}>{row.policy_bucket}</td>
                        <td className="py-2"><Badge tone={riskTone(row.risk_level)}>{row.risk_level}</Badge></td>
                        <td className="max-w-[240px] truncate py-2" title={row.recommendation}>{row.recommendation}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>

            <Card className="p-5 xl:col-span-5">
              <h3 className="text-sm font-semibold text-[var(--dash-text)]">Decisoes por Bucket</h3>
              <p className="mt-1 text-xs text-[var(--dash-muted)]">O que a revisao humana decidiu para a ultima policy revisada.</p>
              <div className="mt-4 h-[320px] overflow-auto">
                <table className="w-full text-left text-sm">
                  <thead className="text-xs uppercase text-[var(--dash-muted)]">
                    <tr><th className="py-2">Bucket</th><th className="py-2">Risco</th><th className="py-2">Decisao</th><th className="py-2 text-right">Total</th><th className="py-2">Apply</th></tr>
                  </thead>
                  <tbody>
                    {tokenDecisionBuckets.map((row) => (
                      <tr key={`${row.policy_bucket}-${row.risk_level}-${row.decision}-${row.approved_for_apply}`} className="border-t border-[var(--dash-border)]">
                        <td className="max-w-[170px] truncate py-2 font-semibold text-[var(--dash-text)]" title={row.policy_bucket}>{row.policy_bucket}</td>
                        <td className="py-2"><Badge tone={riskTone(row.risk_level)}>{row.risk_level}</Badge></td>
                        <td className="max-w-[190px] truncate py-2" title={row.decision}>{row.decision}</td>
                        <td className="py-2 text-right">{fmt(row.total)}</td>
                        <td className="py-2"><Badge tone={row.approved_for_apply ? 'emerald' : 'slate'}>{row.approved_for_apply ? 'sim' : 'nao'}</Badge></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>

            <Card className="p-5 xl:col-span-7">
              <h3 className="text-sm font-semibold text-[var(--dash-text)]">Cobertura da Revisao</h3>
              <p className="mt-1 text-xs text-[var(--dash-muted)]">Quanto de cada bucket ja tem decisao humana registrada.</p>
              <div className="mt-4 h-[320px] overflow-auto">
                <table className="w-full text-left text-sm">
                  <thead className="text-xs uppercase text-[var(--dash-muted)]">
                    <tr><th className="py-2">Bucket</th><th className="py-2">Risco</th><th className="py-2 text-right">Itens</th><th className="py-2 text-right">Revisados</th><th className="py-2 text-right">Aprovados</th><th className="py-2 text-right">Cobertura</th></tr>
                  </thead>
                  <tbody>
                    {tokenDecisionCoverage.map((row) => (
                      <tr key={`${row.policy_bucket}-${row.risk_level}`} className="border-t border-[var(--dash-border)]">
                        <td className="max-w-[260px] truncate py-2 font-semibold text-[var(--dash-text)]" title={row.policy_bucket}>{row.policy_bucket}</td>
                        <td className="py-2"><Badge tone={riskTone(row.risk_level)}>{row.risk_level}</Badge></td>
                        <td className="py-2 text-right">{fmt(row.policy_items)}</td>
                        <td className="py-2 text-right text-blue-400">{fmt(row.reviewed_items)}</td>
                        <td className="py-2 text-right text-emerald-400">{fmt(row.approved_for_apply)}</td>
                        <td className="py-2 text-right">{pct(row.review_coverage_pct)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>

            <Card className="p-5 xl:col-span-12">
              <h3 className="text-sm font-semibold text-[var(--dash-text)]">Runs de Decisao</h3>
              <p className="mt-1 text-xs text-[var(--dash-muted)]">Registro historico das revisoes humanas do token gate.</p>
              <div className="mt-4 max-h-[220px] overflow-auto">
                <table className="w-full text-left text-sm">
                  <thead className="text-xs uppercase text-[var(--dash-muted)]">
                    <tr><th className="py-2">Run</th><th className="py-2">Policy</th><th className="py-2 text-right">Decisoes</th><th className="py-2 text-right">Aprov.</th><th className="py-2 text-right">Rejeit.</th><th className="py-2 text-right">Fix</th><th className="py-2">Arquivo</th></tr>
                  </thead>
                  <tbody>
                    {tokenDecisionRuns.map((row) => (
                      <tr key={row.decision_run_id} className="border-t border-[var(--dash-border)]">
                        <td className="py-2 font-semibold text-[var(--dash-text)]">{row.decision_run_id}</td>
                        <td className="py-2">{row.policy_run_id}</td>
                        <td className="py-2 text-right">{fmt(row.total_decisions)}</td>
                        <td className="py-2 text-right text-emerald-400">{fmt(row.approved_count)}</td>
                        <td className="py-2 text-right text-red-400">{fmt(row.rejected_count)}</td>
                        <td className="py-2 text-right text-amber-400">{fmt(row.fix_count)}</td>
                        <td className="max-w-[360px] truncate py-2" title={row.decisions_path}>{row.decisions_path}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          </div>
        </div>
      ) : viewMode === 'Packages' ? (
        <Card className="p-5">
          <h3 className="text-sm font-semibold text-[var(--dash-text)]">Pacotes com Pendência</h3>
          <p className="mt-1 text-xs text-[var(--dash-muted)]">Ordenado por pendência, aplicação segura e reabertura.</p>
          <div className="mt-4 h-[560px] overflow-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs uppercase text-[var(--dash-muted)]">
                <tr><th className="py-2">Pacote</th><th className="py-2 text-right">Total</th><th className="py-2 text-right">Closed</th><th className="py-2 text-right">Pending</th><th className="py-2 text-right">Aplicar</th><th className="py-2 text-right">Reabrir</th><th className="py-2 text-right">Closed %</th></tr>
              </thead>
              <tbody>
                {packageBacklog.map((row) => (
                  <tr key={row.package_name} className="border-t border-[var(--dash-border)]">
                    <td className="py-2 font-semibold text-[var(--dash-text)]">{row.package_name}</td>
                    <td className="py-2 text-right">{fmt(row.total)}</td>
                    <td className="py-2 text-right text-emerald-400">{fmt(row.closed_count)}</td>
                    <td className="py-2 text-right text-amber-400">{fmt(row.pending_count)}</td>
                    <td className="py-2 text-right text-blue-400">{fmt(row.needs_apply_count)}</td>
                    <td className="py-2 text-right text-red-400">{fmt(row.reopen_count)}</td>
                    <td className="py-2 text-right">{pct(row.closed_pct)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ) : (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <Card className="p-5">
            <h3 className="text-sm font-semibold text-[var(--dash-text)]">Aplicar Confirmação no Output</h3>
            <p className="mt-1 text-xs text-[var(--dash-muted)]">Existe confirmação confiável, mas o output ainda não bate com ela.</p>
            <div className="mt-4 h-[520px] overflow-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase text-[var(--dash-muted)]">
                  <tr><th className="py-2">Segmento</th><th className="py-2">Arquivo</th><th className="py-2">Linha</th><th className="py-2">Chave</th><th className="py-2">Estado</th><th className="py-2 text-right">Prior.</th></tr>
                </thead>
                <tbody>
                  {applyQueue.map((row) => (
                    <tr key={`apply-${row.segment_id}`} className="border-t border-[var(--dash-border)]">
                      <td className="py-2 text-[var(--dash-muted)]">{row.segment_id}</td>
                      <td className="max-w-[180px] truncate py-2" title={row.relative_path}>{row.relative_path}</td>
                      <td className="py-2">{row.source_line_number}</td>
                      <td className="max-w-[190px] truncate py-2 font-semibold text-[var(--dash-text)]" title={row.source_key}>{row.source_key}</td>
                      <td className="py-2 text-blue-400">{row.final_state}</td>
                      <td className="py-2 text-right">{metric(row.priority_score)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
          <Card className="p-5">
            <h3 className="text-sm font-semibold text-[var(--dash-text)]">Reabrir por ML Atual</h3>
            <p className="mt-1 text-xs text-[var(--dash-muted)]">Auto-confirmados que agora pedem reparo/revisão.</p>
            <div className="mt-4 h-[520px] overflow-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase text-[var(--dash-muted)]">
                  <tr><th className="py-2">Segmento</th><th className="py-2">Arquivo</th><th className="py-2">Chave</th><th className="py-2">Ativo</th><th className="py-2">Candidato</th><th className="py-2">Policy</th><th className="py-2 text-right">Prior.</th></tr>
                </thead>
                <tbody>
                  {reopenQueue.map((row) => (
                    <tr key={`reopen-${row.segment_id}`} className="border-t border-[var(--dash-border)]">
                      <td className="py-2 text-[var(--dash-muted)]">{row.segment_id}</td>
                      <td className="max-w-[160px] truncate py-2" title={row.relative_path}>{row.relative_path}</td>
                      <td className="max-w-[190px] truncate py-2 font-semibold text-[var(--dash-text)]" title={row.source_key}>{row.source_key}</td>
                      <td className="py-2">{row.active_action}</td>
                      <td className="py-2">{row.candidate_action}</td>
                      <td className="py-2 text-amber-400">{row.policy_action}</td>
                      <td className="py-2 text-right">{metric(row.priority_score)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}

function Governance({ data }) {
  const [viewMode, setViewMode] = useState('Risco');
  const { kpis, promotionTimeline, blockReasons, confirmationSources, policy, modelSnapshot } = data.governance;
  const isRiskView = viewMode === 'Risco';

  return (
    <div className="flex h-full min-h-0 flex-col gap-4 pb-3">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-6">
        <StatCard title="Locked Humanos" value={compact(kpis.lockedHuman)} icon={Lock} color="violet" />
        <StatCard title="Blocked Structure" value={fmt(kpis.blockedStructure)} icon={ShieldAlert} color="red" />
        <StatCard title="Token Issues" value={fmt(kpis.tokenIssues)} icon={FileWarning} color="amber" />
        <StatCard title="False Safe Holdout" value={fmt(kpis.falseSafeHoldout)} icon={ShieldCheck} color={kpis.falseSafeHoldout ? 'red' : 'emerald'} />
        <StatCard title="Modelos Treinados" value={fmt(kpis.totalModels)} icon={Database} color="blue" />
        <StatCard title="Última Promoção" value={kpis.lastPromotion} icon={Rocket} color="blue" />
      </div>

      <ViewHeader title={isRiskView ? 'Visão de Risco' : 'Visão de Controle'} subtitle={isRiskView ? 'Promoções, rejeições e motivos que bloqueiam automações perigosas.' : 'Origem das confirmações e política ativa de segurança.'}>
        <ViewToggle options={['Risco', 'Controle']} value={viewMode} onChange={setViewMode} />
      </ViewHeader>

      {isRiskView ? (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <ChartCard title="Holdout Coverage por Modelo" subtitle="Verde promove. Vermelho rejeita.">
            <ChartBox className="h-[445px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={promotionTimeline} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="currentColor" opacity={0.12} />
                  <XAxis dataKey="model" axisLine={false} tickLine={false} tick={chartText} />
                  <YAxis tickFormatter={(value) => `${value}%`} axisLine={false} tickLine={false} tick={chartText} />
                  <Tooltip content={<ModelTooltip />} />
                  <Bar dataKey="holdoutCoverage" name="Holdout Coverage" radius={[8, 8, 0, 0]}>
                    {promotionTimeline.map((entry) => <Cell key={entry.model} fill={entry.decision === 'Promovido' ? '#10b981' : '#ef4444'} />)}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </ChartBox>
          </ChartCard>

          <ChartCard title="Motivos de Bloqueio" subtitle="Riscos estruturais antes da automação">
            {blockReasons.length ? (
              <ChartBox className="h-[445px]">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={blockReasons} layout="vertical" margin={{ top: 8, right: 24, left: 45, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke="currentColor" opacity={0.12} />
                    <XAxis type="number" axisLine={false} tickLine={false} tick={chartText} />
                    <YAxis type="category" dataKey="reason" width={150} axisLine={false} tickLine={false} tick={chartText} />
                    <Tooltip formatter={(value) => fmt(value)} />
                    <Bar dataKey="count" name="Ocorrências" fill="#ef4444" radius={[0, 8, 8, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </ChartBox>
            ) : (
              <div className="flex h-[445px] items-center justify-center rounded-lg border border-dashed border-[var(--dash-border)] text-center text-sm text-[var(--dash-muted)]">
                Nenhum motivo de bloqueio estruturado registrado no score operacional atual.
              </div>
            )}
          </ChartCard>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
          <ChartCard title="Fontes de Confirmação" subtitle="Origem da confiança atual" className="xl:col-span-1">
            <ChartBox className="h-[445px]">
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie data={confirmationSources} dataKey="value" nameKey="name" innerRadius={58} outerRadius={96} paddingAngle={3}>
                    {confirmationSources.map((entry) => <Cell key={entry.name} fill={entry.color} />)}
                  </Pie>
                  <Tooltip formatter={(value) => fmt(value)} />
                  <Legend />
                </PieChart>
              </ResponsiveContainer>
            </ChartBox>
          </ChartCard>

          <Card className="p-5 xl:col-span-2">
            <h3 className="text-lg font-bold text-[var(--dash-text)]">Política Atual de Segurança</h3>
            <p className="mt-1 text-sm text-[var(--dash-muted)]">As regras que protegem o pacote antes de qualquer automação.</p>
            <div className="mt-4 grid content-start gap-3 md:grid-cols-2 xl:grid-cols-4">
              {policy.map((item) => (
                <div key={item.title} className="rounded-lg bg-[var(--dash-subtle)] p-3">
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-400">{item.title}</p>
                  <p className="mt-1 font-bold text-[var(--dash-text)]">{item.value}</p>
                </div>
              ))}
            </div>
            <div className="mt-5 grid gap-4 xl:grid-cols-2">
              <ModelSnapshotColumn model={modelSnapshot.active} />
              <ModelSnapshotColumn model={modelSnapshot.latest} />
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}

function Policy({ data }) {
  const [viewMode, setViewMode] = useState('Charts');
  const policy = data.policy;
  if (!policy?.available) {
    return (
      <div className="flex h-full min-h-0 flex-col gap-4 pb-3">
        <Card className="flex h-[520px] items-center justify-center p-6 text-center text-[var(--dash-muted)]">
          Nenhuma política operacional executada ainda.
        </Card>
      </div>
    );
  }

  const { summary, comparison, groupGain, auditItems, history } = policy;
  const gainChart = groupGain.filter((item) => Number(item.new_safe) > 0).slice(0, 8);

  return (
    <div className="flex h-full min-h-0 flex-col gap-4 pb-3">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5">
        <StatCard title="Score Run" value={fmt(summary.score_run_id)} detail={`Policy run ${summary.policy_run_id}`} icon={Scale} color="blue" />
        <StatCard title="Segmentos Avaliados" value={compact(summary.scored_count)} detail={summary.model_version} icon={Database} color="violet" />
        <StatCard title="Auto-safe Modelo" value={compact(summary.active_auto_safe_count)} detail={pct(summary.active_auto_safe_pct)} icon={ShieldCheck} color="emerald" />
        <StatCard title="Auto-safe Política" value={compact(summary.policy_auto_safe_count)} detail={pct(summary.policy_auto_safe_pct)} icon={ShieldCheck} color="emerald" />
        <StatCard title="Novos Seguros" value={fmt(summary.new_safe_count)} trend={pct(summary.new_safe_pct)} detail={`Proteção ativa: ${summary.protect_active_safe ? 'on' : 'off'}`} icon={ArrowUpRight} color="amber" />
      </div>

      <ViewHeader title={viewMode === 'Charts' ? 'Comparison View' : 'Audit View'} subtitle={viewMode === 'Charts' ? 'Comparação agregada entre score puro e política.' : 'Grupos e amostra dos novos seguros para inspeção.'}>
        <ViewToggle options={['Charts', 'Audit']} value={viewMode} onChange={setViewMode} />
      </ViewHeader>

      {viewMode === 'Charts' ? (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <ChartCard title="Score vs Policy" subtitle="Score puro comparado à política operacional por grupo">
            <ChartBox className="h-[445px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={comparison} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="currentColor" opacity={0.12} />
                  <XAxis dataKey="action" tickFormatter={(v) => actionLabels[v] ?? v} axisLine={false} tickLine={false} tick={chartText} />
                  <YAxis tickFormatter={(v) => compact(v)} axisLine={false} tickLine={false} tick={chartText} />
                  <Tooltip formatter={(value) => fmt(value)} labelFormatter={(label) => actionLabels[label] ?? label} />
                  <Legend />
                  <Bar dataKey="score" name="Score puro" fill="#64748b" radius={[8, 8, 0, 0]} />
                  <Bar dataKey="policy" name="Policy" fill="#10b981" radius={[8, 8, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </ChartBox>
          </ChartCard>

          <ChartCard title="Ganho por Grupo" subtitle="Grupos que geraram novos auto-safe">
            <ChartBox className="h-[445px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={gainChart} layout="vertical" margin={{ top: 8, right: 24, left: 80, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke="currentColor" opacity={0.12} />
                  <XAxis type="number" axisLine={false} tickLine={false} tick={chartText} />
                  <YAxis type="category" dataKey="policy_group" width={170} axisLine={false} tickLine={false} tick={chartText} />
                  <Tooltip formatter={(value) => fmt(value)} />
                  <Bar dataKey="new_safe" name="Novos seguros" fill="#f59e0b" radius={[0, 8, 8, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </ChartBox>
          </ChartCard>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-5">
          <Card className="p-5 xl:col-span-2">
            <h3 className="text-lg font-bold text-[var(--dash-text)]">Grupos</h3>
            <p className="mt-1 text-sm text-[var(--dash-muted)]">Resumo por política aplicada.</p>
            <div className="mt-4 h-[445px] overflow-y-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase text-[var(--dash-muted)]">
                  <tr>
                    <th className="py-2">Grupo</th>
                    <th className="py-2 text-right">Novo</th>
                    <th className="py-2 text-right">Humano +</th>
                    <th className="py-2 text-right">Threshold</th>
                  </tr>
                </thead>
                <tbody>
                  {groupGain.map((item) => (
                    <tr key={item.policy_group} className="border-t border-[var(--dash-border)]">
                      <td className="max-w-[210px] truncate py-2 font-semibold text-[var(--dash-text)]">{item.policy_group}</td>
                      <td className="py-2 text-right text-amber-400">{fmt(item.new_safe)}</td>
                      <td className="py-2 text-right text-emerald-400">{fmt(item.new_safe_learned_positive)}</td>
                      <td className="py-2 text-right">{metric(item.min_threshold)}-{metric(item.max_threshold)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <Card className="p-5 xl:col-span-3">
            <h3 className="text-lg font-bold text-[var(--dash-text)]">Auditoria dos Novos Seguros</h3>
            <p className="mt-1 text-sm text-[var(--dash-muted)]">Amostra auditável; não aplica output automaticamente.</p>
            <div className="mt-4 h-[445px] overflow-y-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase text-[var(--dash-muted)]">
                  <tr>
                    <th className="py-2">Segmento</th>
                    <th className="py-2">Grupo</th>
                    <th className="py-2">Chave</th>
                    <th className="py-2 text-right">Prob.</th>
                    <th className="py-2">Razões</th>
                  </tr>
                </thead>
                <tbody>
                  {auditItems.slice(0, 60).map((item) => (
                    <tr key={`${item.segment_id}-${item.source_key}`} className="border-t border-[var(--dash-border)]">
                      <td className="py-2 text-[var(--dash-muted)]">{item.segment_id}</td>
                      <td className="max-w-[180px] truncate py-2">{item.policy_group}</td>
                      <td className="max-w-[220px] truncate py-2 font-semibold text-[var(--dash-text)]" title={item.source_key}>{item.source_key}</td>
                      <td className="py-2 text-right text-emerald-400">{pctMetric(item.model_safe_probability)}</td>
                      <td className="py-2">
                        <div className="flex flex-wrap gap-1">
                          {parseReasons(item.reasons_json).map((reason) => <Badge key={reason} tone="blue">{reason}</Badge>)}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </div>
      )}

      {history.length > 1 && (
        <Card className="p-5">
          <h3 className="text-lg font-bold text-[var(--dash-text)]">Policy Evolution</h3>
          <p className="mt-1 text-sm text-[var(--dash-muted)]">Histórico de cobertura por execução de política.</p>
        </Card>
      )}
    </div>
  );
}

function Lab({ data }) {
  const [viewMode, setViewMode] = useState('Overview');
  const lab = data.lab;
  if (!lab?.available) {
    return (
      <div className="flex h-full min-h-0 flex-col gap-4 pb-3">
        <Card className="flex h-[520px] items-center justify-center p-6 text-center text-[var(--dash-muted)]">
          Nenhum modelo experimental encontrado.
        </Card>
      </div>
    );
  }

  const { summary, gaps, actionComparison, recentModels, candidateDistribution, fileRegressions, regressionAudit } = lab;
  const candidateFalseSafe = Number(summary.candidate_false_safe_count ?? 0);
  const decisionTone = summary.promotion_decision === 'promote' ? 'emerald' : 'red';

  return (
    <div className="flex h-full min-h-0 flex-col gap-4 pb-3">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5">
        <StatCard title="Experimental" value={`Run ${summary.candidate_model_run_id}`} detail={summary.candidate_model_version} icon={Rocket} color="blue" />
        <StatCard title="False Safe" value={fmt(candidateFalseSafe)} detail="meta: zero" icon={ShieldCheck} color={candidateFalseSafe ? 'red' : 'emerald'} />
        <StatCard title="Safe Precision" value={pctMetric(summary.candidate_safe_precision)} detail="experimental" icon={ShieldCheck} color="emerald" />
        <StatCard title="Cobertura Gap" value={compact(gaps.auto_safe_gap_count)} detail={`${pct(gaps.auto_safe_gap_pct_points)} p.p. abaixo do ativo`} icon={ArrowDownRight} color="amber" danger />
        <StatCard title="Promoção" value={summary.promotion_decision ?? 'sem decisão'} detail={summary.candidate_model_version} icon={XCircle} color={decisionTone} />
      </div>

      <ViewHeader
        title={viewMode === 'Overview' ? 'Experimental Overview' : viewMode === 'Distribution' ? 'Candidate Distribution' : 'Regression Audit'}
        subtitle={viewMode === 'Overview' ? 'Comparação do experimental contra o modelo ativo.' : viewMode === 'Distribution' ? 'Distribuição operacional e decisão de promoção.' : 'Arquivos e segmentos onde o experimental regrediu.'}
      >
        <ViewToggle options={['Overview', 'Distribution', 'Regressions']} value={viewMode} onChange={setViewMode} />
      </ViewHeader>

      {viewMode === 'Overview' ? (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <ChartCard title="Ativo vs Experimental" subtitle="Distribuição operacional por ação">
            <ChartBox className="h-[445px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={actionComparison} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="currentColor" opacity={0.12} />
                  <XAxis dataKey="action" axisLine={false} tickLine={false} tick={chartText} />
                  <YAxis tickFormatter={(v) => compact(v)} axisLine={false} tickLine={false} tick={chartText} />
                  <Tooltip formatter={(value) => fmt(value)} />
                  <Legend />
                  <Bar dataKey="active" name="Ativo" fill="#10b981" radius={[8, 8, 0, 0]} />
                  <Bar dataKey="candidate" name="Experimental" fill="#f59e0b" radius={[8, 8, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </ChartBox>
          </ChartCard>

          <ChartCard title="Histórico Recente" subtitle="Coverage, Macro F1 e False Safe por modelo">
            <ChartBox className="h-[445px]">
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart data={recentModels} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="currentColor" opacity={0.12} />
                  <XAxis dataKey="model_run_id" axisLine={false} tickLine={false} tick={chartText} />
                  <YAxis yAxisId="left" tickFormatter={(v) => `${Math.round(v * 100)}%`} axisLine={false} tickLine={false} tick={chartText} />
                  <YAxis yAxisId="right" orientation="right" axisLine={false} tickLine={false} tick={chartText} />
                  <Tooltip formatter={(value, name) => name === 'False Safe' ? fmt(value) : pctMetric(value)} />
                  <Legend />
                  <Line yAxisId="left" type="monotone" dataKey="safe_recall" name="Coverage" stroke="#3b82f6" strokeWidth={3} dot={{ r: 3 }} />
                  <Line yAxisId="left" type="monotone" dataKey="macro_f1" name="Macro F1" stroke="#10b981" strokeWidth={3} dot={{ r: 3 }} />
                  <Bar yAxisId="right" dataKey="false_safe_count" name="False Safe" fill="#ef4444" radius={[8, 8, 0, 0]} />
                </ComposedChart>
              </ResponsiveContainer>
            </ChartBox>
          </ChartCard>
        </div>
      ) : viewMode === 'Distribution' ? (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <Card className="p-5">
              <h3 className="text-lg font-bold text-[var(--dash-text)]">Distribuição do Experimental</h3>
              <p className="mt-1 text-sm text-[var(--dash-muted)]">Ações e classes de risco no último score candidato.</p>
              <div className="mt-4 h-[445px] overflow-y-auto">
                <table className="w-full text-left text-sm">
                  <thead className="text-xs uppercase text-[var(--dash-muted)]">
                    <tr>
                      <th className="py-2">Ação</th>
                      <th className="py-2">Risco</th>
                      <th className="py-2 text-right">Total</th>
                      <th className="py-2 text-right">Prob.</th>
                      <th className="py-2 text-right">Blocks</th>
                    </tr>
                  </thead>
                  <tbody>
                    {candidateDistribution.map((item) => (
                      <tr key={`${item.final_action}-${item.risk_class}`} className="border-t border-[var(--dash-border)]">
                        <td className="py-2 font-semibold text-[var(--dash-text)]">{item.final_action}</td>
                        <td className="py-2">{item.risk_class}</td>
                        <td className="py-2 text-right">{fmt(item.total)}</td>
                        <td className="py-2 text-right text-emerald-400">{pctMetric(item.avg_safe_probability)}</td>
                        <td className="py-2 text-right text-red-400">{fmt(item.deterministic_blocked_count)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
          </Card>

          <Card className="p-5">
              <h3 className="text-lg font-bold text-[var(--dash-text)]">Decisão de Promoção</h3>
              <p className="mt-1 text-sm text-[var(--dash-muted)]">Por que o experimental ainda não substituiu o ativo.</p>
              <div className="mt-4 grid gap-3 md:grid-cols-2">
                <MetricTile title="Ativo Auto-safe" value={`${compact(summary.active_auto_safe_count)} (${pct(summary.active_auto_safe_pct)})`} tone="emerald" />
                <MetricTile title="Experimental Auto-safe" value={`${compact(summary.candidate_auto_safe_count)} (${pct(summary.candidate_auto_safe_pct)})`} tone="amber" />
                <MetricTile title="Active Macro F1" value={pctMetric(summary.active_macro_f1)} />
                <MetricTile title="Candidate Macro F1" value={pctMetric(summary.candidate_macro_f1)} />
                <MetricTile title="Gap Operacional" value={`${compact(gaps.auto_safe_gap_count)} / ${pct(gaps.auto_safe_gap_pct_points)} p.p.`} tone="red" className="md:col-span-2" />
                <div className="md:col-span-2 rounded-md border border-[var(--dash-border)] bg-[var(--dash-card)] p-3">
                  <p className="text-[0.68rem] font-semibold uppercase tracking-wide text-[var(--dash-muted)]">Promotion reason</p>
                  <p className="mt-1 break-words text-sm font-semibold text-[var(--dash-text)]">{summary.promotion_reason ?? 'sem motivo registrado'}</p>
                </div>
              </div>
          </Card>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-5">
          <Card className="p-5 xl:col-span-2">
            <h3 className="text-lg font-bold text-[var(--dash-text)]">Regressão por Arquivo</h3>
            <p className="mt-1 text-sm text-[var(--dash-muted)]">Era seguro no ativo, mas ficou pendente no experimental.</p>
            <div className="mt-4 h-[445px] overflow-y-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase text-[var(--dash-muted)]">
                  <tr>
                    <th className="py-2">Arquivo</th>
                    <th className="py-2 text-right">Reg.</th>
                    <th className="py-2 text-right">Recup.</th>
                    <th className="py-2 text-right">Prob.</th>
                  </tr>
                </thead>
                <tbody>
                  {fileRegressions.map((item) => (
                    <tr key={item.relative_path} className="border-t border-[var(--dash-border)]">
                      <td className="max-w-[240px] truncate py-2 font-semibold text-[var(--dash-text)]" title={item.relative_path}>{item.relative_path}</td>
                      <td className="py-2 text-right text-red-400">{fmt(item.operational_regressions)}</td>
                      <td className="py-2 text-right text-emerald-400">{fmt(item.candidate_recoveries)}</td>
                      <td className="py-2 text-right">{pctMetric(item.avg_candidate_safe_probability)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <Card className="p-5 xl:col-span-3">
            <h3 className="text-lg font-bold text-[var(--dash-text)]">Amostra Auditável de Regressão</h3>
            <p className="mt-1 text-sm text-[var(--dash-muted)]">Segmentos seguros no ativo e retidos pelo experimental.</p>
            <div className="mt-4 h-[445px] overflow-y-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase text-[var(--dash-muted)]">
                  <tr>
                    <th className="py-2">Segmento</th>
                    <th className="py-2">Arquivo</th>
                    <th className="py-2">Chave</th>
                    <th className="py-2">Ação</th>
                    <th className="py-2 text-right">Prob.</th>
                  </tr>
                </thead>
                <tbody>
                  {regressionAudit.slice(0, 80).map((item) => (
                    <tr key={`${item.segment_id}-${item.source_key}`} className="border-t border-[var(--dash-border)]">
                      <td className="py-2 text-[var(--dash-muted)]">{item.segment_id}</td>
                      <td className="max-w-[180px] truncate py-2" title={item.relative_path}>{item.relative_path}</td>
                      <td className="max-w-[220px] truncate py-2 font-semibold text-[var(--dash-text)]" title={item.source_key}>{item.source_key}</td>
                      <td className="py-2 text-amber-400">{item.candidate_action}</td>
                      <td className="py-2 text-right text-emerald-400">{pctMetric(item.candidate_safe_probability)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}

function Specialists({ data }) {
  const [viewMode, setViewMode] = useState('Overview');
  const specialists = data.specialists;
  const {
    summary,
    overview,
    coverageBySpecialist,
    auditorSummary,
    auditorBySpecialist,
    auditorQueue,
    learningSummary,
    learningByLabel,
    learningByFocus,
    titleNamesEvolution,
  } = specialists;
  const titleNamesChart = titleNamesEvolution.map((item) => ({
    ...item,
    auto_safe_ratio: Number(item.auto_safe_pct ?? 0) / 100,
  }));

  return (
    <div className="flex h-full min-h-0 flex-col gap-4 pb-3">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-6">
        <StatCard title="Especialistas" value={fmt(summary.specialists_total)} detail="último por família" icon={Database} color="violet" />
        <StatCard title="Com Score" value={fmt(summary.specialists_with_score)} detail="score finalizado" icon={SearchCheck} color="blue" />
        <StatCard title="False Safe Especialistas" value={fmt(summary.specialist_false_safe)} detail="meta: zero" icon={ShieldAlert} color={summary.specialist_false_safe ? 'red' : 'emerald'} />
        <StatCard title="Cobertura Especialista" value={pct(summary.selected_auto_safe_pct)} detail={summary.selected_model_kind} icon={ShieldCheck} color="emerald" />
        <StatCard title="Auditoria Aberta" value={fmt(summary.auditor_review_required)} detail={pct(summary.auditor_review_required_pct)} icon={AlertCircle} color="amber" />
        <StatCard title="Revisões Humanas" value={fmt(summary.human_reviewed_total)} detail="fila especialista" icon={CheckCircle2} color="emerald" />
      </div>

      <ViewHeader
        title={viewMode === 'Overview' ? 'Specialist Overview' : viewMode === 'Auditor' ? 'General vs Specialist Auditor' : 'Specialist Learning'}
        subtitle={viewMode === 'Overview' ? 'Últimos modelos por família e cobertura por escopo.' : viewMode === 'Auditor' ? 'Divergências entre score geral e especialista.' : 'Revisões humanas da fila especialista.'}
      >
        <ViewToggle options={['Overview', 'Auditor', 'Learning']} value={viewMode} onChange={setViewMode} />
      </ViewHeader>

      {viewMode === 'Overview' ? (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <Card className="p-5">
            <h3 className="text-lg font-bold text-[var(--dash-text)]">Especialistas por Escopo</h3>
            <p className="mt-1 text-sm text-[var(--dash-muted)]">Último modelo finalizado por família.</p>
            <div className="mt-4 h-[445px] overflow-y-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase text-[var(--dash-muted)]">
                  <tr><th className="py-2">Especialista</th><th className="py-2 text-right">Run</th><th className="py-2 text-right">Score</th><th className="py-2 text-right">F1</th><th className="py-2 text-right">Recall</th><th className="py-2 text-right">False</th><th className="py-2 text-right">Auto-safe</th></tr>
                </thead>
                <tbody>
                  {overview.map((item) => (
                    <tr key={item.model_kind} className="border-t border-[var(--dash-border)]">
                      <td className="max-w-[230px] truncate py-2 font-semibold text-[var(--dash-text)]" title={item.model_version}>{item.model_kind}</td>
                      <td className="py-2 text-right">{item.model_run_id}</td>
                      <td className="py-2 text-right">{item.score_run_id ?? '-'}</td>
                      <td className="py-2 text-right">{pctMetric(item.macro_f1)}</td>
                      <td className="py-2 text-right text-blue-400">{pctMetric(item.safe_recall)}</td>
                      <td className="py-2 text-right text-red-400">{fmt(item.false_safe_count)}</td>
                      <td className="py-2 text-right text-emerald-400">{pct(item.auto_safe_pct)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <ChartCard title="Cobertura por Especialista" subtitle="Auto-safe no escopo do score especialista">
            <ChartBox className="h-[445px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={coverageBySpecialist} layout="vertical" margin={{ top: 8, right: 18, left: 90, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke="currentColor" opacity={0.12} />
                  <XAxis type="number" tickFormatter={(v) => `${v}%`} axisLine={false} tickLine={false} tick={chartText} />
                  <YAxis type="category" dataKey="model_kind" width={190} axisLine={false} tickLine={false} tick={chartText} />
                  <Tooltip formatter={(value) => pct(value)} />
                  <Bar dataKey="auto_safe_pct" name="Auto-safe" fill="#3b82f6" radius={[0, 8, 8, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </ChartBox>
          </ChartCard>
        </div>
      ) : viewMode === 'Auditor' ? (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-5">
          <Card className="p-5 xl:col-span-2">
            <h3 className="text-lg font-bold text-[var(--dash-text)]">Divergência por Especialista</h3>
            <p className="mt-1 text-sm text-[var(--dash-muted)]">Score geral vs último score de cada especialista.</p>
            <div className="mt-4 h-[445px] overflow-y-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase text-[var(--dash-muted)]">
                  <tr><th className="py-2">Especialista</th><th className="py-2 text-right">Comp.</th><th className="py-2 text-right">New</th><th className="py-2 text-right">Demoted</th><th className="py-2 text-right">Review</th></tr>
                </thead>
                <tbody>
                  {auditorBySpecialist.map((item) => (
                    <tr key={item.model_kind} className="border-t border-[var(--dash-border)]">
                      <td className="max-w-[210px] truncate py-2 font-semibold text-[var(--dash-text)]">{item.model_kind}</td>
                      <td className="py-2 text-right">{fmt(item.compared)}</td>
                      <td className="py-2 text-right text-emerald-400">{fmt(item.specialist_new_safe_review)}</td>
                      <td className="py-2 text-right text-red-400">{fmt(item.specialist_demoted_review)}</td>
                      <td className="py-2 text-right text-amber-400">{pct(item.review_required_pct)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <Card className="p-5 xl:col-span-3">
            <h3 className="text-lg font-bold text-[var(--dash-text)]">Fila de Auditoria</h3>
            <p className="mt-1 text-sm text-[var(--dash-muted)]">Divergências ordenadas por demoted e new-safe.</p>
            <div className="mt-4 h-[445px] overflow-y-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase text-[var(--dash-muted)]">
                  <tr><th className="py-2">Segmento</th><th className="py-2">Arquivo</th><th className="py-2">Chave</th><th className="py-2">Geral</th><th className="py-2">Especialista</th><th className="py-2">Ação</th><th className="py-2 text-right">Prob.</th></tr>
                </thead>
                <tbody>
                  {auditorQueue.slice(0, 80).map((item) => (
                    <tr key={`${item.segment_id}-${item.source_key}`} className="border-t border-[var(--dash-border)]">
                      <td className="py-2 text-[var(--dash-muted)]">{item.segment_id}</td>
                      <td className="max-w-[160px] truncate py-2" title={item.relative_path}>{item.relative_path}</td>
                      <td className="max-w-[220px] truncate py-2 font-semibold text-[var(--dash-text)]" title={item.source_key}>{item.source_key}</td>
                      <td className="py-2">{item.general_action}</td>
                      <td className="py-2 text-blue-400">{item.specialist_action}</td>
                      <td className="py-2 text-amber-400">{item.auditor_action}</td>
                      <td className="py-2 text-right text-emerald-400">{pctMetric(item.specialist_probability)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <ChartCard title="Evolução title_names" subtitle="Cobertura, recall, F1 e false safe">
            <ChartBox className="h-[445px]">
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart data={titleNamesChart} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="currentColor" opacity={0.12} />
                  <XAxis dataKey="model_run_id" axisLine={false} tickLine={false} tick={chartText} />
                  <YAxis yAxisId="left" tickFormatter={(v) => `${Math.round(v * 100)}%`} axisLine={false} tickLine={false} tick={chartText} />
                  <YAxis yAxisId="right" orientation="right" axisLine={false} tickLine={false} tick={chartText} />
                  <Tooltip formatter={(value, name) => name === 'False Safe' ? fmt(value) : name === 'Auto-safe %' ? pct(value) : pctMetric(value)} />
                  <Legend />
                  <Line yAxisId="left" type="monotone" dataKey="safe_recall" name="Safe Recall" stroke="#f59e0b" strokeWidth={3} />
                  <Line yAxisId="left" type="monotone" dataKey="macro_f1" name="Macro F1" stroke="#10b981" strokeWidth={3} />
                  <Line yAxisId="left" type="monotone" dataKey="auto_safe_ratio" name="Auto-safe %" stroke="#3b82f6" strokeWidth={3} />
                  <Bar yAxisId="right" dataKey="false_safe_count" name="False Safe" fill="#ef4444" radius={[8, 8, 0, 0]} />
                </ComposedChart>
              </ResponsiveContainer>
            </ChartBox>
          </ChartCard>

          <Card className="p-5">
            <h3 className="text-lg font-bold text-[var(--dash-text)]">Aprendizado Humano</h3>
            <p className="mt-1 text-sm text-[var(--dash-muted)]">Revisões da fila `ml_specialist_auditor`.</p>
            <div className="mt-4 grid gap-3 md:grid-cols-2">
              <MetricTile title="Revisados" value={fmt(learningSummary.reviewed_total)} tone="blue" />
              <MetricTile title="Corretos" value={fmt(learningSummary.correct)} tone="emerald" />
              <MetricTile title="Minor Fix" value={fmt(learningSummary.minor_fix)} tone="amber" />
              <MetricTile title="Semantic Error" value={fmt(learningSummary.semantic_error)} tone={learningSummary.semantic_error ? 'red' : 'emerald'} />
              <MetricTile title="Taxa de Aceite" value={pct(learningSummary.acceptance_rate)} tone="emerald" />
              <MetricTile title="Texto Editado" value={fmt(learningSummary.corrected_text_total)} />
            </div>
            <div className="mt-4 grid gap-4 md:grid-cols-2">
              <div>
                <h4 className="text-sm font-bold text-[var(--dash-text)]">Labels</h4>
                {learningByLabel.map((item) => (
                  <div key={item.human_label} className="mt-2 flex justify-between border-b border-[var(--dash-border)] pb-1 text-sm">
                    <span>{item.human_label}</span><span className="font-bold">{fmt(item.total)}</span>
                  </div>
                ))}
              </div>
              <div>
                <h4 className="text-sm font-bold text-[var(--dash-text)]">Divergência</h4>
                {learningByFocus.map((item) => (
                  <div key={item.focus_group} className="mt-2 flex justify-between border-b border-[var(--dash-border)] pb-1 text-sm">
                    <span className="truncate">{item.focus_group}</span><span className="font-bold">{fmt(item.total)}</span>
                  </div>
                ))}
              </div>
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}

function Network({ data }) {
  const [viewMode, setViewMode] = useState('Topology');
  const agents = data.agents ?? {};
  const summary = agents.summary ?? {};
  const nodes = agents.topologyNodes ?? [];
  const health = agents.health ?? [];
  const recommendations = agents.recommendations ?? [];
  const routingRuns = agents.routingRuns ?? [];
  const ensembleImpact = agents.ensembleImpact ?? [];
  const promotionReadiness = agents.promotionReadiness ?? {};
  const experimentalContribution = agents.experimentalContribution ?? [];
  const routedItemsByAgent = agents.routedItemsByAgent ?? [];
  const agentTimeline = agents.agentTimeline ?? [];

  const toneFor = (value) => {
    if (value === 'active' || value === 'operational' || value === 'authoritative') return 'emerald';
    if (value === 'planned' || value === 'dry_run' || value === 'candidate') return 'amber';
    return 'slate';
  };
  const coreNodes = nodes.filter((node) => ['guard', 'macro_model', 'coordinator'].includes(node.agent_type));
  const specialistNodes = nodes.filter((node) => node.agent_type?.includes('specialist') && node.parent === 'coordinator_ensemble_v1');
  const subagentNodes = nodes.filter((node) => node.agent_type === 'subspecialist');
  const byParent = subagentNodes.reduce((acc, node) => {
    const parent = node.parent || 'sem pai';
    acc[parent] = acc[parent] || [];
    acc[parent].push(node);
    return acc;
  }, {});

  const AgentNodeCard = ({ node }) => (
    <Card className="p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h4 className="truncate text-sm font-bold text-[var(--dash-text)]" title={node.id}>{node.id}</h4>
          <p className="mt-1 truncate text-xs text-[var(--dash-muted)]">{node.agent_type} · {node.decision_role}</p>
        </div>
        <Badge tone={toneFor(node.status)}>{node.status}</Badge>
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        <Badge tone={toneFor(node.operational_state)}>{node.operational_state}</Badge>
        <Badge tone={node.false_safe_count ? 'red' : 'emerald'}>false-safe {fmt(node.false_safe_count)}</Badge>
        {node.model_run_id ? <Badge tone="blue">model {node.model_run_id}</Badge> : <Badge tone="slate">no model</Badge>}
      </div>
    </Card>
  );

  return (
    <div className="flex h-full min-h-0 flex-col gap-4 pb-3">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-6">
        <StatCard title="Agentes Registrados" value={fmt(summary.agents_total)} detail="ml_agent_registry" icon={Database} color="blue" />
        <StatCard title="Operacionais" value={fmt(summary.agents_operational)} detail="active + operational/dry-run" icon={Workflow} color="emerald" />
        <StatCard title="Subagentes Experimentais" value={fmt(summary.experimental_subagents)} detail={`${fmt(summary.planned_subagents)} planned`} icon={PackageSearch} color="amber" />
        <StatCard title="Falso-Seguro" value={fmt(summary.latest_false_safe)} detail="últimos modelos por agente" icon={ShieldAlert} color={summary.latest_false_safe ? 'red' : 'emerald'} />
        <StatCard title="Ganho Ensemble" value={fmt(summary.ensemble_net_gain)} detail="policy - general auto-safe" icon={ArrowUpRight} color="emerald" />
        <StatCard title="Evidência Novos Agentes" value={fmt(summary.recommendation_evidence)} detail={`run ${summary.latest_recommendation_run_id ?? '-'}`} icon={SearchCheck} color="violet" />
      </div>

      <ViewHeader
        title={viewMode === 'Topology' ? 'Agent Topology' : viewMode === 'Health' ? 'Agent Health' : viewMode === 'Recommendations' ? 'Agent Recommendations' : viewMode === 'Promotion' ? 'Promotion Readiness' : 'Ensemble Impact'}
        subtitle={viewMode === 'Topology' ? 'Guards, macro model, coordinator, specialists e subagentes.' : viewMode === 'Health' ? 'Saúde operacional por agente registrado.' : viewMode === 'Recommendations' ? 'Evidências humanas para treinar ou reforçar subagentes.' : viewMode === 'Promotion' ? 'A rede composta está pronta para substituir o ativo promovido?' : 'Impacto da camada ensemble/policy por grupo.'}
      >
        <ViewToggle options={['Topology', 'Health', 'Recommendations', 'Promotion', 'Ensemble']} value={viewMode} onChange={setViewMode} />
      </ViewHeader>

      {viewMode === 'Topology' ? (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
          <Card className="p-5 xl:col-span-4">
            <h3 className="text-sm font-semibold text-[var(--dash-text)]">Fluxo Principal</h3>
            <p className="mt-1 text-xs text-[var(--dash-muted)]">Travas determinísticas, modelo geral e coordenador.</p>
            <div className="mt-4 space-y-3">
              {coreNodes.map((node, index) => (
                <div key={node.id}>
                  <AgentNodeCard node={node} />
                  {index < coreNodes.length - 1 && <div className="mx-auto my-2 h-6 w-px bg-[var(--dash-border)]" />}
                </div>
              ))}
            </div>
          </Card>

          <Card className="p-5 xl:col-span-8">
            <h3 className="text-sm font-semibold text-[var(--dash-text)]">Especialistas e Subagentes</h3>
            <p className="mt-1 text-xs text-[var(--dash-muted)]">Ativos, dry-run e planejados por família.</p>
            <div className="mt-4 grid max-h-[520px] gap-4 overflow-y-auto md:grid-cols-2">
              {specialistNodes.map((node) => (
                <div key={node.id} className="rounded-2xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-3">
                  <AgentNodeCard node={node} />
                  <div className="mt-3 grid gap-2">
                    {(byParent[node.id] ?? []).map((child) => (
                      <div key={child.id} className="rounded-xl border border-[var(--dash-border)] bg-[var(--dash-card)] p-3">
                        <div className="flex items-center justify-between gap-2">
                          <span className="truncate text-xs font-bold" title={child.id}>{child.id}</span>
                          <Badge tone={toneFor(child.status)}>{child.status}</Badge>
                        </div>
                        <div className="mt-2 flex flex-wrap gap-2">
                          <Badge tone={toneFor(child.operational_state)}>{child.operational_state}</Badge>
                          {child.model_kind ? <Badge tone="blue">{child.model_kind}</Badge> : <Badge tone="slate">no model</Badge>}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </Card>
        </div>
      ) : viewMode === 'Health' ? (
        <Card className="p-5">
          <h3 className="text-sm font-semibold text-[var(--dash-text)]">Saúde por Agente</h3>
          <p className="mt-1 text-xs text-[var(--dash-muted)]">Modelo, score, threshold, cobertura e snapshot operacional.</p>
          <div className="mt-4 h-[560px] overflow-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs uppercase text-[var(--dash-muted)]">
                <tr>
                  <th className="py-2">Agente</th><th className="py-2">Tipo</th><th className="py-2">Pai</th><th className="py-2">Estado</th><th className="py-2">Model kind</th><th className="py-2 text-right">Model</th><th className="py-2 text-right">Score</th><th className="py-2 text-right">Threshold</th><th className="py-2 text-right">F1</th><th className="py-2 text-right">Precision</th><th className="py-2 text-right">Recall</th><th className="py-2 text-right">False</th><th className="py-2 text-right">Scored</th><th className="py-2 text-right">Auto-safe</th><th className="py-2 text-right">Pending</th>
                </tr>
              </thead>
              <tbody>
                {health.map((row) => (
                  <tr key={row.agent_key} className="border-t border-[var(--dash-border)]">
                    <td className="max-w-[210px] truncate py-2 font-semibold text-[var(--dash-text)]" title={row.agent_key}>{row.agent_key}</td>
                    <td className="py-2">{row.agent_type}</td>
                    <td className="max-w-[180px] truncate py-2" title={row.parent_agent_key}>{row.parent_agent_key ?? '-'}</td>
                    <td className="py-2"><Badge tone={toneFor(row.operational_state)}>{row.operational_state}</Badge></td>
                    <td className="max-w-[210px] truncate py-2" title={row.model_kind}>{row.model_kind ?? '-'}</td>
                    <td className="py-2 text-right">{row.model_run_id ?? '-'}</td>
                    <td className="py-2 text-right">{row.score_run_id ?? '-'}</td>
                    <td className="py-2 text-right">{row.default_threshold ? pct(Number(row.default_threshold) * 100) : '-'}</td>
                    <td className="py-2 text-right">{row.macro_f1 ? pctMetric(row.macro_f1) : '-'}</td>
                    <td className="py-2 text-right">{row.safe_precision ? pctMetric(row.safe_precision) : '-'}</td>
                    <td className="py-2 text-right">{row.safe_recall ? pctMetric(row.safe_recall) : '-'}</td>
                    <td className="py-2 text-right text-red-400">{fmt(row.false_safe_count)}</td>
                    <td className="py-2 text-right">{fmt(row.scored_count)}</td>
                    <td className="py-2 text-right text-emerald-400">{pct(row.auto_safe_pct)}</td>
                    <td className="py-2 text-right text-amber-400">{fmt(row.pending_real_count)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ) : viewMode === 'Recommendations' ? (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <Card className="p-5">
            <h3 className="text-sm font-semibold text-[var(--dash-text)]">Recomendações de Subagentes</h3>
            <p className="mt-1 text-xs text-[var(--dash-muted)]">Priorizadas por negativos, correções e evidência.</p>
            <div className="mt-4 h-[520px] overflow-y-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase text-[var(--dash-muted)]">
                  <tr><th className="py-2">Subagente</th><th className="py-2">Pai</th><th className="py-2">Tipo</th><th className="py-2">Status</th><th className="py-2 text-right">Evid.</th><th className="py-2 text-right">Pos.</th><th className="py-2 text-right">Neg.</th><th className="py-2 text-right">Corr.</th></tr>
                </thead>
                <tbody>
                  {recommendations.map((row) => (
                    <tr key={row.proposed_agent_key} className="border-t border-[var(--dash-border)]">
                      <td className="max-w-[220px] truncate py-2 font-semibold text-[var(--dash-text)]" title={row.proposed_agent_key}>{row.proposed_agent_key}</td>
                      <td className="py-2">{row.parent_agent_key}</td>
                      <td className="py-2"><Badge tone="blue">{row.recommendation_type}</Badge></td>
                      <td className="py-2"><Badge tone={toneFor(row.status)}>{row.status}</Badge></td>
                      <td className="py-2 text-right">{fmt(row.evidence_count)}</td>
                      <td className="py-2 text-right text-emerald-400">{fmt(row.positive_count)}</td>
                      <td className="py-2 text-right text-red-400">{fmt(row.negative_count)}</td>
                      <td className="py-2 text-right text-amber-400">{fmt(row.corrected_count)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
          <Card className="p-5">
            <h3 className="text-sm font-semibold text-[var(--dash-text)]">Motivos e Amostras</h3>
            <p className="mt-1 text-xs text-[var(--dash-muted)]">Prévia compacta da evidência humana.</p>
            <div className="mt-4 h-[520px] space-y-3 overflow-y-auto">
              {recommendations.map((row) => (
                <div key={row.proposed_agent_key} className="rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-3">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <h4 className="text-sm font-bold">{row.proposed_agent_key}</h4>
                      <p className="mt-1 text-xs text-[var(--dash-muted)]">{row.reason}</p>
                    </div>
                    <Badge tone={row.negative_count ? 'red' : 'emerald'}>{fmt(row.sample_count)} samples</Badge>
                  </div>
                  <div className="mt-3 space-y-2">
                    {(row.sample_preview ?? []).map((sample) => (
                      <div key={`${row.proposed_agent_key}-${sample.segment_id}`} className="rounded-lg bg-[var(--dash-card)] p-2 text-xs">
                        <span className="font-semibold">{sample.segment_id}</span> · <span>{sample.source_key}</span> · <span className="text-amber-400">{sample.human_label}</span>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </Card>
        </div>
      ) : viewMode === 'Promotion' ? (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
          <Card className="p-5 xl:col-span-7">
            <h3 className="text-sm font-semibold text-[var(--dash-text)]">Camadas Comparadas</h3>
            <p className="mt-1 text-xs text-[var(--dash-muted)]">Ativo promovido, macro candidato, ensemble operacional e subagentes experimentais.</p>
            <div className="mt-4 grid gap-3 md:grid-cols-2">
              <MetricTile title="Ativo Promovido" value={`Run ${promotionReadiness.active_model_run_id ?? '-'}`} tone="blue" />
              <MetricTile title="Score Ativo" value={`${compact(promotionReadiness.active_auto_safe_count)} / ${pct(promotionReadiness.active_auto_safe_pct)}`} tone="emerald" />
              <MetricTile title="Candidato Macro" value={`Run ${promotionReadiness.candidate_model_run_id ?? '-'}`} tone="blue" />
              <MetricTile title="Macro F1" value={pctMetric(promotionReadiness.candidate_macro_f1)} tone="emerald" />
              <MetricTile title="Ensemble Atual" value={`Policy ${promotionReadiness.ensemble_policy_run_id ?? '-'}`} tone="amber" />
              <MetricTile title="Ganho vs Macro" value={fmt(promotionReadiness.ensemble_gain_vs_candidate_macro)} tone="emerald" />
              <MetricTile title="Gap vs Ativo" value={`${fmt(promotionReadiness.ensemble_gap_vs_active)} (${pct(promotionReadiness.ensemble_gap_vs_active_pct_points)})`} tone={promotionReadiness.ensemble_gap_vs_active < 0 ? 'red' : 'emerald'} />
              <MetricTile title="Status" value={promotionReadiness.promotion_readiness ?? 'sem dados'} tone={promotionReadiness.promotion_readiness === 'ready_for_review' ? 'emerald' : promotionReadiness.promotion_readiness?.startsWith('not_ready') ? 'red' : 'amber'} />
            </div>
            <div className="mt-4 rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-4">
              <h4 className="text-sm font-bold text-[var(--dash-text)]">Gates de Promoção</h4>
              <div className="mt-3 grid gap-2 md:grid-cols-2">
                <Badge tone={promotionReadiness.candidate_false_safe_count ? 'red' : 'emerald'}>false safe {fmt(promotionReadiness.candidate_false_safe_count)}</Badge>
                <Badge tone={promotionReadiness.ensemble_gap_vs_active < 0 ? 'red' : 'emerald'}>gap ativo {fmt(promotionReadiness.ensemble_gap_vs_active)}</Badge>
                <Badge tone="amber">experimental {fmt(promotionReadiness.experimental_agents)}</Badge>
                <Badge tone="blue">operacional {fmt(promotionReadiness.operational_agents)}</Badge>
              </div>
            </div>
          </Card>

          <Card className="p-5 xl:col-span-5">
            <h3 className="text-sm font-semibold text-[var(--dash-text)]">Subagentes Experimentais</h3>
            <p className="mt-1 text-xs text-[var(--dash-muted)]">Caminho de evolução futura, sem autoridade operacional automática.</p>
            <div className="mt-4 h-[420px] overflow-y-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase text-[var(--dash-muted)]">
                  <tr><th className="py-2">Agente</th><th className="py-2 text-right">Rows</th><th className="py-2 text-right">New</th><th className="py-2 text-right">Demoted</th><th className="py-2 text-right">Reco</th></tr>
                </thead>
                <tbody>
                  {experimentalContribution.map((row) => (
                    <tr key={row.agent_key} className="border-t border-[var(--dash-border)]">
                      <td className="max-w-[190px] truncate py-2 font-semibold text-[var(--dash-text)]" title={row.agent_key}>{row.agent_key}</td>
                      <td className="py-2 text-right">{fmt(row.sampled_rows)}</td>
                      <td className="py-2 text-right text-emerald-400">{fmt(row.potential_new_safe)}</td>
                      <td className="py-2 text-right text-red-400">{fmt(row.potential_demotions)}</td>
                      <td className="py-2 text-right text-amber-400">{fmt(row.recommendation_rows)}</td>
                    </tr>
                  ))}
                  {!experimentalContribution.length && <tr><td className="py-6 text-center text-[var(--dash-muted)]" colSpan="5">Sem contribuição experimental materializada.</td></tr>}
                </tbody>
              </table>
            </div>
          </Card>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <ChartCard title="Ganho por Grupo" subtitle="New safe, demoted e aprendizado humano por policy_group">
            <ChartBox className="h-[445px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={ensembleImpact.slice(0, 12)} layout="vertical" margin={{ top: 8, right: 18, left: 100, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke="currentColor" opacity={0.12} />
                  <XAxis type="number" tickFormatter={(v) => compact(v)} axisLine={false} tickLine={false} tick={chartText} />
                  <YAxis type="category" dataKey="policy_group" width={190} axisLine={false} tickLine={false} tick={chartText} />
                  <Tooltip formatter={(value) => fmt(value)} />
                  <Legend />
                  <Bar dataKey="new_safe" name="New safe" fill="#10b981" radius={[0, 8, 8, 0]} />
                  <Bar dataKey="demoted_safe" name="Demoted" fill="#ef4444" radius={[0, 8, 8, 0]} />
                  <Bar dataKey="learned_positive" name="Learned +" fill="#3b82f6" radius={[0, 8, 8, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </ChartBox>
          </ChartCard>
          <ChartCard title="Timeline de Roteamento" subtitle="Cobertura ativa, planejada e recomendações por run">
            <ChartBox className="h-[445px]">
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart data={agentTimeline} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="currentColor" opacity={0.12} />
                  <XAxis dataKey="id" axisLine={false} tickLine={false} tick={chartText} />
                  <YAxis tickFormatter={(v) => compact(v)} axisLine={false} tickLine={false} tick={chartText} />
                  <Tooltip formatter={(value) => fmt(value)} />
                  <Legend />
                  <Bar dataKey="active_agent_covered_count" name="Active covered" fill="#10b981" radius={[8, 8, 0, 0]} />
                  <Bar dataKey="planned_agent_covered_count" name="Planned covered" fill="#f59e0b" radius={[8, 8, 0, 0]} />
                  <Line type="monotone" dataKey="recommendation_count" name="Recommendations" stroke="#3b82f6" strokeWidth={3} />
                </ComposedChart>
              </ResponsiveContainer>
            </ChartBox>
          </ChartCard>
          <Card className="p-5 xl:col-span-2">
            <h3 className="text-sm font-semibold text-[var(--dash-text)]">Itens Roteados por Agente</h3>
            <p className="mt-1 text-xs text-[var(--dash-muted)]">Amostra agregada do último run materializado pelo coordenador.</p>
            <div className="mt-4 max-h-[280px] overflow-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase text-[var(--dash-muted)]">
                  <tr><th className="py-2">Agente</th><th className="py-2">Status</th><th className="py-2 text-right">Rows</th><th className="py-2 text-right">Geral Safe</th><th className="py-2 text-right">Policy Safe</th><th className="py-2 text-right">Specialist Safe</th><th className="py-2 text-right">Reco</th></tr>
                </thead>
                <tbody>
                  {routedItemsByAgent.map((row) => (
                    <tr key={`${row.route_agent_key}-${row.route_status}`} className="border-t border-[var(--dash-border)]">
                      <td className="py-2 font-semibold text-[var(--dash-text)]">{row.route_agent_key}</td>
                      <td className="py-2"><Badge tone={toneFor(row.route_status)}>{row.route_status}</Badge></td>
                      <td className="py-2 text-right">{fmt(row.rows_count)}</td>
                      <td className="py-2 text-right">{fmt(row.general_auto_safe)}</td>
                      <td className="py-2 text-right text-emerald-400">{fmt(row.policy_auto_safe)}</td>
                      <td className="py-2 text-right text-blue-400">{fmt(row.specialist_auto_safe)}</td>
                      <td className="py-2 text-right text-amber-400">{fmt(row.recommendation_rows)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}

const statusTone = (status) => {
  const key = String(status ?? '').trim().toLowerCase();
  if (['done', 'completed', 'ready_for_game_test', 'ready_with_known_issues', 'idle'].includes(key)) return 'emerald';
  if (['starting', 'running', 'queued_visual_stub', 'checking'].includes(key)) return 'blue';
  if (['blocked', 'failed', 'learning_locked'].includes(key)) return 'red';
  if (['cancelled'].includes(key)) return 'amber';
  return 'amber';
};

const statusLabel = (status) => {
  const key = String(status ?? '').trim().toLowerCase();
  return ({
    done: 'Concluída',
    completed: 'Concluída',
    starting: 'Iniciando',
    running: 'Em execução',
    blocked: 'Bloqueada',
    pending: 'Pendente',
    failed: 'Falhou',
    cancelled: 'Cancelada',
    idle: 'Livre',
    learning_locked: 'Aprendizado bloqueado',
    ready_for_game_test: 'Pronta para teste',
    ready_with_known_issues: 'Pronta com ressalvas',
    queued_visual_stub: 'Etapa',
  }[key] ?? status ?? 'Unknown');
};

const normalizedStageStatus = (status) => {
  const key = String(status ?? '').trim().toLowerCase();
  return ({
    completed: 'done',
    complete: 'done',
    success: 'done',
    started: 'running',
    active: 'running',
    in_progress: 'running',
    queued: 'pending',
    queued_visual_stub: 'pending',
  }[key] ?? key ?? 'pending');
};

const normalizedRunStatus = (status) => String(status ?? '').trim().toLowerCase();

const openDashboardTab = (tab) => {
  const target = `${window.location.origin}${window.location.pathname}#${encodeURIComponent(tab)}`;
  window.open(target, '_blank', 'noopener,noreferrer');
};

const neuralProductionStages = {
  source_tree_snapshot: 'Registra o fingerprint exato dos sources English e Spanish usados na avaliacao.',
  score_package_old: 'Reavalia toda a baseline spanish_old com o modelo e as regras atuais.',
  score_package_output: 'Reavalia todo o output candidato com o mesmo modelo, regras e snapshot.',
  score_package_policy: 'Recalcula thresholds e politica usando o score fresco do candidato.',
  quality_epoch_validate: 'Confirma que scores, regras, modelo, sources, baseline e output pertencem a mesma epoch imutavel.',
  quality_promotion_approval: 'Consome todos os provedores registrados e aprova apenas promocoes que continuam integras e monotonicas.',
  quality_epoch_mark_evaluated: 'Fixa a epoch validada para que o Publicavel rejeite qualquer fila obsoleta.',
  pairwise_gender_token_monotonic_gate: 'Valida reparos locais de duplicacao a/o antes de ES_OA/ES_AO sem promover o segmento inteiro.',
  pairwise_gender_token_promotion_queue: 'Materializa apenas reparos de genero que mantiveram baseline fechado, tokens e validacao local.',
  pairwise_monotonic_promotion_queue: 'Materializa como confirmacao pendente apenas reparos locais que melhoram monotonicamente um baseline fechado.',
  segment_state_before: 'Classifica estado com sinais de ML, memoria, politicas e lifecycle.',
  apply_general_dry_run: 'Simula aplicacao de confirmacoes confiaveis, incluindo aprendizado aprovado.',
  apply_token_policy_dry_run: 'Simula aplicacao com gate de politica de tokens.',
  controlled_token_subpolicy_dry_run: 'Consome auditoria madura de subpolitica aprendida.',
  select_cstring_bridge_dry_run: 'Consome proposta Select_CString em shadow e classifica itens prontos, stale ou bloqueados.',
  same_token_boundary_repair_audit: 'Gera auditoria controlada para reparos same-token de boundary.',
  same_token_boundary_repair_dry_run: 'Simula reparos same-token com hashes, lifecycle e validacao local.',
  title_landed_es_repair_dry_run: 'Valida a fila 150 de adjetivos landed-title -es antes da escrita protegida.',
  apply_general_write: 'Escreve confirmacoes liberadas pela camada de confianca.',
  apply_token_policy_write: 'Escreve apenas excecoes aprovadas pela politica de tokens.',
  controlled_token_subpolicy_write: 'Promove corrected_text e escreve output por subpolitica controlada.',
  select_cstring_bridge_write: 'Promove a confirmacao governada Select_CString e escreve apenas itens prontos.',
  same_token_boundary_repair_write: 'Promove reparos same-token confiaveis e fecha no-op alinhado.',
  title_landed_es_repair_write: 'Promove confirmacoes e escreve correcoes PT-BR de titulos -es revisados.',
  segment_state_after: 'Recalcula fechamento apos aplicar conhecimento promovido.',
  token_policy_after: 'Reavalia tokens apos escrita controlada.',
  controlled_token_subpolicy_reaudit: 'Mede ganho real da subpolitica depois do novo segment-state.',
  select_cstring_bridge_reaudit: 'Mede ganho real da ponte Select_CString apos o novo segment-state.',
  same_token_boundary_repair_reaudit: 'Mede ganho real dos reparos same-token depois do segment-state.',
  title_landed_es_repair_reaudit: 'Confere fechamento real dos titulos -es apos segment-state.',
  composite_review_progress: 'Atualiza progresso do gate composto e handoff de aprendizado.',
  package_score_recalibration: 'Consolida filas e comparativos usando os scores ja fixados pelo Diagnostico.',
};

const productionStageDetails = {
  snapshot: 'Cria snapshot local antes de qualquer escrita no output.',
  snapshot_archive: 'Arquiva o snapshot para backup e rastreabilidade da execucao.',
  preflight_sync: 'Sincroniza indice, banco e fontes antes do fluxo principal.',
  source_tree_snapshot: neuralProductionStages.source_tree_snapshot,
  score_package_old: neuralProductionStages.score_package_old,
  score_package_output: neuralProductionStages.score_package_output,
  score_package_policy: neuralProductionStages.score_package_policy,
  quality_epoch_validate: neuralProductionStages.quality_epoch_validate,
  quality_promotion_approval: neuralProductionStages.quality_promotion_approval,
  quality_epoch_mark_evaluated: neuralProductionStages.quality_epoch_mark_evaluated,
  pairwise_gender_token_monotonic_gate: neuralProductionStages.pairwise_gender_token_monotonic_gate,
  pairwise_gender_token_promotion_queue: neuralProductionStages.pairwise_gender_token_promotion_queue,
  pairwise_monotonic_promotion_queue: neuralProductionStages.pairwise_monotonic_promotion_queue,
  segment_state_before: neuralProductionStages.segment_state_before,
  apply_general_dry_run: neuralProductionStages.apply_general_dry_run,
  apply_token_policy_dry_run: neuralProductionStages.apply_token_policy_dry_run,
  controlled_token_subpolicy_dry_run: neuralProductionStages.controlled_token_subpolicy_dry_run,
  select_cstring_bridge_dry_run: neuralProductionStages.select_cstring_bridge_dry_run,
  same_token_boundary_repair_audit: neuralProductionStages.same_token_boundary_repair_audit,
  same_token_boundary_repair_dry_run: neuralProductionStages.same_token_boundary_repair_dry_run,
  title_landed_es_repair_dry_run: neuralProductionStages.title_landed_es_repair_dry_run,
  apply_general_write: neuralProductionStages.apply_general_write,
  apply_token_policy_write: neuralProductionStages.apply_token_policy_write,
  controlled_token_subpolicy_write: neuralProductionStages.controlled_token_subpolicy_write,
  select_cstring_bridge_write: neuralProductionStages.select_cstring_bridge_write,
  same_token_boundary_repair_write: neuralProductionStages.same_token_boundary_repair_write,
  title_landed_es_repair_write: neuralProductionStages.title_landed_es_repair_write,
  apply_locked_override_write: 'Reescreve excecoes manuais travadas e confirmadas no banco.',
  segment_state_after: neuralProductionStages.segment_state_after,
  token_policy_after: neuralProductionStages.token_policy_after,
  controlled_token_subpolicy_reaudit: neuralProductionStages.controlled_token_subpolicy_reaudit,
  select_cstring_bridge_reaudit: neuralProductionStages.select_cstring_bridge_reaudit,
  same_token_boundary_repair_reaudit: neuralProductionStages.same_token_boundary_repair_reaudit,
  title_landed_es_repair_reaudit: neuralProductionStages.title_landed_es_repair_reaudit,
  composite_review_progress: neuralProductionStages.composite_review_progress,
  package_score_recalibration: neuralProductionStages.package_score_recalibration,
  production_report: 'Gera relatorio final com logs, pendencias e proximas acoes.',
};

const productionStageBlueprint = [
  ['snapshot', 'Snapshot pre-run'],
  ['snapshot_archive', 'Arquivar snapshot'],
  ['preflight_sync', 'Sincronizar indice'],
  ['source_tree_snapshot', 'Fingerprint dos sources'],
  ['score_package_old', 'Pontuar baseline old'],
  ['score_package_output', 'Pontuar candidato output'],
  ['score_package_policy', 'Recalcular politica de score'],
  ['quality_epoch_validate', 'Validar epoch de qualidade'],
  ['quality_promotion_approval', 'Aprovar filas de promocao'],
  ['segment_state_before', 'Segment-state inicial'],
  ['apply_general_dry_run', 'Dry-run geral'],
  ['apply_token_policy_dry_run', 'Dry-run politica de tokens'],
  ['controlled_token_subpolicy_dry_run', 'Dry-run subpolitica controlada'],
  ['select_cstring_bridge_dry_run', 'Dry-run Select_CString'],
  ['same_token_boundary_repair_audit', 'Auditoria same-token'],
  ['same_token_boundary_repair_dry_run', 'Dry-run reparo same-token'],
  ['title_landed_es_repair_dry_run', 'Dry-run titulos -es'],
  ['apply_general_write', 'Escrita regular'],
  ['apply_token_policy_write', 'Escrita por politica de token'],
  ['controlled_token_subpolicy_write', 'Escrita subpolitica controlada'],
  ['select_cstring_bridge_write', 'Escrita Select_CString'],
  ['same_token_boundary_repair_write', 'Escrita/fechamento same-token'],
  ['title_landed_es_repair_write', 'Escrita titulos -es'],
  ['apply_locked_override_write', 'Escrita overrides manuais'],
  ['segment_state_after', 'Segment-state pos-escrita'],
  ['token_policy_after', 'Politica de tokens pos-escrita'],
  ['controlled_token_subpolicy_reaudit', 'Reauditoria subpolitica controlada'],
  ['select_cstring_bridge_reaudit', 'Reauditoria Select_CString'],
  ['same_token_boundary_repair_reaudit', 'Reauditoria same-token'],
  ['title_landed_es_repair_reaudit', 'Reauditoria titulos -es'],
  ['composite_review_progress', 'Progresso composto'],
  ['quality_epoch_mark_evaluated', 'Fixar epoch avaliada'],
  ['package_score_recalibration', 'Consolidar epoch e filas'],
  ['production_report', 'Relatorio final'],
].map(([id, label]) => ({ id, label, status: 'queued_visual_stub' }));

const publicationStageDetails = {
  publication_preflight: 'Confere gates, score do pacote e fila confirmada antes de qualquer escrita.',
  apply_confirmed_dry_run: 'Simula a aplicacao exata dos segmentos em needs apply.',
  apply_confirmed_write: 'Escreve somente a fila confirmada que passou no dry-run.',
  pairwise_lifecycle_bridge: 'Consolida reparos pareados ja aplicados usando evidencia exata e idempotente.',
  segment_state_after: 'Recalcula fechamento apos o apply publicavel.',
  publication_preflight_after: 'Recalcula gates, needs apply e score do pacote depois da escrita.',
  production_report: 'Gera relatorio final do fluxo publicavel.',
};

const publicationStageBlueprint = [
  ['publication_preflight', 'Preflight publicavel'],
  ['apply_confirmed_dry_run', 'Dry-run apply confirmado'],
  ['apply_confirmed_write', 'Aplicar fila confirmada'],
  ['pairwise_lifecycle_bridge', 'Consolidar reparos pareados'],
  ['segment_state_after', 'Segment-state pos-apply'],
  ['publication_preflight_after', 'Preflight pos-apply'],
  ['production_report', 'Relatorio final'],
].map(([id, label]) => ({ id, label, status: 'queued_visual_stub' }));

const productionPhaseBlueprint = [
  {
    id: 'preparation',
    title: 'Preparacao',
    purpose: 'Protege o projeto antes de qualquer escrita.',
    stageIds: ['snapshot', 'snapshot_archive', 'preflight_sync', 'segment_state_before'],
  },
  {
    id: 'analysis_policy',
    title: 'Analise',
    purpose: 'Combina travas deterministicas, memoria e aprendizado promovido.',
    stageIds: [
      'apply_general_dry_run',
      'apply_token_policy_dry_run',
      'controlled_token_subpolicy_dry_run',
      'select_cstring_bridge_dry_run',
      'same_token_boundary_repair_audit',
      'same_token_boundary_repair_dry_run',
      'title_landed_es_repair_dry_run',
    ],
  },
  {
    id: 'controlled_apply',
    title: 'Escrita',
    purpose: 'Escreve somente o que passou pelos gates e confirmacoes.',
    stageIds: [
      'apply_general_write',
      'apply_token_policy_write',
      'controlled_token_subpolicy_write',
      'select_cstring_bridge_write',
      'same_token_boundary_repair_write',
      'title_landed_es_repair_write',
      'apply_locked_override_write',
    ],
  },
  {
    id: 'validation_handoff',
    title: 'Validacao',
    purpose: 'Recalcula estado, mede ganho real e entrega relatorio para aprendizado.',
    stageIds: [
      'segment_state_after',
      'token_policy_after',
      'controlled_token_subpolicy_reaudit',
      'select_cstring_bridge_reaudit',
      'same_token_boundary_repair_reaudit',
      'title_landed_es_repair_reaudit',
      'composite_review_progress',
      'production_report',
    ],
  },
];

const evaluationPhaseBlueprint = [
  {
    id: 'preparation',
    title: 'Preparacao',
    purpose: 'Protege a execucao e valida a epoch de qualidade criada pelo Diagnostico.',
    stageIds: ['snapshot', 'snapshot_archive', 'preflight_sync', 'quality_epoch_validate'],
  },
  {
    id: 'analysis_policy',
    title: 'Analise',
    purpose: 'Valida promocoes e executa dry-runs sem recalcular scores no meio do ciclo.',
    stageIds: [
      'quality_promotion_approval',
      'segment_state_before',
      'apply_general_dry_run',
      'apply_token_policy_dry_run',
      'controlled_token_subpolicy_dry_run',
      'select_cstring_bridge_dry_run',
      'same_token_boundary_repair_audit',
      'same_token_boundary_repair_dry_run',
      'title_landed_es_repair_dry_run',
    ],
  },
  {
    id: 'queue_report',
    title: 'Fila e score',
    purpose: 'Consolida promocoes, regressoes, pendencias e fixa a fila candidata para apply.',
    stageIds: ['composite_review_progress', 'quality_epoch_mark_evaluated', 'package_score_recalibration'],
  },
  {
    id: 'evaluation_report',
    title: 'Relatorio',
    purpose: 'Entrega comparativo old vs output e proximas acoes sem escrever output.',
    stageIds: ['production_report'],
  },
];

const publicationPhaseBlueprint = [
  {
    id: 'publication_gates',
    title: 'Gates',
    purpose: 'Confere bloqueios, score do pacote e fila confirmada.',
    stageIds: ['publication_preflight'],
  },
  {
    id: 'apply_confirmed',
    title: 'Apply confirmado',
    purpose: 'Simula e escreve apenas o que ja esta confirmado para output.',
    stageIds: [
      'apply_confirmed_dry_run',
      'apply_confirmed_write',
      'pairwise_lifecycle_bridge',
    ],
  },
  {
    id: 'publication_validation',
    title: 'Validacao',
    purpose: 'Recalcula estado e preflight depois da escrita protegida.',
    stageIds: ['segment_state_after', 'publication_preflight_after'],
  },
  {
    id: 'publication_report',
    title: 'Relatorio',
    purpose: 'Entrega comparativo final old vs output e status de publicacao.',
    stageIds: ['production_report'],
  },
];

const stageById = (stages) => stages.reduce((acc, stage) => {
  acc[stage.id] = stage;
  return acc;
}, {});

const buildProductionPhases = (stages, mode = 'evaluation') => {
  const stageBlueprint = mode === 'publication' ? publicationStageBlueprint : productionStageBlueprint;
  const phaseBlueprint = mode === 'publication'
    ? publicationPhaseBlueprint
    : mode === 'evaluation'
      ? evaluationPhaseBlueprint
      : productionPhaseBlueprint;
  const stageDetails = mode === 'publication' ? publicationStageDetails : productionStageDetails;
  const map = stageById(stages.length ? stages : stageBlueprint);
  return phaseBlueprint.map((phase) => {
    const phaseStages = phase.stageIds.map((id) => {
      const blueprint = stageBlueprint.find((stage) => stage.id === id);
      const live = map[id] ?? blueprint;
      return live ? { ...live, label: blueprint?.label ?? live.label, description: stageDetails[id] ?? live.description } : null;
    }).filter(Boolean);
    const done = phaseStages.filter((stage) => stage.status === 'done').length;
    const running = phaseStages.find((stage) => stage.status === 'running');
    const failed = phaseStages.find((stage) => stage.status === 'failed');
    const status = failed ? 'failed' : running ? 'running' : done === phaseStages.length && phaseStages.length ? 'done' : 'pending';
    const runningContribution = running
      ? clampNumber(Number(running.progress_pct ?? running.metrics?.progress_pct ?? 35), 8, 90) / 100
      : 0;
    return {
      ...phase,
      status,
      stages: phaseStages,
      done,
      total: phaseStages.length,
      progress: phaseStages.length ? Math.round(((done + runningContribution) / phaseStages.length) * 100) : 0,
      currentStage: running ?? phaseStages.find((stage) => stage.status !== 'done') ?? phaseStages.at(-1),
    };
  });
};

function ProductionControl({ data }) {
  const production = data.production ?? {};
  const summary = production.summary ?? {};
  const readiness = production.readiness ?? {};
  const lock = production.lock ?? {};
  const learning = production.learning ?? {};
  const selectCString = summary.select_cstring ?? {};
  const [startStatus, setStartStatus] = useState(null);
  const [startError, setStartError] = useState(null);
  const [runStatus, setRunStatus] = useState(production.run ?? null);
  const runStages = runStatus?.stages ?? [];
  const hasRun = Boolean(runStatus?.run_id);
  const displayStages = runStages.length ? runStages : productionStageBlueprint;
  const displayPhases = buildProductionPhases(displayStages);
  const canStart = learning.can_start_production ?? !lock.locked;
  const runActive = runStatus?.status === 'starting' || runStatus?.status === 'running';
  const currentRunStage = runStages.find((stage) => stage.id === runStatus?.current_stage);
  const completedRunStages = runStages.filter((stage) => stage.status === 'done').length;
  const runProgress = runStages.length ? Math.round((completedRunStages / runStages.length) * 100) : 0;
  const visibleStartStatus = runStatus?.status ?? startStatus;

  useEffect(() => {
    setRunStatus(production.run ?? null);
  }, [production.run?.run_id, production.run?.status]);

  useEffect(() => {
    if (!runActive) return undefined;
    const timer = setInterval(async () => {
      try {
        const response = await fetch(`${API_BASE}/production/runs/latest`);
        if (!response.ok) return;
        const payload = await response.json();
        const latestRun = payload.run ?? null;
        const baselineRunId = runStartBaselineIdRef.current;
        if (runStartPending && baselineRunId && latestRun?.run_id === baselineRunId) return;
        setRunStatus(latestRun);
      } catch {
        // Keep the last visible state; the next dashboard refresh can recover.
      }
    }, 5000);
    return () => clearInterval(timer);
  }, [runActive]);

  const startProduction = async () => {
    setStartStatus('checking');
    setStartError(null);
    try {
      const response = await fetch(`${API_BASE}/production/start`, { method: 'POST' });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        setStartStatus(payload.status ?? 'blocked');
        setStartError(payload.lock?.message ?? payload.error ?? `API ${response.status}`);
        if (payload.run) setRunStatus(payload.run);
        return;
      }
      setStartStatus(payload.status ?? 'running');
      setStartError(payload.message ?? null);
      if (payload.run) setRunStatus(payload.run);
    } catch (err) {
      setStartStatus('failed');
      setStartError(err.message);
    }
  };

  return (
    <div className="flex flex-col gap-4 pb-3">
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1.4fr_0.9fr]">
        <Card className="p-5">
          <div className="flex flex-col gap-5 lg:flex-row lg:items-center lg:justify-between">
            <div>
              <div className="inline-flex items-center gap-2 rounded-full border border-emerald-400/20 bg-emerald-400/10 px-3 py-1 text-xs font-bold text-emerald-300">
                <Activity size={14} /> Production Control
              </div>
              <h2 className="mt-4 text-3xl font-black tracking-tight text-[var(--dash-text)]">CK3 PT-BR Production Control</h2>
              <p className="mt-2 max-w-3xl text-sm text-[var(--dash-muted)]">
                Portal local para acompanhar source, output, gate composto e release do mod. Execucao real permanece protegida por backend e allowlist.
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <button onClick={() => openDashboardTab('Managerial')} className="inline-flex h-10 items-center gap-2 rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] px-4 text-sm font-bold text-[var(--dash-text)] hover:bg-blue-500/10">
                <ExternalLink size={16} /> Managerial
              </button>
              <button onClick={() => openDashboardTab('Operational')} className="inline-flex h-10 items-center gap-2 rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] px-4 text-sm font-bold text-[var(--dash-text)] hover:bg-blue-500/10">
                <ExternalLink size={16} /> Operational
              </button>
              <button onClick={() => openDashboardTab('Neural Network')} className="inline-flex h-10 items-center gap-2 rounded-xl border border-violet-400/30 bg-violet-500/10 px-4 text-sm font-bold text-violet-100 hover:bg-violet-500/20">
                <BrainCircuit size={16} /> Neural Atlas
              </button>
            </div>
          </div>

          <div className="mt-6 grid grid-cols-1 gap-3 md:grid-cols-4">
            <MetricTile title="Readiness" value={statusLabel(readiness.status)} tone={statusTone(readiness.status)} />
            <MetricTile title="Closed" value={pct(readiness.closed_pct)} tone="emerald" />
            <MetricTile title="Acionavel" value={compact(readiness.actionable_pending ?? readiness.pending_operational)} tone="amber" />
            <MetricTile title="ML Watch" value={compact(readiness.model_suspicion_watch)} tone="blue" />
            <MetricTile title="Needs Apply" value={compact(summary.needs_apply)} tone={summary.needs_apply ? 'amber' : 'emerald'} />
            <MetricTile title="Select_CString" value={`${fmt(selectCString.closed ?? 0)}/${fmt(selectCString.total ?? 0)}`} tone={(selectCString.pending ?? 0) ? 'amber' : 'emerald'} />
          </div>
        </Card>

        <Card className="p-5">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h3 className="text-sm font-bold text-[var(--dash-text)]">Production Gate</h3>
              <p className="mt-1 text-xs text-[var(--dash-muted)]">O chat de aprendizado libera a producao antes do start real.</p>
            </div>
            <Badge tone={canStart ? 'emerald' : 'red'}>{canStart ? 'Liberado' : 'Bloqueado'}</Badge>
          </div>
          <div className="mt-5 rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-4">
            <p className="text-sm font-semibold text-[var(--dash-text)]">{learning.gate_message ?? lock.message ?? 'Producao liberada'}</p>
            <p className="mt-1 text-xs text-[var(--dash-muted)]">Status: {learning.status ?? 'idle'} · {learning.current_phase_label || 'sem ciclo ativo'}</p>
            <div className="mt-3 h-2 overflow-hidden rounded-full bg-slate-500/20">
              <div className={cn('h-full rounded-full', canStart ? 'bg-emerald-500' : 'bg-amber-500')} style={{ width: `${Number(learning.progress_pct ?? (canStart ? 100 : 0))}%` }} />
            </div>
            {learning.next_action && <p className="mt-2 text-xs text-[var(--dash-muted)]">{learning.next_action}</p>}
          </div>
          <button
            onClick={startProduction}
            disabled={!canStart || startStatus === 'checking' || runActive}
            className={cn(
              'mt-5 inline-flex h-12 w-full items-center justify-center gap-2 rounded-xl px-4 text-sm font-black transition',
              canStart && !runActive ? 'bg-blue-600 text-white shadow-lg shadow-blue-600/20 hover:bg-blue-500' : 'bg-red-500/15 text-red-300'
            )}
          >
            <Play size={18} /> {runActive ? 'Production Run Running...' : startStatus === 'checking' ? 'Checking...' : 'Start Production Run'}
          </button>
          {(startStatus || startError) && (
            <div className="mt-3 rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-3 text-xs text-[var(--dash-muted)]">
              <span className="font-bold text-[var(--dash-text)]">{statusLabel(visibleStartStatus)}</span>
              {startError && <span> - {startError}</span>}
              {runStatus?.message && !startError && <span> - {runStatus.message}</span>}
            </div>
          )}
        </Card>
      </div>

      <ChartCard
        title={hasRun ? 'Execucao Atual' : 'Fluxo de Producao'}
        subtitle={hasRun ? 'Log e relatorio do executor seguro de producao.' : 'Mapa limpo das etapas que serao executadas ao iniciar uma run.'}
      >
        <div className={cn('grid gap-3', hasRun && 'lg:grid-cols-[0.8fr_1.2fr]')}>
          {hasRun && (
            <div className="space-y-3">
              <MetricTile title="Run" value={runStatus.run_id} tone="blue" />
              <MetricTile title="Status" value={statusLabel(runStatus.status)} tone={statusTone(runStatus.status)} />
              <MetricTile title="Modo" value={runStatus.mode ?? 'safe'} tone="emerald" />
              <MetricTile title="Etapa Atual" value={currentRunStage?.label ?? runStatus.current_stage ?? '-'} tone={runActive ? 'blue' : statusTone(runStatus.status)} />
              <MetricTile title="Progresso" value={`${runProgress}%`} tone={runActive ? 'blue' : statusTone(runStatus.status)} />
              <MetricTile title="Relatorio" value={runStatus.report_path ? 'gerado' : 'pendente'} tone={runStatus.report_path ? 'emerald' : 'amber'} />
            </div>
          )}
          <div className="rounded-xl border border-[var(--dash-border)] bg-slate-950/60 p-4">
            {hasRun && (
              <>
              <div className="mb-3 flex items-center justify-between gap-3">
                <p className="text-sm font-bold text-white">{runStatus.message ?? 'Aguardando...'}</p>
                <Badge tone={statusTone(runStatus.status)}>{statusLabel(runStatus.status)}</Badge>
              </div>
              <div className="mb-4 h-2 overflow-hidden rounded-full bg-slate-500/20">
                <div className={cn('h-full rounded-full', runStatus.status === 'failed' ? 'bg-red-500' : 'bg-blue-500')} style={{ width: `${runProgress}%` }} />
              </div>
              </>
            )}
            <div className="mb-3 flex flex-wrap items-center gap-2 text-xs text-slate-400">
              <span className="inline-flex items-center gap-1 rounded-full border border-violet-400/30 bg-violet-500/10 px-2 py-1 font-bold text-violet-200">
                <BrainCircuit size={13} /> ML / politica
              </span>
              <span>etapas que ganham precisao com memoria, especialistas, gates e aprendizado.</span>
            </div>
            <div className="mb-4 grid gap-3 xl:grid-cols-2">
              {displayPhases.map((phase, phaseIndex) => (
                <div key={phase.id} className="rounded-xl border border-white/10 bg-white/[0.03] p-3">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <p className="text-[10px] font-bold uppercase tracking-wide text-slate-400">Fase {phaseIndex + 1}/4</p>
                      <h4 className="mt-1 text-sm font-black text-white">{phase.title}</h4>
                      <p className="mt-1 text-xs text-slate-400">{phase.purpose}</p>
                    </div>
                    <Badge tone={statusTone(phase.status)}>{statusLabel(phase.status)}</Badge>
                  </div>
                  <div className="mt-3 h-2 overflow-hidden rounded-full bg-slate-500/20">
                    <div className={cn('h-full rounded-full', phase.status === 'failed' ? 'bg-red-500' : phase.status === 'running' ? 'bg-blue-500' : 'bg-emerald-500')} style={{ width: `${phase.progress}%` }} />
                  </div>
                  <p className="mt-2 text-[11px] text-slate-400">
                    {phase.done}/{phase.total} subetapas · atual: <span className="font-bold text-slate-200">{phase.currentStage?.label ?? '-'}</span>
                  </p>
                  <div className="mt-3 grid gap-2 md:grid-cols-2">
                    {phase.stages.map((stage) => {
                      const neuralDetail = neuralProductionStages[stage.id];
                      const stageDetail = productionStageDetails[stage.id];
                      return (
                        <div key={stage.id} className={cn(
                          'rounded-lg border p-3',
                          stage.status === 'running'
                            ? 'border-blue-400/50 bg-blue-500/10'
                            : stage.status === 'failed'
                              ? 'border-red-400/40 bg-red-500/10'
                              : neuralDetail
                                ? 'border-violet-400/40 bg-violet-500/[0.08] ring-1 ring-violet-400/20'
                                : stage.status === 'done'
                                  ? 'border-emerald-400/30 bg-emerald-500/10'
                                  : 'border-white/10 bg-slate-950/30'
                        )}>
                          <div className="flex items-center justify-between gap-2">
                            <div className="flex min-w-0 items-center gap-2">
                              {neuralDetail && (
                                <span
                                  title={neuralDetail}
                                  className="grid h-6 w-6 shrink-0 place-items-center rounded-lg border border-violet-400/30 bg-violet-500/15 text-violet-200"
                                >
                                  <BrainCircuit size={14} />
                                </span>
                              )}
                              <p className="truncate text-xs font-bold text-white">{stage.label}</p>
                            </div>
                            <Badge tone={statusTone(stage.status)}>{statusLabel(stage.status)}</Badge>
                          </div>
                          <div className="mt-1 flex items-center gap-2">
                            <p className="truncate text-[11px] text-slate-400">{stage.id}</p>
                            {neuralDetail && <span className="rounded-full bg-violet-500/10 px-2 py-0.5 text-[10px] font-bold text-violet-200">ML</span>}
                          </div>
                          {stageDetail && (
                            <p className={cn(
                              'mt-2 line-clamp-2 text-[10px] leading-snug',
                              neuralDetail ? 'text-violet-100/80' : 'text-slate-300/80'
                            )}>
                              {stageDetail}
                            </p>
                          )}
                          {hasRun && stage.metrics && (
                            <div className="mt-2 flex flex-wrap gap-1 text-[10px] font-semibold text-slate-300">
                              {Object.entries(stage.metrics).map(([key, value]) => (
                                <span key={key} className="rounded-md bg-slate-900/60 px-1.5 py-0.5">{key}: {fmt(value)}</span>
                              ))}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
            {hasRun && (
              <>
              <div className="mb-4 grid gap-2 text-xs text-slate-400 md:grid-cols-2">
                {runStatus.snapshot_path && <p className="truncate"><span className="font-bold text-slate-200">Snapshot:</span> {runStatus.snapshot_path}</p>}
                {runStatus.snapshot_archive_path && <p className="truncate"><span className="font-bold text-slate-200">Snapshot ZIP:</span> {runStatus.snapshot_archive_path}</p>}
                {runStatus.log_path && <p className="truncate"><span className="font-bold text-slate-200">Log:</span> {runStatus.log_path}</p>}
                {runStatus.report_path && <p className="truncate"><span className="font-bold text-slate-200">Relatorio:</span> {runStatus.report_path}</p>}
                <p><span className="font-bold text-slate-200">Output write:</span> {runStatus.apply_output ? 'enabled' : 'disabled'}</p>
              </div>
              <div className="max-h-[260px] overflow-auto font-mono text-xs leading-relaxed text-slate-300">
                {(runStatus.logs_tail ?? []).length ? (
                  (runStatus.logs_tail ?? []).slice(-45).map((line, index) => <p key={`${index}-${line.slice(0, 12)}`}>{line}</p>)
                ) : (
                  <p>Sem logs ainda.</p>
                )}
              </div>
              </>
            )}
          </div>
        </div>
      </ChartCard>
    </div>
  );
}

function ProductionControlCompact({ data, onRefreshAppState }) {
  const appState = data.appState ?? {};
  const release = appState.release ?? {};
  const operationalClosure = release.operational_closure ?? {};
  const qualityDebt = release.quality_debt ?? {};
  const providerHealth = release.promotion_provider_health ?? {};
  const operationallyClosed = operationalClosure.is_closed === true
    || (!operationalClosure.instrumented && !Number(release.pending_count ?? 0) && !Number(release.needs_apply ?? 0));
  const qualityDebtActionable = qualityDebt.has_actionable_debt === true;
  const qualityDebtHeaderLabel = qualityDebt.status === 'clear'
    ? 'sem dívida ativa'
    : qualityDebt.status === 'monitoring'
      ? 'dívida em observação'
      : qualityDebtActionable
        ? `${compact(qualityDebt.actionable_signal_count ?? 0)} sinais de qualidade`
        : 'dívida não medida';
  const cache = appState.cache ?? {};
  const learning = appState.learning_gate ?? {};
  const productionState = appState.production ?? {};
  const lastRun = productionState.last_run ?? {};
  const productionDelta = release.since_last_production ?? {};
  const integrity = release.operational_integrity ?? {};
  const postRelease = release.post_release ?? release.feedback ?? {};
  const feedbackSummary = postRelease.summary ?? {};
  const safety = release.safety ?? {};
  const evaluationGate = release.evaluation_gate ?? safety.evaluation_gate ?? {};
  const publicationGate = release.publication_gate ?? safety.publication_gate ?? {};
  const baselineControl = release.baseline_control ?? safety.baseline_control ?? {};
  const visualLocks = release.visual_locks ?? safety.visual_locks ?? {};
  const releaseCandidate = release.release_candidate ?? safety.release_candidate ?? {};
  const gameUpdate = release.game_update ?? release.source_output_update ?? safety.source_output_update ?? {};
  const compactStages = productionState.stages_compact ?? [];
  const initialDiskPreflight = productionState.disk_preflight ?? release.disk_preflight ?? safety.disk_preflight ?? null;
  const [startStatus, setStartStatus] = useState(null);
  const [startError, setStartError] = useState(null);
  const [runStatus, setRunStatus] = useState(lastRun?.run_id ? lastRun : null);
  const [refreshing, setRefreshing] = useState(false);
  const [selectedMode, setSelectedMode] = useState('diagnostic');
  const [postReleaseView, setPostReleaseView] = useState('apply');
  const [calibrationSubmittingItemId, setCalibrationSubmittingItemId] = useState(null);
  const [lastDiagnostic, setLastDiagnostic] = useState(null);
  const [actionNotice, setActionNotice] = useState(null);
  const [diskPreflight, setDiskPreflight] = useState(initialDiskPreflight);
  const [clockTick, setClockTick] = useState(0);
  const [diagnosticStartedAt, setDiagnosticStartedAt] = useState(null);
  const [diagnosticFinishedAt, setDiagnosticFinishedAt] = useState(null);
  const [diagnosticRunStatus, setDiagnosticRunStatus] = useState(null);
  const terminalRefreshRef = useRef('');
  const runStartBaselineIdRef = useRef(lastRun?.run_id ?? null);
  const reviewTabsRef = useRef(null);
  const runStatusValue = normalizedRunStatus(runStatus?.status);
  const actionNoticeStatus = normalizedRunStatus(actionNotice?.status);
  const startStatusValue = normalizedRunStatus(startStatus);
  const runActive = ['starting', 'running'].includes(runStatusValue);
  const activeRunMode = runStatus?.run_mode === 'publication' || runStatus?.mode === 'publication_apply_confirmed'
    ? 'publication'
    : 'evaluation';
  const pendingActionMode = actionNotice?.mode === 'publication' ? 'publication' : 'evaluation';
  const visualRunMode = runActive ? activeRunMode : pendingActionMode;
  const activeStageBlueprint = visualRunMode === 'publication' ? publicationStageBlueprint : productionStageBlueprint;
  const evaluationStartPending = actionNotice?.mode === 'evaluation'
    && !runActive
    && !actionNotice?.runId
    && ['checking', 'starting', 'running'].includes(actionNoticeStatus || startStatusValue);
  const publicationStartPending = actionNotice?.mode === 'publication'
    && !runActive
    && !actionNotice?.runId
    && ['checking', 'starting', 'running'].includes(actionNoticeStatus || startStatusValue);
  const runStartPending = evaluationStartPending || publicationStartPending;
  const hasLiveRunStages = Array.isArray(runStatus?.stages) && runStatus.stages.length > 0;
  const normalizedLiveStages = hasLiveRunStages
    ? runStatus.stages.map((stage) => ({ ...stage, status: normalizedStageStatus(stage.status ?? stage.state) }))
    : [];
  const liveStagesHaveActivity = normalizedLiveStages.some((stage) => ['running', 'done', 'failed', 'cancelled'].includes(stage.status));
  const currentStageIndex = !runStartPending && runStatus?.current_stage
    ? activeStageBlueprint.findIndex((stage) => stage.id === runStatus.current_stage)
    : -1;
  const fallbackRunningStageIndex = currentStageIndex >= 0 ? currentStageIndex : 0;
  const liveStagesForDisplay = normalizedLiveStages.length && runActive && !liveStagesHaveActivity
    ? normalizedLiveStages.map((stage, index) => ({
        ...stage,
        status: index < fallbackRunningStageIndex ? 'done' : index === fallbackRunningStageIndex ? 'running' : 'pending',
        progress_pct: index === fallbackRunningStageIndex ? (stage.progress_pct ?? 8) : stage.progress_pct,
      }))
    : normalizedLiveStages;
  const evaluationVisualActive = runActive || runStartPending;
  const activeBlueprintStages = evaluationVisualActive
    ? activeStageBlueprint.map((stage, index) => ({
        ...stage,
        status: currentStageIndex < 0 ? (index === 0 ? 'running' : 'pending') : index < currentStageIndex ? 'done' : index === currentStageIndex ? 'running' : 'pending',
      }))
    : [];
  const runStages = !runStartPending && liveStagesForDisplay.length
    ? liveStagesForDisplay
    : evaluationVisualActive
      ? activeBlueprintStages
      : compactStages.map((stage) => ({ ...stage, status: normalizedStageStatus(stage.status ?? stage.state) }));
  const displayPhases = buildProductionPhases(runStages?.length ? runStages : activeStageBlueprint, visualRunMode);
  const canStart = Boolean(learning.can_start_production) && !runActive && !runStartPending;
  const doneStages = (runStages ?? []).filter((stage) => stage.status === 'done').length;
  const currentStage = runStartPending
    ? (runStages ?? []).find((stage) => stage.status === 'running')
    : (runStages ?? []).find((stage) => stage.id === runStatus?.current_stage) ?? (runStages ?? []).find((stage) => stage.status === 'running');
  const runningStageContribution = evaluationVisualActive && currentStage
    ? clampNumber(Number(currentStage.progress_pct ?? currentStage.metrics?.progress_pct ?? 35), 8, 90) / 100
    : 0;
  const runProgress = (runStages ?? []).length ? Math.round(((doneStages + runningStageContribution) / runStages.length) * 100) : runActive ? 0 : Number(productionState.progress_pct ?? 0);
  const runTerminal = ['completed', 'failed', 'cancelled'].includes(runStatusValue);
  const outputRestoreStatus = gameUpdate.output_restore_status ?? safety.source_output_update?.output_restore_status ?? {};
  const evaluationAllowed = evaluationGate.can_start_evaluation_full_production_now === true;
  const publicationAllowed = publicationGate.can_publish_after_full_production_now === true;
  const diskBlocksProduction = diskPreflight?.ok === false;
  const evaluationReasons = Array.isArray(evaluationGate.blocking_reasons) ? evaluationGate.blocking_reasons : [];
  const publicationReasons = Array.isArray(publicationGate.blocking_reasons) ? publicationGate.blocking_reasons : [];
  const qualityEpoch = release.quality_epoch ?? {};
  const evaluatedDiffSummary = postRelease.diff_review?.summary && typeof postRelease.diff_review.summary === 'object'
    ? postRelease.diff_review.summary
    : {};
  const evaluatedApplyCount = Number(evaluatedDiffSummary.needs_apply ?? 0);
  const rawScoreRegressionCount = Number(evaluatedDiffSummary.raw_score_regressions ?? evaluatedDiffSummary.score_regressions ?? 0);
  const effectiveScoreRegressionCount = Number(
    evaluatedDiffSummary.effective_score_regressions
      ?? evaluatedDiffSummary.effective_package_score_regressions
      ?? 0
  );
  const reviewedRawScoreRegressionCount = Number(evaluatedDiffSummary.reviewed_raw_score_regressions ?? 0);
  const unresolvedRawScoreRegressionCount = Number(
    evaluatedDiffSummary.unresolved_raw_score_regressions
      ?? Math.max(0, rawScoreRegressionCount - reviewedRawScoreRegressionCount)
  );
  const hasVerifiedApplyQueue = evaluatedApplyCount > 0;
  const publicationActionAllowed = publicationAllowed && hasVerifiedApplyQueue;
  const publicationModeStatus = publicationActionAllowed
    ? 'liberada'
    : publicationAllowed
      ? 'sem apply'
      : 'bloqueada';
  const publicationModeTone = publicationActionAllowed ? 'emerald' : publicationAllowed ? 'blue' : 'red';
  const publicationModeStatusKind = publicationActionAllowed ? 'open' : 'blocked';
  const publicationModeWarning = publicationActionAllowed
    ? `${compact(evaluatedApplyCount)} apply verificado pela avaliacao.`
    : publicationAllowed
      ? 'Bloqueada para execucao: avaliacao nao tem apply verificado para aplicar.'
      : 'Bloqueada pelos gates de publicacao.';
  const packageDeltaKeys = ['raw_output_diff_count', 'changed_vs_old', 'package_diff_count'];
  const packageDeltaMeasured = packageDeltaKeys.some((key) => Object.prototype.hasOwnProperty.call(evaluatedDiffSummary, key));
  const packageChangeCount = Number(
    evaluatedDiffSummary.raw_output_diff_count
      ?? evaluatedDiffSummary.changed_vs_old
      ?? evaluatedDiffSummary.package_diff_count
      ?? 0
  );
  const materializedPackageVersions = (Array.isArray(release.package_versions) ? release.package_versions : [])
    .filter((version) => version.status === 'materialized');
  const latestMaterializedPackageVersion = materializedPackageVersions.reduce(
    (latest, version) => Number(version.version_number ?? 0) > Number(latest?.version_number ?? 0) ? version : latest,
    null
  );
  const latestMaterializedVersionLabel = latestMaterializedPackageVersion?.version_number
    ? `V${latestMaterializedPackageVersion.version_number}`
    : 'A versão materializada atual';
  const versionBaseGatesReady = ['scored', 'evaluated', 'published'].includes(String(qualityEpoch.status ?? ''))
    && Number(release.pending_count ?? 0) === 0
    && Number(release.needs_apply ?? 0) === 0
    && effectiveScoreRegressionCount === 0;
  const versionHasPackageDelta = !packageDeltaMeasured || packageChangeCount > 0;
  const versionNoPackageDelta = versionBaseGatesReady && packageDeltaMeasured && packageChangeCount === 0;
  const versionMaterializationEligible = versionBaseGatesReady && versionHasPackageDelta;
  const releaseModes = [
    {
      id: 'diagnostic',
      label: 'Diagnóstico',
      shortLabel: 'Diagnóstico',
      status: 'liberado',
      tone: 'blue',
      color: 'blue',
      statusKind: 'open',
      button: 'Atualizar diagnóstico',
      actionLabel: 'Atualizar',
      description: 'Analisa o estado, gera evidências e sugere promoções. Não confirma apply nem altera output.',
      warning: 'Descoberta segura: propostas continuam sujeitas à Avaliação.',
    },
    {
      id: 'evaluation',
      label: 'Produção de avaliação',
      shortLabel: 'Avaliação',
      status: evaluationAllowed && !diskBlocksProduction ? 'liberada' : 'bloqueada',
      tone: evaluationAllowed && !diskBlocksProduction ? 'emerald' : 'red',
      color: 'emerald',
      statusKind: evaluationAllowed && !diskBlocksProduction ? 'open' : 'blocked',
      button: 'Rodar produção de avaliação',
      actionLabel: 'Rodar',
      description: 'Reavalia scores, valida promoções e aprova somente as seguras para Apply.',
      warning: diskBlocksProduction ? 'Bloqueada por espaço em disco insuficiente.' : 'Não escreve output. Produz a fila de Apply verificada.',
    },
    {
      id: 'publication',
      label: 'Produção publicável',
      shortLabel: 'Publicável',
      status: publicationModeStatus,
      tone: publicationModeTone,
      color: 'violet',
      statusKind: publicationModeStatusKind,
      button: 'Aplicar confirmados',
      actionLabel: 'Aplicar',
      description: 'Aplica a fila verificada, escreve output e exige fechamento completo no lifecycle.',
      warning: publicationModeWarning,
    },
    {
      id: 'hotfix',
      label: 'Hotfix visual',
      shortLabel: 'Hotfix',
      status: releaseCandidate.current_candidate_path ? 'instrumentado' : 'não instrumentado',
      tone: releaseCandidate.current_candidate_path ? 'amber' : 'slate',
      color: 'amber',
      statusKind: releaseCandidate.current_candidate_path ? 'instrumented' : 'unknown',
      button: releaseCandidate.current_candidate_path ? 'Abrir fila de hotfix' : 'Gerar pacote de hotfix visual',
      actionLabel: releaseCandidate.current_candidate_path ? 'Abrir' : 'Preparar',
      description: 'Opera sobre release candidate pequeno baseado em feedback visual.',
      warning: 'Não substitui uma produção full ampla.',
    },
    {
      id: 'version',
      label: 'Materializar nova versão',
      shortLabel: 'Nova versão',
      status: versionNoPackageDelta ? 'sem alterações' : versionMaterializationEligible ? 'liberada' : 'bloqueada',
      tone: versionNoPackageDelta ? 'blue' : versionMaterializationEligible ? 'emerald' : 'red',
      color: 'slate',
      statusKind: versionMaterializationEligible ? 'open' : 'blocked',
      button: 'Materializar nova versão',
      actionLabel: 'Materializar',
      description: 'Congela a versão no banco e promove output/spanish para source/spanish_old.',
      warning: versionNoPackageDelta
        ? `${latestMaterializedVersionLabel} já representa integralmente o output atual. Uma nova versão exige alterações reais no pacote.`
        : versionMaterializationEligible
        ? `Cria backup da baseline atual, verifica hashes e reindexa o banco.${rawScoreRegressionCount > 0 ? ` ${compact(rawScoreRegressionCount)} observações brutas já foram resolvidas pela calibração.` : ''}`
        : `Exige epoch pontuada, zero regressões efetivas, zero pendências e zero apply. Epoch atual: ${qualityEpoch.status ?? 'ausente'}.`,
    },
  ];
  const effectiveSelectedMode = runActive ? activeRunMode : selectedMode;
  const selectedModeInfo = releaseModes.find((mode) => mode.id === effectiveSelectedMode) ?? releaseModes[0];
  const modeActionNotice = actionNotice?.mode === effectiveSelectedMode ? actionNotice : null;
  const latestPublicationPreflight = modeActionNotice?.publicationPreflight
    ?? productionState.publication_preflight
    ?? lastRun?.publication_preflight
    ?? null;
  const publicationCanApply = effectiveSelectedMode === 'publication'
    && !runActive
    && hasVerifiedApplyQueue
    && latestPublicationPreflight?.can_apply_pending === true;
  const actionRunId = modeActionNotice?.runId ?? null;
  const actionRunMatches = Boolean(actionRunId && runStatus?.run_id === actionRunId);
  const actionWaitingForRun = (modeActionNotice?.mode === 'evaluation' || modeActionNotice?.mode === 'publication')
    && !actionRunId
    && ['starting', 'running'].includes(actionNoticeStatus);
  const actionRunStatus = actionRunMatches || (runActive && !actionRunId) ? runStatus : null;
  const evaluationExecutionVisible = !refreshing && (
    actionWaitingForRun
    || Boolean(runActive && (actionRunMatches || !actionRunId))
    || Boolean(
      (modeActionNotice?.mode === 'evaluation' || modeActionNotice?.mode === 'publication')
      && ['failed', 'blocked'].includes(actionNoticeStatus)
      && actionRunMatches
    )
  );

  useEffect(() => {
    setRunStatus(lastRun?.run_id ? lastRun : null);
  }, [lastRun?.run_id, lastRun?.status]);

  useEffect(() => {
    if (initialDiskPreflight) setDiskPreflight(initialDiskPreflight);
  }, [initialDiskPreflight?.checked_at, initialDiskPreflight?.status]);

  useEffect(() => {
    if (runActive && selectedMode !== activeRunMode) {
      setSelectedMode(activeRunMode);
    }
  }, [activeRunMode, runActive, selectedMode]);

  const refreshDiskPreflight = async () => {
    try {
      const response = await fetch(`${API_BASE}/production/preflight/disk`);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error ?? `API ${response.status}`);
      const next = payload.disk_preflight ?? payload;
      setDiskPreflight(next);
      return next;
    } catch (err) {
      setDiskPreflight((current) => current ?? { ok: null, status: 'not_measured', message: err.message });
      return null;
    }
  };

  useEffect(() => {
    refreshDiskPreflight();
  }, []);

  useEffect(() => {
    let active = true;
    const loadLatestRun = async () => {
      try {
        const response = await fetch(`${API_BASE}/production/runs/latest`);
        if (!response.ok) return;
        const payload = await response.json();
        const latestRun = payload.run ?? null;
        const baselineRunId = runStartBaselineIdRef.current;
        if (runStartPending && baselineRunId && latestRun?.run_id === baselineRunId) return;
        if (active && latestRun?.run_id) setRunStatus(latestRun);
      } catch {
        // Keep cached run visible; the next refresh can recover.
      }
    };
    loadLatestRun();
    const timer = setInterval(loadLatestRun, runActive || runStartPending ? 4000 : 15000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [runActive, runStartPending]);

  useEffect(() => {
    if (!runActive || !runStatus?.run_id) return;
    setStartStatus(runStatus.status);
    setActionNotice((current) => {
      if (
        current?.mode === activeRunMode
        && current?.runId === runStatus.run_id
        && current?.status === runStatus.status
        && current?.currentStage === runStatus.current_stage
      ) return current;
      return {
        ...(current ?? {}),
        mode: activeRunMode,
        status: runStatus.status,
        tone: activeRunMode === 'publication' ? 'violet' : 'blue',
        title: activeRunMode === 'publication'
          ? 'Produção publicável em execução'
          : 'Produção de avaliação em execução',
        body: `Run ${runStatus.run_id} · etapa ${runStatus.current_label ?? runStatus.current_stage ?? 'preparando'}.`,
        outputChanged: Boolean(runStatus.output_changed),
        runId: runStatus.run_id,
        currentStage: runStatus.current_stage,
      };
    });
  }, [activeRunMode, runActive, runStatus?.run_id, runStatus?.status, runStatus?.current_stage]);

  useEffect(() => {
    if (!runActive && !runStartPending && !refreshing) return undefined;
    const timer = setInterval(() => setClockTick((tick) => tick + 1), 1000);
    return () => clearInterval(timer);
  }, [runActive, runStartPending, refreshing]);

  useEffect(() => {
    if (!refreshing) return undefined;
    let active = true;
    const loadDiagnosticStatus = async () => {
      try {
        const response = await fetch(`${API_BASE}/diagnostic/status`);
        if (!response.ok) return;
        const payload = await response.json();
        const candidate = payload.diagnostic;
        const candidateStartedAt = timestampMs(candidate?.started_at);
        const belongsToCurrentAttempt = candidate
          && candidateStartedAt !== null
          && (!diagnosticStartedAt || candidateStartedAt >= diagnosticStartedAt - 5000);
        if (active && belongsToCurrentAttempt) setDiagnosticRunStatus(candidate);
      } catch {
        // O POST principal continua sendo a fonte terminal; o próximo polling recupera o status.
      }
    };
    loadDiagnosticStatus();
    const timer = setInterval(loadDiagnosticStatus, 1500);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [refreshing, diagnosticStartedAt]);

  useEffect(() => {
    if (!runStatus?.run_id || !['completed', 'failed'].includes(runStatus.status)) return;
    const key = `${runStatus.run_id}:${runStatus.status}`;
    if (terminalRefreshRef.current === key) return;
    terminalRefreshRef.current = key;
    const refreshTerminal = async () => {
      try {
        const response = await fetch(`${API_BASE}/production/runs/latest`);
        if (response.ok) {
          const payload = await response.json();
          const latestRun = payload.run ?? null;
          if (latestRun?.run_id) {
            setRunStatus(latestRun);
            if (latestRun.status === 'failed' || latestRun.status === 'completed') {
              const latestMode = latestRun.run_mode === 'publication' || latestRun.mode === 'publication_apply_confirmed'
                ? 'publication'
                : 'evaluation';
              setActionNotice((current) => ({
                ...(current ?? {}),
                mode: latestMode,
                status: latestRun.status,
                tone: latestRun.status === 'failed' ? 'red' : 'emerald',
                outputChanged: Boolean(latestRun.output_changed),
                title: latestRun.status === 'failed'
                  ? 'Execucao falhou'
                  : latestMode === 'publication'
                    ? 'Producao publicavel concluida'
                    : 'Producao de avaliacao concluida',
                body: latestRun.message ?? (latestRun.status === 'failed' ? 'A producao terminou com falha.' : 'Run completed.'),
                runId: latestRun.run_id,
                diskPreflight: latestRun.disk_preflight ?? latestRun.failure?.disk_preflight ?? current?.diskPreflight,
                publicationPreflight: latestRun.publication_preflight ?? current?.publicationPreflight,
              }));
            }
          }
        }
      } catch {
        // A run terminal continua visivel mesmo se o refresh pontual falhar.
      }
      try {
        await onRefreshAppState?.();
      } catch {
        // O polling/cache global recupera depois.
      }
      if (runStatus.status === 'completed' || runStatus.status === 'failed') {
        setStartStatus(runStatus.status);
        refreshDiskPreflight();
      }
    };
    refreshTerminal();
  }, [runStatus?.run_id, runStatus?.status, onRefreshAppState]);

  const refreshCache = async () => {
    setRefreshing(true);
    setStartError(null);
    setStartStatus('refreshing');
    setDiagnosticStartedAt(Date.now());
    setDiagnosticFinishedAt(null);
    setDiagnosticRunStatus({
      status: 'starting',
      progress_pct: 1,
      current_label: 'Preparar diagnóstico',
      phases: [
        { id: 'state', label: 'Estado atual', status: 'running' },
        { id: 'discovery', label: 'Descoberta', status: 'pending' },
        { id: 'suggestions', label: 'Sugestões', status: 'pending' },
        { id: 'consolidation', label: 'Consolidação', status: 'pending' },
      ],
    });
    try {
      const response = await fetch(`${API_BASE}/cache/refresh`, { method: 'POST' });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error ?? `API ${response.status}`);
      const completedAt = new Date().toLocaleString('pt-BR', { hour12: false });
      if (payload.diagnostic_status) setDiagnosticRunStatus(payload.diagnostic_status);
      const diagnostic = {
        completedAt,
        cacheUpdated: true,
        outputChanged: false,
        productionRunStarted: false,
        cacheGeneratedAt: payload.cache?.generated_at ?? payload.generated_at ?? completedAt,
        segmentStateRunId: payload.diagnostic_segment_state?.new_segment_state_run_id ?? null,
        stages: payload.diagnostic_segment_state?.stages ?? [],
      };
      setLastDiagnostic(diagnostic);
      setStartStatus('refresh_completed');
      setActionNotice({
        mode: 'diagnostic',
        status: 'completed',
        tone: 'emerald',
        title: 'Diagnóstico atualizado',
        body: `Análise concluída às ${completedAt}. Segment-state ${payload.diagnostic_segment_state?.new_segment_state_run_id ? `#${payload.diagnostic_segment_state.new_segment_state_run_id}` : 'recalculado'}, evidências e sugestões atualizadas, sem confirmar candidatos nem alterar output.`,
        outputChanged: false,
        runId: null,
      });
      setDiagnosticFinishedAt(Date.now());
      await onRefreshAppState?.(payload.app_state);
      const consolidatedDiskPreflight = payload.app_state?.production?.disk_preflight
        ?? payload.app_state?.release?.disk_preflight
        ?? payload.app_state?.release?.safety?.disk_preflight;
      if (consolidatedDiskPreflight) setDiskPreflight(consolidatedDiskPreflight);
      else await refreshDiskPreflight();
    } catch (err) {
      setStartError(err.message);
      setStartStatus('failed');
      setActionNotice({
        mode: 'diagnostic',
        status: 'failed',
        tone: 'red',
        title: 'Falha ao atualizar diagnóstico',
        body: err.message,
        outputChanged: false,
        runId: null,
      });
      setDiagnosticFinishedAt(Date.now());
    } finally {
      setRefreshing(false);
    }
  };

  const submitCalibrationReview = async (itemId, reviewLabel, requireReason = false) => {
    const reviewReason = requireReason
      ? window.prompt('Registre o motivo desta preferencia para a analise de calibracao:')
      : null;
    if (requireReason && !String(reviewReason ?? '').trim()) return;
    setCalibrationSubmittingItemId(itemId);
    setStartError(null);
    try {
      const response = await fetch(`${API_BASE}/production/pairwise-calibration/review`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          item_id: itemId,
          review_label: reviewLabel,
          review_reason: reviewReason,
          reviewer: 'dashboard_human_review',
        }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error ?? `API ${response.status}`);
      setActionNotice({
        mode: 'diagnostic',
        status: 'completed',
        tone: 'emerald',
        title: 'Revisao de calibracao registrada',
        body: `Item #${itemId}: ${reviewLabel}. Nenhum score, apply ou output foi alterado.`,
        outputChanged: false,
        runId: null,
      });
      await onRefreshAppState?.(payload.app_state);
    } catch (err) {
      setStartError(err.message);
    } finally {
      setCalibrationSubmittingItemId(null);
    }
  };

  const startProduction = async () => {
    setStartStatus('checking');
    setStartError(null);
    runStartBaselineIdRef.current = runStatus?.run_id ?? lastRun?.run_id ?? null;
    setRunStatus(null);
    setActionNotice({
      mode: 'evaluation',
      status: 'checking',
      tone: 'blue',
      title: 'Preparando producao de avaliacao',
      body: 'Validando preflight e preparando a primeira fase da nova run.',
      outputChanged: false,
      runId: null,
    });
    const disk = await refreshDiskPreflight();
    if (disk?.ok === false) {
      const message = disk.message ?? 'Insufficient disk space';
      setStartStatus('blocked');
      setStartError(message);
      setActionNotice({
        mode: 'evaluation',
        status: 'blocked',
        tone: 'red',
        title: 'Execução bloqueada por espaço em disco',
        body: message,
        outputChanged: false,
        runId: null,
        diskPreflight: disk,
      });
      return;
    }
    setActionNotice({
      mode: 'evaluation',
      status: 'starting',
      tone: 'blue',
      title: 'Iniciando produção de avaliação',
      body: 'Solicitando novo run em modo evaluation/full production.',
      outputChanged: true,
      runId: null,
    });
    try {
      const response = await fetch(`${API_BASE}/production/start`, { method: 'POST' });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        if (payload.disk_preflight) setDiskPreflight(payload.disk_preflight);
        setStartStatus(payload.status ?? 'blocked');
        setStartError(payload.lock?.message ?? payload.error ?? `API ${response.status}`);
        if (payload.run) setRunStatus(payload.run);
        setActionNotice({
          mode: 'evaluation',
          status: 'blocked',
          tone: 'red',
          title: 'Produção de avaliação bloqueada',
          body: payload.lock?.message ?? payload.error ?? `API ${response.status}`,
          outputChanged: false,
          runId: payload.run?.run_id ?? null,
          diskPreflight: payload.disk_preflight ?? disk,
        });
        return;
      }
      setStartStatus(payload.status ?? 'running');
      setStartError(payload.message ?? null);
      if (payload.run) setRunStatus(payload.run);
      setActionNotice({
        mode: 'evaluation',
        status: payload.run?.status ?? payload.status ?? 'running',
        tone: 'blue',
        title: 'Produção de avaliação iniciada',
        body: `Modo evaluation/full production · status inicial ${payload.run?.status ?? payload.status ?? 'running'} · etapa ${payload.run?.current_stage ?? 'preparando'}.`,
        outputChanged: true,
        runId: payload.run?.run_id ?? null,
      });
    } catch (err) {
      setStartStatus('failed');
      setStartError(err.message);
      setActionNotice({
        mode: 'evaluation',
        status: 'failed',
        tone: 'red',
        title: 'Falha ao iniciar produção',
        body: err.message,
        outputChanged: false,
        runId: null,
      });
    }
  };

  const startPublicationPreflight = async () => {
    setStartStatus('checking');
    setStartError(null);
    setActionNotice({
      mode: 'publication',
      status: 'starting',
      tone: 'violet',
      title: 'Checando candidato publicavel',
      body: 'Validando gates, fila de apply, promocoes e bloqueios antes de qualquer publicacao.',
      outputChanged: false,
      runId: null,
    });
    try {
      const response = await fetch(`${API_BASE}/production/publication/preflight`, { method: 'POST' });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error ?? `API ${response.status}`);
      const preflight = payload.publication_preflight ?? {};
      const counts = preflight.counts ?? {};
      const ready = preflight.status === 'ready' || preflight.can_publish_now === true;
      const applyReady = preflight.can_apply_pending === true;
      const applyCount = Number(counts.needs_apply ?? 0);
      const applyReadyCount = Number(counts.apply_ready ?? 0);
      const applyBlockedCount = Number(counts.apply_blocked ?? 0);
      const applyTokenMismatchCount = Number(counts.apply_token_mismatch ?? 0);
      const promotionCount = Number(counts.promotions ?? 0);
      const regressionCount = Number(counts.score_regressions ?? 0);
      const unhandledCount = Number(counts.unhandled_by_network ?? 0);
      const quality = preflight.quality ?? {};
      const oldPackageScore = scoreLabel(quality.package_old_score);
      const newPackageScore = scoreLabel(quality.package_new_score);
      const packageDelta = scoreDeltaLabel({ score_delta: quality.package_delta });
      const qualityText = quality.package_new_score === null || quality.package_new_score === undefined
        ? 'score do pacote nao medido'
        : `score pacote ${oldPackageScore} -> ${newPackageScore} (${packageDelta})`;
      const body = ready
        ? `Publicavel: sem bloqueios. ${qualityText}. Promocoes ${compact(promotionCount)}, regressoes ${compact(regressionCount)}, pendentes ${compact(unhandledCount)}.`
        : applyReady
          ? `Fila confirmada pronta: aplicar ${compact(applyCount)} ajustes protegidos. ${qualityText}. Promocoes ${compact(promotionCount)}, regressoes ${compact(regressionCount)}, pendentes ${compact(unhandledCount)}.`
          : `Bloqueado: ${qualityText}. Apply ${compact(applyReadyCount)}/${compact(applyCount)} pronto, ${compact(applyBlockedCount)} bloqueado, token mismatch ${compact(applyTokenMismatchCount)}. Promocoes ${compact(promotionCount)}, regressoes ${compact(regressionCount)}, pendentes ${compact(unhandledCount)}. Proximo passo: ${preflight.next_action ?? 'revisar bloqueios'}.`;
      setStartStatus(ready || applyReady ? 'completed' : 'blocked');
      setActionNotice({
        mode: 'publication',
        status: ready || applyReady ? 'completed' : 'blocked',
        tone: ready ? 'emerald' : applyReady ? 'amber' : 'red',
        title: ready ? 'Candidato publicavel liberado' : applyReady ? 'Fila confirmada pronta para apply' : 'Candidato publicavel bloqueado',
        body,
        outputChanged: false,
        runId: null,
        publicationPreflight: preflight,
      });
      await onRefreshAppState?.();
    } catch (err) {
      setStartStatus('failed');
      setStartError(err.message);
      setActionNotice({
        mode: 'publication',
        status: 'failed',
        tone: 'red',
        title: 'Falha no preflight publicavel',
        body: err.message,
        outputChanged: false,
        runId: null,
      });
    }
  };

  const startPublicationApplyConfirmed = async () => {
    setStartStatus('checking');
    setStartError(null);
    runStartBaselineIdRef.current = runStatus?.run_id ?? lastRun?.run_id ?? null;
    setActionNotice({
      mode: 'publication',
      status: 'starting',
      tone: 'violet',
      title: 'Iniciando apply publicavel',
      body: 'Aplicando somente a fila confirmada em needs apply.',
      outputChanged: true,
      runId: null,
      publicationPreflight: modeActionNotice?.publicationPreflight,
    });
    setRunStatus(null);
    try {
      const response = await fetch(`${API_BASE}/production/publication/apply-confirmed/start`, { method: 'POST' });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        setStartStatus(payload.status ?? 'blocked');
        setStartError(payload.lock?.message ?? payload.error ?? `API ${response.status}`);
        if (payload.run) setRunStatus(payload.run);
        setActionNotice({
          mode: 'publication',
          status: payload.status ?? 'blocked',
          tone: 'red',
          title: 'Apply publicavel bloqueado',
          body: payload.lock?.message ?? payload.error ?? `API ${response.status}`,
          outputChanged: false,
          runId: payload.run?.run_id ?? null,
          publicationPreflight: payload.publication_preflight ?? modeActionNotice?.publicationPreflight,
        });
        return;
      }
      setStartStatus(payload.status ?? 'running');
      if (payload.run) setRunStatus(payload.run);
      setActionNotice({
        mode: 'publication',
        status: payload.run?.status ?? payload.status ?? 'running',
        tone: 'violet',
        title: 'Apply publicavel iniciado',
        body: `Fila confirmada em execucao. Status ${payload.run?.status ?? payload.status ?? 'running'}; etapa ${payload.run?.current_stage ?? 'preparando'}.`,
        outputChanged: true,
        runId: payload.run?.run_id ?? null,
        publicationPreflight: payload.publication_preflight ?? modeActionNotice?.publicationPreflight,
      });
    } catch (err) {
      setStartStatus('failed');
      setStartError(err.message);
      setActionNotice({
        mode: 'publication',
        status: 'failed',
        tone: 'red',
        title: 'Falha ao iniciar apply publicavel',
        body: err.message,
        outputChanged: false,
        runId: null,
        publicationPreflight: modeActionNotice?.publicationPreflight,
      });
    }
  };

  const materializeVersion = async () => {
    setStartStatus('checking');
    setStartError(null);
    setActionNotice({
      mode: 'version', status: 'checking', tone: 'blue', title: 'Validando nova versão',
      body: 'Checando epoch, regressões, pendências, apply e integridade dos pacotes.',
      outputChanged: false, runId: null,
    });
    try {
      const preflightResponse = await fetch(`${API_BASE}/version/materialize/preflight`, { method: 'POST' });
      const preflight = await preflightResponse.json().catch(() => ({}));
      if (!preflightResponse.ok) throw new Error(preflight.error ?? `API ${preflightResponse.status}`);
      if (preflight.eligible === false && preflight.reason === 'no_package_delta') {
        const currentVersion = preflight.current_version_number ? `V${preflight.current_version_number}` : latestMaterializedVersionLabel;
        setStartStatus('completed');
        setActionNotice({
          mode: 'version', status: 'completed', tone: 'blue',
          title: 'Nenhuma nova versão necessária',
          body: `${currentVersion} já representa integralmente output/spanish. O próximo checkpoint será liberado somente depois de alterações reais no pacote.`,
          outputChanged: false, runId: null,
        });
        await onRefreshAppState?.();
        return;
      }
      const confirmed = window.confirm(
        `Materializar v${preflight.version_number}?\n\n` +
        `${preflight.changed_count} mudanças serão congeladas no banco.\n` +
        `source/spanish_old será substituído por uma cópia verificada de output/spanish.\n` +
        'A baseline atual será preservada em release_candidates.'
      );
      if (!confirmed) {
        setStartStatus(null);
        setActionNotice(null);
        return;
      }
      setStartStatus('running');
      setActionNotice({
        mode: 'version', status: 'running', tone: 'blue',
        title: `Materializando v${preflight.version_number}`,
        body: 'Congelando banco, copiando baseline, verificando hashes e reindexando fontes.',
        outputChanged: false, runId: null,
      });
      const response = await fetch(`${API_BASE}/version/materialize/start`, { method: 'POST' });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error ?? `API ${response.status}`);
      setStartStatus('completed');
      setActionNotice({
        mode: 'version', status: 'completed', tone: 'emerald',
        title: `Versão v${payload.version_number} materializada`,
        body: `${payload.item_count ?? 0} segmentos congelados. spanish_old sincronizado com output; backup preservado. Rode Diagnóstico para abrir a próxima epoch.`,
        outputChanged: false, runId: null,
      });
      await onRefreshAppState?.(payload.app_state);
    } catch (err) {
      setStartStatus('failed');
      setStartError(err.message);
      setActionNotice({
        mode: 'version', status: 'failed', tone: 'red', title: 'Materialização bloqueada',
        body: err.message, outputChanged: false, runId: null,
      });
    }
  };

  const runSelectedModeAction = () => {
    if (effectiveSelectedMode === 'diagnostic') {
      refreshCache();
      return;
    }
    if (effectiveSelectedMode === 'evaluation') {
      startProduction();
      return;
    }
    if (effectiveSelectedMode === 'publication') {
      if (publicationCanApply) {
        startPublicationApplyConfirmed();
      } else {
        startPublicationPreflight();
      }
      return;
    }
    if (effectiveSelectedMode === 'version') {
      materializeVersion();
    }
  };

  const readinessTone = release.needs_apply ? 'amber' : learning.can_start_production ? 'emerald' : 'red';
  const gateText = learning.can_start_production ? 'Liberado' : 'Bloqueado';
  const lastRunStatus = runStatus?.status ?? 'sem run';
  const cacheTone = cache.stale ? 'amber' : 'emerald';
  const deltaAvailable = Boolean(productionDelta.available);
  const deltaClosed = Number(productionDelta.closed_delta ?? 0);
  const deltaPending = Number(productionDelta.pending_delta ?? 0);
  const deltaNeedsApply = Number(productionDelta.needs_apply_delta ?? 0);
  const nextAction = cache.stale
    ? 'Atualizar cache'
    : release.needs_apply
      ? 'Revisar needs_apply'
      : !learning.can_start_production
        ? 'Aguardar learning gate'
        : deltaAvailable && deltaClosed > 0
          ? 'Seguro para rodar producao'
          : 'Monitorar estado atual';
  const modeButtonDisabled =
    runActive || runStartPending
      ? true
      : effectiveSelectedMode === 'diagnostic'
      ? refreshing
      : effectiveSelectedMode === 'evaluation'
        ? (!evaluationAllowed || diskBlocksProduction || runActive || startStatus === 'checking')
        : effectiveSelectedMode === 'publication'
          ? (refreshing || startStatus === 'checking' || (!publicationActionAllowed && !publicationCanApply))
        : effectiveSelectedMode === 'version'
          ? (!versionMaterializationEligible || refreshing || ['checking', 'running'].includes(startStatus))
        : true;
  const modeButtonClass = (mode, active) => {
    const base = 'group relative flex h-9 min-w-0 flex-1 items-center justify-start overflow-visible rounded-xl border px-3 pr-8 text-white shadow-lg transition hover:-translate-y-0.5 hover:shadow-xl focus:outline-none focus:ring-2 focus:ring-cyan-300/70';
    const palette =
      mode.color === 'emerald' ? 'border-emerald-200/35 bg-emerald-600 shadow-emerald-950/35 hover:bg-emerald-500' :
        mode.color === 'violet' ? 'border-violet-200/35 bg-violet-600 shadow-violet-950/35 hover:bg-violet-500' :
          mode.color === 'amber' ? 'border-amber-200/35 bg-amber-600 shadow-amber-950/35 hover:bg-amber-500' :
            mode.color === 'blue' ? 'border-blue-200/35 bg-blue-600 shadow-blue-950/35 hover:bg-blue-500' :
              'border-slate-200/25 bg-slate-600 shadow-slate-950/25 hover:bg-slate-500';
    return cn(
      base,
      palette,
      active ? 'z-10 -translate-y-0.5 border-cyan-200/90 opacity-100 ring-2 ring-cyan-300/90 brightness-115 shadow-[0_0_18px_rgba(34,211,238,0.42)]' : 'opacity-70 saturate-[0.78] hover:opacity-95'
    );
  };
  const modeIcon = (mode) => {
    const icons = {
      diagnostic: SearchCheck,
      evaluation: Scale,
      publication: Rocket,
      hotfix: ShieldAlert,
      version: Layers3,
    };
    const Icon = icons[mode.id] ?? Workflow;
    return <Icon size={18} strokeWidth={2.2} />;
  };
  const modeStatusIcon = (mode) => {
    if (mode.statusKind === 'blocked') return <Lock size={10} />;
    if (mode.statusKind === 'instrumented') return <ShieldCheck size={10} />;
    if (mode.statusKind === 'unknown') return <FileWarning size={10} />;
    return <Unlock size={10} />;
  };
  const modeTooltip = (mode) => `${mode.label}: ${mode.description} ${mode.warning} Status: ${mode.status}.`;
  // Campos de feedback/hotfix ainda sao opcionais. Quando o backend nao medir,
  // a tela mostra "nao medido" em vez de inferir estado operacional.
  const valueOrPending = (value) => {
    if (value === null || value === undefined || value === '' || value === 'pending_instrumentation') return 'nao medido';
    return value;
  };
  const numberOrNull = (value) => {
    if (value === null || value === undefined || value === '' || value === 'pending_instrumentation') return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  };
  const metricValue = (value) => {
    const parsed = numberOrNull(value);
    return parsed === null ? 'nao medido' : compact(parsed);
  };
  const stageMetrics = (run, stageId) => {
    const stages = Array.isArray(run?.stages) ? run.stages : [];
    const stage = stages.find((item) => item?.id === stageId);
    return stage?.metrics && typeof stage.metrics === 'object' ? stage.metrics : {};
  };
  const runLifecycleApplyCount = (run) => {
    const writeMetrics = stageMetrics(run, 'apply_confirmed_write');
    const dryRunMetrics = stageMetrics(run, 'apply_confirmed_dry_run');
    const preflightMetrics = stageMetrics(run, 'publication_preflight');
    return numberOrNull(
      writeMetrics.released
        ?? writeMetrics.candidates
        ?? dryRunMetrics.released
        ?? preflightMetrics.apply_lifecycle
        ?? preflightMetrics.lifecycle_apply_ready
    ) ?? 0;
  };
  const runOutputWriteCount = (run) => {
    const writeMetrics = stageMetrics(run, 'apply_confirmed_write');
    const preflightMetrics = stageMetrics(run, 'publication_preflight');
    return numberOrNull(
      run?.output_written_count
        ?? writeMetrics.output_written
        ?? writeMetrics.output_write
        ?? preflightMetrics.apply_output_write
    ) ?? 0;
  };
  const runOutputStatusLabel = (run, mayChange = false) => {
    if (mayChange) return 'output pode mudar';
    if (!run?.run_id) return 'output nao medido';
    const lifecycleCount = runLifecycleApplyCount(run);
    const writtenCount = runOutputWriteCount(run);
    if (writtenCount > 0) return `output alterado (${compact(writtenCount)} escritas)`;
    if (lifecycleCount > 0) return `apply ${compact(lifecycleCount)} lifecycle`;
    if (run?.output_changed) return 'output alterado';
    return 'sem escrita no output';
  };
  const toneForCount = (value, zeroTone = 'emerald') => {
    const parsed = numberOrNull(value);
    if (parsed === null) return 'slate';
    return parsed > 0 ? 'amber' : zeroTone;
  };
  const knownOpen = numberOrNull(feedbackSummary.known_open_findings ?? feedbackSummary.known_open ?? postRelease.known_open_findings ?? postRelease.known_open);
  const approvedPendingApply = numberOrNull(feedbackSummary.approved_pending_apply ?? postRelease.approved_pending_apply ?? release.needs_apply) ?? Number(release.needs_apply ?? 0);
  const appliedPendingValidation = numberOrNull(feedbackSummary.applied_pending_validation ?? postRelease.applied_pending_validation);
  const closedFeedback = numberOrNull(feedbackSummary.closed_findings ?? feedbackSummary.closed ?? postRelease.closed_findings ?? postRelease.closed_feedback);
  const feedbackAffected = numberOrNull(feedbackSummary.affected_segments ?? postRelease.affected_segments);
  const feedbackClosed = numberOrNull(feedbackSummary.closed_segments ?? postRelease.closed_segments);
  const acceptedHolds = numberOrNull(feedbackSummary.accepted_holds ?? postRelease.accepted_holds);
  const parserBacklog = numberOrNull(feedbackSummary.parser_dynamic_backlog ?? postRelease.parser_dynamic_backlog);
  const feedbackItems = Array.isArray(postRelease.active_feedback_items)
    ? postRelease.active_feedback_items
    : Array.isArray(postRelease.active_items)
      ? postRelease.active_items
      : Array.isArray(postRelease.items)
        ? postRelease.items
        : Array.isArray(postRelease.feedback_items)
          ? postRelease.feedback_items
          : Array.isArray(postRelease.findings)
            ? postRelease.findings
            : [];
  const archivedFeedbackItems = Array.isArray(postRelease.archived_feedback_items)
    ? postRelease.archived_feedback_items
    : Array.isArray(postRelease.archived_items)
      ? postRelease.archived_items
      : [];
  const diffReview = postRelease.diff_review ?? {};
  const diffSummary = diffReview.summary ?? {};
  const patternDiscovery = postRelease.quality_pattern_discovery ?? {};
  const patternFamilies = Array.isArray(patternDiscovery.families) ? patternDiscovery.families : [];
  const actionablePatternCount = Number(patternDiscovery.actionable_family_count ?? 0);
  const providerProposals = release.provider_proposals ?? {};
  const proposalRows = Array.isArray(providerProposals.proposals) ? providerProposals.proposals : [];
  const proposalDraftCount = Number(providerProposals.draft_count ?? proposalRows.length);
  const providerRows = Array.isArray(providerHealth.providers) ? providerHealth.providers : [];
  const providerStatusLabels = {
    clean: 'sem candidatos',
    promotion_ready: 'promoção pronta',
    candidates_filtered: 'candidatos filtrados',
    failed: 'falhou',
    not_run: 'não executado',
  };
  const providerStatusTone = (status) => (
    status === 'clean'
      ? 'emerald'
      : status === 'promotion_ready'
        ? 'blue'
        : status === 'candidates_filtered'
          ? 'amber'
          : status === 'failed'
            ? 'red'
            : 'slate'
  );
  const lowScoreCohorts = diffReview.low_score_cohorts ?? {};
  const lowScoreActionable = Number(lowScoreCohorts.actionable ?? diffSummary.low_score_actionable ?? 0);
  const lowScoreInformational = Number(lowScoreCohorts.informational ?? diffSummary.low_score_informational ?? 0);
  const lowScoreUnexplained = Number(lowScoreCohorts.low_confidence_without_specific_evidence ?? diffSummary.low_score_unexplained ?? 0);
  const changedScoreComparison = diffSummary.changed_score_comparison ?? {};
  const packageScoreComparison = diffSummary.package_score_comparison ?? {};
  const changedCohortScoreComparison = diffSummary.changed_cohort_score_comparison ?? {};
  const rawChangedSegments = Array.isArray(diffReview.changed_segments) ? diffReview.changed_segments : [];
  const rawPackageSegments = Array.isArray(diffReview.package_diff_segments)
    ? diffReview.package_diff_segments
    : rawChangedSegments.filter((item) => String(item?.state_group ?? '').toLowerCase() === 'closed' && Number(item?.needs_output_apply ?? 0) === 0);
  const rawPromotionSegments = Array.isArray(diffReview.promotion_segments)
    ? diffReview.promotion_segments
    : rawChangedSegments.filter((item) => item?.recommended_resolution === 'use_candidate' && Number(item?.needs_output_apply ?? 0) === 0);
  const rawNewSegments = Array.isArray(diffReview.new_segments) ? diffReview.new_segments : [];
  const rawApplySegments = Array.isArray(diffReview.apply_segments) ? diffReview.apply_segments : [];
  const rawLowScoreSegments = Array.isArray(diffReview.low_score_segments) ? diffReview.low_score_segments : [];
  const rawScoreRegressionSegments = Array.isArray(diffReview.score_regression_segments) ? diffReview.score_regression_segments : [];
  const calibrationReview = diffReview.calibration_review ?? {};
  const calibrationPolicyDecision = String(calibrationReview.policy_decision ?? 'not_evaluated');
  const calibrationPolicyReasons = Array.isArray(calibrationReview.policy_reasons)
    ? calibrationReview.policy_reasons
    : [];
  const calibrationPolicyReasonLabels = {
    no_scored_epoch: 'nenhuma epoch pontuada',
    no_applied_pairwise_candidates: 'nenhum ajuste pairwise aplicado',
    invalid_integrity: 'falha de integridade ou validacao',
    raw_non_improving: 'score bruto igual ou regressivo',
    score_contract_changed: 'contrato de score alterado',
    large_batch: 'lote de alto volume',
    immature_provider: 'provedor ainda sem historico suficiente',
    control_accuracy_below_threshold: 'acuracia dos controles abaixo do limite',
    low_confidence_concentration: 'concentracao de baixa confianca',
    low_confidence_sample: 'amostra de baixa confianca',
    control_health_not_measured: 'saude dos controles ainda nao medida',
    periodic_sample_due: 'amostra periodica vencida',
    existing_pending_review: 'fila atual ainda pendente',
    already_calibrated_current_epoch: 'calibracao da epoch atual ja concluida',
    stable_mature_small_batch: 'lote pequeno, maduro e estavel',
  };
  const calibrationPolicySummary = calibrationPolicyReasons.length
    ? calibrationPolicyReasons.map((reason) => calibrationPolicyReasonLabels[reason] ?? reason.replaceAll('_', ' ')).join('; ')
    : 'politica ainda nao avaliada';
  const rawCalibrationReviewSegments = Array.isArray(diffReview.calibration_review_segments)
    ? diffReview.calibration_review_segments
    : [];
  const rawUnhandledSegments = Array.isArray(diffReview.unhandled_segments) ? diffReview.unhandled_segments : [];
  const scoreValue = (score) => {
    if (score === null || score === undefined || score === '' || score === 'not_measured') return null;
    const parsed = Number(score);
    return Number.isFinite(parsed) ? parsed : null;
  };
  const segmentNewScore = (item) => scoreValue(item?.review_new_score ?? item?.effective_new_score ?? item?.new_score ?? item?.model_safe_probability);
  const segmentOldScore = (item) => scoreValue(item?.review_old_score ?? item?.old_score ?? item?.old_model_safe_probability);
  const segmentDelta = (item) => scoreValue(item?.review_score_delta ?? item?.effective_score_delta ?? item?.score_delta);
  const sortSegmentsByScoreDesc = (items) => [...items].sort((a, b) => {
    const bScore = segmentNewScore(b);
    const aScore = segmentNewScore(a);
    const scoreDiff = (bScore ?? -1) - (aScore ?? -1);
    if (scoreDiff !== 0) return scoreDiff;
    return Number(a?.segment_id ?? 0) - Number(b?.segment_id ?? 0);
  });
  const sortSegmentsByRegressionDelta = (items) => [...items].sort((a, b) => {
    const aDelta = segmentDelta(a);
    const bDelta = segmentDelta(b);
    const deltaDiff = (aDelta ?? 1) - (bDelta ?? 1);
    if (deltaDiff !== 0) return deltaDiff;
    return Number(a?.segment_id ?? 0) - Number(b?.segment_id ?? 0);
  });
  const sortSegmentsByScoreAsc = (items) => [...items].sort((a, b) => {
    const aScore = segmentNewScore(a);
    const bScore = segmentNewScore(b);
    const aMissing = Number.isFinite(aScore) ? 1 : 0;
    const bMissing = Number.isFinite(bScore) ? 1 : 0;
    if (aMissing !== bMissing) return aMissing - bMissing;
    const scoreDiff = (aScore ?? 0) - (bScore ?? 0);
    if (scoreDiff !== 0) return scoreDiff;
    return Number(a?.segment_id ?? 0) - Number(b?.segment_id ?? 0);
  });
  const changedSegments = sortSegmentsByScoreDesc(rawChangedSegments);
  const packageSegments = sortSegmentsByRegressionDelta(rawPackageSegments);
  const promotionSegments = sortSegmentsByScoreDesc(rawPromotionSegments);
  const newSegments = sortSegmentsByScoreDesc(rawNewSegments);
  const applySegments = sortSegmentsByScoreDesc(rawApplySegments);
  const lowScoreSegments = sortSegmentsByScoreAsc(rawLowScoreSegments.filter((item) => {
    const score = segmentNewScore(item);
    return Number.isFinite(score) && score < 0.5;
  }));
  const scoreRegressionSegments = sortSegmentsByRegressionDelta(rawScoreRegressionSegments);
  const calibrationReviewSegments = [...rawCalibrationReviewSegments].sort((a, b) => {
    const priorityDiff = Number(Boolean(b?.is_priority)) - Number(Boolean(a?.is_priority));
    if (priorityDiff !== 0) return priorityDiff;
    return Number(a?.display_order ?? 0) - Number(b?.display_order ?? 0);
  });
  const unhandledSegments = sortSegmentsByScoreDesc(rawUnhandledSegments);
  const scoreTone = (score) => {
    const parsed = scoreValue(score);
    if (!Number.isFinite(parsed)) return 'slate';
    if (parsed >= 0.9) return 'emerald';
    if (parsed >= 0.75) return 'blue';
    if (parsed >= 0.5) return 'amber';
    return 'red';
  };
  const scoreLabel = (score) => {
    const parsed = scoreValue(score);
    return Number.isFinite(parsed) ? pctMetric(parsed) : 'N/A';
  };
  const lowScoreCohortMeta = {
    explicit_text_issue: { label: 'Defeito explicito', tone: 'red' },
    structural_block_without_issue: { label: 'Bloqueio estrutural', tone: 'amber' },
    deterministic_safe_but_low_score: { label: 'Seguro deterministico', tone: 'emerald' },
    unchanged_or_preserved_text: { label: 'Texto preservado', tone: 'blue' },
    low_confidence_without_specific_evidence: { label: 'Sem evidencia', tone: 'slate' },
  };
  const lowScoreCohort = (item) => lowScoreCohortMeta[item?.low_score_cohort]
    ?? { label: 'Nao classificado', tone: 'slate' };
  const scoreDeltaLabel = (item) => {
    const delta = segmentDelta(item);
    if (!Number.isFinite(delta)) return 'N/A';
    const sign = delta > 0 ? '+' : '';
    return `${sign}${pctMetric(delta)}`;
  };
  const scoreDeltaTone = (item) => {
    const delta = segmentDelta(item);
    if (!Number.isFinite(delta)) return 'slate';
    if (delta > 0.0001) return 'emerald';
    if (delta < -0.0001) return 'red';
    return 'amber';
  };
  const scorePointDeltaLabel = (delta) => {
    const parsed = scoreValue(delta);
    if (!Number.isFinite(parsed)) return 'sem delta';
    const points = parsed * 100;
    const sign = points > 0 ? '+' : '';
    const maximumFractionDigits = Math.abs(points) < 0.01 ? 4 : 2;
    return `${sign}${points.toLocaleString('pt-BR', { maximumFractionDigits })} p.p.`;
  };
  const scoreCellTitle = (item) => {
    const rawNew = scoreLabel(item?.new_score ?? item?.model_safe_probability);
    const rawOld = scoreLabel(item?.raw_old_score ?? item?.old_model_safe_probability ?? item?.old_score);
    const effectiveNew = scoreLabel(item?.effective_new_score ?? item?.new_score ?? item?.model_safe_probability);
    const rawDelta = scoreDeltaLabel({ score_delta: item?.score_delta });
    if (item?.score_review_basis === 'raw_model_after_applied_adjustment') {
      return `Revisao do score bruto apos ajuste aplicado. Old: ${scoreLabel(item?.review_old_score)}; novo: ${scoreLabel(item?.review_new_score)}; delta: ${scoreDeltaLabel(item)}. Score efetivo preservado: ${effectiveNew}.`;
    }
    if (item?.score_used_kind === 'pairwise_same_contract') {
      const pairwiseOld = scoreLabel(item?.pairwise_baseline_score ?? item?.old_score);
      const pairwiseRaw = scoreLabel(item?.pairwise_candidate_score_raw);
      return `Evidência pairwise comparável (${item?.pairwise_evidence_type ?? 'tipo não informado'}). Baseline: ${pairwiseOld}; candidato bruto: ${pairwiseRaw}; calibrado: ${effectiveNew}; delta: ${scoreDeltaLabel(item)}.`;
    }
    if (item?.score_calibration) {
      return `Score calibrado por ${item.score_calibration}. Old bruto: ${rawOld}; novo bruto: ${rawNew}; novo efetivo: ${effectiveNew}; delta bruto: ${rawDelta}.`;
    }
    return `Score bruto. Old: ${rawOld}; novo: ${rawNew}.`;
  };
  const integrityLabel = (item) => {
    const status = String(item?.package_integrity_status ?? 'ok');
    if (status === 'locked_confirmation_output_mismatch') return 'conf. travada diverge';
    if (status === 'needs_output_apply') return 'needs apply';
    if (status === 'not_closed') return 'nao fechado';
    if (status === 'ok') return 'ok';
    return status.replaceAll('_', ' ');
  };
  const integrityTone = (item) => {
    const status = String(item?.package_integrity_status ?? 'ok');
    if (status === 'ok') return 'emerald';
    if (status === 'needs_output_apply' || status === 'not_closed') return 'amber';
    return 'red';
  };
  const integrityTitle = (item) => {
    const status = item?.package_integrity_status ?? 'ok';
    const reason = item?.package_integrity_reason ?? '';
    const confirmed = item?.confirmed_text;
    const confirmedPart = confirmed !== null && confirmed !== undefined ? ` Confirmado: ${confirmed}` : '';
    return `${status}${reason ? `: ${reason}` : ''}.${confirmedPart}`;
  };
  const feedbackSegmentLabel = (item) => {
    if (Array.isArray(item.segment_ids) && item.segment_ids.length) return item.segment_ids.join(', ');
    if (item.segment_id !== null && item.segment_id !== undefined) return String(item.segment_id);
    const affected = numberOrNull(item.affected_segments);
    return affected === null ? 'nao medido' : compact(affected);
  };
  const activeFeedbackItems = feedbackItems.filter((item) => {
    const status = String(item.status ?? '').trim().toLowerCase();
    const inactiveStatuses = new Set(['closed', 'hold', 'accepted_hold', 'applied_to_candidate', 'archived', 'done', 'resolved', 'cancelled']);
    return status && !inactiveStatuses.has(status);
  });
  const feedbackRows = activeFeedbackItems.map((item, index) => ({
    ...item,
    row_id: item.id ?? `feedback-${index}`,
    observed_text: item.observed_text ?? item.text_observed ?? item.evidence ?? 'nao medido',
    category: item.category ?? item.area ?? item.risk ?? 'nao medido',
    segment_label: feedbackSegmentLabel(item),
    visual_severity: item.visual_severity ?? item.severity ?? 'nao medido',
    next_action: item.next_action ?? item.current_candidate_status ?? item.decision ?? 'nao medido',
  }));
  const packageOldScore = packageScoreComparison.weighted_avg_old_score ?? packageScoreComparison.avg_old_score;
  const packageNewScore = packageScoreComparison.weighted_avg_new_score ?? packageScoreComparison.avg_new_score ?? packageScoreComparison.package_quality_score;
  const packageDelta = packageScoreComparison.weighted_avg_delta ?? packageScoreComparison.avg_delta;
  const packageCoverage = packageScoreComparison.coverage;
  const packageMeasuredCount = Number(packageScoreComparison.measured_count ?? 0);
  const packageTotalCount = Number(packageScoreComparison.total_segment_count ?? 0);
  const changedCohortOldScore = changedCohortScoreComparison.weighted_avg_old_score ?? changedCohortScoreComparison.avg_old_score;
  const changedCohortNewScore = changedCohortScoreComparison.weighted_avg_new_score ?? changedCohortScoreComparison.avg_new_score;
  const changedCohortDelta = changedCohortScoreComparison.weighted_avg_delta ?? changedCohortScoreComparison.avg_delta;
  const packageScoreSummaryLabel = Number(packageScoreComparison.measured_count ?? 0) > 0
    ? `score global old ${scoreLabel(packageOldScore)} -> output ${scoreLabel(packageNewScore)} (${scorePointDeltaLabel(packageDelta)})`
    : 'score pacote nao medido';
  const packageCoverageLabel = packageTotalCount > 0
    ? `cobertura ${scoreLabel(packageCoverage)} (${compact(packageMeasuredCount)}/${compact(packageTotalCount)})`
    : 'cobertura nao medida';
  const changedCohortScoreLabel = Number(changedCohortScoreComparison.measured_count ?? 0) > 0
    ? `${compact(changedCohortScoreComparison.measured_count)} mudancas: ${scoreLabel(changedCohortOldScore)} -> ${scoreLabel(changedCohortNewScore)} (${scorePointDeltaLabel(changedCohortDelta)})`
    : 'sem mudancas pontuadas';
  const outputQualityBands = packageScoreComparison.quality_bands?.output ?? {};
  const qualityBandsLabel = `critico ${compact(outputQualityBands.critical ?? 0)} | baixo ${compact(outputQualityBands.low ?? 0)} | moderado ${compact(outputQualityBands.moderate ?? 0)} | bom ${compact(outputQualityBands.good ?? 0)} | alta ${compact(outputQualityBands.high ?? 0)} | sem score ${compact(outputQualityBands.unmeasured ?? 0)}`;
  const rawOutputDiffCount = diffSummary.raw_output_diff_count ?? diffSummary.changed_vs_old ?? rawChangedSegments.length;
  const packageExcludedCount = diffSummary.package_excluded_count ?? Math.max(0, Number(rawOutputDiffCount ?? 0) - Number(diffSummary.package_diff_count ?? packageSegments.length ?? 0));
  const hasCompletedCalibrationHistory = Boolean(
    calibrationReview.run_id
    && Number(calibrationReview.decided_count ?? 0) > 0
  );
  const reviewTabs = [
    { id: 'apply', label: 'Apply', count: diffSummary.needs_apply ?? applySegments.length, tone: applySegments.length ? 'amber' : 'slate' },
    { id: 'package', label: 'Pacote', count: diffSummary.package_diff_count ?? packageSegments.length, tone: packageSegments.length ? 'blue' : 'slate' },
    { id: 'new', label: 'Novos', count: diffSummary.new_vs_old ?? newSegments.length, tone: newSegments.length ? 'blue' : 'slate' },
    { id: 'promotions', label: 'Promocoes', count: diffSummary.promotions_vs_old ?? promotionSegments.length, tone: promotionSegments.length ? 'emerald' : 'slate' },
    { id: 'regressions', label: 'Score bruto', count: rawScoreRegressionCount, tone: effectiveScoreRegressionCount || unresolvedRawScoreRegressionCount ? 'red' : rawScoreRegressionCount ? 'blue' : 'slate' },
    { id: 'discovery', label: 'Descobertas', count: actionablePatternCount, tone: actionablePatternCount ? 'amber' : patternFamilies.length ? 'blue' : 'slate' },
    { id: 'proposals', label: 'Propostas', count: proposalDraftCount, tone: proposalDraftCount ? 'amber' : providerProposals.instrumented ? 'emerald' : 'slate' },
    { id: 'providers', label: 'Provedores', count: providerRows.length, tone: providerHealth.status === 'healthy' ? 'emerald' : providerHealth.instrumented ? 'amber' : 'slate' },
    { id: 'calibration', label: 'Calibracao', count: calibrationReview.pending_count ?? calibrationReviewSegments.filter((item) => item.review_status === 'pending').length, tone: calibrationPolicyDecision === 'skip' || calibrationReview.consumption_status === 'consumed' ? 'emerald' : calibrationPolicyDecision === 'sample' ? 'blue' : Number(calibrationReview.pending_count ?? 0) ? 'amber' : calibrationReviewSegments.length ? 'blue' : 'slate' },
    { id: 'unhandled', label: 'Pendentes', count: diffSummary.unhandled_by_network ?? unhandledSegments.length, tone: unhandledSegments.length ? 'amber' : 'slate' },
    { id: 'feedback', label: 'Feedbacks', count: feedbackRows.length, tone: feedbackRows.length ? 'blue' : 'slate' },
    { id: 'low_score', label: 'Baixo score', count: diffSummary.low_score ?? lowScoreSegments.length, tone: lowScoreActionable ? 'red' : lowScoreUnexplained ? 'amber' : Number(diffSummary.low_score ?? lowScoreSegments.length) ? 'blue' : 'slate' },
  ];
  const activeReviewTabIndex = Math.max(0, reviewTabs.findIndex((tab) => tab.id === postReleaseView));
  const previousReviewTab = reviewTabs[(activeReviewTabIndex - 1 + reviewTabs.length) % reviewTabs.length];
  const nextReviewTab = reviewTabs[(activeReviewTabIndex + 1) % reviewTabs.length];
  const selectReviewTab = (tabId) => {
    setPostReleaseView(tabId);
    requestAnimationFrame(() => {
      const tabStrip = reviewTabsRef.current;
      const selectedTab = tabStrip?.querySelector(`[data-review-tab-id="${tabId}"]`);
      if (!tabStrip || !selectedTab) return;
      const centeredLeft = selectedTab.offsetLeft - (tabStrip.clientWidth - selectedTab.clientWidth) / 2;
      tabStrip.scrollTo({ left: Math.max(0, centeredLeft), behavior: 'smooth' });
    });
  };
  const navigateReviewTabs = (direction) => {
    const nextIndex = (activeReviewTabIndex + direction + reviewTabs.length) % reviewTabs.length;
    selectReviewTab(reviewTabs[nextIndex].id);
  };
  const reviewTabClass = (tab, active) => {
    const base = 'group relative inline-flex h-9 min-w-[108px] shrink-0 snap-start items-center justify-between gap-2 rounded-lg border border-b-2 px-3 py-2 text-[11px] font-black shadow-sm transition focus:outline-none focus:ring-2 focus:ring-cyan-300/70';
    if (!active) {
      const toneClass = tab.tone === 'red'
        ? 'border-red-300/25 bg-red-500/10 text-red-100/75 hover:bg-red-500/18'
        : tab.tone === 'amber'
          ? 'border-amber-300/25 bg-amber-500/10 text-amber-100/75 hover:bg-amber-500/18'
          : tab.tone === 'emerald'
            ? 'border-emerald-300/25 bg-emerald-500/10 text-emerald-100/75 hover:bg-emerald-500/18'
            : tab.tone === 'blue'
              ? 'border-blue-300/25 bg-blue-500/10 text-blue-100/75 hover:bg-blue-500/18'
              : 'border-[var(--dash-border)] bg-[var(--dash-card)] text-[var(--dash-muted)] hover:border-blue-300/35 hover:text-blue-100';
      return cn(base, toneClass, 'hover:-translate-y-0.5');
    }
    const selected = '-translate-y-0.5 border-cyan-200/90 ring-2 ring-cyan-300/80 shadow-[0_0_15px_rgba(34,211,238,0.3)]';
    if (tab.tone === 'red') return cn(base, selected, 'bg-red-500/30 text-red-50');
    if (tab.tone === 'amber') return cn(base, selected, 'bg-amber-500/30 text-amber-50');
    if (tab.tone === 'emerald') return cn(base, selected, 'bg-emerald-500/30 text-emerald-50');
    if (tab.tone === 'blue') return cn(base, selected, 'bg-blue-500/30 text-blue-50');
    return cn(base, selected, 'bg-slate-500/20 text-slate-50');
  };
  const comparisonSegments = postReleaseView === 'package' ? packageSegments : promotionSegments;
  const missingReleaseReadiness = [knownOpen, appliedPendingValidation].some((value) => value === null);
  const publishStatus = publicationAllowed ? 'Liberada' : publicationGate.status === 'not_measured' ? 'Não medida' : 'Bloqueada';
  const publishTone = publicationAllowed ? 'emerald' : publicationGate.status === 'not_measured' ? 'slate' : 'red';
  const productionPublicationStatus = publicationActionAllowed
    ? 'Publicável'
    : publicationAllowed
      ? 'Sem apply'
      : publishStatus;
  const productionPublicationTone = publicationActionAllowed ? 'emerald' : publicationAllowed ? 'blue' : publishTone;
  const latestPublicationCounts = latestPublicationPreflight?.counts && typeof latestPublicationPreflight.counts === 'object'
    ? latestPublicationPreflight.counts
    : {};
  const outputPackageDiffCount = numberOrNull(
    diffSummary.package_diff_count
      ?? latestPublicationCounts.package_diff
      ?? evaluatedDiffSummary.package_diff_count
      ?? evaluatedDiffSummary.package_diff
  );
  const outputRawDiffCount = numberOrNull(
    diffSummary.raw_output_diff_count
      ?? diffSummary.changed_vs_old
      ?? latestPublicationCounts.raw_output_diff
      ?? evaluatedDiffSummary.raw_output_diff_count
      ?? evaluatedDiffSummary.raw_output_diff
      ?? evaluatedDiffSummary.changed_vs_old
  );
  const outputCurrentDiffCount = outputPackageDiffCount ?? outputRawDiffCount;
  const outputCurrentLabel = outputCurrentDiffCount !== null && outputCurrentDiffCount > 0
    ? `${compact(outputCurrentDiffCount)} mudancas`
    : outputRestoreStatus.restored_exactly
      ? 'restaurado'
      : valueOrPending(outputRestoreStatus.restored_exactly);
  const outputCurrentTone = outputCurrentDiffCount !== null && outputCurrentDiffCount > 0
    ? 'amber'
    : outputRestoreStatus.restored_exactly
      ? 'emerald'
      : 'slate';
  const mainBlocker = release.needs_apply
    ? `needs_output_apply: ${compact(release.needs_apply)}`
    : publicationReasons.length
      ? publicationReasons.join(', ')
      : missingReleaseReadiness
        ? 'feedback pos-release nao medido'
        : publicationAllowed
          ? 'sem bloqueador'
          : 'publicacao bloqueada';
  const postReleaseCards = [
    { title: 'Pendências conhecidas', value: metricValue(knownOpen), tone: toneForCount(knownOpen, 'emerald') },
    { title: 'Em validacao', value: metricValue(appliedPendingValidation), tone: toneForCount(appliedPendingValidation, 'emerald') },
    { title: 'Fechadas', value: metricValue(closedFeedback), tone: closedFeedback === null ? 'slate' : 'emerald' },
    { title: 'Holds', value: metricValue(acceptedHolds), tone: acceptedHolds === null ? 'slate' : 'blue' },
  ];
  const miniPostReleaseChips = [
    approvedPendingApply ? { label: 'aplicar', value: compact(approvedPendingApply), tone: 'amber' } : null,
    parserBacklog !== null ? { label: 'parser', value: compact(parserBacklog), tone: parserBacklog ? 'amber' : 'emerald' } : null,
    feedbackAffected !== null ? { label: 'afetados', value: compact(feedbackAffected), tone: 'blue' } : null,
    feedbackClosed !== null ? { label: 'segmentos fechados', value: compact(feedbackClosed), tone: 'emerald' } : null,
  ].filter(Boolean);
  const gateCards = [
    {
      title: 'Gate de avaliação',
      status: evaluationAllowed ? 'Liberado' : evaluationGate.status === 'not_measured' ? 'Não medido' : 'Bloqueado',
      tone: evaluationAllowed ? 'emerald' : evaluationGate.status === 'not_measured' ? 'slate' : 'red',
      source: 'can_start_evaluation_full_production_now',
      reasons: evaluationReasons,
    },
    {
      title: 'Gate de publicação',
      status: publishStatus,
      tone: publishTone,
      source: 'can_publish_after_full_production_now',
      reasons: publicationReasons,
    },
  ];
  const releaseReadinessItems = [
    { label: 'Gate avaliação', value: evaluationAllowed ? 'Liberada' : valueOrPending(evaluationGate.status), tone: evaluationAllowed ? 'emerald' : 'red' },
    { label: 'Gate publicação', value: publishStatus, tone: publishTone },
    { label: 'Needs apply', value: compact(release.needs_apply), tone: release.needs_apply ? 'amber' : 'emerald' },
    { label: 'Locks visuais', value: metricValue(visualLocks.count), tone: visualLocks.count == null ? 'slate' : 'blue' },
    { label: 'Pendências', value: metricValue(knownOpen), tone: toneForCount(knownOpen, 'emerald') },
    { label: 'Validação', value: metricValue(appliedPendingValidation), tone: toneForCount(appliedPendingValidation, 'emerald') },
  ];
  const sourceOutputChecklist = [
    { label: 'Baseline', value: baselineControl.stable_baseline_path ?? 'source\\spanish_old', tone: 'blue' },
    { label: 'Output atual', value: outputCurrentLabel, tone: outputCurrentTone },
    { label: 'Candidate', value: releaseCandidate.current_candidate_path ? 'hotfix candidate' : 'nao medido', tone: releaseCandidate.current_candidate_path ? 'amber' : 'slate' },
    { label: 'Último preflight', value: compactDateTime(gameUpdate.latest_preflight ?? safety.preflight?.generated_at), tone: gameUpdate.latest_preflight || safety.preflight?.generated_at ? 'blue' : 'slate' },
  ];
  const updateChecklist = [
    { label: 'Snapshot', ok: Boolean(runStatus?.snapshot_path || runStatus?.snapshot_archive_path), pending: runStatus?.snapshot_archive_path ? 'ok' : 'nao medido' },
    { label: 'Output restaurado', ok: outputRestoreStatus.restored_exactly === true, pending: outputRestoreStatus.restored_exactly ? 'ok' : 'nao medido' },
    { label: 'Locks carregados', ok: numberOrNull(visualLocks.count) !== null, pending: visualLocks.count == null ? 'nao medido' : `${compact(visualLocks.count)} locks` },
    { label: 'Produção de avaliação', ok: evaluationAllowed, pending: evaluationAllowed ? 'liberada' : 'bloqueada' },
    { label: 'Publicação', ok: publicationAllowed, pending: publicationAllowed ? 'liberada' : 'bloqueada' },
  ];
  const productionStateRows = [
    { label: 'Baseline', value: baselineControl.stable_baseline_path ?? 'source\\spanish_old', tone: 'blue' },
    { label: 'Output', value: outputCurrentLabel, tone: outputCurrentTone },
    { label: 'Preflight', value: compactDateTime(gameUpdate.latest_preflight ?? safety.preflight?.generated_at), tone: gameUpdate.latest_preflight || safety.preflight?.generated_at ? 'blue' : 'slate' },
    { label: 'Apply verificado', value: hasVerifiedApplyQueue ? compact(evaluatedApplyCount) : '0', tone: hasVerifiedApplyQueue ? 'amber' : 'blue' },
    { label: 'Avaliação', value: evaluationAllowed ? 'liberada' : 'bloqueada', tone: evaluationAllowed ? 'emerald' : 'red' },
    { label: 'Publicável', value: productionPublicationStatus.toLowerCase(), tone: productionPublicationTone },
  ];
  const blockingSummary = publicationActionAllowed
    ? `${compact(evaluatedApplyCount)} apply verificado pela avaliação`
    : publicationAllowed
      ? 'sem apply verificado para aplicar'
      : publicationReasons.length
        ? publicationReasons.join(', ')
        : evaluationReasons.length
          ? evaluationReasons.join(', ')
          : 'not_measured';
  const lastDiagnosticLine = lastDiagnostic
    ? `Diagnóstico atualizado: ${lastDiagnostic.completedAt} · sugestões recalculadas · sem confirmação · output intacto`
    : `Último diagnóstico: cache ${cache.generated_at ? `atualizado em ${cache.generated_at}` : 'pendente'} · sem produção iniciada nesta sessão`;
  const visibleProductionRun = runStatus?.run_id ? runStatus : lastRun;
  const visibleProductionRunId = visibleProductionRun?.run_id ?? productionDelta.last_production_run_id ?? '-';
  const visibleProductionStatus = visibleProductionRun?.status ?? 'idle';
  const lastProductionLine = `Última produção: ${visibleProductionRunId} · ${statusLabel(visibleProductionStatus)} · Segment-state #${visibleProductionRun?.new_segment_state_run_id ?? release.latest_segment_state_run_id ?? '-'} · Coverage ${pct(release.output_coverage)}`;
  const currentActionRun = actionRunStatus ?? (runActive ? runStatus : null);
  const currentActionStatus = normalizedRunStatus(currentActionRun?.status);
  const currentRunLine = currentActionRun?.run_id
    ? `Run ${currentActionRun.run_id} · ${statusLabel(currentActionRun.status)} · etapa ${currentStage?.label ?? currentActionRun.current_label ?? currentActionRun.current_stage ?? 'sem etapa ativa'}`
    : 'Nenhum run de produção iniciado nesta sessão.';
  const lastResultLine = effectiveSelectedMode === 'diagnostic' ? lastDiagnosticLine : lastProductionLine;
  const runFailure = currentActionStatus === 'failed' ? (currentActionRun.failure ?? {}) : {};
  const failedStageId = currentActionRun?.failed_stage ?? runFailure.stage ?? currentActionRun?.current_stage ?? '';
  const failedStage = (runStages ?? []).find((stage) => stage.id === failedStageId);
  const failedStageLabel = failedStage?.label ?? failedStageId ?? 'nao medido';
  const failureMessage = runFailure.message ?? currentActionRun?.message ?? startError ?? '';
  const failureDisk = modeActionNotice?.diskPreflight ?? currentActionRun?.disk_preflight ?? runFailure.disk_preflight ?? diskPreflight;
  const terminalDiskFailure = currentActionStatus === 'failed'
    ? (runFailure.reason === 'insufficient_disk_space' || /insufficient disk space/i.test(failureMessage))
    : false;
  const actionDiskBlock = modeActionNotice?.status === 'blocked' && modeActionNotice?.diskPreflight?.ok === false;
  const diskFailure = effectiveSelectedMode === 'evaluation' && (terminalDiskFailure || actionDiskBlock);
  const actionMayChangeOutput = runActive || actionWaitingForRun || ['starting', 'running'].includes(actionNoticeStatus);
  const runOutputLabel = actionMayChangeOutput
    ? 'output pode mudar'
    : currentActionRun?.run_id
      ? runOutputStatusLabel(currentActionRun)
      : modeActionNotice?.outputChanged
        ? 'output alterado'
        : modeActionNotice
          ? 'sem escrita no output'
          : 'output nao medido';
  const runOutputChanged = Boolean(currentActionRun?.output_changed || modeActionNotice?.outputChanged);
  const runOutputBadgeTone = actionMayChangeOutput || runOutputChanged
    ? 'amber'
    : runOutputLabel === 'output nao medido'
      ? 'slate'
      : 'emerald';
  const runSegmentStateId = currentActionRun?.new_segment_state_run_id ?? currentActionRun?.segment_state_run_id ?? null;
  const runSegmentStateLabel = runSegmentStateId ? `segment-state #${runSegmentStateId}` : 'sem novo segment-state';
  const pipelineRunMode = runActive ? activeRunMode : modeActionNotice?.mode;
  const executionStatus = pipelineRunMode === 'evaluation' || pipelineRunMode === 'publication'
    ? (currentActionRun?.status ?? modeActionNotice?.status)
    : modeActionNotice?.status;
  const executionStatusValue = normalizedRunStatus(executionStatus);
  const executionTone = executionStatusValue === 'failed' || executionStatusValue === 'blocked'
    ? 'red'
    : executionStatusValue === 'completed' || executionStatusValue === 'refresh_completed'
      ? 'emerald'
      : modeActionNotice?.tone ?? selectedModeInfo.tone ?? 'blue';
  const diagnosticElapsedMs = diagnosticStartedAt
    ? (refreshing ? Date.now() : (diagnosticFinishedAt ?? Date.now())) - diagnosticStartedAt
    : 0;
  const diagnosticPhases = Array.isArray(diagnosticRunStatus?.phases)
    ? diagnosticRunStatus.phases
    : [];
  const diagnosticStatusValue = normalizedRunStatus(diagnosticRunStatus?.status);
  const diagnosticProgress = refreshing
    ? clampNumber(Number(diagnosticRunStatus?.progress_pct ?? 1), 1, 99)
    : executionStatusValue === 'failed' || diagnosticStatusValue === 'failed'
      ? 100
      : modeActionNotice?.mode === 'diagnostic'
        ? 100
        : 0;
  const executionProgress = modeActionNotice?.mode === 'diagnostic' || (refreshing && effectiveSelectedMode === 'diagnostic')
    ? diagnosticProgress
    : modeActionNotice?.mode === 'evaluation' || modeActionNotice?.mode === 'publication'
      ? (runActive ? Math.max(8, runProgress) : currentActionStatus === 'completed' ? 100 : currentActionStatus === 'failed' ? 100 : runStartPending ? 8 : ['completed', 'blocked', 'failed'].includes(executionStatusValue) ? 100 : executionStatusValue === 'starting' ? 8 : 0)
      : 0;
  const diagnosticExecutionActive = refreshing && effectiveSelectedMode === 'diagnostic';
  const diagnosticElapsedLabel = diagnosticStartedAt ? durationLabel(diagnosticElapsedMs) : 'nao medido';
  const diagnosticCurrentStepLabel = diagnosticRunStatus?.current_label
    ?? (refreshing ? 'Preparar diagnóstico' : 'Concluído');
  const diagnosticStepStatus = (stepIndex) => {
    const measured = diagnosticPhases[stepIndex]?.status;
    if (measured) return normalizedStageStatus(measured);
    if (startStatus === 'failed') return stepIndex === 0 ? 'failed' : 'pending';
    if (!refreshing) return modeActionNotice?.mode === 'diagnostic' ? 'done' : 'pending';
    return stepIndex === 0 ? 'running' : 'pending';
  };
  const diagnosticStepDetail = (stepIndex, fallback) => (
    diagnosticPhases[stepIndex]?.current_label
    ?? diagnosticPhases[stepIndex]?.current_stage
    ?? fallback
  );
  const executionTitle = modeActionNotice?.mode === 'diagnostic'
    ? (refreshing ? 'Diagnóstico em execução' : modeActionNotice?.title)
    : modeActionNotice?.mode === 'evaluation'
      ? (currentActionStatus === 'failed' ? 'Execução falhou' : runActive ? 'Produção de avaliação em execução' : currentActionStatus === 'completed' ? 'Produção de avaliação concluída' : modeActionNotice?.title)
      : modeActionNotice?.title;
  const executionDetail = modeActionNotice?.mode === 'evaluation'
    ? currentActionStatus === 'failed'
      ? `Run ${currentActionRun.run_id} · falha em ${failedStageLabel} · ${failureMessage || 'mensagem nao medida'}`
      : `Modo Producao de avaliacao · ${currentActionRun?.run_id ? `run ${currentActionRun.run_id}` : 'aguardando run'} · etapa ${currentStage?.label ?? currentActionRun?.current_label ?? currentActionRun?.current_stage ?? 'preparando'}`
    : modeActionNotice?.body;
  const displayExecutionDetail = modeActionNotice?.mode === 'evaluation' && currentActionStatus === 'completed'
    ? `Run ${currentActionRun.run_id} concluida Â· ${runOutputLabel} Â· ${runSegmentStateLabel}`
    : executionDetail;
  const runModeUsesPipeline = pipelineRunMode === 'evaluation' || pipelineRunMode === 'publication';
  const displayExecutionTitle = modeActionNotice?.mode === 'diagnostic'
    ? (refreshing ? 'Diagnostico em execucao' : modeActionNotice?.title)
    : modeActionNotice?.mode === 'evaluation'
      ? (currentActionStatus === 'failed' ? 'Execucao falhou' : runActive ? 'Producao de avaliacao em execucao' : currentActionStatus === 'completed' ? 'Producao de avaliacao concluida' : modeActionNotice?.title)
      : modeActionNotice?.mode === 'publication'
        ? (currentActionStatus === 'failed' ? 'Execucao falhou' : runActive ? 'Producao publicavel em execucao' : currentActionStatus === 'completed' ? 'Producao publicavel concluida' : modeActionNotice?.title)
        : executionTitle;
  const displayExecutionDetailText = runModeUsesPipeline
    ? currentActionStatus === 'completed'
      ? `Run ${currentActionRun?.run_id ?? '-'} concluida - ${runOutputLabel} - ${runSegmentStateLabel}`
      : currentActionStatus === 'failed'
        ? `Run ${currentActionRun?.run_id ?? '-'} - falha em ${failedStageLabel} - ${failureMessage || 'mensagem nao medida'}`
        : `Modo ${pipelineRunMode === 'publication' ? 'Producao publicavel' : 'Producao de avaliacao'} - ${currentActionRun?.run_id ? `run ${currentActionRun.run_id}` : 'aguardando run'} - etapa ${currentStage?.label ?? currentActionRun?.current_label ?? currentActionRun?.current_stage ?? 'preparando'}`
    : displayExecutionDetail;
  const selectedModePreviewSteps = {
    diagnostic: [
      { label: 'Estado atual', status: refreshing ? 'running' : 'pending', detail: 'Recalcula fechamento e identifica a superfície elegível sem alterar output.' },
      { label: 'Descoberta', status: 'pending', detail: 'Analisa famílias conhecidas e gera candidatos shadow de melhoria.' },
      { label: 'Sugestões', status: 'pending', detail: 'Registra evidências pareadas e promoções sugeridas, sem confirmar apply.' },
      { label: 'Consolidação', status: 'pending', detail: 'Atualiza cache, preflight, filas e comparativos do painel.' },
    ],
    evaluation: [
      { label: 'Preparacao', status: evaluationAllowed && !diskBlocksProduction ? 'pending' : 'failed', detail: 'Snapshot, arquivo, indice e segment-state antes da escrita.' },
      { label: 'Analise', status: 'pending', detail: 'Dry-runs e politicas medem o que pode ser promovido com seguranca.' },
      { label: 'Escrita', status: 'pending', detail: 'Gera output de avaliacao para medir score, diff e regressao.' },
      { label: 'Validacao', status: 'pending', detail: 'Recalcula estado, reaudita e emite relatorio final.' },
    ],
    publication: [
      { label: 'Gates', status: publicationAllowed ? 'pending' : 'failed', detail: 'Confere bloqueios, feedback ativo e estado de publicacao.' },
      { label: 'Apply confirmado', status: 'pending', detail: 'Valida correcoes ja confirmadas antes de escrever no output.' },
      { label: 'Promocoes', status: 'pending', detail: 'Revisa candidatos melhores que o old antes de virarem apply.' },
      { label: 'Relatorio final', status: 'pending', detail: 'Compara old vs output final, score do pacote e motivos de bloqueio.' },
    ],
    hotfix: [
      { label: 'Fila', status: releaseCandidate.current_candidate_path ? 'pending' : 'failed' },
      { label: 'Pacote', status: 'pending' },
      { label: 'Validar', status: 'pending' },
      { label: 'Handoff', status: 'pending' },
    ],
  }[effectiveSelectedMode] ?? [];
  const executionTerminalProblem = executionStatusValue === 'failed' || executionStatusValue === 'blocked';
  const publicationPreflight = latestPublicationPreflight ?? {};
  const publicationPreflightCounts = publicationPreflight.counts ?? {};
  const publicationPreflightReady = publicationPreflight.status === 'ready' || publicationPreflight.can_publish_now === true;
  const publicationPreflightApply = Number(publicationPreflightCounts.needs_apply ?? 0);
  const publicationPreflightApplyReady = Number(publicationPreflightCounts.apply_ready ?? 0);
  const publicationPreflightApplyBlocked = Number(publicationPreflightCounts.apply_blocked ?? 0);
  const publicationPreflightTokenMismatch = Number(publicationPreflightCounts.apply_token_mismatch ?? 0);
  const publicationPreflightPromotions = Number(publicationPreflightCounts.promotions ?? 0);
  const publicationPreflightRegressions = Number(publicationPreflightCounts.score_regressions ?? 0);
  const publicationPreflightUnhandled = Number(publicationPreflightCounts.unhandled_by_network ?? 0);
  const publicationPreflightQuality = publicationPreflight.quality ?? {};
  const publicationPackageScoreLabel = publicationPreflightQuality.package_new_score === null || publicationPreflightQuality.package_new_score === undefined
    ? 'score do pacote nao medido'
    : `score pacote ${scoreLabel(publicationPreflightQuality.package_old_score)} -> ${scoreLabel(publicationPreflightQuality.package_new_score)} (${scoreDeltaLabel({ score_delta: publicationPreflightQuality.package_delta })})`;
  const evaluationPipelineVisible = runModeUsesPipeline && evaluationExecutionVisible;
  const executionSteps = runModeUsesPipeline && evaluationPipelineVisible
    ? displayPhases.map((phase) => ({
        label: phase.title,
        status: phase.status,
        detail: phase.currentStage?.label ?? phase.currentStage?.id ?? '',
      }))
    : runModeUsesPipeline && ['failed', 'blocked'].includes(executionStatusValue)
      ? [
          { label: 'Preparacao', status: executionTerminalProblem ? 'failed' : 'done', detail: failureMessage || 'Nao foi possivel iniciar a producao de avaliacao.' },
          { label: 'Analise', status: 'pending' },
          { label: 'Escrita', status: 'pending' },
          { label: 'Validacao', status: 'pending' },
        ]
    : modeActionNotice?.mode === 'diagnostic' || diagnosticExecutionActive
      ? [
          { label: 'Estado atual', status: diagnosticStepStatus(0), detail: diagnosticStepDetail(0, 'Recalcula fechamento e identifica a superfície elegível sem alterar output.') },
          { label: 'Descoberta', status: diagnosticStepStatus(1), detail: diagnosticStepDetail(1, 'Analisa famílias conhecidas e gera candidatos shadow de melhoria.') },
          { label: 'Sugestões', status: diagnosticStepStatus(2), detail: diagnosticStepDetail(2, 'Registra evidências pareadas e promoções sugeridas, sem confirmar apply.') },
          { label: 'Consolidação', status: diagnosticStepStatus(3), detail: diagnosticStepDetail(3, 'Atualiza cache, preflight, filas e comparativos do painel.') },
        ]
      : modeActionNotice?.mode === 'publication'
        ? [
            {
              label: 'Gates',
              status: publicationPreflightReady ? 'done' : executionStatusValue === 'failed' ? 'failed' : 'failed',
              detail: (publicationPreflight.blocking_reasons ?? []).length
                ? `Bloqueios: ${publicationPreflight.blocking_reasons.join(', ')}`
                : 'Sem bloqueios de publicacao.',
            },
            {
              label: 'Apply',
              status: publicationPreflightApply > 0
                ? publicationPreflightApplyReady === publicationPreflightApply ? 'done' : 'failed'
                : 'done',
              detail: publicationPreflightApply > 0
                ? `${compact(publicationPreflightApplyReady)}/${compact(publicationPreflightApply)} pronto para escrita; ${compact(publicationPreflightApplyBlocked)} bloqueado; token mismatch ${compact(publicationPreflightTokenMismatch)}.`
                : 'Sem correcoes confirmadas aguardando escrita.',
            },
            {
              label: 'Promocoes',
              status: publicationPreflightPromotions > 0 ? 'done' : 'pending',
              detail: `${compact(publicationPreflightPromotions)} promocoes medidas contra baseline.`,
            },
            {
              label: 'Relatorio',
              status: executionStatusValue === 'failed' ? 'failed' : 'done',
              detail: `${publicationPackageScoreLabel}. Regressoes ${compact(publicationPreflightRegressions)} - Pendentes ${compact(publicationPreflightUnhandled)} - Proximo: ${publicationPreflight.next_action ?? 'revisar'}.`,
            },
          ]
        : selectedModePreviewSteps;
  const executionStepClass = (status) => (
    status === 'done'
      ? 'border-emerald-400/30 bg-emerald-500/10 text-emerald-300'
      : status === 'running'
        ? 'border-blue-400/40 bg-blue-500/15 text-blue-300'
      : status === 'failed'
          ? 'border-red-400/35 bg-red-500/10 text-red-300'
          : status === 'cancelled'
            ? 'border-amber-400/35 bg-amber-500/10 text-amber-300'
          : 'border-[var(--dash-border)] bg-[var(--dash-card)] text-[var(--dash-muted)]'
  );
  const selectedModeTitle = {
    diagnostic: 'Analisa o estado e sugere novas promoções.',
    evaluation: 'Valida promoções e aprova a fila de apply.',
    publication: 'Aplica, escreve e fecha o output publicável.',
    hotfix: 'Opera hotfix visual.',
  }[effectiveSelectedMode] ?? selectedModeInfo.description;
  const latestRunFailure = visibleProductionRun?.status === 'failed' ? (visibleProductionRun.failure ?? {}) : {};
  const latestRunFailureMessage = latestRunFailure.message ?? visibleProductionRun?.message ?? '';
  const latestTerminalDiskFailure = visibleProductionRun?.status === 'failed'
    ? (latestRunFailure.reason === 'insufficient_disk_space' || /insufficient disk space/i.test(latestRunFailureMessage))
    : false;
  const latestRunStageLabel = visibleProductionRun?.current_label ?? visibleProductionRun?.current_stage ?? 'sem etapa ativa';
  const latestRunStartedLabel = runDateTimeLabel(visibleProductionRun?.run_id, visibleProductionRun?.started_at);
  const latestRunFinishedLabel = visibleProductionRun?.finished_at ? compactDateTime(visibleProductionRun.finished_at) : null;
  const latestRunLine = visibleProductionRun?.run_id
    ? `Run ${latestRunStartedLabel} - ${statusLabel(visibleProductionRun.status)} - etapa ${latestRunStageLabel}${latestRunFinishedLabel ? ` - fim ${latestRunFinishedLabel}` : ''}`
    : lastProductionLine;
  const latestRunOutputLabel = visibleProductionRun?.run_id
    ? runOutputStatusLabel(visibleProductionRun)
    : 'output nao medido';
  const latestRunSegmentStateId = visibleProductionRun?.new_segment_state_run_id ?? visibleProductionRun?.segment_state_run_id ?? null;
  const latestRunSegmentStateLabel = latestRunSegmentStateId ? `segment-state #${latestRunSegmentStateId}` : 'sem novo segment-state';
  const latestRunTone = visibleProductionRun?.status === 'failed'
    ? (latestTerminalDiskFailure ? 'amber' : 'red')
    : visibleProductionRun?.status === 'completed'
      ? 'emerald'
      : runActive
        ? 'blue'
        : 'slate';
  const latestRunTitle = visibleProductionRun?.status === 'failed'
    ? (latestTerminalDiskFailure ? 'Espaço em disco insuficiente' : 'Execução falhou')
    : visibleProductionRun?.run_id
      ? `Run ${latestRunStartedLabel}`
      : 'Sem run recente';
  const latestRunDetail = visibleProductionRun?.status === 'failed'
    ? (latestRunFailureMessage || 'Falha sem mensagem detalhada.')
    : visibleProductionRun?.run_id
      ? latestRunLine
      : lastProductionLine;
  const latestRunDisk = visibleProductionRun?.disk_preflight ?? latestRunFailure.disk_preflight ?? diskPreflight;
  const nowMs = Date.now() + clockTick * 0;
  const runStartedMs = timestampMs(runStatus?.started_at ?? runStatus?.created_at);
  const runUpdatedMs = timestampMs(runStatus?.updated_at ?? runStatus?.last_event_at ?? runStatus?.finished_at ?? runStatus?.started_at);
  const stageStartedMs = timestampMs(currentStage?.started_at ?? currentStage?.updated_at ?? currentStage?.finished_at ?? runStatus?.updated_at);
  const runElapsed = runStartedMs ? durationLabel(nowMs - runStartedMs) : 'nao medido';
  const stageElapsed = stageStartedMs ? durationLabel(nowMs - stageStartedMs) : 'nao medido';
  const updateElapsed = runUpdatedMs ? durationLabel(nowMs - runUpdatedMs) : 'nao medido';
  const staleUpdateMs = runUpdatedMs ? nowMs - runUpdatedMs : null;
  const staleUpdateWarning = runActive && Number.isFinite(staleUpdateMs) && staleUpdateMs > 120000;
  const liveLogs = (Array.isArray(runStatus?.logs_tail) ? runStatus.logs_tail : [])
    .map(logLineText)
    .filter(Boolean)
    .slice(-5);
  const liveDiskPreflight = runStatus?.disk_preflight ?? diskPreflight ?? {};
  const sqliteBackupMode = liveDiskPreflight.sqlite_backup_mode ?? 'not_measured';
  const sqliteBackupLabel = sqliteBackupMode === 'metadata_only' ? 'metadata only' : valueOrPending(sqliteBackupMode);
  const currentStageLabel = currentStage?.label ?? runStatus?.current_label ?? runStatus?.current_stage ?? 'preparando';
  const liveModeLabel = runStatus?.mode === 'publication_apply_confirmed' || runStatus?.run_mode === 'publication'
    ? 'Producao publicavel'
    : runStatus?.mode === 'full_production_apply' || runStatus?.mode === 'evaluation_full_production' || runStatus?.run_mode === 'evaluation'
    ? 'Producao de avaliacao'
    : runStatus?.mode ?? runStatus?.run_mode ?? selectedModeInfo.label;
  const liveRunStatusTone = runStatusValue === 'failed'
    ? 'red'
    : runStatusValue === 'cancelled'
      ? 'amber'
      : runActive
        ? 'blue'
        : runStatusValue === 'completed'
          ? 'emerald'
          : 'slate';
  const integrityItems = [
    { label: 'source', value: integrity.source_status ?? 'pending_instrumentation', tone: integrity.source_status === 'clean' ? 'emerald' : 'slate' },
    { label: 'output', value: integrity.output_status ?? 'pending_instrumentation', tone: integrity.output_status?.includes?.('alter') ? 'amber' : 'slate' },
    { label: 'confirmations', value: integrity.confirmations_status ?? 'pending_instrumentation', tone: integrity.confirmations_status === 'aligned' ? 'emerald' : 'amber' },
    { label: 'needs_apply', value: compact(release.needs_apply), tone: release.needs_apply ? 'amber' : 'emerald' },
    { label: 'gate', value: gateText, tone: learning.can_start_production ? 'emerald' : 'red' },
  ];
  const terminalProblem = executionTerminalProblem;
  const executionPanelActive = runActive || refreshing || Boolean(modeActionNotice?.mode === effectiveSelectedMode && (terminalProblem || ['starting', 'running'].includes(executionStatusValue)));
  const executionPanelTitle = runActive
    ? 'Execucao em andamento'
    : refreshing
      ? 'Diagnostico em execucao'
    : executionStatusValue === 'failed' || executionStatusValue === 'blocked'
      ? 'Execucao interrompida'
      : executionStatusValue === 'completed' || executionStatusValue === 'refresh_completed'
        ? 'Execucao concluida'
        : displayExecutionTitle || 'Execucao preparada';
  const executionPanelCurrent = runActive
    ? currentStageLabel
    : modeActionNotice?.mode === 'evaluation' && currentActionStatus === 'completed'
      ? (runOutputChanged ? 'Concluida com alteracoes' : 'Concluida sem alteracoes')
      : modeActionNotice?.mode === 'evaluation' && currentActionStatus === 'failed'
        ? `Falha em ${failedStageLabel}`
    : modeActionNotice?.mode === 'publication'
      ? modeActionNotice?.body ?? selectedModeInfo.description
    : effectiveSelectedMode === 'diagnostic'
      ? refreshing ? 'Analisando candidatos e atualizando sugestões' : modeActionNotice?.title ?? selectedModeInfo.description
      : selectedModeInfo.description;

  return (
    <div className="flex h-full min-h-0 flex-col gap-2 pb-0">
      <Card className="p-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex min-w-0 items-center gap-3">
            <div className="grid h-9 w-9 place-items-center rounded-xl border border-emerald-400/25 bg-emerald-400/10 text-emerald-300">
              <Activity size={17} />
            </div>
            <div className="min-w-0">
              <h2 className="truncate text-lg font-black text-[var(--dash-text)]">CK3 PT-BR Release Control</h2>
              <p className="truncate text-xs text-[var(--dash-muted)]">
                Pos-release QA e hotfix seguro · Cache {cache.generated_at ? `atualizado em ${cache.generated_at}` : 'pendente'} · SQLite {cache.stale ? 'mudou desde o cache' : 'sincronizado'}
              </p>
            </div>
          </div>
          <div className="flex flex-wrap justify-end gap-1.5 text-[10px] font-bold">
            <Badge tone="blue">Segment-state #{release.latest_segment_state_run_id ?? '-'}</Badge>
            <Badge tone="violet">Ledger #{release.latest_ledger_run_id ?? '-'}</Badge>
            <Badge tone={cacheTone}>{cache.stale ? 'cache defasado' : 'cache atualizado'}</Badge>
            <Badge tone={cacheTone}>{cache.stale ? 'SQLite mudou' : 'SQLite sync'}</Badge>
          </div>
        </div>

        <div className="mt-2 flex flex-wrap items-center gap-1.5 rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] px-2.5 py-2 text-xs font-bold text-[var(--dash-muted)]">
          <span><span className="text-blue-400">{compact(release.total_segments)}</span> segmentos</span>
          <span className="text-[var(--dash-soft)]">·</span>
          <span>pacote <span className={operationallyClosed ? 'text-emerald-400' : 'text-amber-400'}>{operationallyClosed ? 'fechado' : 'aberto'}</span></span>
          <span className="text-[var(--dash-soft)]">·</span>
          <span><span className={release.pending_count ? 'text-amber-400' : 'text-emerald-400'}>{compact(release.pending_count)}</span> pendentes</span>
          <span className="text-[var(--dash-soft)]">·</span>
          <span>needs apply <span className={release.needs_apply ? 'text-amber-400' : 'text-emerald-400'}>{compact(release.needs_apply)}</span></span>
          <span className="text-[var(--dash-soft)]">·</span>
          <span className={qualityDebtActionable ? 'text-amber-400' : qualityDebt.status === 'clear' ? 'text-emerald-400' : 'text-blue-400'}>{qualityDebtHeaderLabel}</span>
          <span className="text-[var(--dash-soft)]">·</span>
          <span>provedores <span className={providerHealth.status === 'healthy' ? 'text-emerald-400' : 'text-amber-400'}>{compact(providerHealth.executed_provider_count ?? 0)}/{compact(providerHealth.provider_count ?? 0)}</span></span>
          <span className="text-[var(--dash-soft)]">·</span>
          <span>modo <span className="text-[var(--dash-text)]">{selectedModeInfo.shortLabel}</span></span>
        </div>

        <div className="hidden">
          <MetricTile title="Segmentos totais" value={compact(release.total_segments)} tone="blue" />
          <MetricTile title="Fechados" value={`${pct(release.closed_rate)} · ${compact(release.closed_count)}`} tone="emerald" />
          <MetricTile title="Pendências operacionais" value={compact(release.pending_count)} tone={release.pending_count ? 'amber' : 'emerald'} />
          <MetricTile title="Needs Apply" value={compact(release.needs_apply)} tone={release.needs_apply ? 'amber' : 'emerald'} />
          <MetricTile title="Modo atual" value={selectedModeInfo.label} tone={selectedModeInfo.tone} />
        </div>
      </Card>

      <div className="grid min-h-0 flex-1 gap-2 xl:grid-cols-[minmax(330px,0.5fr)_minmax(0,1.5fr)]">
        <Card className="dashboard-card-scroll flex min-h-0 flex-col overflow-y-auto p-2">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h3 className="text-sm font-black text-[var(--dash-text)]">Production Control</h3>
              <p className="mt-1 text-xs text-[var(--dash-muted)]">Escolha o tipo de execução antes de iniciar qualquer etapa.</p>
            </div>
            <Badge tone={selectedModeInfo.tone}>{selectedModeInfo.status}</Badge>
          </div>

          <div className="mt-2 flex flex-nowrap items-center gap-2.5">
            {releaseModes.map((mode) => (
              <button
                key={mode.id}
                type="button"
                aria-disabled={runActive || runStartPending}
                onClick={() => {
                  if (!runActive && !runStartPending) setSelectedMode(mode.id);
                }}
                className={cn(modeButtonClass(mode, effectiveSelectedMode === mode.id), (runActive || runStartPending) && 'cursor-not-allowed')}
                data-tooltip-title={runActive ? 'Execução em andamento' : mode.shortLabel}
                data-tooltip-description={runActive ? 'Aguarde a execução atual terminar.' : [mode.description, mode.warning].filter(Boolean).join(' ')}
                data-tooltip-meta={`Status: ${mode.status}`}
                aria-label={runActive ? 'Aguarde a execução atual terminar.' : modeTooltip(mode)}
              >
                <span
                  className={cn(
                    'pointer-events-none absolute inset-x-2 top-0 h-0.5 rounded-b-full bg-cyan-200 opacity-0 shadow-[0_0_12px_rgba(34,211,238,0.9)] transition',
                    effectiveSelectedMode === mode.id && 'opacity-100'
                  )}
                />
                <span className="shrink-0 drop-shadow-sm">{modeIcon(mode)}</span>
                <span
                  className={cn(
                    'absolute right-2.5 top-1/2 grid h-4 w-4 -translate-y-1/2 place-items-center rounded-full border shadow-sm',
                    mode.statusKind === 'blocked' ? 'border-red-100/40 bg-red-500 text-white' :
                      mode.statusKind === 'instrumented' ? 'border-amber-100/45 bg-amber-400 text-amber-950' :
                        mode.statusKind === 'unknown' ? 'border-slate-100/35 bg-slate-500 text-white' :
                          'border-emerald-100/45 bg-emerald-400 text-emerald-950'
                  )}
                >
                  {modeStatusIcon(mode)}
                </span>
              </button>
            ))}
          </div>

          <button
            onClick={runSelectedModeAction}
            disabled={modeButtonDisabled}
            title={`${selectedModeInfo.button}: ${selectedModeInfo.description} ${selectedModeInfo.warning}`}
            aria-label={selectedModeInfo.button}
            className={cn(
              'mt-2 inline-flex h-9 w-full items-center justify-center gap-2 rounded-xl px-4 text-sm font-black transition',
              !modeButtonDisabled ? 'bg-blue-600 text-white shadow-lg shadow-blue-600/20 hover:bg-blue-500' : 'bg-slate-500/10 text-[var(--dash-muted)]'
            )}
          >
            <Play size={18} /> {runActive ? (activeRunMode === 'publication' ? 'Executando publicavel...' : 'Executando avaliacao...') : refreshing ? 'Atualizando...' : startStatus === 'checking' ? 'Checando...' : effectiveSelectedMode === 'publication' && publicationCanApply ? 'Aplicar confirmados' : selectedModeInfo.actionLabel}
          </button>

          <div className="mt-2 flex h-[156px] flex-col justify-between overflow-hidden rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-2.5">
            {runActive ? (
              <>
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-[10px] font-black uppercase tracking-wide text-[var(--dash-muted)]">Modo real ativo</p>
                    <p className="line-clamp-1 text-sm font-black text-[var(--dash-text)]">Avaliação em execução</p>
                  </div>
                  <Badge tone="blue">{statusLabel(runStatus?.status)}</Badge>
                </div>
                <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-slate-500/20">
                  <div className="h-full rounded-full bg-blue-500 transition-all duration-500" style={{ width: `${Math.max(0, Math.min(100, runProgress))}%` }} />
                </div>
                <div className="mt-2 grid grid-cols-3 gap-1 text-[10px] font-bold">
                  <span className="truncate rounded-md bg-black/15 px-1.5 py-0.5" title={String(runStatus?.run_id ?? '-')}>run {runStatus?.run_id ?? '-'}</span>
                  <span className="truncate rounded-md bg-black/15 px-1.5 py-0.5" title={currentStageLabel}>etapa {currentStageLabel}</span>
                  <span className="rounded-md bg-black/15 px-1.5 py-0.5">progresso {Math.round(runProgress)}%</span>
                  <span className="rounded-md bg-black/15 px-1.5 py-0.5">total {runElapsed}</span>
                  <span className="rounded-md bg-black/15 px-1.5 py-0.5">etapa {stageElapsed}</span>
                  <span className={cn('rounded-md bg-black/15 px-1.5 py-0.5', staleUpdateWarning ? 'text-amber-300' : '')}>evento {updateElapsed}</span>
                </div>
              </>
            ) : (modeActionNotice || diagnosticExecutionActive) ? (
              <>
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-[10px] font-black uppercase tracking-wide text-[var(--dash-muted)]">Modo selecionado</p>
                  <p className="line-clamp-1 text-sm font-black text-[var(--dash-text)]">{displayExecutionTitle || selectedModeTitle}</p>
                </div>
                <Badge tone={executionTone}>{statusLabel(executionStatus ?? startStatus ?? selectedModeInfo.status)}</Badge>
              </div>
              <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-500/20">
                <div
                  className={cn('h-full rounded-full transition-all duration-500', executionTone === 'red' ? 'bg-red-500' : executionTone === 'emerald' ? 'bg-emerald-500' : 'bg-blue-500')}
                  style={{ width: `${Math.max(0, Math.min(100, executionProgress))}%` }}
                />
              </div>
              {executionSteps.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1">
                  {executionSteps.map((step) => (
                    <span
                      key={step.label}
                      title={step.detail || step.label}
                      className={cn('inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10px] font-bold', executionStepClass(step.status))}
                    >
                      <span className={cn('h-1.5 w-1.5 rounded-full', step.status === 'done' ? 'bg-emerald-400' : step.status === 'running' ? 'bg-blue-400 animate-pulse' : step.status === 'failed' ? 'bg-red-400' : 'bg-slate-400')} />
                      {step.label}
                    </span>
                  ))}
                </div>
              )}
              {(modeActionNotice?.mode === 'diagnostic' || diagnosticExecutionActive) && (
                <div className="mt-2 grid grid-cols-3 gap-1 text-[10px] font-bold">
                  <span className="rounded-md bg-black/15 px-1.5 py-0.5">total {diagnosticElapsedLabel}</span>
                  <span className="truncate rounded-md bg-black/15 px-1.5 py-0.5" title={diagnosticCurrentStepLabel}>etapa {diagnosticCurrentStepLabel}</span>
                  <span className="rounded-md bg-black/15 px-1.5 py-0.5">progresso {Math.round(executionProgress)}%</span>
                </div>
              )}
              {startError && <p className="mt-1 font-bold text-red-300">{startError}</p>}
              </>
            ) : (
              <>
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-[10px] font-black uppercase tracking-wide text-[var(--dash-muted)]">Modo selecionado</p>
                    <p className="line-clamp-1 text-sm font-black leading-snug text-[var(--dash-text)]">{selectedModeTitle}</p>
                  </div>
                  <Badge tone={selectedModeInfo.tone}>{selectedModeInfo.status}</Badge>
                </div>
                <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-500/20">
                  <div className={cn('h-full rounded-full', selectedModeInfo.tone === 'red' ? 'bg-red-500' : selectedModeInfo.tone === 'emerald' ? 'bg-emerald-500' : selectedModeInfo.tone === 'amber' ? 'bg-amber-500' : 'bg-blue-500')} style={{ width: '0%' }} />
                </div>
                <div className="mt-2 flex flex-wrap gap-1">
                  {selectedModePreviewSteps.map((step) => (
                    <span
                      key={step.label}
                      title={step.detail || step.label}
                      className={cn('inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10px] font-bold', executionStepClass(step.status))}
                    >
                      <span className={cn('h-1.5 w-1.5 rounded-full', step.status === 'done' ? 'bg-emerald-400' : step.status === 'running' ? 'bg-blue-400 animate-pulse' : step.status === 'failed' ? 'bg-red-400' : 'bg-slate-400')} />
                      {step.label}
                    </span>
                  ))}
                </div>
              </>
            )}
          </div>

          <div className="hidden">
            {gateCards.map((gate) => (
              <div key={gate.title} className="rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-2.5">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <h4 className="text-xs font-black text-[var(--dash-text)]">{gate.title}</h4>
                    <p className="mt-0.5 truncate text-[10px] text-[var(--dash-soft)]" title={gate.source}>{gate.source}</p>
                  </div>
                  <Badge tone={gate.tone}>{gate.status}</Badge>
                </div>
                <p className="mt-2 line-clamp-2 text-xs font-bold text-[var(--dash-muted)]">
                  {gate.reasons.length ? gate.reasons.join(', ') : gate.tone === 'emerald' ? 'sem bloqueador' : 'not_measured'}
                </p>
              </div>
            ))}
          </div>

          <div className="mt-2 rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-2.5">
            <div className="flex items-center justify-between gap-2">
              <h4 className="text-xs font-black text-[var(--dash-text)]">Estado de produção</h4>
              <Badge tone={productionPublicationTone}>{productionPublicationStatus.toLowerCase()}</Badge>
            </div>
            <div className="mt-2 grid grid-cols-2 gap-1.5">
              {productionStateRows.map((item) => (
                <div key={item.label} className="min-w-0 rounded-lg border border-[var(--dash-border)] bg-[var(--dash-card)] px-2 py-1.5">
                  <p className="text-[9px] font-black uppercase tracking-wide text-[var(--dash-muted)]">{item.label}</p>
                  <p className={cn('mt-0.5 truncate text-xs font-black', item.tone === 'emerald' ? 'text-emerald-400' : item.tone === 'red' ? 'text-red-400' : item.tone === 'amber' ? 'text-amber-400' : item.tone === 'blue' ? 'text-blue-400' : 'text-[var(--dash-text)]')} title={String(item.value)}>{String(item.value)}</p>
                </div>
              ))}
            </div>
            <p className="mt-2 line-clamp-2 text-[10px] font-bold text-[var(--dash-muted)]">Motivos: {blockingSummary}</p>
          </div>

          <div className="hidden">
            {releaseReadinessItems.map((item) => (
              <MetricTile key={item.label} title={item.label} value={item.value} tone={item.tone} />
            ))}
          </div>

          <div className="hidden">
            <div className="rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-2.5">
              <h4 className="text-[10px] font-black uppercase tracking-wide text-[var(--dash-muted)]">Delta desde producao</h4>
              {deltaAvailable ? (
                <div className="mt-1 grid grid-cols-3 gap-1.5 text-xs">
                  <div>
                    <p className="text-[var(--dash-muted)]">Fechados</p>
                    <p className="font-black text-emerald-400">+{compact(deltaClosed)}</p>
                  </div>
                  <div>
                    <p className="text-[var(--dash-muted)]">Pendentes</p>
                    <p className={cn('font-black', deltaPending <= 0 ? 'text-emerald-400' : 'text-amber-400')}>{deltaPending > 0 ? '+' : ''}{compact(deltaPending)}</p>
                  </div>
                  <div>
                    <p className="text-[var(--dash-muted)]">Apply</p>
                    <p className={cn('font-black', deltaNeedsApply === 0 ? 'text-emerald-400' : 'text-amber-400')}>{deltaNeedsApply > 0 ? '+' : ''}{compact(deltaNeedsApply)}</p>
                  </div>
                </div>
              ) : (
                <p className="mt-1 text-xs font-bold text-[var(--dash-muted)]">{productionDelta.reason ?? 'pending_instrumentation'}</p>
              )}
              <p className="mt-1 truncate text-[10px] text-[var(--dash-soft)]">Ultima producao: {productionDelta.last_production_run_id ?? runStatus?.run_id ?? '-'}</p>
            </div>

            <div className="rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-2.5">
              <h4 className="text-[10px] font-black uppercase tracking-wide text-[var(--dash-muted)]">Controle de publicação</h4>
              <div className="mt-1 flex flex-wrap gap-1.5">
                <Badge tone={evaluationAllowed ? 'emerald' : 'red'}>Avaliação {evaluationAllowed ? 'liberada' : 'bloqueada'}</Badge>
                <Badge tone={publishTone}>Publicação {publishStatus}</Badge>
              </div>
              <p className="mt-1 truncate text-[10px] text-[var(--dash-soft)]">Preflight: {compactDateTime(gameUpdate.latest_preflight ?? safety.preflight?.generated_at)}</p>
            </div>
          </div>

          <div className={cn(
            'mt-2 rounded-xl border p-2.5',
            visibleProductionRun?.status === 'failed'
              ? latestTerminalDiskFailure
                ? 'border-amber-400/35 bg-amber-500/10 text-amber-100'
                : 'border-red-400/35 bg-red-500/10 text-red-100'
              : 'border-[var(--dash-border)] bg-[var(--dash-subtle)] text-[var(--dash-text)]'
          )}>
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <p className="text-[10px] font-black uppercase tracking-wide text-[var(--dash-muted)]">Status da última run</p>
                <p className="mt-0.5 line-clamp-1 text-sm font-black" title={latestRunDetail}>{latestRunTitle}</p>
              </div>
              <Badge tone={latestRunTone}>{statusLabel(visibleProductionRun?.status ?? 'idle')}</Badge>
            </div>
            <p className="mt-1 line-clamp-1 text-xs font-bold" title={latestRunDetail}>{latestRunDetail}</p>
            <div className="mt-1.5 grid grid-cols-2 gap-1 text-[10px] font-bold xl:grid-cols-4">
              <span className="rounded-md bg-black/15 px-1.5 py-0.5" title={visibleProductionRun?.run_id ? `Run id ${visibleProductionRun.run_id}` : undefined}>Run {visibleProductionRun?.run_id ? latestRunStartedLabel : '-'}</span>
              <span className="rounded-md bg-black/15 px-1.5 py-0.5">{visibleProductionRun?.report_path ? 'relatório disponível' : 'relatório não medido'}</span>
              <span className="rounded-md bg-black/15 px-1.5 py-0.5">{latestRunOutputLabel}</span>
              <span className="rounded-md bg-black/15 px-1.5 py-0.5">{latestRunSegmentStateLabel}</span>
            </div>
            {latestTerminalDiskFailure && (
              <div className="mt-1.5 space-y-1 text-[10px] font-black">
                <div className="grid grid-cols-3 gap-1">
                  <span className="rounded-md bg-black/15 px-1.5 py-0.5">Requerido {fmtBytes(latestRunDisk?.required_free_bytes)}</span>
                  <span className="rounded-md bg-black/15 px-1.5 py-0.5">Livre {fmtBytes(latestRunDisk?.free_bytes)}</span>
                  <span className="rounded-md bg-black/15 px-1.5 py-0.5">Faltam {fmtBytes(latestRunDisk?.missing_bytes)}</span>
                </div>
                <p className="line-clamp-1 text-amber-200" title={latestRunDisk?.suggestion || 'Sugestão: limpar ou mover snapshots antigos.'}>
                  {latestRunDisk?.suggestion || 'Sugestão: limpar ou mover snapshots antigos.'}
                </p>
              </div>
            )}
            {visibleProductionRun?.report_path && (
              <details className="mt-1 text-[10px] text-[var(--dash-muted)]">
                <summary className="cursor-pointer font-bold text-blue-400">detalhes</summary>
                <p className="mt-1 truncate" title={visibleProductionRun.report_path}>Relatório: {visibleProductionRun.report_path}</p>
              </details>
            )}
          </div>

          <div className="hidden">
            <h4 className="text-xs font-black uppercase tracking-wide text-[var(--dash-muted)]">Último resultado</h4>
            <p className="mt-1 text-sm font-bold text-[var(--dash-text)]">{runStatus?.message ?? 'Nenhuma run ativa ou recente carregada.'}</p>
            <div className="mt-2 space-y-0.5 text-xs text-[var(--dash-muted)]">
              <p>Segment-state: <span className="font-bold text-[var(--dash-text)]">#{release.latest_segment_state_run_id ?? '-'}</span></p>
              <p>Ledger: <span className="font-bold text-[var(--dash-text)]">#{release.latest_ledger_run_id ?? '-'}</span></p>
              <p>Output coverage: <span className="font-bold text-[var(--dash-text)]">{pct(release.output_coverage)}</span></p>
              {visibleProductionRun?.report_path && <p className="truncate">Relatório: <span className="font-bold text-[var(--dash-text)]">{visibleProductionRun.report_path}</span></p>}
            </div>
          </div>
        </Card>

        <Card className="flex min-h-0 overflow-hidden p-2.5">
          {executionPanelActive ? (
            <div className="flex min-h-0 w-full flex-col">
              <div className="mb-2 flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <h3 className="text-sm font-black text-[var(--dash-text)]">{executionPanelTitle}</h3>
                  <p className="text-xs text-[var(--dash-muted)]">
                    {runActive ? 'Agora' : 'Resultado'}: <span className="font-black text-blue-400">{executionPanelCurrent}</span>
                  </p>
                </div>
                <div className="flex flex-wrap justify-end gap-1.5 text-[10px] font-bold">
                  <Badge tone="blue">{currentActionRun?.run_id ? `run ${currentActionRun.run_id}` : selectedModeInfo.shortLabel}</Badge>
                  <Badge tone={runActive ? liveRunStatusTone : executionTone}>{runActive ? statusLabel(runStatus?.status) : statusLabel(executionStatus ?? startStatus ?? 'idle')}</Badge>
                  <Badge tone={runOutputBadgeTone}>{runOutputLabel}</Badge>
                </div>
              </div>

              <div className="mb-2">
                <div className="h-1.5 overflow-hidden rounded-full bg-slate-500/20">
                  <div
                    className={cn('h-full rounded-full transition-all duration-500', executionTone === 'red' ? 'bg-red-500' : executionTone === 'emerald' ? 'bg-emerald-500' : executionTone === 'amber' ? 'bg-amber-500' : 'bg-blue-500')}
                    style={{ width: `${Math.max(0, Math.min(100, runActive ? runProgress : executionProgress))}%` }}
                  />
                </div>
                <div className="mt-1 flex items-center justify-between gap-2 text-[10px] font-bold text-[var(--dash-muted)]">
                  <span>Modo {runActive ? liveModeLabel : selectedModeInfo.label}</span>
                  <span>{Math.round(runActive ? runProgress : executionProgress)}%</span>
                </div>
              </div>

              {evaluationPipelineVisible ? (
              <div className="mt-2 grid min-h-0 flex-1 gap-2 xl:grid-cols-2">
                {displayPhases.map((phase, index) => (
                  <div
                    key={phase.id}
                    className={cn(
                      'flex min-h-0 flex-col rounded-xl border bg-[var(--dash-subtle)] p-2',
                      phase.status === 'running' ? 'border-blue-400/35 shadow-[0_0_0_1px_rgba(59,130,246,0.12)]' : 'border-[var(--dash-border)]'
                    )}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <p className="text-[9px] font-black uppercase tracking-wide text-[var(--dash-muted)]">Fase {index + 1}/4</p>
                        <h4 className="truncate text-sm font-black text-[var(--dash-text)]">{phase.title}</h4>
                        <p className="mt-1 line-clamp-2 text-[11px] leading-snug text-[var(--dash-muted)]">{phase.purpose}</p>
                      </div>
                      <Badge tone={statusTone(phase.status)}>{statusLabel(phase.status)}</Badge>
                    </div>
                    <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-slate-500/15">
                      <div
                        className={cn(
                          'h-full rounded-full transition-all duration-500',
                          phase.status === 'failed' ? 'bg-red-500' : phase.status === 'running' ? 'bg-blue-500' : phase.status === 'done' ? 'bg-emerald-500' : 'bg-slate-500'
                        )}
                        style={{ width: `${phase.progress}%` }}
                      />
                    </div>
                    <div className="dashboard-card-scroll mt-2 min-h-0 flex-1 overflow-auto pr-1">
                      <div className="flex flex-wrap gap-1">
                        {phase.stages.map((stage) => (
                          <span
                            key={stage.id}
                            title={[
                              `${stage.label ?? stage.id}: ${statusLabel(stage.status)}`,
                              stage.started_at ? `Inicio: ${stage.started_at}` : null,
                              stage.finished_at ? `Fim: ${stage.finished_at}` : null,
                              stage.started_at && stage.finished_at ? `Duracao: ${durationLabel(timestampMs(stage.finished_at) - timestampMs(stage.started_at))}` : null,
                              stage.log_line_count !== undefined ? `Logs: ${stage.log_line_count}` : null,
                            ].filter(Boolean).join('\n')}
                            className={cn('inline-flex max-w-full items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10px] font-bold', executionStepClass(stage.status))}
                          >
                            <span className={cn(
                              'h-1.5 w-1.5 shrink-0 rounded-full',
                              stage.status === 'done' ? 'bg-emerald-400' :
                                stage.status === 'running' ? 'animate-pulse bg-blue-400' :
                                  stage.status === 'failed' ? 'bg-red-400' :
                                    stage.status === 'cancelled' ? 'bg-amber-400' : 'bg-slate-400'
                            )} />
                            <span className="truncate">{stage.label ?? stage.id}</span>
                          </span>
                        ))}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
              ) : (
              <div className="mt-2 grid min-h-0 flex-1 content-start gap-2 xl:grid-cols-2">
                {executionSteps.map((step, index) => (
                  <div key={step.label} className={cn('rounded-xl border bg-[var(--dash-subtle)] p-3', executionStepClass(step.status))}>
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <p className="text-[9px] font-black uppercase tracking-wide opacity-70">Etapa {index + 1}/{executionSteps.length}</p>
                        <h4 className="mt-1 truncate text-sm font-black">{step.label}</h4>
                        {step.detail && <p className="mt-1 line-clamp-2 text-xs opacity-80">{step.detail}</p>}
                      </div>
                      <span className={cn(
                        'mt-1 h-2 w-2 shrink-0 rounded-full',
                        step.status === 'done' ? 'bg-emerald-400' :
                          step.status === 'running' ? 'animate-pulse bg-blue-400' :
                            step.status === 'failed' ? 'bg-red-400' :
                              step.status === 'cancelled' ? 'bg-amber-400' : 'bg-slate-400'
                      )} />
                    </div>
                  </div>
                ))}
              </div>
              )}

              {evaluationPipelineVisible ? (
              <div className="mt-2 grid gap-2 xl:grid-cols-2">
                <div className="rounded-xl border border-emerald-400/20 bg-emerald-500/10 p-2">
                  <div className="flex items-center gap-2 text-[10px] font-black uppercase tracking-wide text-emerald-300">
                    <Database size={13} /> Snapshot / preflight
                  </div>
                  <div className="mt-1.5 grid grid-cols-3 gap-1 text-[10px] font-bold">
                    <span className="rounded-md bg-black/10 px-1.5 py-0.5">SQLite: {sqliteBackupLabel}</span>
                    <span className="rounded-md bg-black/10 px-1.5 py-0.5">Snapshot: {fmtBytes(liveDiskPreflight.estimated_snapshot_bytes)}</span>
                    <span className="rounded-md bg-black/10 px-1.5 py-0.5">DB não duplicado</span>
                  </div>
                </div>
                <div className="rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-2">
                  <div className="flex items-center gap-2 text-[10px] font-black uppercase tracking-wide text-[var(--dash-muted)]">
                    <TerminalSquare size={13} /> Logs recentes
                  </div>
                  <div className="mt-1.5 space-y-0.5 text-[10px] font-bold text-[var(--dash-muted)]">
                    {liveLogs.length ? liveLogs.map((line, index) => (
                      <p key={`${index}-${line.slice(0, 16)}`} className="truncate" title={line}>{line}</p>
                    )) : (
                      <p>Aguardando eventos da etapa atual.</p>
                    )}
                  </div>
                </div>
              </div>
              ) : (
              <div className="mt-2 rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-2">
                <div className="flex items-center gap-2 text-[10px] font-black uppercase tracking-wide text-[var(--dash-muted)]">
                  <TerminalSquare size={13} /> Resumo da execucao
                </div>
                <p className="mt-1.5 line-clamp-2 text-xs font-bold text-[var(--dash-muted)]">
                  {displayExecutionDetailText || selectedModeInfo.warning || 'Aguardando execucao do modo selecionado.'}
                </p>
              </div>
              )}
            </div>
          ) : (
          <div className="flex min-h-0 w-full flex-col">
          <div className="mb-2 flex items-center justify-between gap-3">
            <div>
              <h3 className="text-sm font-black text-[var(--dash-text)]">Feedback e pos-release</h3>
              <p className="text-xs text-[var(--dash-muted)]">Fila visual, hotfix protegido e preparo para update source/output.</p>
            </div>
            <Badge tone={release.needs_apply ? 'amber' : 'emerald'}>{release.needs_apply ? 'hotfix pendente' : 'limpo'}</Badge>
          </div>

          <div className="mt-2 flex min-h-0 flex-1">
            <div className="flex min-h-0 w-full flex-1 flex-col rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-2.5">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <h4 className="text-sm font-black text-[var(--dash-text)]">
                    {postReleaseView === 'feedback'
                      ? 'Feedback visual / hotfix'
                      : postReleaseView === 'package'
                        ? 'Pacote old vs output'
                      : postReleaseView === 'promotions'
                        ? 'Promocoes'
                      : postReleaseView === 'regressions'
                          ? 'Auditoria do score bruto'
                      : postReleaseView === 'discovery'
                          ? 'Familias de qualidade descobertas'
                      : postReleaseView === 'proposals'
                          ? 'Propostas assistidas de provedor'
                      : postReleaseView === 'providers'
                          ? 'Cobertura e produtividade dos provedores'
                      : postReleaseView === 'calibration'
                          ? calibrationPolicyDecision === 'skip' && hasCompletedCalibrationHistory
                            ? 'Calibracao concluida'
                            : calibrationPolicyDecision === 'skip'
                            ? 'Calibracao dispensada pela politica'
                            : calibrationPolicyDecision === 'sample'
                              ? 'Amostra pairwise de calibracao'
                              : 'Revisao pairwise de calibracao'
                          : postReleaseView === 'apply'
                            ? 'Needs apply'
                            : postReleaseView === 'new'
                              ? 'Novos segmentos'
                              : postReleaseView === 'low_score'
                                ? 'Baixo score'
                                : 'Pendentes'}
                  </h4>
                  <p className="text-xs text-[var(--dash-muted)]">
                    {postReleaseView === 'feedback'
                      ? 'Somente feedback aberto ou aguardando acao. Itens fechados e holds aceitos ficam arquivados.'
                      : postReleaseView === 'package'
                        ? `Comparativo final fechado entre source\\spanish_old e output\\spanish. ${packageScoreSummaryLabel}; ${packageCoverageLabel}. ${changedCohortScoreLabel}. Diferencas brutas: ${compact(rawOutputDiffCount)}; fora do pacote: ${compact(packageExcludedCount)}.`
                      : postReleaseView === 'promotions'
                        ? 'Promocoes contra o old que passaram no gate e podem virar apply confirmado.'
                      : postReleaseView === 'regressions'
                          ? `${compact(rawScoreRegressionCount)} variacoes brutas auditaveis; ${compact(effectiveScoreRegressionCount)} regressões efetivas no pacote; ${compact(reviewedRawScoreRegressionCount)} ja revisadas/calibradas; ${compact(unresolvedRawScoreRegressionCount)} ainda sem resolucao.`
                      : postReleaseView === 'discovery'
                          ? `Mineracao do pacote inteiro na epoch #${patternDiscovery.quality_epoch_id ?? '-'}. ${compact(patternDiscovery.evidence_segment_count ?? 0)} segmentos com evidencia; ${compact(patternDiscovery.ignored_score_only_count ?? 0)} casos de score baixo permaneceram apenas informativos.`
                      : postReleaseView === 'proposals'
                          ? `Rascunhos desabilitados gerados a partir da descoberta #${providerProposals.discovery_run_id ?? '-'}. Cada proposta exige implementacao deterministica, shadow integral, invariantes e testes de fronteira antes de virar provedor.`
                      : postReleaseView === 'providers'
                          ? `Funil persistido no SQLite para a epoch #${providerHealth.quality_epoch_id ?? '-'}. Casos inspecionados viram evidência somente quando passam pelos filtros determinísticos e pelo gate pareado.`
                      : postReleaseView === 'calibration'
                          ? calibrationPolicyDecision === 'skip' && hasCompletedCalibrationHistory
                            ? `A epoch #${calibrationReview.quality_epoch_id ?? '-'} ja foi calibrada. A politica atual dispensou uma nova amostra (${calibrationPolicySummary}), mas a fila #${calibrationReview.run_id} e suas decisoes permanecem visiveis.`
                            : calibrationPolicyDecision === 'skip'
                            ? `A epoch #${calibrationReview.quality_epoch_id ?? '-'} nao precisa de revisao manual: ${calibrationPolicySummary}. A decisao ficou registrada sem alterar score ou output.`
                            : `Politica ${calibrationPolicyDecision === 'sample' ? 'por amostra' : 'obrigatoria'} na epoch #${calibrationReview.quality_epoch_id ?? '-'}. Motivos: ${calibrationPolicySummary}. Prioritarios: ${compact(calibrationReview.priority_count ?? 0)}; controles: ${compact(Number(calibrationReview.positive_control_count ?? 0) + Number(calibrationReview.negative_control_count ?? 0))}.`
                          : postReleaseView === 'apply'
                            ? 'Confirmados cujo output atual ainda difere do texto aprovado e precisa de aplicacao protegida.'
                            : postReleaseView === 'new'
                              ? 'Segmentos sem equivalente no old, ordenados por score novo.'
                      : postReleaseView === 'low_score'
                                ? `Baixo score nao significa erro automaticamente. ${compact(lowScoreActionable)} acionaveis (${Number(lowScoreCohorts.actionable_share_pct ?? 0).toLocaleString('pt-BR', { maximumFractionDigits: 2 })}% da fila); ${compact(lowScoreInformational)} informativos; ${compact(lowScoreUnexplained)} sem evidencia especifica. ${qualityBandsLabel}.`
                                : 'Pendencias operacionais do segment-state: o que ainda nao fechou e precisa explicar o motivo.'}
                  </p>
                </div>
                <Badge tone={diffReview.instrumented || feedbackRows.length ? 'blue' : 'slate'}>
                  {postReleaseView === 'feedback'
                    ? feedbackRows.length ? `${feedbackRows.length} ativos` : archivedFeedbackItems.length ? 'fila limpa' : 'nao instrumentado'
                    : postReleaseView === 'package'
                      ? packageScoreSummaryLabel
                    : postReleaseView === 'calibration'
                      ? calibrationPolicyDecision === 'skip' && hasCompletedCalibrationHistory
                        ? `fila #${calibrationReview.run_id} - ${compact(calibrationReview.decided_count ?? 0)} decisoes preservadas`
                        : calibrationPolicyDecision === 'skip'
                        ? 'dispensada'
                        : calibrationReview.consumption_status === 'consumed'
                        ? `fila #${calibrationReview.run_id ?? '-'} - ${compact(calibrationReview.consumed_count ?? 0)} consumidos`
                        : `fila #${calibrationReview.run_id ?? '-'} - ${compact(calibrationReview.pending_count ?? 0)} pendentes`
                    : postReleaseView === 'discovery'
                      ? `fila #${patternDiscovery.run_id ?? '-'} - ${compact(patternDiscovery.family_count ?? 0)} familias`
                    : postReleaseView === 'proposals'
                      ? `geracao #${providerProposals.run_id ?? '-'} - ${compact(proposalDraftCount)} rascunhos`
                    : postReleaseView === 'providers'
                      ? `${compact(providerHealth.executed_provider_count ?? 0)}/${compact(providerHealth.provider_count ?? 0)} executados - score #${providerHealth.score_run_id ?? '-'}`
                    : diffReview.old_score_run_id
                      ? `score #${diffReview.old_score_run_id} -> #${diffReview.score_run_id ?? '-'}`
                      : `score #${diffReview.score_run_id ?? '-'}`}
                </Badge>
              </div>
              {postReleaseView === 'low_score' ? (
                <div className="mt-2 flex flex-wrap items-center gap-1.5">
                  <Badge tone="red">Defeito explicito {compact(lowScoreCohorts.explicit_text_issue ?? 0)}</Badge>
                  <Badge tone="amber">Bloqueio estrutural {compact(lowScoreCohorts.structural_block_without_issue ?? 0)}</Badge>
                  <Badge tone="emerald">Seguro deterministico {compact(lowScoreCohorts.deterministic_safe_but_low_score ?? 0)}</Badge>
                  <Badge tone="blue">Texto preservado {compact(lowScoreCohorts.unchanged_or_preserved_text ?? 0)}</Badge>
                  <Badge tone="slate">Sem evidencia {compact(lowScoreUnexplained)}</Badge>
                </div>
              ) : null}
              {postReleaseView === 'discovery' ? (
                <div className="mt-2 flex flex-wrap items-center gap-1.5">
                  <Badge tone="amber">Novas {compact(patternDiscovery.new_family_count ?? 0)}</Badge>
                  <Badge tone="blue">Recorrentes {compact(patternDiscovery.recurring_family_count ?? 0)}</Badge>
                  <Badge tone="emerald">Cobertas {compact(patternDiscovery.covered_family_count ?? 0)}</Badge>
                  <Badge tone="slate">Em observacao {compact(Number(patternDiscovery.family_count ?? 0) - Number(patternDiscovery.actionable_family_count ?? 0) - Number(patternDiscovery.covered_family_count ?? 0))}</Badge>
                </div>
              ) : null}
              {postReleaseView === 'proposals' ? (
                <div className="mt-2 flex flex-wrap items-center gap-1.5">
                  <Badge tone={proposalDraftCount ? 'amber' : 'emerald'}>Rascunhos {compact(proposalDraftCount)}</Badge>
                  <Badge tone="blue">Positivos {compact(providerProposals.positive_case_count ?? 0)}</Badge>
                  <Badge tone="slate">Negativos {compact(providerProposals.negative_case_count ?? 0)}</Badge>
                  <Badge tone="emerald">Fronteira {compact(providerProposals.boundary_case_count ?? 0)}</Badge>
                  <Badge tone="slate">Escrita no output 0</Badge>
                </div>
              ) : null}
              {postReleaseView === 'providers' ? (
                <div className="mt-2 flex flex-wrap items-center gap-1.5">
                  <Badge tone={providerHealth.status === 'healthy' ? 'emerald' : 'amber'}>Executados {compact(providerHealth.executed_provider_count ?? 0)}/{compact(providerHealth.provider_count ?? 0)}</Badge>
                  <Badge tone="blue">Inspecionados {compact(providerHealth.inspected_count ?? 0)}</Badge>
                  <Badge tone={Number(providerHealth.shadow_eligible_count ?? 0) ? 'amber' : 'emerald'}>Elegíveis {compact(providerHealth.shadow_eligible_count ?? 0)}</Badge>
                  <Badge tone={Number(providerHealth.promotion_ready_count ?? 0) ? 'blue' : 'slate'}>Promoções {compact(providerHealth.promotion_ready_count ?? 0)}</Badge>
                  <Badge tone={Number(providerHealth.uncovered_actionable_family_count ?? 0) ? 'amber' : 'emerald'}>Sem provedor {compact(providerHealth.uncovered_actionable_family_count ?? 0)}</Badge>
                </div>
              ) : null}
              <div className="mt-3 flex min-w-0 items-center gap-1">
                <button
                  type="button"
                  onClick={() => navigateReviewTabs(-1)}
                  className="grid h-9 w-7 shrink-0 place-items-center rounded-lg border border-[var(--dash-border)] bg-[var(--dash-card)] text-[var(--dash-muted)] transition hover:-translate-y-0.5 hover:border-cyan-300/50 hover:text-cyan-200 focus:outline-none focus:ring-2 focus:ring-cyan-300/70"
                  aria-label={`Relatório anterior: ${previousReviewTab.label}`}
                  title={`Relatório anterior: ${previousReviewTab.label}`}
                >
                  <ChevronLeft size={15} />
                </button>
                <div
                  ref={reviewTabsRef}
                  role="tablist"
                  aria-label="Relatórios de pós-release"
                  className="report-tab-strip dashboard-card-scroll flex min-w-0 flex-1 snap-x snap-mandatory flex-nowrap gap-1.5 overflow-x-auto overflow-y-hidden pb-1.5 pt-1.5"
                >
                  {reviewTabs.map((tab) => (
                    <button
                      key={tab.id}
                      type="button"
                      role="tab"
                      data-review-tab-id={tab.id}
                      aria-selected={postReleaseView === tab.id}
                      onClick={() => selectReviewTab(tab.id)}
                      className={reviewTabClass(tab, postReleaseView === tab.id)}
                    >
                      {tab.label}
                      <Badge tone={tab.tone}>{compact(tab.count)}</Badge>
                    </button>
                  ))}
                </div>
                <button
                  type="button"
                  onClick={() => navigateReviewTabs(1)}
                  className="grid h-9 w-7 shrink-0 place-items-center rounded-lg border border-[var(--dash-border)] bg-[var(--dash-card)] text-[var(--dash-muted)] transition hover:-translate-y-0.5 hover:border-cyan-300/50 hover:text-cyan-200 focus:outline-none focus:ring-2 focus:ring-cyan-300/70"
                  aria-label={`Próximo relatório: ${nextReviewTab.label}`}
                  title={`Próximo relatório: ${nextReviewTab.label}`}
                >
                  <ChevronRight size={15} />
                </button>
              </div>
              <div className="dashboard-card-scroll mt-2 min-h-0 flex-1 overflow-auto rounded-lg border border-[var(--dash-border)]">
                {postReleaseView === 'feedback' ? (
                  <table className="w-full min-w-[760px] text-center text-xs">
                    <thead className="sticky top-0 bg-[var(--dash-card)] text-[10px] uppercase tracking-wide text-[var(--dash-muted)]">
                      <tr>
                        <th className="px-2 py-2">ID</th>
                        <th className="px-2 py-2 text-left">Evidencia</th>
                        <th className="px-2 py-2">Categoria</th>
                        <th className="px-2 py-2">Segmentos</th>
                        <th className="px-2 py-2">Status</th>
                        <th className="px-2 py-2">Sev.</th>
                        <th className="px-2 py-2 text-left">Proxima acao</th>
                      </tr>
                    </thead>
                    <tbody>
                      {feedbackRows.length ? feedbackRows.slice(0, 24).map((item) => (
                        <tr key={item.row_id} className="border-t border-[var(--dash-border)] text-[var(--dash-text)]">
                          <td className="max-w-[220px] truncate px-2 py-2 font-bold" title={item.id ?? '-'}>{item.id ?? '-'}</td>
                          <td className="max-w-[360px] truncate px-2 py-2 text-left" title={item.observed_text}>{item.observed_text}</td>
                          <td className="max-w-[140px] truncate px-2 py-2" title={item.category}>{item.category}</td>
                          <td className="px-2 py-2">{item.segment_label}</td>
                          <td className="px-2 py-2"><Badge tone={item.status === 'closed' ? 'emerald' : item.status === 'accepted_hold' || item.status === 'hold' ? 'blue' : 'amber'}>{item.status ?? 'nao medido'}</Badge></td>
                          <td className="px-2 py-2">{item.visual_severity}</td>
                          <td className="max-w-[220px] truncate px-2 py-2 text-left" title={item.next_action}>{item.next_action}</td>
                        </tr>
                      )) : (
                        <tr>
                          <td colSpan={7} className="px-3 py-5 text-center font-bold text-[var(--dash-muted)]">
                            Nenhum feedback ativo. Itens fechados/aplicados ficam fora desta fila.
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                ) : postReleaseView === 'promotions' || postReleaseView === 'package' ? (
                  <table className="w-full min-w-[1340px] text-center text-xs">
                    <thead className="sticky top-0 bg-[var(--dash-card)] text-[10px] uppercase tracking-wide text-[var(--dash-muted)]">
                      <tr>
                        <th className="px-2 py-2">Segmento</th>
                        <th className="px-2 py-2 text-left">Arquivo / chave</th>
                        <th className="px-2 py-2 text-left">Old</th>
                        <th className="px-2 py-2 text-left">Output</th>
                        <th className="px-2 py-2">Score old</th>
                        <th className="px-2 py-2">Score novo</th>
                        <th className="px-2 py-2">Delta</th>
                        <th className="px-2 py-2">Integridade</th>
                        <th className="px-2 py-2">Gate</th>
                        <th className="px-2 py-2">Estado</th>
                      </tr>
                    </thead>
                    <tbody>
                      {comparisonSegments.length ? comparisonSegments.slice(0, 80).map((item) => (
                        <tr key={`${postReleaseView}-${item.segment_id}`} className="border-t border-[var(--dash-border)] text-[var(--dash-text)]">
                          <td className="px-2 py-2 font-bold text-[var(--dash-muted)]">{item.segment_id}</td>
                          <td className="max-w-[220px] truncate px-2 py-2 text-left" title={`${item.relative_path} :: ${item.source_key}`}>{item.relative_path} :: <span className="font-bold">{item.source_key}</span></td>
                          <td className="max-w-[260px] truncate px-2 py-2 text-left" title={item.old_text ?? ''}>{item.old_text ?? '-'}</td>
                          <td className="max-w-[260px] truncate px-2 py-2 text-left" title={item.output_text ?? ''}>{item.output_text ?? '-'}</td>
                          <td className="px-2 py-2"><Badge tone={scoreTone(segmentOldScore(item))}>{scoreLabel(segmentOldScore(item))}</Badge></td>
                          <td className="px-2 py-2" title={scoreCellTitle(item)}><Badge tone={scoreTone(segmentNewScore(item))}>{scoreLabel(segmentNewScore(item))}</Badge></td>
                          <td className="px-2 py-2" title={scoreCellTitle(item)}><Badge tone={scoreDeltaTone(item)}>{scoreDeltaLabel(item)}</Badge></td>
                          <td className="max-w-[180px] truncate px-2 py-2" title={integrityTitle(item)}><Badge tone={integrityTone(item)}>{integrityLabel(item)}</Badge></td>
                          <td className="max-w-[170px] truncate px-2 py-2" title={item.promotion_gate ?? ''}>{item.promotion_gate ?? 'nao medido'}</td>
                          <td className="max-w-[180px] truncate px-2 py-2" title={item.final_state ?? item.score_action ?? ''}>{item.final_state ?? item.score_action ?? 'nao medido'}</td>
                        </tr>
                      )) : (
                        <tr><td colSpan={10} className="px-3 py-5 text-center font-bold text-[var(--dash-muted)]">{postReleaseView === 'package' ? 'Nenhuma diferenca entre old e output medida.' : 'Nenhuma promocao contra o old medida.'}</td></tr>
                      )}
                    </tbody>
                  </table>
                ) : postReleaseView === 'proposals' ? (
                  <table className="w-full min-w-[1180px] text-center text-xs">
                    <thead className="sticky top-0 bg-[var(--dash-card)] text-[10px] uppercase tracking-wide text-[var(--dash-muted)]">
                      <tr>
                        <th className="px-2 py-2">Prioridade</th>
                        <th className="px-2 py-2 text-left">Proposta</th>
                        <th className="px-2 py-2">Contexto</th>
                        <th className="px-2 py-2">Familias</th>
                        <th className="px-2 py-2">Segmentos</th>
                        <th className="px-2 py-2">Casos</th>
                        <th className="px-2 py-2">Estado</th>
                        <th className="px-2 py-2 text-left">Contrato</th>
                      </tr>
                    </thead>
                    <tbody>
                      {proposalRows.length ? proposalRows.map((item) => {
                        const selector = item.contract?.selector ?? {};
                        const files = Array.isArray(selector.file_families) ? selector.file_families : [];
                        const sample = Array.isArray(item.sample_cases) ? item.sample_cases[0] : null;
                        const totalCases = Number(item.positive_case_count ?? 0)
                          + Number(item.negative_case_count ?? 0)
                          + Number(item.boundary_case_count ?? 0);
                        return (
                          <tr key={`proposal-${item.proposal_key}`} className="border-t border-[var(--dash-border)] text-[var(--dash-text)]">
                            <td className="px-2 py-2"><Badge tone={Number(item.priority ?? 0) >= 75 ? 'red' : Number(item.priority ?? 0) >= 60 ? 'amber' : 'blue'}>{Number(item.priority ?? 0).toLocaleString('pt-BR', { maximumFractionDigits: 1 })}</Badge></td>
                            <td className="max-w-[280px] px-2 py-2 text-left" title={sample ? `${sample.relative_path} :: ${sample.source_key}\n${sample.input_text}` : item.evidence_type}>
                              <p className="font-black">{item.label ?? String(item.issue_type ?? '').replaceAll('_', ' ')}</p>
                              <p className="truncate text-[10px] text-[var(--dash-muted)]">{item.provider_id} - desabilitado</p>
                            </td>
                            <td className="px-2 py-2">{String(item.token_context ?? '-').replaceAll('_', ' ')}</td>
                            <td className="px-2 py-2" title={files.join(', ')}>{compact(item.family_count ?? 0)}</td>
                            <td className="px-2 py-2 font-black">{compact(item.segment_count ?? 0)}</td>
                            <td className="px-2 py-2" title={`${compact(item.positive_case_count ?? 0)} positivos; ${compact(item.negative_case_count ?? 0)} negativos; ${compact(item.boundary_case_count ?? 0)} de fronteira`}>{compact(totalCases)}</td>
                            <td className="px-2 py-2"><Badge tone="amber">revisao obrigatoria</Badge></td>
                            <td className="max-w-[300px] px-2 py-2 text-left" title={item.evidence_type}>
                              deterministico, idempotente, tokens preservados e gate pairwise
                            </td>
                          </tr>
                        );
                      }) : (
                        <tr><td colSpan={8} className="px-3 py-5 text-center font-bold text-[var(--dash-muted)]">Nenhuma familia acionavel sem cobertura; nenhuma proposta foi criada nesta epoch.</td></tr>
                      )}
                    </tbody>
                  </table>
                ) : postReleaseView === 'providers' ? (
                  <table className="w-full min-w-[1080px] text-center text-xs">
                    <thead className="sticky top-0 bg-[var(--dash-card)] text-[10px] uppercase tracking-wide text-[var(--dash-muted)]">
                      <tr>
                        <th className="px-2 py-2 text-left">Provedor</th>
                        <th className="px-2 py-2">Estado</th>
                        <th className="px-2 py-2">Famílias</th>
                        <th className="px-2 py-2">Inspecionados</th>
                        <th className="px-2 py-2">Elegíveis</th>
                        <th className="px-2 py-2">Evidências</th>
                        <th className="px-2 py-2">Promoções</th>
                        <th className="px-2 py-2">Última produtividade</th>
                      </tr>
                    </thead>
                    <tbody>
                      {providerRows.length ? providerRows.map((item) => (
                        <tr key={`provider-${item.provider_id}`} className="border-t border-[var(--dash-border)] text-[var(--dash-text)]">
                          <td className="max-w-[280px] px-2 py-2 text-left" title={item.evidence_type ?? ''}>
                            <p className="font-black">{item.label ?? String(item.provider_id ?? '').replaceAll('_', ' ')}</p>
                            <p className="truncate text-[10px] text-[var(--dash-muted)]">{item.provider_id}</p>
                          </td>
                          <td className="px-2 py-2"><Badge tone={providerStatusTone(item.status)}>{providerStatusLabels[item.status] ?? String(item.status ?? 'não medido').replaceAll('_', ' ')}</Badge></td>
                          <td className="px-2 py-2" title={`${compact(item.closed_family_count ?? 0)} famílias históricas fechadas`}>
                            {compact(item.active_family_count ?? 0)} ativas / {compact(item.observed_family_count ?? 0)} observadas
                          </td>
                          <td className="px-2 py-2 font-black">{compact(item.inspected_count ?? 0)}</td>
                          <td className="px-2 py-2"><Badge tone={Number(item.shadow_eligible_count ?? 0) ? 'amber' : 'emerald'}>{compact(item.shadow_eligible_count ?? 0)}</Badge></td>
                          <td className="px-2 py-2">{compact(item.evidence_count ?? 0)}</td>
                          <td className="px-2 py-2"><Badge tone={Number(item.promotion_ready_count ?? 0) ? 'blue' : 'slate'}>{compact(item.promotion_ready_count ?? 0)}</Badge></td>
                          <td className="max-w-[220px] px-2 py-2" title={item.last_productive_at ?? ''}>
                            {Number(item.last_productive_count ?? 0) > 0
                              ? `${compact(item.last_productive_count)} no score #${item.last_productive_score_run_id ?? '-'} · ${compactDateTime(item.last_productive_at)}`
                              : 'sem promoção histórica'}
                          </td>
                        </tr>
                      )) : (
                        <tr><td colSpan={8} className="px-3 py-5 text-center font-bold text-[var(--dash-muted)]">Nenhum provedor habilitado foi instrumentado.</td></tr>
                      )}
                    </tbody>
                  </table>
                ) : postReleaseView === 'discovery' ? (
                  <table className="w-full min-w-[1080px] text-center text-xs">
                    <thead className="sticky top-0 bg-[var(--dash-card)] text-[10px] uppercase tracking-wide text-[var(--dash-muted)]">
                      <tr>
                        <th className="px-2 py-2">Prioridade</th>
                        <th className="px-2 py-2">Familia</th>
                        <th className="px-2 py-2">Contexto</th>
                        <th className="px-2 py-2">Area</th>
                        <th className="px-2 py-2">Segmentos</th>
                        <th className="px-2 py-2">Score medio</th>
                        <th className="px-2 py-2">Estado</th>
                        <th className="px-2 py-2">Provedor</th>
                      </tr>
                    </thead>
                    <tbody>
                      {patternFamilies.length ? patternFamilies.map((item) => {
                        const statusLabels = {
                          new_candidate: 'nova',
                          recurring_candidate: 'recorrente',
                          covered_by_provider: 'coberta',
                          monitoring: 'observacao',
                          closed_observation: 'historico fechado',
                        };
                        const statusTone = item.status === 'covered_by_provider'
                          ? 'emerald'
                          : item.status === 'closed_observation'
                            ? 'slate'
                          : item.status === 'new_candidate'
                            ? 'amber'
                            : item.status === 'recurring_candidate'
                              ? 'blue'
                              : 'slate';
                        const sample = Array.isArray(item.samples) ? item.samples[0] : null;
                        return (
                          <tr key={`pattern-${item.family_key}`} className="border-t border-[var(--dash-border)] text-[var(--dash-text)]">
                            <td className="px-2 py-2"><Badge tone={Number(item.priority ?? 0) >= 75 ? 'red' : Number(item.priority ?? 0) >= 60 ? 'amber' : 'blue'}>{Number(item.priority ?? 0).toLocaleString('pt-BR', { maximumFractionDigits: 1 })}</Badge></td>
                            <td className="max-w-[240px] px-2 py-2 font-bold" title={sample ? `${sample.relative_path} :: ${sample.source_key}\n${sample.candidate_text}` : item.issue_type}>{String(item.issue_type ?? 'nao medido').replaceAll('_', ' ')}</td>
                            <td className="px-2 py-2">{String(item.token_context ?? '-').replaceAll('_', ' ')}</td>
                            <td className="px-2 py-2">{item.file_family ?? '-'}</td>
                            <td className="px-2 py-2 font-black" title={`${compact(item.closed_segment_count ?? 0)} fechados no lifecycle`}>
                              {item.operational_segment_count === null || item.operational_segment_count === undefined
                                ? compact(item.segment_count ?? 0)
                                : `${compact(item.operational_segment_count)} ativos / ${compact(item.segment_count ?? 0)} evidencias`}
                            </td>
                            <td className="px-2 py-2"><Badge tone={scoreTone(item.average_score)}>{scoreLabel(item.average_score)}</Badge></td>
                            <td className="px-2 py-2"><Badge tone={statusTone}>{statusLabels[item.status] ?? String(item.status ?? '-').replaceAll('_', ' ')}</Badge></td>
                            <td className="max-w-[210px] px-2 py-2" title={item.evidence_type ?? ''}>{item.provider_id ? String(item.provider_id).replaceAll('_', ' ') : '-'}</td>
                          </tr>
                        );
                      }) : (
                        <tr><td colSpan={8} className="px-3 py-5 text-center font-bold text-[var(--dash-muted)]">A descoberta ainda nao foi materializada para a epoch atual.</td></tr>
                      )}
                    </tbody>
                  </table>
                ) : postReleaseView === 'calibration' ? (
                  <table className="w-full min-w-[1380px] text-center text-xs">
                    <thead className="sticky top-0 bg-[var(--dash-card)] text-[10px] uppercase tracking-wide text-[var(--dash-muted)]">
                      <tr>
                        <th className="px-2 py-2">Segmento</th>
                        <th className="px-2 py-2">Fila</th>
                        <th className="px-2 py-2 text-left">Arquivo / chave</th>
                        <th className="px-2 py-2 text-left">Texto A</th>
                        <th className="px-2 py-2 text-left">Texto B</th>
                        <th className="px-2 py-2">Score A</th>
                        <th className="px-2 py-2">Score B</th>
                        <th className="px-2 py-2">Delta</th>
                        <th className="px-2 py-2">Decisao humana</th>
                      </tr>
                    </thead>
                    <tbody>
                      {calibrationReviewSegments.length ? calibrationReviewSegments.map((item) => (
                        <tr key={`calibration-${item.id}`} className="border-t border-[var(--dash-border)] text-[var(--dash-text)]">
                          <td className="px-2 py-2 font-bold text-[var(--dash-muted)]">{item.segment_id}</td>
                          <td className="px-2 py-2">
                            <Badge tone={item.is_priority ? 'amber' : item.is_control ? 'blue' : 'slate'}>
                              {item.is_priority ? 'prioridade' : item.is_control ? 'controle' : 'revisao'}
                            </Badge>
                          </td>
                          <td className="max-w-[220px] truncate px-2 py-2 text-left" title={`${item.relative_path} :: ${item.source_key}`}>{item.relative_path} :: <span className="font-bold">{item.source_key}</span></td>
                          <td className="max-w-[260px] truncate px-2 py-2 text-left" title={item.baseline_text ?? ''}>{item.baseline_text ?? '-'}</td>
                          <td className="max-w-[260px] truncate px-2 py-2 text-left" title={item.candidate_text ?? ''}>{item.candidate_text ?? '-'}</td>
                          <td className="px-2 py-2"><Badge tone={scoreTone(item.baseline_score_raw)}>{scoreLabel(item.baseline_score_raw)}</Badge></td>
                          <td className="px-2 py-2"><Badge tone={scoreTone(item.candidate_score_raw)}>{scoreLabel(item.candidate_score_raw)}</Badge></td>
                          <td className="px-2 py-2"><Badge tone={scoreDeltaTone(item)}>{scoreDeltaLabel(item)}</Badge></td>
                          <td className="px-2 py-2">
                            {item.review_status === 'decided' ? (
                              <Badge tone={item.reviewer_label === 'invalid_pair' ? 'red' : 'emerald'}>{item.reviewer_label ?? 'decidido'}</Badge>
                            ) : (
                              <div className="flex min-w-[250px] justify-center gap-1">
                                {[
                                  ['baseline_preferred', 'A', 'Selecionar o texto A como melhor.'],
                                  ['candidate_preferred', 'B', 'Selecionar o texto B como melhor.'],
                                  ['equivalent', 'Equivalentes', 'Marcar os textos A e B como equivalentes.'],
                                  ['invalid_pair', 'Invalido', 'Marcar este par como invalido.'],
                                ].map(([label, buttonLabel, description]) => (
                                  <button
                                    key={label}
                                    type="button"
                                    aria-label={description}
                                    data-tooltip-title={buttonLabel === 'A' || buttonLabel === 'B' ? `Preferir ${buttonLabel}` : buttonLabel}
                                    data-tooltip-description={description}
                                    disabled={calibrationSubmittingItemId === item.id}
                                    onClick={() => submitCalibrationReview(item.id, label, !item.is_control)}
                                    className={cn(
                                      'rounded-lg border border-[var(--dash-border)] bg-[var(--dash-card)] px-2 py-1 font-black text-[var(--dash-muted)] transition hover:border-blue-300/45 hover:text-blue-100 disabled:opacity-40',
                                      (buttonLabel === 'A' || buttonLabel === 'B') && 'min-w-10'
                                    )}
                                  >
                                    {buttonLabel}
                                  </button>
                                ))}
                              </div>
                            )}
                          </td>
                        </tr>
                      )) : (
                        <tr><td colSpan={9} className="px-3 py-5 text-center font-bold text-[var(--dash-muted)]">{calibrationPolicyDecision === 'skip' ? `Calibracao dispensada: ${calibrationPolicySummary}.` : 'Nenhuma fila de calibracao materializada para a epoch atual.'}</td></tr>
                      )}
                    </tbody>
                  </table>
                ) : postReleaseView === 'regressions' ? (
                  <table className="w-full min-w-[1160px] text-center text-xs">
                    <thead className="sticky top-0 bg-[var(--dash-card)] text-[10px] uppercase tracking-wide text-[var(--dash-muted)]">
                      <tr>
                        <th className="px-2 py-2">Segmento</th>
                        <th className="px-2 py-2 text-left">Arquivo / chave</th>
                        <th className="px-2 py-2 text-left">Old</th>
                        <th className="px-2 py-2 text-left">Output</th>
                        <th className="px-2 py-2">Score old</th>
                        <th className="px-2 py-2">Score novo</th>
                        <th className="px-2 py-2">Delta</th>
                        <th className="px-2 py-2">Revisao</th>
                        <th className="px-2 py-2">Estado</th>
                      </tr>
                    </thead>
                    <tbody>
                      {scoreRegressionSegments.length ? scoreRegressionSegments.slice(0, 80).map((item) => (
                        <tr key={`regression-${item.segment_id}`} className="border-t border-[var(--dash-border)] text-[var(--dash-text)]">
                          <td className="px-2 py-2 font-bold text-[var(--dash-muted)]">{item.segment_id}</td>
                          <td className="max-w-[220px] truncate px-2 py-2 text-left" title={`${item.relative_path} :: ${item.source_key}`}>{item.relative_path} :: <span className="font-bold">{item.source_key}</span></td>
                          <td className="max-w-[250px] truncate px-2 py-2 text-left" title={item.old_text ?? ''}>{item.old_text ?? '-'}</td>
                          <td className="max-w-[250px] truncate px-2 py-2 text-left" title={item.output_text ?? ''}>{item.output_text ?? '-'}</td>
                          <td className="px-2 py-2"><Badge tone={scoreTone(segmentOldScore(item))}>{scoreLabel(segmentOldScore(item))}</Badge></td>
                          <td className="px-2 py-2" title={scoreCellTitle(item)}><Badge tone={scoreTone(segmentNewScore(item))}>{scoreLabel(segmentNewScore(item))}</Badge></td>
                          <td className="px-2 py-2" title={scoreCellTitle(item)}><Badge tone={scoreDeltaTone(item)}>{scoreDeltaLabel(item)}</Badge></td>
                          <td className="max-w-[220px] px-2 py-2" title={item.score_review_reason ?? ''}>
                            <div className="flex items-center justify-center gap-1.5">
                              <span className="truncate">{item.score_review_kind === 'raw_equal' ? 'score bruto igual' : item.score_review_kind === 'raw_regression' ? 'regressao bruta' : item.score_action ?? 'nao medido'}</span>
                              {item.calibration_review_item_id ? (
                                <Badge tone={item.calibration_review_priority ? 'amber' : item.calibration_review_status === 'decided' ? 'emerald' : 'blue'}>
                                  {item.calibration_review_priority ? 'prioridade' : item.calibration_review_status === 'decided' ? 'revisado' : 'na fila'}
                                </Badge>
                              ) : null}
                            </div>
                          </td>
                          <td className="max-w-[190px] truncate px-2 py-2" title={item.final_state ?? ''}>{item.final_state ?? 'nao medido'}</td>
                        </tr>
                      )) : (
                        <tr><td colSpan={9} className="px-3 py-5 text-center font-bold text-[var(--dash-muted)]">Nenhum ajuste com score bruto regressivo ou igual.</td></tr>
                      )}
                    </tbody>
                  </table>
                ) : postReleaseView === 'apply' ? (
                  <table className="w-full min-w-[1160px] text-center text-xs">
                    <thead className="sticky top-0 bg-[var(--dash-card)] text-[10px] uppercase tracking-wide text-[var(--dash-muted)]">
                      <tr>
                        <th className="px-2 py-2">Segmento</th>
                        <th className="px-2 py-2 text-left">Arquivo / chave</th>
                        <th className="px-2 py-2 text-left">Output atual</th>
                        <th className="px-2 py-2 text-left">Confirmado</th>
                        <th className="px-2 py-2">Score novo</th>
                        {postReleaseView === 'low_score' ? <th className="px-2 py-2">Coorte</th> : null}
                        <th className="px-2 py-2">Acao</th>
                        <th className="px-2 py-2">Estado</th>
                        <th className="px-2 py-2 text-left">Motivo</th>
                      </tr>
                    </thead>
                    <tbody>
                      {applySegments.length ? applySegments.slice(0, 120).map((item) => (
                        <tr key={`apply-${item.segment_id}`} className="border-t border-[var(--dash-border)] text-[var(--dash-text)]">
                          <td className="px-2 py-2 font-bold text-[var(--dash-muted)]">{item.segment_id}</td>
                          <td className="max-w-[220px] truncate px-2 py-2 text-left" title={`${item.relative_path} :: ${item.source_key}`}>{item.relative_path} :: <span className="font-bold">{item.source_key}</span></td>
                          <td className="max-w-[250px] truncate px-2 py-2 text-left" title={item.output_text ?? ''}>{item.output_text ?? '-'}</td>
                          <td className="max-w-[250px] truncate px-2 py-2 text-left" title={item.confirmed_text ?? ''}>{item.confirmed_text ?? '-'}</td>
                          <td className="px-2 py-2" title={scoreCellTitle(item)}><Badge tone={scoreTone(segmentNewScore(item))}>{scoreLabel(segmentNewScore(item))}</Badge></td>
                          <td className="max-w-[150px] truncate px-2 py-2" title={item.score_action ?? item.active_action ?? item.candidate_action ?? ''}>{item.score_action ?? item.active_action ?? item.candidate_action ?? 'nao medido'}</td>
                          <td className="max-w-[180px] truncate px-2 py-2" title={item.final_state ?? item.review_state ?? ''}>{item.final_state ?? item.review_state ?? 'nao medido'}</td>
                          <td className="max-w-[250px] truncate px-2 py-2 text-left" title={item.reasons_json ?? item.confirmation_label ?? ''}>{item.confirmation_label ?? item.reasons_json ?? 'nao medido'}</td>
                        </tr>
                      )) : (
                        <tr><td colSpan={8} className="px-3 py-5 text-center font-bold text-[var(--dash-muted)]">Nenhum needs apply medido.</td></tr>
                      )}
                    </tbody>
                  </table>
                ) : postReleaseView === 'new' ? (
                  <table className="w-full min-w-[900px] text-center text-xs">
                    <thead className="sticky top-0 bg-[var(--dash-card)] text-[10px] uppercase tracking-wide text-[var(--dash-muted)]">
                      <tr>
                        <th className="px-2 py-2">Segmento</th>
                        <th className="px-2 py-2 text-left">Arquivo / chave</th>
                        <th className="px-2 py-2 text-left">Source</th>
                        <th className="px-2 py-2 text-left">Output</th>
                        <th className="px-2 py-2">Score novo</th>
                        <th className="px-2 py-2">Acao</th>
                      </tr>
                    </thead>
                    <tbody>
                      {newSegments.length ? newSegments.slice(0, 80).map((item) => (
                        <tr key={`new-${item.segment_id}`} className="border-t border-[var(--dash-border)] text-[var(--dash-text)]">
                          <td className="px-2 py-2 font-bold text-[var(--dash-muted)]">{item.segment_id}</td>
                          <td className="max-w-[220px] truncate px-2 py-2 text-left" title={`${item.relative_path} :: ${item.source_key}`}>{item.relative_path} :: <span className="font-bold">{item.source_key}</span></td>
                          <td className="max-w-[260px] truncate px-2 py-2 text-left" title={item.spanish_text ?? ''}>{item.spanish_text ?? '-'}</td>
                          <td className="max-w-[260px] truncate px-2 py-2 text-left" title={item.output_text ?? ''}>{item.output_text ?? '-'}</td>
                          <td className="px-2 py-2"><Badge tone={scoreTone(segmentNewScore(item))}>{scoreLabel(segmentNewScore(item))}</Badge></td>
                          <td className="max-w-[160px] truncate px-2 py-2" title={postReleaseView === 'unhandled' ? item.final_state ?? item.score_action ?? '' : item.score_action ?? item.final_state ?? ''}>
                            {postReleaseView === 'unhandled' ? item.final_state ?? item.score_action ?? 'nao medido' : item.score_action ?? item.final_state ?? 'nao medido'}
                          </td>
                        </tr>
                      )) : (
                        <tr><td colSpan={6} className="px-3 py-5 text-center font-bold text-[var(--dash-muted)]">Nenhum segmento novo medido.</td></tr>
                      )}
                    </tbody>
                  </table>
                ) : (
                  <table className="w-full min-w-[900px] text-center text-xs">
                    <thead className="sticky top-0 bg-[var(--dash-card)] text-[10px] uppercase tracking-wide text-[var(--dash-muted)]">
                      <tr>
                        <th className="px-2 py-2">Segmento</th>
                        <th className="px-2 py-2 text-left">Arquivo / chave</th>
                        <th className="px-2 py-2 text-left">Output</th>
                        <th className="px-2 py-2">Score novo</th>
                        <th className="px-2 py-2">Acao</th>
                        <th className="px-2 py-2">Risco</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(postReleaseView === 'low_score' ? lowScoreSegments : unhandledSegments).length ? (postReleaseView === 'low_score' ? lowScoreSegments : unhandledSegments).slice(0, 80).map((item) => (
                        <tr key={`${postReleaseView}-${item.segment_id}`} className="border-t border-[var(--dash-border)] text-[var(--dash-text)]">
                          <td className="px-2 py-2 font-bold text-[var(--dash-muted)]">{item.segment_id}</td>
                          <td className="max-w-[240px] truncate px-2 py-2 text-left" title={`${item.relative_path} :: ${item.source_key}`}>{item.relative_path} :: <span className="font-bold">{item.source_key}</span></td>
                          <td className="max-w-[330px] truncate px-2 py-2 text-left" title={item.output_text ?? item.old_text ?? ''}>{item.output_text ?? item.old_text ?? '-'}</td>
                          <td className="px-2 py-2"><Badge tone={scoreTone(segmentNewScore(item))}>{scoreLabel(segmentNewScore(item))}</Badge></td>
                          {postReleaseView === 'low_score' ? (
                            <td className="px-2 py-2">
                              <Badge tone={lowScoreCohort(item).tone}>{lowScoreCohort(item).label}</Badge>
                            </td>
                          ) : null}
                          <td className="max-w-[160px] truncate px-2 py-2">{item.score_action ?? item.final_state ?? 'nao medido'}</td>
                          <td className="max-w-[120px] truncate px-2 py-2">{item.risk_class ?? item.score_tier ?? 'nao medido'}</td>
                        </tr>
                      )) : (
                        <tr><td colSpan={postReleaseView === 'low_score' ? 7 : 6} className="px-3 py-5 text-center font-bold text-[var(--dash-muted)]">Nenhuma fila medida para esta aba.</td></tr>
                      )}
                    </tbody>
                  </table>
                )}
              </div>
            </div>

            <div className="hidden">
              <div className="rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-2.5">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <h4 className="text-sm font-black text-[var(--dash-text)]">Estado de produção</h4>
                    <p className="text-xs text-[var(--dash-muted)]">Baseline, output, preflight e gates em uma leitura única.</p>
                  </div>
                  <Badge tone={productionPublicationTone}>{productionPublicationStatus}</Badge>
                </div>
                <div className="mt-2 grid grid-cols-2 gap-1.5">
                  {productionStateRows.map((item) => (
                    <div key={item.label} className="min-w-0 rounded-lg border border-[var(--dash-border)] bg-[var(--dash-card)] px-2 py-1.5">
                      <p className="text-[9px] font-black uppercase tracking-wide text-[var(--dash-muted)]">{item.label}</p>
                      <p className={cn('mt-0.5 truncate text-xs font-black', item.tone === 'emerald' ? 'text-emerald-400' : item.tone === 'red' ? 'text-red-400' : item.tone === 'amber' ? 'text-amber-400' : item.tone === 'blue' ? 'text-blue-400' : 'text-[var(--dash-text)]')} title={String(item.value)}>{String(item.value)}</p>
                    </div>
                  ))}
                </div>
                <div className="mt-2 rounded-lg border border-[var(--dash-border)] bg-[var(--dash-card)] px-2 py-1.5">
                  <p className="text-[9px] font-black uppercase tracking-wide text-[var(--dash-muted)]">Motivos de bloqueio</p>
                  <p className="mt-0.5 line-clamp-3 text-xs font-bold text-[var(--dash-text)]" title={blockingSummary}>{blockingSummary}</p>
                </div>
              </div>
            </div>

            <div className="hidden">
              <div className="rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-2">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <h4 className="text-sm font-black text-[var(--dash-text)]">Gate de publicação</h4>
                    <p className="text-xs text-[var(--dash-muted)]">Publicação fica separada da produção de avaliação.</p>
                  </div>
                  <Badge tone={publishTone}>{publishStatus}</Badge>
                </div>
                <div className="mt-2 space-y-1.5 text-xs">
                  <div className="rounded-lg border border-[var(--dash-border)] bg-[var(--dash-card)] px-2 py-1.5">
                    <p className="text-[10px] font-black uppercase tracking-wide text-[var(--dash-muted)]">Motivos</p>
                    <p className="mt-1 line-clamp-2 font-bold text-[var(--dash-text)]">
                      {publicationReasons.length ? publicationReasons.join(', ') : publicationAllowed ? 'sem bloqueador' : 'not_measured'}
                    </p>
                  </div>
                  <div className="grid grid-cols-2 gap-1.5">
                    <div className="rounded-lg border border-[var(--dash-border)] bg-[var(--dash-card)] px-2 py-1.5">
                      <p className="text-[10px] font-black uppercase tracking-wide text-[var(--dash-muted)]">Baseline</p>
                      <p className="mt-1 truncate font-bold text-blue-400" title={baselineControl.stable_baseline_path ?? 'source\\spanish_old'}>{baselineControl.stable_baseline_path ?? 'source\\spanish_old'}</p>
                    </div>
                    <div className="rounded-lg border border-[var(--dash-border)] bg-[var(--dash-card)] px-2 py-1.5">
                      <p className="text-[10px] font-black uppercase tracking-wide text-[var(--dash-muted)]">Output</p>
                      <p className={cn('mt-1 truncate font-bold', outputCurrentTone === 'amber' ? 'text-amber-400' : outputCurrentTone === 'emerald' ? 'text-emerald-400' : 'text-[var(--dash-text)]')} title={baselineControl.output_restored_from ?? 'source\\spanish_source'}>
                        {outputCurrentLabel}
                      </p>
                    </div>
                  </div>
                </div>
              </div>

              <div className="rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-2">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <h4 className="text-sm font-black text-[var(--dash-text)]">Source/output/update</h4>
                    <p className="text-xs text-[var(--dash-muted)]">Status para pequena atualizacao do jogo.</p>
                  </div>
                  <Badge tone={cache.stale ? 'amber' : 'slate'}>{cache.stale ? 'cache defasado' : 'baseline atual'}</Badge>
                </div>
                <div className="mt-1.5 grid grid-cols-2 gap-1.5">
                  {sourceOutputChecklist.map((item) => (
                    <div key={item.label} className="rounded-lg border border-[var(--dash-border)] bg-[var(--dash-card)] p-1.5">
                      <p className="text-[10px] font-black uppercase tracking-wide text-[var(--dash-muted)]">{item.label}</p>
                      <p className={cn('mt-1 truncate text-xs font-black', item.tone === 'emerald' ? 'text-emerald-400' : item.tone === 'amber' ? 'text-amber-400' : 'text-[var(--dash-text)]')}>{String(item.value)}</p>
                    </div>
                  ))}
                </div>
              </div>

              <div className="rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-2">
                <h4 className="text-sm font-black text-[var(--dash-text)]">Checklist full production</h4>
                <div className="mt-1.5 grid grid-cols-2 gap-1.5">
                  {updateChecklist.map((item) => (
                    <div key={item.label} className="flex items-center justify-between gap-2 rounded-lg border border-[var(--dash-border)] bg-[var(--dash-card)] px-2 py-1.5">
                      <span className="truncate text-xs font-bold text-[var(--dash-text)]">{item.label}</span>
                      <Badge tone={item.ok ? 'emerald' : 'slate'}>{item.ok ? 'ok' : item.pending}</Badge>
                    </div>
                  ))}
                </div>
              </div>

              <div className="rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-2">
                <h4 className="text-sm font-black text-[var(--dash-text)]">Fluxo protegido</h4>
                <div className="mt-2 h-2 overflow-hidden rounded-full bg-slate-500/20">
                  <div className={cn('h-full rounded-full', runStatus?.status === 'failed' ? 'bg-red-500' : runActive ? 'bg-blue-500' : 'bg-emerald-500')} style={{ width: `${Math.max(0, Math.min(100, runProgress))}%` }} />
                </div>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {displayPhases.map((phase, index) => (
                    <span key={phase.id} title={`${phase.title}: ${phase.purpose}`} className="inline-flex items-center gap-1 rounded-md border border-violet-400/25 bg-violet-500/10 px-2 py-1 text-[10px] font-bold text-violet-200">
                      <BrainCircuit size={11} /> {index + 1}/4 {phase.title}
                    </span>
                  ))}
                </div>
              </div>
            </div>
          </div>
          </div>
          )}
        </Card>
      </div>
    </div>
  );
}

const qualityBandMeta = [
  { id: 'critical', label: 'Crítico', range: '< 20%', color: '#ef4444' },
  { id: 'low', label: 'Baixo', range: '20–49%', color: '#f97316' },
  { id: 'moderate', label: 'Moderado', range: '50–74%', color: '#f59e0b' },
  { id: 'good', label: 'Bom', range: '75–89%', color: '#3b82f6' },
  { id: 'high', label: 'Alto', range: '≥ 90%', color: '#10b981' },
];

const symmetricPercentAxis = (values) => {
  const finiteValues = values.map(Number).filter(Number.isFinite);
  if (!finiteValues.length) return { domain: [0, 100], ticks: [0, 25, 50, 75, 100], step: 25 };
  const min = Math.min(...finiteValues);
  const max = Math.max(...finiteValues);
  const midpoint = (min + max) / 2;
  const requiredHalfSpan = Math.max(1, (max - min) * 0.75);
  const stepOptions = [0.25, 0.5, 1, 2, 5, 10, 20, 25];
  let stepIndex = Math.max(0, stepOptions.findIndex((step) => step * 2 >= requiredHalfSpan));
  if (stepIndex < 0) stepIndex = stepOptions.length - 1;
  let step = stepOptions[stepIndex];
  let center = Math.round(midpoint / step) * step;
  while ((min < center - (2 * step) || max > center + (2 * step)) && stepIndex < stepOptions.length - 1) {
    step = stepOptions[++stepIndex];
    center = Math.round(midpoint / step) * step;
  }
  const lower = Math.max(0, center - (2 * step));
  const upper = Math.min(100, center + (2 * step));
  const ticks = Array.from({ length: 5 }, (_, index) => Number((lower + ((upper - lower) * index / 4)).toFixed(2)));
  return { domain: [lower, upper], ticks, step: Number(((upper - lower) / 4).toFixed(2)) };
};

function OverviewKpi({ label, value, detail, tone = 'blue', icon: Icon = Activity }) {
  const toneClasses = {
    blue: 'border-blue-400/25 bg-blue-500/10 text-blue-300',
    emerald: 'border-emerald-400/25 bg-emerald-500/10 text-emerald-300',
    amber: 'border-amber-400/25 bg-amber-500/10 text-amber-300',
    red: 'border-red-400/25 bg-red-500/10 text-red-300',
    violet: 'border-violet-400/25 bg-violet-500/10 text-violet-300',
  };
  const toneBars = {
    blue: 'from-blue-400/90 via-blue-400/30',
    emerald: 'from-emerald-400/90 via-emerald-400/30',
    amber: 'from-amber-400/90 via-amber-400/30',
    red: 'from-red-400/90 via-red-400/30',
    violet: 'from-violet-400/90 via-violet-400/30',
  };
  return (
    <div className="dashboard-surface quality-overview-kpi relative h-full min-h-[106px] min-w-0 overflow-hidden border p-3.5">
      <span className={cn('absolute inset-x-0 top-0 h-px bg-gradient-to-r to-transparent', toneBars[tone])} aria-hidden="true" />
      <div className="flex items-center justify-between gap-3">
        <p className="truncate text-[0.68rem] font-semibold uppercase tracking-wide text-[var(--dash-muted)]">{label}</p>
        <span className={cn('grid h-8 w-8 shrink-0 place-items-center rounded-lg border', toneClasses[tone])}>
          <Icon size={14} />
        </span>
      </div>
      <p className="mt-2 truncate text-[1.35rem] font-black tracking-tight text-[var(--dash-text)]">{value}</p>
      <p className="mt-1 truncate text-[11px] leading-4 text-[var(--dash-muted)]" title={detail}>{detail}</p>
    </div>
  );
}

const qualityVersionTooltipCopy = (source = {}) => {
  const bands = source.qualityBands ?? {};
  const hasBands = qualityBandMeta.some((band) => bands[band.id] != null);
  const bandLines = hasBands
    ? qualityBandMeta.map((band) => `${band.label} ${band.range}: ${compact(bands[band.id])}`)
    : ['Distribuição histórica não materializada.'];
  if (Number(bands.unmeasured ?? 0) > 0) bandLines.push(`N/A: ${compact(bands.unmeasured)}`);
  return {
    title: `${source.version ?? 'Versão'} · score operacional ${pct(source.score)}`,
    description: [`Cobertura medida: ${pct(source.coverage)}`, ...bandLines, 'Snapshot sensível à baseline; não representa ganho isolado.'].join('\n'),
    meta: `${source.contract ?? 'contrato não medido'} · use delta pareado para comparar ganho`,
  };
};

function QualityVersionDot({ cx, cy, payload }) {
  if (!Number.isFinite(cx) || !Number.isFinite(cy) || !payload) return null;
  const tooltip = qualityVersionTooltipCopy(payload);
  return (
    <g
      className="cursor-pointer outline-none"
      role="img"
      tabIndex="0"
      aria-label={`${tooltip.title}. ${tooltip.description.replaceAll('\n', '. ')}`}
      data-tooltip-title={tooltip.title}
      data-tooltip-description={tooltip.description}
      data-tooltip-meta={tooltip.meta}
    >
      <circle cx={cx} cy={cy} r="13" fill="transparent" />
      <circle cx={cx} cy={cy} r="5" fill="var(--dash-card)" stroke="var(--dash-accent)" strokeWidth="3" pointerEvents="none" />
    </g>
  );
}

const qualityBandTooltipCopy = (source = {}) => {
  const delta = Number(source.outputCount ?? 0) - Number(source.oldCount ?? 0);
  return {
    title: `${source.band ?? 'Faixa'} · score ${source.range ?? 'N/A'}`,
    description: `Quantidade atual: ${fmt(source.outputCount)} segmentos\nParticipação: ${pct(source.outputPct)}\nBase anterior: ${fmt(source.oldCount)} segmentos`,
    meta: `Variação: ${delta >= 0 ? '+' : ''}${fmt(delta)} segmentos`,
  };
};

function ProjectOverviewDashboard({ data }) {
  const appState = data.appState ?? {};
  const release = appState.release ?? {};
  const operationalClosure = release.operational_closure ?? {};
  const qualityDebt = release.quality_debt ?? {};
  const providerHealth = release.promotion_provider_health ?? {};
  const postRelease = release.post_release ?? {};
  const diffSummary = postRelease.diff_review?.summary ?? {};
  const lowScoreCohorts = postRelease.diff_review?.low_score_cohorts ?? {};
  const actionableLowScore = Number(lowScoreCohorts.actionable ?? diffSummary.low_score_actionable ?? 0);
  const informationalLowScore = Number(lowScoreCohorts.informational ?? diffSummary.low_score_informational ?? 0);
  const packageScore = diffSummary.package_score_comparison ?? {};
  const scoreSemantics = packageScore.score_semantics ?? {};
  const validatedPairwiseGain = packageScore.validated_pairwise_gain ?? {};
  const qualityBands = packageScore.quality_bands ?? {};
  const oldBands = qualityBands.old ?? {};
  const outputBands = qualityBands.output ?? {};
  const totalSegments = Number(packageScore.total_segment_count ?? release.total_segments ?? 0);
  const measuredSegments = Number(packageScore.measured_count ?? 0);
  const oldScore = Number(packageScore.avg_old_score ?? packageScore.weighted_avg_old_score ?? 0);
  const outputScore = Number(packageScore.avg_new_score ?? packageScore.weighted_avg_new_score ?? 0);
  const scoreDelta = Number(packageScore.avg_delta ?? packageScore.weighted_avg_delta ?? 0);
  const coverage = Number(packageScore.coverage ?? 0);
  const pending = Number(release.pending_count ?? 0);
  const needsApply = Number(release.needs_apply ?? 0);
  const regressions = Number(diffSummary.score_regressions ?? packageScore.regressed_count ?? 0);
  const critical = Number(outputBands.critical ?? 0);
  const low = Number(outputBands.low ?? 0);
  const attentionCount = critical + low;
  const unmeasured = Number(outputBands.unmeasured ?? Math.max(0, totalSegments - measuredSegments));
  const comparable = packageScore.comparable_score_contract === true;
  const crossVersionComparable = scoreSemantics.cross_version_comparable === true;
  const baselineSensitive = scoreSemantics.baseline_sensitive !== false;
  const scoreContract = packageScore.score_contract ?? {};
  const packageVersions = Array.isArray(release.package_versions) ? release.package_versions : [];
  const currentVersionNumber = packageVersions.length
    ? Math.max(...packageVersions.map((item) => Number(item.version_number ?? 0))) + 1
    : 3;

  const scoreLabel = (value) => Number.isFinite(value)
    ? `${(value * 100).toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}%`
    : 'nao medido';
  const deltaPoints = scoreDelta * 100;
  const deltaLabel = `${deltaPoints >= 0 ? '+' : ''}${deltaPoints.toLocaleString('pt-BR', { minimumFractionDigits: 4, maximumFractionDigits: 4 })} p.p.`;
  const pairwiseGainAvailable = validatedPairwiseGain.available === true;
  const pairwiseDeltaPoints = Number(validatedPairwiseGain.avg_delta ?? 0) * 100;
  const pairwiseGlobalPoints = Number(validatedPairwiseGain.global_equivalent_delta ?? 0) * 100;
  const pairwiseDeltaLabel = pairwiseGainAvailable
    ? `${pairwiseDeltaPoints >= 0 ? '+' : ''}${pairwiseDeltaPoints.toLocaleString('pt-BR', { minimumFractionDigits: 4, maximumFractionDigits: 4 })} p.p.`
    : 'não medido';
  const pairwiseDetail = pairwiseGainAvailable
    ? `V${validatedPairwiseGain.version_number} · ${fmt(validatedPairwiseGain.improved_count)}/${fmt(validatedPairwiseGain.paired_count)} melhoraram · impacto global ${pairwiseGlobalPoints >= 0 ? '+' : ''}${pairwiseGlobalPoints.toLocaleString('pt-BR', { minimumFractionDigits: 4, maximumFractionDigits: 4 })} p.p.`
    : 'nenhum delta pareado materializado';
  const operationallyClosed = operationalClosure.is_closed === true
    || (!operationalClosure.instrumented && pending === 0 && needsApply === 0);
  const closureLabel = operationallyClosed ? 'Fechado' : needsApply ? 'Aguardando apply' : 'Aberto';
  const qualityDebtActionable = qualityDebt.has_actionable_debt === true;
  const qualityDebtSignals = Number(qualityDebt.actionable_signal_count ?? 0);
  const qualityDebtLabel = qualityDebt.status === 'clear'
    ? 'Sem dívida ativa'
    : qualityDebt.status === 'monitoring'
      ? 'Em observação'
      : qualityDebtActionable
        ? `${compact(qualityDebtSignals)} sinais`
        : 'Não medido';
  const qualityDebtDetail = `${compact(qualityDebt.uncovered_actionable_family_count ?? 0)} famílias sem provedor · ${compact(qualityDebt.promotion_ready_count ?? 0)} promoções · ${compact(qualityDebt.closed_family_count ?? 0)} históricas fechadas`;
  const providerCount = Number(providerHealth.provider_count ?? 0);
  const executedProviderCount = Number(providerHealth.executed_provider_count ?? 0);
  const providerCoverageLabel = providerHealth.instrumented
    ? pct(Number(providerHealth.coverage_rate ?? 0))
    : 'Não medido';
  const providerDetail = `${compact(executedProviderCount)}/${compact(providerCount)} executados · ${compact(providerHealth.inspected_count ?? 0)} casos · ${compact(providerHealth.shadow_eligible_count ?? 0)} elegíveis`;
  const releaseReady = operationallyClosed && regressions === 0;
  const releaseLabel = releaseReady ? 'Pronto' : 'Atencao';
  const nextAction = needsApply > 0
    ? `Aplicar ${fmt(needsApply)} alteracoes validadas`
    : regressions > 0
      ? `Auditar ${fmt(regressions)} regressoes`
      : pending > 0
        ? `Fechar ${fmt(pending)} pendencias`
        : qualityDebtActionable
          ? `Priorizar ${fmt(qualityDebtSignals)} sinais de qualidade`
          : 'Congelar a proxima versao';

  const bandRows = qualityBandMeta.map((band) => ({
    band: band.label,
    range: band.range,
    color: band.color,
    oldCount: Number(oldBands[band.id] ?? 0),
    outputCount: Number(outputBands[band.id] ?? 0),
    oldPct: measuredSegments ? Number(((Number(oldBands[band.id] ?? 0) / measuredSegments) * 100).toFixed(2)) : 0,
    outputPct: measuredSegments ? Number(((Number(outputBands[band.id] ?? 0) / measuredSegments) * 100).toFixed(2)) : 0,
  }));

  // Chart contract: snapshots V3+ conectados visualmente, mesma população de
  // pacote e escala percentual focada com cinco intervalos simétricos.
  const storedVersionTrend = packageVersions
    .filter((item) => Number(item.version_number ?? 0) >= 3 && item.full_average_score != null)
    .map((item) => ({
      version: `V${item.version_number}`,
      score: Number((Number(item.full_average_score) * 100).toFixed(2)),
      coverage: Number(item.segment_count ?? 0)
        ? Number(((Number(item.measured_score_count ?? 0) / Number(item.segment_count)) * 100).toFixed(2))
        : 0,
      contract: item.score_rule_version ?? 'nao medido',
      kind: 'snapshot_operacional',
      qualityBands: item.quality_bands ?? null,
    }));
  const versionTrend = storedVersionTrend.length
    ? [
        ...storedVersionTrend,
        {
          version: `V${currentVersionNumber} candidato`,
          score: Number((outputScore * 100).toFixed(2)),
          coverage: Number((coverage * 100).toFixed(2)),
          contract: scoreContract.output?.rule_version ?? 'contrato atual',
          kind: 'snapshot_operacional',
          qualityBands: outputBands,
        },
      ]
    : [
        { version: 'Baseline', score: Number((oldScore * 100).toFixed(2)), coverage: Number((coverage * 100).toFixed(2)), kind: 'snapshot_operacional', qualityBands: oldBands },
        { version: 'Output', score: Number((outputScore * 100).toFixed(2)), coverage: Number((coverage * 100).toFixed(2)), kind: 'snapshot_operacional', qualityBands: outputBands },
      ];
  const scoreAxis = symmetricPercentAxis(versionTrend.map((item) => item.score));

  return (
    <div className="quality-overview flex h-full min-h-0 flex-col gap-3 overflow-y-auto pr-1 xl:overflow-hidden">
      <section className="flex shrink-0 flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-black text-[var(--dash-text)]">Visão Geral da Qualidade</h2>
          <p className="mt-1 text-xs text-[var(--dash-muted)]">
            Risco operacional, ganho pareado validado e concentração dos segmentos por faixa.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={operationallyClosed ? 'emerald' : 'amber'}>{operationallyClosed ? 'pacote fechado' : 'pacote aberto'}</Badge>
          <Badge tone={qualityDebtActionable ? 'amber' : qualityDebt.status === 'clear' ? 'emerald' : 'blue'}>{qualityDebtLabel}</Badge>
          <Badge tone={comparable ? 'emerald' : 'amber'}>{comparable ? 'contrato interno comparável' : 'contrato divergente'}</Badge>
          <Badge tone={crossVersionComparable ? 'emerald' : 'amber'}>{crossVersionComparable ? 'versões comparáveis' : baselineSensitive ? 'baseline-sensitive' : 'comparabilidade pendente'}</Badge>
          <Badge tone="blue">score #{scoreContract.old?.run_id ?? '-'} -&gt; #{scoreContract.output?.run_id ?? '-'}</Badge>
          <Badge tone={appState.cache?.stale ? 'amber' : 'emerald'}>{appState.cache?.stale ? 'cache defasado' : 'dados atuais'}</Badge>
        </div>
      </section>

      <section className="grid shrink-0 grid-cols-2 gap-3 lg:grid-cols-3 xl:grid-cols-6">
        <OverviewKpi label="Score operacional" value={scoreLabel(outputScore)} detail={`risco na epoch · baseline ${scoreLabel(oldScore)} · ${deltaLabel}`} tone="blue" icon={BarChart3} />
        <OverviewKpi label="Ganho pareado" value={pairwiseDeltaLabel} detail={pairwiseDetail} tone={pairwiseGainAvailable && Number(validatedPairwiseGain.regressed_count ?? 0) === 0 ? 'emerald' : 'amber'} icon={Scale} />
        <OverviewKpi label="Fechamento operacional" value={closureLabel} detail={`${fmt(operationalClosure.closed_count ?? release.closed_count ?? 0)} fechados · ${fmt(pending)} pendentes · ${fmt(needsApply)} apply`} tone={operationallyClosed ? 'emerald' : 'amber'} icon={ShieldCheck} />
        <OverviewKpi label="Dívida de qualidade" value={qualityDebtLabel} detail={qualityDebtDetail} tone={qualityDebtActionable ? 'amber' : qualityDebt.status === 'clear' ? 'emerald' : 'blue'} icon={qualityDebtActionable ? AlertTriangle : CheckCircle2} />
        <OverviewKpi label="Cobertura de provedores" value={providerCoverageLabel} detail={providerDetail} tone={providerHealth.status === 'healthy' ? 'emerald' : providerHealth.instrumented ? 'amber' : 'slate'} icon={Workflow} />
        <OverviewKpi label="Gate de publicação" value={releaseLabel} detail={releaseReady ? 'zero pendências, apply e regressões' : nextAction} tone={releaseReady ? 'emerald' : 'amber'} icon={releaseReady ? Rocket : AlertTriangle} />
      </section>

      <section className="grid min-h-[620px] flex-1 gap-3 xl:min-h-0 xl:grid-cols-[minmax(0,1.65fr)_minmax(340px,0.75fr)]">
        <Card className="quality-chart-card flex min-h-[360px] flex-col overflow-hidden p-4 xl:min-h-0">
          <div className="flex shrink-0 items-start justify-between gap-3">
              <div>
                <h3 className="text-sm font-black text-[var(--dash-text)]">Snapshots do score operacional por versão</h3>
                <p className="mt-1 text-xs text-[var(--dash-muted)]">V3 em diante · linha e área mostram a evolução visual; ganho entre versões usa o comparativo pareado.</p>
              </div>
              <Badge tone="blue">atual {scoreLabel(outputScore)}</Badge>
            </div>
            <div className="mt-4 min-h-[270px] flex-1">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={versionTrend} margin={{ top: 16, right: 20, left: -4, bottom: 4 }}>
                  <defs>
                    <linearGradient id="quality-version-score-gradient" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="var(--dash-accent)" stopOpacity={0.34} />
                      <stop offset="58%" stopColor="var(--dash-accent)" stopOpacity={0.12} />
                      <stop offset="100%" stopColor="var(--dash-accent)" stopOpacity={0.015} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 5" stroke="currentColor" opacity={0.09} vertical={false} />
                  <XAxis dataKey="version" tick={chartText} tickLine={false} axisLine={{ opacity: 0.18 }} />
                  <YAxis tick={chartText} domain={scoreAxis.domain} ticks={scoreAxis.ticks} tickFormatter={(value) => `${Number(value).toLocaleString('pt-BR', { maximumFractionDigits: 2 })}%`} tickLine={false} axisLine={false} />
                  <Area
                    type="monotone"
                    dataKey="score"
                    name="Score operacional"
                    isAnimationActive={false}
                    stroke="var(--dash-accent)"
                    strokeWidth={3}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    fill="url(#quality-version-score-gradient)"
                    dot={<QualityVersionDot />}
                    activeDot={false}
                  />
                </AreaChart>
              </ResponsiveContainer>
          </div>
        </Card>

        <Card className="quality-chart-card flex min-h-[360px] flex-col overflow-hidden p-4 xl:min-h-0">
          <div className="flex shrink-0 items-start justify-between gap-3">
              <div>
                <h3 className="text-sm font-black text-[var(--dash-text)]">Segmentos por faixa de score</h3>
                <p className="mt-1 text-xs text-[var(--dash-muted)]">Quantidade atual medida; o comparativo com a baseline fica no tooltip.</p>
              </div>
              <Badge tone={attentionCount ? 'red' : 'emerald'}>{compact(attentionCount)}</Badge>
            </div>
            <div className="mt-4 min-h-[270px] flex-1">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={bandRows} margin={{ top: 16, right: 8, left: 4, bottom: 4 }} barCategoryGap="24%">
                  <CartesianGrid strokeDasharray="3 5" stroke="currentColor" opacity={0.09} vertical={false} />
                  <XAxis dataKey="band" tick={chartText} tickLine={false} axisLine={{ opacity: 0.18 }} />
                  <YAxis tick={chartText} tickFormatter={(value) => compact(value)} tickLine={false} axisLine={false} />
                  <Bar dataKey="outputCount" name="Output atual" radius={[7, 7, 2, 2]} maxBarSize={64} isAnimationActive={false} cursor="pointer">
                    {bandRows.map((row) => {
                      const tooltip = qualityBandTooltipCopy(row);
                      return (
                        <Cell
                          key={row.band}
                          fill={row.color}
                          cursor="pointer"
                          role="img"
                          tabIndex="0"
                          aria-label={`${tooltip.title}. ${tooltip.description.replaceAll('\n', '. ')}`}
                          data-tooltip-title={tooltip.title}
                          data-tooltip-description={tooltip.description}
                          data-tooltip-meta={tooltip.meta}
                        />
                      );
                    })}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
          </div>
        </Card>
      </section>
    </div>
  );
}

const dashboardViewItems = [
  { id: 'overview', label: 'Visão Geral', subtitle: 'Índice global, evolução das versões e prioridades de qualidade.' },
  { id: 'network', label: 'Rede', subtitle: 'Arquitetura neuro-simbólica, agentes, microagentes e ligações.' },
];

const dashboardViewFromHash = () => {
  const hash = decodeURIComponent(window.location.hash.replace(/^#/, ''));
  const [, rawView] = hash.split('/');
  return dashboardViewItems.some((item) => item.id === rawView) ? rawView : 'overview';
};

function ProjectIntelligenceDashboard({ data }) {
  const [view, setView] = useState(dashboardViewFromHash);
  const appState = data.appState ?? {};
  const release = appState.release ?? {};
  const learning = appState.learning_gate ?? {};
  const production = appState.production ?? {};
  const lifecycle = data.lifecycle ?? {};
  const mlPerformance = data.mlPerformance ?? {};
  const agents = data.agents ?? {};
  const summary = data.production?.summary ?? lifecycle.taxonomy ?? {};
  const agentSummary = agents.summary ?? {};
  const learningByLabel = agents.learningByLabel ?? [];
  const learningByFocus = agents.learningByFocus ?? [];
  const modelTrend = mlPerformance.mlTrendByModel ?? mlPerformance.mlTrend ?? [];
  const datasetComposition = mlPerformance.datasetComposition ?? [];
  const outputEvolution = lifecycle.outputApply?.evolution ?? [];
  const packageBacklog = lifecycle.packageBacklog ?? [];
  const tokenBuckets = lifecycle.tokenPolicy?.bucketDistribution ?? [];
  const cache = appState.cache ?? {};

  useEffect(() => {
    const onHashChange = () => setView(dashboardViewFromHash());
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  const selectView = (nextView) => {
    setView(nextView);
    window.history.replaceState(null, '', `#${encodeURIComponent(`Dashboard/${nextView}`)}`);
    window.dispatchEvent(new Event('hashchange'));
  };

  if (view === 'network') {
    return <NeuralArchitecture data={data} />;
  }

  return <ProjectOverviewDashboard data={data} />;

  const latestModel = modelTrend.at(-1) ?? {};
  const previousModel = modelTrend.at(-2) ?? {};
  const pendingDistribution = [
    { name: 'Fechados', value: Number(release.closed_count ?? summary.closed_segments ?? 0), color: '#10b981' },
    { name: 'Pendentes', value: Number(release.pending_count ?? summary.raw_pending ?? 0), color: '#f59e0b' },
    { name: 'Needs apply', value: Number(release.needs_apply ?? summary.needs_apply ?? 0), color: '#3b82f6' },
  ].filter((item) => item.value > 0);
  const releaseTrendSource = (release.segment_state_trend?.length ? release.segment_state_trend : outputEvolution).slice(-12);
  const releaseTrend = releaseTrendSource.map((row) => ({
    run: `#${row.state_run_id ?? row.run_id ?? '-'}`,
    fechado: Number(row.closed_pct ?? row.closed_rate ?? release.closed_rate ?? 0),
    pendencias: Number(row.pending_count ?? row.pending ?? 0),
    apply: Number(row.output_apply_pending_count ?? row.needs_apply ?? 0),
  }));
  const previousDelta = release.previous_segment_state_delta ?? {};
  const productionDelta = release.since_last_production ?? {};
  const qualifiedPending = release.qualified_pending ?? {};
  const corpusTotal = Number(release.total_segments ?? 0) || Number(release.closed_count ?? 0) + Number(release.pending_count ?? 0) + Number(release.needs_apply ?? 0);
  const overviewNextAction = cache.stale
    ? 'Atualizar cache'
    : Number(release.needs_apply ?? 0) > 0
      ? 'Revisar needs_apply'
      : !learning.can_start_production
        ? 'Aguardar gate'
        : Number(release.pending_count ?? 0) > 0
          ? 'Revisar pendencias qualificadas'
          : 'Seguro para release';
  const qualifiedPendingCards = [
    { label: 'Acionaveis', value: qualifiedPending.actionable_pending, tone: Number(qualifiedPending.actionable_pending ?? 0) ? 'amber' : 'emerald' },
    { label: 'Contexto/dominio', value: qualifiedPending.context_domain ?? 'pending_instrumentation', tone: qualifiedPending.context_domain === 'pending_instrumentation' ? 'slate' : 'blue' },
    { label: 'Aguardando politica', value: qualifiedPending.policy_waiting, tone: Number(qualifiedPending.policy_waiting ?? 0) ? 'blue' : 'emerald' },
    { label: 'Alta incerteza', value: qualifiedPending.high_uncertainty, tone: Number(qualifiedPending.high_uncertainty ?? 0) ? 'violet' : 'emerald' },
  ];
  const learningCycleActive = Boolean(learning.current_phase_label || learning.status === 'running' || production.active);
  const learningCycleLabel = learning.current_phase_label || (learningCycleActive ? 'Ciclo em andamento' : 'Sem ciclo ativo');
  const learningCycleStatus = learningCycleActive
    ? (learning.status || 'em andamento')
    : (learning.can_start_production ? 'Ultima execucao concluida' : 'Aguardando liberacao');
  const learningResponsible = learning.next_action?.toLowerCase?.().includes('prompt') || learning.next_action?.toLowerCase?.().includes('chat')
    ? 'Chat Execucao'
    : 'Chat Treino';
  const learningNextAction = cache.stale
    ? 'Atualizar cache'
    : learning.next_action || (learning.can_start_production ? 'Aguardar proximo foco' : 'Liberar gate de aprendizado');
  const learningFocus = learning.next_focus ?? learning.focus ?? 'pending_instrumentation';
  const maturityCards = [
    { label: 'Policies ativas', value: agentSummary.active_policies ?? 'pending_instrumentation', tone: agentSummary.active_policies == null ? 'slate' : 'emerald' },
    { label: 'Microagentes ativos', value: agentSummary.operational_agents ?? agentSummary.agents_operational ?? 'pending_instrumentation', tone: (agentSummary.operational_agents ?? agentSummary.agents_operational) == null ? 'slate' : 'emerald' },
    { label: 'Lifecycle bridges', value: agentSummary.lifecycle_bridges ?? 'pending_instrumentation', tone: agentSummary.lifecycle_bridges == null ? 'slate' : 'violet' },
    { label: 'Watch', value: agentSummary.operational_false_safe ?? agentSummary.latest_false_safe ?? 0, tone: Number(agentSummary.operational_false_safe ?? agentSummary.latest_false_safe ?? 0) ? 'amber' : 'emerald' },
  ];
  const actionReadinessCards = [
    { label: 'Lifecycle pronto', value: qualifiedPending.policy_waiting ?? 'pending_instrumentation', tone: qualifiedPending.policy_waiting == null ? 'slate' : 'blue' },
    { label: 'Apply protegido', value: release.needs_apply ?? 0, tone: Number(release.needs_apply ?? 0) ? 'amber' : 'emerald' },
    { label: 'Precisa contexto', value: qualifiedPending.context_domain ?? 'pending_instrumentation', tone: qualifiedPending.context_domain === 'pending_instrumentation' ? 'slate' : 'amber' },
    { label: 'Novo microagente', value: agentSummary.recommendation_evidence ?? 'pending_instrumentation', tone: agentSummary.recommendation_evidence == null ? 'slate' : 'violet' },
  ];
  const recentLearningEvents = [
    release.latest_segment_state_run_id ? `Run #${release.latest_segment_state_run_id}: segment-state atualizado` : null,
    release.latest_ledger_run_id ? `Ledger #${release.latest_ledger_run_id}: sinais disponiveis` : null,
    productionDelta.available ? `Desde producao: +${compact(productionDelta.closed_delta)} fechados` : null,
  ].filter(Boolean);
  const learningEvidenceSource = learningByLabel.length
    ? learningByLabel
    : learningByFocus.length
      ? learningByFocus
      : [];
  const learningMix = learningEvidenceSource.slice(0, 8).map((row) => ({
    label: row.human_label ?? row.focus_group ?? row.label ?? row.name ?? 'evidencia',
    value: Number(row.total ?? row.value ?? row.count ?? 0),
    corrected: Number(row.corrected ?? row.corrected_total ?? 0),
  })).filter((row) => row.value > 0);
  const learningDataState = learningMix.length
    ? 'loaded'
    : data._fullDashboardLoaded
      ? 'empty'
      : 'loading';
  const pendingHotspots = (packageBacklog.length ? packageBacklog : tokenBuckets).slice(0, 8).map((row) => ({
    label: row.package_name ?? row.package ?? row.relative_path ?? row.policy_bucket ?? row.name ?? 'grupo',
    value: Number(row.pending_count ?? row.pending ?? row.total ?? row.count ?? 0),
  })).filter((row) => row.value > 0);
  const pendingFamilyHotspots = (release.pending_by_family ?? []).slice(0, 8).map((row) => ({
    label: row.label ?? row.issue_family ?? row.family ?? 'familia',
    value: Number(row.value ?? row.count ?? row.total ?? 0),
  })).filter((row) => row.value > 0);
  const pendingPrimaryData = pendingFamilyHotspots.length ? pendingFamilyHotspots : pendingHotspots;
  const pendingPrimaryTitle = pendingFamilyHotspots.length ? 'Gargalos por familia' : 'Pendencias por pacote';
  const pendingPrimarySubtitle = pendingFamilyHotspots.length
    ? 'Issue families pendentes no ledger mais recente.'
    : 'Fallback por pacote/path quando familia ainda nao esta instrumentada.';
  const pendingActionability = release.pending_actionability ?? {};
  const pendingNextFocus = release.pending_next_focus ?? {};
  const pendingNotApplicable = pendingActionability.status === 'not_applicable';
  const watchValue = (key) => pendingNotApplicable ? 'n/a' : (pendingActionability[key] ?? 'pending_instrumentation');
  const watchTone = (key, tone) => pendingNotApplicable || watchValue(key) === 'pending_instrumentation' ? 'slate' : tone;
  const pendingWatchCards = [
    { label: 'Precisa contexto', value: watchValue('needs_context'), tone: watchTone('needs_context', 'blue') },
    { label: 'Precisa dominio', value: watchValue('needs_domain'), tone: watchTone('needs_domain', 'violet') },
    { label: 'Novo microagente', value: watchValue('needs_new_microagent'), tone: watchTone('needs_new_microagent', 'violet') },
    { label: 'Reparo textual', value: watchValue('text_repair'), tone: watchTone('text_repair', 'amber') },
    { label: 'Alta incerteza', value: watchValue('high_uncertainty'), tone: watchTone('high_uncertainty', 'red') },
  ];
  const largestPendingVolume = pendingPrimaryData[0] ?? {};
  const readyActionCount = Number(release.needs_apply ?? 0)
    + Number(summary.actionable_pending ?? qualifiedPending.actionable_pending ?? 0)
    + Number(summary.governed_bridge_pending ?? qualifiedPending.policy_waiting ?? 0);
  const readyActionLabel = Number(release.needs_apply ?? 0) > 0
    ? 'needs_apply'
    : Number(summary.actionable_pending ?? qualifiedPending.actionable_pending ?? 0) > 0
      ? 'fila acionavel'
      : Number(summary.governed_bridge_pending ?? qualifiedPending.policy_waiting ?? 0) > 0
        ? 'ponte governada'
        : 'nenhuma fila segura';
  const pendingHealthBadges = [
    { label: `Needs apply: ${compact(release.needs_apply ?? 0)}`, tone: Number(release.needs_apply ?? 0) ? 'amber' : 'emerald' },
    { label: release.operational_integrity?.source_status === 'pending_instrumentation' ? 'Source: pending_instrumentation' : `Source: ${release.operational_integrity?.source_status}`, tone: release.operational_integrity?.source_status === 'pending_instrumentation' ? 'slate' : 'emerald' },
    { label: release.operational_integrity?.output_status === 'pending_instrumentation' ? 'Output: pending_instrumentation' : `Output: ${release.operational_integrity?.output_status}`, tone: release.operational_integrity?.output_status === 'pending_instrumentation' ? 'slate' : 'emerald' },
    { label: `Segment-state: #${release.latest_segment_state_run_id ?? '-'}`, tone: 'blue' },
  ];
  const qualityCards = [
    { label: 'Modelo atual', value: latestModel.modelVersion ?? mlPerformance.kpis?.active_model ?? 'pendente', tone: 'blue' },
    { label: 'Macro F1', value: latestModel.macroF1 != null ? pctMetric(latestModel.macroF1) : pct(mlPerformance.kpis?.macro_f1), tone: 'emerald' },
    { label: 'Falso seguro', value: compact(agentSummary.operational_false_safe ?? mlPerformance.kpis?.false_safe ?? 0), tone: Number(agentSummary.operational_false_safe ?? 0) ? 'red' : 'emerald' },
    { label: 'Ganho rede', value: compact(agentSummary.ensemble_gain ?? agentSummary.active_gate_guarded_releases ?? 0), tone: 'violet' },
  ];
  const currentF1 = latestModel.macroF1 != null ? Number(latestModel.macroF1) : Number(mlPerformance.kpis?.macroF1 ?? 0);
  const previousF1 = previousModel.macroF1 != null ? Number(previousModel.macroF1) : null;
  const deltaF1 = previousF1 == null ? null : currentF1 - previousF1;
  const currentFalseSafe = Number(latestModel.falseSafe ?? agentSummary.operational_false_safe ?? 0);
  const previousFalseSafe = Number(previousModel.falseSafe ?? 0);
  const currentSafePrecision = latestModel.safePrecision != null ? Number(latestModel.safePrecision) : Number(mlPerformance.kpis?.safePrecision ?? 0);
  const currentSafeRecall = latestModel.safeRecall != null ? Number(latestModel.safeRecall) : Number(mlPerformance.kpis?.holdoutCoverage ?? 0);
  const networkGain = Number(agentSummary.ensemble_gain ?? agentSummary.active_gate_guarded_releases ?? 0);
  const qualityStatus = currentFalseSafe > 0
    ? 'bloquear'
    : deltaF1 == null
      ? 'manter'
      : deltaF1 > 0
        ? 'promover'
        : deltaF1 < -0.02
          ? 'auditar'
          : 'manter';
  const qualityTone = qualityStatus === 'bloquear'
    ? 'red'
    : qualityStatus === 'auditar'
      ? 'amber'
      : qualityStatus === 'promover'
        ? 'emerald'
        : 'blue';
  const qualityReason = currentFalseSafe > 0
    ? 'falso seguro detectado no modelo atual'
    : deltaF1 == null
      ? 'sem modelo anterior suficiente para delta'
      : deltaF1 > 0
        ? 'F1 melhorou e falso seguro esta zerado'
        : deltaF1 < 0
          ? 'F1 caiu vs anterior, mas falso seguro permanece zerado'
          : 'F1 estavel e falso seguro zerado';
  const qualityNextAction = qualityStatus === 'bloquear'
    ? 'bloquear promocao e auditar falso seguro'
    : qualityStatus === 'auditar'
      ? 'auditar regressao de F1'
      : qualityStatus === 'promover'
        ? 'avaliar promocao do candidato'
        : 'manter modelo atual';
  const f1ComparisonData = [
    { label: 'Anterior', f1: previousF1 == null ? 0 : Number((previousF1 * 100).toFixed(2)) },
    { label: 'Atual', f1: Number((currentF1 * 100).toFixed(2)) },
  ];
  const gainBreakdown = [
    { label: 'Modelo', value: 'pending_instrumentation', tone: 'slate' },
    { label: 'Lifecycle/policies', value: 'pending_instrumentation', tone: 'slate' },
    { label: 'Reparos protegidos', value: 'pending_instrumentation', tone: 'slate' },
    { label: 'Ganho consolidado', value: compact(networkGain), tone: networkGain ? 'violet' : 'slate' },
  ];
  const qualityRiskCards = [
    { label: 'Delta F1', value: deltaF1 == null ? 'pending_instrumentation' : `${deltaF1 >= 0 ? '+' : ''}${(deltaF1 * 100).toLocaleString('pt-BR', { maximumFractionDigits: 2 })} p.p.`, tone: deltaF1 == null ? 'slate' : deltaF1 >= 0 ? 'emerald' : currentFalseSafe ? 'red' : 'amber' },
    { label: 'Falso seguro atual', value: fmt(currentFalseSafe), tone: currentFalseSafe ? 'red' : 'emerald' },
    { label: 'Falso seguro anterior', value: fmt(previousFalseSafe), tone: previousFalseSafe ? 'red' : 'emerald' },
    { label: 'Holdout/Safe recall', value: pctMetric(currentSafeRecall), tone: 'blue' },
  ];
  const cacheAttention = Boolean(appState.cache?.stale);
  const gateOk = Boolean(learning.can_start_production);
  const needsApplyOk = Number(release.needs_apply ?? 0) === 0;
  const outputCoverageOk = Number(release.output_coverage ?? 0) > 99;
  const runIdle = !production.active;
  const integrity = release.operational_integrity ?? {};
  const sourceKnown = integrity.source_status && integrity.source_status !== 'pending_instrumentation';
  const outputKnown = integrity.output_status && integrity.output_status !== 'pending_instrumentation';
  const productionOutdated = Boolean(productionDelta.available && (Number(productionDelta.closed_delta ?? 0) > 0 || Number(productionDelta.pending_delta ?? 0) < 0 || Number(productionDelta.needs_apply_delta ?? 0) !== 0));
  const releaseBlocked = !gateOk || !needsApplyOk || !runIdle || integrity.source_status === 'attention' || integrity.output_status === 'attention';
  const releaseAttention = !releaseBlocked && (cacheAttention || productionOutdated || !sourceKnown || !outputKnown);
  const productionReadiness = releaseBlocked ? 'bloqueada' : releaseAttention ? 'pronta com atencao' : 'pronta';
  const publicationReadiness = productionOutdated
    ? 'aguardando nova producao'
    : releaseBlocked
      ? 'bloqueada'
      : cacheAttention
        ? 'atualize cache antes'
        : 'pronta para avaliar';
  const releaseTone = releaseBlocked ? 'red' : releaseAttention ? 'amber' : 'emerald';
  const releaseNextAction = production.active
    ? 'aguardar run atual finalizar'
    : cacheAttention
      ? 'atualizar cache'
      : !gateOk
        ? 'aguardar learning gate'
        : !needsApplyOk
          ? 'revisar needs_apply'
          : productionOutdated
            ? 'rodar nova producao'
            : 'avaliar publicacao';
  const releaseChecklist = [
    {
      label: 'Cache local',
      status: cacheAttention ? 'atencao' : 'ok',
      tone: cacheAttention ? 'amber' : 'emerald',
      value: appState.cache?.generated_at ? shortDateTime(appState.cache.generated_at) : 'nao gerado',
      detail: cacheAttention ? 'SQLite mudou desde o cache; atualize antes de decidir release.' : 'Cache alinhado para leitura visual.',
    },
    {
      label: 'Learning gate',
      status: gateOk ? 'ok' : 'bloqueado',
      tone: gateOk ? 'emerald' : 'red',
      value: gateOk ? 'Liberado' : 'Bloqueado',
      detail: learning.reason ?? learning.status ?? 'Sem detalhe do gate.',
    },
    {
      label: 'Needs apply',
      status: needsApplyOk ? 'ok' : 'bloqueado',
      tone: needsApplyOk ? 'emerald' : 'red',
      value: fmt(release.needs_apply),
      detail: needsApplyOk ? 'Nao ha escrita pendente no output.' : 'Existem alteracoes aguardando aplicacao.',
    },
    {
      label: 'Output coverage',
      status: outputCoverageOk ? 'ok' : 'atencao',
      tone: outputCoverageOk ? 'emerald' : 'amber',
      value: pct(release.output_coverage),
      detail: 'Cobertura de segmentos ativos com output.',
    },
    {
      label: 'Run ativa',
      status: runIdle ? 'ok' : 'bloqueado',
      tone: runIdle ? 'emerald' : 'red',
      value: production.active ? production.current_stage ?? 'running' : 'livre',
      detail: runIdle ? 'Nenhuma execucao em andamento.' : 'Aguarde finalizar antes de release.',
    },
    {
      label: 'Source limpo',
      status: sourceKnown ? 'ok' : 'desconhecido',
      tone: sourceKnown ? 'emerald' : 'slate',
      value: integrity.source_status ?? 'pending_instrumentation',
      detail: sourceKnown ? 'Sinal operacional disponivel.' : 'Ainda sem instrumentacao direta de source.',
    },
    {
      label: 'Output alinhado',
      status: outputKnown ? 'ok' : 'desconhecido',
      tone: outputKnown ? 'emerald' : 'slate',
      value: integrity.output_status ?? 'pending_instrumentation',
      detail: outputKnown ? 'Sinal operacional disponivel.' : 'Ainda sem instrumentacao direta de output.',
    },
    {
      label: 'Segment-state atual',
      status: release.latest_segment_state_run_id ? 'ok' : 'desconhecido',
      tone: release.latest_segment_state_run_id ? 'blue' : 'slate',
      value: `#${release.latest_segment_state_run_id ?? '-'}`,
      detail: release.segment_state_finished_at ? shortDateTime(release.segment_state_finished_at) : 'pending_instrumentation',
    },
    {
      label: 'Ultima producao',
      status: productionOutdated ? 'atencao' : production.last_run?.run_id ? 'ok' : 'desconhecido',
      tone: productionOutdated ? 'amber' : production.last_run?.run_id ? 'blue' : 'slate',
      value: production.last_run?.run_id ?? 'pending_instrumentation',
      detail: production.last_run?.finished_at ? `${ageLabel(production.last_run.finished_at)} atras` : 'Sem timestamp final.',
    },
  ];
  const releaseExecutionCards = [
    { label: 'Run', value: production.last_run?.run_id ?? '-', tone: 'blue' },
    { label: 'Status', value: statusLabel(production.last_run?.status ?? 'idle'), tone: statusTone(production.last_run?.status) },
    { label: 'Etapa final', value: production.current_stage ?? production.last_run?.current_stage ?? '-', tone: 'slate' },
    { label: 'Progresso', value: `${production.progress_pct ?? 0}%`, tone: Number(production.progress_pct ?? 0) === 100 ? 'emerald' : 'blue' },
    { label: 'Segment-state', value: production.last_run?.summary?.segment_state_run_id ? `#${production.last_run.summary.segment_state_run_id}` : `#${release.latest_segment_state_run_id ?? '-'}`, tone: 'blue' },
    { label: 'Ledger', value: production.last_run?.summary?.ledger_run_id ? `#${production.last_run.summary.ledger_run_id}` : `#${release.latest_ledger_run_id ?? '-'}`, tone: 'violet' },
    { label: 'Output coverage', value: pct(release.output_coverage), tone: 'emerald' },
    { label: 'Idade', value: ageLabel(production.last_run?.finished_at), tone: productionOutdated ? 'amber' : 'slate' },
  ];

  if (view === 'network') {
    return <NeuralArchitecture data={data} />;
  }

  return (
    <div className="flex h-full min-h-0 flex-col gap-3 pb-0">
      {view === 'overview' && (
        <>
          <div className="grid shrink-0 grid-cols-2 gap-3 xl:grid-cols-6">
            <MetricTile title="Fechados" value={`${pct(release.closed_rate)} · ${compact(release.closed_count)}`} tone="emerald" />
            <MetricTile title="Pendencias" value={compact(release.pending_count)} tone={release.pending_count ? 'amber' : 'emerald'} />
            <MetricTile title="Needs Apply" value={compact(release.needs_apply)} tone={release.needs_apply ? 'amber' : 'emerald'} />
            <MetricTile title="Cobertura de Output" value={pct(release.output_coverage)} tone="blue" />
            <MetricTile title="Segment-state" value={`#${release.latest_segment_state_run_id ?? '-'}`} tone="slate" />
            <MetricTile title="Gate" value={learning.can_start_production ? 'Liberado' : 'Bloqueado'} tone={learning.can_start_production ? 'emerald' : 'red'} />
          </div>
          <div className="grid min-h-0 flex-1 gap-4 xl:grid-cols-[1.25fr_0.75fr]">
            <Card className="flex min-h-0 flex-col p-5">
              <div className="mb-4 flex shrink-0 items-start justify-between gap-3">
                <div>
                  <h3 className="text-sm font-black text-[var(--dash-text)]">{releaseTrend.length > 1 ? 'Evolucao de fechamento' : 'Snapshot atual'}</h3>
                  <p className="text-xs text-[var(--dash-muted)]">
                    Total do corpus: <span className="font-bold text-[var(--dash-text)]">{compact(corpusTotal)}</span> · dados {cache.stale ? 'defasados' : 'atualizados'}
                  </p>
                </div>
                <Badge tone={release.pending_count ? 'amber' : 'emerald'}>{release.readiness ?? 'status'}</Badge>
              </div>
              <div className="mb-3 flex shrink-0 items-center justify-between gap-3 rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] px-3 py-2">
                <p className="text-[10px] font-black uppercase tracking-wide text-[var(--dash-muted)]">Proxima acao macro</p>
                <p className="truncate text-xs font-black text-[var(--dash-text)]">{overviewNextAction}</p>
              </div>
              <div className="min-h-0 flex-1">
                {releaseTrend.length > 1 ? (
                  <ResponsiveContainer width="100%" height="100%">
                    <ComposedChart data={releaseTrend}>
                      <CartesianGrid strokeDasharray="3 3" stroke="currentColor" opacity={0.08} />
                      <XAxis dataKey="run" tick={chartText} />
                      <YAxis yAxisId="left" tick={chartText} domain={[0, 100]} tickFormatter={(value) => `${value}%`} />
                      <YAxis yAxisId="right" orientation="right" tick={chartText} tickFormatter={(value) => compact(value)} />
                      <Tooltip content={<ModelTooltip />} />
                      <Legend />
                      <Bar yAxisId="right" dataKey="pendencias" name="Pendentes" fill="#f59e0b" radius={[5, 5, 0, 0]} />
                      <Line yAxisId="left" type="monotone" dataKey="fechado" name="% fechado" stroke="#10b981" strokeWidth={3} dot={false} />
                    </ComposedChart>
                  </ResponsiveContainer>
                ) : (
                  <div className="grid h-full grid-cols-2 gap-3">
                    <MetricTile title="Run atual" value={`#${release.latest_segment_state_run_id ?? '-'}`} tone="blue" />
                    <MetricTile title="Total corpus" value={compact(corpusTotal)} tone="slate" />
                    <MetricTile title="Fechados" value={compact(release.closed_count)} tone="emerald" />
                    <MetricTile title="Pendentes" value={compact(release.pending_count)} tone="amber" />
                    <MetricTile title="Delta run anterior" value={previousDelta.available ? `+${compact(previousDelta.closed_delta)} / ${compact(previousDelta.pending_delta)}` : 'pending_instrumentation'} tone={previousDelta.available ? 'emerald' : 'slate'} />
                    <MetricTile title="Desde producao" value={productionDelta.available ? `+${compact(productionDelta.closed_delta)}` : 'pending_instrumentation'} tone={productionDelta.available ? 'violet' : 'slate'} />
                  </div>
                )}
              </div>
            </Card>
            <Card className="flex min-h-0 flex-col p-5">
              <div className="shrink-0">
                <h3 className="text-sm font-black text-[var(--dash-text)]">Distribuicao atual</h3>
                <p className="mt-1 text-xs text-[var(--dash-muted)]">Fechado, pendente e aplicacao pendente com leitura direta.</p>
              </div>
              <div className="mt-4 grid min-h-0 flex-1 grid-rows-[1fr_auto] gap-3">
                <div className="relative min-h-0">
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Pie data={pendingDistribution} innerRadius={62} outerRadius={100} dataKey="value" nameKey="name" paddingAngle={2}>
                        {pendingDistribution.map((entry) => <Cell key={entry.name} fill={entry.color} />)}
                      </Pie>
                      <Tooltip formatter={(value) => fmt(value)} />
                    </PieChart>
                  </ResponsiveContainer>
                  <div className="pointer-events-none absolute inset-0 grid place-items-center text-center">
                    <div>
                      <p className="text-[10px] font-black uppercase tracking-wide text-[var(--dash-muted)]">Corpus</p>
                      <p className="text-xl font-black text-[var(--dash-text)]">{compact(corpusTotal)}</p>
                    </div>
                  </div>
                </div>
                <div className="grid grid-cols-3 gap-2">
                  <MetricTile title="Fechados" value={compact(release.closed_count)} tone="emerald" />
                  <MetricTile title="Pendentes" value={compact(release.pending_count)} tone="amber" />
                  <MetricTile title="Needs apply" value={compact(release.needs_apply)} tone={release.needs_apply ? 'amber' : 'emerald'} />
                </div>
                <div className="rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-2.5">
                  <div className="mb-2 flex items-center justify-between">
                    <h4 className="text-[10px] font-black uppercase tracking-wide text-[var(--dash-muted)]">Pendencia qualificada</h4>
                    <Badge tone={cache.stale ? 'amber' : 'emerald'}>{cache.stale ? 'cache defasado' : 'dados atuais'}</Badge>
                  </div>
                  <div className="grid grid-cols-2 gap-2">
                    {qualifiedPendingCards.map((item) => (
                      <MetricTile key={item.label} title={item.label} value={typeof item.value === 'number' ? compact(item.value) : item.value ?? 'pending_instrumentation'} tone={item.tone} />
                    ))}
                  </div>
                </div>
              </div>
            </Card>
          </div>
        </>
      )}

      {view === 'learning' && (
        <div className="grid min-h-0 flex-1 gap-4 xl:grid-cols-[0.75fr_1.25fr]">
          <div className="grid min-h-0 gap-3">
            <Card className="p-5">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h3 className="text-sm font-black text-[var(--dash-text)]">Estado do aprendizado</h3>
                  <p className="mt-1 text-xs text-[var(--dash-muted)]">Gate, ciclo e responsavel operacional.</p>
                </div>
                <Badge tone={learning.can_start_production ? 'emerald' : 'amber'}>{learning.can_start_production ? 'Liberado' : 'Em treino'}</Badge>
              </div>
              <div className="mt-4 grid gap-2">
                <MetricTile title="Gate" value={learning.can_start_production ? 'Liberado' : 'Bloqueado'} tone={learning.can_start_production ? 'emerald' : 'amber'} />
                <MetricTile title="Ciclo atual" value={learningCycleLabel} tone="blue" />
                <MetricTile title="Status do ciclo" value={learningCycleStatus} tone={learningCycleActive ? 'blue' : 'emerald'} />
                <MetricTile title="Responsavel" value={learningResponsible} tone="violet" />
              </div>
            </Card>

            <Card className="p-5">
              <h3 className="text-sm font-black text-[var(--dash-text)]">Proxima acao</h3>
              <div className="mt-3 rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-3">
                <p className="text-[10px] font-black uppercase tracking-wide text-[var(--dash-muted)]">Acao recomendada</p>
                <p className="mt-1 text-sm font-black text-[var(--dash-text)]">{learningNextAction}</p>
                <div className="mt-3 flex flex-wrap gap-1.5">
                  <Badge tone="violet">Responsavel: {learningResponsible}</Badge>
                  <Badge tone={learningCycleActive ? 'blue' : 'emerald'}>{learningCycleActive ? 'Em andamento' : 'Aguardando'}</Badge>
                </div>
              </div>
            </Card>

            <Card className="p-5">
              <h3 className="text-sm font-black text-[var(--dash-text)]">Proximo foco</h3>
              <div className="mt-3 grid gap-2">
                <MetricTile title="Foco recomendado" value={learningFocus} tone={learningFocus === 'pending_instrumentation' ? 'slate' : 'violet'} />
                <MetricTile title="Motivo" value={learning.reason ?? 'pending_instrumentation'} tone={learning.reason ? 'blue' : 'slate'} />
              </div>
            </Card>
          </div>

          <div className="grid min-h-0 gap-3">
            <Card className="flex min-h-0 flex-col p-5">
              <div className="flex shrink-0 items-start justify-between gap-3">
                <div>
                  <h3 className="text-sm font-black text-[var(--dash-text)]">Evidencia de aprendizado</h3>
                  <p className="mt-1 text-xs text-[var(--dash-muted)]">Sinais fortes podem ser subconjuntos da evidencia revisada.</p>
                </div>
                <Badge tone="violet">evidencia</Badge>
              </div>
              <div className="mt-4 min-h-0 flex-1">
                {learningMix.length ? (
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={learningMix} margin={{ top: 12, right: 12, left: 0, bottom: 8 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="currentColor" opacity={0.08} />
                      <XAxis dataKey="label" tick={chartText} interval={0} angle={-10} textAnchor="end" height={68} />
                      <YAxis tick={chartText} tickFormatter={(value) => compact(value)} />
                      <Tooltip formatter={(value, name) => [fmt(value), name === 'corrected' ? 'corrigidos' : 'revisados']} />
                      <Bar dataKey="value" name="revisados" fill="#8b5cf6" radius={[5, 5, 0, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                ) : (
                  <div className="flex h-full min-h-[220px] items-center justify-center rounded-2xl border border-dashed border-[var(--dash-border)] bg-[var(--dash-subtle)] text-center">
                    <div>
                      <p className="text-sm font-black text-[var(--dash-text)]">
                        {learningDataState === 'loading' ? 'Carregando evidencias revisadas' : 'Sem evidencia revisada instrumentada'}
                      </p>
                      <p className="mt-1 text-xs text-[var(--dash-muted)]">
                        {learningDataState === 'loading'
                          ? 'Aguardando o payload analitico completo do dashboard.'
                          : 'O banco nao retornou learningByLabel/learningByFocus para este cache.'}
                      </p>
                    </div>
                  </div>
                )}
              </div>
            </Card>

            <div className="grid gap-3 lg:grid-cols-2">
              <Card className="p-4">
                <h3 className="text-sm font-black text-[var(--dash-text)]">Maturidade da rede</h3>
                <div className="mt-3 grid grid-cols-2 gap-2">
                  {maturityCards.map((item) => (
                    <MetricTile key={item.label} title={item.label} value={typeof item.value === 'number' ? compact(item.value) : item.value} tone={item.tone} />
                  ))}
                </div>
              </Card>
              <Card className="p-4">
                <h3 className="text-sm font-black text-[var(--dash-text)]">Pronto para acao</h3>
                <div className="mt-3 grid grid-cols-2 gap-2">
                  {actionReadinessCards.map((item) => (
                    <MetricTile key={item.label} title={item.label} value={typeof item.value === 'number' ? compact(item.value) : item.value} tone={item.tone} />
                  ))}
                </div>
              </Card>
            </div>

            <Card className="p-4">
              <div className="flex items-center justify-between gap-3">
                <h3 className="text-sm font-black text-[var(--dash-text)]">Ultimas acoes</h3>
                <Badge tone={recentLearningEvents.length ? 'blue' : 'slate'}>{recentLearningEvents.length ? 'resumo' : 'pending_instrumentation'}</Badge>
              </div>
              <div className="mt-3 grid gap-2 md:grid-cols-3">
                {(recentLearningEvents.length ? recentLearningEvents : ['pending_instrumentation']).slice(0, 3).map((event) => (
                  <div key={event} className="rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] px-3 py-2 text-xs font-bold text-[var(--dash-text)]">
                    {event}
                  </div>
                ))}
              </div>
            </Card>
          </div>
        </div>
      )}

      {view === 'pending' && (
        <div className="grid min-h-0 flex-1 gap-4 xl:grid-cols-[1.15fr_0.85fr]">
          <Card className="flex min-h-0 flex-col p-5">
            <div className="flex shrink-0 items-start justify-between gap-3">
              <div>
                <h3 className="text-sm font-black text-[var(--dash-text)]">{pendingPrimaryTitle}</h3>
                <p className="mt-1 text-xs text-[var(--dash-muted)]">{pendingPrimarySubtitle}</p>
              </div>
              <Badge tone={pendingFamilyHotspots.length ? 'violet' : 'amber'}>{pendingFamilyHotspots.length ? 'familia' : 'pacote'}</Badge>
            </div>
            <div className="mt-4 min-h-0 flex-1">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={pendingPrimaryData} layout="vertical" margin={{ left: 24, right: 24 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="currentColor" opacity={0.08} />
                  <XAxis type="number" tick={chartText} tickFormatter={(value) => compact(value)} />
                  <YAxis type="category" dataKey="label" tick={chartText} width={190} />
                  <Tooltip
                    formatter={(value) => [fmt(value), 'pendencias']}
                    labelFormatter={(label) => `${pendingFamilyHotspots.length ? 'Familia' : 'Pacote'}: ${label}`}
                  />
                  <Bar dataKey="value" fill="#f59e0b" radius={[0, 5, 5, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
            <div className="mt-3 grid shrink-0 gap-2 md:grid-cols-2">
              <MetricTile
                title="Maior volume"
                value={largestPendingVolume.label ? `${largestPendingVolume.label} - ${compact(largestPendingVolume.value)}` : 'pending_instrumentation'}
                tone={largestPendingVolume.label ? 'amber' : 'slate'}
              />
              <MetricTile
                title="Maior acao pronta"
                value={readyActionCount ? `${readyActionLabel} - ${compact(readyActionCount)}` : 'nenhuma fila segura'}
                tone={readyActionCount ? 'emerald' : 'slate'}
              />
            </div>
          </Card>
          <div className="grid min-h-0 gap-2">
            <Card className="p-3">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h3 className="text-sm font-black text-[var(--dash-text)]">Leitura operacional</h3>
                  <p className="mt-1 text-xs text-[var(--dash-muted)]">Volume bruto separado de filas realmente acionaveis.</p>
                </div>
                <Badge tone={(summary.actionable_pending ?? 0) ? 'amber' : 'emerald'}>{(summary.actionable_pending ?? 0) ? 'acao aberta' : 'sem fila manual'}</Badge>
              </div>
              <div className="mt-2 grid grid-cols-2 gap-2">
                <MetricTile className="p-2" title="Pendencia bruta" value={compact(release.pending_count)} tone="amber" />
                <MetricTile className="p-2" title="Watch ML" value={compact(summary.model_suspicion_watch ?? qualifiedPending.high_uncertainty ?? 0)} tone="violet" />
                <MetricTile className="p-2" title="Ponte pendente" value={compact(summary.governed_bridge_pending ?? qualifiedPending.policy_waiting ?? 0)} tone="blue" />
                <MetricTile className="p-2" title="Acionavel" value={compact(summary.actionable_pending ?? qualifiedPending.actionable_pending ?? 0)} tone={(summary.actionable_pending ?? qualifiedPending.actionable_pending ?? 0) ? 'amber' : 'emerald'} />
              </div>
              <p className="mt-2 line-clamp-2 rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] px-2.5 py-1.5 text-[0.72rem] leading-4 text-[var(--dash-muted)]" title="Acionavel 0 significa que nao ha fila manual segura aberta agora; Watch alto indica volume aguardando contexto, politica ou microagente.">
                Acionavel 0 significa que nao ha fila manual segura aberta agora; Watch alto indica volume aguardando contexto, politica ou microagente.
              </p>
            </Card>

            <Card className="p-3">
              <div className="flex items-center justify-between gap-3">
                <h3 className="text-sm font-black text-[var(--dash-text)]">Watch ML por causa</h3>
                <Badge tone={pendingActionability.instrumented_pending ? 'violet' : 'slate'}>{pendingActionability.instrumented_pending ? `ledger #${pendingActionability.ledger_run_id ?? '-'}` : 'pending_instrumentation'}</Badge>
              </div>
              <div className="mt-2 grid grid-cols-2 gap-2">
                {pendingWatchCards.map((item) => (
                  <MetricTile
                    key={item.label}
                    className="p-2"
                    title={item.label}
                    value={typeof item.value === 'number' ? compact(item.value) : item.value}
                    tone={item.tone}
                  />
                ))}
              </div>
            </Card>

            <Card className="p-3">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h3 className="text-sm font-black text-[var(--dash-text)]">Proximo gargalo</h3>
                  <p className="mt-1 text-xs text-[var(--dash-muted)]">Escolha sugerida pelos sinais atuais, sem hardcode.</p>
                </div>
                <Badge tone={pendingNextFocus.label === 'pending_instrumentation' ? 'slate' : 'violet'}>{pendingNextFocus.status ?? 'pending_instrumentation'}</Badge>
              </div>
              <div className="mt-2 grid gap-2">
                <MetricTile className="p-2" title="Foco recomendado" value={pendingNextFocus.label ?? 'pending_instrumentation'} tone={pendingNextFocus.label === 'pending_instrumentation' ? 'slate' : 'violet'} />
                <MetricTile className="p-2" title="Motivo" value={pendingNextFocus.reason ?? 'pending_instrumentation'} tone={pendingNextFocus.reason ? 'blue' : 'slate'} />
              </div>
              <div className="mt-2 flex flex-wrap gap-1">
                {pendingHealthBadges.map((item) => (
                  <Badge key={item.label} tone={item.tone}>{item.label}</Badge>
                ))}
              </div>
            </Card>
          </div>
        </div>
      )}

      {view === 'release' && (
        <div className="grid min-h-0 flex-1 gap-4 xl:grid-cols-[0.9fr_1.1fr]">
          <div className="grid min-h-0 gap-3">
            <Card className="p-5">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h3 className="text-sm font-black text-[var(--dash-text)]">Status de release</h3>
                  <p className="mt-1 text-xs text-[var(--dash-muted)]">Prontidao para rodar producao e decidir publicacao.</p>
                </div>
                <Badge tone={releaseTone}>{releaseBlocked ? 'bloqueado' : releaseAttention ? 'atencao' : 'pronto'}</Badge>
              </div>
              <div className="mt-4 grid grid-cols-2 gap-2">
                <MetricTile title="Producao" value={productionReadiness} tone={releaseTone} />
                <MetricTile title="Release" value={publicationReadiness} tone={publicationReadiness.includes('aguardando') ? 'amber' : releaseTone} />
              </div>
              <div className="mt-3 rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-3">
                <p className="text-[10px] font-black uppercase tracking-wide text-[var(--dash-muted)]">Proxima acao segura</p>
                <p className="mt-1 text-base font-black text-[var(--dash-text)]">{releaseNextAction}</p>
              </div>
            </Card>

            <Card className="flex min-h-0 flex-col p-5">
              <div className="flex items-center justify-between gap-3">
                <h3 className="text-sm font-black text-[var(--dash-text)]">Checklist de release</h3>
                <Badge tone={releaseTone}>{releaseChecklist.filter((item) => item.status === 'ok').length}/{releaseChecklist.length} ok</Badge>
              </div>
              <div className="dashboard-card-scroll mt-4 grid min-h-0 max-h-[calc(100vh-500px)] gap-2 overflow-y-auto pr-1">
                {releaseChecklist.map((item) => (
                  <div key={item.label} className="grid grid-cols-[1fr_auto] gap-3 rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-2">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <p className="truncate text-sm font-black text-[var(--dash-text)]">{item.label}</p>
                        <p className="truncate text-xs font-bold text-[var(--dash-muted)]">{item.value}</p>
                      </div>
                      <p className="mt-0.5 line-clamp-1 text-xs text-[var(--dash-muted)]" title={item.detail}>{item.detail}</p>
                    </div>
                    <Badge tone={item.tone}>{item.status}</Badge>
                  </div>
                ))}
              </div>
            </Card>
          </div>

          <div className="grid min-h-0 gap-3">
            <Card className="p-5">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h3 className="text-sm font-black text-[var(--dash-text)]">Estado atual vs ultima producao</h3>
                  <p className="mt-1 text-xs text-[var(--dash-muted)]">Mostra se a rede ja evoluiu depois do ultimo pacote produzido.</p>
                </div>
                <Badge tone={productionOutdated ? 'amber' : productionDelta.available ? 'emerald' : 'slate'}>
                  {productionOutdated ? 'producao defasada' : productionDelta.available ? 'alinhado' : 'pending_instrumentation'}
                </Badge>
              </div>
              <div className="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
                <MetricTile title="Segment-state atual" value={`#${release.latest_segment_state_run_id ?? '-'}`} tone="blue" />
                <MetricTile title="Ultima producao" value={productionDelta.last_production_run_id ?? production.last_run?.run_id ?? 'pending_instrumentation'} tone="slate" />
                <MetricTile title="Ganho pos-producao" value={productionDelta.available ? `+${compact(productionDelta.closed_delta)}` : 'pending_instrumentation'} tone={productionDelta.available ? 'emerald' : 'slate'} />
                <MetricTile title="Pendencias reduzidas" value={productionDelta.available ? compact(Math.abs(Number(productionDelta.pending_delta ?? 0))) : 'pending_instrumentation'} tone={productionDelta.available ? 'amber' : 'slate'} />
              </div>
              <div className="mt-3 flex flex-wrap gap-1.5">
                <Badge tone="blue">Baseline: #{productionDelta.baseline_segment_state_run_id ?? '-'}</Badge>
                <Badge tone={Number(productionDelta.needs_apply_delta ?? 0) === 0 ? 'emerald' : 'amber'}>Needs apply delta: {productionDelta.available ? fmt(productionDelta.needs_apply_delta) : 'pending_instrumentation'}</Badge>
                <Badge tone={cache.stale ? 'amber' : 'emerald'}>{cache.stale ? 'cache defasado' : 'cache atual'}</Badge>
              </div>
            </Card>

            <Card className="p-5">
              <div className="flex items-center justify-between gap-3">
                <h3 className="text-sm font-black text-[var(--dash-text)]">Ultima execucao</h3>
                <Badge tone={productionOutdated ? 'amber' : statusTone(production.last_run?.status)}>{productionOutdated ? 'defasada' : statusLabel(production.last_run?.status ?? 'idle')}</Badge>
              </div>
              <div className="mt-4 grid grid-cols-2 gap-2 lg:grid-cols-4">
                {releaseExecutionCards.map((item) => (
                  <MetricTile key={item.label} title={item.label} value={item.value} tone={item.tone} />
                ))}
              </div>
              <div className="mt-3 rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-3">
                <p className="text-[10px] font-black uppercase tracking-wide text-[var(--dash-muted)]">Resultado</p>
                <p className="mt-1 text-sm font-black text-[var(--dash-text)]">{production.last_run?.message ?? 'Sem run ativa. O cache preserva a ultima leitura ate novo ciclo ou refresh.'}</p>
                <p className="mt-2 truncate text-xs text-[var(--dash-muted)]" title={production.last_run?.report_path ?? production.last_run?.summary?.report_path ?? ''}>
                  Relatorio: {production.last_run?.report_path ?? production.last_run?.summary?.report_path ?? 'pending_instrumentation'}
                </p>
              </div>
            </Card>

            <Card className="p-5">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h3 className="text-sm font-black text-[var(--dash-text)]">Separacao operacional</h3>
                  <p className="mt-1 text-xs text-[var(--dash-muted)]">Rede madura nao significa pacote publicado; a producao gera o pacote testavel.</p>
                </div>
                <Badge tone={publicationReadiness.includes('aguardando') ? 'amber' : releaseTone}>handoff</Badge>
              </div>
              <div className="mt-4 grid grid-cols-2 gap-3">
                <MetricTile title="Pronto para producao" value={productionReadiness} tone={releaseTone} />
                <MetricTile title="Pronto para publicar" value={publicationReadiness} tone={publicationReadiness.includes('aguardando') ? 'amber' : releaseTone} />
              </div>
            </Card>
          </div>
        </div>
      )}

      {false && view === 'release' && (
        <div className="grid min-h-0 flex-1 gap-4 xl:grid-cols-[0.9fr_1.1fr]">
          <Card className="p-5">
            <h3 className="text-sm font-black text-[var(--dash-text)]">Checklist de release</h3>
            <div className="mt-4 space-y-3">
              {releaseChecklist.map((item) => (
                <div key={item.label} className="flex items-center justify-between gap-3 rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-3">
                  <div>
                    <p className="text-sm font-black text-[var(--dash-text)]">{item.label}</p>
                    <p className="text-xs text-[var(--dash-muted)]">{item.detail}</p>
                  </div>
                  <Badge tone={item.ok ? 'emerald' : 'amber'}>{item.ok ? 'ok' : 'atenção'}</Badge>
                </div>
              ))}
            </div>
          </Card>
          <Card className="p-5">
            <h3 className="text-sm font-black text-[var(--dash-text)]">Ultima execucao</h3>
            <div className="mt-4 grid grid-cols-2 gap-3">
              <MetricTile title="Run" value={production.last_run?.run_id ?? '-'} tone="blue" />
              <MetricTile title="Status" value={statusLabel(production.last_run?.status ?? 'idle')} tone={statusTone(production.last_run?.status)} />
              <MetricTile title="Etapa" value={production.current_stage ?? '-'} tone="slate" />
              <MetricTile title="Progresso" value={`${production.progress_pct ?? 0}%`} tone="emerald" />
            </div>
            <p className="mt-4 rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-4 text-sm text-[var(--dash-muted)]">
              {production.last_run?.message ?? 'Sem run ativa. O cache preserva a ultima leitura ate novo ciclo ou refresh.'}
            </p>
          </Card>
        </div>
      )}

      {view === 'quality' && (
        <div className="grid min-h-0 flex-1 gap-4 xl:grid-cols-[0.85fr_1.15fr]">
          <div className="grid min-h-0 gap-3">
            <Card className="p-5">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h3 className="text-sm font-black text-[var(--dash-text)]">Status do modelo</h3>
                  <p className="mt-1 text-xs text-[var(--dash-muted)]">Qualidade media por classe com trava de falso seguro.</p>
                </div>
                <Badge tone={qualityTone}>{qualityStatus}</Badge>
              </div>
              <div className="mt-4 rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-3">
                <p className="text-[10px] font-black uppercase tracking-wide text-[var(--dash-muted)]">Motivo</p>
                <p className="mt-1 text-sm font-black text-[var(--dash-text)]">{qualityReason}</p>
              </div>
              <div className="mt-3 rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-3">
                <p className="text-[10px] font-black uppercase tracking-wide text-[var(--dash-muted)]">Proxima acao de qualidade</p>
                <p className="mt-1 text-base font-black text-[var(--dash-text)]">{qualityNextAction}</p>
              </div>
            </Card>

            <div className="grid gap-3 md:grid-cols-2">
              <Card className="p-4">
                <div className="flex items-center justify-between gap-3">
                  <h3 className="text-sm font-black text-[var(--dash-text)]">Qualidade</h3>
                  <Badge tone={deltaF1 == null ? 'slate' : deltaF1 >= 0 ? 'emerald' : 'amber'}>{deltaF1 == null ? 'sem delta' : deltaF1 >= 0 ? 'melhora' : 'queda'}</Badge>
                </div>
                <div className="mt-3 grid gap-2">
                  <MetricTile title="Macro F1" value={pctMetric(currentF1)} tone={deltaF1 != null && deltaF1 < 0 ? 'amber' : 'emerald'} />
                  <MetricTile title="Delta F1" value={qualityRiskCards[0].value} tone={qualityRiskCards[0].tone} />
                  <MetricTile title="Safe precision" value={pctMetric(currentSafePrecision)} tone="blue" />
                </div>
              </Card>
              <Card className="p-4">
                <div className="flex items-center justify-between gap-3">
                  <h3 className="text-sm font-black text-[var(--dash-text)]">Seguranca</h3>
                  <Badge tone={currentFalseSafe ? 'red' : 'emerald'}>{currentFalseSafe ? 'risco' : 'sem falso seguro'}</Badge>
                </div>
                <div className="mt-3 grid gap-2">
                  <MetricTile title="Falso seguro atual" value={fmt(currentFalseSafe)} tone={currentFalseSafe ? 'red' : 'emerald'} />
                  <MetricTile title="Falso seguro anterior" value={fmt(previousFalseSafe)} tone={previousFalseSafe ? 'red' : 'emerald'} />
                  <MetricTile title="Holdout/Safe recall" value={pctMetric(currentSafeRecall)} tone="blue" />
                </div>
              </Card>
            </div>

            <Card className="p-4">
              <div className="flex items-center justify-between gap-3">
                <h3 className="text-sm font-black text-[var(--dash-text)]">Ganho da rede</h3>
                <Badge tone={networkGain ? 'violet' : 'slate'}>{networkGain ? `${compact(networkGain)} segmentos` : 'pending_instrumentation'}</Badge>
              </div>
              <p className="mt-1 text-xs text-[var(--dash-muted)]">Quando nao ha decomposicao, o ganho fica como consolidado do pacote de maturacao.</p>
              <div className="mt-3 grid grid-cols-2 gap-2">
                {gainBreakdown.map((item) => (
                  <MetricTile key={item.label} title={item.label} value={item.value} tone={item.tone} />
                ))}
              </div>
            </Card>
          </div>

          <div className="grid min-h-0 gap-3">
            <Card className="flex min-h-0 flex-col p-5">
              <div className="flex shrink-0 items-start justify-between gap-3">
                <div>
                  <h3 className="text-sm font-black text-[var(--dash-text)]">Comparacao atual vs anterior</h3>
                  <p className="mt-1 text-xs text-[var(--dash-muted)]">F1 mede qualidade media; falso seguro mede risco operacional.</p>
                </div>
                <Badge tone={qualityTone}>{deltaF1 == null ? 'pending_instrumentation' : `${deltaF1 >= 0 ? '+' : ''}${(deltaF1 * 100).toLocaleString('pt-BR', { maximumFractionDigits: 2 })} p.p.`}</Badge>
              </div>
              <div className="mt-4 grid shrink-0 grid-cols-2 gap-2 lg:grid-cols-4">
                <MetricTile title="Modelo atual" value={latestModel.modelVersion ?? mlPerformance.kpis?.activeModelShort ?? '-'} tone="blue" />
                <MetricTile title="Modelo anterior" value={previousModel.modelVersion ?? '-'} tone="slate" />
                <MetricTile title="F1 atual" value={pctMetric(currentF1)} tone={deltaF1 != null && deltaF1 < 0 ? 'amber' : 'emerald'} />
                <MetricTile title="F1 anterior" value={previousF1 == null ? '-' : pctMetric(previousF1)} tone="slate" />
              </div>
              <div className="mt-4 min-h-0 flex-1">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={f1ComparisonData} margin={{ top: 12, right: 20, left: 0, bottom: 8 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="currentColor" opacity={0.08} />
                    <XAxis dataKey="label" tick={chartText} />
                    <YAxis tick={chartText} domain={[0, 100]} tickFormatter={(value) => `${value}%`} />
                    <Tooltip formatter={(value) => [`${Number(value).toLocaleString('pt-BR', { maximumFractionDigits: 2 })}%`, 'Macro F1']} />
                    <Bar dataKey="f1" radius={[6, 6, 0, 0]}>
                      {f1ComparisonData.map((row) => (
                        <Cell key={row.label} fill={row.label === 'Atual' ? (deltaF1 != null && deltaF1 < 0 ? '#f59e0b' : '#10b981') : '#64748b'} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </Card>

            <div className="grid gap-3 md:grid-cols-2">
              <Card className="p-4">
                <h3 className="text-sm font-black text-[var(--dash-text)]">Riscos compactos</h3>
                <div className="mt-3 grid grid-cols-2 gap-2">
                  {qualityRiskCards.map((item) => (
                    <MetricTile key={item.label} title={item.label} value={item.value} tone={item.tone} />
                  ))}
                </div>
              </Card>
              <Card className="p-4">
                <h3 className="text-sm font-black text-[var(--dash-text)]">Freshness</h3>
                <div className="mt-3 grid gap-2">
                  <MetricTile title="Avaliacao" value={latestModel.runId ? `modelo #${latestModel.runId}` : 'pending_instrumentation'} tone={latestModel.runId ? 'blue' : 'slate'} />
                  <MetricTile title="Timestamp" value={shortDateTime(latestModel.startedAt)} tone={latestModel.startedAt ? 'slate' : 'slate'} />
                  <MetricTile title="Holdout" value="pending_instrumentation" tone="slate" />
                </div>
              </Card>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function DashboardTabs({ value, onChange }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[var(--dash-border)] bg-[var(--dash-card)] p-3">
      <div>
        <h2 className="text-lg font-black text-[var(--dash-text)]">Inteligência do Projeto</h2>
        <p className="text-xs text-[var(--dash-muted)]">Uma visão única para progresso, aprendizado, qualidade, liberação e rede.</p>
      </div>
      <nav className="inline-flex flex-wrap items-center gap-1 rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-1">
        {dashboardViewItems.map((item) => (
          <button
            key={item.id}
            onClick={() => onChange(item.id)}
            className={cn(
              'h-8 rounded-lg px-3 text-xs font-black transition',
              value === item.id ? 'bg-blue-600 text-white shadow-lg shadow-blue-600/20' : 'text-[var(--dash-muted)] hover:bg-blue-500/10 hover:text-[var(--dash-text)]'
            )}
          >
            {item.label}
          </button>
        ))}
      </nav>
    </div>
  );
}

function Managerial({ data }) {
  const [viewMode, setViewMode] = useState('Overview');
  const production = data.production ?? {};
  const summary = production.summary ?? {};
  const readiness = production.readiness ?? {};
  const gate = production.gate ?? {};
  const learning = data.learning ?? production.learning ?? {};
  const lifecycle = data.lifecycle ?? {};
  const packages = lifecycle.packageBacklog ?? [];
  const tokenPolicyBuckets = lifecycle.tokenPolicy?.bucketDistribution ?? [];
  const outputEvolution = lifecycle.outputApply?.evolution ?? [];
  const agents = data.agents ?? {};
  const agentSummary = agents.summary ?? {};
  const agentNodes = agents.topologyNodes ?? [];
  const recommendations = agents.recommendations ?? [];
  const mlPerformance = data.mlPerformance ?? {};
  const mlKpis = mlPerformance.kpis ?? {};
  const modelTrend = mlPerformance.mlTrendByModel ?? mlPerformance.mlTrend ?? [];
  const datasetComposition = mlPerformance.datasetComposition ?? [];
  const productionPhases = buildProductionPhases(production.stages ?? []);
  const taxonomy = lifecycle.taxonomy ?? {};
  const selectCString = summary.select_cstring ?? taxonomy.select_cstring ?? {};

  const readinessLabel = {
    ready_for_game_test: 'Ready for game test',
    ready_with_known_issues: 'Ready with known issues',
    learning_locked: 'Learning active',
    blocked: 'Blocked',
  }[readiness.status] ?? readiness.status ?? 'Sem leitura';
  const readinessTone = readiness.status === 'ready_for_game_test'
    ? 'emerald'
    : readiness.status === 'ready_with_known_issues'
      ? 'amber'
      : readiness.status === 'learning_locked' || readiness.status === 'blocked'
        ? 'red'
        : 'slate';
  const distribution = (summary.taxonomy_distribution ?? taxonomy.distribution ?? [
    { name: 'Consolidados', value: Number(summary.closed_segments ?? 0), color: '#10b981' },
    { name: 'Pendencia acionavel', value: Number(summary.actionable_pending ?? 0), color: '#f59e0b' },
    { name: 'Suspeita ML / Watch', value: Number(summary.model_suspicion_watch ?? 0), color: '#8b5cf6' },
    { name: 'Ponte governada pendente', value: Number(summary.governed_bridge_pending ?? 0), color: '#38bdf8' },
  ]).filter((item) => Number(item.value) > 0);
  const evolution = outputEvolution.slice(-12).map((row) => ({
    run: `#${row.state_run_id}`,
    cobertura: Number(row.closed_pct ?? 0),
    pendencias: Number(row.pending_count ?? 0),
    apply: Number(row.output_apply_pending_count ?? 0),
  }));
  const bottleneckMatchers = [
    ['gender', 'Genero'],
    ['mixed', 'Mixed token'],
    ['dynamic', 'Dynamic scope'],
    ['select_cstring', 'Select_CString'],
    ['token_added', 'Token added'],
    ['tutorial', 'Tutorial'],
  ];
  const bottlenecks = bottleneckMatchers.map(([needle, label]) => ({
    label,
    value: tokenPolicyBuckets
      .filter((row) => String(row.policy_bucket ?? '').toLowerCase().includes(needle))
      .reduce((acc, row) => acc + Number(row.total ?? row.count ?? 0), 0),
  })).filter((row) => row.value > 0);
  const fallbackBottlenecks = packages.slice(0, 6).map((row) => ({
    label: row.package_name ?? row.package ?? row.relative_path ?? 'pacote',
    value: Number(row.pending_count ?? row.pending ?? 0),
  }));
  const bottleneckData = bottlenecks.length ? bottlenecks : fallbackBottlenecks;
  const layerRows = [
    ['Deterministic Guards', 'Bloqueiam antes do ML quando tokens, estrutura ou locked human exigem cautela.', ShieldCheck, 'authoritative'],
    ['Trusted Memory', 'Confirmacoes humanas e output testado viram conhecimento confiavel.', Database, 'knowledge'],
    ['General Macro Model', 'Observa o pacote inteiro e calcula risco amplo.', BrainCircuit, 'baseline'],
    ['Coordinator Ensemble', 'Roteia para especialistas e escolhe a decisao mais cautelosa.', Route, 'router'],
    ['Issue Ledger', 'Registra sinais estruturais e linguísticos que guiam bloqueios, filas e microagentes.', FileWarning, 'guardrail'],
    ['Operational Specialists', 'Atuam por familia quando ja foram auditados.', Layers3, 'operational'],
    ['Symbolic Guarded Policies', 'Reduzem risco com regras pequenas e explicaveis, sem aplicar sozinhas.', GitBranch, 'guarded'],
    ['Experimental / Planned Neurons', 'Aprendem em laboratorio sem autoridade direta de release.', PackageSearch, 'lab'],
    ['Human / Game Feedback', 'Fecha o ciclo com revisao humana e teste real no CK3.', Activity, 'feedback'],
  ];
  const toneForState = (value) => {
    if (['done', 'active', 'operational', 'authoritative', 'released'].includes(value)) return 'emerald';
    if (['running', 'dry_run', 'experimental', 'pending'].includes(value)) return 'amber';
    if (['failed', 'blocked'].includes(value)) return 'red';
    return 'blue';
  };
  const pendingWork = [
    { label: 'actionable_pending', value: Number(summary.actionable_pending ?? 0), color: '#f59e0b' },
    { label: 'governed_bridge_pending', value: Number(summary.governed_bridge_pending ?? 0), color: '#38bdf8' },
    { label: 'model_watch', value: Number(summary.model_suspicion_watch ?? 0), color: '#8b5cf6' },
    { label: 'audit_required', value: Number(gate.invalid_releases ?? 0), color: '#ef4444' },
    { label: 'cluster_required', value: Number(agentSummary.recommendation_evidence ?? 0), color: '#8b5cf6' },
    { label: 'guard_block', value: Number(summary.blocked_critical ?? 0), color: '#64748b' },
  ].filter((item) => item.value > 0);
  const nextBestActionText = summary.needs_apply
    ? 'Rodar producao para aplicar confirmacoes pendentes antes de medir novo ganho.'
    : Number(summary.governed_bridge_pending ?? 0) > 0
      ? 'Investigar a ponte governada pendente; nao aplicar os textos sem diagnostico.'
      : Number(summary.model_suspicion_watch ?? 0) > 0
        ? 'Amostrar e calibrar suspeitas ML; isto nao e backlog manual bruto.'
        : learning.can_start_production
          ? 'Producao liberada; gerar/testar versao do mod e usar feedback para aprendizado.'
          : 'Aprendizado em andamento; aguarde liberacao do gate antes da producao.';
  const latestModel = modelTrend.at(-1) ?? {};
  const previousModel = modelTrend.at(-2) ?? {};
  const learningEvidence = datasetComposition.length
    ? datasetComposition
    : [
      { label: 'positive', value: Number(mlKpis.positiveCount ?? 0) },
      { label: 'negative/boundary', value: Number(mlKpis.negativeCount ?? 0) },
      { label: 'recommendations', value: Number(agentSummary.recommendation_evidence ?? 0) },
    ].filter((item) => item.value > 0);

  return (
    <div className="flex flex-col gap-4 pb-3">
      <ViewHeader
        title="Managerial Control"
        subtitle="Leitura rapida de release, arquitetura e neuroniozinhos operacionais."
      >
        <ViewToggle options={['Overview', 'Production Flow', 'Neural Network', 'Learning Impact']} value={viewMode} onChange={setViewMode} />
      </ViewHeader>

      {viewMode === 'Overview' ? (
        <>
          <Card className="p-5">
            <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
              <div>
                <p className="text-xs font-bold uppercase tracking-wide text-blue-300">Pergunta executiva</p>
                <h2 className="mt-1 text-2xl font-black text-[var(--dash-text)]">O projeto esta pronto para gerar/testar uma versao do mod?</h2>
                <p className="mt-2 text-sm text-[var(--dash-muted)]">Resposta atual: <span className="font-bold text-[var(--dash-text)]">{readinessLabel}</span></p>
              </div>
              <Badge tone={readinessTone}>{readiness.recommended_action ?? 'sem acao'}</Badge>
            </div>
          </Card>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
            <StatCard title="Release Readiness" value={readinessLabel} detail={learning.can_start_production ? 'production_safe=true' : 'learning gate ativo'} icon={Rocket} color={readinessTone} />
            <StatCard title="Closed" value={pct(summary.closed_pct)} detail={`${fmt(summary.closed_segments)} segmentos`} icon={CheckCircle2} color="emerald" />
            <StatCard title="Pendencia Acionavel" value={compact(summary.actionable_pending)} detail={`bruto ${compact(summary.raw_pending)} inclui watch`} icon={AlertCircle} color={summary.actionable_pending ? 'amber' : 'emerald'} />
            <StatCard title="Suspeita ML / Watch" value={compact(summary.model_suspicion_watch)} detail="amostrar/calibrar, nao fila manual" icon={BrainCircuit} color="violet" />
            <StatCard title="Ponte Select_CString" value={`${fmt(selectCString.closed ?? 0)}/${fmt(selectCString.total ?? 0)}`} detail={`${fmt(selectCString.pending ?? summary.governed_bridge_pending ?? 0)} pendentes`} icon={SearchCheck} color={(selectCString.pending ?? summary.governed_bridge_pending) ? 'amber' : 'emerald'} />
            <StatCard title="Needs Apply" value={compact(summary.needs_apply)} detail="zero e estado bom" icon={TerminalSquare} color={summary.needs_apply ? 'amber' : 'emerald'} />
            <StatCard title="Operational Safety" value={fmt(agentSummary.operational_false_safe ?? 0)} detail={`invalid ${fmt(gate.invalid_releases)} · auto apply ${gate.auto_apply_allowed ? 'on' : 'off seguro'}`} icon={ShieldCheck} color={(agentSummary.operational_false_safe ?? 0) || gate.invalid_releases ? 'red' : 'emerald'} />
            <StatCard title="Learning State" value={learning.status ?? 'sem status'} detail={learning.can_start_production ? 'producao liberada' : 'aguardando liberacao'} icon={Activity} color={learning.can_start_production ? 'emerald' : 'amber'} />
          </div>

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1.2fr_0.8fr]">
            <Card className="p-5">
              <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
                <div>
                  <h3 className="text-sm font-black text-[var(--dash-text)]">Proxima decisao recomendada</h3>
                  <p className="mt-1 text-sm text-[var(--dash-muted)]">
                    {nextBestActionText}
                  </p>
                  <p className="mt-2 text-xs font-semibold text-violet-300">
                    Ultimo ganho em shadow/checkpoint: {fmt(gate.guarded_releases ?? agentSummary.active_gate_guarded_releases)} releases guardados.
                  </p>
                </div>
                <Badge tone={summary.needs_apply || summary.governed_bridge_pending ? 'amber' : learning.can_start_production ? 'emerald' : 'red'}>
                  {summary.needs_apply ? 'needs apply' : summary.governed_bridge_pending ? 'governed bridge review' : learning.can_start_production ? 'production ready' : 'learning active'}
                </Badge>
              </div>
            </Card>
            <Card className="p-5">
              <h3 className="text-sm font-black text-[var(--dash-text)]">Pending Work</h3>
              <div className="mt-4 space-y-3">
                {pendingWork.map((item) => (
                  <div key={item.label}>
                    <div className="mb-1 flex items-center justify-between text-xs">
                      <span className="font-bold text-[var(--dash-text)]">{item.label}</span>
                      <span className="text-[var(--dash-muted)]">{fmt(item.value)}</span>
                    </div>
                    <div className="h-2 overflow-hidden rounded-full bg-slate-500/20">
                      <div className="h-full rounded-full" style={{ width: `${Math.min(100, (item.value / Math.max(1, Number(summary.raw_pending ?? summary.pending_operational ?? item.value))) * 100)}%`, backgroundColor: item.color }} />
                    </div>
                  </div>
                ))}
                {!pendingWork.length && <p className="text-sm text-[var(--dash-muted)]">Sem pendencias relevantes no snapshot atual.</p>}
              </div>
            </Card>
          </div>

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
            <ChartCard title="Distribuicao de Release" subtitle="Consolidados, acionaveis, watch do modelo e ponte governada.">
              <ChartBox>
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart>
                    <Pie data={distribution} dataKey="value" nameKey="name" innerRadius={62} outerRadius={104} paddingAngle={3}>
                      {distribution.map((entry) => <Cell key={entry.name} fill={entry.color} />)}
                    </Pie>
                    <Tooltip formatter={(value) => fmt(value)} />
                    <Legend />
                  </PieChart>
                </ResponsiveContainer>
              </ChartBox>
            </ChartCard>

            <ChartCard title="Cobertura Recente" subtitle="Fechamento e fila de apply por snapshot." className="xl:col-span-2">
              <ChartBox>
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart data={evolution} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="currentColor" opacity={0.12} />
                    <XAxis dataKey="run" axisLine={false} tickLine={false} tick={chartText} />
                    <YAxis yAxisId="left" domain={[0, 100]} tickFormatter={(v) => `${v}%`} axisLine={false} tickLine={false} tick={chartText} />
                    <YAxis yAxisId="right" orientation="right" tickFormatter={(v) => compact(v)} axisLine={false} tickLine={false} tick={chartText} />
                    <Tooltip formatter={(value, name) => name === 'Cobertura' ? pct(value) : fmt(value)} />
                    <Legend />
                    <Bar yAxisId="right" dataKey="pendencias" name="Pendencias" fill="#f59e0b" radius={[8, 8, 0, 0]} opacity={0.65} />
                    <Bar yAxisId="right" dataKey="apply" name="Needs apply" fill="#3b82f6" radius={[8, 8, 0, 0]} opacity={0.55} />
                    <Line yAxisId="left" type="monotone" dataKey="cobertura" name="Cobertura" stroke="#10b981" strokeWidth={3} dot={{ r: 4, strokeWidth: 2 }} />
                  </ComposedChart>
                </ResponsiveContainer>
              </ChartBox>
            </ChartCard>

            <ChartCard title="Gargalos" subtitle="Filas de token ou pacotes com maior pendencia." className="xl:col-span-3">
              <ChartBox className="h-[300px]">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={bottleneckData} layout="vertical" margin={{ top: 8, right: 18, left: 110, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke="currentColor" opacity={0.12} />
                    <XAxis type="number" tickFormatter={(v) => compact(v)} axisLine={false} tickLine={false} tick={chartText} />
                    <YAxis type="category" dataKey="label" axisLine={false} tickLine={false} tick={chartText} width={110} />
                    <Tooltip formatter={(value) => fmt(value)} />
                    <Bar dataKey="value" name="Pendencias" fill="#f59e0b" radius={[0, 8, 8, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </ChartBox>
            </ChartCard>
          </div>
        </>
      ) : viewMode === 'Production Flow' ? (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          {productionPhases.map((phase, index) => (
            <Card key={phase.id} className="p-5">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <p className="text-xs font-bold uppercase tracking-wide text-blue-300">Fase {index + 1}/4</p>
                  <h3 className="mt-1 text-lg font-black text-[var(--dash-text)]">{phase.title}</h3>
                  <p className="mt-1 text-sm text-[var(--dash-muted)]">{phase.purpose}</p>
                </div>
                <Badge tone={toneForState(phase.status)}>{statusLabel(phase.status)}</Badge>
              </div>
              <div className="mt-4 h-2 overflow-hidden rounded-full bg-slate-500/20">
                <div className={cn('h-full rounded-full', phase.status === 'failed' ? 'bg-red-500' : phase.status === 'running' ? 'bg-blue-500' : 'bg-emerald-500')} style={{ width: `${phase.progress}%` }} />
              </div>
              <div className="mt-3 flex flex-wrap gap-2 text-xs">
                <Badge tone="blue">{phase.done}/{phase.total} subetapas</Badge>
                <Badge tone={neuralProductionStages[phase.currentStage?.id] ? 'violet' : 'slate'}>{phase.currentStage?.label ?? 'aguardando'}</Badge>
              </div>
              <div className="mt-4 grid gap-2 md:grid-cols-2">
                {phase.stages.map((stage) => (
                  <div key={stage.id} className="rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-3">
                    <div className="flex items-center justify-between gap-2">
                      <p className="truncate text-xs font-bold text-[var(--dash-text)]">{stage.label}</p>
                      <Badge tone={toneForState(stage.status)}>{statusLabel(stage.status)}</Badge>
                    </div>
                    <p className="mt-1 truncate text-[11px] text-[var(--dash-muted)]">{productionStageDetails[stage.id] ?? stage.id}</p>
                    {neuralProductionStages[stage.id] && (
                      <div className="mt-2 inline-flex items-center gap-1 rounded-full bg-violet-500/10 px-2 py-0.5 text-[10px] font-bold text-violet-300">
                        <BrainCircuit size={12} /> ML/policy
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </Card>
          ))}
        </div>
      ) : viewMode === 'Neural Network' ? (
        <div className="flex flex-col gap-4">
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-7">
            <StatCard title="Agentes" value={fmt(agentSummary.agents_total)} detail={`${fmt(agentSummary.agents_operational)} operacionais`} icon={Database} color="blue" />
            <StatCard title="Experimentais" value={fmt(agentSummary.experimental_subagents)} detail={`${fmt(agentSummary.planned_subagents)} planejados`} icon={PackageSearch} color="amber" />
            <StatCard title="Shadow/Candidate" value={agentSummary.shadow_agents ?? agentSummary.candidate_agents ?? 'sem dado'} detail="observado, sem autoridade direta" icon={GitBranch} color="violet" />
            <StatCard title="Guarded Releases" value={fmt(agentSummary.active_gate_guarded_releases)} detail={`overlay ${agentSummary.active_gate_overlay_run_id ?? '-'}`} icon={ShieldCheck} color="emerald" />
            <StatCard title="Invalid Releases" value={fmt(agentSummary.active_gate_invalid_releases)} detail="gate ativo" icon={ShieldAlert} color={agentSummary.active_gate_invalid_releases ? 'red' : 'emerald'} />
            <StatCard title="False Safe Op." value={fmt(agentSummary.operational_false_safe ?? 0)} detail={(agentSummary.operational_false_safe ?? 0) === 0 ? 'zero operacional' : 'revisar'} icon={ShieldCheck} color={(agentSummary.operational_false_safe ?? 0) === 0 ? 'emerald' : 'red'} />
            <StatCard title="Evidencia" value={fmt(agentSummary.recommendation_evidence)} detail="novos neuroniozinhos" icon={SearchCheck} color="violet" />
          </div>

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-4">
            {layerRows.map(([title, detail, Icon, role]) => (
              <div key={title} className="rounded-2xl border border-[var(--dash-border)] bg-[var(--dash-card)] p-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="grid h-10 w-10 place-items-center rounded-xl bg-blue-500/10 text-blue-300"><Icon size={18} /></div>
                  <Badge tone={role === 'authoritative' || role === 'operational' ? 'emerald' : role === 'lab' || role === 'feedback' ? 'amber' : 'blue'}>{role}</Badge>
                </div>
                <h3 className="mt-4 text-sm font-black text-[var(--dash-text)]">{title}</h3>
                <p className="mt-2 text-xs leading-relaxed text-[var(--dash-muted)]">{detail}</p>
              </div>
            ))}
          </div>

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <ChartCard title="Agentes Operacionais" subtitle="Leitura compacta do registro atual.">
              <div className="max-h-[420px] overflow-auto">
                <table className="w-full text-left text-sm">
                  <thead className="text-xs uppercase text-[var(--dash-muted)]">
                    <tr><th className="py-2">Agente</th><th className="py-2">Tipo</th><th className="py-2">Estado</th><th className="py-2 text-right">False</th></tr>
                  </thead>
                  <tbody>
                    {agentNodes.slice(0, 18).map((node) => (
                      <tr key={node.id} className="border-t border-[var(--dash-border)]">
                        <td className="max-w-[260px] truncate py-2 font-semibold text-[var(--dash-text)]" title={node.id}>{node.id}</td>
                        <td className="py-2">{node.agent_type}</td>
                        <td className="py-2"><Badge tone={toneForState(node.operational_state)}>{node.operational_state}</Badge></td>
                        <td className="py-2 text-right text-red-300">{fmt(node.false_safe_count)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </ChartCard>

            <ChartCard title="Recomendacoes" subtitle="Evidencias para proximos especialistas ou reforcos.">
              <div className="max-h-[420px] space-y-3 overflow-auto">
                {recommendations.slice(0, 8).map((row) => (
                  <div key={row.proposed_agent_key} className="rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-3">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <h4 className="truncate text-sm font-bold text-[var(--dash-text)]" title={row.proposed_agent_key}>{row.proposed_agent_key}</h4>
                        <p className="mt-1 line-clamp-2 text-xs text-[var(--dash-muted)]">{row.reason ?? 'sem motivo registrado'}</p>
                      </div>
                      <Badge tone={row.negative_count ? 'red' : 'emerald'}>{fmt(row.evidence_count)} evid.</Badge>
                    </div>
                  </div>
                ))}
                {!recommendations.length && <p className="text-sm text-[var(--dash-muted)]">Sem recomendacoes novas no snapshot atual.</p>}
              </div>
            </ChartCard>
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-4">
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5">
            <StatCard title="Latest Dataset Run" value={mlKpis.latestDatasetRunId ?? '-'} detail="dataset supervisionado" icon={Database} color="blue" />
            <StatCard title="Latest Model Run" value={mlKpis.latestModelRunId ?? mlKpis.activeModelShort ?? '-'} detail={mlKpis.activeModel ?? 'modelo atual'} icon={BrainCircuit} color="violet" />
            <StatCard title="Macro F1 Delta" value={metric(Number(latestModel.macroF1 ?? mlKpis.macroF1 ?? 0) - Number(previousModel.macroF1 ?? latestModel.macroF1 ?? mlKpis.macroF1 ?? 0))} detail="ultimo modelo vs anterior" icon={BarChart3} color="blue" />
            <StatCard title="Safe Recall Delta" value={metric(Number(latestModel.safeRecall ?? mlKpis.holdoutCoverage ?? 0) - Number(previousModel.safeRecall ?? latestModel.safeRecall ?? mlKpis.holdoutCoverage ?? 0))} detail="cobertura conservadora" icon={ShieldCheck} color="emerald" />
            <StatCard title="False Safe Delta" value={fmt(Number(latestModel.falseSafe ?? 0) - Number(previousModel.falseSafe ?? latestModel.falseSafe ?? 0))} detail="zero continua sendo bom" icon={ShieldAlert} color={(latestModel.falseSafe ?? 0) ? 'red' : 'emerald'} />
            <StatCard title="Shadow Release Gain" value={fmt(gate.guarded_releases ?? agentSummary.active_gate_guarded_releases)} detail={`overlay ${gate.active_overlay_run_id ?? agentSummary.active_gate_overlay_run_id ?? '-'}`} icon={ArrowUpRight} color="violet" />
            <StatCard title="Checkpoint Blocked" value={fmt(gate.invalid_releases ?? agentSummary.active_gate_invalid_releases)} detail="trava de seguranca" icon={AlertTriangle} color={(gate.invalid_releases ?? agentSummary.active_gate_invalid_releases) ? 'amber' : 'emerald'} />
            <StatCard title="New Microagents" value={fmt(recommendations.length || agentSummary.recommendation_evidence)} detail="sugestoes com evidencia" icon={SearchCheck} color="amber" />
            <StatCard title="Operational False Safe" value={fmt(agentSummary.operational_false_safe ?? 0)} detail="deve ficar zerado" icon={ShieldCheck} color={(agentSummary.operational_false_safe ?? 0) ? 'red' : 'emerald'} />
            <StatCard title="Learning Gate" value={learning.can_start_production ? 'released' : 'locked'} detail={learning.current_phase_label || learning.status || 'sem ciclo ativo'} icon={Activity} color={learning.can_start_production ? 'emerald' : 'amber'} />
          </div>

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <ChartCard title="Qualidade por Modelo" subtitle="Macro F1, safe recall e predicted safe/cobertura.">
              <ChartBox className="h-[360px]">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={modelTrend} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="currentColor" opacity={0.12} />
                    <XAxis dataKey="model" axisLine={false} tickLine={false} tick={chartText} />
                    <YAxis domain={[0, 1]} tickFormatter={(v) => `${Math.round(v * 100)}%`} axisLine={false} tickLine={false} tick={chartText} />
                    <Tooltip content={<ModelTooltip />} />
                    <Legend />
                    <Line type="monotone" dataKey="macroF1" name="Macro F1" stroke="#2563eb" strokeWidth={3} />
                    <Line type="monotone" dataKey="safeRecall" name="Safe Recall" stroke="#10b981" strokeWidth={3} />
                    <Line type="monotone" dataKey="safePrecision" name="Safe Precision" stroke="#f59e0b" strokeWidth={3} />
                  </LineChart>
                </ResponsiveContainer>
              </ChartBox>
            </ChartCard>

            <ChartCard title="Risco por Modelo" subtitle="False-safe menor vale mais que confianca agressiva.">
              <ChartBox className="h-[360px]">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={modelTrend} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="currentColor" opacity={0.12} />
                    <XAxis dataKey="model" axisLine={false} tickLine={false} tick={chartText} />
                    <YAxis axisLine={false} tickLine={false} tick={chartText} />
                    <Tooltip content={<ModelTooltip />} />
                    <Bar dataKey="falseSafe" name="False Safe" fill="#ef4444" radius={[8, 8, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </ChartBox>
            </ChartCard>

            <ChartCard title="Evidencia por Ciclo" subtitle="Dataset e revisoes que alimentam o aprendizado." className="xl:col-span-1">
              <ChartBox className="h-[320px]">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={learningEvidence} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="currentColor" opacity={0.12} />
                    <XAxis dataKey="label" axisLine={false} tickLine={false} tick={chartText} />
                    <YAxis axisLine={false} tickLine={false} tick={chartText} />
                    <Tooltip formatter={(value) => fmt(value)} />
                    <Bar dataKey="value" name="Evidencias" fill="#8b5cf6" radius={[8, 8, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </ChartBox>
            </ChartCard>

            <ChartCard title="Matriz de Gargalo" subtitle="Familias pendentes e possivel responsavel neural.">
              <div className="max-h-[320px] overflow-auto">
                <table className="w-full text-left text-sm">
                  <thead className="text-xs uppercase text-[var(--dash-muted)]">
                    <tr><th className="py-2">Familia</th><th className="py-2">Responsavel provavel</th><th className="py-2 text-right">Pendencia</th></tr>
                  </thead>
                  <tbody>
                    {bottleneckData.slice(0, 10).map((row) => (
                      <tr key={row.label} className="border-t border-[var(--dash-border)]">
                        <td className="py-2 font-semibold text-[var(--dash-text)]">{row.label}</td>
                        <td className="py-2 text-[var(--dash-muted)]">{row.label.toLowerCase().includes('token') ? 'token policy / guarded subpolicy' : row.label.toLowerCase().includes('genero') ? 'gender specialist' : 'coordinator ensemble'}</td>
                        <td className="py-2 text-right text-amber-300">{fmt(row.value)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </ChartCard>
          </div>
        </div>
      )}
    </div>
  );
}

function ProductionArchitecture({ data }) {
  const stages = data.production?.stages ?? [];
  return (
    <div className="flex flex-col gap-4 pb-3">
      <Card className="p-5">
        <h2 className="text-xl font-black text-[var(--dash-text)]">Arquitetura do Fluxo de Producao</h2>
        <p className="mt-2 text-sm text-[var(--dash-muted)]">O sistema separa release operacional de aprendizado. Producao usa conhecimento promovido; aprendizado evolui a rede.</p>
      </Card>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-5">
        {stages.map((stage, index) => (
          <Card key={stage.id} className="p-4">
            <div className="flex items-center justify-between">
              <div className="grid h-9 w-9 place-items-center rounded-xl bg-blue-500/10 text-blue-300">{index + 1}</div>
              <Badge tone={statusTone(stage.status)}>{statusLabel(stage.status)}</Badge>
            </div>
            <h3 className="mt-4 text-sm font-black text-[var(--dash-text)]">{stage.label}</h3>
            <p className="mt-2 text-xs text-[var(--dash-muted)]">{fmt(stage.completed)} concluidos, {fmt(stage.pending)} pendentes</p>
          </Card>
        ))}
      </div>
    </div>
  );
}

const neuralAtlasBlueprint = {
  nodes: [
    {
      id: 'source',
      label: 'CK3 Sources',
      type: 'input',
      family: 'ingest',
      status: 'stable',
      role: 'Spanish, English, old PT-BR and clean output mirror.',
      description: 'Entrada versionada do jogo. A producao compara source, output e memoria antes de qualquer escrita.',
      x: 8,
      y: 64,
      icon: Database,
      tone: 'blue',
      metrics: ['288k segments', 'line mirror', 'token mirror'],
      next: 'Detectar novos, removidos e alterados a cada update.',
    },
    {
      id: 'guards',
      label: 'Deterministic Guards',
      type: 'guard',
      family: 'safety',
      status: 'authoritative',
      role: 'Hard gate for tokens, structure, locked text and unsafe changes.',
      description: 'Camada simbolica: preserva placeholders, comandos CK3, linhas, chaves e excecoes manuais.',
      x: 20,
      y: 42,
      icon: ShieldCheck,
      tone: 'emerald',
      metrics: ['hard blocks', 'token policy', 'manual locks'],
      next: 'Continuar expandindo guard profiles por familia de risco.',
    },
    {
      id: 'memory',
      label: 'Trusted Memory',
      type: 'memory',
      family: 'knowledge',
      status: 'growing',
      role: 'Human decisions, confirmations, lifecycle bridges and tested output.',
      description: 'Nossa base de conhecimento local. Ela guarda aprovacoes, rejeicoes, reparos e estados de fechamento.',
      x: 24,
      y: 82,
      icon: Lock,
      tone: 'amber',
      metrics: ['confirmed text', 'feedback', 'lifecycle state'],
      next: 'Separar conhecimento confiavel de evidencias ainda em observacao.',
    },
    {
      id: 'macro',
      label: 'General Macro Model',
      type: 'model',
      family: 'ml',
      status: 'operational',
      role: 'Broad risk classifier for the whole localization package.',
      description: 'Modelo geral ve o pacote inteiro e sinaliza risco, qualidade e candidatos seguros ou suspeitos.',
      x: 40,
      y: 50,
      icon: BrainCircuit,
      tone: 'violet',
      metrics: ['macro F1', 'risk score', 'coverage'],
      next: 'Ser cada vez melhor em saber quando chamar especialistas.',
    },
    {
      id: 'coordinator',
      label: 'Coordinator Ensemble',
      type: 'coordinator',
      family: 'routing',
      status: 'active',
      role: 'Routes segments, arbitrates specialists and keeps the cautious answer.',
      description: 'O cerebro operacional. Ele decide se segue o macro, chama um especialista ou segura o segmento para revisao.',
      x: 54,
      y: 64,
      icon: Route,
      tone: 'blue',
      metrics: ['routing', 'votes', 'policy overlay'],
      next: 'Evoluir para coordenador que sugere novos neuroniozinhos.',
    },
    {
      id: 'religion',
      label: 'Religion Specialist',
      type: 'specialist',
      family: 'religion',
      status: 'operational',
      role: 'Religious terms, gods, doctrines and contextual preserved names.',
      description: 'Especialista em vocabulario religioso, termos preservados e casos sensiveis de contexto cultural.',
      x: 68,
      y: 38,
      icon: Layers3,
      tone: 'emerald',
      metrics: ['operational', 'subagents in lab', 'low false-safe'],
      next: 'Promover subagentes maduros por subtipo.',
    },
    {
      id: 'titles',
      label: 'Titles Specialist',
      type: 'specialist',
      family: 'titles',
      status: 'learning',
      role: 'Nicknames, ruler labels, gendered adjectives and feudal labels.',
      description: 'Area ambigua: alguns nomes traduzem, outros preservam, e alguns quebram o espelho por fluidez no jogo.',
      x: 69,
      y: 59,
      icon: GitBranch,
      tone: 'amber',
      metrics: ['subagents', 'exceptions', 'gender tokens'],
      next: 'Finalizar title_adjectives, culture_title_labels e ramos de nobreza.',
    },
    {
      id: 'ui',
      label: 'UI & Short Labels',
      type: 'specialist',
      family: 'ui',
      status: 'promising',
      role: 'Short labels, buttons, tooltips and compact localization strings.',
      description: 'Camada nova que fecha rotulos curtos com ponte guardada, sem trocar estado de segmentos ja fechados.',
      x: 67,
      y: 81,
      icon: TerminalSquare,
      tone: 'violet',
      metrics: ['15.730 closed', '354 blocked', '0 needs_apply'],
      next: 'Transformar dry-run promissor em politica recorrente rastreavel.',
    },
    {
      id: 'title_adjectives',
      label: 'Title Adjectives',
      type: 'subagent',
      family: 'titles',
      status: 'operational',
      role: 'Gendered adjective endings and nickname fluency.',
      description: 'Subagente focado em casos como o/a Temido, Impaciente e variacoes com genero.',
      x: 88,
      y: 42,
      icon: BrainCircuit,
      tone: 'emerald',
      metrics: ['gender fluency', 'tokens', 'titles'],
      next: 'Aumentar negativos especificos para reduzir ambiguidade.',
    },
    {
      id: 'culture_labels',
      label: 'Culture Title Labels',
      type: 'subagent',
      family: 'titles',
      status: 'operational',
      role: 'Cultural ruler labels, local titles and preserved flavor.',
      description: 'Subagente para titulos culturais onde traducao literal pode quebrar estetica historica.',
      x: 89,
      y: 58,
      icon: PackageSearch,
      tone: 'emerald',
      metrics: ['cultural labels', 'ruler title', 'context'],
      next: 'Medir ganho por familia antes de promover mais autoridade.',
    },
    {
      id: 'religion_terms',
      label: 'Religion Subagents',
      type: 'subagent',
      family: 'religion',
      status: 'experimental',
      role: 'Bosnian terms, possessive gods, preserved terms and Sufri cases.',
      description: 'Neuroniozinhos de laboratorio: especificos o suficiente para aprender sem generalizar demais.',
      x: 88,
      y: 25,
      icon: SearchCheck,
      tone: 'blue',
      metrics: ['experimental', 'watch', 'pending promotion'],
      next: 'Promover somente quando houver evidencia positiva e falso-seguro zero.',
    },
    {
      id: 'lifecycle',
      label: 'Lifecycle Policies',
      type: 'policy',
      family: 'production',
      status: 'guarded',
      role: 'Turns trusted learning into operational closure.',
      description: 'Ponte entre aprendizado e producao: fecha somente quando hashes, tokens, output e estado atual concordam.',
      x: 52,
      y: 86,
      icon: Workflow,
      tone: 'emerald',
      metrics: ['guarded bridges', 'segment-state', 'needs_apply 0'],
      next: 'Transformar cada ganho em politica auditavel e reversivel.',
    },
    {
      id: 'output',
      label: 'Production Output',
      type: 'output',
      family: 'release',
      status: 'safe',
      role: 'Writes validated mod output only after production gate is open.',
      description: 'Saida final do mod. Toda escrita passa por snapshot, dry-run, validacao e relatorio.',
      x: 90,
      y: 86,
      icon: Rocket,
      tone: 'emerald',
      metrics: ['validated write', 'snapshots', 'reports'],
      next: 'Gerar versao jogavel e coletar feedback real.',
    },
  ],
  edges: [
    ['source', 'guards', 'structure'],
    ['source', 'memory', 'history'],
    ['guards', 'macro', 'safe features'],
    ['memory', 'macro', 'labels'],
    ['guards', 'coordinator', 'hard gate'],
    ['macro', 'coordinator', 'risk score'],
    ['memory', 'coordinator', 'trusted memory'],
    ['coordinator', 'religion', 'route'],
    ['coordinator', 'titles', 'route'],
    ['coordinator', 'ui', 'route'],
    ['religion', 'religion_terms', 'subtasks'],
    ['titles', 'title_adjectives', 'gender'],
    ['titles', 'culture_labels', 'culture'],
    ['coordinator', 'lifecycle', 'approved signal'],
    ['ui', 'lifecycle', 'guarded bridge'],
    ['title_adjectives', 'lifecycle', 'policy vote'],
    ['culture_labels', 'lifecycle', 'policy vote'],
    ['religion_terms', 'lifecycle', 'shadow evidence'],
    ['lifecycle', 'output', 'closure'],
    ['guards', 'output', 'validation'],
  ],
};

const atlasIconByType = {
  guard: ShieldCheck,
  memory: Lock,
  macro_model: BrainCircuit,
  macro_model_candidate: BrainCircuit,
  coordinator: Route,
  registry: Database,
  specialist: Layers3,
  specialist_legacy: GitBranch,
  issue_memory: SearchCheck,
  microagent: BrainCircuit,
  subcoordinator: Route,
  symbolic_subpolicy: Workflow,
  composition_coordinator: Workflow,
  lifecycle_policy: Workflow,
  lifecycle_state: Activity,
  production_gateway: Rocket,
  input: Database,
  model: BrainCircuit,
  policy: Workflow,
  output: Rocket,
};

const atlasToneByStatus = {
  active: 'emerald',
  operational: 'emerald',
  stable: 'blue',
  authoritative: 'emerald',
  growing: 'amber',
  candidate: 'amber',
  shadow: 'blue',
  planned: 'amber',
  experimental: 'blue',
  experimental_watch: 'amber',
  learning: 'amber',
  promising: 'violet',
  guarded: 'emerald',
  safe: 'emerald',
};

const atlasNodePalettes = {
  guard: { accent: '#2dd4bf', darkBg: 'rgba(7, 47, 48, 0.50)', lightBg: 'rgba(218, 249, 241, 0.82)', lightBorder: 'rgba(13, 148, 136, 0.42)' },
  memory: { accent: '#14b8a6', darkBg: 'rgba(8, 51, 68, 0.42)', lightBg: 'rgba(220, 252, 245, 0.80)', lightBorder: 'rgba(15, 118, 110, 0.34)' },
  macro: { accent: '#60a5fa', darkBg: 'rgba(30, 64, 175, 0.24)', lightBg: 'rgba(219, 234, 254, 0.78)', lightBorder: 'rgba(37, 99, 235, 0.38)' },
  macroCandidate: { accent: '#fbbf24', darkBg: 'rgba(120, 83, 18, 0.22)', lightBg: 'rgba(254, 243, 199, 0.80)', lightBorder: 'rgba(217, 119, 6, 0.36)' },
  coordinator: { accent: '#38bdf8', darkBg: 'rgba(12, 74, 110, 0.34)', lightBg: 'rgba(224, 242, 254, 0.82)', lightBorder: 'rgba(2, 132, 199, 0.40)' },
  registry: { accent: '#5eead4', darkBg: 'rgba(19, 78, 74, 0.26)', lightBg: 'rgba(204, 251, 241, 0.72)', lightBorder: 'rgba(13, 148, 136, 0.30)' },
  specialist: { accent: '#22c55e', darkBg: 'rgba(20, 83, 45, 0.24)', lightBg: 'rgba(220, 252, 231, 0.72)', lightBorder: 'rgba(22, 163, 74, 0.34)' },
  titleSpecialist: { accent: '#f59e0b', darkBg: 'rgba(113, 63, 18, 0.23)', lightBg: 'rgba(254, 243, 199, 0.74)', lightBorder: 'rgba(217, 119, 6, 0.34)' },
  microagent: { accent: '#818cf8', darkBg: 'rgba(49, 46, 129, 0.30)', lightBg: 'rgba(224, 231, 255, 0.72)', lightBorder: 'rgba(99, 102, 241, 0.34)' },
  microSelect: { accent: '#93c5fd', darkBg: 'rgba(30, 58, 138, 0.26)', lightBg: 'rgba(219, 234, 254, 0.70)', lightBorder: 'rgba(59, 130, 246, 0.30)' },
  microGender: { accent: '#a78bfa', darkBg: 'rgba(76, 29, 149, 0.28)', lightBg: 'rgba(237, 233, 254, 0.74)', lightBorder: 'rgba(124, 58, 237, 0.32)' },
  microSpanish: { accent: '#fb7185', darkBg: 'rgba(136, 19, 55, 0.22)', lightBg: 'rgba(255, 228, 230, 0.72)', lightBorder: 'rgba(225, 29, 72, 0.30)' },
  composer: { accent: '#67e8f9', darkBg: 'rgba(21, 94, 117, 0.25)', lightBg: 'rgba(207, 250, 254, 0.70)', lightBorder: 'rgba(8, 145, 178, 0.30)' },
  lifecycle: { accent: '#34d399', darkBg: 'rgba(6, 78, 59, 0.24)', lightBg: 'rgba(209, 250, 229, 0.74)', lightBorder: 'rgba(5, 150, 105, 0.34)' },
  production: { accent: '#2dd4bf', darkBg: 'rgba(15, 118, 110, 0.26)', lightBg: 'rgba(204, 251, 241, 0.78)', lightBorder: 'rgba(13, 148, 136, 0.36)' },
};

const atlasPaletteForNode = (node) => {
  if (node.id?.includes('macro_risk_model_latest')) return atlasNodePalettes.macroCandidate;
  if (node.id?.includes('macro_risk_model')) return atlasNodePalettes.macro;
  if (node.type === 'guard') return atlasNodePalettes.guard;
  if (node.type === 'memory' || node.type === 'issue_memory') return atlasNodePalettes.memory;
  if (node.type === 'coordinator') return atlasNodePalettes.coordinator;
  if (node.type === 'registry') return atlasNodePalettes.registry;
  if (node.id?.includes('titles')) return atlasNodePalettes.titleSpecialist;
  if (node.type === 'specialist' || node.type === 'specialist_legacy') return atlasNodePalettes.specialist;
  if (node.id?.includes('select_cstring')) return atlasNodePalettes.microSelect;
  if (node.id?.includes('requirement_effect_router')) return atlasNodePalettes.microagent;
  if (node.id?.includes('effect_list')) return atlasNodePalettes.microagent;
  if (node.id?.includes('gender')) return atlasNodePalettes.microGender;
  if (node.id?.includes('spanish')) return atlasNodePalettes.microSpanish;
  if (node.type === 'composition_coordinator') return atlasNodePalettes.composer;
  if (node.type === 'lifecycle_policy' || node.type === 'lifecycle_state') return atlasNodePalettes.lifecycle;
  if (node.type === 'production_gateway' || node.type === 'output') return atlasNodePalettes.production;
  if (node.type === 'microagent') return atlasNodePalettes.microagent;
  return atlasNodePalettes.macro;
};

const atlasPositionById = {
  deterministic_guards: { x: 8, y: 42 },
  translation_memory: { x: 8, y: 68 },
  macro_risk_model_active: { x: 23, y: 42 },
  macro_risk_model_latest: { x: 23, y: 68 },
  coordinator_ensemble_v1: { x: 37, y: 55 },
  agent_registry: { x: 37, y: 82 },
  religion_specialist: { x: 51, y: 38 },
  titles_specialist_legacy: { x: 51, y: 55 },
  culture_title_labels: { x: 51, y: 75 },
  issue_ledger: { x: 63, y: 55 },
  requirement_effect_router_readonly: { x: 69, y: 42 },
  effect_list_package: { x: 69, y: 55 },
  artifact_activity_effect_policy: { x: 69, y: 68 },
  micro_short_label_style: { x: 76, y: 29 },
  micro_dynamic_ck3_expression: { x: 76, y: 42 },
  select_cstring_local_player_preterite_verb_rewrite: { x: 76, y: 55 },
  select_cstring_local_player_reflexive_phrase_rewrite: { x: 76, y: 68 },
  select_cstring_local_player_possessive_pronoun_rewrite: { x: 76, y: 82 },
  micro_gender_token: { x: 85.5, y: 31 },
  micro_spanish_residual: { x: 85.5, y: 44 },
  micro_long_text_composer: { x: 85.5, y: 58 },
  micro_semantic_review_router: { x: 85.5, y: 73 },
  lifecycle_shadow_checkpoint: { x: 94.5, y: 40 },
  segment_state: { x: 94.5, y: 62 },
  production_runner: { x: 94.5, y: 84 },
};

const atlasFamilyColumns = {
  guards: 10,
  macro: 28,
  coordinator: 44,
  specialists: 61,
  subagents: 76,
  lifecycle_policies: 88,
  production_output: 94,
  ingest: 8,
  safety: 20,
  knowledge: 22,
  ml: 40,
  routing: 54,
  religion: 68,
  titles: 69,
  ui: 67,
  production: 88,
  release: 94,
};

const ptIdentifierLabel = (value) => {
  let text = String(value ?? '').toLowerCase();
  const phrases = {
    select_cstring: 'selectcstring',
    script_value: 'scriptvalue',
    local_player: 'jogadorlocal',
    same_token: 'mesmotoken',
    effect_list: 'listadeefeitos',
    short_label: 'rotulocurto',
    long_text: 'textolongo',
    custom_localization: 'localizacaocustomizada',
    dry_run: 'simulacao',
    read_only: 'somenteleitura',
    whole_segment: 'segmentocompleto',
    source_segments: 'segmentosfonte',
  };
  Object.entries(phrases).forEach(([source, target]) => {
    text = text.replaceAll(source, target);
  });
  const words = {
    selectcstring: 'Select_CString',
    scriptvalue: 'ScriptValue',
    jogadorlocal: 'jogador local',
    mesmotoken: 'mesmo token',
    listadeefeitos: 'lista de efeitos',
    rotulocurto: 'rótulo curto',
    textolongo: 'texto longo',
    localizacaocustomizada: 'localização personalizada',
    simulacao: 'simulação',
    somenteleitura: 'somente leitura',
    segmentocompleto: 'segmento completo',
    segmentosfonte: 'segmentos-fonte',
    activity: 'atividade',
    agent: 'agente',
    alignment: 'alinhamento',
    allowlist: 'lista de permissão',
    artifact: 'artefato',
    audit: 'auditoria',
    auditor: 'auditor',
    baseline: 'base',
    boundary: 'limite',
    bridge: 'ponte',
    builder: 'construtor',
    candidate: 'candidato',
    catalog: 'catálogo',
    checkpoint: 'ponto de controle',
    cleanup: 'limpeza',
    composition: 'composição',
    concept: 'conceito',
    context: 'contexto',
    controlled: 'controlado',
    coordinator: 'coordenador',
    culture: 'cultura',
    decision: 'decisão',
    delta: 'variação',
    domain: 'domínio',
    dynamic: 'dinâmico',
    effect: 'efeito',
    embedded: 'incorporado',
    entity: 'entidade',
    evidence: 'evidência',
    experimental: 'experimental',
    final: 'final',
    fragment: 'fragmento',
    gender: 'gênero',
    global: 'global',
    governance: 'governança',
    governed: 'governado',
    guard: 'proteção',
    guarded: 'protegido',
    hard: 'rígido',
    heritage: 'herança',
    lifecycle: 'ciclo de vida',
    literal: 'literal',
    manual: 'manual',
    maturity: 'maturidade',
    measure: 'medir',
    model: 'modelo',
    morphology: 'morfologia',
    multiline: 'multilinha',
    narrative: 'narrativa',
    negative: 'negativo',
    nickname: 'apelido',
    observe: 'observar',
    output: 'saída',
    overlay: 'camada',
    partial: 'parcial',
    phrase: 'frase',
    policy: 'política',
    positive: 'positivo',
    preservation: 'preservação',
    production: 'produção',
    profile: 'perfil',
    pronoun: 'pronome',
    readiness: 'prontidão',
    relation: 'relação',
    release: 'liberação',
    religion: 'religião',
    repair: 'reparo',
    requirement: 'requisito',
    review: 'revisão',
    rewrite: 'reescrita',
    route: 'rota',
    router: 'roteador',
    runtime: 'execução',
    safe: 'seguro',
    sampler: 'amostrador',
    sampling: 'amostragem',
    scope: 'escopo',
    score: 'pontuação',
    second: 'segunda',
    semantic: 'semântico',
    sentence: 'frase',
    shadow: 'observação',
    specialist: 'especialista',
    split: 'divisão',
    splitter: 'divisor',
    subpolicy: 'subpolítica',
    surface: 'superfície',
    terminal: 'terminal',
    title: 'título',
    token: 'token',
    trait: 'traço',
    tradition: 'tradição',
    triage: 'triagem',
    unlock: 'desbloqueio',
    validate: 'validar',
    activities: 'atividades',
    agents: 'agentes',
    artifacts: 'artefatos',
    blocker: 'bloqueio',
    blockers: 'bloqueios',
    candidates: 'candidatos',
    change: 'alteração',
    changes: 'alterações',
    composed: 'composto',
    decisions: 'decisões',
    effects: 'efeitos',
    english: 'inglês',
    expression: 'expressão',
    expressions: 'expressões',
    file: 'arquivo',
    files: 'arquivos',
    guards: 'proteções',
    group: 'grupo',
    groups: 'grupos',
    issue: 'problema',
    issues: 'problemas',
    label: 'rótulo',
    labels: 'rótulos',
    local: 'local',
    mature: 'maduro',
    multistage: 'várias etapas',
    native: 'nativo',
    network: 'rede',
    outputs: 'saídas',
    payload: 'conteúdo',
    payloads: 'conteúdos',
    player: 'jogador',
    policies: 'políticas',
    possessive: 'possessivo',
    pronouns: 'pronomes',
    registry: 'registro',
    repairs: 'reparos',
    rows: 'linhas',
    run: 'execução',
    requirements: 'requisitos',
    routes: 'rotas',
    same: 'mesmo',
    segment: 'segmento',
    segments: 'segmentos',
    source: 'fonte',
    sources: 'fontes',
    spanish: 'espanhol',
    splitters: 'divisores',
    state: 'estado',
    status: 'estado',
    tokens: 'tokens',
    validation: 'validação',
    vote: 'voto',
    weak: 'fraco',
  };
  return text
    .split(/[_\s]+/)
    .filter(Boolean)
    .map((word) => words[word] ?? word)
    .join(' ')
    .replace(/\s+/g, ' ')
    .trim();
};

const ptFieldLabel = (value) => {
  const labels = {
    accuracy: 'acuracia',
    active_model_version: 'versao modelo ativo',
    active_model_run_id: 'run do modelo ativo',
    active_score_run_id: 'score ativo',
    agents_considered_count: 'agentes considerados',
    ambiguous_pronoun_argument_evidence: 'evidencia de pronome ambiguo',
    auto_apply_allowed: 'auto-apply permitido',
    blank_valid_count: 'vazios validos',
    blocked_structure_count: 'bloqueios estruturais',
    boundary_context_evidence: 'evidencia de contexto boundary',
    candidate_count: 'candidatos',
    checkpoint_allowed: 'checkpoint permitido',
    checkpoint_blocked: 'checkpoint bloqueado',
    checkpoint_run: 'run de checkpoint',
    closed_count: 'segmentos fechados',
    closed_ratio: 'proporcao fechada',
    composition_potential_segments: 'segmentos com potencial de composicao',
    composition_recheck_queue: 'fila de rechecagem de composicao',
    conflict_count: 'conflitos',
    context_boundary_resolved_rows: 'linhas de boundary resolvidas',
    context_boundary_review_rows: 'linhas de boundary em revisao',
    covered_issue_count: 'problemas cobertos',
    covered_issues: 'problemas cobertos',
    dataset_run_id: 'dataset',
    dynamic_ck3_checkpoint_allowed: 'checkpoint dinamico CK3 permitido',
    dynamic_select_cstring_observations: 'observacoes Select_CString dinamicas',
    evidence_rows: 'linhas de evidencia',
    experimental_false_safe_count: 'falso-seguro experimental',
    experimental_watch_count: 'watch experimental',
    false_positive_reopen_count: 'reaberturas falso-positivas',
    false_safe_count: 'falso seguro',
    final_auto_safe_count: 'auto-safe final',
    full_queue_boundary_context_evidence: 'evidencia boundary na fila completa',
    full_queue_positive_learning_evidence: 'evidencia positiva na fila completa',
    full_queue_review_rows: 'linhas de revisao na fila completa',
    gender_boundary_checkpoint_allowed: 'checkpoint de boundary de genero permitido',
    high_confidence_rows: 'linhas de alta confianca',
    human_confirmed_count: 'humanos confirmados',
    human_locked_count: 'travados humanos',
    issues_total: 'problemas totais',
    issue_families_open: 'familias abertas',
    known_preterite_verb_shift: 'mudanca conhecida de preterito',
    latest_checkpoint_run_id: 'checkpoint mais recente',
    latest_context_review_decision_run: 'ultima decisao de revisao contextual',
    latest_ptbr_evidence_decision_run: 'ultima decisao de evidencia PT-BR',
    latest_shadow_ready: 'shadow pronto mais recente',
    latest_shadow_repair_blocked: 'reparo shadow bloqueado recente',
    latest_shadow_repair_dry_run: 'dry-run de reparo shadow recente',
    latest_shadow_repair_ready: 'reparo shadow pronto recente',
    latest_shadow_run_id: 'shadow mais recente',
    learning_gate_production_safe: 'gate de aprendizado libera producao',
    ledger_issue_count: 'problemas no ledger',
    literal_microagent_candidates: 'candidatos do microagente literal',
    literal_subtype_audit_rows: 'linhas de auditoria de subtipo literal',
    local_player_possessive_pronoun_evidence: 'evidencia de possessivo local-player',
    local_player_preterite_verb_evidence: 'evidencia de preterito local-player',
    local_player_reflexive_phrase_evidence: 'evidencia de frase reflexiva local-player',
    low_confidence_rows: 'linhas de baixa confianca',
    macro_f1: 'macro F1',
    materialized_routing_sample: 'amostra roteada',
    medium_confidence_rows: 'linhas de media confianca',
    model_run_id: 'run do modelo',
    model_version: 'versao do modelo',
    needs_apply_latest: 'precisa aplicar recente',
    needs_autofix_count: 'precisam autofix',
    needs_human_count: 'precisam humano',
    operational_state_authoritative: 'estado authoritative',
    operational_state_candidate: 'estado candidate',
    operational_state_dry_run: 'estado dry-run',
    operational_state_experimental: 'estado experimental',
    operational_state_operational: 'estado operacional',
    operational_state_shadow: 'estado shadow',
    output_apply_pending_count: 'aplicar output pendente',
    pending_count: 'pendentes',
    policy_status: 'status da politica',
    positive_checkpoint_allowed: 'checkpoint positivo permitido',
    positive_learning_evidence: 'evidencia positiva de aprendizado',
    positive_release_allowed: 'release positivo permitido',
    positive_release_candidates: 'candidatos a release positivo',
    production_release_allowed: 'release de producao permitido',
    production_release_allowed_for_new_shadow: 'release permitido para shadow novo',
    promotion_hint: 'indicacao de promocao',
    ptbr_evidence_decision_run: 'run de decisao de evidencia PT-BR',
    ptbr_evidence_queue_rows: 'linhas na fila de evidencia PT-BR',
    ptbr_hint_rows: 'linhas com dica PT-BR',
    recommendation_count: 'recomendacoes',
    registered_agents: 'agentes registrados',
    regular_preterite_verb_shift: 'mudanca regular de preterito',
    remaining_specialist_blockers: 'bloqueios restantes de especialista',
    repair_queue_size: 'tamanho da fila de reparo',
    reopen_count: 'reaberturas',
    route_sample_count: 'amostra de roteamento',
    routing_run_id: 'run de roteamento',
    run_id: 'run',
    safe_precision: 'precisao segura',
    safe_recall: 'recall seguro',
    safe_pattern_candidate: 'candidato de padrao seguro',
    same_payload_noop_ready: 'payload igual noop pronto',
    same_payload_observations: 'observacoes de payload igual',
    same_token_repair_queue_items: 'itens de reparo same-token',
    scope_sum_routed_count: 'soma de escopo roteada',
    segment_composition_blocked: 'composicao de segmento bloqueada',
    segment_composition_bridge_candidate: 'candidato de ponte de composicao',
    segment_composition_final_overlay_segments: 'segmentos finais no overlay de composicao',
    segment_composition_maturity_audit_run: 'run de auditoria de maturidade da composicao',
    segment_composition_maturity_issues: 'problemas de maturidade da composicao',
    segment_composition_maturity_status: 'status de maturidade da composicao',
    segment_composition_overlay_run: 'run de overlay de composicao',
    segment_composition_preterite_segments: 'segmentos de preterito na composicao',
    segment_composition_total_segments: 'segmentos totais na composicao',
    segment_lifecycle_bridge_blocked: 'ponte lifecycle bloqueada',
    segment_lifecycle_bridge_candidate: 'candidato de ponte lifecycle',
    segment_lifecycle_bridge_proposal_run: 'run de proposta da ponte lifecycle',
    segment_lifecycle_bridge_ready: 'ponte lifecycle pronta',
    segments_full_coverage: 'segmentos com cobertura completa',
    segments_no_coverage: 'segmentos sem cobertura',
    segments_partial_coverage: 'segmentos parciais',
    segments_scanned_count: 'segmentos analisados',
    shadow_ready_boundary_repairs: 'reparos boundary shadow prontos',
    shadow_repair_blocked: 'reparo shadow bloqueado',
    shadow_repair_dry_run: 'dry-run de reparo shadow',
    shadow_repair_ready: 'reparo shadow pronto',
    short_label_checkpoint_allowed: 'checkpoint de label curto permitido',
    short_label_guarded_lifecycle_bridge_releases: 'releases protegidos de labels curtos',
    snapshot_archiving: 'arquivamento de snapshot',
    spanish_suspect_payload_observations: 'observacoes de payload espanhol suspeito',
    static_token_only_guarded_checkpoint: 'checkpoint protegido apenas-token-estatico',
    status_active: 'ativos',
    status_experimental: 'experimentais',
    status_planned: 'planejados',
    subagents: 'subagentes',
    token_change_repair_queue_items: 'itens de reparo por mudanca de token',
    total_segments: 'segmentos totais',
    training_examples_latest_model: 'exemplos no treino recente',
    trigger_gender_role_surface_checkpoint: 'checkpoint de superficie por papel de genero',
    validation_review_rows: 'linhas de validacao revisadas',
    validation_sample_rows: 'linhas da amostra de validacao',
    weighted_coverage_ratio: 'cobertura ponderada',
    writes_in_this_learning_cycle: 'escritas neste ciclo de aprendizado',
  };
  return labels[value] ?? ptIdentifierLabel(value);
};

const ptStatus = (value) => ({
  active: 'ativo',
  authoritative: 'com autoridade',
  candidate: 'candidato',
  checkpoint: 'ponto de controle',
  dry_run: 'simulação',
  evidence_validated: 'evidência validada',
  experimental: 'experimental',
  experimental_watch: 'experimental em observação',
  growing: 'em crescimento',
  guarded: 'protegido',
  learning: 'aprendendo',
  operational: 'operacional',
  planned: 'planejado',
  promising: 'promissor',
  safe: 'seguro',
  shadow: 'em observação',
  shadow_audit: 'auditoria em observação',
  stable: 'estável',
}[value] ?? ptIdentifierLabel(value));

const ptType = (value) => ({
  composition_coordinator: 'compositor',
  coordinator: 'coordenador',
  guard: 'guarda',
  input: 'entrada',
  issue_memory: 'memoria de problemas',
  lifecycle_policy: 'política do ciclo de vida',
  lifecycle_state: 'estado do ciclo de vida',
  macro_model: 'modelo macro',
  macro_model_candidate: 'macro candidato',
  memory: 'memoria',
  microagent: 'microagente',
  model: 'modelo',
  output: 'saida',
  policy: 'politica',
  production_gateway: 'producao',
  registry: 'registro',
  specialist: 'especialista',
  specialist_legacy: 'especialista legado',
  subcoordinator: 'subcoordenador',
  subspecialist: 'subespecialista',
  symbolic_guard: 'guarda simbolico',
  symbolic_subpolicy: 'subpolitica simbolica',
  governance_auditor: 'auditor de governanca',
  governance_bridge: 'ponte de governanca',
  composition_policy: 'política de composição',
  review_sampler: 'amostrador de revisão',
}[value] ?? ptIdentifierLabel(value));

const ptFamily = (value) => ({
  coordinator: 'coordenacao',
  guards: 'guardas',
  lifecycle_policies: 'políticas do ciclo de vida',
  macro: 'modelo macro',
  production_output: 'produção e saída',
  specialists: 'especialistas',
  subagents: 'subagentes',
  memory: 'memoria',
  network: 'rede',
}[value] ?? ptIdentifierLabel(value));

const ptRole = (value) => ({
  authoritative_hard_gate: 'Trava estrutural principal.',
  broad_risk_classifier: 'Classifica risco geral do pacote.',
  candidate_general_classifier: 'Modelo candidato para comparacao.',
  domain_vote: 'Voto especializado por dominio.',
  governed_promotion_path: 'Promove evidencias por etapas seguras.',
  legacy_domain_vote: 'Evidencia legada usada com cautela.',
  multi_issue_segment_composer: 'Combina reparos parciais em segmentos complexos.',
  network_inventory: 'Inventario dos agentes da rede.',
  operational_progress_meter: 'Mede o estado operacional dos segmentos.',
  problem_level_memory: 'Memoria dos problemas detectados.',
  residual_spanish_repair_specialist: 'Detecta e separa residuo espanhol real.',
  route_and_arbitrate: 'Roteia segmentos e arbitra evidencias.',
  route_and_split: 'Roteia e divide casos por subpolitica.',
  semantic_triage: 'Organiza pendencias semanticas.',
  short_ui_label_specialist: 'Cuida de labels curtos de interface.',
  safe_output_writer: 'Escreve output apenas pelo fluxo protegido.',
  trusted_evidence_store: 'Guarda evidencias confiaveis e revisoes.',
  dynamic_expression_specialist: 'Analisa expressoes dinamicas do CK3.',
  gender_token_specialist: 'Valida genero, artigos e tokens relacionados.',
  terminal_guard: 'Guard terminal somente leitura.',
  guarded_context_checkpoint: 'Checkpoint contextual protegido.',
  guarded_repair_checkpoint: 'Checkpoint de reparo protegido.',
  guarded_release: 'Release protegido por politica.',
  shadow_positive_boundary: 'Boundary positivo em shadow.',
  negative_boundary: 'Boundary negativo de seguranca.',
  audit_bridge_readiness: 'Audita a prontidão da ponte antes de qualquer liberação.',
  baseline_score: 'Fornece a pontuação de referência.',
  boundary_only: 'Avalia somente o limite de segurança.',
  cleanup_gate: 'Controla a etapa final de limpeza.',
  controlled_production_readiness_audit: 'Audita de forma controlada a prontidão para produção.',
  custom_localization_flower_lexical_repair: 'Repara vocabulário floral em localizações personalizadas.',
  custom_localization_native_breed_name_policy: 'Avalia quando nomes nativos de raças devem ser preservados.',
  custom_localization_runtime_boundary_review: 'Revisa limites de execução da localização personalizada.',
  custom_localization_short_fragment_specialist: 'Especializa-se em fragmentos curtos de localização personalizada.',
  dynamic_literal_repair_builder: 'Constrói reparos para literais dinâmicos.',
  evidence_only: 'Produz evidência, sem autoridade para alterar a saída.',
  guarded_label_rewrite_checkpoint: 'Ponto de controle protegido para reescrita de rótulos.',
  guarded_lifecycle_policy: 'Política protegida do ciclo de vida.',
  guarded_positive_candidate: 'Candidato positivo mantido sob proteção.',
  guarded_relation_rewrite_checkpoint: 'Ponto de controle protegido para reescrita de relações.',
  guarded_release_candidate: 'Candidato a liberação protegida.',
  guarded_rewrite_checkpoint: 'Ponto de controle protegido para reescrita.',
  guarded_sentence_rewrite_checkpoint: 'Ponto de controle protegido para reescrita de frases.',
  guarded_shadow_text_policy: 'Política protegida de texto em observação.',
  guarded_title_semantic_checkpoint: 'Ponto de controle semântico protegido para títulos.',
  hard_gate: 'Trava rígida de segurança.',
  learning_only_microagent: 'Aprende padrões sem alterar diretamente a saída.',
  lifecycle_reopen_triage: 'Faz triagem de reaberturas no ciclo de vida.',
  literal_boundary_review: 'Revisa limites de literais.',
  manual_boundary_review: 'Revisão manual dos limites de segurança.',
  manual_narrative_boundary: 'Revisão manual do limite narrativo.',
  manual_sampler_review: 'Revisão manual por amostragem.',
  manual_sampling: 'Amostragem manual.',
  measure_composed_shadow_release: 'Mede a liberação composta em observação.',
  measure_final_composed_shadow_release: 'Mede a liberação final composta em observação.',
  measure_multistage_composed_shadow_release: 'Mede a liberação composta em várias etapas.',
  mechanical_short_label_residual_repair: 'Repara mecanicamente resíduos em rótulos curtos.',
  needs_policy_evidence: 'Requer mais evidência antes de definir uma política.',
  negative_boundary_review: 'Revisa limites negativos de segurança.',
  partial_existing_policy_router: 'Roteia casos parciais para políticas existentes.',
  partial_phrase_mapping_guard: 'Protege o mapeamento parcial de frases.',
  partial_semantic_voter: 'Emite voto semântico parcial.',
  partial_surface_repair_builder: 'Constrói reparos parciais de superfície.',
  partial_token_alignment_builder: 'Constrói alinhamentos parciais de tokens.',
  partial_token_context_guard: 'Protege o contexto de reparos parciais de tokens.',
  partial_token_repair_builder: 'Constrói reparos parciais de tokens.',
  positive_boundary: 'Limite positivo de segurança.',
  positive_sampler_review: 'Revisão positiva por amostragem.',
  profile_and_sample: 'Cria perfil e seleciona amostras.',
  propose_governed_bridge_candidates: 'Propõe candidatos para uma ponte governada.',
  repair_local_player_pronoun_literal: 'Repara literais de pronome do jogador local.',
  repair_route_checkpoint: 'Ponto de controle da rota de reparo.',
  route_and_observe: 'Roteia casos e acompanha os resultados.',
  route_multiagent_segment_closure: 'Coordena o fechamento de segmentos por vários agentes.',
  route_then_boundary: 'Roteia primeiro e depois avalia o limite.',
  second_layer_cleanup_checkpoint: 'Ponto de controle da segunda camada de limpeza.',
  semantic_delta_boundary_review: 'Revisa o limite da variação semântica.',
  short_ui_policy_sampling: 'Amostra políticas para interfaces curtas.',
  token_policy_allowlist: 'Lista de permissão da política de tokens.',
  token_subpolicy_shadow: 'Subpolítica de tokens em observação.',
  validate_dynamic_literal_payload_delta: 'Valida variações em literais dinâmicos.',
  vote: 'Emite voto especializado.',
  weak_auto_sampling_router: 'Roteia automaticamente amostras de evidência fraca.',
  whole_segment_recheck_gate: 'Reavalia o segmento completo antes do fechamento.',
}[value] ?? ptIdentifierLabel(value));

const ptSentence = (value) => {
  if (!value) return '';
  const text = String(value);
  const translations = {
    'Specializes in short custom-localization fragments that are safe only when their key, file lane and fragment role indicate a clean localizable word or tiny phrase.':
      'Especializa-se em fragmentos curtos de localização personalizada, seguros apenas quando a chave, a faixa do arquivo e a função do fragmento indicam uma palavra ou frase curta claramente localizável.',
    'Audits whether custom_localization segments with fully covered issue families are mature enough for whole-segment recheck. It measures composition opportunity only and has no output authority.':
      'Audita se segmentos de localização personalizada, com todas as famílias de problemas cobertas, estão maduros para reavaliação completa. Mede apenas oportunidades de composição e não possui autoridade sobre a saída.',
    'Learns narrow PT-BR lexical repairs for flower labels discovered by custom_localization composition recheck failures, such as papoila->papoula, aster->áster and peonía->peônia.':
      'Aprende reparos lexicais PT-BR específicos para nomes de flores encontrados em falhas de reavaliação da composição, como papoila→papoula, aster→áster e peonía→peônia.',
    'Detects dog type labels where English and Spanish preserve a native breed/name but PT-BR translated or morphologically localized it, requiring a preservation policy before closure.':
      'Detecta rótulos de raças em que inglês e espanhol preservam o nome nativo, mas o PT-BR o traduziu ou flexionou. Esses casos exigem uma política de preservação antes do fechamento.',
    'Narrow subagent that repairs CK3 visible markup from #bold No#!/#bold no#!/#BOLD No#! to PT-BR #bold Não#!/#bold não#!/#BOLD Não#! when no other residual blocker remains.':
      'Subagente específico que corrige a marcação visível do CK3 de #bold No#!/#bold no#!/#BOLD No#! para #bold Não#!/#bold não#!/#BOLD Não#! quando não existe outro bloqueio residual.',
    'Learns possessive local-player branch shifts such as tus to sus before PT-BR final wording.':
      'Aprende mudanças de pronomes possessivos nos ramos do jogador local, como tus para sus, antes da redação final em PT-BR.',
    'Splits token-safe pending items where the model requests autofix but no current specialist explains the issue. Reviewed safe UI surfaces can now pass through a guarded lifecycle bridge, but this agent still does not write production output.':
      'Separa pendências seguras quanto a tokens quando o modelo solicita correção automática, mas nenhum especialista atual explica o problema. Superfícies de interface revisadas podem seguir por uma ponte protegida do ciclo de vida, mas este agente ainda não escreve na saída de produção.',
    'domain adjectives can look like ordinary Portuguese words':
      'Adjetivos de domínio podem parecer palavras comuns em português.',
    'file-level calibration is not enough for broad promotion':
      'Calibração apenas por arquivo não é suficiente para uma promoção ampla.',
    'repair PT-BR flower variants such as papoulas and áster before lifecycle closure':
      'Reparar variantes florais em PT-BR, como papoulas e áster, antes do fechamento do ciclo de vida.',
    'create a native dog-breed preservation microagent for cases like asong gubat':
      'Criar um microagente de preservação de nomes nativos de raças para casos como asong gubat.',
    'review another strong-candidate slice before considering lifecycle authority':
      'Revisar outra amostra de candidatos fortes antes de considerar autoridade no ciclo de vida.',
    'botanical names can be common names, latinized names, or intentionally preserved cultural terms':
      'Nomes botânicos podem ser nomes populares, formas latinizadas ou termos culturais preservados intencionalmente.',
    'mojibake-like strings require manual confirmation before repair':
      'Textos com aparência de codificação corrompida exigem confirmação manual antes do reparo.',
    'convert checkpointed repairs into a production-flow proposal only after review':
      'Converter reparos aprovados no ponto de controle em proposta para o fluxo de produção somente após revisão.',
    'split botanical domain-review cases into a separate preservation/translation policy':
      'Separar casos botânicos em uma política própria de preservação ou tradução.',
    'some breed names can have accepted Portuguese forms':
      'Alguns nomes de raças podem ter formas aceitas em português.',
    'preserving every English/Spanish identical dog type would be too broad':
      'Preservar todo nome de raça idêntico em inglês e espanhol seria uma regra ampla demais.',
    'review asong gubat and telomian as a tiny domain policy':
      'Revisar asong gubat e telomian em uma política de domínio bem específica.',
    'decide whether native multiword names and latinized breed names should be preserved or localized':
      'Decidir se nomes nativos compostos e nomes latinizados de raças devem ser preservados ou localizados.',
    'does not repair Spanish that remains outside the bold No marker':
      'Não repara espanhol que permaneça fora do marcador bold No.',
    'issue coverage is not output application':
      'Cobrir o problema não significa aplicar a alteração na saída.',
    'promote only through lifecycle after final production policy review':
      'Promover somente pelo ciclo de vida e após revisão final da política de produção.',
    'route the 5 blocked cases to Spanish residual and punctuation repair lanes':
      'Encaminhar os cinco casos bloqueados para as filas de resíduo espanhol e reparo de pontuação.',
    'surface clusters can mix real UI with narrative prose containing markup':
      'Agrupamentos de superfície podem misturar interface real com prosa narrativa que contém marcação.',
    'safe_surface evidence is not segment-level production authority':
      'Evidência de superfície segura não concede autoridade de produção sobre o segmento.',
    'create warning/list/confirmation microagents only after more reviewed evidence':
      'Criar microagentes de avisos, listas e confirmações somente após ampliar a evidência revisada.',
    'keep event/building/plain prose as semantic review queues':
      'Manter eventos, construções e prosa comum nas filas de revisão semântica.',
    'Routes segments and issues to macro, specialists, subagents and lifecycle gates. It organizes evidence rather than allowing each agent to close output alone.':
      'Roteia segmentos e problemas para o macro, especialistas, subagentes e gates de lifecycle. Organiza evidencias sem permitir que um agente feche output sozinho.',
    'Read-only dry-run router for requirement/effect surfaces. It separates terminal guards from shadow splitters without apply or lifecycle authority.':
      'Router dry-run somente leitura para superficies de requisito/efeito. Separa guards terminais de splitters shadow sem autoridade de apply ou lifecycle.',
    'Learns Spanish second-person preterite literals in local-player Select_CString branches before PT-BR rewrite.':
      'Aprende literais espanhois de preterito em segunda pessoa nos ramos Select_CString do jogador local antes da reescrita PT-BR.',
    'Learns reflexive branch shifts such as second-person local-player phrase to third-person phrase.':
      'Aprende mudancas de ramo reflexivo, como converter frase de segunda pessoa do jogador local para frase em terceira pessoa.',
    'Learns local-player possessive pronoun shifts before PT-BR rewrite.':
      'Aprende mudancas de pronome possessivo do jogador local antes da reescrita PT-BR.',
    'Learns local-player Select_CString branch shifts before PT-BR rewrite.':
      'Aprende mudancas de ramo Select_CString do jogador local antes da reescrita PT-BR.',
    'full issue coverage is not the same as safe whole-segment closure':
      'Cobertura total de problemas nao e o mesmo que fechamento seguro do segmento inteiro.',
    'semantic-only candidates may hide file-lane context errors':
      'Candidatos apenas semanticos podem esconder erros de contexto por arquivo/faixa.',
    'keep composition auditor in shadow until more slices confirm precision':
      'Manter o auditor de composicao em shadow ate mais fatias confirmarem precisao.',
    'route PT-BR flower style repairs to a small lexical microagent':
      'Roteiar reparos de estilo floral PT-BR para um microagente lexical pequeno.',
    'route native breed/name preservation to a domain microagent':
      'Roteiar preservacao de raca/nome nativo para um microagente de dominio.',
    'Catalog of macro, guards, specialists, microagents, symbolic subpolicies and lifecycle actors.':
      'Catalogo dos modelos macro, guardas, especialistas, microagentes, subpoliticas simbolicas e atores de lifecycle.',
    'Current promoted general classifier. It sees the broad package and remains active because newer candidates lowered operational safe recall.':
      'Classificador geral promovido atualmente. Enxerga o pacote como um todo e segue ativo porque candidatos recentes reduziram o recall seguro operacional.',
    'Most recent trained classifier. It has zero false-safe in holdout but lower safe recall, so it is not promoted.':
      'Classificador treinado mais recente. Tem zero falso-seguro no holdout, mas recall seguro menor, por isso nao foi promovido.',
    'Protects keys, line structure, placeholders, CK3 tokens, blanks, locked human decisions and token policy violations.':
      'Protege chaves, estrutura de linha, placeholders, tokens CK3, vazios, decisoes humanas travadas e violacoes de politica de tokens.',
    'Stores human confirmations, locked exceptions, approved repairs and game-tested output as evidence rather than blind truth.':
      'Guarda confirmacoes humanas, excecoes travadas, reparos aprovados e output testado no jogo como evidencia, nao como verdade cega.',
    'Operational specialist for religion terminology, divine references and preserved religious terms.':
      'Especialista operacional para terminologia religiosa, referencias divinas e termos religiosos preservados.',
    'Legacy broad title specialist. It remains useful as evidence, but broad title logic has known ambiguity and experimental false-safe history.':
      'Especialista amplo legado de titulos. Ainda e util como evidencia, mas titulos amplos misturam ambiguidades e historico experimental de falso-seguro.',
    'Operational specialist for cultural title labels and culture/title naming conventions.':
      'Especialista operacional para labels de titulos culturais e convencoes de nomeacao por cultura/titulo.',
    'Breaks pending segments into issue-level units so several microagents can work on one segment before a final composition audit.':
      'Quebra segmentos pendentes em problemas menores, permitindo que varios microagentes atuem antes de uma auditoria final de composicao.',
    'Handles compact UI labels and short texts. This is the largest open issue family and the highest-impact next area.':
      'Trata labels compactos de interface e textos curtos. E a maior familia aberta e a area de maior impacto imediato.',
    'Understands CK3 dynamic expressions such as Custom helpers and Select_CString branches. Current focus is no-op and literal-payload handling.':
      'Entende expressoes dinamicas do CK3, como helpers Custom e ramos Select_CString. O foco atual e no-op e payload literal.',
    'Validates gender helper usage, pronoun surfaces and gendered endings without allowing unsafe token drift.':
      'Valida helpers de genero, pronomes e terminacoes generificadas sem permitir deriva insegura de tokens.',
    'Detects and repairs actual Spanish residue while avoiding false alarms caused by CK3 keys, names or package language identifiers.':
      'Detecta e repara residuo espanhol real, evitando falsos alarmes causados por chaves CK3, nomes ou identificadores do pacote.',
    'Combines partial repairs from multiple microagents in long or mixed segments, then requires final recheck before segment closure.':
      'Combina reparos parciais de varios microagentes em segmentos longos ou mistos, exigindo rechecagem final antes do fechamento.',
    'Large open family for semantic review routing. It currently identifies need for semantic attention more than it fixes text.':
      'Grande familia aberta para roteamento de revisao semantica. Hoje identifica necessidade de atencao mais do que corrige texto.',
    'Moves candidates from evidence to shadow, checkpoint, lifecycle observation and guarded release without directly authorizing output writes.':
      'Move candidatos de evidencia para shadow, checkpoint, observacao lifecycle e release protegido sem autorizar escrita direta de output.',
    'Materializes current lifecycle state for each active localization segment without modifying output.':
      'Materializa o estado de lifecycle atual de cada segmento ativo sem modificar o output.',
    'Runs preflight, snapshots, dry-runs, token policy checks, controlled writes and post-write audits through the separate production flow.':
      'Executa preflight, snapshots, dry-runs, checagens de politica de token, escritas controladas e auditorias pos-escrita pelo fluxo separado de producao.',
    'scope-sum routed count is not unique segment count':
      'A soma de escopo roteada nao equivale a segmentos unicos.',
    'routing sample limit can hide tail behavior':
      'O limite da amostra de roteamento pode esconder comportamento de cauda.',
    'instrument unique routed segments':
      'Instrumentar contagem de segmentos roteados unicos.',
    'surface when a missing specialist should be created':
      'Evidenciar quando um especialista ausente deve ser criado.',
    'stale memory when source semantics change':
      'Memoria pode ficar desatualizada quando a semantica da fonte muda.',
    'old trusted text can encode legacy mistakes':
      'Texto confiavel antigo pode carregar erros legados.',
    'continue hash-based stale checks':
      'Continuar checagens de desatualizacao por hash.',
    'separate production feedback from training evidence':
      'Separar feedback de producao de evidencia de treino.',
    'PT-BR fluency may require intentional structural divergence from Spanish mirror':
      'A fluidez em PT-BR pode exigir divergencia estrutural intencional em relacao ao espelho espanhol.',
    'Portuguese may need seu/sua/seus/suas depending on noun gender and number':
      'O portugues pode exigir seu/sua/seus/suas conforme genero e numero do substantivo.',
    'Spanish source package labels should not be treated as localization residue':
      'Labels do pacote fonte espanhol nao devem ser tratados como residuo de localizacao.',
    'Spanish verb pair recognition is not yet a PT-BR translation':
      'Reconhecer pares verbais em espanhol ainda nao e o mesmo que traduzir para PT-BR.',
    'branch simplification can hide visible grammar issues':
      'Simplificar ramos pode esconder problemas gramaticais visiveis.',
    'broad title category mixes names to preserve, names to translate, gendered adjectives and structural exceptions':
      'A categoria ampla de titulos mistura nomes a preservar, nomes a traduzir, adjetivos generificados e excecoes estruturais.',
    'clean exactness is not always semantic correctness':
      'Exatidao limpa nem sempre significa correcao semantica.',
    'closed does not imply output was just written':
      'Fechado nao significa que o output acabou de ser escrito.',
    'coverage of issues is not the same as closing a segment':
      'Cobertura de problemas nao e o mesmo que fechar um segmento.',
    'culture labels can look like ordinary titles but carry historical naming constraints':
      'Labels culturais podem parecer titulos comuns, mas carregam restricoes historicas de nomenclatura.',
    'duplicate or overlapping issue families need composition rules':
      'Familias de problemas duplicadas ou sobrepostas precisam de regras de composicao.',
    'false block can slow production':
      'Bloqueio falso pode desacelerar a producao.',
    'generic model can miss specialized CK3 patterns':
      'Modelo generico pode perder padroes especializados do CK3.',
    'literal Spanish inside dynamic payload may require semantic rewrite':
      'Espanhol literal dentro de payload dinamico pode exigir reescrita semantica.',
    'many planned/experimental agents can visually look like failures if not labelled as lab work':
      'Muitos agentes planejados ou experimentais podem parecer falhas visualmente se nao forem marcados como trabalho de laboratorio.',
    'must not run while learning_status is locked':
      'Nao deve rodar enquanto o learning_status estiver bloqueado.',
    'nested Select_CString expressions may not be parsed by simple regex':
      'Expressoes Select_CString aninhadas podem nao ser interpretadas por regex simples.',
    'newer datasets do not automatically mean better promotion':
      'Datasets mais novos nao significam promocao melhor automaticamente.',
    'over-conservative candidate may inflate pending counts':
      'Candidato conservador demais pode inflar contagens de pendencia.',
    'partial repairs can conflict when merged':
      'Reparos parciais podem conflitar quando combinados.',
    'production writes are outside this learning thread':
      'Escritas de producao ficam fora desta frente de aprendizado.',
    'promotion would reduce operational coverage':
      'A promocao reduziria a cobertura operacional.',
    'proper names may resemble Spanish':
      'Nomes proprios podem se parecer com espanhol.',
    'raw pending includes watch/model suspicion, not all manual work':
      'Pendencia bruta inclui watch/suspeita do modelo, nao apenas trabalho manual.',
    'reflexive phrasing may need semantic context':
      'Frase reflexiva pode precisar de contexto semantico.',
    'requires final segment/context composition before closure':
      'Exige composicao final de segmento/contexto antes do fechamento.',
    'segment-level closure requires final holistic validation':
      'Fechamento em nivel de segmento exige validacao holistica final.',
    'semantic similarity cannot be inferred from tokens alone':
      'Similaridade semantica nao pode ser inferida apenas por tokens.',
    'short labels can hide semantic mismatch':
      'Labels curtos podem esconder incompatibilidade semantica.',
    'some divine names must be preserved while others translate semantically':
      'Alguns nomes divinos devem ser preservados enquanto outros traduzem semanticamente.',
    'too broad to become one reliable repair agent':
      'Amplo demais para virar um unico agente de reparo confiavel.',
    'visual dashboards can confuse shadow readiness with production authority':
      'Dashboards visuais podem confundir prontidao shadow com autoridade de producao.',
    'wrong helper can create visible gender bugs':
      'Helper errado pode criar bugs visiveis de genero.',
    'wrong reflexive target can change meaning':
      'Alvo reflexivo errado pode alterar o significado.',
    'audit full-coverage candidates before lifecycle closure':
      'Auditar candidatos com cobertura completa antes do fechamento lifecycle.',
    'classify 8 unclassified boundaries':
      'Classificar 8 boundaries ainda nao classificados.',
    'compare against active model by production-relevant cohorts':
      'Comparar com o modelo ativo por coortes relevantes para producao.',
    'connect with gender token microagent':
      'Conectar com o microagente de tokens de genero.',
    'continue policy-by-group strategy':
      'Continuar a estrategia de politicas por grupo.',
    'expand by subfamilies instead of one broad rule':
      'Expandir por subfamilias em vez de uma regra ampla.',
    'expand evidence beyond 45-row validation sample':
      'Expandir evidencia alem da amostra de validacao de 45 linhas.',
    'instrument blocker counts by rule family':
      'Instrumentar contagens de bloqueio por familia de regra.',
    'instrument composition precision':
      'Instrumentar precisao de composicao.',
    'integrate guarded lifecycle only after visual/readiness review':
      'Integrar lifecycle protegido apenas apos revisao visual e de prontidao.',
    'isolate boundary verbs that require context':
      'Isolar verbos de boundary que exigem contexto.',
    'keep authoritative':
      'Manter como autoridade.',
    'keep evidence-only until subagents absorb reliable patterns':
      'Manter apenas como evidencia ate os subagentes absorverem padroes confiaveis.',
    'keep production in chat 2':
      'Manter producao no chat 2.',
    'keep separating actionable pending, watch and lifecycle backlog':
      'Continuar separando pendencia acionavel, watch e backlog de lifecycle.',
    'keep subagents narrow':
      'Manter subagentes estreitos e especializados.',
    'mature evidence before production authority':
      'Amadurecer evidencia antes de autoridade de producao.',
    'only run full production after meaningful guarded gains':
      'Rodar producao completa apenas apos ganhos protegidos relevantes.',
    'only then create a repair shadow policy':
      'So entao criar uma politica shadow de reparo.',
    'prefer title_policy_microagent and culture_title_labels for narrow cases':
      'Preferir title_policy_microagent e culture_title_labels para casos estreitos.',
    'promote only same-token repairs with clean shadow behavior':
      'Promover apenas reparos same-token com comportamento shadow limpo.',
    'promote only when candidate improves coverage without false-safe regression':
      'Promover apenas quando o candidato melhorar cobertura sem regressao de falso-seguro.',
    'require noun agreement evidence before rewrite':
      'Exigir evidencia de concordancia nominal antes da reescrita.',
    'review 240-item repair queue':
      'Revisar fila de reparo com 240 itens.',
    'sample-review reflexive pairs':
      'Revisar amostra de pares reflexivos.',
    'separate operational authority from evidence-only status in visualization':
      'Separar autoridade operacional de status apenas-evidencia na visualizacao.',
    'separate pronoun, article and adjective-ending subpatterns':
      'Separar subpadroes de pronome, artigo e terminacao adjetiva.',
    'separate tense/current-state subpatterns':
      'Separar subpadroes de tempo verbal e estado atual.',
    'show authority badges: decision_authorized, evidence_only, planned_only':
      'Mostrar selos de autoridade: decisao autorizada, apenas evidencia e apenas planejado.',
    'split into short-label semantic pairs, event surface, religion/culture/title semantic families':
      'Dividir em pares semanticos de label curto, superficie de evento e familias semanticas de religiao/cultura/titulos.',
    'split local-player Spanish literal payload into smaller correction agents':
      'Dividir payload literal espanhol de local-player em agentes de correcao menores.',
    'then attack ES_OA/ES_ElLa/ES_DelDela custom helpers':
      'Depois atacar helpers custom ES_OA/ES_ElLa/ES_DelDela.',
    'train more targeted evidence':
      'Treinar evidencias mais direcionadas.',
    'use as stable baseline':
      'Usar como baseline estavel.',
    'use coverage as queue source':
      'Usar cobertura como fonte de fila.',
    'use feedback from religion_semantic_microagent':
      'Usar feedback do religion_semantic_microagent.',
    'use recheck queue before lifecycle closure':
      'Usar fila de rechecagem antes do fechamento lifecycle.',
  };
  if (translations[text]) return translations[text];
  return text
    .replace(/^Read-only catalog policy (.+) from (.+)\.$/i, 'Política de catálogo somente leitura $1, derivada de $2.')
    .replace(/^Read-only allowlist for exact (.+) correction\.$/i, 'Lista de permissão somente leitura para correção exata de $1.')
    .replace(/^Read-only terminal guard for (.+)$/i, 'Proteção terminal somente leitura para $1')
    .replace(/^Read-only reuse splitter for (.+)$/i, 'Divisor de reutilização somente leitura para $1')
    .replace(/^Coordinates /, 'Coordena ')
    .replace(/^Aggregates /, 'Agrega ')
    .replace(/^Audits /, 'Audita ')
    .replace(/^Builds /, 'Constrói ')
    .replace(/^Classifies /, 'Classifica ')
    .replace(/^Detects /, 'Detecta ')
    .replace(/^Extracts /, 'Extrai ')
    .replace(/^Explains /, 'Explica ')
    .replace(/^Guards /, 'Protege ')
    .replace(/^Handles /, 'Trata ')
    .replace(/^Observes /, 'Observa ')
    .replace(/^Routes /, 'Roteia ')
    .replace(/^Splits /, 'Separa ')
    .replace(/^Validates /, 'Valida ')
    .replace(/^Votes on /, 'Avalia ')
    .replaceAll('Composition overlay', 'Camada de composição')
    .replaceAll('Coordinator overlay', 'Camada coordenadora')
    .replaceAll('Final Select_CString shadow coordinator', 'Coordenador final Select_CString em observação')
    .replaceAll('Shadow-only governed bridge proposal', 'Proposta de ponte governada somente em observação')
    .replaceAll('Learns ', 'Aprende ')
    .replaceAll('learns ', 'aprende ')
    .replaceAll('second-person', 'segunda pessoa')
    .replaceAll('third-person', 'terceira pessoa')
    .replaceAll('reflexive branch shifts', 'mudancas de ramo reflexivo')
    .replaceAll('branch shifts', 'mudancas de ramo')
    .replaceAll('local-player phrase', 'frase do jogador local')
    .replaceAll('third-person phrase', 'frase em terceira pessoa')
    .replaceAll('before PT-BR rewrite', 'antes da reescrita PT-BR')
    .replaceAll('read-only', 'somente leitura')
    .replaceAll('dry-run', 'simulação')
    .replaceAll('shadow', 'observação')
    .replaceAll('terminal guards', 'proteções terminais')
    .replaceAll('terminal guard', 'proteção terminal')
    .replaceAll('splitters', 'divisores')
    .replaceAll('splitter', 'divisor')
    .replaceAll('without apply or lifecycle authority', 'sem autoridade de aplicação ou ciclo de vida')
    .replaceAll('without apply or lifecycle', 'sem aplicação ou ciclo de vida')
    .replaceAll('evidence', 'evidência')
    .replaceAll('candidates', 'candidatos')
    .replaceAll('candidate', 'candidato')
    .replaceAll('outputs', 'saídas')
    .replaceAll('inputs', 'entradas')
    .replaceAll('routing', 'roteamento')
    .replaceAll('route', 'rotear')
    .replaceAll('checkpoint', 'checkpoint')
    .replaceAll('lifecycle', 'ciclo de vida')
    .replaceAll('production', 'produção')
    .replaceAll('release', 'liberação')
    .replaceAll('review', 'revisão')
    .replaceAll('policy', 'política')
    .replaceAll('policies', 'políticas')
    .replace(/\bsegments\b/g, 'segmentos')
    .replace(/\bsegment\b/g, 'segmento')
    .replaceAll('token changes', 'alterações de token')
    .replaceAll('token change', 'alteração de token')
    .replace(/\bblocked\b/g, 'bloqueado')
    .replace(/\bsafe\b/g, 'seguro')
    .replace(/\bcurrent\b/g, 'atual')
    .replace(/\bbefore\b/g, 'antes de')
    .replace(/\bafter\b/g, 'depois de');
};

const ptListItem = (value) => {
  const text = String(value).replaceAll('_', ' ');
  const key = text.toLowerCase();
  const translations = {
    'active score run': 'run de score ativo',
    'agent recommendations': 'recomendacoes dos agentes',
    'agent registry': 'registro de agentes',
    'agent proposals': 'propostas dos agentes',
    'approved repair candidates': 'candidatos de reparo aprovados',
    'approved confirmations': 'confirmacoes aprovadas',
    'balanced training strategy': 'estrategia de treino balanceado',
    'baronies/counties/adjectives': 'baronias/condados/adjetivos',
    'blocked structure': 'estrutura bloqueada',
    'blockers': 'bloqueios',
    'boundary recommendations': 'recomendacoes de boundary',
    'boundary repair queues': 'filas de reparo de boundary',
    'candidate metrics': 'metricas do candidato',
    'checkpoint candidates': 'candidatos em checkpoint',
    'checkpoint allowlists': 'allowlists de checkpoint',
    '9 terminal dry-run policies': '9 politicas terminais em dry-run',
    '9 shadow splitters': '9 splitters shadow',
    'auditable routing evidence': 'evidencia de roteamento auditavel',
    'issue ledger': 'ledger de problemas',
    'requirement/effect candidates': 'candidatos de requisito/efeito',
    'dynamic ck3 expressions': 'expressoes dinamicas CK3',
    'closed auto-confirmed states': 'estados fechados por autoconfirmacao',
    'closed count': 'contagem fechada',
    'composite repair candidates': 'candidatos de reparo composto',
    'composition candidates': 'candidatos de composicao',
    'composition impact': 'impacto da composicao',
    'composition queues': 'filas de composicao',
    'confirmations': 'confirmacoes',
    'confirmed output': 'output confirmado',
    'confirmed text': 'texto confirmado',
    'confirmation evidence': 'evidencia de confirmacao',
    'context-aware rewrite candidates': 'candidatos de reescrita com contexto',
    'controlled token policies': 'politicas controladas de token',
    'culture title labels': 'labels de titulo cultural',
    'culture title paths': 'caminhos de titulo cultural',
    'culture-title decisions': 'decisoes de titulos culturais',
    'custom localization helper families': 'familias de helpers custom de localizacao',
    'custom localization composition review decision run 111': 'decisão de revisão da composição personalizada · execução 111',
    'regional custom loc dog type labels': 'rótulos regionais de raças em localização personalizada',
    'english/spanish preservation comparison': 'comparação de preservação entre inglês e espanhol',
    'native breed/name preservation candidates': 'candidatos à preservação de nomes nativos de raças',
    'morphology review blockers': 'bloqueios da revisão morfológica',
    'regional custom loc flower type labels': 'rótulos regionais de flores em localização personalizada',
    'latest segment confirmations': 'confirmações mais recentes dos segmentos',
    'high-confidence flower lexical repair checkpoint': 'ponto de controle de reparos florais com alta confiança',
    'domain-review flower blockers': 'bloqueios florais para revisão de domínio',
    'dashboard groups': 'grupos do dashboard',
    'dataset run 426': 'dataset run 426',
    'deterministic guard features': 'features das travas deterministicas',
    'dry-run reports': 'relatorios de dry-run',
    'dynamic pattern shadow decisions': 'decisoes shadow de padrao dinamico',
    'dynamic select cstring repair queue': 'fila de reparo Select_CString dinamico',
    'dynamic expression candidates': 'candidatos de expressao dinamica',
    'dynamic token ledger': 'historico de tokens dinamicos',
    'english/spanish/old/output derived features': 'features derivadas de ingles/espanhol/old/output',
    'english reference': 'referencia em ingles',
    'english reference signals': 'sinais da referencia em ingles',
    'es oa/es xa/es ella helpers': 'helpers ES_OA/ES_XA/ES_ElLa',
    'final mod output': 'output final do mod',
    'final safe output': 'output final seguro',
    'future shadow repair candidates': 'futuros candidatos shadow de reparo',
    'gender boundary checkpoints': 'checkpoints de boundary de genero',
    'gender token issue ledger': 'historico de problemas de tokens de genero',
    'gender token queue': 'fila de tokens de genero',
    'governed bridge candidates': 'candidatos de ponte governada',
    'guarded checkpoint candidates': 'candidatos de checkpoint protegido',
    'guarded lifecycle states': 'estados lifecycle protegidos',
    'guarded output candidates': 'candidatos protegidos de output',
    'hard structural violations': 'violacoes estruturais graves',
    'health summary': 'resumo de saude',
    'human feedback': 'feedback humano',
    'human and issue-review bridge evidence': 'evidencia humana e ponte de revisao de problemas',
    'human locked': 'travado por humano',
    'human reviewed title evidence': 'evidencia de titulos revisada por humano',
    'issue families': 'familias de problemas',
    'learned patterns': 'padroes aprendidos',
    'lifecycle closure evidence': 'evidencia de fechamento lifecycle',
    'lifecycle policies': 'politicas de lifecycle',
    'lifecycle state': 'estado lifecycle',
    'literal subtype audit': 'auditoria de subtipo literal',
    'local-player branch pairs': 'pares de ramos local-player',
    'locked decisions': 'decisoes travadas',
    'locked human overrides': 'sobrescritas humanas travadas',
    'long-text structural split': 'divisao estrutural de texto longo',
    'low-token texts': 'textos com poucos tokens',
    'macro scores': 'scores do modelo macro',
    'microagent detections': 'deteccoes dos microagentes',
    'microagent training targets': 'alvos de treino dos microagentes',
    'model risk signals': 'sinais de risco do modelo',
    'needs apply': 'precisa aplicar',
    'nickname and title policy evidence': 'evidencia de politica de apelidos e titulos',
    'old trusted text': 'texto confiavel antigo',
    'old/output divergence': 'divergencia entre old e output',
    'operational states': 'estados operacionais',
    'output/spanish writes': 'escritas em output/spanish',
    'output text': 'texto do output',
    'partial repairs': 'reparos parciais',
    'partial coverage': 'cobertura parcial',
    'pending issue families': 'familias de pendencias',
    'pending queues': 'filas pendentes',
    'pending count': 'contagem pendente',
    'policy decisions': 'decisoes de politica',
    'possessive pronoun evidence': 'evidencia de pronome possessivo',
    'post-write segment state': 'estado do segmento apos escrita',
    'preterite verb evidence': 'evidencia de verbo no preterito',
    'production report': 'relatorio de producao',
    'production reports': 'relatorios de producao',
    'promotion candidates': 'candidatos de promocao',
    'promotion comparison': 'comparacao de promocao',
    'promotion guidance': 'orientacao de promocao',
    'recheck queue': 'fila de rechecagem',
    'reflexive phrase evidence': 'evidencia de frase reflexiva',
    'religion candidates': 'candidatos de religiao',
    'religion domain decision': 'decisao do dominio de religiao',
    'religion keys': 'chaves de religiao',
    'religion paths': 'caminhos de religiao',
    'remaining specialist blockers': 'bloqueios restantes de especialistas',
    'reopen lifecycle policies': 'politicas lifecycle de reabertura',
    'repair queues': 'filas de reparo',
    'residual spanish flags': 'marcadores de espanhol residual',
    'review decisions': 'decisoes de revisao',
    'reviewed safe-short-label decisions': 'decisoes revisadas de labels curtos seguros',
    'risk class': 'classe de risco',
    'routing candidates': 'candidatos de roteamento',
    'routing decisions': 'decisoes de roteamento',
    'routing evidence': 'evidencia de roteamento',
    'routing run': 'run de roteamento',
    'safe probability': 'probabilidade segura',
    'safe-pattern candidates': 'candidatos de padrao seguro',
    'safe/blocked gender observations': 'observacoes de genero seguras/bloqueadas',
    'safe to score': 'liberado para score',
    'same-token repair candidates': 'candidatos de reparo same-token',
    'score runs': 'runs de score',
    'select cstring gender branches': 'ramos de genero Select_CString',
    'select cstring pattern audit': 'auditoria de padrao Select_CString',
    'semantic context': 'contexto semantico',
    'semantic evidence': 'evidencia semantica',
    'semantic queues': 'filas semanticas',
    'semantic subchecks': 'subchecagens semanticas',
    'segment lifecycle state': 'estado lifecycle do segmento',
    'segment features': 'features do segmento',
    'segment state': 'estado do segmento',
    'segment state pending items': 'itens pendentes do segment-state',
    'semantic candidates': 'candidatos semanticos',
    'shadow policy results': 'resultados de politica shadow',
    'short label candidates': 'candidatos de label curto',
    'short label releases': 'releases de labels curtos',
    'short labels': 'labels curtos',
    'source segments': 'segmentos fonte',
    'source segment metadata': 'metadados do segmento fonte',
    'specialist recommendations': 'recomendacoes dos especialistas',
    'specialist policies': 'politicas dos especialistas',
    'spanish source': 'fonte em espanhol',
    'style boundaries': 'boundaries de estilo',
    'subagent split recommendations': 'recomendacoes de divisao em subagentes',
    'subspecialist votes': 'votos dos subespecialistas',
    'structural safe state': 'estado estrutural seguro',
    'title evidence': 'evidencia de titulos',
    'title label keys': 'chaves de labels de titulo',
    'title paths': 'caminhos de titulos',
    'token extraction': 'extracao de tokens',
    'token policies': 'politicas de token',
    'token policy reports': 'relatorios de politica de token',
    'token-policy decisions': 'decisoes de politica de token',
    'token-policy repair candidates': 'candidatos de reparo por politica de token',
    'token-policy review queues': 'filas de revisao de politica de token',
    'token-policy subchecks': 'subchecagens de politica de token',
    'token policy review': 'revisao de politica de token',
    'topology nodes': 'nos da topologia',
    'training examples': 'exemplos de treino',
    'training dataset run 38': 'dataset de treino run 38',
    'trusted memory': 'memoria confiavel',
    'valid blank': 'vazio valido',
    'validators': 'validadores',
    'watch count': 'contagem em watch',
    'watch states': 'estados em watch',
  };
  return translations[key] ?? ptIdentifierLabel(value);
};

const ptMetricValue = (value) => {
  if (typeof value === 'boolean') return value ? 'sim' : 'não';
  if (typeof value === 'number') return metric(value);
  if (Array.isArray(value)) return value.map(ptMetricValue).join(', ');
  const text = String(value ?? '');
  const status = ptStatus(text);
  if (status !== text) return status;
  const sentence = ptSentence(text);
  return sentence !== text ? sentence : ptIdentifierLabel(text);
};

const summarizeMetrics = (metrics) => {
  if (!metrics) return [];
  if (Array.isArray(metrics)) return metrics.slice(0, 6).map(ptMetricValue);
  return Object.entries(metrics)
    .slice(0, 6)
    .map(([key, value]) => `${ptFieldLabel(key)}: ${ptMetricValue(value)}`);
};

const asList = (value) => {
  if (!value) return [];
  return Array.isArray(value) ? value.map(String) : [String(value)];
};

const ATLAS_LAYOUT_STORAGE_KEY = 'ck3_ptbr_neural_atlas_layout_v1';
const DASHBOARD_THEME_STORAGE_KEY = 'ck3_ptbr_dashboard_theme';
const ATLAS_DEFAULT_NODE_SIZE = { w: 168, h: 54 };

const ptNodeLabels = {
  deterministic_guards: 'Proteções determinísticas',
  translation_memory: 'Memória de tradução',
  macro_risk_model_active: 'Modelo macro v0038',
  macro_risk_model_latest: 'Candidato macro v0427',
  coordinator_ensemble_v1: 'Coordenação v1',
  agent_registry: 'Registro de agentes',
  religion_specialist: 'Especialista em religião',
  titles_specialist_legacy: 'Títulos legados',
  culture_title_labels: 'Títulos culturais',
  issue_ledger: 'Registro de problemas',
  micro_short_label_style: 'Estilo de rótulos curtos',
  micro_custom_localization_fragment: 'Fragmentos personalizados',
  custom_localization_composition_auditor: 'Auditoria de composição',
  micro_ptbr_flower_lexicon: 'Léxico floral PT-BR',
  micro_native_dog_type_preservation: 'Preservação de raças',
  micro_short_label_bold_no_repair: 'Reparo de “Não” em negrito',
  micro_dynamic_ck3_expression: 'Expressões dinâmicas CK3',
  select_cstring_local_player_preterite_verb_rewrite: 'Select_CString: verbo no pretérito',
  select_cstring_local_player_reflexive_phrase_rewrite: 'Select_CString: frase reflexiva',
  select_cstring_local_player_possessive_pronoun_rewrite: 'Select_CString: pronome possessivo',
  micro_gender_token: 'Tokens de gênero',
  micro_spanish_residual: 'Resíduo em espanhol',
  micro_autofix_unknown_router: 'Roteador de correção automática',
  micro_long_text_composer: 'Compositor de textos longos',
  micro_semantic_review_router: 'Roteador de revisão semântica',
  lifecycle_shadow_checkpoint: 'Ciclo de observação e controle',
  segment_state: 'Estado dos segmentos',
  production_runner: 'Executor de produção',
  requirement_effect_router_readonly: 'Roteador de requisitos e efeitos',
  effect_list_package: 'Pacote de lista de efeitos',
  artifact_activity_effect_policy: 'Política de artefatos e atividades',
};

const compactNodeLabel = (node) => {
  const localized = ptNodeLabels[node?.id] ?? ptIdentifierLabel(node?.label ?? node?.id ?? '');
  return localized.charAt(0).toUpperCase() + localized.slice(1);
};

const fallbackPosition = (node, index, siblingCount) => {
  const x = atlasFamilyColumns[node.family] ?? atlasFamilyColumns[node.type] ?? 50;
  const spread = Math.min(72, Math.max(28, siblingCount * 13));
  const start = 50 - spread / 2;
  return { x, y: Math.max(12, Math.min(90, start + index * 13)) };
};

const compactAgentLabel = (agent) => {
  const key = agent?.agent_key ?? '';
  const labels = {
    requirement_effect_router_readonly: 'Roteador de requisitos e efeitos',
    not_requirement_effect_global_router: 'Roteador fora de requisito',
    not_requirement_effect_culture_religion_router: 'Cultura e religião',
    not_requirement_effect_culture_policy: 'Política de cultura',
    not_requirement_effect_culture_tradition_heritage_policy: 'Tradição e herança',
    effect_list_multiline_policy: 'Lista de efeitos multilinha',
    effect_list_artifact_activity_policy: 'Efeitos de artefatos e atividades',
    effect_list_gender_local_player_policy: 'Efeitos de gênero do jogador',
    effect_list_trait_accolade_policy: 'Efeitos de traços e condecorações',
    effect_list_script_value_policy: 'Efeitos de ScriptValue',
    effect_list_concept_policy: 'Efeitos de conceitos',
    artifact_activity_gender_local_player_policy: 'Gênero do jogador em artefatos',
    artifact_activity_script_value_policy: 'ScriptValue em artefatos',
    artifact_item_effect_policy: 'Efeitos de itens de artefato',
    artifact_item_scope_getter_policy: 'Escopo de itens de artefato',
    acclaimed_knight_entity_unlock_final_policy: 'Desbloqueio de cavaleiro aclamado',
    select_cstring_player_target_direct_policy: 'Select_CString: jogador e alvo',
    select_cstring_possessive_policy: 'Select_CString: possessivos',
    select_cstring_es_helper_policy: 'Select_CString: auxiliares ES',
    local_player_requirement_policy: 'Requisito do jogador local',
    es_oa_requirement_policy: 'Requisito ES_OA',
    script_value_requirement_policy: 'Requisito ScriptValue',
    concept_requirement_policy: 'Requisito de conceito',
    name_nickname_requirement_guard: 'Proteção de nomes e apelidos',
    select_cstring_same_token_lifecycle_policy: 'Ciclo Select_CString com mesmo token',
    select_cstring_same_token_composition_overlay: 'Composição Select_CString com mesmo token',
    select_cstring_multistage_composition_overlay: 'Composição Select_CString em várias etapas',
    select_cstring_final_composition_overlay: 'Composição final Select_CString',
    select_cstring_final_composition_maturity_auditor: 'Auditoria da composição Select_CString',
    select_cstring_governed_bridge_proposal: 'Ponte governada Select_CString',
  };
  if (labels[key]) return labels[key];
  const localized = ptIdentifierLabel(
    key
      .replace(/^micro_/, '')
      .replace(/_readonly$/, '')
      .replace(/_policy$/, '')
      .replace(/_microagent$/, '')
  );
  return localized.charAt(0).toUpperCase() + localized.slice(1);
};

const ptAgentDescription = (agent) => {
  const descriptions = {
    coordinator_ensemble_v1:
      'Coordena a pontuação geral, os votos dos especialistas, o aprendizado humano e as políticas de segurança.',
    composition_coordinator_v1:
      'Coordena segmentos com várias famílias de problemas abertas, mede quando os microagentes existentes podem fechá-los em conjunto e identifica a família que ainda bloqueia o fechamento.',
    select_cstring_same_token_lifecycle_policy:
      'Agrega reparos maduros de Select_CString em observação que preservam a estrutura normalizada de tokens do CK3, bloqueia resíduos em espanhol e conteúdos que alteram tokens e mantém desativada a liberação para produção.',
    select_cstring_same_token_composition_overlay:
      'Combina liberações de mesmo token do ciclo de vida com pontos de controle da segunda camada de limpeza, medindo o ganho agregado antes de qualquer ponte para produção.',
    select_cstring_multistage_composition_overlay:
      'Mede a cobertura conjunta em observação do ciclo de vida de mesmo token, da limpeza de resíduos e da validação de conteúdos literais dinâmicos.',
    select_cstring_final_composition_overlay:
      'Coordena a composição final de Select_CString, reunindo ciclo de vida, limpeza de resíduos, conteúdo literal dinâmico e pronomes do jogador local.',
    select_cstring_final_composition_maturity_auditor:
      'Audita a composição final de Select_CString em busca de segmentos duplicados, referências de origem inválidas, fontes inesperadas, bloqueios pendentes e prontidão mecânica da ponte.',
    select_cstring_governed_bridge_proposal:
      'Propõe uma ponte governada, somente em observação, para a cadeia final de composição Select_CString. Materializa candidatos e proteções necessárias, sem promover confirmações nem escrever na saída.',
    long_text_repair_router:
      'Classifica falhas revisadas de composição em textos longos, cria rotas de reparo reutilizáveis e separa reparos seguros de alterações estruturais de tokens.',
  };
  if (descriptions[agent?.agent_key]) return descriptions[agent.agent_key];

  const role = String(ptRole(agent?.decision_role ?? agent?.role ?? '')).replace(/[.]$/, '');
  const type = ptType(agent?.agent_type ?? 'agente');
  const scopeSource = agent?.scope_group ?? agent?.dashboard_group ?? agent?.parent_agent_key ?? '';
  const scope = scopeSource ? ptIdentifierLabel(scopeSource) : '';
  return [
    role ? `Função: ${role}.` : '',
    scope ? `Escopo: ${scope}.` : '',
    type ? `Tipo: ${type}.` : '',
  ].filter(Boolean).join(' ');
};

const agentMatchesNode = (agent, node) => {
  const id = node.id ?? '';
  const key = agent.agent_key ?? '';
  const parent = agent.parent_agent_key ?? '';
  const group = agent.dashboard_group ?? '';
  const type = agent.agent_type ?? '';
  const scope = agent.scope_group ?? '';
  const haystack = `${key} ${parent} ${group} ${type} ${scope}`.toLowerCase();

  if (id === 'artifact_activity_effect_policy') {
    return key === 'artifact_activity_effect_policy' || parent === 'artifact_activity_effect_policy';
  }
  if (id === 'effect_list_package') {
    return key !== 'artifact_activity_effect_policy' && (
      haystack.includes('effect_list') ||
      key.startsWith('artifact_activity_') ||
      key.startsWith('artifact_item_') ||
      parent.startsWith('effect_list_') ||
      parent.startsWith('artifact_item_')
    );
  }
  if (id === 'requirement_effect_router_readonly') {
    return key === 'requirement_effect_router_readonly' ||
      parent === 'requirement_effect_router_readonly' ||
      (scope === 'requirement_effect_router' && !haystack.includes('effect_list'));
  }
  if (id === 'agent_registry') return false;
  if (id === 'deterministic_guards') return type.includes('guard') || group === 'Safety';
  if (id === 'macro_risk_model_active' || id === 'macro_risk_model_latest') return group === 'Macro' || key.includes('macro');
  if (id === 'coordinator_ensemble_v1') return group === 'Coordinator' || type.includes('coordinator') || key.includes('coordinator');
  if (id === 'religion_specialist') return group === 'Religion' || haystack.includes('religion');
  if (id === 'titles_specialist_legacy') return group === 'Titles' || haystack.includes('title') || haystack.includes('nickname');
  if (id === 'culture_title_labels') return haystack.includes('culture') || key.includes('culture_title');
  if (id === 'issue_ledger') return group === 'Issue Network' && !parent;
  if (id === 'lifecycle_shadow_checkpoint') return group === 'Lifecycle' || haystack.includes('lifecycle') || haystack.includes('shadow');
  if (id === 'micro_short_label_style') return haystack.includes('short_label');
  if (id === 'micro_dynamic_ck3_expression') return haystack.includes('dynamic') || haystack.includes('script_value') || haystack.includes('concept');
  if (id === 'micro_gender_token') return haystack.includes('gender') || haystack.includes('es_oa') || haystack.includes('es_el') || haystack.includes('es_del');
  if (id === 'micro_spanish_residual') return haystack.includes('spanish') || haystack.includes('residual');
  if (id === 'micro_autofix_unknown_router') return haystack.includes('autofix_unknown');
  if (id === 'micro_semantic_review_router') return haystack.includes('semantic');
  if (id === 'micro_long_text_composer') return haystack.includes('long_text') || haystack.includes('composition');
  if (id.includes('custom_localization')) return haystack.includes('custom_localization');
  if (id.includes('select_cstring')) return haystack.includes('select_cstring');
  return false;
};

const summarizeNodeAgents = (agents, node) => {
  const matched = agents.filter((agent) => agentMatchesNode(agent, node));
  const byState = matched.reduce((acc, agent) => {
    const state = agent.operational_state ?? 'unknown';
    acc[state] = (acc[state] ?? 0) + 1;
    return acc;
  }, {});
  const byType = matched.reduce((acc, agent) => {
    const type = agent.agent_type ?? 'unknown';
    acc[type] = (acc[type] ?? 0) + 1;
    return acc;
  }, {});
  const operational = matched.filter((agent) => (
    agent.status === 'active' && ['authoritative', 'operational', 'dry_run'].includes(agent.operational_state)
  )).length;
  const shadow = matched.filter((agent) => agent.operational_state === 'shadow').length;
  return {
    agents: matched,
    total: matched.length,
    operational,
    shadow,
    byState,
    byType,
  };
};

const normalizeNeuralAtlas = (source) => {
  const nodes = Array.isArray(source?.nodes) ? source.nodes : [];
  const groups = nodes.reduce((acc, node) => {
    const key = node.family ?? node.type ?? 'network';
    acc[key] = (acc[key] ?? 0) + 1;
    return acc;
  }, {});
  const seen = {};
  const normalizedNodes = nodes.map((node) => {
    const key = node.family ?? node.type ?? 'network';
    const groupIndex = seen[key] ?? 0;
    seen[key] = groupIndex + 1;
    const fixed = atlasPositionById[node.id];
    const fallback = fallbackPosition(node, groupIndex, groups[key]);
    return {
      ...node,
      id: node.id,
      rawLabel: node.label ?? node.id,
      label: compactNodeLabel(node),
      displayLabel: compactNodeLabel(node),
      type: node.type ?? 'node',
      family: node.family ?? 'network',
      status: node.status ?? 'active',
      role: ptRole(node.role ?? ''),
      rawRole: node.role ?? '',
      description: ptSentence(node.description ?? ''),
      x: fixed?.x ?? node.x ?? fallback.x,
      y: fixed?.y ?? node.y ?? fallback.y,
      icon: atlasIconByType[node.type] ?? BrainCircuit,
      tone: node.tone ?? atlasToneByStatus[node.status] ?? 'blue',
      palette: atlasPaletteForNode(node),
      metrics: summarizeMetrics(node.metrics),
      inputs: asList(node.inputs).map(ptListItem),
      outputs: asList(node.outputs).map(ptListItem),
      risks: asList(node.risks).map(ptSentence),
      next_steps: asList(node.next_steps ?? node.next).map(ptSentence),
    };
  });
  const normalizedEdges = (Array.isArray(source?.edges) ? source.edges : []).map((edge) => {
    if (Array.isArray(edge)) {
      return { source: edge[0], target: edge[1], label: edge[2] ?? '', kind: edge[3] ?? '', strength: edge[4] ?? '' };
    }
    return {
      source: edge.source,
      target: edge.target,
      label: edge.label ?? '',
      kind: edge.kind ?? '',
      strength: edge.strength ?? '',
    };
  });
  return {
    nodes: normalizedNodes,
    edges: normalizedEdges.filter((edge) => edge.source && edge.target),
    versionInfo: source?.version_info ?? {},
    notes: source?.visualization_notes ?? {},
    generatedAt: source?.generated_at,
    sourcePath: source?.sourcePath,
    versionKey: source?.generated_at ?? `${normalizedNodes.length}-${normalizedEdges.length}`,
  };
};

function NeuralArchitecture({ data }) {
  const canvasRef = useRef(null);
  const agents = data.agents ?? {};
  const dashboardSummary = agents.summary ?? {};
  const [networkSource, setNetworkSource] = useState(null);
  const [networkError, setNetworkError] = useState(null);
  const [networkLoading, setNetworkLoading] = useState(true);
  const [networkLoadAttempt, setNetworkLoadAttempt] = useState(0);
  const sourceNetwork = networkSource ?? { nodes: [], edges: [], version_info: {} };
  const versionInfo = sourceNetwork.version_info ?? {};
  const inventory = versionInfo.agent_inventory ?? {};
  const classificationCounts = inventory.classification_counts ?? {};
  const registrySummary = sourceNetwork.registrySummary ?? {};
  const routerComponent = registrySummary.requirement_effect_router ?? {};
  const effectListComponent = registrySummary.effect_list_package ?? {};
  const notReqComponent = registrySummary.not_requirement_effect_package ?? {};
  const registryAgents = registrySummary.registry_agents ?? [];
  const artifactActivityComponent = registryAgents.find((agent) => agent.agent_key === 'artifact_activity_effect_policy') ?? null;
  const enhancedNetwork = useMemo(() => {
    const baseNodes = sourceNetwork.nodes ?? [];
    const baseEdges = sourceNetwork.edges ?? [];
    const nodes = [...baseNodes];
    const edges = [...baseEdges];
    if (routerComponent.registered && !nodes.some((node) => node.id === 'requirement_effect_router_readonly')) {
      nodes.push(
        {
          id: 'requirement_effect_router_readonly',
          label: 'Requirement/Effect Router',
          type: 'subcoordinator',
          family: 'subagents',
          status: 'operational',
          role: 'route_and_arbitrate',
          description: 'Roteador somente leitura para superfícies de requisito e efeito. A maturação dessas regras chegou a 10.159 segmentos com especificação; considerando também os demais casos, a cobertura chega a 11.407 de 11.725 segmentos.',
          inputs: ['registro de problemas', 'candidatos de requisito e efeito', 'expressões dinâmicas do CK3'],
          outputs: ['políticas terminais em simulação', 'divisores em observação', 'evidência de roteamento auditável'],
          metrics: {
            operational_state: routerComponent.operational_state ?? 'dry_run',
            requirement_effect_agents: routerComponent.requirement_effect_agents ?? 29,
            spec_after_requirement_effect: registrySummary.spec_associated_after_requirement_effect_maturation ?? 10159,
            spec_after_not_requirement_effect: registrySummary.spec_associated_after_not_requirement_effect ?? 11407,
            segments_without_useful_spec: registrySummary.segments_without_useful_spec ?? 318,
            terminal_policies: routerComponent.terminal_policies ?? 9,
            splitters: routerComponent.splitters ?? 9,
            effect_list_registered: effectListComponent.registered ? 1 : 0,
            auto_apply_allowed: 0,
            production_release_allowed: 0,
          },
          risks: ['Opera somente em simulação; não possui autoridade sobre a saída.', 'Os divisores em observação roteiam o contexto, mas não fecham segmentos.'],
          next_steps: ['Monitorar a evidência das políticas terminais.', 'Promover somente depois da validação governada.'],
        },
      );
      edges.push(
        {
          source: 'issue_ledger',
          target: 'requirement_effect_router_readonly',
          label: 'rota do backlog de requisitos e efeitos',
          kind: 'issue_route',
          strength: 'medium',
        },
        {
          source: 'requirement_effect_router_readonly',
          target: 'lifecycle_shadow_checkpoint',
          label: 'proteções terminais em simulação e divisores em observação',
          kind: 'read_only_checkpoint',
          strength: 'medium',
        },
      );
    }
    if (effectListComponent.registered && !nodes.some((node) => node.id === 'effect_list_package')) {
      nodes.push(
        {
          id: 'effect_list_package',
          label: 'Effect-list Package',
          type: 'subcoordinator',
          family: 'subagents',
          status: 'operational',
          role: 'route_and_split',
          description: 'Pacote somente leitura para listas de efeitos: organiza especificações, políticas terminais e divisores sem aplicar alterações nem avançar o ciclo de vida.',
          inputs: ['roteador de requisitos e efeitos', 'listas de efeitos', 'registro de problemas'],
          outputs: ['10 especificações', '6 políticas terminais', '4 divisores', '1.090 terminais registrados'],
          metrics: {
            agents: effectListComponent.agents_count ?? 11,
            specs: effectListComponent.specs ?? 10,
            terminal_policies: effectListComponent.terminal_policies ?? 6,
            splitters: effectListComponent.splitters ?? 4,
            route_count: effectListComponent.route_count ?? 1654,
            with_spec: effectListComponent.with_spec ?? 1272,
            terminal_registered: effectListComponent.terminal_registered ?? 1090,
            coverage_gain: effectListComponent.coverage_gain ?? 1654,
            segments_without_useful_spec: effectListComponent.segments_without_useful_spec ?? 5941,
          },
          risks: ['Ainda há subrotas sem especificação útil.', 'Não aplica alterações nem avança o ciclo de vida; produz apenas roteamento e evidência.'],
          next_steps: ['Instrumentar artefatos e atividades.', 'Instrumentar modificadores de construções.', 'Promover especificações somente depois da validação governada.'],
        },
      );
      edges.push(
        {
          source: 'requirement_effect_router_readonly',
          target: 'effect_list_package',
          label: 'rota da lista de efeitos',
          kind: 'read_only_route',
          strength: 'strong',
        },
        {
          source: 'effect_list_package',
          target: 'issue_ledger',
          label: 'lacunas de especificação e bloqueios',
          kind: 'evidence_feedback',
          strength: 'medium',
        },
        {
          source: 'effect_list_package',
          target: 'micro_dynamic_ck3_expression',
          label: 'especificações de ScriptValue e conceitos',
          kind: 'spec_route',
          strength: 'medium',
        },
        {
          source: 'effect_list_package',
          target: 'select_cstring_local_player_preterite_verb_rewrite',
          label: 'proteções de efeitos do jogador local',
          kind: 'spec_route',
          strength: 'low',
        },
      );
    }
    if (artifactActivityComponent && !nodes.some((node) => node.id === 'artifact_activity_effect_policy')) {
      nodes.push(
        {
          id: 'artifact_activity_effect_policy',
          label: 'Artifact/Activity',
          type: 'subcoordinator',
          family: 'subagents',
          status: 'shadow',
          role: 'route_and_split',
          description: 'Subcoordenador em observação para efeitos de artefatos e atividades. Registra o gargalo na arquitetura, mas ainda não aplica alterações nem fecha o ciclo de vida.',
          inputs: ['roteador de requisitos e efeitos', 'candidatos de efeitos de artefatos e atividades', 'catálogo de listas de efeitos'],
          outputs: ['rotas de artefatos e atividades', 'reutilização de políticas da lista de efeitos', 'evidência para novas especificações'],
          metrics: {
            operational_state: artifactActivityComponent.operational_state ?? 'shadow',
            review_total: 240,
            reuse_cataloged_policies: 227,
            auto_apply_allowed: 0,
            lifecycle_allowed: 0,
          },
          risks: ['Está em observação e não deve ser contabilizado como operacional.', 'Ainda é necessário medir a cobertura após o registro.'],
          next_steps: ['Executar diagnóstico global após o registro.', 'Criar especificações terminais quando houver evidência suficiente.'],
        },
      );
      edges.push(
        {
          source: 'requirement_effect_router_readonly',
          target: 'artifact_activity_effect_policy',
          label: 'rota de efeitos de artefatos e atividades',
          kind: 'shadow_route',
          strength: 'medium',
        },
        {
          source: 'artifact_activity_effect_policy',
          target: 'effect_list_package',
          label: 'reutilização de políticas catalogadas',
          kind: 'catalog_reuse',
          strength: 'medium',
        },
        {
          source: 'artifact_activity_effect_policy',
          target: 'issue_ledger',
          label: 'evidência de cobertura',
          kind: 'evidence_feedback',
          strength: 'low',
        },
      );
    }
    if (nodes === baseNodes && edges === baseEdges) {
      return sourceNetwork;
    }
    return {
      ...sourceNetwork,
      nodes,
      edges,
    };
  }, [
    sourceNetwork,
    routerComponent.registered,
    routerComponent.operational_state,
    routerComponent.requirement_effect_agents,
    routerComponent.terminal_policies,
    routerComponent.splitters,
    effectListComponent.registered,
    effectListComponent.agents_count,
    effectListComponent.specs,
    effectListComponent.terminal_policies,
    effectListComponent.splitters,
    effectListComponent.route_count,
    effectListComponent.with_spec,
    effectListComponent.terminal_registered,
    effectListComponent.coverage_gain,
    effectListComponent.segments_without_useful_spec,
    artifactActivityComponent,
  ]);
  const atlas = useMemo(() => normalizeNeuralAtlas(enhancedNetwork), [enhancedNetwork]);
  const registryNodes = enhancedNetwork.nodes ?? [];
  const currentSegmentState = sourceNetwork.currentSegmentState ?? {};
  const numberOrNull = (value) => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  };
  const graphNodeCount = registryNodes.length;
  const registeredAgentCount = numberOrNull(registrySummary.registered_agents ?? inventory.registered_agents ?? dashboardSummary.registered_agents ?? dashboardSummary.agents_total);
  const activeAgentCount = numberOrNull(registrySummary.active_agents ?? registrySummary.status_counts?.active);
  const experimentalAgentCount = numberOrNull(registrySummary.experimental_agents ?? registrySummary.status_counts?.experimental);
  const plannedAgentCount = numberOrNull(registrySummary.planned_agents ?? registrySummary.status_counts?.planned);
  const operationalRegisteredCount =
    numberOrNull(registrySummary.operational_agents) ?? numberOrNull(
      Number(classificationCounts.authoritative_core ?? 0) +
      Number(classificationCounts.operational_core ?? 0) +
      Number(classificationCounts.operational_uninstrumented ?? 0)
    ) ?? numberOrNull(dashboardSummary.agents_operational);
  const activeSubspecialistCount = numberOrNull(registrySummary.active_subspecialists ?? registrySummary.active_by_type?.subspecialist);
  const statusOperationalCounts = registrySummary.status_operational_state_counts ?? {};
  const dashboardGroupCounts = registrySummary.dashboard_group_counts ?? {};
  const activeDryRunCount = numberOrNull(statusOperationalCounts['active/dry_run']);
  const experimentalDryRunCount = numberOrNull(statusOperationalCounts['experimental/dry_run']);
  const shadowAgentCount = numberOrNull(registrySummary.shadow_agents) ??
    numberOrNull(Number(statusOperationalCounts['active/shadow'] ?? 0) + Number(statusOperationalCounts['experimental/shadow'] ?? 0));
  const dryRunAgentCount = numberOrNull(registrySummary.dry_run_agents) ??
    numberOrNull(Number(activeDryRunCount ?? 0) + Number(experimentalDryRunCount ?? 0));
  const requirementEffectAgentCount = numberOrNull(registrySummary.requirement_effect_agents ?? routerComponent.requirement_effect_agents);
  const effectListAgentCount = numberOrNull(registrySummary.effect_list_agents ?? effectListComponent.agents_count);
  const notRequirementEffectAgentCount = numberOrNull(registrySummary.not_requirement_effect_agents ?? notReqComponent.agents_count);
  const withoutUsefulSpecCount = numberOrNull(registrySummary.segments_without_useful_spec ?? effectListComponent.segments_without_useful_spec);
  const terminalGuardAgentCount = numberOrNull(registrySummary.terminal_guard_agents);
  const splitterAgentCount = numberOrNull(registrySummary.splitter_agents);
  const specAssociatedAfterNotReq = numberOrNull(registrySummary.spec_associated_after_not_requirement_effect);
  const coverageGainSinceEffectList = numberOrNull(registrySummary.coverage_gain_since_effect_list);
  const labRegisteredCount = numberOrNull(registrySummary.lab_useful_evidence) ??
    numberOrNull(Number(classificationCounts.lab_useful_evidence ?? 0) + Number(classificationCounts.candidate_with_evidence ?? 0));
  const plannedBacklogCount = numberOrNull(registrySummary.planned_backlog ?? classificationCounts.planned_backlog) ?? plannedAgentCount;
  const issueNetworkCount = numberOrNull(dashboardGroupCounts['Issue Network']);
  const operationalAtlasDelta = Math.max(0, Number(operationalRegisteredCount ?? 0) - graphNodeCount);
  const macroCoordinatorCount = registryNodes.filter((node) => ['macro', 'coordinator'].includes(node.family)).length;
  const guardsPolicyCount = registryNodes.filter((node) => ['guards', 'lifecycle_policies'].includes(node.family) || ['guard', 'lifecycle_policy', 'lifecycle_state'].includes(node.type)).length;
  const specialistCount = registryNodes.filter((node) => node.family === 'specialists' || node.type?.startsWith?.('specialist')).length;
  const labGraphCount = registryNodes.filter((node) => node.family === 'subagents' || node.type?.includes?.('microagent') || node.status === 'experimental').length;
  const sourceOutputCount = registryNodes.filter((node) => ['production_output', 'memory'].includes(node.family) || ['production_gateway', 'memory'].includes(node.type)).length;
  const atlasSegmentStateId = versionInfo.latest_segment_state_run?.id ?? null;
  const networkSegmentStateId =
    currentSegmentState.run_id ??
    data.appState?.release?.latest_segment_state_run_id ??
    data.summary?.latest_segment_state_run_id ??
    null;
  const networkUpdatedAt = sourceNetwork.generated_at ?? data.appState?.cache?.generated_at ?? null;
  const atlasIsStale = atlasSegmentStateId && networkSegmentStateId && Number(atlasSegmentStateId) < Number(networkSegmentStateId);
  const atlasUpdatedLabel = compactDateTime(networkUpdatedAt);
  const registryDiagLabel = registrySummary.post_architecture_generated_at
    ? compactDateTime(registrySummary.post_architecture_generated_at)
    : registrySummary.diagnostic_generated_at
      ? compactDateTime(registrySummary.diagnostic_generated_at)
      : 'instrumentação pendente';
  const networkSourceLine = `Atlas #${atlasSegmentStateId ?? 'pendente'} | registro #${networkSegmentStateId ?? 'pendente'} | histórico #${registrySummary.post_architecture_summary?.ledger_run_id ?? '76'} | diagnóstico ${registryDiagLabel}`;
  const atlasMetricCards = [
    {
      title: 'Registrados',
      value: registeredAgentCount == null ? 'instrumentação pendente' : fmt(registeredAgentCount),
      tone: 'violet',
      help: 'Total de agentes, subagentes, políticas e componentes catalogados no registro interno.',
    },
    {
      title: 'Operacionais',
      value: operationalRegisteredCount == null ? 'instrumentação pendente' : fmt(operationalRegisteredCount),
      tone: 'emerald',
      help: `Agentes ativos com autoridade operacional real. Observação e simulação ficam separadas. Falso-seguro operacional conhecido: ${fmt(registrySummary.operational_false_safe ?? versionInfo.current_macro_model?.false_safe_count ?? dashboardSummary.operational_false_safe ?? 0)}.`,
    },
    {
      title: 'Observação',
      value: shadowAgentCount == null ? 'instrumentação pendente' : fmt(shadowAgentCount),
      tone: 'violet',
      help: 'Agentes em observação ou laboratório: roteiam, separam filas e acumulam evidência sem autoridade operacional direta.',
    },
    {
      title: 'Simulação',
      value: dryRunAgentCount == null ? 'instrumentação pendente' : fmt(dryRunAgentCount),
      tone: 'blue',
      help: 'Agentes em ensaio controlado: produzem evidência e pontos de controle, mas ainda não escrevem nem fecham com autoridade final.',
    },
  ];
  const atlasComposition = [
    ['Registro', registeredAgentCount],
    ['Op.', operationalRegisteredCount],
    ['Simulação', dryRunAgentCount],
    ['Observação', shadowAgentCount],
    ['Requisito/efeito', requirementEffectAgentCount],
    ['Lista de efeitos', effectListAgentCount],
  ];
  const routerBadgeText = routerComponent.registered
    ? `Requisito/efeito: ${fmt(requirementEffectAgentCount ?? routerComponent.requirement_effect_agents ?? 0)} agentes`
    : 'Roteador de requisitos e efeitos: instrumentação pendente';
  const routerBadgeHelp = routerComponent.registered
    ? `Roteador somente leitura. ${fmt(requirementEffectAgentCount ?? routerComponent.requirement_effect_agents ?? 0)} agentes de requisito/efeito; pacote de lista de efeitos ${effectListComponent.registered ? 'registrado' : 'pendente'}; sem aplicação ou autoridade no ciclo de vida. Terminais: ${fmt(routerComponent.terminal_policies ?? 0)}. Divisores: ${fmt(routerComponent.splitters ?? 0)}.`
    : 'Componente ainda não encontrado no registro interno de agentes.';
  const effectListBadgeHelp = effectListComponent.registered
    ? [
        `Pacote de lista de efeitos: ${fmt(effectListAgentCount ?? effectListComponent.agents_count ?? 0)} componentes agregados.`,
        `Especificações: ${fmt(effectListComponent.specs ?? 0)}; terminais: ${fmt(effectListComponent.terminal_policies ?? 0)}; divisores: ${fmt(effectListComponent.splitters ?? 0)}.`,
        `Cobertura: ${fmt(effectListComponent.coverage_before ?? 0)} -> ${fmt(effectListComponent.coverage_after ?? 0)} (+${fmt(effectListComponent.coverage_gain ?? 0)}).`,
        `Rota da lista de efeitos: ${fmt(effectListComponent.route_count ?? 0)}; com especificação: ${fmt(effectListComponent.with_spec ?? 0)}; terminais registrados: ${fmt(effectListComponent.terminal_registered ?? 0)}.`,
      ].join(' ')
    : 'Pacote de lista de efeitos ainda não carregado nos dados.';
  const semSpecHelp = effectListComponent.top_blockers?.length
    ? `Sem especificação útil: ${fmt(withoutUsefulSpecCount ?? 0)}. Próximos gargalos: ${effectListComponent.top_blockers.slice(0, 3).map((item) => `${ptFieldLabel(item.route)} ${fmt(item.segments)}`).join(', ')}.`
    : `Sem especificação útil: ${fmt(withoutUsefulSpecCount ?? 0)}.`;
  const remainingGaps = registrySummary.remaining_gaps ?? {};
  const remainingGapsText = [
    ['Contexto de domínio', remainingGaps.domain_context_after_requirement_effect],
    ['Bloqueados', remainingGaps.blocked_uncertain],
    ['Resíduo ScriptValue', remainingGaps.script_value_effect_residual_repair_or_preserved_sublane],
    ['Resíduo de condecoração', remainingGaps.accolade_trait_residual_repair_or_preserved_sublane],
    ['Resíduo de cultura ou nome', remainingGaps.residual_culture_or_name_policy],
  ]
    .filter(([, value]) => Number(value) > 0)
    .map(([label, value]) => `${label} ${fmt(value)}`)
    .join(', ');
  const specCoverageTooltip = [
    `Especificações: ${fmt(specAssociatedAfterNotReq ?? 0)}/${fmt(registrySummary.post_architecture_summary?.pending_segments ?? 11725)}.`,
    `Sem especificação útil: ${fmt(withoutUsefulSpecCount ?? 0)}.`,
    `Ganho desde a lista de efeitos: +${fmt(coverageGainSinceEffectList ?? 0)}.`,
    remainingGapsText ? `Restante: ${remainingGapsText}.` : null,
  ].filter(Boolean).join(' ');
  const notReqTooltip = notReqComponent.registered
    ? [
        'Roteador global para casos fora de requisito.',
        `Divisor em observação e somente leitura.`,
        `Universo: ${fmt(notReqComponent.universe ?? 0)}; reuso: ${fmt(notReqComponent.reuse_existing_policies ?? 0)}/${fmt(notReqComponent.review_total ?? 0)} na amostra.`,
        `Subárvore validada: fora de requisito → cultura/religião → cultura → tradição/herança.`,
        `Não roteados preservados: ${fmt(notReqComponent.unrouted_or_preserved ?? 0)}.`,
      ].join(' ')
    : 'Roteador fora de requisito ainda não carregado nos dados.';
  const networkSummaryTooltip = [
    `Mapa visual: ${fmt(graphNodeCount)} nós.`,
    `Registro: ${registeredAgentCount == null ? 'instrumentação pendente' : fmt(registeredAgentCount)} agentes.`,
    `Operacionais: ${operationalRegisteredCount == null ? 'instrumentação pendente' : fmt(operationalRegisteredCount)} pelo critério ativo e com autoridade, operacional ou em simulação.`,
    `Simulação: ${dryRunAgentCount == null ? 'instrumentação pendente' : fmt(dryRunAgentCount)}.`,
    `Observação: ${shadowAgentCount == null ? 'instrumentação pendente' : fmt(shadowAgentCount)}.`,
    `Terminais: ${terminalGuardAgentCount == null ? 'instrumentação pendente' : fmt(terminalGuardAgentCount)}. Divisores: ${splitterAgentCount == null ? 'instrumentação pendente' : fmt(splitterAgentCount)}.`,
    `Requisito/efeito: ${requirementEffectAgentCount == null ? 'instrumentação pendente' : fmt(requirementEffectAgentCount)} agentes.`,
    `Fora de requisito: ${notRequirementEffectAgentCount == null ? 'instrumentação pendente' : fmt(notRequirementEffectAgentCount)} agentes.`,
    `Lista de efeitos: ${effectListAgentCount == null ? 'instrumentação pendente' : fmt(effectListAgentCount)} componentes.`,
    specCoverageTooltip,
    notReqTooltip,
    networkSourceLine,
  ].join('\n');
  const atlasCompositionHelp = {
    Registro: 'Total atual de agentes no registro ml_agent_registry.',
    'Op.': 'Operacionais = agentes ativos com autoridade, operacionais ou em simulação. Agentes em observação ficam fora.',
    'Simulação': 'Agentes ativos em simulação e somente leitura; contam como operacionais, mas não aplicam alterações na saída.',
    'Observação': 'Agentes em observação ou laboratório: roteiam e acumulam evidência, sem autoridade operacional.',
    'Requisito/efeito': routerBadgeHelp,
    'Lista de efeitos': effectListBadgeHelp,
  };
  const atlasSummary = {
    ...dashboardSummary,
    graph_nodes: graphNodeCount,
    registered_agents: registeredAgentCount,
    operational_registered: operationalRegisteredCount,
    lab_registered: labRegisteredCount,
    operational_false_safe: versionInfo.current_macro_model?.false_safe_count ?? dashboardSummary.operational_false_safe ?? 0,
  };
  const defaultPositions = useMemo(
    () => Object.fromEntries(atlas.nodes.map((node) => [node.id, { x: node.x, y: node.y }])),
    [atlas.versionKey]
  );
  const defaultSizes = useMemo(
    () => Object.fromEntries(atlas.nodes.map((node) => [node.id, { ...ATLAS_DEFAULT_NODE_SIZE }])),
    [atlas.versionKey]
  );
  const [selectedId, setSelectedId] = useState(null);
  const [hoveredId, setHoveredId] = useState(null);
  const [positions, setPositions] = useState(defaultPositions);
  const [nodeSizes, setNodeSizes] = useState(defaultSizes);
  const [dragging, setDragging] = useState(null);
  const [resizing, setResizing] = useState(null);
  const [layoutSaved, setLayoutSaved] = useState(false);
  const nodeAgentClusters = useMemo(
    () => Object.fromEntries(atlas.nodes.map((node) => [node.id, summarizeNodeAgents(registryAgents, node)])),
    [atlas.versionKey, registryAgents]
  );

  const effectiveSelectedId = atlas.nodes.some((node) => node.id === selectedId) ? selectedId : null;
  const selectedNode = atlas.nodes.find((node) => node.id === effectiveSelectedId) ?? null;
  const selectedAgentCluster = selectedNode ? nodeAgentClusters[selectedNode.id] : null;
  const focusId = hoveredId ?? effectiveSelectedId;

  useEffect(() => {
    let alive = true;
    const loadNetwork = async () => {
      setNetworkLoading(true);
      setNetworkError(null);
      try {
        const response = await fetch(`${API_BASE}/neural-visualization`);
        if (!response.ok) throw new Error(`API ${response.status}`);
        const payload = await response.json();
        if (!Array.isArray(payload.network?.nodes) || payload.network.nodes.length === 0) {
          throw new Error('A API não retornou os nós da rede.');
        }
        if (!alive) return;
        setNetworkSource({
          ...(payload.network ?? {}),
          sourcePath: payload.sourcePath,
          registrySummary: payload.registrySummary,
          currentSegmentState: payload.currentSegmentState,
        });
        setNetworkError(null);
      } catch (err) {
        if (!alive) return;
        setNetworkError(err.message);
      } finally {
        if (alive) setNetworkLoading(false);
      }
    };
    loadNetwork();
    return () => {
      alive = false;
    };
  }, [networkLoadAttempt]);

  useEffect(() => {
    let applied = false;
    try {
      const raw = window.localStorage.getItem(ATLAS_LAYOUT_STORAGE_KEY);
      if (raw) {
        const saved = JSON.parse(raw);
        const known = new Set(atlas.nodes.map((node) => node.id));
        const savedPositions = Object.fromEntries(
          Object.entries(saved.positions ?? {}).filter(([id]) => known.has(id))
        );
        const savedSizes = Object.fromEntries(
          Object.entries(saved.sizes ?? {}).filter(([id]) => known.has(id))
        );
        setPositions({ ...defaultPositions, ...savedPositions });
        setNodeSizes({ ...defaultSizes, ...savedSizes });
        setLayoutSaved(true);
        applied = true;
      }
    } catch {
      applied = false;
    }
    if (!applied) {
      setPositions(defaultPositions);
      setNodeSizes(defaultSizes);
      setLayoutSaved(false);
    }
    setSelectedId(null);
    setDragging(null);
    setResizing(null);
  }, [atlas.versionKey, defaultPositions, defaultSizes]);

  const pointerToPercent = (event) => {
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) return null;
    return {
      x: Math.max(4, Math.min(96, ((event.clientX - rect.left) / rect.width) * 100)),
      y: Math.max(8, Math.min(92, ((event.clientY - rect.top) / rect.height) * 100)),
    };
  };

  const startDrag = (event, nodeId) => {
    event.currentTarget.setPointerCapture?.(event.pointerId);
    setDragging(nodeId);
    setResizing(null);
  };

  const startResize = (event, nodeId) => {
    event.stopPropagation();
    const current = nodeSizes[nodeId] ?? ATLAS_DEFAULT_NODE_SIZE;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    setResizing({
      id: nodeId,
      startX: event.clientX,
      startY: event.clientY,
      startW: current.w,
      startH: current.h,
    });
    setDragging(null);
  };

  const dragNode = (event) => {
    if (resizing) {
      const deltaX = event.clientX - resizing.startX;
      const deltaY = event.clientY - resizing.startY;
      setNodeSizes((current) => ({
        ...current,
        [resizing.id]: {
          w: Math.max(132, Math.min(260, resizing.startW + deltaX)),
          h: Math.max(50, Math.min(116, resizing.startH + deltaY)),
        },
      }));
      setLayoutSaved(false);
      return;
    }
    if (!dragging) return;
    const next = pointerToPercent(event);
    if (!next) return;
    setPositions((current) => ({ ...current, [dragging]: next }));
    setLayoutSaved(false);
  };

  const stopDrag = () => {
    setDragging(null);
    setResizing(null);
  };

  const restoreFavoriteLayout = (event) => {
    event.stopPropagation();
    try {
      const raw = window.localStorage.getItem(ATLAS_LAYOUT_STORAGE_KEY);
      if (!raw) {
        setPositions(defaultPositions);
        setNodeSizes(defaultSizes);
        setLayoutSaved(false);
        return;
      }
      const saved = JSON.parse(raw);
      const known = new Set(atlas.nodes.map((node) => node.id));
      const savedPositions = Object.fromEntries(
        Object.entries(saved.positions ?? {}).filter(([id]) => known.has(id))
      );
      const savedSizes = Object.fromEntries(
        Object.entries(saved.sizes ?? {}).filter(([id]) => known.has(id))
      );
      setPositions({ ...defaultPositions, ...savedPositions });
      setNodeSizes({ ...defaultSizes, ...savedSizes });
      setLayoutSaved(true);
    } catch {
      setPositions(defaultPositions);
      setNodeSizes(defaultSizes);
      setLayoutSaved(false);
    }
    setSelectedId(null);
    setDragging(null);
    setResizing(null);
  };

  const resetAtlasLayout = (event) => {
    event.stopPropagation();
    setPositions(defaultPositions);
    setNodeSizes(defaultSizes);
    setSelectedId(null);
    setDragging(null);
    setResizing(null);
    setLayoutSaved(false);
  };

  const saveAtlasLayout = (event) => {
    event.stopPropagation();
    try {
      window.localStorage.setItem(
        ATLAS_LAYOUT_STORAGE_KEY,
        JSON.stringify({
          savedAt: new Date().toISOString(),
          versionKey: atlas.versionKey,
          positions,
          sizes: nodeSizes,
        })
      );
      setLayoutSaved(true);
    } catch {
      setLayoutSaved(false);
    }
  };

  const relatedIds = new Set(
    atlas.edges
      .filter((edge) => edge.source === focusId || edge.target === focusId)
      .flatMap((edge) => [edge.source, edge.target])
  );

  const DetailChip = ({ item, tone = 'slate' }) => (
    <span
      title={item}
      className={cn(
        'rounded-full border px-3 py-1 text-xs font-bold transition',
        tone === 'cyan'
          ? 'border-cyan-300/10 bg-cyan-300/10 text-cyan-100'
          : 'border-white/8 bg-white/8 text-slate-200'
      )}
    >
      {item}
    </span>
  );

  if (!networkSource && networkLoading) {
    return (
      <div className="neural-atlas-shell h-full min-h-[520px]">
        <section className="dashboard-surface grid h-full min-h-[520px] place-items-center overflow-hidden border">
          <div className="max-w-md px-6 text-center" role="status" aria-live="polite">
            <span className="mx-auto grid h-14 w-14 place-items-center rounded-2xl border border-cyan-300/20 bg-cyan-400/10 text-cyan-200 shadow-[0_18px_50px_rgba(34,211,238,0.10)]">
              <Activity className="animate-pulse" size={25} aria-hidden="true" />
            </span>
            <h2 className="mt-5 text-xl font-black text-[var(--dash-text)]">Carregando rede</h2>
            <p className="mt-2 text-sm leading-6 text-[var(--dash-muted)]">
              Buscando o atlas e o registro de agentes mais recentes no SQLite.
            </p>
            <div className="mx-auto mt-5 h-1.5 w-44 overflow-hidden rounded-full bg-white/8">
              <span className="block h-full w-2/3 animate-pulse rounded-full bg-cyan-300/70" />
            </div>
          </div>
        </section>
      </div>
    );
  }

  if (!networkSource && networkError) {
    return (
      <div className="neural-atlas-shell h-full min-h-[520px]">
        <section className="dashboard-surface grid h-full min-h-[520px] place-items-center overflow-hidden border">
          <div className="max-w-lg px-6 text-center" role="alert">
            <span className="mx-auto grid h-14 w-14 place-items-center rounded-2xl border border-amber-300/20 bg-amber-400/10 text-amber-200">
              <AlertTriangle size={25} aria-hidden="true" />
            </span>
            <h2 className="mt-5 text-xl font-black text-[var(--dash-text)]">Não foi possível carregar a rede</h2>
            <p className="mt-2 text-sm leading-6 text-[var(--dash-muted)]">{networkError}</p>
            <button
              type="button"
              onClick={() => setNetworkLoadAttempt((attempt) => attempt + 1)}
              className="dashboard-segmented-button is-active mx-auto mt-5 inline-flex items-center justify-center gap-2 px-4"
            >
              <RotateCcw size={15} aria-hidden="true" />
              Tentar novamente
            </button>
          </div>
        </section>
      </div>
    );
  }

  return (
    <div className="neural-atlas-shell h-full min-h-0 pb-0">
      <style>{`
        .neural-atlas-shell {
          --atlas-bg: var(--dash-bg);
          --atlas-panel: var(--dash-card);
          --atlas-line: color-mix(in srgb, var(--dash-accent) 32%, transparent);
          --atlas-text: var(--dash-text);
          --atlas-muted: var(--dash-muted);
          color: var(--atlas-text);
        }
        .atlas-stage {
          background:
            radial-gradient(circle at 18% 24%, rgba(20, 184, 166, 0.18), transparent 28%),
            radial-gradient(circle at 76% 18%, rgba(139, 92, 246, 0.20), transparent 30%),
            radial-gradient(circle at 66% 84%, rgba(59, 130, 246, 0.16), transparent 34%),
            linear-gradient(135deg, #050814 0%, #07101f 48%, #101525 100%);
        }
        .atlas-stage::before {
          content: "";
          position: absolute;
          inset: 0;
          background-image:
            linear-gradient(rgba(255,255,255,0.04) 1px, transparent 1px),
            linear-gradient(90deg, rgba(255,255,255,0.04) 1px, transparent 1px);
          background-size: 48px 48px;
          mask-image: radial-gradient(circle at center, black, transparent 82%);
          pointer-events: none;
        }
        .atlas-node {
          background: var(--node-bg-dark);
          border-color: rgba(226, 232, 240, 0.44);
          box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--node-accent) 18%, transparent);
          transition: box-shadow 180ms ease, border-color 180ms ease, background 180ms ease, opacity 180ms ease;
          touch-action: none;
          cursor: pointer;
        }
        .atlas-node:hover {
          box-shadow: 0 18px 60px var(--node-glow), inset 0 0 0 1px color-mix(in srgb, var(--node-accent) 38%, transparent);
          border-color: color-mix(in srgb, var(--node-accent) 72%, white 18%);
        }
        .atlas-node-icon {
          border-color: color-mix(in srgb, var(--node-accent) 44%, transparent);
          background: color-mix(in srgb, var(--node-accent) 16%, transparent);
          color: color-mix(in srgb, var(--node-accent) 76%, white);
        }
        .atlas-node-selected {
          border-color: color-mix(in srgb, var(--node-accent) 80%, white 14%);
          background: color-mix(in srgb, var(--node-bg-dark) 80%, var(--node-accent) 20%);
        }
        .atlas-node-focused {
          border-color: color-mix(in srgb, var(--node-accent) 72%, white 16%);
        }
        .atlas-detail-scroll {
          scrollbar-width: thin;
          scrollbar-color: rgba(125, 211, 252, 0.10) transparent;
        }
        .atlas-detail-scroll::-webkit-scrollbar {
          width: 7px;
        }
        .atlas-detail-scroll::-webkit-scrollbar-track {
          background: transparent;
        }
        .atlas-detail-scroll::-webkit-scrollbar-thumb {
          background: rgba(125, 211, 252, 0.10);
          border-radius: 999px;
        }
        .atlas-detail-scroll:hover::-webkit-scrollbar-thumb {
          background: rgba(125, 211, 252, 0.28);
        }
        [data-content-theme='light'] .atlas-stage {
          background:
            radial-gradient(circle at 18% 22%, rgba(20, 184, 166, 0.16), transparent 28%),
            radial-gradient(circle at 76% 18%, rgba(99, 102, 241, 0.14), transparent 30%),
            radial-gradient(circle at 66% 82%, rgba(59, 130, 246, 0.13), transparent 34%),
            linear-gradient(135deg, #f8fafc 0%, #eef2f7 46%, #e2e8f0 100%);
        }
        [data-content-theme='light'] .atlas-stage::before {
          background-image:
            linear-gradient(rgba(51, 65, 85, 0.055) 1px, transparent 1px),
            linear-gradient(90deg, rgba(51, 65, 85, 0.055) 1px, transparent 1px);
        }
        [data-content-theme='light'] .atlas-node {
          background: var(--node-bg-light) !important;
          border-color: var(--node-border-light) !important;
          box-shadow: 0 12px 32px rgba(15, 23, 42, 0.08), inset 0 0 0 1px color-mix(in srgb, var(--node-accent) 16%, transparent);
        }
        [data-content-theme='light'] .atlas-node:hover {
          border-color: color-mix(in srgb, var(--node-accent) 70%, #172033 6%) !important;
          box-shadow: 0 16px 46px color-mix(in srgb, var(--node-accent) 18%, transparent), inset 0 0 0 1px color-mix(in srgb, var(--node-accent) 26%, transparent);
        }
        [data-content-theme='light'] .atlas-node h3 {
          color: #172033 !important;
        }
        [data-content-theme='light'] .atlas-node-icon {
          border-color: color-mix(in srgb, var(--node-accent) 34%, transparent);
          background: color-mix(in srgb, var(--node-accent) 13%, white);
          color: color-mix(in srgb, var(--node-accent) 82%, #172033 18%);
        }
        [data-content-theme='light'] .atlas-node .atlas-resize-mark {
          border-color: rgba(51, 65, 85, 0.70) !important;
        }
        [data-content-theme='light'] .atlas-detail-scroll h3,
        [data-content-theme='light'] .atlas-detail-scroll .text-white {
          color: #172033 !important;
        }
        [data-content-theme='light'] .atlas-detail-scroll .text-slate-300,
        [data-content-theme='light'] .atlas-detail-scroll .text-slate-400 {
          color: #6b6258 !important;
        }
      `}</style>

      <section className="atlas-frame h-full min-h-0 overflow-hidden border">
        <div className="relative atlas-stage h-full min-h-[520px] overflow-hidden p-3" ref={canvasRef} onPointerMove={dragNode} onPointerUp={stopDrag} onPointerCancel={stopDrag} onClick={() => setSelectedId(null)}>
            <div className="relative z-10 flex flex-col gap-2 lg:flex-row lg:items-start lg:justify-between">
              <div className="flex items-center gap-2">
                <div className="atlas-title-card rounded-2xl border border-white/10 bg-black/15 px-3 py-2 backdrop-blur" title={networkSummaryTooltip}>
                  <h2 className="text-lg font-black tracking-tight text-white lg:text-xl">
                    Rede de Tradução CKIII
                  </h2>
                  {networkError && (
                    <p className="mt-1 text-xs font-bold text-amber-100">
                      Atualização indisponível: {networkError}
                    </p>
                  )}
                </div>
                <div className="atlas-toolbar flex rounded-2xl border border-white/10 bg-black/20 p-1 backdrop-blur">
                  <button
                    type="button"
                    title="Início: voltar ao último layout favorito salvo"
                    aria-label="Início: voltar ao último layout favorito salvo"
                    onClick={restoreFavoriteLayout}
                    className="grid h-10 w-10 place-items-center rounded-xl transition"
                  >
                    <Home size={16} />
                  </button>
                  <button
                    type="button"
                    title={layoutSaved ? 'Favorito: o layout atual já está salvo' : 'Favorito: salvar o layout atual'}
                    aria-label={layoutSaved ? 'Favorito: o layout atual já está salvo' : 'Favorito: salvar o layout atual'}
                    onClick={saveAtlasLayout}
                    className={cn(
                      'grid h-10 w-10 place-items-center rounded-xl transition',
                      layoutSaved && 'atlas-toolbar-favorite-saved'
                    )}
                  >
                    <Star size={16} fill={layoutSaved ? 'currentColor' : 'none'} />
                  </button>
                </div>
                <div className="atlas-toolbar flex rounded-2xl border border-white/10 bg-black/20 p-1 backdrop-blur">
                  <button
                    type="button"
                    title="Redefinir: voltar ao layout padrão do grafo"
                    aria-label="Redefinir: voltar ao layout padrão do grafo"
                    onClick={resetAtlasLayout}
                    className="grid h-10 w-10 place-items-center rounded-xl transition"
                  >
                    <RotateCcw size={16} />
                  </button>
                </div>
              </div>
              <div className="atlas-metrics-panel pointer-events-none grid w-full grid-cols-2 gap-1 rounded-2xl border border-white/10 bg-black/20 p-1.5 backdrop-blur sm:grid-cols-4 lg:w-auto lg:min-w-[620px]">
                <div className="col-span-full flex flex-wrap justify-end gap-1.5 text-[0.64rem] font-black">
                  <span className="rounded-full border border-white/10 bg-white/[0.05] px-2 py-0.5 text-slate-300" title="Nós renderizados neste mapa macro. O registro real fica agregado em grupos.">
                    Mapa {fmt(graphNodeCount)}
                  </span>
                  <span className="rounded-full border border-white/10 bg-white/[0.05] px-2 py-0.5 text-slate-300" title="Data do atlas visual, não do estado operacional atual.">
                    Atlas {atlasUpdatedLabel}
                  </span>
                  <span className="rounded-full border border-white/10 bg-blue-500/10 px-2 py-0.5 text-blue-200" title={networkSourceLine}>
                    Atlas #{atlasSegmentStateId ?? '-'}
                  </span>
                  <span className="rounded-full border border-white/10 bg-emerald-500/10 px-2 py-0.5 text-emerald-200" title="Último estado operacional dos segmentos conhecido.">
                    Estado #{networkSegmentStateId ?? '-'}
                  </span>
                  {atlasIsStale && (
                    <span className="rounded-full border border-amber-300/20 bg-amber-400/10 px-2 py-0.5 text-amber-200" title="O mapa visual está em uma execução anterior; os cartões do registro usam dados atuais quando disponíveis.">
                      atlas antigo
                    </span>
                  )}
                </div>
                {atlasMetricCards.map((card) => (
                  <div
                    key={card.title}
                    className="atlas-metric-card pointer-events-auto cursor-pointer rounded-xl border px-2 py-1.5"
                    title={card.help}
                    aria-label={`${card.title}: ${card.help}`}
                  >
                    <p className="text-[0.58rem] font-semibold uppercase tracking-wide text-[var(--dash-muted)]">{card.title}</p>
                    <p className={cn(
                      'mt-0.5 text-sm font-black leading-none',
                      card.tone === 'emerald' ? 'text-emerald-400' :
                        card.tone === 'red' ? 'text-red-400' :
                          card.tone === 'blue' ? 'text-blue-400' :
                            card.tone === 'amber' ? 'text-amber-400' :
                              card.tone === 'violet' ? 'text-violet-400' :
                                'text-[var(--dash-text)]'
                    )}>{card.value}</p>
                  </div>
                ))}
              </div>
            </div>

            <svg className="pointer-events-none absolute inset-0 z-[1] h-full w-full" viewBox="0 0 100 100" preserveAspectRatio="none">
              <defs>
                <filter id="atlasGlow">
                  <feGaussianBlur stdDeviation="0.45" result="blur" />
                  <feMerge>
                    <feMergeNode in="blur" />
                    <feMergeNode in="SourceGraphic" />
                  </feMerge>
                </filter>
              </defs>
              {atlas.edges.map((edge, index) => {
                const { source, target } = edge;
                const start = positions[source];
                const end = positions[target];
                const isActive = source === focusId || target === focusId;
                if (!start || !end) return null;
                const midX = (start.x + end.x) / 2;
                const midY = (start.y + end.y) / 2 - 7;
                return (
                  <g key={`${source}-${target}-${index}`}>
                    <path
                      d={`M ${start.x} ${start.y} Q ${midX} ${midY} ${end.x} ${end.y}`}
                      fill="none"
                      stroke={isActive ? 'rgba(125, 211, 252, 0.96)' : 'rgba(148, 163, 184, 0.18)'}
                      strokeWidth={isActive ? 0.42 : 0.13}
                      filter={isActive ? 'url(#atlasGlow)' : undefined}
                    />
                  </g>
                );
              })}
            </svg>

            <div className="absolute inset-0 z-[2]">
              {atlas.nodes.map((node) => {
                const pos = positions[node.id] ?? { x: node.x, y: node.y };
                const Icon = node.icon;
                const isSelected = effectiveSelectedId === node.id;
                const isRelated = relatedIds.has(node.id);
                const isFocused = focusId === node.id;
                const opacity = isSelected || isFocused || isRelated ? 1 : hoveredId ? 0.45 : 0.86;
                const palette = node.palette ?? atlasNodePalettes.macro;
                const nodeTooltip = [
                  node.label,
                  `Tipo: ${ptType(node.type)}`,
                  `Estado: ${ptStatus(node.status)}`,
                  node.rawRole ? `Função: ${ptRole(node.rawRole)}` : null,
                  node.description ? `Resumo: ${node.description}` : null,
                ].filter(Boolean).join('\n');
                const agentCluster = nodeAgentClusters[node.id] ?? { total: 0, operational: 0, shadow: 0 };
                return (
                  <button
                    key={node.id}
                    type="button"
                    title={nodeTooltip}
                    aria-label={nodeTooltip}
                    onPointerDown={(event) => startDrag(event, node.id)}
                    onPointerEnter={() => setHoveredId(node.id)}
                    onPointerLeave={() => setHoveredId(null)}
                    onClick={(event) => {
                      event.stopPropagation();
                      setSelectedId((current) => (current === node.id ? null : node.id));
                    }}
                    className={cn(
                      'atlas-node absolute rounded-[1.45rem] border p-2.5 text-left backdrop-blur-md',
                      isSelected ? 'atlas-node-selected' : isFocused ? 'atlas-node-focused' : ''
                    )}
                    style={{
                      '--node-accent': palette.accent,
                      '--node-bg-dark': palette.darkBg,
                      '--node-bg-light': palette.lightBg,
                      '--node-border-light': palette.lightBorder,
                      '--node-glow': `${palette.accent}33`,
                      left: `${pos.x}%`,
                      top: `${pos.y}%`,
                      width: `${nodeSizes[node.id]?.w ?? ATLAS_DEFAULT_NODE_SIZE.w}px`,
                      minHeight: `${nodeSizes[node.id]?.h ?? ATLAS_DEFAULT_NODE_SIZE.h}px`,
                      opacity,
                      transform: 'translate(-50%, -50%)',
                    }}
                  >
                    <div className="flex items-center gap-2">
                      <div className="atlas-node-icon grid h-8 w-8 shrink-0 place-items-center rounded-full border">
                        <Icon size={15} />
                      </div>
                      <div className="min-w-0">
                        <h3 className="line-clamp-2 text-[0.78rem] font-black leading-tight text-white">{node.displayLabel ?? node.label}</h3>
                        {agentCluster.total > 0 && (
                          <div
                            className="mt-1 flex items-center gap-1 text-[0.55rem] font-black uppercase tracking-wide text-slate-300/90"
                            title={`${agentCluster.total} neurônios ligados; ${agentCluster.operational} operacionais; ${agentCluster.shadow} em observação.`}
                          >
                            <span className="flex items-center gap-0.5">
                              {['operational', 'shadow', 'lab'].map((kind, dotIndex) => (
                                <span
                                  key={kind}
                                  className={cn(
                                    'h-1.5 w-1.5 rounded-full',
                                    dotIndex === 0 ? 'bg-emerald-300' : dotIndex === 1 ? 'bg-violet-300' : 'bg-sky-300'
                                  )}
                                />
                              ))}
                            </span>
                            <span>{fmt(agentCluster.total)} neurônios</span>
                          </div>
                        )}
                      </div>
                    </div>
                    <span
                      title="Redimensionar"
                      onPointerDown={(event) => startResize(event, node.id)}
                      onClick={(event) => event.stopPropagation()}
                      className="absolute bottom-1.5 right-1.5 h-4 w-4 cursor-nwse-resize rounded-br-[1rem] opacity-35 transition hover:opacity-90"
                    >
                      <span className="atlas-resize-mark absolute bottom-0 right-0 h-2.5 w-2.5 border-b border-r border-white/70" />
                    </span>
                  </button>
                );
              })}
            </div>
          {selectedNode && (
            <aside onClick={(event) => event.stopPropagation()} className="atlas-detail-scroll absolute bottom-5 right-5 top-5 z-20 w-[360px] overflow-y-auto rounded-3xl border border-white/10 bg-[#070b18]/95 p-6 shadow-[0_28px_90px_rgba(0,0,0,0.42)] backdrop-blur-xl">
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="text-xs font-black uppercase tracking-wide text-cyan-200">{ptFamily(selectedNode.family)}</p>
                <h3 className="mt-2 text-2xl font-black text-white">{selectedNode.label}</h3>
              </div>
              <Badge tone={selectedNode.tone}>{ptStatus(selectedNode.status)}</Badge>
            </div>
            <p className="mt-4 text-sm leading-6 text-slate-300">{selectedNode.description}</p>
            <div className="mt-5 rounded-2xl border border-white/10 bg-white/[0.04] p-4">
              <p className="text-xs font-black uppercase tracking-wide text-slate-400">Função</p>
              <p className="mt-2 text-sm leading-6 text-white">{selectedNode.role}</p>
            </div>
            {selectedAgentCluster?.total > 0 && (
              <div className="mt-4 rounded-2xl border border-violet-300/20 bg-violet-400/10 p-4">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="text-xs font-black uppercase tracking-wide text-violet-100">Neurônios ligados</p>
                    <p className="mt-1 text-xs leading-5 text-violet-100/80">
                      Agentes reais do registro agregados neste nó macro.
                    </p>
                  </div>
                  <span className="rounded-full border border-violet-200/20 bg-violet-300/15 px-2 py-1 text-xs font-black text-violet-100">
                    {fmt(selectedAgentCluster.total)}
                  </span>
                </div>
                <div className="mt-3 flex flex-wrap gap-1.5 text-[0.66rem] font-black">
                  <span className="rounded-full border border-emerald-300/20 bg-emerald-400/10 px-2 py-1 text-emerald-100">
                    operacionais {fmt(selectedAgentCluster.operational)}
                  </span>
                  <span className="rounded-full border border-violet-300/20 bg-violet-400/10 px-2 py-1 text-violet-100">
                    observação {fmt(selectedAgentCluster.shadow)}
                  </span>
                  {Object.entries(selectedAgentCluster.byType).slice(0, 3).map(([type, total]) => (
                    <span key={type} className="rounded-full border border-white/10 bg-white/[0.055] px-2 py-1 text-slate-200">
                      {ptType(type)} {fmt(total)}
                    </span>
                  ))}
                </div>
                <div className="mt-3 max-h-48 space-y-2 overflow-y-auto pr-1">
                  {selectedAgentCluster.agents.slice(0, 12).map((agent) => (
                    <div
                      key={agent.agent_key}
                      data-tooltip-title={compactAgentLabel(agent)}
                      data-tooltip-description={ptAgentDescription(agent)}
                      data-tooltip-meta={`Estado: ${ptStatus(agent.operational_state)}`}
                      className="rounded-xl border border-white/10 bg-black/15 px-3 py-2"
                    >
                      <div className="flex items-start justify-between gap-2">
                        <p className="text-xs font-black leading-4 text-white">{compactAgentLabel(agent)}</p>
                        <span className={cn(
                          'shrink-0 rounded-full px-2 py-0.5 text-[0.58rem] font-black',
                          agent.operational_state === 'shadow' ? 'bg-violet-400/10 text-violet-100' :
                            agent.operational_state === 'dry_run' ? 'bg-sky-400/10 text-sky-100' :
                              ['operational', 'authoritative'].includes(agent.operational_state) ? 'bg-emerald-400/10 text-emerald-100' :
                                'bg-white/10 text-slate-200'
                        )}>
                          {ptStatus(agent.operational_state)}
                        </span>
                      </div>
                      <p className="mt-1 text-[0.68rem] leading-4 text-slate-300">
                        {ptType(agent.agent_type)} · {ptRole(agent.decision_role)}
                      </p>
                    </div>
                  ))}
                  {selectedAgentCluster.total > 12 && (
                    <p className="rounded-xl border border-white/10 bg-white/[0.04] px-3 py-2 text-xs font-bold text-slate-300">
                      +{fmt(selectedAgentCluster.total - 12)} neurônios agregados neste nó.
                    </p>
                  )}
                </div>
              </div>
            )}
            <div className="mt-4 grid gap-3">
              {(selectedNode.metrics ?? []).map((item) => (
                <div
                  key={item}
                  title={item}
                  className="rounded-xl border border-white/10 bg-white/[0.04] px-4 py-3 text-sm font-bold text-slate-100"
                >
                  {item}
                </div>
              ))}
            </div>
            <div className="mt-5 grid gap-3 md:grid-cols-2 xl:grid-cols-1">
              {!!selectedNode.inputs?.length && (
                <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
                  <p className="text-xs font-black uppercase tracking-wide text-slate-400">Entradas</p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    {selectedNode.inputs.slice(0, 8).map((item) => (
                      <DetailChip key={item} item={item} />
                    ))}
                  </div>
                </div>
              )}
              {!!selectedNode.outputs?.length && (
                <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
                  <p className="text-xs font-black uppercase tracking-wide text-slate-400">Saídas</p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    {selectedNode.outputs.slice(0, 8).map((item) => (
                      <DetailChip key={item} item={item} tone="cyan" />
                    ))}
                  </div>
                </div>
              )}
            </div>
            {!!selectedNode.risks?.length && (
              <div className="mt-5 rounded-2xl border border-red-300/20 bg-red-300/10 p-4">
                <p className="text-xs font-black uppercase tracking-wide text-red-100">Atenção</p>
                <div className="mt-3 space-y-2">
                  {selectedNode.risks.slice(0, 3).map((item) => (
                    <p
                      key={item}
                      title={item}
                      className="rounded-lg border border-transparent px-3 py-2 text-sm leading-5 text-red-50"
                    >
                      {item}
                    </p>
                  ))}
                </div>
              </div>
            )}
            <div className="mt-5 rounded-2xl border border-amber-300/20 bg-amber-300/10 p-4">
              <p className="text-xs font-black uppercase tracking-wide text-amber-100">Próximo aprendizado</p>
              <div className="mt-2 space-y-2 text-sm leading-6 text-amber-50">
                {(selectedNode.next_steps?.length ? selectedNode.next_steps : ['Aguardando a próxima definição do fluxo.']).slice(0, 4).map((item) => (
                  <p
                    key={item}
                    title={item}
                    className="rounded-lg border border-transparent px-3 py-2"
                  >
                    {item}
                  </p>
                ))}
              </div>
            </div>
          </aside>
          )}
        </div>
      </section>
    </div>
  );
}

const screens = {
  Production: ProductionControlCompact,
  Dashboard: ProjectIntelligenceDashboard,
  Managerial,
  Operational: Cockpit,
  Cockpit,
  'ML Performance': MLPerformance,
  Pipeline,
  Lifecycle,
  Governance,
  Policy,
  Lab,
  Specialists,
  'System Architecture': ProductionArchitecture,
  'Neural Network': NeuralArchitecture,
  Network,
};

const navItems = ['Production', 'Dashboard'];
const navLabels = { Production: 'Production', Dashboard: 'Dashboard', Managerial: 'Managerial', Operational: 'Operational', Cockpit: 'Cockpit', 'ML Performance': 'Performance', Pipeline: 'Pipeline', Lifecycle: 'Lifecycle', Governance: 'Governance', Policy: 'Policy', Lab: 'Lab', Specialists: 'Specialists', 'System Architecture': 'System', 'Neural Network': 'Atlas', Network: 'Network' };
const operationalNavItems = navItems;
const screenMeta = {
  Production: { title: 'Production Control', subtitle: 'Source, output, gate e inicio seguro do fluxo de producao' },
  Dashboard: { title: 'Inteligência do Projeto', subtitle: 'Qualidade do pacote, evolução das versões e arquitetura da rede' },
  Managerial: { title: 'Managerial Dashboard', subtitle: 'Visao macro de release e pendencias operacionais' },
  Operational: { title: 'Operational Dashboard', subtitle: 'Cockpit analitico do pacote e confianca atual' },
  Cockpit: { title: 'Cockpit Executivo', subtitle: 'O projeto está avançando e está seguro?' },
  'ML Performance': { title: 'ML Performance', subtitle: 'Nossa rede neural está aprendendo melhor ou só ficando confiante demais?' },
  Pipeline: { title: 'Pipeline', subtitle: 'Onde está o trabalho agora?' },
  Lifecycle: { title: 'Lifecycle', subtitle: 'Estado final operacional dos segmentos' },
  Governance: { title: 'Governance', subtitle: 'Estamos protegidos contra erros perigosos?' },
  Policy: { title: 'Policy', subtitle: 'Modelo puro vs política operacional por grupo' },
  Lab: { title: 'Lab', subtitle: 'Modelo experimental vs modelo ativo' },
  Specialists: { title: 'Specialists', subtitle: 'Especialistas por família, auditoria e aprendizado humano' },
  'System Architecture': { title: 'System Architecture', subtitle: 'Fluxo de producao do source ao release' },
  'Neural Network': { title: 'Neural Atlas', subtitle: 'Mapa visual da rede neuro-simbolica, coordenador e neuroniozinhos' },
  Network: { title: 'Network', subtitle: 'Modelo geral, coordenador, agentes e subagentes' },
};

class DashboardErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    console.error('[dashboard] render error', error, info);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="min-h-screen bg-slate-950 p-6 text-white">
        <div className="mx-auto max-w-3xl rounded-2xl border border-red-400/30 bg-red-500/10 p-6 shadow-2xl">
          <div className="inline-flex items-center gap-2 rounded-full border border-red-300/30 bg-red-300/10 px-3 py-1 text-xs font-black text-red-100">
            <AlertTriangle size={14} /> Dashboard render error
          </div>
          <h1 className="mt-4 text-2xl font-black">A tela encontrou um erro ao renderizar</h1>
          <p className="mt-2 text-sm leading-6 text-red-50">
            O app nao ficou mais em branco silencioso. Recarregue a pagina depois de reiniciar backend/front, ou volte para a tela principal.
          </p>
          <pre className="mt-4 max-h-64 overflow-auto rounded-xl border border-white/10 bg-black/40 p-4 text-xs text-red-50">
            {String(this.state.error?.stack ?? this.state.error?.message ?? this.state.error)}
          </pre>
          <div className="mt-5 flex flex-wrap gap-2">
            <button onClick={() => window.location.assign('/#Production')} className="h-10 rounded-xl bg-blue-600 px-4 text-sm font-black text-white">
              Voltar para Production
            </button>
            <button onClick={() => window.location.reload()} className="h-10 rounded-xl border border-white/15 bg-white/10 px-4 text-sm font-black text-white">
              Recarregar
            </button>
          </div>
        </div>
      </div>
    );
  }
}

function App() {
  const [activeTab, setActiveTab] = useState(() => {
    const hash = decodeURIComponent(window.location.hash.replace(/^#/, ''));
    const route = hash.split('/')[0];
    return screens[route] ? route : 'Production';
  });
  const [isDarkMode, setIsDarkMode] = useState(() => {
    try {
      const saved = window.localStorage.getItem(DASHBOARD_THEME_STORAGE_KEY);
      if (saved === 'dark') return true;
      if (saved === 'light') return false;
      return window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? true;
    } catch {
      return true;
    }
  });
  const [data, setData] = useState(null);
  const [appState, setAppState] = useState(null);
  const [error, setError] = useState(null);
  const [dashboardView, setDashboardView] = useState(dashboardViewFromHash);
  const needsFullDashboard = activeTab !== 'Production' && !(activeTab === 'Dashboard' && dashboardView === 'network');

  const fetchAppStatePayload = async () => {
    const response = await fetch(`${API_BASE}/app-state`);
    if (!response.ok) throw new Error(`API ${response.status}`);
    return response.json();
  };

  const refreshAppStateNow = async (consolidatedPayload = null) => {
    const payload = consolidatedPayload ?? await fetchAppStatePayload();
    setAppState(payload);
    setError(null);
    return payload;
  };

  useEffect(() => {
    let alive = true;
    const loadAppState = async () => {
      try {
        const payload = await fetchAppStatePayload();
        if (alive) {
          setAppState(payload);
          setError(null);
        }
      } catch (err) {
        if (alive) setError(err.message);
      }
    };
    loadAppState();
    const timer = setInterval(loadAppState, 60000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    if (!needsFullDashboard) return undefined;
    let alive = true;
    const load = async () => {
      try {
        const response = await fetch(`${API_BASE}/dashboard`);
        if (!response.ok) throw new Error(`API ${response.status}`);
        const payload = await response.json();
        if (alive) {
          setData(payload);
          setError(null);
        }
      } catch (err) {
        if (alive) setError(err.message);
      }
    };
    load();
    const timer = setInterval(load, 60000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [needsFullDashboard]);

  useEffect(() => {
    const onHashChange = () => {
      const hash = decodeURIComponent(window.location.hash.replace(/^#/, ''));
      const route = hash.split('/')[0];
      setDashboardView(dashboardViewFromHash());
      if (screens[route]) setActiveTab(route);
    };
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  useEffect(() => {
    try {
      window.localStorage.setItem(DASHBOARD_THEME_STORAGE_KEY, isDarkMode ? 'dark' : 'light');
    } catch {
      // LocalStorage pode estar indisponivel em modo privado; a sessao ainda funciona.
    }
  }, [isDarkMode]);

  useEffect(() => {
    const onStorage = (event) => {
      if (event.key !== DASHBOARD_THEME_STORAGE_KEY) return;
      if (event.newValue === 'dark') setIsDarkMode(true);
      if (event.newValue === 'light') setIsDarkMode(false);
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);

  const selectTab = (name) => {
    setActiveTab(name);
    window.history.replaceState(null, '', `#${encodeURIComponent(name)}`);
  };

  const selectDashboardView = (nextView) => {
    setActiveTab('Dashboard');
    setDashboardView(nextView);
    window.history.replaceState(null, '', `#${encodeURIComponent(`Dashboard/${nextView}`)}`);
    window.dispatchEvent(new Event('hashchange'));
  };

  const ActiveScreen = screens[activeTab];
  const currentScreen = screenMeta[activeTab];
  const activeDashboardItem = dashboardViewItems.find((item) => item.id === dashboardView) ?? dashboardViewItems[0];
  const headerSubtitle = activeTab === 'Dashboard' ? activeDashboardItem.subtitle : currentScreen.subtitle;
  const showOperationalNav = operationalNavItems.includes(activeTab);
  const canRenderWithoutApi = activeTab === 'Neural Network' || activeTab === 'Network' || (activeTab === 'Dashboard' && dashboardView === 'network');
  const canRenderWithAppState = activeTab === 'Production' || activeTab === 'Dashboard';
  const screenData = { ...(data ?? { agents: { summary: {} } }), appState: appState ?? {}, _fullDashboardLoaded: Boolean(data) };
  const isWaitingForData = !data && !error && !canRenderWithoutApi && !(canRenderWithAppState && appState);
  const canRenderScreen = Boolean(data || canRenderWithoutApi || (canRenderWithAppState && appState));

  return (
    <div className="h-screen overflow-hidden">
      <div
        data-content-theme={isDarkMode ? 'dark' : 'light'}
        className={`${isDarkMode ? 'dark ' : ''}dashboard-shell h-screen w-full overflow-hidden p-4 [&_button]:cursor-pointer`}
      >
        <main className="mx-auto flex h-full max-w-[1920px] flex-col overflow-hidden">
          <header className="dashboard-header grid min-h-[64px] shrink-0 grid-cols-12 items-center gap-4 border px-4 py-2">
            <div className="col-span-12 lg:col-span-5">
              <div className="flex items-center gap-4">
                <div className="grid h-10 w-10 place-items-center rounded-xl border border-red-500/25 bg-[#070b18] shadow-[0_0_24px_rgba(251,23,75,0.18)]">
                  <img src="/favicon.svg" alt="" className="h-8 w-8" aria-hidden="true" />
                </div>
                <div className="min-w-0">
                  <h1 className="truncate text-lg font-semibold tracking-tight">{currentScreen.title}</h1>
                  <p className="truncate text-sm text-[var(--dash-muted)]">{headerSubtitle}</p>
                </div>
              </div>
            </div>

            <div className="col-span-12 flex flex-wrap items-center justify-start gap-2 lg:col-span-7 lg:justify-end">
              {activeTab === 'Dashboard' ? (
                <nav className="dashboard-segmented w-fit flex-wrap">
                  {dashboardViewItems.map((item) => (
                    <button
                      key={item.id}
                      onClick={() => selectDashboardView(item.id)}
                      className={cn(
                        'dashboard-segmented-button px-3 text-xs font-black',
                        dashboardView === item.id && 'is-active'
                      )}
                    >
                      {item.label}
                    </button>
                  ))}
                </nav>
              ) : showOperationalNav && (
                <nav className="dashboard-segmented w-fit flex-wrap">
                  <button
                    onClick={() => openDashboardTab('Dashboard/overview')}
                    className="dashboard-segmented-button is-active grid w-8 place-items-center"
                    title="Abrir Dashboard em nova guia"
                    aria-label="Abrir Dashboard em nova guia"
                  >
                    <LayoutDashboard size={15} />
                  </button>
                </nav>
              )}
              <button
                onClick={() => setIsDarkMode((current) => !current)}
                className="dashboard-icon-button"
                title={isDarkMode ? 'Ativar tema claro' : 'Ativar tema escuro'}
                aria-label={isDarkMode ? 'Ativar tema claro' : 'Ativar tema escuro'}
              >
                {isDarkMode ? <Sun /> : <Moon />}
              </button>
            </div>
          </header>

          <div className="mt-3 min-h-0 flex-1 overflow-hidden">
            {error && (
              <Card className="mb-5 border-red-500/40 p-4 text-red-300">
                Não consegui carregar a API local: {error}. Inicie com <code>python dashboard/backend.py --host 127.0.0.1 --port 8765</code>.
              </Card>
            )}
            {isWaitingForData && <Card className="p-6">Carregando dados reais do SQLite...</Card>}
            {canRenderScreen && <ActiveScreen data={screenData} onRefreshAppState={refreshAppStateNow} />}
          </div>
        </main>
      </div>
    </div>
  );
}

createRoot(document.getElementById('root')).render(
  <DashboardErrorBoundary>
    <>
      <GlobalTooltipLayer />
      <App />
    </>
  </DashboardErrorBoundary>
);
