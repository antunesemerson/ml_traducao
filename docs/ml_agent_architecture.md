# Arquitetura de Agentes ML

Documento conceitual complementar: `docs/neurosymbolic_network_architecture.md`.

Este documento descreve a camada de coordenacao entre modelo geral, especialistas e subespecialistas do projeto CK3 PT-BR.

## Objetivo

Transformar o classificador geral em um coordenador operacional que sabe quando usar especialistas de escopo menor.

O modelo geral continua avaliando o pacote inteiro. Os especialistas atuam como votos por dominio. As regras deterministicas continuam acima de todos como travas de seguranca.

## Fluxo Alvo

```text
validacoes deterministicas
        |
modelo geral macro
        |
coordenador / roteador
        |
especialistas e subespecialistas
        |
microagentes por habilidade
        |
ledger de problemas e reparos
        |
politica final de seguranca
        |
auto-safe / revisao humana / bloqueio / correcao
```

## Papeis

- `deterministic_guards`: trava estrutural. Tokens, placeholders, locked human, estrutura e problemas graves sempre bloqueiam antes de qualquer ML.
- `general_macro`: modelo geral para todo o pacote. Ele e o baseline.
- `coordinator_ensemble_v1`: coordenador que combina score geral, votos especialistas, aprendizado humano e politica operacional.
- `specialist`: agente amplo por familia, como `religion`.
- `subspecialist`: agente estreito, como `religion_bosnian_terms` ou `title_adjectives`.
- `microagent`: agente transversal por habilidade, como token de genero, espanhol residual, fronteira de texto ou estilo de label curta.
- `issue_ledger`: memoria operacional de problemas detectados, evidencias, propostas de reparo e validacoes por segmento.

## Estados

- `authoritative`: trava deterministica com prioridade maxima.
- `operational`: agente ja usado pela politica operacional.
- `dry_run`: coordenador ou mecanismo em modo analitico.
- `experimental`: agente treinavel e pontuavel, mas ainda sem autoridade para promocao automatica.
- `candidate`: ideia de agente ainda em observacao.

## Politica de Decisao

1. Se uma trava deterministica bloqueia, nenhum modelo pode liberar.
2. Se o segmento tem locked human, nenhum modelo sobrescreve.
3. Se o geral e seguro e um especialista discorda, a divergencia vira auditoria; por padrao o sistema protege a decisao mais cautelosa.
4. Se o geral segura e um especialista libera, a promocao exige especialista operacional, falso-seguro zero, escopo sem drift, revisao humana positiva e ausencia de negativo aprendido para o segmento.
5. Se varios especialistas conflitam, o coordenador bloqueia e manda para revisao.
6. Se um segmento tem varios problemas pequenos, o coordenador deve chamar microagentes por habilidade e compor uma decisao final apenas depois das validacoes locais.
7. Um especialista de dominio pode arbitrar estilo/contexto, mas nao precisa resolver sozinho todos os reparos internos do segmento.

## Tabelas

### `ml_agent_registry`

Registro oficial dos agentes.

Campos principais:

- `agent_key`: chave logica do agente.
- `agent_type`: `guard`, `macro_model`, `coordinator`, `specialist`, `subspecialist`.
- `parent_agent_key`: hierarquia.
- `model_kind`: modelo associado em `ml_model_runs`, quando existir.
- `status`: `active` ou `planned`.
- `operational_state`: `authoritative`, `operational`, `dry_run`, `experimental`, `candidate`.
- `decision_role`: papel na decisao.
- `scope_sql`: escopo logico do agente.
- `default_threshold`: threshold sugerido.

### `ml_agent_routing_runs`

Fotografia de uma rodada de arquitetura/roteamento. Nesta fase registra metadados e agregados; nao altera score, confirmacoes ou output.

### `ml_agent_routing_items`

Materializacao amostrada do roteador por segmento/agente.

Cada run de arquitetura grava uma amostra limitada por agente com:

- acao do modelo geral;
- acao da politica;
- acao do especialista/subespecialista, quando houver score;
- status do agente no roteamento;
- confianca do voto especialista, quando disponivel.

O limite atual e 250 segmentos por agente para manter o banco leve.

### `ml_agent_recommendations`

Recomendacoes de criacao, treinamento ou reforco de subagentes baseadas em evidencia humana.

### `ml_issue_ledger_runs`

Fotografia de uma rodada de ledger por problema.

Campos principais:

- `segment_state_run_id`: snapshot de estado usado como base.
- `pending_segments_count`: volume pendente no snapshot.
- `ledger_segment_count`: segmentos com pelo menos um item de problema.
- `ledger_item_count`: total de problemas/habilidades materializados.
- `family_counts_json`: contagem por familia de microagente.
- `agent_counts_json`: contagem por agente roteado.

### `ml_issue_ledger_items`

Itens de problema por segmento. Um segmento pode ter varios itens.

Campos principais:

