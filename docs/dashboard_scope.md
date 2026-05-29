# Escopo Atual do Dashboard CK3 PT-BR

Este documento descreve o estado atual do dashboard local/offline do projeto CK3 PT-BR. Ele deve ser atualizado sempre que uma tela, métrica, consulta, card ou gráfico mudar.

Objetivo: permitir que qualquer chat leia rapidamente o estado atual do dashboard antes de sugerir ajustes ou criar novas visualizações.

## Arquitetura

- Frontend: React/Vite em `dashboard/src/main.jsx`.
- Backend local: Python read-only em `dashboard/backend.py`.
- Banco: `memory/translation_engine.sqlite`.
- Endpoint principal: `GET http://127.0.0.1:8765/api/dashboard`.
- O dashboard é apenas analítico:
  - não escreve no banco;
  - não executa pipeline;
  - não promove modelo;
  - não aplica output;
  - não altera confirmações.

## Telas

### Cockpit

Subtítulo: `O projeto está avançando e está seguro?`

Função: visão executiva operacional do projeto.

Cards:

- `Total de Segmentos`
  - Fonte: `source_segments`.
  - Métrica: segmentos ativos.
- `Cobertura com Output`
  - Fonte: `source_segments` + `output_segments`.
  - Métrica: segmentos ativos com `portuguese_text` preenchido / segmentos ativos.
- `Eficiência Auto-Safe`
  - Fonte: último `ml_score_runs` operacional.
  - Métrica: `final_auto_safe_count / scored_count`.
- `Revisão Pendente`
  - Fonte: último `ml_score_runs` operacional.
  - Métrica: `needs_human_count + needs_autofix_count + blocked_structure_count`.

Visões:

- `Run`
  - Gráfico usa execuções de score.
- `Modelo`
  - Gráfico usa última execução de score por versão/modelo.

Gráficos:

- `Evolução da Confiança Geral`
  - Séries:
    - `Pendências`: segmentos pendentes no score.
    - `Qualidade ML`: `macro_f1 * 100` do modelo usado no score.
    - `Auto-Safe`: `final_auto_safe_count / scored_count`.
- `Distribuição Atual`
  - Fonte: último score operacional.
  - Fatias:
    - `auto_safe`;
    - `needs_human`;
    - `needs_autofix`;
    - `blocked_structure`.

Card inferior:

- `Status operacional`
  - Modelo ativo.
  - Score run.
  - Dataset run.
  - Badges de conexão/status.

### ML Performance

Subtítulo: `Nossa rede neural está aprendendo melhor ou só ficando confiante demais?`

Função: acompanhar qualidade e evolução dos modelos gerais.

Cards:

- `Modelo Ativo`
  - Fonte: `ml_model_registry`.
- `Macro F1`
  - Fonte: modelo ativo em `ml_model_runs`.
- `Safe Precision`
  - Fonte: modelo ativo em `ml_model_runs`.
  - Interpretação: precisão pós-trava de segurança.
- `Holdout Coverage`
  - Fonte: `safe_recall` do modelo ativo.
- `Negative Coverage`
  - Fonte: último dataset em `ml_dataset_runs`.
  - Métrica: `negative_count / total_count`.

Visões:

- `Modelo`
  - Gráficos de evolução por run ou versão.
- `Dataset`
  - Composição do dataset e comparação atual vs candidato.

Alternador de eixo:

- `Run`
  - Cada execução de treino.
- `Modelo`
  - Última run por versão curta.

Gráficos:

- `Evolução por Modelo`
  - Séries:
    - `macro_f1`;
    - `safe_precision`;
    - `safe_recall`.
- `False Safe por Modelo`
  - Barras: `false_safe_count`.
- `Composição do Dataset`
  - Barras:
    - positivos;
    - negativos;
    - neutros;
    - strong positive;
    - strong negative.

Card:

- `Atual vs Candidato`
  - Métricas:
    - accuracy;
    - macro F1;
    - safe precision;
    - holdout coverage;
    - false safe;
    - predicted safe.

### Pipeline

Subtítulo: `Onde está o trabalho agora?`

Função: visualizar esteira de produção e gargalos.

