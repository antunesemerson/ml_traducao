import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';
import {
  AlertCircle,
  ArrowDownRight,
  ArrowUpRight,
  BarChart3,
  CheckCircle2,
  Database,
  FileWarning,
  LayoutDashboard,
  Lock,
  Moon,
  PackageSearch,
  Scale,
  Rocket,
  SearchCheck,
  ShieldAlert,
  ShieldCheck,
  Sun,
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
  <div className={`rounded-lg border border-[var(--dash-border)] bg-[var(--dash-card)] shadow-[0_2px_12px_rgba(15,23,42,0.07)] ${className}`}>
    {children}
  </div>
);

const StatCard = ({ title, value, detail, trend, icon: Icon, color = 'blue', danger = false }) => (
  <Card className="min-h-[156px] p-4">
    <div className="mb-3 flex items-start justify-between">
      <div className={`rounded-lg p-2 ${colorClasses[color] ?? colorClasses.blue}`}>
        <Icon size={18} />
      </div>
      {trend !== undefined && trend !== null && (
        <span className={`flex items-center gap-1 text-xs font-bold ${danger ? 'text-red-500' : 'text-emerald-500'}`}>
          {danger ? <ArrowDownRight size={14} /> : <ArrowUpRight size={14} />}
          {trend}
        </span>
      )}
    </div>
    <h3 className="text-sm font-medium text-[var(--dash-muted)]">{title}</h3>
    <p className="mt-1 text-2xl font-bold text-[var(--dash-text)] xl:text-[1.65rem]">{value}</p>
    {detail && <p className="mt-1.5 text-xs text-[var(--dash-soft)]">{detail}</p>}
  </Card>
);

const SplitStatCard = ({ left, right }) => (
  <Card className="min-h-[156px] p-4">
    <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-4">
      <MiniStat {...left} />
      <div className="h-[90%] min-h-20 w-px self-center bg-[var(--dash-border)]" />
      <MiniStat {...right} />
    </div>
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
  <div className={`rounded-md border border-[var(--dash-border)] bg-[var(--dash-card)] p-3 ${className}`}>
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
  <Card className={`p-5 ${className}`}>
    <div className="mb-4">
      <h3 className="text-lg font-bold text-[var(--dash-text)]">{title}</h3>
      {subtitle && <p className="mt-1 text-sm text-[var(--dash-muted)]">{subtitle}</p>}
    </div>
    {children}
  </Card>
);

const Badge = ({ children, tone = 'emerald' }) => (
  <span className={`rounded-full px-3 py-1 text-xs font-bold ${colorClasses[tone] ?? colorClasses.emerald}`}>{children}</span>
);