- `issue_family`: familia do problema, como `gender_token_microagent`.
- `issue_kind`: subtipo detectado, como `select_cstring_gender_literal`.
- `agent_key`: microagente sugerido.
- `proposed_action`: proxima acao recomendada.
- `token_impact`: risco/relacao com tokens.
- `evidence_json`: sinais usados para gerar o item.
- `status`: `open` ou `blocked` nesta primeira versao.

### `ml_issue_review_queue_runs`

Rodada de fila de revisao criada a partir do ledger.

Campos principais:

- `ledger_run_id`: ledger usado como fonte.
- `agent_key`: microagente alvo.
- `queue_strategy`: estrategia de selecao, hoje `stratified_issue_ledger`.
- `selected_count`: itens selecionados.
- `bucket_counts_json`: distribuicao da fila por bucket.
- `csv_path`, `jsonl_path`, `decisions_template_path`: arquivos para revisao/ingestao futura.

### `ml_issue_review_queue_items`

Itens concretos da fila de revisao.

Campos principais:

- `ledger_item_id`: item de problema original.
- `queue_bucket`: estrato usado para diversificar a fila.
- `priority_score`: prioridade local da fila.
- `suggested_decision`: decisao sugerida para o revisor avaliar.
- `review_status`: `pending` nesta fase inicial.
- `reviewer_decision`, `corrected_text`, `reviewer_notes`: campos preparados para ingestao futura.

### `ml_issue_review_decision_runs`

Rodada de ingestao das decisoes humanas preenchidas a partir de uma fila de problemas.

Campos principais:

- `queue_run_id`: fila revisada, quando a ingestao pertence a uma fila unica.
- `agent_key`: microagente afetado pelas decisoes.
- `decisions_path`: arquivo JSONL/JSON/CSV consumido.
- `accepted_count`, `skipped_count`, `invalid_count`: controle de qualidade da ingestao.
- `safe_count`, `repair_count`, `context_count`, `new_microagent_count`: tipos de evidencia gerada.
- `report_path`: relatorio auditavel da ingestao.

### `ml_issue_review_decisions`

Decisoes humanas aceitas por item da fila.

Campos principais:

- `queue_item_id`: item revisado na fila.
- `ledger_item_id`: problema original no ledger.
- `normalized_decision`: decisao normalizada, como `safe_short_label`, `needs_repair` ou `needs_new_microagent`.
- `evidence_label`: classe de evidencia usada pela rede, como `positive_evidence` ou `repair_required`.
- `corrected_text`: texto corrigido quando a decisao inclui reparo.
- `reasons_json`: trilha com regra, decisao original e contexto da fila.

## Proxima Camada: Microagentes

Diagnostico de 2026-06-03 mostrou que as pendencias atuais nao sao principalmente segmentos sem revisao. Elas sao linhas ja confirmadas no passado, mas reabertas pelo estado composto moderno por sinais de qualidade.

Referencia:

- Segment-state run: 139.
- Pendentes operacionais: 13.736.
- `needs_apply`: 0.
- `reopen_auto_confirmed_autofix`: 13.728.

Familias primarias nas pendencias:

- `short_label_style_microagent`: 5.720.
- `gender_token_microagent`: 3.297.
- `dynamic_ck3_expression_microagent`: 1.963.
- `autofix_unknown_microagent`: 1.232.
- `title_policy_microagent`: 750.
- `religion_semantic_microagent`: 322.
- `culture_semantic_microagent`: 223.
- `spanish_residual_microagent`: 181.

Decisao arquitetural:

- reduzir a dependencia de especialistas que tentam fechar o segmento inteiro;
- criar ledger de problemas por segmento;
- permitir que varios microagentes votem/reparem partes do mesmo segmento;
- usar especialistas de dominio como validadores de contexto e estilo;
- medir promocao/descarte por ganho liquido, falso seguro, cobertura e reducao real de pendencias apos producao.

## Subagentes de Religiao

Os seguintes subagentes ja foram registrados como escopos treinaveis:

- `religion_bosnian_terms`
- `religion_sufri`
- `religion_possessive_gods`
- `religion_preserved_terms`

Estado atual esperado:

- `status = active`
- `operational_state = experimental`
- `decision_role = vote`
- `parent_agent_key = religion`

Eles podem ser treinados e pontuados, mas ainda nao devem ser promovidos para a politica operacional sem auditoria.

### `religion_bosnian_terms`

Escopo:

```sql
relative_path = 'religion/religion_christianity_l_spanish.yml'
AND source_key LIKE 'bosnian_%'
```

Motivo: termos religiosos bosnios podem precisar preservar titulos ou formas especificas em vez de traducao literal.

### `religion_sufri`

Escopo:

```sql
relative_path = 'religion/religion_islam_l_spanish.yml'
AND source_key LIKE 'sufri_%'
```

Motivo: `Sufri/Sufrism` nao deve ser confundido com `Sufi/Sufism`.

### `religion_possessive_gods`

Escopo: campos possessivos religiosos.

Motivo: muitos casos precisam preservar preposicao em PT-BR, como `de Perun`, `de Erlik` ou `da morte`.