Cards:

Quatro cards duplos, cada um com dois KPIs separados por linha vertical:

- `Segmentos Totais` / `Com Output`
- `Sem Output` / `Locked Humanos`
- `Confirmados` / `Pendentes Revisão`
- `Issues Estruturais` / `Autofix`

Visões:

- `Produção`
  - Status geral e fluxo da esteira.
- `Gargalos`
  - Pacotes mais problemáticos e ritmo de revisão humana.

Gráficos:

- `Status dos Segmentos`
  - Fonte: `source_segments`, `output_segments`, `segment_confirmations`.
- `Fluxo do Segmento`
  - Etapas:
    - Source;
    - Output;
    - Analisado;
    - Scored ML;
    - Confirmado;
    - Locked.
- `Pacotes com Mais Pendência`
  - Fonte: último score operacional.
- `Revisões Humanas por Dia`
  - Fonte: `local_learning_candidates`.

### Governance

Subtítulo: `Estamos protegidos contra erros perigosos?`

Função: acompanhar segurança, bloqueios, promoções e governança de modelo.

Cards:

- `Locked Humanos`
- `Blocked Structure`
- `Token Issues`
- `False Safe Holdout`
- `Modelos Treinados`
- `Última Promoção`

Visões:

- `Risco`
  - Promoções/modelos e motivos de bloqueio.
- `Controle`
  - Fontes de confirmação e política atual de segurança.

Gráficos:

- `Holdout Coverage por Modelo`
  - Fonte: últimas decisões em `ml_model_promotions` + `ml_model_runs`.
  - Barras:
    - `safe_recall * 100`.
  - Cor:
    - verde para promovido;
    - vermelho para rejeitado/não promovido.
- `Motivos de Bloqueio`
  - Fonte principal: `issues`.
  - Fallback: `ml_score_items` do último score operacional.

Controle:

- `Fontes de Confirmação`
  - Fonte: `segment_confirmations`.
- `Política Atual de Segurança`
  - Regras compactas:
    - threshold seguro;
    - false safe precisa ser 0;
    - locked humano nunca é sobrescrito;
    - estrutura/tokens têm prioridade.
  - Comparação:
    - modelo ativo/promovido;
    - último modelo treinado.
  - Métricas:
    - accuracy;
    - macro F1;
    - safe precision;
    - holdout coverage;
    - safe recall;
    - negative coverage.

### Policy

Subtítulo: `Modelo puro vs política operacional por grupo`

Função: comparar score puro do modelo com política operacional por grupo.

Fonte:

- `ml_policy_runs`;
- `ml_policy_items`;
- `ml_score_items`;
- `output_segments`.

Cards:

- `Score Run`
- `Segmentos Avaliados`
- `Auto-safe Modelo`
- `Auto-safe Política`
- `Novos Seguros`

Visões:

- `Charts`
  - `Score vs Policy`
  - `Ganho por Grupo`
- `Audit`
  - tabela `Grupos`
  - tabela `Auditoria dos Novos Seguros`

Métricas:

- `active_auto_safe_count`
- `policy_auto_safe_count`
- `new_safe_count`
- `demoted_safe_count`
- `protect_active_safe`
- `new_safe_pct`

Interpretação:

- Score puro: decisão final original do modelo + travas determinísticas.
- Policy: camada operacional por grupo, sem alterar output.
- Novo seguro: segmento não auto-safe no score que virou auto-safe na policy.
- Proteção ativa: segmentos já seguros não são rebaixados automaticamente.

### Lab

Subtítulo: `Modelo experimental vs modelo ativo`

Função: acompanhar o modelo experimental mais recente contra o modelo ativo.

Fonte:

- `ml_model_registry`;
- `ml_model_runs`;
- `ml_score_runs`;
- `ml_score_items`;
- `ml_model_promotions`.

Cards:

- `Experimental`
  - Run e versão do candidato.
- `False Safe`
  - `candidate_false_safe_count`.
- `Safe Precision`
  - `candidate_safe_precision`.
- `Cobertura Gap`
  - `active_auto_safe_count - candidate_auto_safe_count`.
