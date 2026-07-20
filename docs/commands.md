# Guia De Comandos

Este arquivo reúne os principais comandos do projeto CK3 PT-BR.

Os comandos devem ser executados na raiz do projeto:

```powershell
Set-Location -LiteralPath "<caminho-do-projeto>"
```

## Setup E Ciclo Base

Preparar banco, schema e índices:

```powershell
python pipeline\main.py setup
```

Rodar ciclo base de análise, memória e sugestões:

```powershell
python pipeline\main.py cycle
```

Ver relatório de confirmações e cobertura:

```powershell
python pipeline\main.py confirmations
```

## Aplicação Do Output

Aplicar apenas confirmacoes humanas ja consolidadas no output:

```powershell
python pipeline\main.py segment-apply --auto-limit 500 --auto-apply
```

Gerar fila bruta de confirmacoes humanas bloqueadas por diferenca de token:

```powershell
python pipeline\main.py segment-token-queue
```

Classificar esses bloqueios em buckets de politica:

```powershell
python pipeline\main.py segment-token-policy
```

Auditar limpeza estrita dos textos confirmados com mojibake no gate:

```powershell
python pipeline\main.py mojibake-strict
```

Gerar uma fila balanceada para revisar excecoes de token por bucket:

```powershell
python pipeline\main.py segment-token-policy-queue --token-policy-per-bucket 25
```

Ingerir decisoes humanas de uma fila de politica de token, sem aplicar output:

```powershell
python pipeline\main.py segment-token-policy-decisions --token-policy-run-id 6 --token-policy-decisions reports\decisions_token_policy.jsonl --token-policy-source-report reports\FILA_ORIGINAL.csv
```

Corrigir `confirmed_text` a partir de decisoes `fix_confirmed_text`, sem aplicar output:

```powershell
python pipeline\main.py segment-token-confirmation-fixes --token-policy-run-id 6 --auto-apply
```

Aplicar somente excecoes de token ja aprovadas por decisao humana:

```powershell
python pipeline\main.py segment-apply --segment-require-token-policy-decision --token-policy-run-id 6 --auto-apply
```

Gerar fila focada em uma familia de risco:

```powershell
python pipeline\main.py segment-token-policy-queue --token-policy-buckets review_gender_token_change --token-policy-per-bucket 40
```

Gerar output inicial a partir de `source/spanish_old`:

```powershell
python pipeline\main.py apply --bootstrap-old
```

Aplicar atualizações seguras no output:

```powershell
python pipeline\main.py apply
```

Rodar ciclo completo e aplicação:

```powershell
python pipeline\main.py full
```

Use aplicação com cuidado. Experimentos de ML, score, holdout e filas de revisão não precisam alterar `output/spanish`.

## Aprendizado Local

Absorver feedback humano/local já revisado:

```powershell
python pipeline\main.py learn-feedback
```

Gerar baseline estatístico:

```powershell
python pipeline\main.py ml-baseline
```

Construir dataset supervisionado:

```powershell
python pipeline\main.py ml-dataset
```

Treinar classificador local:

```powershell
python pipeline\main.py ml-train-risk
```

Treinar com feature set/estratégia específicos:

```powershell
python pipeline\main.py ml-train-risk --ml-feature-set language_v4 --ml-train-strategy dedup_weighted_v2
```

## Avaliação ML

Avaliar holdout por arquivo/pacote:

```powershell
python pipeline\main.py ml-holdout-eval
```

Gerar fila de revisão para falsos seguros do holdout:

```powershell
python pipeline\main.py ml-holdout-review-queue
```

Pontuar corpus real com modelo específico:

```powershell
python pipeline\main.py ml-score --ml-model-run-id 128 --ml-score-batch-size 10000
```

Auditar score:

```powershell
python pipeline\main.py ml-score-audit --auto-limit 25
```

Criar fila de regressão entre modelo ativo e candidato:

```powershell
python pipeline\main.py ml-score-regression-queue --ml-active-score-run-id 36 --ml-candidate-score-run-id 78 --auto-limit 120 --ml-per-path-limit 8
```

Comparar/promover modelo em dry-run:

```powershell
python pipeline\main.py ml-promote-model --ml-active-score-run-id 36 --ml-candidate-score-run-id 78
```

Promover modelo somente se a política aprovar e a aplicação for intencional:

```powershell
python pipeline\main.py ml-promote-model --ml-active-score-run-id 36 --ml-candidate-score-run-id 78 --auto-apply
```

Exportar modelo ativo para backup:

```powershell
python pipeline\main.py ml-export-model
```

## Política Por Grupo

Simular política por grupo sobre um score:

```powershell
python pipeline\main.py ml-group-threshold-policy --ml-active-score-run-id 78
```

Gerar fila de auditoria de novos seguros da política:

```powershell
python pipeline\main.py ml-policy-audit-queue --policy-audit-focus new_safe --auto-limit 100
```

Gerar fila filtrada por grupos:

```powershell
python pipeline\main.py ml-policy-audit-queue --policy-audit-focus new_safe --ml-groups religion_possessive_lowercase,title_directional_north --auto-limit 90
```

## Revisão Paralela

Preparar lote de revisão de política:

```powershell
python pipeline\parallel_review_loop.py prepare-policy --batches 3 --batch-size 30 --queue reports\NOME_DA_FILA.csv
```

Aplicar decisões estruturadas de política:

```powershell
python pipeline\parallel_review_loop.py apply-policy-jsonl reports\decisions.jsonl --source-report reports\NOME_DA_FILA.csv
```

Aplicar template JSON de revisão:

```powershell
python pipeline\parallel_review_loop.py apply reports\decisions.json
```

Depois de aplicar revisões, rode:

```powershell
python pipeline\main.py learn-feedback
python pipeline\main.py ml-dataset
```

Treine novamente apenas quando houver volume relevante de novos exemplos ou uma hipótese clara para testar.

## Progresso E Dashboard

Simular propostas de provedores para a descoberta mais recente, sem persistir:

```powershell
python pipeline\quality_provider_proposal_generator.py
```

Persistir somente os rascunhos, contratos e testes de fronteira no SQLite:

```powershell
python pipeline\quality_provider_proposal_generator.py --apply
```

Esse comando nunca escreve confirmacoes, scores ou arquivos em `output/`.

Atualizar o `segment-state` autoritativo sem gerar o relatorio auxiliar pesado:

```powershell
python pipeline\main.py segment-state
```

O snapshot concluido no SQLite define o sucesso. Para gerar o relatorio somente quando
ele for necessario, sem recalcular o snapshot:

```powershell
python pipeline\segment_state_snapshot.py --report-run-id 743
```

Tambem e possivel pedir o relatorio depois do commit em uma nova execucao:

```powershell
python pipeline\main.py segment-state --segment-state-report
```

Planejar a retencao conservadora dos detalhes historicos, sem excluir nada:

```powershell
python pipeline\sqlite_history_retention.py
```

O plano protege automaticamente epochs, versoes materializadas, runs referenciadas,
runs inacabadas e os snapshots recentes. A aplicacao exige o token `PRUNE-...` gerado
para o estado exato do banco; consulte `docs/sqlite_history_retention.md`.

Gerar relatório consolidado de progresso:

```powershell
python pipeline\main.py ml-progress
```

O dashboard deve ignorar runs incompletos:

```sql
WHERE finished_at IS NOT NULL
```

## Utilidades

Compilar scripts alterados:

```powershell
python -m py_compile pipeline\main.py pipeline\ml_train_risk.py
```

Auditar tokens de gênero:

```powershell
python pipeline\main.py gender-token-audit
```

Gerar fila micro de revisão:

```powershell
python pipeline\main.py micro-review-queue
```

## Lembretes

- `ml-score` pode demorar vários minutos.
- Runs com `finished_at IS NULL` são parciais.
- `reports/`, `logs/`, `memory/`, `source/`, `output/` e `prompts/` são ignorados no Git.
- Não altere `output/spanish` durante experimentos de ML.
- Falso seguro zero é mais importante que cobertura alta nesta fase.
### Auditar observações de qualidade fechadas

Auditoria estratificada e somente de metadados sobre as famílias marcadas como
`closed_observation`. O comando não reabre lifecycle e não escreve confirmações,
scores ou output:

```powershell
python pipeline\quality_closed_observation_audit.py --apply
```

O diagnóstico contínuo executa esta etapa logo após a descoberta de padrões. Os
totais distinguem famílias amostradas, segmentos únicos e vínculos
segmento–família para evitar dupla contagem quando um segmento aparece em mais de
uma família.