### `religion_preserved_terms`

Escopo: termos religiosos especificos que podem precisar ser preservados em vez de traduzidos genericamente.

Motivo: exemplos como `txiv neeb` e `ntuj cub tawg`.

## Comandos

Sincronizar arquitetura no banco:

```powershell
python pipeline\main.py ml-agent-architecture
```

Montar datasets dos subagentes de religiao sem treinar:

```powershell
python pipeline\main.py ml-specialist-models --ml-specialist religion_subspecialists --ml-specialist-dataset-only
```

Treinar apenas os subagentes promissores:

```powershell
python pipeline\main.py ml-specialist-models --ml-specialist religion_promising_subspecialists
```

Pontuar os subagentes promissores:

```powershell
python pipeline\main.py ml-specialist-score --ml-specialist religion_promising_subspecialists
```

Gerar fila de auditoria/calibracao dos subagentes experimentais:

```powershell
python pipeline\main.py ml-agent-audit-queue --ml-specialist religion_promising_subspecialists --auto-limit 60 --triage-batch-size 20
```

Materializar ledger de problemas para as pendencias atuais:

```powershell
python pipeline\main.py issue-ledger
```

Gerar fila estratificada para um microagente:

```powershell
python pipeline\main.py issue-review-queue --auto-limit 120 --issue-agent-key micro_short_label_style --issue-queue-per-bucket 20
```

Gerar um rascunho conservador de decisoes para auditoria assistida:

```powershell
python pipeline\issue_review_assisted_draft.py --queue-jsonl reports\20260603_122102_issue_review_queue_micro_short_label_style.jsonl --reviewer codex_assisted_micro_short_label_v2
```

Ingerir as decisoes preenchidas pelo revisor:

```powershell
python pipeline\main.py issue-review-ingest --issue-review-decisions reports\20260603_122102_issue_review_queue_micro_short_label_style_decisions_template.jsonl --issue-review-queue-run-id 2 --issue-reviewer Emerson
```

Decisoes aceitas pela ingestao:

- `safe_short_label`: o item da fila era seguro como rotulo curto.
- `false_positive_reopen`: a reabertura foi falso positivo; evidencia positiva para calibracao.
- `needs_repair`: precisa de correcao textual ou estrutural.
- `needs_domain_context`: precisa de contexto de dominio antes de decidir.
- `needs_new_microagent`: sinal de que a rede deve criar/reforcar um neuronio especializado.
- `manual_exception`: excecao manual valida, sem generalizar como regra ampla.

### Ponte Para Dataset Supervisionado

A partir de `ml_build_dataset_v3_issue_review_bridge`, as decisoes em `ml_issue_review_decisions` entram no dataset supervisionado com `evidence_source = issue_review_decision`.

Mapeamento atual:

- `safe_short_label` e `false_positive_reopen`: exemplos positivos treinaveis para o macro.
- `needs_repair`: exemplo negativo treinavel, normalmente `needs_autofix`; quando a nota contem `spanish_residual`, vira `issue_label = residual_spanish`.
- `needs_domain_context`: evidencia neutra de roteamento, sem `candidate_text`, para nao ensinar o macro a bloquear todo texto que apenas precisa de outro especialista.
- `needs_new_microagent`: evidencia neutra de arquitetura, sem `candidate_text`, usada para sugerir criacao/reforco de neuronio.

Checkpoint de 2026-06-03:

- Dataset run: 399.
- `issue_review_decision`: 120 exemplos.
- Treinaveis pelo macro: 49.
- Neutros de coordenacao/arquitetura: 70.
- Modelo candidato macro: 320.
- Resultado: nao promover; houve regressao de falso seguro (`2`) contra o ativo (`0`).
- Regra de promocao: a ponte aumenta evidencia e pode melhorar metricas parciais, mas candidato com falso-seguro acima do modelo ativo permanece apenas como aprendizado, sem autoridade operacional.

Checkpoint posterior de 2026-06-03:

- `ml_train_risk_v2_label_aware_minor_fix` corrigiu a interpretacao de `minor_fix`:
  - `minor_fix` sem mudanca real entre candidato e final vira `auto_safe`;
  - `minor_fix` com texto diferente vira `needs_autofix`, nao `needs_human` generico.
- Modelo candidato macro: 321.
- Split interno: `0` falso-seguro, `100%` de precisao safe, `30,48%` de safe recall.
- Score analitico do pacote: 97.166 `final_auto_safe` em 221.564 segmentos pontuados (`43,85%`).
- Holdout por arquivo do dataset 399: `14` falso-seguros em 2.918 predicted-safe (`0,48%`).
- Falso-seguro de holdout concentrou em `titles_cultural_names_l_spanish.yml` e `titles_l_spanish.yml`, especialmente chaves `*_adj`.
- Threshold sweep mostrou que `0,95` zera falso-seguro no holdout, mas reduz a cobertura de holdout para 303 predicted-safe.
- Decisao: nao promover macro 321. O macro deve permanecer cauteloso; a recuperacao de cobertura deve vir de especialistas/subagentes de titulos culturais e adjetivos, com guardas de holdout.