- `Promoção`
  - Última decisão registrada para o candidato.

Visões:

- `Overview`
  - `Ativo vs Experimental`
  - `Histórico Recente`
- `Distribution`
  - `Distribuição do Experimental`
  - `Decisão de Promoção`
- `Regressions`
  - `Regressão por Arquivo`
  - `Amostra Auditável de Regressão`

Métricas principais:

- `candidate_auto_safe_count`
- `candidate_auto_safe_pct`
- `active_auto_safe_count`
- `active_auto_safe_pct`
- `auto_safe_gap_count`
- `auto_safe_gap_pct_points`
- `candidate_false_safe_count`
- `candidate_safe_precision`
- `candidate_safe_recall`
- `candidate_macro_f1`

Interpretação:

- Regressão: era `auto_safe` no ativo e deixou de ser no experimental.
- Recuperação: não era `auto_safe` no ativo e virou `auto_safe` no experimental.
- Um candidato com false safe 0 ainda pode ser `do_not_promote` por regressão operacional.

### Specialists

Subtítulo: `Especialistas por família, auditoria e aprendizado humano`

Função: acompanhar a arquitetura real de modelos especialistas, seus scores por escopo, divergências contra o modelo geral e aprendizado humano gerado pela fila de auditoria.

Estado atual:

- Usa modelos reais em `ml_model_runs` com `model_kind LIKE 'specialist_%'`.
- Usa scores finalizados em `ml_score_runs` e `ml_score_items`.
- Compara o último score geral (`risk_action_classifier`) com o último score de cada especialista.
- Usa `local_learning_candidates` com `queue_source = 'ml_specialist_auditor'` para medir validação humana da fila.
- Mantém `Policy` separado: a tela `Policy` continua sendo a visão de política operacional por grupo.

Especialistas atuais:

- `specialist_religion`
- `specialist_titles`
- `specialist_title_names`
- `specialist_title_adjectives`
- `specialist_title_cultural_names`
- `specialist_culture_title_labels`

Cards:

- `Especialistas`: total de famílias especialistas consideradas pelo último modelo de cada `model_kind`.
- `Com Score`: especialistas com pelo menos um score finalizado.
- `False Safe Especialistas`: soma de `false_safe_count` nos últimos modelos especialistas.
- `Cobertura Especialista`: cobertura auto-safe do especialista selecionado, hoje priorizando `specialist_title_names`.
- `Auditoria Aberta`: divergências `specialist_new_safe_review + specialist_demoted_review`.
- `Revisões Humanas`: casos revisados na fila `ml_specialist_auditor`.

Visões:

- `Overview`
  - `Especialistas por Escopo`: tabela compacta com último modelo por família, run do modelo, run do score, F1, recall, false safe e cobertura auto-safe.
  - `Cobertura por Especialista`: barras horizontais de `auto_safe_pct` no escopo do score especialista.
- `Auditor`
  - `Divergência por Especialista`: compara geral vs especialista por família.
  - `Fila de Auditoria`: divergências auditáveis, sem aplicação automática.
- `Learning`
  - `Evolução title_names`: série temporal de `specialist_title_names`.
  - `Aprendizado Humano`: resumo de labels e focos revisados na fila especialista.

Métricas atuais:

- `specialists_total`
- `specialists_with_score`
- `specialist_false_safe`
- `selected_auto_safe_count`
- `selected_auto_safe_pct`
- `selected_score_run_id`
- `auditor_review_required`
- `auditor_review_required_pct`
- `human_reviewed_total`

Payload atual:

- `summary`
- `overview`
- `coverageBySpecialist`
- `auditorSummary`
- `auditorBySpecialist`
- `auditorQueue`
- `learningSummary`
- `learningByLabel`
- `learningByFocus`
- `titleNamesEvolution`

Categorias de auditoria:

- `auto_safe_agree`: geral e especialista liberam.
- `needs_human_agree`: ambos seguram para revisão.
- `specialist_new_safe_review`: especialista liberaria algo que o geral segura.
- `specialist_demoted_review`: especialista segura algo que o geral liberaria.

Futuro:

- Adicionar seletor de especialista para alternar `specialist_title_names`, `religion`, `title_adjectives` etc.
- Incluir bloco específico de família de títulos para diferenciar especialista amplo/legado e subespecialistas.
- Expor `auditor_review_required` também na evolução temporal quando a tabela histórica estiver consolidada.
- Mostrar estados vazios quando um especialista existir mas ainda não tiver score.

### Network

Subtítulo: `Modelo geral, coordenador, agentes e subagentes`

Função: mostrar a arquitetura operacional da camada de agentes ML: travas determinísticas, modelo macro, coordenador, especialistas, subagentes experimentais e impacto da política final.

Estado atual:

- Usa `ml_agent_registry` como registro oficial de agentes.
- Usa `ml_agent_routing_runs` como snapshot de roteamento/arquitetura.
- Usa `ml_agent_recommendations` para evidências de novos subagentes.
- Usa `ml_agent_routing_items` quando materializado para mostrar contribuição experimental sem misturar com promoção operacional.
- Combina `ml_model_runs`, `ml_score_runs` e `ml_specialist_policy_snapshots` para saúde dos agentes.
- Combina `ml_policy_runs` e `ml_policy_items` para impacto ensemble.
- A tela é somente analítica: não executa pipeline, não altera banco, não aplica output e não promove modelos.

Cards:

- `Agentes Registrados`: total em `ml_agent_registry`.
- `Operacionais`: agentes `active` com estado `authoritative`, `operational` ou `dry_run`.
- `Subagentes Experimentais`: agentes `active`, `experimental` e `subspecialist`; mostra `planned` como detalhe secundário.
- `Falso-Seguro`: preferir `operational_false_safe` como valor principal; mostrar `experimental_false_safe` como detalhe secundário.
  - `latest_false_safe_total` continua disponível para compatibilidade, mas pode misturar agentes operacionais e experimentais.
  - Se o falso-seguro vier apenas de agente experimental, tratar como alerta de laboratório, não como falha do gate ativo.
- `Ganho Ensemble`: `policy_auto_safe_count - active_auto_safe_count` no último policy run.
- `Evidência Agentes`: soma de `evidence_count` no último run de recomendações.

Visões:

- `Topology`
  - `Fluxo Principal`: `deterministic_guards`, `general_macro`, `coordinator_ensemble_v1`.
  - `Especialistas e Subagentes`: especialistas ativos e subagentes experimentais agrupados por pai.
  - Mostrar badges de autoridade:
    - `decision_authorized`: agente pode participar do fluxo operacional/guardado.
    - `evidence_only`: agente ativo apenas para aprendizado, auditoria e laboratório.
    - `planned_only`: agente recomendado, ainda sem registro operacional.
- `Health`
  - tabela de saúde por agente com tipo, pai, estado, autoridade, `health_status`, `model_kind`, último modelo, último score, threshold, F1, precision, recall, false safe, scored, auto-safe e pendências reais.
  - `experimental_false_safe_watch` deve aparecer como laboratório/treino, não como regressão operacional.
- `Recommendations`
  - tabela de recomendações com `recommendation_type`, incluindo `train_subagent`;
  - painel de motivos e amostras humanas.
- `Promotion`
  - compara `Active promoted macro`, `Candidate macro`, `Operational ensemble` e `Experimental subagents`;
  - mostra status `not_ready`, `watch` ou `ready_for_review` conforme false safe, gap de cobertura e agentes experimentais;
  - deixa claro que subagentes experimentais são caminho de evolução, não promoção automática.
- `Ensemble`
  - gráfico de ganho por `policy_group`;
  - timeline de roteamento com cobertura ativa, planejada e recomendações;
  - tabela de itens roteados por agente no último run.

Payload atual:

- `agents.summary`
- `agents.activeGate`
- `agents.topologyNodes`
- `agents.topologyEdges`
- `agents.registry`
- `agents.health`
- `agents.recommendations`
- `agents.routingRuns`
- `agents.routedItemsByAgent`
- `agents.routingSamples`
- `agents.ensembleImpact`
- `agents.promotionReadiness`
- `agents.experimentalContribution`
- `agents.learningByAgent`
- `agents.agentTimeline`