const ViewToggle = ({ options, value, onChange }) => (
  <div className="flex rounded-lg border border-[var(--dash-border)] bg-[var(--dash-card)] p-1 shadow-[0_2px_12px_rgba(15,23,42,0.07)]">
    {options.map((item) => (
      <button
        key={item}
        onClick={() => onChange(item)}
        className={`rounded-md px-4 py-2 text-sm font-bold transition ${
          value === item ? 'bg-blue-600 text-white shadow-lg shadow-blue-600/20' : 'text-[var(--dash-muted)] hover:bg-[var(--dash-subtle)] hover:text-[var(--dash-text)]'
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
  const { summary, groupComparison, divergenceMatrix, auditQueue, evolution } = specialists;

  return (
    <div className="flex h-full min-h-0 flex-col gap-4 pb-3">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-6">
        <StatCard title="Especialistas Treinados" value={fmt(summary.specialists_total)} detail="modelos dedicados" icon={Database} color="violet" />
        <StatCard title="Especialistas Ativos" value={fmt(summary.specialists_active)} detail="em produção" icon={ShieldCheck} color="emerald" />
        <StatCard title="Cobertura com Auditor" value={pct(summary.auditor_auto_safe_pct)} detail={compact(summary.auditor_auto_safe_count)} icon={SearchCheck} color="blue" />
        <StatCard title="Divergências Abertas" value={fmt(summary.open_disagreements)} detail="new safe + demoted" icon={AlertCircle} color="amber" />
        <StatCard title="False Safe Especialistas" value={fmt(summary.specialist_false_safe)} detail="meta: zero" icon={ShieldAlert} color={summary.specialist_false_safe ? 'red' : 'emerald'} />
        <StatCard title="Novos Auto-safe Auditor" value={fmt(summary.auditor_new_safe)} detail="camada auditável" icon={ArrowUpRight} color="emerald" />
      </div>

      <ViewHeader
        title={viewMode === 'Overview' ? 'Specialist Overview' : viewMode === 'Audit' ? 'Auditor Queue' : 'Temporal Evolution'}
        subtitle={viewMode === 'Overview' ? 'Modelos por família, grupos e divergências.' : viewMode === 'Audit' ? 'Exemplos priorizados para revisão humana.' : 'Cobertura e divergências por execução.'}
      >
        <ViewToggle options={['Overview', 'Audit', 'Evolution']} value={viewMode} onChange={setViewMode} />
      </ViewHeader>

      {viewMode === 'Overview' ? (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <ChartCard title="Comparação por Grupo" subtitle="Geral, política e auditor combinado">
            <ChartBox className="h-[445px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={groupComparison.slice(0, 10)} layout="vertical" margin={{ top: 8, right: 18, left: 90, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke="currentColor" opacity={0.12} />
                  <XAxis type="number" tickFormatter={(v) => compact(v)} axisLine={false} tickLine={false} tick={chartText} />
                  <YAxis type="category" dataKey="group_name" width={170} axisLine={false} tickLine={false} tick={chartText} />
                  <Tooltip formatter={(value) => fmt(value)} />
                  <Legend />
                  <Bar dataKey="general_auto_safe" name="Geral" fill="#64748b" radius={[0, 8, 8, 0]} />
                  <Bar dataKey="policy_auto_safe" name="Política" fill="#10b981" radius={[0, 8, 8, 0]} />
                  <Bar dataKey="auditor_auto_safe" name="Auditor" fill="#3b82f6" radius={[0, 8, 8, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </ChartBox>
          </ChartCard>

          <Card className="p-5">
            <h3 className="text-lg font-bold text-[var(--dash-text)]">Matriz de Divergência</h3>
            <p className="mt-1 text-sm text-[var(--dash-muted)]">Categorias que exigem inspeção ou acompanhamento.</p>
            <div className="mt-4 h-[445px] overflow-y-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase text-[var(--dash-muted)]">
                  <tr><th className="py-2">Categoria</th><th className="py-2 text-right">Total</th><th className="py-2">Risco</th><th className="py-2">Ação</th></tr>
                </thead>
                <tbody>
                  {divergenceMatrix.map((item) => (
                    <tr key={item.category} className="border-t border-[var(--dash-border)]">
                      <td className="py-2 font-semibold text-[var(--dash-text)]">{item.category}</td>
                      <td className="py-2 text-right text-amber-400">{fmt(item.count)}</td>
                      <td className="py-2">{item.risk_level}</td>
                      <td className="py-2">{item.recommended_action}</td>
                    </tr>
                  ))}
                  {!divergenceMatrix.length && <tr><td className="py-6 text-center text-[var(--dash-muted)]" colSpan="4">Nenhuma divergência registrada.</td></tr>}
                </tbody>
              </table>
            </div>
          </Card>
        </div>
      ) : viewMode === 'Audit' ? (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-5">
          <Card className="p-5 xl:col-span-2">
            <h3 className="text-lg font-bold text-[var(--dash-text)]">Especialistas</h3>
            <p className="mt-1 text-sm text-[var(--dash-muted)]">Tabela preparada para modelos especialistas reais.</p>
            <div className="mt-4 h-[445px] overflow-y-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase text-[var(--dash-muted)]">
                  <tr><th className="py-2">Especialista</th><th className="py-2">Status</th><th className="py-2 text-right">F1</th><th className="py-2 text-right">False</th></tr>
                </thead>
                <tbody>
                  {specialists.specialists.map((item) => (
                    <tr key={item.model_run_id} className="border-t border-[var(--dash-border)]">
                      <td className="max-w-[210px] truncate py-2 font-semibold text-[var(--dash-text)]">{item.specialist_name}</td>
                      <td className="py-2">{item.status}</td>
                      <td className="py-2 text-right">{pctMetric(item.macro_f1)}</td>
                      <td className="py-2 text-right text-red-400">{fmt(item.false_safe)}</td>
                    </tr>
                  ))}
                  {!specialists.specialists.length && <tr><td className="py-6 text-center text-[var(--dash-muted)]" colSpan="4">Nenhum especialista treinado ainda.</td></tr>}
                </tbody>
              </table>
            </div>
          </Card>

          <Card className="p-5 xl:col-span-3">
            <h3 className="text-lg font-bold text-[var(--dash-text)]">Fila de Auditoria</h3>
            <p className="mt-1 text-sm text-[var(--dash-muted)]">Itens liberados, rebaixados ou protegidos pelo auditor/política.</p>
            <div className="mt-4 h-[445px] overflow-y-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase text-[var(--dash-muted)]">
                  <tr><th className="py-2">Segmento</th><th className="py-2">Grupo</th><th className="py-2">Chave</th><th className="py-2">Geral</th><th className="py-2">Auditor</th><th className="py-2 text-right">Prob.</th></tr>
                </thead>
                <tbody>
                  {auditQueue.slice(0, 80).map((item) => (
                    <tr key={`${item.segment_id}-${item.source_key}`} className="border-t border-[var(--dash-border)]">
                      <td className="py-2 text-[var(--dash-muted)]">{item.segment_id}</td>
                      <td className="max-w-[180px] truncate py-2">{item.group_name}</td>
                      <td className="max-w-[220px] truncate py-2 font-semibold text-[var(--dash-text)]" title={item.source_key}>{item.source_key}</td>
                      <td className="py-2">{item.general_action}</td>
                      <td className="py-2 text-blue-400">{item.auditor_action}</td>
                      <td className="py-2 text-right text-emerald-400">{pctMetric(item.general_probability)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <ChartCard title="Evolução Temporal" subtitle="Cobertura geral, auditor e divergências">
            <ChartBox className="h-[445px]">
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart data={evolution} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="currentColor" opacity={0.12} />
                  <XAxis dataKey="run_id" axisLine={false} tickLine={false} tick={chartText} />
                  <YAxis yAxisId="left" tickFormatter={(v) => `${v}%`} axisLine={false} tickLine={false} tick={chartText} />
                  <YAxis yAxisId="right" orientation="right" axisLine={false} tickLine={false} tick={chartText} />
                  <Tooltip formatter={(value, name) => name === 'Divergências' ? fmt(value) : pct(value)} />
                  <Legend />
                  <Line yAxisId="left" type="monotone" dataKey="general_auto_safe_pct" name="Geral" stroke="#64748b" strokeWidth={3} />
                  <Line yAxisId="left" type="monotone" dataKey="auditor_auto_safe_pct" name="Auditor" stroke="#3b82f6" strokeWidth={3} />
                  <Bar yAxisId="right" dataKey="disagreements" name="Divergências" fill="#f59e0b" radius={[8, 8, 0, 0]} />
                </ComposedChart>
              </ResponsiveContainer>
            </ChartBox>
          </ChartCard>

          <Card className="p-5">
            <h3 className="text-lg font-bold text-[var(--dash-text)]">Estado Atual</h3>
            <p className="mt-1 text-sm text-[var(--dash-muted)]">Especialistas ainda não têm tabela própria; auditor usa política por grupo.</p>
            <div className="mt-4 grid gap-3 md:grid-cols-2">
              <MetricTile title="Especialistas" value={fmt(summary.specialists_total)} tone="blue" />
              <MetricTile title="Auditor Auto-safe" value={`${compact(summary.auditor_auto_safe_count)} / ${pct(summary.auditor_auto_safe_pct)}`} tone="emerald" />
              <MetricTile title="Divergências" value={fmt(summary.open_disagreements)} tone="amber" />
              <MetricTile title="False Safe" value={fmt(summary.specialist_false_safe)} tone={summary.specialist_false_safe ? 'red' : 'emerald'} />
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}

const screens = {
  Cockpit,
  'ML Performance': MLPerformance,
  Pipeline,
  Governance,
  Policy,
  Lab,
  Specialists,
};

const navItems = ['Cockpit', 'ML Performance', 'Pipeline', 'Governance', 'Policy', 'Lab', 'Specialists'];
const navLabels = { Cockpit: 'Cockpit', 'ML Performance': 'Performance', Pipeline: 'Pipeline', Governance: 'Governance', Policy: 'Policy', Lab: 'Lab', Specialists: 'Specialists' };
const screenMeta = {
  Cockpit: { title: 'Cockpit Executivo', subtitle: 'O projeto está avançando e está seguro?' },
  'ML Performance': { title: 'ML Performance', subtitle: 'Nossa rede neural está aprendendo melhor ou só ficando confiante demais?' },
  Pipeline: { title: 'Pipeline', subtitle: 'Onde está o trabalho agora?' },
  Governance: { title: 'Governance', subtitle: 'Estamos protegidos contra erros perigosos?' },
  Policy: { title: 'Policy', subtitle: 'Modelo puro vs política operacional por grupo' },
  Lab: { title: 'Lab', subtitle: 'Modelo experimental vs modelo ativo' },
  Specialists: { title: 'Specialists', subtitle: 'Modelos por família, divergências e auditoria' },
};

function App() {
  const [activeTab, setActiveTab] = useState('Cockpit');
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

  const ActiveScreen = screens[activeTab];
  const currentScreen = screenMeta[activeTab];

  return (
    <div className="min-h-screen">
      <style>{`
        [data-content-theme='light'] {
          --dash-bg: rgb(246 243 238);
          --dash-card: rgb(255 255 255);
          --dash-border: rgb(226 221 213);
          --dash-text: rgb(15 23 42);
          --dash-muted: rgb(71 85 105);
          --dash-soft: rgb(100 116 139);
          --dash-subtle: rgb(246 243 238);
        }
        [data-content-theme='dark'] {
          --dash-bg: rgb(2 6 23);
          --dash-card: rgb(15 23 42);
          --dash-border: rgb(51 65 85);
          --dash-text: rgb(255 255 255);
          --dash-muted: rgb(148 163 184);
          --dash-soft: rgb(100 116 139);
          --dash-subtle: rgba(15, 23, 42, 0.6);
        }
      `}</style>

      <div className="flex h-screen bg-slate-950 text-slate-100 transition-colors [&_button]:cursor-pointer">
        <main className="flex min-w-0 flex-1 flex-col">
          <header className="flex h-[72px] items-center justify-between gap-6 border-b border-slate-800 bg-slate-900 px-6">
            <div className="min-w-0">
              <h1 className="truncate text-[1.35rem] font-black text-white">{currentScreen.title}</h1>
              <p className="truncate text-sm text-slate-400">{currentScreen.subtitle}</p>
            </div>

            <div className="flex shrink-0 items-center gap-3">
              <nav className="flex rounded-lg border border-slate-800 bg-slate-950/50 p-1 shadow-lg shadow-black/10">
                {navItems.map((name) => (
                  <button
                    key={name}
                    onClick={() => setActiveTab(name)}
                    className={`rounded-md px-2.5 py-2 text-sm font-bold transition ${activeTab === name ? 'bg-blue-600 text-white shadow-lg shadow-blue-600/20' : 'text-slate-300 hover:bg-slate-800 hover:text-white'}`}
                  >
                    {navLabels[name]}
                  </button>
                ))}
              </nav>

              <button onClick={() => setIsDarkMode((current) => !current)} className="rounded-lg p-2 text-slate-100 transition hover:bg-slate-800" aria-label="Alternar tema">
                {isDarkMode ? <Sun /> : <Moon />}
              </button>
            </div>
          </header>

          <div data-content-theme={isDarkMode ? 'dark' : 'light'} className={`${isDarkMode ? 'dark ' : ''}flex-1 overflow-y-auto bg-[var(--dash-bg)] px-5 py-4 text-[var(--dash-text)] transition-colors lg:px-7 lg:py-5`}>
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