Checkpoint de guarda `title_cultural_adj` em 2026-06-03:

- `ml_train_risk_v3_title_cultural_adj_guard` adicionou travas para:
  - adjetivos de titulos/culturas `*_adj` com capitalizacao insegura;
  - sufixos/direcoes espanholas em adjetivos culturais (`-e\u00f1o`, `-\u00e9s`, `sur`);
  - nomes de titulos com capitalizacao interna suspeita;
  - textos conhecidos inseguros como `b_ourense = Orense`.
- A trava cobriu 14/14 falso-seguros da fila de holdout anterior.
- Modelo candidato macro: 322.
- Split interno: `0` falso-seguro, `100%` de precisao safe, `30,89%` de safe recall.
- Holdout por arquivo do dataset 399: `0` falso-seguros em 2.877 predicted-safe.
- Threshold sweep voltou a recomendar `0,90` com falso-seguro zero.
- Score analitico do pacote: 97.523 `final_auto_safe` em 221.564 segmentos pontuados (`44,02%`).
- Decisao do portao: `do_not_promote`, por regressao de cobertura operacional contra o ativo (`44,02%` vs `49,86%`, queda `11,72%`, limite `10%`).
- Interpretacao: o neuronio melhorou seguranca e deve permanecer como trava/feature; a promocao do macro ainda depende de recuperar cobertura com especialistas seguros, nao de relaxar o holdout.

Checkpoint de recuperacao de cobertura em 2026-06-03:

- Teste de `safe_multiplier = 6` aumentou cobertura sem perder holdout, mas expôs um falso-seguro pontual em `custom_localization/es_custom_loc_l_spanish.yml::Loc_ES_matrimonio_1`.
- Foi adicionada trava conhecida para o fragmento inseguro `[CHARACTER.GetUIName] se`.
- Modelo candidato macro: 324.
- Split interno: `0` falso-seguro em 764 predicted-safe.
- Holdout por arquivo do dataset 399: `0` falso-seguro em 3.139 predicted-safe.
- Score analitico do pacote: 101.012 `final_auto_safe` em 221.564 segmentos pontuados (`45,59%`).
- Comparacao justa no pacote atual:
  - ativo 76 / score 336: 116.334 `final_auto_safe` (`52,51%`);
  - candidato 324 / score 335: 101.012 `final_auto_safe` (`45,59%`).
- Decisao do portao: `do_not_promote`, por regressao de cobertura operacional (`13,17%`, limite `10%`).
- Interpretacao: candidato 324 e seguro o bastante para continuar como candidato de aprendizado, mas ainda precisa recuperar aproximadamente 3.700 liberacoes seguras para passar o gate de cobertura, ou 15.300 para empatar com o ativo no pacote atual.

Checkpoint de calibracao conservadora em 2026-06-03:

- Threshold sweep com `safe_multiplier = 6` indicou `auto_min_score = 0,89` como ponto maximo seguro no holdout.
- O threshold `0,88` foi rejeitado por gerar 4 falso-seguros no holdout, concentrados em `culture/culture_titles_l_spanish.yml` e `religion/religion_paganism_l_spanish.yml`.
- Modelo candidato macro: 326.
- Split interno: `0` falso-seguro em 818 predicted-safe.
- Holdout por arquivo do dataset 399: `0` falso-seguro em 3.523 predicted-safe, com `100%` de precisao safe e `26,30%` de safe recall.
- Score analitico do pacote: 103.111 `final_auto_safe` em 221.564 segmentos pontuados (`46,54%`).
- Comparacao justa no pacote atual:
  - ativo 76 / score 336: 116.334 `final_auto_safe` (`52,51%`);
  - candidato 326 / score 337: 103.111 `final_auto_safe` (`46,54%`).
- Decisao do portao: `do_not_promote`, por regressao de cobertura operacional (`11,37%`, limite `10%`).
- A fila de regressao ativa contra candidato mostrou 15.868 segmentos que o ativo libera e o candidato ainda segura:
  - `needs_autofix`: 12.078;
  - `needs_human`: 3.790;
  - principais caminhos: `titles_l_spanish.yml` (2.603), `effects_l_spanish.yml` (571), `buildings_l_spanish.yml` (519), `interactions_l_spanish.yml` (429), `triggers/character_triggers_l_spanish.yml` (363).
- Interpretacao: a calibracao macro recuperou cobertura com seguranca, mas nao resolve a lacuna sozinha. O proximo ganho deve vir de politicas/agentes especializados por familia, especialmente labels e nomes de titulos, UI curta, efeitos/interacoes e blocos de DLC.

Checkpoint de coordenacao de titulos em 2026-06-03:

