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

Subtítulo: `Modelos por família, divergências e auditoria`

Função: acompanhar futura arquitetura de modelos especialistas e auditor.

Estado atual:

- Não há tabelas próprias de especialistas.
- A tela usa:
  - modelos com `model_kind` ou `model_version` contendo `special`, se existirem;
  - dados reais de `ml_policy_runs`/`ml_policy_items` como camada auditor/policy.

Cards:

- `Especialistas Treinados`
- `Especialistas Ativos`
- `Cobertura com Auditor`
- `Divergências Abertas`
- `False Safe Especialistas`
- `Novos Auto-safe Auditor`

Visões:

- `Overview`
  - `Comparação por Grupo`
  - `Matriz de Divergência`
- `Audit`
  - tabela `Especialistas`
  - tabela `Fila de Auditoria`
- `Evolution`
  - `Evolução Temporal`
  - `Estado Atual`

Métricas atuais:

- `specialists_total`
- `specialists_active`
- `auditor_auto_safe_count`
- `auditor_auto_safe_pct`
- `open_disagreements`
- `specialist_false_safe`
- `auditor_new_safe`

Matriz de divergência atual:

- `auditor_new_safe`
- `auditor_demoted_safe`
- `learned_negative_blocked`

Futuro:

- Quando existirem tabelas próprias, incluir:
  - especialista por família/grupo;
  - ação do especialista;
  - probabilidade do especialista;
  - divergência entre geral, especialista e auditor;
  - fila humana priorizada por discordância.

## Alturas e Layout

Padrão visual atual:

- Cards KPI: `min-h-[156px]`.
- Cards de gráfico principais: `h-[445px]` nas telas com uma linha principal.
- Cockpit usa gráficos `h-[375px]` e card inferior de status.
- Layout de conteúdo: tema escuro, cards densos, botões segmentados.
- Menus principais usam nomes em inglês:
  - Cockpit
  - Performance
  - Pipeline
  - Governance
  - Policy
  - Lab
  - Specialists

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

