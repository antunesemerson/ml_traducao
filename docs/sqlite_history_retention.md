# Retenção do histórico SQLite

O pipeline preserva os registros-resumo dos runs e remove somente detalhes antigos que
podem ser reconstruídos. A política protege automaticamente:

- runs referenciados por outros históricos;
- epochs de qualidade e versões materializadas;
- runs inacabados;
- os três scores mais recentes de cada lane (`legacy`, `old`, `output`);
- os cinco snapshots de segment-state mais recentes.

## 1. Simular

```powershell
python pipeline\sqlite_history_retention.py
```

A simulação é somente leitura. Ela informa quantos runs e itens são elegíveis e gera um
token `PRUNE-...` vinculado ao estado exato do banco.

## 2. Piloto controlado

Pare execuções de Diagnóstico, Avaliação e Publicável antes de aplicar. Use o token da
simulação e limite o primeiro lote:

```powershell
python pipeline\sqlite_history_retention.py --apply --confirm PRUNE-... --max-runs 5
```

Depois do piloto, gere uma nova simulação. O plano e o token mudam porque o estado do
banco mudou.

Para lotes previsíveis, selecione explicitamente a família e combine limite de runs com
um orçamento máximo de detalhes. A ferramenta para antes do run que ultrapassaria o
orçamento:

```powershell
python pipeline\sqlite_history_retention.py `
  --apply --confirm PRUNE-... `
  --scope score --max-runs 25 --max-detail-items 1000000
```

Os escopos aceitos são `score`, `state` e `all`. Para `segment_state`, use inicialmente
um lote menor, pois cada snapshot pode ter centenas de milhares de detalhes.

## 3. Retenção completa

```powershell
python pipeline\sqlite_history_retention.py --apply --confirm PRUNE-...
```

Cada run é removido em uma transação independente. A operação registra auditoria nas
tabelas `history_retention_runs` e `history_retention_scopes` e faz checkpoints do WAL.
Os IDs e resumos dos runs permanecem no banco.

Se o processo for encerrado entre duas transações, o próximo lote classifica a auditoria
órfã como `interrupted`, recompõe os totais a partir de `history_retention_scopes` e
preserva somente as exclusões que já haviam sido confirmadas. Por isso, sempre gere um
novo plano e use o novo token depois de uma interrupção ou limite de tempo.

## 4. Compactação física segura

A retenção transforma páginas antigas em espaço reutilizável, mas não reduz imediatamente
o arquivo. Gere primeiro o plano de uma cópia compactada:

```powershell
python pipeline\sqlite_history_retention.py `
  --compact-into memory\translation_engine.compacted.sqlite
```

Se houver espaço livre suficiente, o plano fornece um token `COMPACT-...`. Reexecute com
esse token para criar a cópia por `VACUUM INTO`. A ferramenta aplica `quick_check` e
`foreign_key_check`, mas nunca substitui automaticamente o banco original.