- O ensemble de especialistas foi ajustado para permitir democoes pendentes quando `protect_general_safe = True`, desde que nao existam `pending_new_safe`. Assim o coordenador pode aproveitar votos positivos revisados sem deixar um especialista derrubar o macro.
- `culture_title_labels` entrou no ensemble protegido com 80 novos seguros e 18 democoes apenas auditadas.
- Foram revisados 13 `specialist_new_safe_review` pendentes:
  - 5 nomes proprios de titulo preservados como `correct`;
  - 7 adjetivos toponimicos curtos como `correct`;
  - 1 excecao contextual (`c_rijeka_adj = fluminense`) como `contextual_exception`, sem generalizar regra ampla.
- Apos a ingestao, `title_names`, `title_adjectives` e `culture_title_labels` ficaram `READY_CONSERVATIVE_AUDIT` contra o macro candidato 326.
- Ensemble protegido com `title_promising_subspecialists`:
  - score base 337 / macro 326: 103.111 `auto_safe`;
  - ensemble: 103.239 `auto_safe`;
  - novos seguros por especialista: 128;
  - democoes protegidas/auditadas: 646.
- Politica de grupo no score 337 adiciona 604 novos seguros; a uniao com o ensemble soma 631 segmentos unicos, projetando 103.742 `auto_safe`.
- O novo dataset 400 incorporou os 13 aprendizados, mas o macro treinado como modelo 327 ficou mais conservador:
  - split interno: `0` falso-seguro em 821 predicted-safe;
  - holdout: `0` falso-seguro em 2.766 predicted-safe;
  - score analitico: 102.229 `auto_safe` (`46,14%`);
  - decisao: `do_not_promote`, regressao operacional de `12,12%`.
- Interpretacao: estes exemplos fortalecem a camada especializada/coordenadora, nao o macro geral. O melhor candidato macro continua sendo 326, enquanto o proximo ganho deve vir de novos blocos governados pelo coordenador.

Checkpoint de prospeccao de bloco `title_preserved_old_output` em 2026-06-03:

- Diagnostico de regressao entre ativo 76 / score 336 e candidato macro 326 / score 337 mostrou 15.868 segmentos que o ativo libera e o candidato segura.
- O maior padrao recuperavel em titulos e `candidate_text == spanish_text == old_text == output_text` em `titles_l_spanish.yml`.
- Potencial bruto:
  - 2.594 regressoes em `titles_l_spanish.yml` com texto preservado de espanhol/old/output;
  - apos excluir revisados e negativos conhecidos, 2.273 candidatos limpos para amostragem.
- Contraexemplos conhecidos impedem promocao ampla sem amostra:
  - `b_ourense = Orense` marcado como `residual_spanish`;
  - `c_saintois_adj = saintoises` marcado como `residual_spanish`;
  - `c_west-tokharestan = Tojaristan Oeste` marcado como `residual_spanish`.
- Foi criado `pipeline/ml_regression_pattern_queue.py` para gerar filas estratificadas desse tipo de regressao sem tocar output/modelos.
- A fila v1 estava enviesada por ordem de bucket. A v2 mudou a selecao para round-robin por `key_shape:probability_bucket`, mantendo evidencia balanceada.
- Fila v2 gerada:
  - relatorio: `reports/20260603_165420_title_preserved_regression_queue.txt`;
  - template: `reports/20260603_165420_title_preserved_regression_review_decisions_template.json`;
  - candidatos encontrados: 2.273;
  - selecionados para revisao: 120;
  - cobertura da amostra: adjetivos, baronias, condados, ducados, reinos, imperios e um caso `other`.
- Triagem assistida de alta confianca:
  - arquivo: `reports/20260603_165420_title_preserved_regression_review_decisions_codex_confident_subset.json`;
  - ingeridos: 81 exemplos;
  - positivos/contextuais: 38 (`34 correct`, `4 contextual_exception`);
  - negativos/rejeitados: 43 (`42 residual_spanish`, `1 rejected_suggestion`);
  - decisao: linhas ambiguas ficaram fora do aprendizado para evitar ensinar ruido.
- Resultado de aprendizado:
  - `learn-feedback`: 81 candidatos aprendidos, 648 padroes atualizados;
  - dataset 401: 90.942 exemplos, 1.685 negativos;
  - modelo 328: split interno com `0/788` falso-seguro, Macro F1 `0,2688`;
  - score recortado em titulos com locked incluido, run 342: 24.328/30.416 `auto_safe` (`79,98%`);
  - no subset ingerido, o modelo 328 nao liberou nenhum negativo como `auto_safe`, mas tambem nao liberou os 38 positivos/contextuais.
- Interpretacao: o bloco nao deve virar politica ampla. A evidencia aponta para micro-neuronios:
  - `title_direction_residual_guard`: bloqueia `Noreste`, `Sureste`, `Occidental`, `Ruta de`;
  - `title_exonym_residual_guard`: bloqueia formas espanholas comuns como `Bruselas`, `Egipto`, `Rusia`, `Venecia`, `Nankin`;
  - `title_demonym_suffix_guard`: bloqueia demonimos com sufixos espanhois como `-es` em contexto normando/anglo-saxao;
  - `title_safe_exonym_release`: libera apenas exonimos PT-BR revisados e repetiveis como `Baviera`, `Gales`, `Inglaterra`, `Brabante`, `Dinamarca`.