Interpretação:

- `deterministic_guards` é hard gate e deve preceder qualquer ML.
- `general_macro` é o classificador geral/baseline.
- `coordinator_ensemble_v1` é o roteador/árbitro em dry-run.
- `specialist` e `subspecialist` votam em escopos menores.
- `experimental` indica subagente treinável, ainda sem autoridade operacional/promovida.
- `experimental` pode ser útil mesmo sem promoção: ele identifica fronteiras, gera evidência humana e indica onde criar subpolíticas menores.
- `planned` indica agente recomendado por evidência, ainda sem registro operacional.
- `activeGate` mostra o gate composto em uso: overlay ativo, checkpoint guardado, tipo de promoção, releases guardados, bloqueadores e se auto-apply segue desativado.
- O gate ativo atual deve ser considerado seguro quando `operational_false_safe = 0`, `active_gate_auto_apply_allowed = 0`, `active_gate_invalid_releases = 0` e `active_gate_apply_allowed_count = 0`.
- Se `experimental_false_safe > 0` e `operational_false_safe = 0`, a leitura correta é: "há agente experimental precisando treino/calibração", não "o sistema operacional está inseguro".

### Lifecycle

Subtitulo: `Estado final operacional dos segmentos`

Funcao: mostrar o fechamento operacional dos segmentos depois da combinacao entre confirmacoes, output atual, blanks validos e score ML atual. Esta tela responde se o pacote esta consolidado, o que ainda precisa ser aplicado no output e quais segmentos devem ser reabertos porque o modelo atual discorda de uma confirmacao antiga.

Fontes:

- `segment_state_runs`: snapshot consolidado por execucao de ciclo de vida.
- `segment_state_items`: estado final de cada segmento dentro do snapshot.

Cards:

- `Segmentos Consolidados`: `closed_count / total_segments`.
- `Pendencia Operacional`: `pending_count / total_segments`.
- `Aplicar Output Confirmado`: `output_apply_pending_count`, casos confirmados que ainda precisam refletir no output.
- `Blanks Validos`: `blank_valid_count`, linhas vazias aceitas por regra/contexto.
- `Reabrir por ML Atual`: `reopen_count`, confirmacoes antigas que o modelo atual recomenda revisar.

Visoes:

- `Overview`
  - grafico `Closed vs Pending`;
  - grafico de distribuicao por `state`;
  - tabela consolidada de estados com totais, output pendente, blanks e reabertura.
- `Output`
  - tabela `Output, Aplicacao e Revisao`;
  - separa estados onde o output ja bate, esta pendente de aplicacao ou deve ser reaberto.
- `Apply`
  - mostra historico real do `segment-apply`;
  - diferencia dry-run de escrita real em `output/spanish`;
  - acompanha `Outputs Reescritos`, `Ultimo Lote Aplicado`, `Token Bloqueado`, `Arquivos Tocados` e `Dry-runs`;
  - grafico combina pendencia de aplicacao do snapshot, linhas aplicadas e bloqueios por token;
  - tabelas mostram runs recentes, aplicacoes por pacote e fila de bloqueios por token.
- `Token Policy`
  - mostra o gate estrutural posterior ao token mismatch;
  - acompanha `Gate de Tokens`, `Critico`, `Alto Risco`, `Revisao Humana`, `Bloqueados` e `Candidatos Politica`;
  - acompanha decisoes humanas em `Decisoes do Gate`, `Aprovadas Aplicar`, `Precisam Correcao`, `Precisam Subpolitica` e `Critico Aprovado`;
  - acompanha uma camada experimental de overlay de politica, comparando risco original vs risco apos subpoliticas candidatas;
  - acompanha checkpoints oficiais do modelo composto, indicando se o coordenador esta pronto para promocao guardada;
  - grafico distribui `policy_bucket` por risco;
  - graficos/tabelas adicionais podem mostrar `Criticos Antes`, `Criticos Apos Overlay`, `Criticos Liberados`, `Regras Candidatas`, `Apply Permitido`, `Status de Promocao`, `Bloqueadores` e `Avisos`;
  - tabela lista ultimas execucoes de `segment-token-policy`;
  - tabela lista ultimas execucoes de `segment-token-policy-overlay`;
  - tabela mostra buckets por pacote;
  - fila auditavel mostra candidatos para excecao manual ou politica futura;
  - tabelas adicionais mostram decisoes por bucket, cobertura da revisao e runs de decisao.
