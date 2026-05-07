![CK3 PT-BR localization pipeline](assets/cover.png)

# CK3 PT-BR Localization ML Pipeline

Pipeline local para analisar arquivos de localization do Crusader Kings III, aprender com uma traducao beta em portugues brasileiro e gerar sugestoes seguras para reescrever `output/spanish` como mod que substitui o idioma espanhol.

## Objetivo

- Preservar `source` como entrada somente leitura.
- Manter `output/spanish` como espelho estrutural de `source/spanish_source`.
- Classificar segmentos por confiabilidade.
- Construir memoria de traducao com segmentos confiaveis.
- Gerar sugestoes, receber feedback humano e melhorar em ciclos.
- Revisar sugestoes com API externa de forma auditavel, sem aplicar automaticamente por padrao.
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
python pipeline\main.py full-api
```

- `setup`: cria/atualiza banco e roda indexacao somente se os hashes dos arquivos mudaram.
- `cycle`: roda `setup`, analise, memoria, sugestoes e avaliacao.
- `apply`: aplica sugestoes seguras/aprovadas em `output/spanish`.
- `full`: roda `cycle` e depois `apply`.
- `full-api`: roda `cycle`, revisa ate 200 sugestoes com API, promove respostas com confianca minima configurada, roda novo `cycle` e depois `apply`.

Forcar reindexacao:

```powershell
python pipeline\main.py cycle --force-index
```

Aplicar tambem sugestoes `safe` ainda pendentes:

```powershell
python pipeline\main.py apply --apply-include-safe-pending
```

Primeira criacao do `output/spanish` traduzido a partir de `source/spanish_old`:

```powershell
python pipeline\main.py apply --bootstrap-old
```

Esse modo e para o primeiro preenchimento real do mod. Ele escreve `old_text` no `output/spanish` para todos os segmentos possiveis e usa feedback/sugestoes como sobreposicao quando existirem. Depois desse bootstrap, use `apply` sem `--bootstrap-old` para aplicar apenas mudancas incrementais.

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

Quando a sugestao estiver errada, mas o `old_text` ja estiver correto:

```sql
UPDATE suggestion_feedback
SET decision = 'accepted_old', reason = 'old_text esta certo'
WHERE id = ...;
```

`reason` e apenas informativo. O comportamento do sistema deve depender de `decision`.

3. Rode novamente:

```powershell
python pipeline\main.py cycle
```

Registros `pending` sao reconstruidos automaticamente e nao contam como aprendizado. Registros `accepted`, `edited` e `accepted_old` fecham o segmento como resolvido. Registros `rejected` descartam aquela sugestao, mas o segmento continua aberto para novas tentativas.

4. Quando as sugestoes estiverem boas:

```powershell
python pipeline\main.py apply
```

Por padrao, `apply` usa apenas sugestoes aprovadas/editadas e cria backup em `memory/backups`.

No primeiro ciclo do projeto, como `output/spanish` ainda e uma copia do espanhol original, rode:

```powershell
python pipeline\main.py apply --bootstrap-old
```

Nos ciclos seguintes, use o apply incremental:

```powershell
python pipeline\main.py apply
```

## Scripts

- `pipeline/db.py`: schema e migracoes incrementais.
- `pipeline/index_source.py`: extrai e alinha segmentos dos pacotes.
- `pipeline/index_inline_fragments.py`: extrai textos traduziveis dentro de comandos CK3.
- `pipeline/analyze_segments.py`: classifica confiabilidade.
- `pipeline/build_translation_memory.py`: monta memoria de traducao.
- `pipeline/suggest_translations.py`: gera sugestoes e fila de feedback.
- `pipeline/validate_suggestions_api.py`: cria pareceres de API em `api_reviews`.
- `pipeline/apply_api_reviews.py`: promove pareceres aprovados/seguros para `suggestion_feedback`.
- `pipeline/evaluate_suggestions.py`: mede precisao, aprendizado, fila e aplicacao no output.
- `pipeline/apply_safe_output_updates.py`: reescreve `output/spanish`.
- `pipeline/main.py`: orquestra o fluxo.

## Revisao Com API

A API funciona como parecer auditavel. Ela grava em `api_reviews` e nao altera `suggestion_feedback` por conta propria.

Configure a chave no ambiente:

```powershell
$env:OPENAI_API_KEY = "..."
```

Se existir um `.env` na raiz, `validate_suggestions_api.py` tambem carrega automaticamente:

```text
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5.4-mini
```

Instale dependencias:

```powershell
pip install -r requirements.txt
```

Gerar pareceres para sugestoes pendentes:

```powershell
python pipeline\validate_suggestions_api.py --limit 25
```

Politica para comandos CK3: a API deve preservar estrutura e chaves reservadas, mas pode traduzir texto exibido dentro de argumentos. Exemplo:

```text
[Concept('decision', 'decisiones')|E] -> [Concept('decision', 'decisões')|E]
```

Revisar no banco:

```sql
SELECT *
FROM api_reviews
WHERE status = 'pending_human'
ORDER BY confidence_score DESC;
```

Quando um parecer da API estiver bom, aprove:

```sql
UPDATE api_reviews
SET status = 'approved'
WHERE id = ...;
```

Promover pareceres aprovados para `suggestion_feedback`:

```powershell
python pipeline\apply_api_reviews.py --include-approved
```

Automacao conservadora, apenas para respostas com alta confianca e tokens preservados:

```powershell
python pipeline\apply_api_reviews.py --mark-auto-ready --min-confidence 0.97
```

Depois rode o ciclo normal para reconstruir memoria e metricas:

```powershell
python pipeline\main.py cycle
```

Fluxo completo com API e output:

```powershell
python pipeline\main.py full-api
```

Por padrao, esse modo valida ate 200 sugestoes por ciclo e promove automaticamente apenas pareceres com `confidence_score >= 0.97` e tokens validos. Para ajustar:

```powershell
python pipeline\main.py full-api --api-limit 200 --api-min-confidence 0.97 --api-concurrency 4
```

Para testar uma nova confianca de promocao sem reescrever `output/spanish`, use:

```powershell
python pipeline\main.py full-api --api-limit 200 --api-min-confidence 0.95 --api-concurrency 4 --skip-apply
```

Para reduzir tempo local, o fuzzy matching de memoria fica desativado por padrao em `config/settings.json`, porque os ciclos recentes mostraram melhor qualidade nas regras, memoria exata, feedback humano e API. Se quiser testar mais cobertura com mais custo:

```json
"suggestions": {
  "enable_fuzzy": true,
  "max_fuzzy_candidates": 150
}
```

## Fragmentos Inline CK3

Alguns comandos CK3 tem estrutura protegida, mas contem textos traduziveis dentro de aspas:

```text
[Concept('head_of_faith', 'cabeza de tu fe')|E]
[Select_CString( CHARACTER.IsLocalPlayer, 'tu', 'su' )]
[CHARACTER.LocalPlayerString( 'robaste', 'robo' )]
```

`index_inline_fragments.py` cataloga esses fragmentos em `inline_fragments`, separando chaves reservadas de textos traduziveis. A primeira versao e observacional; a proxima evolucao e gerar sugestoes e aplicar correcoes dentro desses comandos preservando a estrutura.

## Residuos Persistentes

Algumas palavras espanholas recorrentes podem ser tratadas por regra local antes de uma camada com API/LLM. A lista inicial fica em:

- `pipeline/analyze_segments.py`: `PERSISTENT_SPANISH_RESIDUES`
- `pipeline/suggest_translations.py`: `PERSISTENT_SPANISH_RESIDUES`

Exemplos atuais:

```text
cortesano -> cortesão
cortesanos -> cortesões
decisiones -> decisões
gobernantes -> governantes
invitados -> convidados
rechaza -> rejeita
situación -> situação
situaciones -> situações
```

Essas regras aumentam a prioridade de revisao e podem gerar sugestoes seguras quando os tokens do `spanish_source` continuam preservados.

Tambem ha regras conservadoras de formatacao para sugerir revisao quando texto aparece grudado apos tags ou tokens protegidos, e para remover aspas angulares espanholas `«»` da saida pt-BR.
