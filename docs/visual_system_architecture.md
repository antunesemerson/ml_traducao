# CK3 PT-BR Visual System Architecture

Este documento define a nova arquitetura visual do sistema local CK3 PT-BR. O objetivo e reduzir peso visual, melhorar performance com cache e consolidar os dashboards em uma experiencia unica.

## Objetivo

Separar claramente tres superficies:

1. **Production Control**
   - Tela inicial de comando.
   - Serve para iniciar producao, acompanhar progresso e ver o ultimo resultado.
   - Deve ser compacta e idealmente sem barra de rolagem em desktop.

2. **Project Intelligence Dashboard**
   - Dashboard unico novo, substituindo os dashboards operacional e gerencial antigos.
   - Serve para entender progresso, aprendizado, pendencias, release, qualidade e network.

3. **Network**
   - Agora deve virar uma aba dentro do dashboard unico.
   - Continua sendo explicativa, visual e publicavel.
   - Nao precisa ser reescrita nesta primeira fase, apenas integrada como rota/aba linkavel.

## Decisoes de produto

- Remover links separados para dashboard operacional e dashboard gerencial.
- Manter link para o novo dashboard unico.
- Manter Network como experiencia visual, mas dentro do novo dashboard:
  - `Visao Geral`
  - `Aprendizado`
  - `Pendencias`
  - `Release`
  - `Qualidade`
  - `Network`
- A tela principal deve responder rapidamente:
  - posso rodar producao?
  - existe treino bloqueando?
  - qual e o estado atual?
  - o que a ultima run fez?
  - ha algo para aplicar?

## Problemas atuais

- `dashboard/backend.py` monta um payload muito grande em `/api/dashboard`.
- O frontend atual acumula muitas visoes historicas:
  - Cockpit
  - Performance
  - Pipeline
  - Governance
  - Policy
  - Lab
  - Specialists
  - Lifecycle
  - Managerial
  - Production Control
  - Network
- A tela principal mostra detalhes demais para uma tela de comando.
- A base SQLite esta crescendo; recalcular tudo em tempo real prejudica a experiencia.
- O status da production run some/limpa visualmente rapido demais apos concluir; queremos manter o ultimo resultado ate a proxima run ou reinicio.

## Arquitetura de cache

### Principio

O backend deve calcular uma fotografia consolidada ao iniciar e servi-la em memoria.

### Arquivos

Sugestao:

- `memory/dashboard_cache.json`
- `memory/dashboard_cache_meta.json` ou meta dentro do mesmo JSON
- `memory/production_run_status.json` continua sendo o status incremental da run ativa/ultima run.

### Ciclo

Ao iniciar o backend:

1. Ler SQLite.
2. Calcular `app_state`.
3. Calcular `intelligence_dashboard`.
4. Salvar em memoria.
5. Persistir em `memory/dashboard_cache.json`.

Durante uso normal:

- `GET /api/app-state` retorna cache.
- `GET /api/dashboard/intelligence` retorna cache.
- `GET /api/production/status` retorna status leve e atualizado.

Durante production run:

- Manter cache anterior como ultima fotografia estavel.
- Atualizar somente `production_run_status.json` com progresso incremental.
- Tela principal usa o status incremental para mostrar progresso.

Ao terminar production run:

1. Recalcular cache uma vez.
2. Persistir nova fotografia.
3. Manter ultimo resultado da run visivel ate proxima run ou restart.

Acao manual:

- `POST /api/cache/refresh` forca recalculo do cache.

## Endpoints propostos

### `GET /api/app-state`

Payload compacto para a tela principal.

Campos sugeridos:

- `generated_at`
- `cache`
  - `generated_at`
  - `source`
  - `stale`
  - `refresh_in_progress`
- `release`
  - `readiness`
  - `closed_count`
  - `pending_count`
  - `closed_rate`
  - `needs_apply`
  - `output_coverage`
  - `latest_segment_state_run_id`
  - `latest_ledger_run_id`
- `learning_gate`
  - `can_start_production`
  - `status`
  - `reason`
  - `active_cycle`
  - `lock`
- `production`
  - `active`
  - `last_run`
  - `current_stage`
  - `progress_pct`
  - `stages_compact`
- `navigation`
  - `dashboard_url`
  - `network_tab_url`

### `GET /api/dashboard/intelligence`

Payload agregado para o dashboard unico.

Top-level sugerido:

- `overview`
- `learning`
- `pending`
- `release`
- `quality`
- `network`
- `meta`

### `GET /api/production/status`

Continua leve e incremental.

### `POST /api/cache/refresh`

Recalcula cache em memoria e arquivo.

### Compatibilidade temporaria

Manter `/api/dashboard` durante a migracao, mas o frontend novo deve preferir:

- `/api/app-state`
- `/api/dashboard/intelligence`
- `/api/production/status`

## Production Control compacta

Layout desktop ideal:

1. Header compacto
   - nome do sistema
   - status SQLite/cache
   - ultima atualizacao
   - botoes: `Refresh`, `Dashboard`, `Network`