- O ganho deve ser governado pelo coordenador: guards negativos primeiro, releases positivos apenas com evidencia repetida. O macro 328 nao e candidato a promocao geral.

Auditoria shadow dos guards de `title_preserved_old_output` em 2026-06-03:

- Script: `pipeline/title_preserved_guard_audit.py`.
- Relatorio: `reports/20260603_175523_title_preserved_guard_audit.txt`.
- Template de revisao: `reports/20260603_175523_title_preserved_guard_review_template.json`.
- Escopo medido:
  - `titles_l_spanish.yml`;
  - `spanish_text == old_text == current output_text`;
  - sem escrita em source/output/modelos.
- Resultado:
  - linhas preservadas escaneadas: 29.336;
  - hits de guard: 82;
  - segmentos unicos atingidos: 82;
  - candidatos selecionados para revisao: 81.
- Por guard:
  - `title_preserved_direction_residual_guard`: 36 hits, 7 negativos revisados, 29 sem revisao, 27 eram `auto_safe` no ativo 76/score 336, 1 era `auto_safe` no macro 326/score 337, 0 no modelo 328/score 342, 9 pendentes no segment-state;
  - `title_preserved_exonym_residual_guard`: 41 hits, 24 negativos revisados, 17 sem revisao, 40 eram `auto_safe` no ativo 76/score 336, 2 eram `auto_safe` no macro 326/score 337, 0 no modelo 328/score 342, 11 pendentes no segment-state;
  - `title_preserved_demonym_suffix_guard`: 5 hits, 4 negativos revisados, 1 sem revisao, 4 eram `auto_safe` no ativo 76/score 336, 0 no macro 326/score 337, 0 no modelo 328/score 342, 0 pendentes no segment-state.
- Interpretacao:
  - a abordagem antiga de cobertura ampla liberava 71/82 hits como seguros;
  - o macro candidato conservador bloqueia quase tudo, mas perde cobertura demais no pacote geral;
  - a abordagem modular permite manter cobertura antiga onde ela e boa e inserir guards estreitos onde ha evidencia de falso-seguro;
  - estes guards reduzem risco e criam fila de correcao/auditoria, mas nao fecham pendencias sozinhos.

Revisao da fila dos guards em 2026-06-03:

- Decisoes: `reports/20260603_175523_title_preserved_guard_review_decisions_codex.json`.
- Aplicacao de revisao humana local: lotes 533-536.
- Foram ingeridos 81 novos `residual_spanish` confiantes:
  - `learn-feedback`: 81 candidatos aprendidos, 648 padroes atualizados;
  - dataset 402: 91.023 exemplos, 1.766 negativos (`1,94%`);
  - modelo 329: split interno `0/870` falso-seguro, Macro F1 `0,2657`;
  - score recortado em titulos com locked incluido, run 343: 24.384/30.416 `auto_safe` (`80,17%`).
- Validacao dos 81 negativos revisados:
  - score ativo antigo 336: 70/81 ainda seriam `auto_safe`;
  - macro candidato 337: 3/81 ainda seriam `auto_safe`;
  - modelo 328 / score 342: 0/81 `auto_safe`;
  - modelo 329 / score 343: 0/81 `auto_safe`.
- Interpretacao:
  - a nova abordagem nao gerou grande aumento imediato de cobertura;
  - ela reduziu falso-seguro conhecido em um ponto especifico onde a cobertura antiga era agressiva demais;
  - o proximo ganho real depende de uma camada de correcao/release para converter estes bloqueios em segmentos fechados.

Checkpoint do issue ledger em 2026-06-03:

- Diagnostico: `reports/20260603_182741_pending_architecture_diagnostic.txt`.
- Ledger: `reports/20260603_182744_issue_ledger.txt`.
- Segment-state base: run 139.
- Estado do pacote:
  - total ativo: 288.100 segmentos;
  - fechado/consolidado: 274.364 (`95,23%`);
  - pendente operacional: 13.736 (`4,77%`);
  - precisa aplicar output: 0.
- O ledger inspecionou 13.736 pendentes e criou 25.422 issues, media de `1,85` problemas por segmento pendente.
- Isso confirma a mudanca arquitetural:
  - especialistas de dominio continuam uteis para contexto;
  - mas muitos segmentos precisam ser quebrados em problemas menores;
  - microagentes devem tratar habilidades transversais;
  - um compositor/coordenador deve juntar os reparos e so fechar quando todas as travas concordarem.
