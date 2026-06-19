import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
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
  RefreshCw,
  Route,
  Scale,
  Rocket,
  SearchCheck,
  ShieldAlert,
  ShieldCheck,
  Star,
  Sun,
  TerminalSquare,
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
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

const API_BASE = import.meta.env.VITE_DASHBOARD_API ?? 'http://127.0.0.1:8765/api';

const fmt = (value) => Number(value ?? 0).toLocaleString('pt-BR');
const compact = (value) => Intl.NumberFormat('pt-BR', { notation: 'compact', maximumFractionDigits: 1 }).format(Number(value ?? 0));
const pct = (value) => `${Number(value ?? 0).toLocaleString('pt-BR', { maximumFractionDigits: 2 })}%`;
const metric = (value) => Number(value ?? 0).toLocaleString('pt-BR', { maximumFractionDigits: 4 });
const pctMetric = (value) => pct(Number(value ?? 0) * 100);

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

const ModelTooltip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null;
  const source = payload[0]?.payload ?? {};
  return (
    <div className="rounded-md border border-slate-300 bg-white p-3 text-sm shadow-lg dark:border-slate-700 dark:bg-slate-950">
      <p className="mb-2 font-bold text-slate-900 dark:text-white">{source.modelVersion ?? `Run ${label}`}</p>
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
    <div className="rounded-md border border-slate-300 bg-white p-3 text-sm shadow-lg dark:border-slate-700 dark:bg-slate-950">
      <p className="mb-2 font-bold text-slate-900 dark:text-white">
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
  <div className={`rounded-2xl border border-[var(--dash-border)] bg-[var(--dash-card)] shadow-[var(--dash-shadow)] ${className}`}>
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
    <p className={`mt-0.5 text-base font-black ${tone === 'emerald' ? 'text-emerald-400' : tone === 'red' ? 'text-red-400' : tone === 'blue' ? 'text-blue-400' : tone === 'amber' ? 'text-amber-400' : 'text-[var(--dash-text)]'}`}>{value}</p>
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
  <span className={`rounded-full px-3 py-1 text-xs font-bold ${colorClasses[tone] ?? colorClasses.emerald}`}>{children}</span>
);

