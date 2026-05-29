import React, { useEffect, useState } from 'react';
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
  Layers3,
  LayoutDashboard,
  Lock,
  Moon,
  PackageSearch,
  Play,
  Route,
  Scale,
  Rocket,
  SearchCheck,
  ShieldAlert,
  ShieldCheck,
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
  <div className={`rounded-2xl border border-[var(--dash-border)] bg-[var(--dash-card)] shadow-[0_18px_60px_rgba(0,0,0,0.18)] ${className}`}>
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
  <div className={`rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-3 ${className}`}>
    <p className="text-[0.68rem] font-semibold uppercase tracking-wide text-[var(--dash-muted)]">{title}</p>
    <p className={`mt-1 text-lg font-black ${tone === 'emerald' ? 'text-emerald-400' : tone === 'red' ? 'text-red-400' : tone === 'blue' ? 'text-blue-400' : tone === 'amber' ? 'text-amber-400' : 'text-[var(--dash-text)]'}`}>{value}</p>
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
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5">
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
        <StatCard title="Pendência Operacional" value={compact(summary.pending_count)} detail={pct(summary.pending_pct)} icon={AlertCircle} color="amber" />
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
                      <Cell key={entry.name} fill={entry.group === 'closed' ? '#10b981' : '#f59e0b'} />
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
  queued_visual_stub: 'Visual queue',
}[status] ?? status ?? 'Unknown');

const openDashboardTab = (tab) => {
  const target = `${window.location.origin}${window.location.pathname}#${encodeURIComponent(tab)}`;
  window.open(target, '_blank', 'noopener,noreferrer');
};

