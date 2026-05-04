![CK3 PT-BR localization pipeline](assets/cover.png)

# CK3 PT-BR Localization ML Pipeline

Pipeline local para analisar arquivos de localization do Crusader Kings III, aprender com uma traducao beta em portugues brasileiro e gerar sugestoes seguras para reescrever `output/spanish` como mod que substitui o idioma espanhol.

## Objetivo

- Preservar `source` como entrada somente leitura.
- Manter `output/spanish` como espelho estrutural de `source/spanish_source`.
- Classificar segmentos por confiabilidade.
- Construir memoria de traducao com segmentos confiaveis.
- Gerar sugestoes, receber feedback humano e melhorar em ciclos.
- Aplicar no `output/spanish` somente sugestoes seguras/aprovadas.

## Estrutura

```text
config/settings.json
pipeline/
assets/
memory/
reports/
source/
output/spanish/
```

`source/` e `output/` ficam fora do Git por conterem arquivos extraidos/gerados do jogo. O banco SQLite e os relatorios tambem sao artefatos locais.

## Pipeline

Comando principal:

```powershell
python pipeline\main.py cycle
```

Modos:

```powershell
python pipeline\main.py setup
python pipeline\main.py cycle
python pipeline\main.py apply
python pipeline\main.py full
```

- `setup`: cria/atualiza banco e roda indexacao somente se os hashes dos arquivos mudaram.
- `cycle`: roda `setup`, analise, memoria, sugestoes e avaliacao.
- `apply`: aplica sugestoes seguras/aprovadas em `output/spanish`.
- `full`: roda `cycle` e depois `apply`.

Forcar reindexacao:

```powershell
python pipeline\main.py cycle --force-index
```

Aplicar tambem sugestoes `safe` ainda pendentes:

```powershell
python pipeline\main.py apply --apply-include-safe-pending
```

## Ciclo De Aprendizado

1. Rode:

```powershell
python pipeline\main.py cycle
```

2. Revise alguns registros em `suggestion_feedback`.

Decisoes aceitas:

```sql
UPDATE suggestion_feedback
SET decision = 'accepted'
WHERE id = ...;
```

Decisoes rejeitadas:

```sql
UPDATE suggestion_feedback
SET decision = 'rejected', reason = 'contexto errado'
WHERE id = ...;
```

Correcoes manuais:

```sql
UPDATE suggestion_feedback
SET decision = 'edited', corrected_text = 'Texto corrigido'
WHERE id = ...;
```

3. Rode novamente:

```powershell
python pipeline\main.py cycle
```

Registros `pending` sao reconstruidos automaticamente. Registros `accepted`, `rejected` e `edited` viram aprendizado.

4. Quando as sugestoes estiverem boas:

```powershell
python pipeline\main.py apply
```

Por padrao, `apply` usa apenas sugestoes aprovadas/editadas e cria backup em `memory/backups`.

## Scripts

- `pipeline/db.py`: schema e migracoes incrementais.
- `pipeline/index_source.py`: extrai e alinha segmentos dos pacotes.
- `pipeline/analyze_segments.py`: classifica confiabilidade.
- `pipeline/build_translation_memory.py`: monta memoria de traducao.
- `pipeline/suggest_translations.py`: gera sugestoes e fila de feedback.
- `pipeline/evaluate_suggestions.py`: mede precisao do feedback avaliado.
- `pipeline/apply_safe_output_updates.py`: reescreve `output/spanish`.
- `pipeline/main.py`: orquestra o fluxo.

## Sem API Externa

A primeira versao usa apenas processamento local e memoria de traducao. Uma camada com API/LLM pode ser adicionada futuramente para novos updates do jogo ou segmentos sem cobertura na memoria.