- `Packages`
  - backlog por arquivo/pacote;
  - prioriza `pending_count`, `output_apply_pending`, `reopen_count` e `blank_valid_count`.
- `Queues`
  - fila auditavel de segmentos confirmados para aplicar no output;
  - fila auditavel de segmentos confirmados que o ML atual quer reabrir.

Payload atual:

- `lifecycle.summary`
- `lifecycle.groupDistribution`
- `lifecycle.stateDistribution`
- `lifecycle.outputApplication`
- `lifecycle.packageBacklog`
- `lifecycle.applyQueue`
- `lifecycle.reopenQueue`
- `lifecycle.outputApply.summary`
- `lifecycle.outputApply.runs`
- `lifecycle.outputApply.evolution`
- `lifecycle.outputApply.packageItems`
- `lifecycle.outputApply.tokenBlocks`
- `lifecycle.tokenPolicy.summary`
- `lifecycle.tokenPolicy.runs`
- `lifecycle.tokenPolicy.bucketDistribution`
- `lifecycle.tokenPolicy.packageBuckets`
- `lifecycle.tokenPolicy.reviewQueue`
- `lifecycle.tokenPolicy.decisions.summary`
- `lifecycle.tokenPolicy.decisions.runs`
- `lifecycle.tokenPolicy.decisions.byBucket`
- `lifecycle.tokenPolicy.decisions.coverage`
- `lifecycle.tokenPolicy.overlay.summary`
- `lifecycle.tokenPolicy.overlay.runs`
- `lifecycle.tokenPolicy.overlay.riskComparison`
- `lifecycle.tokenPolicy.overlay.actionDistribution`
- `lifecycle.tokenPolicy.overlay.bucketDistribution`
- `lifecycle.tokenPolicy.overlay.releasedByRule`
- `lifecycle.tokenPolicy.overlay.releasedItems`
- `lifecycle.tokenPolicy.overlay.remainingCritical`
- `lifecycle.tokenPolicy.checkpoints.summary`
- `lifecycle.tokenPolicy.checkpoints.runs`
- `lifecycle.tokenPolicy.checkpoints.trend`
- `lifecycle.tokenPolicy.checkpoints.statusDistribution`
- `lifecycle.tokenPolicy.checkpoints.registry`
- `lifecycle.tokenPolicy.checkpoints.promotions`
- `lifecycle.tokenPolicy.checkpoints.activeQueue.summary`
- `lifecycle.tokenPolicy.checkpoints.activeQueue.runs`
- `lifecycle.tokenPolicy.checkpoints.activeQueue.routes`
- `lifecycle.tokenPolicy.checkpoints.reviewProgress.summary`
- `lifecycle.tokenPolicy.checkpoints.reviewProgress.routes`
- `lifecycle.tokenPolicy.checkpoints.reviewProgress.trend`

Interpretacao:

- A tela e read-only e nao deve aplicar output automaticamente.
- `pending_apply_confirmed` indica trabalho mecanico/auditavel de sincronizar output.
- `reopen_auto_confirmed_autofix` e familia similar indica que a confirmacao antiga nao deve ser sobrescrita em massa; primeiro vira revisao humana ou fila controlada.
- Blanks validos sao excecoes estruturais/esteticas e devem continuar protegidos contra preenchimento automatico.
- `segment_output_apply_runs.apply = 1` representa escrita real no output; `apply = 0` e dry-run.
- `applied_backfill` deve contar como aplicado real, pois representa o primeiro lote aplicado antes da criacao completa do historico.
- Itens com `token_mismatch = 1` sao bloqueios deterministiscos e nao devem ser aplicados automaticamente.
- `segment_token_policy_items.policy_bucket` classifica por que um token mismatch foi bloqueado ou deve ir para revisao.
- `blocked_*` nao aplica; `manual_exception_candidate_*` pede revisao humana; `policy_candidate_*` pode virar regra segura depois de amostra validada.
- `blocked_suspicious_confirmed_text` deve receber destaque nas proximas melhorias porque pode indicar texto confirmado com problema de encoding.
- `segment_token_policy_decisions` registra a decisao humana posterior ao gate; ainda nao altera `output/spanish`.
- A ultima policy revisada pode ser diferente do ultimo gate gerado. O dashboard deve mostrar o `policy_run_id` da decisao para evitar falsa cobertura.
- `critical_approved_for_apply` deve permanecer zero; qualquer valor acima disso e alerta de governanca.
- `segment_token_policy_overlay_*` e uma camada experimental read-only. Ela nao aplica output e nao substitui o gate base.
- `released_critical_count` significa "deixaria de ser critico na simulacao", nao "auto-aplicado".
- `apply_allowed_count` deve permanecer zero enquanto a politica candidata estiver apenas em teste.
- `ml_composite_checkpoints.promotion_status = ready_for_guarded_promotion` significa que o coordenador composto pode ser promovido como gate de revisao, mantendo auto-apply desabilitado.
- `recommended_action` deve ser exibido como orientacao operacional, nao como acao executada.
- `lifecycle.tokenPolicy.checkpoints.registry.operational_state = operational` indica que o gate composto foi promovido para uso operacional de revisao.
- `lifecycle.tokenPolicy.checkpoints.registry.auto_apply_allowed` deve continuar `0`; se aparecer `1`, mostrar alerta de governanca.
- Relatorios `segment_token_overlay_review_queue_active_gate_critical` mostram a fila critica usando o gate composto ativo. `Rows selected = 0` e um marco positivo.
- Relatorios `segment_token_overlay_review_queue_active_gate_all` mostram a distribuicao operacional por rotas/subespecialistas, sem liberar auto-apply.
- O backend tambem expoe a ultima fila operacional no SQLite via `activeQueue`, para o dashboard nao depender de parsear arquivos de relatorio.
- `activeQueue.summary` pode representar a ultima fila gerada, inclusive filtrada por rota/limite.
- `activeQueue.fullSummary` e `activeQueue.fullRoutes` preservam a visao completa mais recente do gate ativo sem filtros.
- `activeQueue.runs` lista os lotes pequenos por subespecialista via `route_filter_csv`, `risk_filter_csv` e `limit_count`.
- `reviewProgress` mede quanto da fila ativa ja recebeu decisao humana em `segment_token_policy_decisions`, por rota/subespecialista.
- `reviewProgress.summary.approved_for_apply_count` ainda nao aplica output; indica apenas que a decisao humana poderia liberar o segmento para uma etapa posterior de apply com travas.
- O fluxo de ingestao seguro para revisoes do gate composto e `ml-composite-review-ingest`: dry-run por padrao, gravacao no banco somente com `--auto-apply`, sem alterar `output/spanish`.
- `reviewProgress.summary.queued_items` e `queue_coverage_pct` medem quanto do gate ativo ja foi entregue para revisao em lotes pequenos.
- `reviewProgress.routes` tambem expoe `queued_items`, `unqueued_items` e `queue_coverage_pct` por subespecialista.
- Para proximos lotes ineditos, usar `segment-token-overlay-queue --token-overlay-skip-queued --token-overlay-skip-reviewed`.

## Extensoes Planejadas

### Production Control

Nova tela/web local fora do dashboard analitico principal.

Objetivo: iniciar e acompanhar um ciclo de producao do mod, consumindo uma API local futura com allowlist de comandos. A tela deve mostrar estado das fontes, estado do output, timeline de execucao, logs resumidos, pendencias, bloqueios e links para dashboards.

### Learning Gate / Production Lock

Funcao: impedir que o fluxo geral de producao rode enquanto a frente de aprendizado estiver no meio de treino, auditoria, checkpoint ou promocao.

Fonte canonica:

- `memory/learning_status.json`: estado detalhado da frente de aprendizado.
- `memory/training_lock.json`: trava operacional lida pelo backend; existe enquanto `production_safe = false`.