- Familias de issue no ledger:
  - `short_label_style_microagent`: 10.442 (`41,07%`);
  - `dynamic_ck3_expression_microagent`: 5.137 (`20,21%`);
  - `gender_token_microagent`: 3.300 (`12,98%`);
  - `semantic_review_router`: 1.689 (`6,64%`);
  - `autofix_unknown_microagent`: 1.237 (`4,87%`);
  - `spanish_residual_microagent`: 834 (`3,28%`);
  - `high_issue_auditor`: 816 (`3,21%`);
  - `title_policy_microagent`: 772 (`3,04%`);
  - `culture_semantic_microagent`: 358 (`1,41%`);
  - `religion_semantic_microagent`: 336 (`1,32%`);
  - `nickname_name_policy`: 314 (`1,24%`);
  - `surface_boundary_microagent`: 107 (`0,42%`);
  - `long_text_composer`: 73 (`0,29%`);
  - `structural_token_gate`: 7 (`0,03%`).
- Impacto de token:
  - `none_or_unknown`: 16.025;
  - `token_sensitive`: 8.429;
  - `usually_same_tokens`: 941;
  - `token_mismatch`: 27.
- Proximos alvos recomendados:
  1. construir fila de `micro_short_label_style`, por ser o maior bloco;
  2. construir fila de `micro_gender_token`, por alto impacto e risco de token;
  3. clusterizar `micro_autofix_unknown_router` antes de criar novos neuronios;
  4. manter `titles`, `religion` e `culture` como revisores de contexto depois dos micro-reparos.

Fila inicial de `micro_short_label_style` em 2026-06-03:

- Fonte: ledger run 2.
- Queue run: 3.
- Relatorio: `reports/20260603_182957_issue_review_queue_micro_short_label_style.txt`.
- JSONL: `reports/20260603_182957_issue_review_queue_micro_short_label_style.jsonl`.
- Rascunho assistido: `reports/20260603_183010_20260603_182957_issue_review_queue_micro_short_label_style_codex_assisted_micro_short_label_v1_reviewed.txt`.
- Candidatos disponiveis: 10.442.
- Selecionados: 120 (`1,15%`), com ate 20 por bucket.
- Buckets principais:
  - `active_safe_candidate_autofix`: 12;
  - `needs_human_conflict`: 12;
  - `domain_titles_names`: 12;
  - `domain_religion`: 11;
  - `domain_culture`: 11;
  - `domain_rules_tooltips`: 11;
  - `domain_events_longform`: 11;
  - `domain_interactions_activities`: 11;
  - `package_dlc`: 11;
  - `general_short_label`: 11;
  - `token_sensitive`: 7.
- Rascunho assistido:
  - `needs_domain_context`: 66;
  - `safe_short_label`: 26;
  - `needs_repair`: 17;
  - `false_positive_reopen`: 7;
  - `needs_new_microagent`: 4.
- Decisao:
  - nao ingerir automaticamente este rascunho;
  - o microagente `short_label_style` ainda esta amplo demais;
  - os 26 `safe_short_label` e 7 `false_positive_reopen` podem virar evidencia positiva depois de auditoria;
  - os 17 `needs_repair` devem alimentar microagentes de espanhol residual/surface repair;
  - os 66 `needs_domain_context` devem ser roteados para dominios ou compositor, nao para o macro como negativo simples;
  - os 4 `needs_new_microagent` reforcam que token de genero/dinamico precisa ficar com neuronio proprio.

Subclusterizacao de `micro_short_label_style` em 2026-06-03:

- Script: `pipeline/issue_review_subcluster.py`.
- Modo: `python pipeline/main.py issue-review-subcluster`.
- Relatorio final: `reports/20260603_184313_issue_review_subcluster_20260603_182957_issue_review_queue_m_4493ce88f7.txt`.
- Entrada:
  - fila JSONL: `reports/20260603_182957_issue_review_queue_micro_short_label_style.jsonl`;
  - rascunho assistido: `reports/20260603_183010_20260603_182957_issue_review_queue_micro_short_label_style_codex_assisted_micro_short_label_v1_reviewed.jsonl`.
- A primeira versao super-roteou casos longos para `token_policy_context`; a regra foi corrigida para mandar apenas `token_sensitive`, `token_policy` ou `token_mismatch` reais ao token gate.
- A subclusterizacao final dividiu 120 itens em:
  - `short_label_positive_candidate`: 32;
  - `long_or_dynamic_context`: 31;
  - `generic_domain_context_delegate`: 24;
  - `spanish_residual_repair_candidate`: 17;
  - `token_policy_context`: 4;
  - `gender_dynamic_token_delegate`: 4;
  - `culture_context_delegate`: 3;
  - `religion_context_delegate`: 2;
  - `title_context_delegate`: 2;
  - `surface_or_text_repair_candidate`: 1.
- O filtro removeu 1 falso positivo de evidencia positiva porque havia `space_before_punctuation`.
- Os 32 positivos limpos foram ingeridos via `issue-review-ingest`:
  - relatorio: `reports/20260603_184347_issue_review_ingest.txt`;
  - aceitos: 32;
  - invalidos: 0.
- Dataset 403:
  - total: 91.055 exemplos;
  - positivos: 89.219;
  - negativos: 1.766.
