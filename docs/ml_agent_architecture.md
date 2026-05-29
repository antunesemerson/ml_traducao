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