Endpoint:

- `GET /api/learning/status`
- tambem embutido em `GET /api/dashboard` como `learning`
- resumo embutido em `production.learning`

Payload:

- `learning.status`: `idle`, `running`, `blocked`, `failed`, `completed`, `released`.
- `learning.production_safe`: booleano calculado pelo status de aprendizado.
- `learning.can_start_production`: booleano final para o botao de producao.
- `learning.objective`: objetivo do ciclo atual.
- `learning.current_phase` e `current_phase_label`.
- `learning.phase_total`, `phase_completed`, `phase_pending`, `progress_pct`.
- `learning.phases`: lista de fases com `id`, `label`, `status`, timestamps e resumo.
- `learning.last_report`, `blockers`, `warnings`, `next_action`.

Interpretacao:

- Se `can_start_production = false`, a tela de Producao deve desabilitar `Start Production Run` e mostrar alerta `Learning Cycle Active`.
- Se `status = released` e `can_start_production = true`, a frente de aprendizado liberou a producao para usar o conhecimento atualmente promovido.
- `blocked` ou `failed` nao significa problema no mod; significa que o laboratorio precisa resolver um checkpoint antes de liberar a producao.
- O status e mantido por `pipeline/learning_status.py`.

Documento base: `docs/production_flow_architecture.md`.

Prompt de construcao: `prompts/production_web_app_prompt.md`.

Contrato futuro sugerido:

- `GET /api/production/status`
- `POST /api/production/start`
- `GET /api/production/runs/:runId`
- `GET /api/production/runs/:runId/logs`

### Architecture Views

Nova visualizacao didatica para explicar o fluxo de Producao e a rede neuro-simbolica.

Documentos base:

- `docs/production_flow_architecture.md`
- `docs/neurosymbolic_network_architecture.md`

Prompt de construcao: `prompts/dashboard_architecture_views_prompt.md`.

Essa visualizacao deve continuar read-only quando estiver dentro do dashboard analitico. Se houver execucao de pipeline, ela deve ficar na web local de Producao, nao no dashboard analitico.

## Alturas e Layout

Padrão visual atual:

- Cards KPI: `min-h-[156px]`.
- Cards de gráfico principais: `h-[445px]` nas telas com uma linha principal.
- Cockpit usa gráficos `h-[375px]` e card inferior de status.
- Layout de conteúdo:
  - shell centralizado com `max-w-[1920px]`;
  - fundo escuro `#07111f` no tema dark;
  - header em card arredondado com marca `CK3 PT-BR Translation Intelligence`;
  - título e subtítulo da tela ficam dentro do header;
  - não há faixa intermediária entre header e KPIs;
  - cards/painéis com `rounded-2xl`, borda translúcida e sombra ampla;
  - menu principal usa o mesmo estilo dos submenus segmentados em ciano, mantendo navegação em inglês.
- KPIs seguem o padrão do novo protótipo:
  - título e valor à esquerda;
  - ícone em bloco colorido à direita;
  - detalhe e delta na base do card;
  - cards compostos usam divisor vertical central.
- Menus principais usam nomes em inglês:
  - Cockpit
  - Performance
  - Pipeline
  - Lifecycle
  - Governance
  - Policy
  - Lab
  - Specialists
  - Network

## Boas Práticas Para Próximas Alterações

- Antes de criar visualização nova, ler este documento.
- Preferir adicionar visão/toggle dentro de tela existente quando o assunto for continuação direta.
- Criar aba nova quando a semântica for diferente, como:
  - produção vs laboratório;
  - política operacional vs modelo experimental;
  - auditor/especialistas vs modelo geral.
- Não misturar botões executores no dashboard analítico.
- Evitar métricas que tecnicamente estão corretas mas visualmente inúteis, como gráfico de barras de `false_safe_count` quando todos os valores são zero.
- Quando uma métrica vem de fallback, documentar isso no card ou neste arquivo.
- Métricas de segurança devem priorizar:
  - false safe;
  - safe precision;
  - regressão operacional;
  - locked humano;
  - bloqueios estruturais/tokens.