- Modelo macro 330:
  - dataset: 403;
  - falso-seguro interno: `0/834`;
  - Macro F1: `0,2616`;
  - score comparativo limitado a 50.000 segmentos:
    - modelo 329 / score 346: 3.544 `auto_safe`;
    - modelo 330 / score 347: 3.544 `auto_safe`;
    - houve redistribuicao de `needs_autofix` para `needs_human`, sem ganho de cobertura.
- Cruzamento dos 32 positivos com o recorte score 346/347:
  - 10 apareceram no recorte;
  - nenhum virou `auto_safe`;
  - 1 passou de `needs_autofix` para `needs_human`.
- Interpretacao:
  - estes exemplos sao bons para calibracao e memoria;
  - mas o macro nao esta aproveitando esta evidencia como cobertura;
  - o proximo ganho deve vir de uma subpolitica guardada `short_label_positive_release`, governada pelo coordenador, nao de promover o macro 330.

Subpolitica guardada `short_label_positive_release` em 2026-06-03:

- Script: `pipeline/issue_review_short_label_positive_release.py`.
- Modo: `python pipeline/main.py issue-review-short-label-positive-release --issue-agent-key micro_short_label_style`.
- Tabelas:
  - `ml_issue_short_label_release_runs`;
  - `ml_issue_short_label_release_items`.
- Primeiro run shadow:
  - release run id: 1;
  - decision run id: 3;
  - candidatos: 32;
  - liberados shadow: 32;
  - bloqueados: 0;
  - ganho estimado de fechamento: 32;
  - relatorio: `reports/20260603_191452_808576_issue_short_label_positive_release.txt`.
- Portoes por linha:
  - decisao aceita em `ml_issue_review_decisions`;
  - `safe_short_label`/`positive_evidence` ou `false_positive_reopen`/`false_positive_reopen`;
  - item da fila revisado e sem `corrected_text`;
  - issue family/kind de short label reaberto;
  - `token_status=ok`;
  - `token_impact=none_or_unknown` ou equivalente same-token;
  - sem `issue_codes`;
  - texto atual da confirmacao igual ao texto revisado na fila;
  - estado atual ainda em `reopen_auto_confirmed` ou `reopen_auto_confirmed_autofix`;
  - sem lock manual;
  - limites conservadores de tamanho e tokens.
- Interpretacao:
  - o macro 330 nao deve ser promovido por este ganho;
  - a subpolitica prova que o coordenador pode usar evidencia positiva estreita para liberar um pacote pequeno com travas;
  - ainda e shadow: nao escreve output, nao altera segment-state e nao fecha confirmacoes por conta propria.

Checkpoint guardado de `short_label_positive_release` em 2026-06-03:

- Script: `pipeline/issue_review_short_label_positive_checkpoint.py`.
- Modo: `python pipeline/main.py issue-review-short-label-positive-checkpoint`.
- Tabelas:
  - `ml_issue_short_label_release_checkpoint_runs`;
  - `ml_issue_short_label_release_checkpoint_items`.
- Primeiro checkpoint:
  - checkpoint run id: 1;
  - release run id: 1;
  - status: `ready_for_guarded_lifecycle_policy`;
  - promotion status: `guarded_candidate`;
  - candidatos: 32;
  - allowlistados: 32;
  - bloqueados: 0;
  - relatorio: `reports/20260603_192821_472762_issue_short_label_positive_checkpoint.txt`.
- Portoes adicionais:
  - release run precisa estar em `policy_status=shadow`;
  - release nao pode ter bloqueios;
  - hash da confirmacao atual precisa bater com o hash validado no release;
  - segment-state atual ainda precisa estar em reopen auto-confirmed;
  - segmento nao pode estar locked nem fechado;
  - `production_release_allowed=0`.
- Interpretacao:
  - o checkpoint e uma allowlist governada, nao uma aplicacao;
  - a futura etapa de lifecycle/producao pode consumir estes 32 com rastreabilidade;
  - se alguma linha mudar antes da producao consumir, o checkpoint seguinte deve bloquear por hash/state stale.

## Estado Atual

A implementacao atual e analitica e segura:

- cria o registro de agentes;
- cria snapshots de arquitetura;
- materializa uma amostra de roteamento por agente;
- escreve recomendacoes de treinamento/reforco;
- permite datasets, treino e score em subagentes experimentais;
- gera filas de auditoria/calibracao para alimentar aprendizado humano por agente;
- alimenta o dashboard;
- nao muda output;
- nao promove modelos automaticamente;
- nao altera confirmacoes humanas.

## Estado Operacional Atual

Referencia em 2026-05-28:

- `coordinator_ensemble_v1` opera como roteador/arbrito em modo guardado.
- Gate composto ativo: overlay 33.
- Releases guardados ativos: 285.
- `same_scope_es_oa_to_es_xa_article_candidate` foi promovido como perfil guardado.
- `auto_apply_allowed = 0`.
- `invalid_releases = 0`.
- Falso seguro operacional: 0.
- Falso seguro experimental conhecido: 2 no especialista legado amplo `specialist_titles`.