const ViewToggle = ({ options, value, onChange }) => (
  <div className="inline-flex h-10 items-center rounded-xl border border-[var(--dash-border)] bg-[var(--dash-card)] p-1 shadow-[0_18px_60px_rgba(0,0,0,0.18)]">
    {options.map((item) => (
      <button
        key={item}
        onClick={() => onChange(item)}
        className={`h-8 rounded-lg px-3 text-sm font-medium transition ${
          value === item ? 'bg-blue-600 text-white shadow-lg shadow-blue-600/20 dark:bg-blue-500/20 dark:text-blue-200 dark:shadow-none' : 'text-[var(--dash-muted)] hover:bg-[var(--dash-subtle)] hover:text-[var(--dash-text)]'
        }`}
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
  if (['done', 'ready_for_game_test', 'ready_with_known_issues', 'idle'].includes(status)) return 'emerald';
  if (['starting', 'running', 'queued_visual_stub', 'checking'].includes(status)) return 'blue';
  if (['blocked', 'failed', 'learning_locked'].includes(status)) return 'red';
  return 'amber';
};

const statusLabel = (status) => ({
  done: 'Done',
  starting: 'Starting',
  running: 'Running',
  blocked: 'Blocked',
  pending: 'Pending',
  failed: 'Failed',
  idle: 'Idle',
  learning_locked: 'Learning locked',
  ready_for_game_test: 'Ready for game test',
  ready_with_known_issues: 'Ready with known issues',
  queued_visual_stub: 'Etapa',
}[status] ?? status ?? 'Unknown');

const openDashboardTab = (tab) => {
  const target = `${window.location.origin}${window.location.pathname}#${encodeURIComponent(tab)}`;
  window.open(target, '_blank', 'noopener,noreferrer');
};

const neuralProductionStages = {
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
};

const productionStageDetails = {
  snapshot: 'Cria snapshot local antes de qualquer escrita no output.',
  snapshot_archive: 'Arquiva o snapshot para backup e rastreabilidade da execucao.',
  preflight_sync: 'Sincroniza indice, banco e fontes antes do fluxo principal.',
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
  production_report: 'Gera relatorio final com logs, pendencias e proximas acoes.',
};

const productionStageBlueprint = [
  ['snapshot', 'Pre-run Snapshot'],
  ['snapshot_archive', 'Archive Snapshot'],
  ['preflight_sync', 'Preflight Index Sync'],
  ['segment_state_before', 'Segment State'],
  ['apply_general_dry_run', 'General Apply Dry-run'],
  ['apply_token_policy_dry_run', 'Token Policy Apply Dry-run'],
  ['controlled_token_subpolicy_dry_run', 'Controlled Token Subpolicy Dry-run'],
  ['select_cstring_bridge_dry_run', 'Select_CString Bridge Dry-run'],
  ['same_token_boundary_repair_audit', 'Same-token Boundary Repair Audit'],
  ['same_token_boundary_repair_dry_run', 'Same-token Boundary Repair Dry-run'],
  ['title_landed_es_repair_dry_run', 'Landed Title -es Repair Dry-run'],
  ['apply_general_write', 'Write Regular Output'],
  ['apply_token_policy_write', 'Write Token-Policy Output'],
  ['controlled_token_subpolicy_write', 'Controlled Token Subpolicy Write'],
  ['select_cstring_bridge_write', 'Select_CString Bridge Write'],
  ['same_token_boundary_repair_write', 'Same-token Boundary Repair Write/Close'],
  ['title_landed_es_repair_write', 'Landed Title -es Repair Write'],
  ['apply_locked_override_write', 'Write Locked Manual Overrides'],
  ['segment_state_after', 'Post-write Segment State'],
  ['token_policy_after', 'Post-write Token Policy'],
  ['controlled_token_subpolicy_reaudit', 'Controlled Token Subpolicy Reaudit'],
  ['select_cstring_bridge_reaudit', 'Select_CString Bridge Reaudit'],
  ['same_token_boundary_repair_reaudit', 'Same-token Boundary Repair Reaudit'],
  ['title_landed_es_repair_reaudit', 'Landed Title -es Repair Reaudit'],
  ['composite_review_progress', 'Composite Review Progress'],
  ['production_report', 'Production Report'],
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
    title: 'Analise e Politicas',
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
    title: 'Aplicacao Controlada',
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
    title: 'Validacao e Handoff',
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

const stageById = (stages) => stages.reduce((acc, stage) => {
  acc[stage.id] = stage;
  return acc;
}, {});

const buildProductionPhases = (stages) => {
  const map = stageById(stages.length ? stages : productionStageBlueprint);
  return productionPhaseBlueprint.map((phase) => {
    const phaseStages = phase.stageIds.map((id) => map[id] ?? productionStageBlueprint.find((stage) => stage.id === id)).filter(Boolean);
    const done = phaseStages.filter((stage) => stage.status === 'done').length;
    const running = phaseStages.find((stage) => stage.status === 'running');
    const failed = phaseStages.find((stage) => stage.status === 'failed');
    const status = failed ? 'failed' : running ? 'running' : done === phaseStages.length && phaseStages.length ? 'done' : 'pending';
    return {
      ...phase,
      status,
      stages: phaseStages,
      done,
      total: phaseStages.length,
      progress: phaseStages.length ? Math.round((done / phaseStages.length) * 100) : 0,
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
        setRunStatus(payload.run ?? null);
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
                          neuralDetail && 'ring-1 ring-violet-400/20',
                          stage.status === 'running'
                            ? 'border-blue-400/50 bg-blue-500/10'
                            : stage.status === 'done'
                              ? 'border-emerald-400/30 bg-emerald-500/10'
                              : stage.status === 'failed'
                                ? 'border-red-400/40 bg-red-500/10'
                                : neuralDetail
                                  ? 'border-violet-400/25 bg-violet-500/[0.06]'
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

function ProductionControlCompact({ data }) {
  const appState = data.appState ?? {};
  const release = appState.release ?? {};
  const cache = appState.cache ?? {};
  const learning = appState.learning_gate ?? {};
  const productionState = appState.production ?? {};
  const lastRun = productionState.last_run ?? {};
  const compactStages = productionState.stages_compact ?? [];
  const [startStatus, setStartStatus] = useState(null);
  const [startError, setStartError] = useState(null);
  const [runStatus, setRunStatus] = useState(lastRun?.run_id ? lastRun : null);
  const [refreshing, setRefreshing] = useState(false);
  const runStages = runStatus?.stages ?? compactStages;
  const displayPhases = buildProductionPhases(runStages?.length ? runStages : productionStageBlueprint);
  const runActive = runStatus?.status === 'starting' || runStatus?.status === 'running';
  const canStart = Boolean(learning.can_start_production) && !runActive;
  const doneStages = (runStages ?? []).filter((stage) => stage.status === 'done').length;
  const runProgress = (runStages ?? []).length ? Math.round((doneStages / runStages.length) * 100) : Number(productionState.progress_pct ?? 0);
  const currentStage = (runStages ?? []).find((stage) => stage.id === runStatus?.current_stage) ?? (runStages ?? []).find((stage) => stage.status === 'running');

  useEffect(() => {
    setRunStatus(lastRun?.run_id ? lastRun : null);
  }, [lastRun?.run_id, lastRun?.status]);

  useEffect(() => {
    if (!runActive) return undefined;
    const timer = setInterval(async () => {
      try {
        const response = await fetch(`${API_BASE}/production/runs/latest`);
        if (!response.ok) return;
        const payload = await response.json();
        setRunStatus(payload.run ?? null);
      } catch {
        // Keep cached run visible; the next refresh can recover.
      }
    }, 4000);
    return () => clearInterval(timer);
  }, [runActive]);

  const refreshCache = async () => {
    setRefreshing(true);
    setStartError(null);
    try {
      const response = await fetch(`${API_BASE}/cache/refresh`, { method: 'POST' });
      if (!response.ok) throw new Error(`API ${response.status}`);
      window.location.reload();
    } catch (err) {
      setStartError(err.message);
    } finally {
      setRefreshing(false);
    }
  };

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

  const readinessTone = release.needs_apply ? 'amber' : learning.can_start_production ? 'emerald' : 'red';
  const gateText = learning.can_start_production ? 'Liberado' : 'Bloqueado';
  const lastRunStatus = runStatus?.status ?? 'sem run';

  return (
    <div className="flex h-full min-h-0 flex-col gap-2 pb-0">
      <Card className="p-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex min-w-0 items-center gap-3">
            <div className="grid h-9 w-9 place-items-center rounded-xl border border-emerald-400/25 bg-emerald-400/10 text-emerald-300">
              <Activity size={17} />
            </div>
            <div className="min-w-0">
              <h2 className="truncate text-lg font-black text-[var(--dash-text)]">CK3 PT-BR Production Control</h2>
              <p className="truncate text-xs text-[var(--dash-muted)]">
                Cache {cache.generated_at ? `atualizado em ${cache.generated_at}` : 'pendente'} · SQLite {cache.stale ? 'mudou desde o cache' : 'sincronizado'}
              </p>
            </div>
          </div>
        </div>

        <div className="mt-3 grid grid-cols-2 gap-2 md:grid-cols-5">
          <MetricTile title="Readiness" value={release.readiness ?? 'unknown'} tone={readinessTone} />
          <MetricTile title="Closed" value={`${pct(release.closed_rate)} · ${compact(release.closed_count)}`} tone="emerald" />
          <MetricTile title="Pending" value={compact(release.pending_count)} tone={release.pending_count ? 'amber' : 'emerald'} />
          <MetricTile title="Needs Apply" value={compact(release.needs_apply)} tone={release.needs_apply ? 'amber' : 'emerald'} />
          <MetricTile title="Learning Gate" value={gateText} tone={learning.can_start_production ? 'emerald' : 'red'} />
        </div>
      </Card>

      <div className="grid min-h-0 flex-1 gap-2 xl:grid-cols-[0.7fr_1.3fr]">
        <Card className="flex min-h-0 flex-col p-2.5">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h3 className="text-sm font-black text-[var(--dash-text)]">Controle</h3>
              <p className="mt-1 text-xs text-[var(--dash-muted)]">{learning.reason || learning.next_action || 'Produção protegida pelo learning gate.'}</p>
            </div>
            <Badge tone={learning.can_start_production ? 'emerald' : 'red'}>{gateText}</Badge>
          </div>

          <button
            onClick={startProduction}
            disabled={!canStart || startStatus === 'checking'}
            className={cn(
              'mt-2 inline-flex h-10 w-full items-center justify-center gap-2 rounded-xl px-4 text-sm font-black transition',
              canStart ? 'bg-blue-600 text-white shadow-lg shadow-blue-600/20 hover:bg-blue-500' : 'bg-red-500/15 text-red-300'
            )}
          >
            <Play size={18} /> {runActive ? 'Run em execução...' : startStatus === 'checking' ? 'Checando...' : 'Start Production Run'}
          </button>

          {(startError || startStatus) && (
            <div className="mt-3 rounded-lg border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-3 text-xs text-[var(--dash-muted)]">
              <span className="font-bold text-[var(--dash-text)]">{statusLabel(startStatus ?? lastRunStatus)}</span>
              {startError && <span> · {startError}</span>}
            </div>
          )}

          <div className="mt-2 grid grid-cols-2 gap-2">
            <MetricTile title="Última run" value={runStatus?.run_id ?? '-'} tone="blue" />
            <MetricTile title="Status" value={statusLabel(lastRunStatus)} tone={statusTone(lastRunStatus)} />
            <MetricTile title="Progresso" value={`${runProgress}%`} tone={runActive ? 'blue' : statusTone(lastRunStatus)} />
            <MetricTile title="Etapa" value={currentStage?.label ?? runStatus?.current_stage ?? '-'} tone="slate" />
          </div>

          <div className="mt-2 min-h-0 flex-1 rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-2.5">
            <h4 className="text-xs font-black uppercase tracking-wide text-[var(--dash-muted)]">Último resultado</h4>
            <p className="mt-1 text-sm font-bold text-[var(--dash-text)]">{runStatus?.message ?? 'Nenhuma run ativa ou recente carregada.'}</p>
            <div className="mt-2 space-y-0.5 text-xs text-[var(--dash-muted)]">
              <p>Segment-state: <span className="font-bold text-[var(--dash-text)]">#{release.latest_segment_state_run_id ?? '-'}</span></p>
              <p>Ledger: <span className="font-bold text-[var(--dash-text)]">#{release.latest_ledger_run_id ?? '-'}</span></p>
              <p>Output coverage: <span className="font-bold text-[var(--dash-text)]">{pct(release.output_coverage)}</span></p>
              {runStatus?.report_path && <p className="truncate">Relatório: <span className="font-bold text-[var(--dash-text)]">{runStatus.report_path}</span></p>}
            </div>
          </div>
        </Card>

        <Card className="flex min-h-0 flex-col p-2.5">
          <div className="mb-2 flex items-center justify-between gap-3">
            <div>
              <h3 className="text-sm font-black text-[var(--dash-text)]">Fluxo de Produção</h3>
              <p className="text-xs text-[var(--dash-muted)]">4 fases compactas; detalhes técnicos ficam nos relatórios.</p>
            </div>
            <Badge tone={runActive ? 'blue' : statusTone(lastRunStatus)}>{statusLabel(lastRunStatus)}</Badge>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-slate-500/20">
            <div className={cn('h-full rounded-full', runStatus?.status === 'failed' ? 'bg-red-500' : runActive ? 'bg-blue-500' : 'bg-emerald-500')} style={{ width: `${Math.max(0, Math.min(100, runProgress))}%` }} />
          </div>
          <div className="mt-2 grid min-h-0 flex-1 grid-rows-2 gap-2 lg:grid-cols-2">
            {displayPhases.map((phase, index) => (
              <div key={phase.id} className="min-h-0 rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-2">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-[10px] font-black uppercase tracking-wide text-[var(--dash-soft)]">Fase {index + 1}/4</p>
                    <h4 className="truncate text-sm font-black text-[var(--dash-text)]">{phase.title}</h4>
                    <p className="mt-1 text-xs leading-snug text-[var(--dash-muted)]">{phase.purpose}</p>
                  </div>
                  <Badge tone={statusTone(phase.status)}>{statusLabel(phase.status)}</Badge>
                </div>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {phase.stages.map((stage) => {
                    const isMl = Boolean(neuralProductionStages[stage.id]);
                    return (
                      <span
                        key={stage.id}
                        title={`${stage.label} · ${productionStageDetails[stage.id] ?? stage.id}`}
                        className={cn(
                          'inline-flex max-w-full items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10px] font-bold',
                          stage.status === 'done'
                            ? 'border-emerald-400/35 bg-slate-100 text-emerald-600 dark:border-emerald-400/25 dark:bg-slate-800/70 dark:text-emerald-300'
                            : stage.status === 'running'
                              ? 'border-blue-400/30 bg-blue-500/10 text-blue-200'
                              : stage.status === 'failed'
                                ? 'border-red-400/30 bg-red-500/10 text-red-200'
                                : isMl
                                  ? 'border-violet-400/25 bg-violet-500/10 text-violet-200'
                                  : 'border-slate-400/15 bg-slate-900/20 text-[var(--dash-muted)]'
                        )}
                      >
                        {isMl && <BrainCircuit size={11} />}
                        <span className="truncate">{stage.label}</span>
                      </span>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}

const dashboardViewItems = [
  { id: 'overview', label: 'Visao Geral', subtitle: 'Acompanha fechamento, pendencias e estado geral do pacote.' },
  { id: 'learning', label: 'Aprendizado', subtitle: 'Mostra gate de aprendizado, evidencias e progresso dos ciclos.' },
  { id: 'pending', label: 'Pendencias', subtitle: 'Prioriza gargalos e grupos que ainda travam fechamento.' },
  { id: 'release', label: 'Release', subtitle: 'Resume prontidao, execucao recente e checklist de publicacao.' },
  { id: 'quality', label: 'Qualidade', subtitle: 'Compara modelo ativo, qualidade e riscos do conjunto.' },
  { id: 'network', label: 'Network', subtitle: 'Visualiza a arquitetura neuro-simbolica, agentes e ligacoes.' },
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
  const modelTrend = mlPerformance.mlTrendByModel ?? mlPerformance.mlTrend ?? [];
  const datasetComposition = mlPerformance.datasetComposition ?? [];
  const outputEvolution = lifecycle.outputApply?.evolution ?? [];
  const packageBacklog = lifecycle.packageBacklog ?? [];
  const tokenBuckets = lifecycle.tokenPolicy?.bucketDistribution ?? [];

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

  const latestModel = modelTrend.at(-1) ?? {};
  const previousModel = modelTrend.at(-2) ?? {};
  const pendingDistribution = [
    { name: 'Fechados', value: Number(release.closed_count ?? summary.closed_segments ?? 0), color: '#10b981' },
    { name: 'Pendentes', value: Number(release.pending_count ?? summary.raw_pending ?? 0), color: '#f59e0b' },
    { name: 'Needs apply', value: Number(release.needs_apply ?? summary.needs_apply ?? 0), color: '#3b82f6' },
  ].filter((item) => item.value > 0);
  const releaseTrend = outputEvolution.slice(-10).map((row) => ({
    run: `#${row.state_run_id ?? row.run_id ?? '-'}`,
    fechado: Number(row.closed_pct ?? row.closed_rate ?? release.closed_rate ?? 0),
    pendencias: Number(row.pending_count ?? 0),
    apply: Number(row.output_apply_pending_count ?? 0),
  }));
  const learningMix = datasetComposition.length
    ? datasetComposition.slice(0, 8).map((row) => ({
      label: row.label ?? row.name ?? row.decision ?? 'dataset',
      value: Number(row.value ?? row.count ?? row.total ?? 0),
    }))
    : [
      { label: 'agentes', value: Number(agentSummary.registered_agents ?? agentSummary.total_agents ?? 0) },
      { label: 'operacionais', value: Number(agentSummary.operational_agents ?? agentSummary.active_agents ?? 0) },
      { label: 'evidencias', value: Number(agentSummary.recommendation_evidence ?? 0) },
    ].filter((row) => row.value > 0);
  const pendingHotspots = (packageBacklog.length ? packageBacklog : tokenBuckets).slice(0, 8).map((row) => ({
    label: row.package_name ?? row.package ?? row.relative_path ?? row.policy_bucket ?? row.name ?? 'grupo',
    value: Number(row.pending_count ?? row.pending ?? row.total ?? row.count ?? 0),
  })).filter((row) => row.value > 0);
  const qualityCards = [
    { label: 'Modelo atual', value: latestModel.modelVersion ?? mlPerformance.kpis?.active_model ?? 'pendente', tone: 'blue' },
    { label: 'Macro F1', value: latestModel.macroF1 != null ? pctMetric(latestModel.macroF1) : pct(mlPerformance.kpis?.macro_f1), tone: 'emerald' },
    { label: 'Falso seguro', value: compact(agentSummary.operational_false_safe ?? mlPerformance.kpis?.false_safe ?? 0), tone: Number(agentSummary.operational_false_safe ?? 0) ? 'red' : 'emerald' },
    { label: 'Ganho rede', value: compact(agentSummary.ensemble_gain ?? agentSummary.active_gate_guarded_releases ?? 0), tone: 'violet' },
  ];
  const releaseChecklist = [
    { label: 'Cache local', ok: !appState.cache?.stale, detail: appState.cache?.generated_at ?? 'nao gerado' },
    { label: 'Learning gate', ok: Boolean(learning.can_start_production), detail: learning.reason ?? learning.status ?? '-' },
    { label: 'Needs apply', ok: Number(release.needs_apply ?? 0) === 0, detail: fmt(release.needs_apply) },
    { label: 'Output coverage', ok: Number(release.output_coverage ?? 0) > 99, detail: pct(release.output_coverage) },
    { label: 'Run ativa', ok: !production.active, detail: production.active ? production.current_stage ?? 'running' : 'livre' },
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
            <MetricTile title="Output" value={pct(release.output_coverage)} tone="blue" />
            <MetricTile title="Segment-state" value={`#${release.latest_segment_state_run_id ?? '-'}`} tone="slate" />
            <MetricTile title="Gate" value={learning.can_start_production ? 'Liberado' : 'Bloqueado'} tone={learning.can_start_production ? 'emerald' : 'red'} />
          </div>
          <div className="grid min-h-0 flex-1 gap-4 xl:grid-cols-[1.25fr_0.75fr]">
            <Card className="flex min-h-0 flex-col p-5">
              <div className="mb-4 flex shrink-0 items-start justify-between gap-3">
                <div>
                  <h3 className="text-sm font-black text-[var(--dash-text)]">Evolucao de fechamento</h3>
                  <p className="text-xs text-[var(--dash-muted)]">Leitura macro por runs de segment-state.</p>
                </div>
                <Badge tone={release.pending_count ? 'amber' : 'emerald'}>{release.readiness ?? 'status'}</Badge>
              </div>
              <div className="min-h-0 flex-1">
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart data={releaseTrend.length ? releaseTrend : [{ run: `#${release.latest_segment_state_run_id ?? '-'}`, fechado: release.closed_rate, pendencias: release.pending_count, apply: release.needs_apply }]}>
                    <CartesianGrid strokeDasharray="3 3" stroke="currentColor" opacity={0.08} />
                    <XAxis dataKey="run" tick={chartText} />
                    <YAxis yAxisId="left" tick={chartText} />
                    <YAxis yAxisId="right" orientation="right" tick={chartText} />
                    <Tooltip content={<ModelTooltip />} />
                    <Bar yAxisId="right" dataKey="pendencias" name="Pendencias" fill="#f59e0b" radius={[5, 5, 0, 0]} />
                    <Line yAxisId="left" type="monotone" dataKey="fechado" name="Fechado %" stroke="#10b981" strokeWidth={3} dot={false} />
                    <Line yAxisId="right" type="monotone" dataKey="apply" name="Needs apply" stroke="#3b82f6" strokeWidth={2} dot={false} />
                  </ComposedChart>
                </ResponsiveContainer>
              </div>
            </Card>
            <Card className="flex min-h-0 flex-col p-5">
              <div className="shrink-0">
                <h3 className="text-sm font-black text-[var(--dash-text)]">Distribuicao atual</h3>
                <p className="mt-1 text-xs text-[var(--dash-muted)]">Fechado, pendente e aplicacao pendente.</p>
              </div>
              <div className="mt-4 min-h-0 flex-1">
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart>
                    <Pie data={pendingDistribution} innerRadius={58} outerRadius={92} dataKey="value" nameKey="name" paddingAngle={2}>
                      {pendingDistribution.map((entry) => <Cell key={entry.name} fill={entry.color} />)}
                    </Pie>
                    <Tooltip formatter={(value) => fmt(value)} />
                    <Legend />
                  </PieChart>
                </ResponsiveContainer>
              </div>
            </Card>
          </div>
        </>
      )}

      {view === 'learning' && (
        <div className="grid min-h-0 flex-1 gap-4 xl:grid-cols-[0.75fr_1.25fr]">
          <Card className="p-5">
            <h3 className="text-sm font-black text-[var(--dash-text)]">Estado do aprendizado</h3>
            <div className="mt-4 grid gap-3">
              <MetricTile title="Gate" value={learning.can_start_production ? 'Liberado' : 'Em treino'} tone={learning.can_start_production ? 'emerald' : 'amber'} />
              <MetricTile title="Fase atual" value={learning.current_phase_label ?? learning.status ?? '-'} tone="blue" />
              <MetricTile title="Progresso" value={learning.progress_pct != null ? `${learning.progress_pct}%` : '-'} tone="slate" />
              <MetricTile title="Proxima acao" value={learning.next_action ?? 'monitorar'} tone="violet" />
            </div>
          </Card>
          <Card className="flex min-h-0 flex-col p-5">
            <h3 className="text-sm font-black text-[var(--dash-text)]">Evidencia de aprendizado</h3>
            <div className="mt-4 min-h-0 flex-1">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={learningMix}>
                  <CartesianGrid strokeDasharray="3 3" stroke="currentColor" opacity={0.08} />
                  <XAxis dataKey="label" tick={chartText} interval={0} angle={-12} textAnchor="end" height={70} />
                  <YAxis tick={chartText} />
                  <Tooltip formatter={(value) => fmt(value)} />
                  <Bar dataKey="value" fill="#8b5cf6" radius={[5, 5, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </Card>
        </div>
      )}

      {view === 'pending' && (
        <div className="grid min-h-0 flex-1 gap-4 xl:grid-cols-[1.15fr_0.85fr]">
          <Card className="flex min-h-0 flex-col p-5">
            <h3 className="text-sm font-black text-[var(--dash-text)]">Gargalos restantes</h3>
            <p className="mt-1 text-xs text-[var(--dash-muted)]">Top grupos para decidir o proximo neuroniozinho ou politica.</p>
            <div className="mt-4 min-h-0 flex-1">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={pendingHotspots} layout="vertical" margin={{ left: 24 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="currentColor" opacity={0.08} />
                  <XAxis type="number" tick={chartText} />
                  <YAxis type="category" dataKey="label" tick={chartText} width={170} />
                  <Tooltip formatter={(value) => fmt(value)} />
                  <Bar dataKey="value" fill="#f59e0b" radius={[0, 5, 5, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </Card>
          <Card className="p-5">
            <h3 className="text-sm font-black text-[var(--dash-text)]">Leitura operacional</h3>
            <div className="mt-4 space-y-3">
              <MetricTile title="Pendencia bruta" value={compact(release.pending_count)} tone="amber" />
              <MetricTile title="Watch ML" value={compact(summary.model_suspicion_watch ?? 0)} tone="violet" />
              <MetricTile title="Ponte pendente" value={compact(summary.governed_bridge_pending ?? 0)} tone="blue" />
              <MetricTile title="Acionavel" value={compact(summary.actionable_pending ?? 0)} tone={(summary.actionable_pending ?? 0) ? 'amber' : 'emerald'} />
            </div>
          </Card>
        </div>
      )}

      {view === 'release' && (
        <div className="grid min-h-0 flex-1 gap-4 xl:grid-cols-[0.85fr_1.15fr]">
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
        <div className="grid min-h-0 flex-1 gap-4 xl:grid-cols-[0.75fr_1.25fr]">
          <Card className="p-5">
            <h3 className="text-sm font-black text-[var(--dash-text)]">Qualidade do conjunto</h3>
            <div className="mt-4 grid gap-3">
              {qualityCards.map((item) => <MetricTile key={item.label} title={item.label} value={item.value} tone={item.tone} />)}
            </div>
          </Card>
          <Card className="p-5">
            <h3 className="text-sm font-black text-[var(--dash-text)]">Comparacao de modelos</h3>
            <div className="mt-4 grid grid-cols-2 gap-3">
              <MetricTile title="Atual" value={latestModel.modelVersion ?? '-'} tone="blue" />
              <MetricTile title="Anterior" value={previousModel.modelVersion ?? '-'} tone="slate" />
              <MetricTile title="F1 atual" value={latestModel.macroF1 != null ? pctMetric(latestModel.macroF1) : '-'} tone="emerald" />
              <MetricTile title="F1 anterior" value={previousModel.macroF1 != null ? pctMetric(previousModel.macroF1) : '-'} tone="slate" />
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}

function DashboardTabs({ value, onChange }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[var(--dash-border)] bg-[var(--dash-card)] p-3">
      <div>
        <h2 className="text-lg font-black text-[var(--dash-text)]">Project Intelligence</h2>
        <p className="text-xs text-[var(--dash-muted)]">Uma visao unica para progresso, aprendizado, qualidade, release e rede.</p>
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
  return labels[value] ?? String(value).replaceAll('_', ' ');
};

const ptStatus = (value) => ({
  active: 'ativo',
  authoritative: 'autoritario',
  candidate: 'candidato',
  experimental: 'experimental',
  experimental_watch: 'watch experimental',
  growing: 'em crescimento',
  guarded: 'protegido',
  learning: 'aprendendo',
  operational: 'operacional',
  planned: 'planejado',
  promising: 'promissor',
  safe: 'seguro',
  shadow: 'shadow',
  stable: 'estavel',
}[value] ?? value);

const ptType = (value) => ({
  composition_coordinator: 'compositor',
  coordinator: 'coordenador',
  guard: 'guarda',
  input: 'entrada',
  issue_memory: 'memoria de problemas',
  lifecycle_policy: 'politica lifecycle',
  lifecycle_state: 'estado lifecycle',
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
}[value] ?? String(value).replaceAll('_', ' '));

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
  semantic_triage: 'Organiza pendencias semanticas.',
  short_ui_label_specialist: 'Cuida de labels curtos de interface.',
  safe_output_writer: 'Escreve output apenas pelo fluxo protegido.',
  trusted_evidence_store: 'Guarda evidencias confiaveis e revisoes.',
  dynamic_expression_specialist: 'Analisa expressoes dinamicas do CK3.',
  gender_token_specialist: 'Valida genero, artigos e tokens relacionados.',
}[value] ?? value);

const ptSentence = (value) => {
  if (!value) return '';
  const text = String(value);
  const translations = {
    'Routes segments and issues to macro, specialists, subagents and lifecycle gates. It organizes evidence rather than allowing each agent to close output alone.':
      'Roteia segmentos e problemas para o macro, especialistas, subagentes e gates de lifecycle. Organiza evidencias sem permitir que um agente feche output sozinho.',
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
  return translations[text] ?? text;
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
    'issue ledger': 'historico de problemas',
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
  return translations[key] ?? ptSentence(text);
};

const summarizeMetrics = (metrics) => {
  if (!metrics) return [];
  if (Array.isArray(metrics)) return metrics.slice(0, 6).map(String);
  return Object.entries(metrics)
    .slice(0, 6)
    .map(([key, value]) => `${ptFieldLabel(key)}: ${typeof value === 'number' ? metric(value) : value}`);
};

const asList = (value) => {
  if (!value) return [];
  return Array.isArray(value) ? value.map(String) : [String(value)];
};

const ATLAS_LAYOUT_STORAGE_KEY = 'ck3_ptbr_neural_atlas_layout_v1';
const DASHBOARD_THEME_STORAGE_KEY = 'ck3_ptbr_dashboard_theme';
const ATLAS_DEFAULT_NODE_SIZE = { w: 168, h: 54 };

const fallbackPosition = (node, index, siblingCount) => {
  const x = atlasFamilyColumns[node.family] ?? atlasFamilyColumns[node.type] ?? 50;
  const spread = Math.min(72, Math.max(28, siblingCount * 13));
  const start = 50 - spread / 2;
  return { x, y: Math.max(12, Math.min(90, start + index * 13)) };
};

const normalizeNeuralAtlas = (source) => {
  const nodes = source?.nodes ?? neuralAtlasBlueprint.nodes;
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
      label: node.label ?? node.id,
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
  const normalizedEdges = (source?.edges ?? neuralAtlasBlueprint.edges).map((edge) => {
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
  const atlas = useMemo(() => normalizeNeuralAtlas(networkSource ?? neuralAtlasBlueprint), [networkSource]);
  const sourceNetwork = networkSource ?? neuralAtlasBlueprint;
  const versionInfo = sourceNetwork.version_info ?? {};
  const registryNodes = sourceNetwork.nodes ?? [];
  const atlasSummary = {
    agents_total: registryNodes.length,
    agents_operational: registryNodes.filter((node) => ['active', 'operational'].includes(node.status) || ['operational', 'authoritative'].includes(node.operational_state)).length,
    experimental_subagents: registryNodes.filter((node) => node.type === 'subagent' || node.agent_type === 'subspecialist' || node.status === 'experimental').length,
    operational_false_safe: versionInfo.current_macro_model?.false_safe_count ?? 0,
    ...dashboardSummary,
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

  const effectiveSelectedId = atlas.nodes.some((node) => node.id === selectedId) ? selectedId : null;
  const selectedNode = atlas.nodes.find((node) => node.id === effectiveSelectedId) ?? null;
  const focusId = hoveredId ?? effectiveSelectedId;

  useEffect(() => {
    let alive = true;
    const loadNetwork = async () => {
      try {
        const response = await fetch(`${API_BASE}/neural-visualization`);
        if (!response.ok) throw new Error(`API ${response.status}`);
        const payload = await response.json();
        if (!alive) return;
        setNetworkSource({ ...(payload.network ?? {}), sourcePath: payload.sourcePath });
        setNetworkError(null);
      } catch (err) {
        if (!alive) return;
        setNetworkError(err.message);
      }
    };
    loadNetwork();
    return () => {
      alive = false;
    };
  }, []);

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

  return (
    <div className="neural-atlas-shell h-full min-h-0 pb-0">
      <style>{`
        .neural-atlas-shell {
          --atlas-bg: #050814;
          --atlas-panel: rgba(9, 16, 35, 0.78);
          --atlas-line: rgba(147, 197, 253, 0.30);
          --atlas-text: #f8fafc;
          --atlas-muted: #9fb3d1;
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
        [data-content-theme='light'] .neural-atlas-shell {
          --atlas-text: #172033;
          --atlas-muted: #64748b;
        }
        [data-content-theme='light'] .atlas-frame {
          background: #f1f5f9;
          border-color: rgba(148, 163, 184, 0.34);
          box-shadow: 0 24px 80px rgba(15, 23, 42, 0.12);
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
        [data-content-theme='light'] .atlas-title-card,
        [data-content-theme='light'] .atlas-toolbar,
        [data-content-theme='light'] .atlas-metrics-panel {
          background: rgba(255, 255, 255, 0.76);
          border-color: rgba(148, 163, 184, 0.30);
          color: #172033;
          box-shadow: 0 14px 42px rgba(15, 23, 42, 0.08);
        }
        [data-content-theme='light'] .atlas-title-card h2 {
          color: #172033;
        }
        [data-content-theme='light'] .atlas-toolbar button {
          color: #334155;
        }
        [data-content-theme='light'] .atlas-toolbar button:hover {
          background: rgba(59, 130, 246, 0.08);
          color: #111827;
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
        [data-content-theme='light'] .atlas-detail-scroll {
          background: rgba(255, 250, 241, 0.94);
          border-color: rgba(106, 84, 55, 0.18);
          box-shadow: 0 24px 80px rgba(81, 67, 45, 0.18);
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

      <section className="atlas-frame h-full min-h-0 overflow-hidden rounded-[2rem] border border-white/10 bg-[#050814] shadow-[0_30px_120px_rgba(0,0,0,0.42)]">
        <div className="relative atlas-stage h-full min-h-[520px] overflow-hidden p-3" ref={canvasRef} onPointerMove={dragNode} onPointerUp={stopDrag} onPointerCancel={stopDrag} onClick={() => setSelectedId(null)}>
            <div className="relative z-10 flex flex-col gap-2 lg:flex-row lg:items-start lg:justify-between">
              <div className="flex items-center gap-2">
                <div className="atlas-title-card rounded-2xl border border-white/10 bg-black/15 px-3 py-2 backdrop-blur">
                  <h2 className="text-lg font-black tracking-tight text-white lg:text-xl">
                    Network Tradução CKIII
                  </h2>
                  {networkError && (
                    <p className="mt-1 text-xs font-bold text-amber-100">
                      fallback: {networkError}
                    </p>
                  )}
                </div>
                <div className="atlas-toolbar flex rounded-2xl border border-white/10 bg-black/20 p-1 backdrop-blur">
                  <button
                    type="button"
                    title="Voltar para o layout padrão"
                    onClick={resetAtlasLayout}
                    className="grid h-9 w-9 place-items-center rounded-xl text-slate-200 transition hover:bg-white/10 hover:text-white"
                  >
                    <Home size={16} />
                  </button>
                  <button
                    type="button"
                    title={layoutSaved ? 'Layout favorito salvo' : 'Salvar layout favorito'}
                    onClick={saveAtlasLayout}
                    className={cn(
                      'grid h-9 w-9 place-items-center rounded-xl transition hover:bg-white/10',
                      layoutSaved ? 'text-amber-200' : 'text-slate-200 hover:text-white'
                    )}
                  >
                    <Star size={16} fill={layoutSaved ? 'currentColor' : 'none'} />
                  </button>
                </div>
              </div>
              <div className="atlas-metrics-panel grid w-full grid-cols-2 gap-2 rounded-2xl border border-white/10 bg-black/20 p-2 backdrop-blur sm:grid-cols-4 lg:w-auto lg:min-w-[430px]">
                <MetricTile title="Agents" value={fmt(atlasSummary.agents_total)} tone="blue" />
                <MetricTile title="Operational" value={fmt(atlasSummary.agents_operational)} tone="emerald" />
                <MetricTile title="Lab Subagents" value={fmt(atlasSummary.experimental_subagents)} tone="amber" />
                <MetricTile title="False Safe Op." value={fmt(atlasSummary.operational_false_safe ?? 0)} tone={(atlasSummary.operational_false_safe ?? 0) ? 'red' : 'emerald'} />
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
                return (
                  <button
                    key={node.id}
                    type="button"
                    title={`${node.label}\n${ptType(node.type)}`}
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
                        <h3 className="truncate text-[0.82rem] font-black leading-tight text-white">{node.label}</h3>
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
                <p className="text-xs font-black uppercase tracking-wide text-cyan-200">{selectedNode.family}</p>
                <h3 className="mt-2 text-2xl font-black text-white">{selectedNode.label}</h3>
              </div>
              <Badge tone={selectedNode.tone}>{ptStatus(selectedNode.status)}</Badge>
            </div>
            <p className="mt-4 text-sm leading-6 text-slate-300">{selectedNode.description}</p>
            <div className="mt-5 rounded-2xl border border-white/10 bg-white/[0.04] p-4">
              <p className="text-xs font-black uppercase tracking-wide text-slate-400">Funcao</p>
              <p className="mt-2 text-sm leading-6 text-white">{selectedNode.role}</p>
            </div>
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
                  <p className="text-xs font-black uppercase tracking-wide text-slate-400">Saidas</p>
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
                <p className="text-xs font-black uppercase tracking-wide text-red-100">Atencao</p>
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
              <p className="text-xs font-black uppercase tracking-wide text-amber-100">Proximo aprendizado</p>
              <div className="mt-2 space-y-2 text-sm leading-6 text-amber-50">
                {(selectedNode.next_steps?.length ? selectedNode.next_steps : ['Aguardando proxima definicao do chat 1.']).slice(0, 4).map((item) => (
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
  Dashboard: { title: 'Project Intelligence', subtitle: 'Visao geral, aprendizado, pendencias, release, qualidade e Network' },
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
  const [refreshingCache, setRefreshingCache] = useState(false);
  const needsFullDashboard = activeTab !== 'Production' && !(activeTab === 'Dashboard' && dashboardView === 'network');

  useEffect(() => {
    let alive = true;
    const loadAppState = async () => {
      try {
        const response = await fetch(`${API_BASE}/app-state`);
        if (!response.ok) throw new Error(`API ${response.status}`);
        const payload = await response.json();
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

  const refreshCache = async () => {
    setRefreshingCache(true);
    try {
      const response = await fetch(`${API_BASE}/cache/refresh`, { method: 'POST' });
      if (!response.ok) throw new Error(`API ${response.status}`);
      const payload = await response.json();
      setAppState(payload.app_state ?? null);
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setRefreshingCache(false);
    }
  };

  const ActiveScreen = screens[activeTab];
  const currentScreen = screenMeta[activeTab];
  const activeDashboardItem = dashboardViewItems.find((item) => item.id === dashboardView) ?? dashboardViewItems[0];
  const headerSubtitle = activeTab === 'Dashboard' ? activeDashboardItem.subtitle : currentScreen.subtitle;
  const showOperationalNav = operationalNavItems.includes(activeTab);
  const canRenderWithoutApi = activeTab === 'Neural Network' || activeTab === 'Network';
  const canRenderWithAppState = activeTab === 'Production' || activeTab === 'Dashboard';
  const screenData = { ...(data ?? { agents: { summary: {} } }), appState: appState ?? {}, _fullDashboardLoaded: Boolean(data) };
  const isWaitingForData = !data && !error && !canRenderWithoutApi && !(canRenderWithAppState && appState);
  const canRenderScreen = Boolean(data || canRenderWithoutApi || (canRenderWithAppState && appState));

  return (
    <div className="h-screen overflow-hidden">
      <style>{`
        [data-content-theme='light'] {
          --dash-bg: rgb(241 245 249);
          --dash-card: rgba(255, 255, 255, 0.96);
          --dash-border: rgba(148, 163, 184, 0.32);
          --dash-text: rgb(15 23 42);
          --dash-muted: rgb(71 85 105);
          --dash-soft: rgb(100 116 139);
          --dash-subtle: rgba(248, 250, 252, 0.92);
          --dash-shadow: 0 14px 36px rgba(15, 23, 42, 0.08);
        }
        [data-content-theme='dark'] {
          --dash-bg: rgb(5 12 24);
          --dash-card: rgba(13, 22, 39, 0.78);
          --dash-border: rgba(255, 255, 255, 0.10);
          --dash-text: rgb(255 255 255);
          --dash-muted: rgb(158 179 207);
          --dash-soft: rgb(105 124 149);
          --dash-subtle: rgba(255, 255, 255, 0.05);
          --dash-shadow: 0 18px 60px rgba(0, 0, 0, 0.18);
        }
      `}</style>

      <div
        data-content-theme={isDarkMode ? 'dark' : 'light'}
        className={`${isDarkMode ? 'dark ' : ''}h-screen w-full overflow-hidden bg-[var(--dash-bg)] p-4 text-[var(--dash-text)] transition-colors [&_button]:cursor-pointer`}
      >
        <main className="mx-auto flex h-full max-w-[1920px] flex-col overflow-hidden">
          <header className="grid min-h-[64px] shrink-0 grid-cols-12 items-center gap-4 rounded-2xl border border-[var(--dash-border)] bg-[var(--dash-card)] px-4 py-2 shadow-[var(--dash-shadow)]">
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
                <nav className="inline-flex w-fit flex-wrap items-center gap-1 rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-1">
                  {dashboardViewItems.map((item) => (
                    <button
                      key={item.id}
                      onClick={() => selectDashboardView(item.id)}
                      className={cn(
                        'h-8 rounded-lg px-3 text-xs font-black transition',
                        dashboardView === item.id
                          ? 'bg-blue-600 text-white shadow-lg shadow-blue-600/20 dark:bg-blue-500/20 dark:text-blue-200 dark:shadow-none'
                          : 'text-[var(--dash-muted)] hover:bg-[var(--dash-subtle)] hover:text-[var(--dash-text)]'
                      )}
                    >
                      {item.label}
                    </button>
                  ))}
                </nav>
              ) : showOperationalNav && (
                <nav className="inline-flex w-fit flex-wrap items-center gap-1 rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-1">
                  <button
                    onClick={() => openDashboardTab('Dashboard/overview')}
                    className="grid h-8 w-8 place-items-center rounded-lg bg-blue-600 text-white shadow-lg shadow-blue-600/20 transition hover:bg-blue-500 dark:bg-blue-500/20 dark:text-blue-200 dark:shadow-none"
                    title="Abrir Dashboard em nova guia"
                    aria-label="Abrir Dashboard em nova guia"
                  >
                    <LayoutDashboard size={15} />
                  </button>
                </nav>
              )}
              <button
                onClick={refreshCache}
                disabled={refreshingCache}
                className="grid h-10 w-10 place-items-center rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] text-[var(--dash-text)] transition hover:bg-blue-500/10 disabled:opacity-60"
                title="Atualizar cache dos dados"
                aria-label="Atualizar cache dos dados"
              >
                <RefreshCw size={18} className={refreshingCache ? 'animate-spin' : ''} />
              </button>
              <button
                onClick={() => setIsDarkMode((current) => !current)}
                className="grid h-10 w-10 place-items-center rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] text-[var(--dash-text)] transition hover:bg-blue-500/10"
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
            {canRenderScreen && <ActiveScreen data={screenData} />}
          </div>
        </main>
      </div>
    </div>
  );
}

createRoot(document.getElementById('root')).render(
  <DashboardErrorBoundary>
    <App />
  </DashboardErrorBoundary>
);