2. KPI strip
   - `Readiness`
   - `Closed`
   - `Pending`
   - `Needs Apply`
   - `Learning Gate`

3. Acao principal
   - botao `Start Production Run`
   - desabilitado quando learning gate bloquear ou run ativa existir
   - texto curto explicando bloqueio

4. Fluxo em 4 fases
   - Preparacao
   - Analise e Politicas
   - Aplicacao Controlada
   - Validacao e Handoff

5. Ultima run
   - run id
   - status
   - duracao
   - applied
   - needs_apply
   - relatorio
   - mensagem final

Durante run ativa:

- O fluxo deve destacar fase/subetapa atual.
- Logs detalhados devem ficar colapsados.
- Evitar cards grandes por etapa.

## Novo Project Intelligence Dashboard

Abas:

### Visao Geral

Pergunta: `O projeto esta pronto e evoluindo?`

KPIs:

- total segmentos
- fechados consolidados
- pendentes operacionais
- taxa fechada
- needs apply
- readiness
- ultimo segment-state
- ultimo ledger

Graficos:

- distribuicao fechado/pendente/watch/apply
- evolucao de fechados e pendentes por run
- top gargalos por familia

### Aprendizado

Pergunta: `A rede esta aprendendo ou apenas acumulando excecoes?`

KPIs:

- modelo ativo
- ultimo modelo
- Macro F1
- Safe Precision
- False Safe
- checkpoint/lifecycle gain recente
- filas revisadas

Graficos:

- qualidade por modelo
- ganho por familia/neuronio
- applied vs retained por ciclo

### Pendencias

Pergunta: `O que impede chegar a 100%?`

KPIs:

- pendentes totais
- top family
- top domain
- pendencias com microagente maduro
- pendencias que precisam novo microagente
- pendencias que precisam humano/contexto

Graficos/tabelas:

- issue families do ultimo ledger
- gargalos por dominio
- gargalos por pacote
- matriz: familia x proxima acao

### Release

Pergunta: `O que a ultima producao fez e o que falta para testar/publicar?`

KPIs:

- ultima production run
- status
- output written
- confirmations promoted
- needs_apply
- snapshots arquivados

Graficos/tabelas:

- historico de production runs
- applied por run
- pendencias apos cada run
- relatorios/snapshots

### Qualidade

Pergunta: `Estamos protegidos contra erro estrutural e falso seguro?`

KPIs:

- false safe
- high issues
- token/placeholder risks
- retained/blocked
- human locked
- confirmations auto/human

Graficos:

- bloqueios preservados por motivo
- confirmacoes por fonte
- watch vs actionable

### Network

Pergunta: `Como a rede neuro-simbolica esta organizada?`

Conteudo:

- reutilizar `docs/neurosymbolic_network_visualization.json`
- mostrar macro, coordenador, guards, especialistas, subagentes e lifecycle policies
- manter foco explicativo/publicavel
- nao precisa misturar com operacao diaria

## KPIs principais recomendados

Primarios:

- `Closed Consolidated`
- `Operational Pending`
- `Closed Rate`
- `Needs Apply`
- `Release Readiness`

Drivers:

- `Latest Segment-State Run`
- `Latest Ledger Run`
- `Pending by Issue Family`
- `Gain by Lifecycle/Checkpoint`
- `Safe Apply Count`
- `Applied vs Retained`

Guardrails:

- `False Safe`
- `High Issue Count`
- `Token/Placeholder Risk`
- `Human Locked`
- `Learning Gate`

Metrica nova recomendada:

- `Estimated Closability`

Definicao:

Agrupa pendentes em:

- `ready_for_lifecycle`
- `ready_for_protected_apply`
- `needs_existing_microagent`
- `needs_new_microagent`
- `needs_human_context`
- `domain_policy_required`

Essa metrica orienta o proximo foco de aprendizado e evita olhar apenas para volume bruto.

## Fases de implementacao

### Fase 1 - Cache e Production Control compacta

Implementar:

- cache backend
- `/api/app-state`
- `/api/cache/refresh`
- tela principal compacta
- manter ultima run em tela
- remover links operacional/gerencial
- manter link dashboard unico e Network

Nao implementar ainda:

- dashboard unico completo
- alteracao profunda da Network

### Fase 2 - Dashboard unico

Implementar:

- `Project Intelligence Dashboard`
- abas:
  - Visao Geral
  - Aprendizado
  - Pendencias
  - Release
  - Qualidade
  - Network
- usar `/api/dashboard/intelligence`

### Fase 3 - Refinamento analitico

Implementar:

- Estimated Closability
- Next Best Focus
- detalhe por microagente/neuronio
- drilldown por familia, pacote e dominio

## Regras de seguranca

- Interface visual nao deve rodar treino.
- Interface visual nao deve promover modelo.
- Interface visual nao deve editar source.
- Production Control pode iniciar production run apenas pelo backend protegido existente.
- Cache refresh nao pode escrever output.
- Dashboard unico deve ser read-only.