function ProductionControl({ data }) {
  const production = data.production ?? {};
  const summary = production.summary ?? {};
  const readiness = production.readiness ?? {};
  const lock = production.lock ?? {};
  const learning = production.learning ?? {};
  const [startStatus, setStartStatus] = useState(null);
  const [startError, setStartError] = useState(null);
  const [runStatus, setRunStatus] = useState(production.run ?? null);
  const runStages = runStatus?.stages ?? [];
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
            </div>
          </div>

          <div className="mt-6 grid grid-cols-1 gap-3 md:grid-cols-4">
            <MetricTile title="Readiness" value={statusLabel(readiness.status)} tone={statusTone(readiness.status)} />
            <MetricTile title="Closed" value={pct(readiness.closed_pct)} tone="emerald" />
            <MetricTile title="Pending" value={compact(readiness.pending_operational)} tone="amber" />
            <MetricTile title="Needs Apply" value={compact(summary.needs_apply)} tone={summary.needs_apply ? 'amber' : 'emerald'} />
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

      {runStatus?.run_id && (
        <ChartCard title="Execucao Atual" subtitle="Log e relatorio do executor seguro de producao.">
          <div className="grid gap-3 lg:grid-cols-[0.8fr_1.2fr]">
            <div className="space-y-3">
              <MetricTile title="Run" value={runStatus.run_id} tone="blue" />
              <MetricTile title="Status" value={statusLabel(runStatus.status)} tone={statusTone(runStatus.status)} />
              <MetricTile title="Modo" value={runStatus.mode ?? 'safe'} tone="emerald" />
              <MetricTile title="Etapa Atual" value={currentRunStage?.label ?? runStatus.current_stage ?? '-'} tone={runActive ? 'blue' : statusTone(runStatus.status)} />
              <MetricTile title="Progresso" value={`${runProgress}%`} tone={runActive ? 'blue' : statusTone(runStatus.status)} />
              <MetricTile title="Relatorio" value={runStatus.report_path ? 'gerado' : 'pendente'} tone={runStatus.report_path ? 'emerald' : 'amber'} />
            </div>
            <div className="rounded-xl border border-[var(--dash-border)] bg-slate-950/60 p-4">
              <div className="mb-3 flex items-center justify-between gap-3">
                <p className="text-sm font-bold text-white">{runStatus.message ?? 'Aguardando...'}</p>
                <Badge tone={statusTone(runStatus.status)}>{statusLabel(runStatus.status)}</Badge>
              </div>
              <div className="mb-4 h-2 overflow-hidden rounded-full bg-slate-500/20">
                <div className={cn('h-full rounded-full', runStatus.status === 'failed' ? 'bg-red-500' : 'bg-blue-500')} style={{ width: `${runProgress}%` }} />
              </div>
              {runStages.length > 0 && (
                <div className="mb-4 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
                  {runStages.map((stage) => (
                    <div key={stage.id} className={cn(
                      'rounded-lg border p-3',
                      stage.status === 'running'
                        ? 'border-blue-400/50 bg-blue-500/10'
                        : stage.status === 'done'
                          ? 'border-emerald-400/30 bg-emerald-500/10'
                          : stage.status === 'failed'
                            ? 'border-red-400/40 bg-red-500/10'
                            : 'border-white/10 bg-white/[0.03]'
                    )}>
                      <div className="flex items-center justify-between gap-2">
                        <p className="truncate text-xs font-bold text-white">{stage.label}</p>
                        <Badge tone={statusTone(stage.status)}>{statusLabel(stage.status)}</Badge>
                      </div>
                      <p className="mt-1 truncate text-[11px] text-slate-400">{stage.id}</p>
                      {stage.metrics && (
                        <div className="mt-2 flex flex-wrap gap-1 text-[10px] font-semibold text-slate-300">
                          {Object.entries(stage.metrics).map(([key, value]) => (
                            <span key={key} className="rounded-md bg-slate-900/60 px-1.5 py-0.5">{key}: {fmt(value)}</span>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
              <div className="mb-4 grid gap-2 text-xs text-slate-400 md:grid-cols-2">
                {runStatus.snapshot_path && <p className="truncate"><span className="font-bold text-slate-200">Snapshot:</span> {runStatus.snapshot_path}</p>}
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
            </div>
          </div>
        </ChartCard>
      )}
    </div>
  );
}

function Managerial({ data }) {
  const production = data.production ?? {};
  const summary = production.summary ?? {};
  const lifecycle = data.lifecycle ?? {};
  const packages = lifecycle.packageBacklog ?? [];
  return (
    <div className="flex flex-col gap-4 pb-3">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        <StatCard title="Progresso Geral" value={pct(summary.closed_pct)} detail={`${fmt(summary.closed_segments)} consolidados`} icon={Rocket} color="emerald" />
        <StatCard title="Pendente Operacional" value={compact(summary.pending_operational)} detail="revisao, autofix ou aprendizado" icon={AlertCircle} color="amber" />
        <StatCard title="Output Aplicado" value={compact(summary.applied)} detail={`${fmt(summary.needs_apply)} ainda pendente`} icon={CheckCircle2} color="blue" />
        <StatCard title="Bloqueio Critico" value={fmt(summary.blocked_critical)} detail="invalid releases no gate ativo" icon={ShieldAlert} color={summary.blocked_critical ? 'red' : 'emerald'} />
      </div>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <ChartCard title="Release Readiness" subtitle="Resumo gerencial da versao atual." className="xl:col-span-2">
          <div className="grid gap-3 md:grid-cols-2">
            {[
              ['Source ativo', summary.active_segments, 'blue'],
              ['Consolidado', summary.closed_segments, 'emerald'],
              ['Needs apply', summary.needs_apply, 'amber'],
              ['Valid blanks', summary.valid_blank, 'slate'],
              ['Intentional blanks', summary.intentional_blank, 'slate'],
              ['Pendencia operacional', summary.pending_operational, 'amber'],
            ].map(([title, value, tone]) => (
              <MetricTile key={title} title={title} value={fmt(value)} tone={tone} />
            ))}
          </div>
        </ChartCard>
        <ChartCard title="Top Pacotes" subtitle="Onde a producao ainda trava.">
          <div className="max-h-[330px] overflow-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs uppercase text-[var(--dash-muted)]">
                <tr><th className="py-2">Pacote</th><th className="py-2 text-right">Pending</th><th className="py-2 text-right">Apply</th></tr>
              </thead>
              <tbody>
                {packages.slice(0, 10).map((row) => (
                  <tr key={row.package ?? row.relative_path} className="border-t border-[var(--dash-border)]">
                    <td className="max-w-[220px] truncate py-2 font-semibold text-[var(--dash-text)]">{row.package ?? row.relative_path}</td>
                    <td className="py-2 text-right text-amber-300">{fmt(row.pending_count ?? row.pending)}</td>
                    <td className="py-2 text-right text-blue-300">{fmt(row.output_apply_pending ?? row.needs_apply)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </ChartCard>
      </div>
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

function NeuralArchitecture({ data }) {
  const agents = data.agents ?? {};
  const summary = agents.summary ?? {};
  const nodes = [
    ['Guards', 'Tokens, chaves, locked human', ShieldCheck, 'authoritative'],
    ['Trusted Memory', 'Confirmacoes e output testado', Database, 'knowledge'],
    ['General Macro', 'Classificador amplo', BrainCircuit, 'model'],
    ['Coordinator', 'Roteia e arbitra votos', Route, 'router'],
    ['Specialists', 'Familias e subfamilias', Layers3, 'experts'],
    ['Guarded Policies', 'Overlay, checkpoint e sombra', GitBranch, 'policy'],
    ['Output Apply', 'Dry-run, backup e escrita', TerminalSquare, 'apply'],
    ['Feedback', 'Jogo, comunidade e revisao', Activity, 'learning'],
  ];
  return (
    <div className="flex flex-col gap-4 pb-3">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
        <StatCard title="Agentes" value={fmt(summary.agents_total)} detail={`${fmt(summary.agents_operational)} operacionais`} icon={Cpu} color="blue" />
        <StatCard title="Subagentes Exp." value={fmt(summary.experimental_subagents)} detail="laboratorio, sem autoridade direta" icon={BrainCircuit} color="violet" />
        <StatCard title="False Safe Op." value={fmt(summary.operational_false_safe ?? 0)} detail="deve permanecer zero" icon={ShieldCheck} color="emerald" />
        <StatCard title="False Safe Lab" value={fmt(summary.experimental_false_safe ?? 0)} detail="calibracao experimental" icon={AlertTriangle} color="amber" />
      </div>
      <Card className="p-5">
        <h2 className="text-xl font-black text-[var(--dash-text)]">Rede Neuro-Simbolica</h2>
        <p className="mt-2 text-sm text-[var(--dash-muted)]">Travas deterministicas protegem, o modelo geral observa, o coordenador chama especialistas, e output so e aplicado em etapa separada.</p>
        <div className="mt-6 grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
          {nodes.map(([title, detail, Icon, role]) => (
            <div key={title} className="rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-4">
              <div className="flex items-center justify-between">
                <div className="grid h-10 w-10 place-items-center rounded-xl bg-blue-500/10 text-blue-300"><Icon size={18} /></div>
                <Badge tone={role === 'authoritative' ? 'emerald' : role === 'learning' ? 'amber' : 'blue'}>{role}</Badge>
              </div>
              <h3 className="mt-4 text-sm font-black text-[var(--dash-text)]">{title}</h3>
              <p className="mt-2 text-xs text-[var(--dash-muted)]">{detail}</p>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}

const screens = {
  Production: ProductionControl,
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

const navItems = ['Production', 'Managerial', 'Operational', 'Pipeline', 'Lifecycle', 'Governance', 'Policy', 'Lab', 'Specialists', 'System Architecture', 'Neural Network', 'Network'];
const navLabels = { Production: 'Production', Managerial: 'Managerial', Operational: 'Operational', Cockpit: 'Cockpit', 'ML Performance': 'Performance', Pipeline: 'Pipeline', Lifecycle: 'Lifecycle', Governance: 'Governance', Policy: 'Policy', Lab: 'Lab', Specialists: 'Specialists', 'System Architecture': 'System', 'Neural Network': 'Neural', Network: 'Network' };
const operationalNavItems = ['Operational', 'Pipeline', 'Lifecycle', 'Governance', 'Policy', 'Lab', 'Specialists', 'Network'];
const screenMeta = {
  Production: { title: 'Production Control', subtitle: 'Source, output, gate e inicio seguro do fluxo de producao' },
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
  'Neural Network': { title: 'Neural Architecture', subtitle: 'Rede neuro-simbolica, coordenador e neuroniozinhos' },
  Network: { title: 'Network', subtitle: 'Modelo geral, coordenador, agentes e subagentes' },
};

function App() {
  const [activeTab, setActiveTab] = useState(() => {
    const hash = decodeURIComponent(window.location.hash.replace(/^#/, ''));
    return screens[hash] ? hash : 'Production';
  });
  const [isDarkMode, setIsDarkMode] = useState(true);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
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
  }, []);

  useEffect(() => {
    const onHashChange = () => {
      const hash = decodeURIComponent(window.location.hash.replace(/^#/, ''));
      if (screens[hash]) setActiveTab(hash);
    };
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  const selectTab = (name) => {
    setActiveTab(name);
    window.history.replaceState(null, '', `#${encodeURIComponent(name)}`);
  };

  const ActiveScreen = screens[activeTab];
  const currentScreen = screenMeta[activeTab];
  const showOperationalNav = operationalNavItems.includes(activeTab);

  return (
    <div className="min-h-screen">
      <style>{`
        [data-content-theme='light'] {
          --dash-bg: rgb(241 245 249);
          --dash-card: rgb(255 255 255);
          --dash-border: rgb(226 232 240);
          --dash-text: rgb(15 23 42);
          --dash-muted: rgb(71 85 105);
          --dash-soft: rgb(100 116 139);
          --dash-subtle: rgb(248 250 252);
        }
        [data-content-theme='dark'] {
          --dash-bg: rgb(7 17 31);
          --dash-card: rgba(15, 23, 42, 0.72);
          --dash-border: rgba(255, 255, 255, 0.10);
          --dash-text: rgb(255 255 255);
          --dash-muted: rgb(148 163 184);
          --dash-soft: rgb(100 116 139);
          --dash-subtle: rgba(255, 255, 255, 0.05);
        }
      `}</style>

      <div
        data-content-theme={isDarkMode ? 'dark' : 'light'}
        className={`${isDarkMode ? 'dark ' : ''}min-h-screen w-full overflow-x-hidden bg-[var(--dash-bg)] p-6 text-[var(--dash-text)] transition-colors [&_button]:cursor-pointer`}
      >
        <main className="mx-auto max-w-[1920px]">
          <header className="grid min-h-[78px] grid-cols-12 items-center gap-4 rounded-2xl border border-[var(--dash-border)] bg-[var(--dash-card)] px-5 py-3 shadow-[0_18px_60px_rgba(0,0,0,0.18)]">
            <div className="col-span-12 lg:col-span-5">
              <div className="flex items-center gap-4">
                <div className="grid h-10 w-10 place-items-center rounded-xl border border-blue-500/25 bg-blue-500/10 text-blue-500 dark:text-blue-300">
                  <ShieldCheck size={20} />
                </div>
                <div className="min-w-0">
                  <h1 className="truncate text-xl font-semibold tracking-tight">{currentScreen.title}</h1>
                  <p className="truncate text-sm text-[var(--dash-muted)]">{currentScreen.subtitle}</p>
                </div>
              </div>
            </div>

            <div className="col-span-12 flex flex-wrap items-center justify-start gap-2 lg:col-span-7 lg:justify-end">
              {showOperationalNav && (
                <nav className="inline-flex w-fit flex-wrap items-center gap-1 rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] p-1">
                  {operationalNavItems.map((name) => (
                    <button
                      key={name}
                      onClick={() => selectTab(name)}
                      className={cn(
                        'h-8 rounded-lg px-3 text-sm font-medium transition',
                        activeTab === name
                          ? 'bg-blue-600 text-white shadow-lg shadow-blue-600/20 dark:bg-blue-500/20 dark:text-blue-200 dark:shadow-none'
                          : 'text-[var(--dash-muted)] hover:bg-[var(--dash-subtle)] hover:text-[var(--dash-text)]'
                      )}
                    >
                      {navLabels[name]}
                    </button>
                  ))}
                </nav>
              )}
              <button
                onClick={() => setIsDarkMode((current) => !current)}
                className="grid h-10 w-10 place-items-center rounded-xl border border-[var(--dash-border)] bg-[var(--dash-subtle)] text-[var(--dash-text)] transition hover:bg-blue-500/10"
                aria-label="Alternar tema"
              >
                {isDarkMode ? <Sun /> : <Moon />}
              </button>
            </div>
          </header>

          <div className="mt-4">
            {error && (
              <Card className="mb-5 border-red-500/40 p-4 text-red-300">
                Não consegui carregar a API local: {error}. Inicie com <code>python dashboard/backend.py</code>.
              </Card>
            )}
            {!data && !error && <Card className="p-6">Carregando dados reais do SQLite...</Card>}
            {data && <ActiveScreen data={data} />}
          </div>
        </main>
      </div>
    </div>
  );
}

createRoot(document.getElementById('root')).render(<App />);
