![CK3 PT-BR localization pipeline](assets/cover.png)

# CK3 PT-BR Localization ML Pipeline

Pipeline local para analisar arquivos de localization do Crusader Kings III e gerar um mod que substitui o pacote espanhol por portugues brasileiro.

O projeto trabalha com arquivos `.yml`, banco SQLite, memoria de traducao e ciclos de aprendizado local. O foco atual e corrigir residuos de espanhol com alta confiabilidade, sem depender de API externa.

## Estrutura

```text
config/settings.json
pipeline/
assets/
memory/
reports/
logs/
source/
output/spanish/
```

`source/`, `output/`, `memory/`, `reports/` e `logs/` sao artefatos locais e nao devem ser versionados.

Pacotes esperados:

- `source/spanish_source`: espanhol original, espelho estrutural.
- `source/english_source`: ingles original, referencia semantica.
- `source/spanish_old`: melhor traducao atual usada como base.
- `output/spanish`: saida final do mod.

## Fluxos

### Fluxo Principal

Usado para indexar, analisar, construir memoria, sugerir e aplicar no output.

```powershell
python pipeline\main.py setup
python pipeline\main.py cycle
python pipeline\main.py apply
python pipeline\main.py full
```

- `setup`: cria/atualiza banco e indexa arquivos quando houver mudancas.
- `cycle`: roda analise, memoria, sugestoes e avaliacao.
- `apply`: reescreve `output/spanish` com sugestoes aprovadas.
- `full`: roda `cycle` e depois `apply`.

Primeiro preenchimento completo do output a partir de `source/spanish_old`:

```powershell
python pipeline\main.py apply --bootstrap-old
```

### Aprendizado Local Sem API

Usado para calibrar confianca em lotes pequenos, sem escrever `output/spanish`.

```powershell
python pipeline\main.py learn-local --learn-limit 20
```

Comecar por exemplos positivos do corpus central:

```powershell
python pipeline\main.py learn-local --learn-source positive --learn-focus core --learn-limit 20
```

Revisar pendencias problemáticas, como no fluxo anterior:

```powershell
python pipeline\main.py learn-local --learn-source pending --learn-focus all --learn-limit 20
```

Revise os candidatos no banco:

```sql
SELECT
  id,
  segment_id,
  english_text,
  old_text,
  suggested_text,
  local_confidence_score,
  local_status,
  human_label,
  reason
FROM local_learning_candidates
WHERE run_id = (SELECT MAX(id) FROM local_learning_runs)
ORDER BY id;
```

Classifique por categoria:

```sql
UPDATE local_learning_candidates
SET human_label = 'structure_error',
    reason = 'token colado ao texto',
    reviewer = 'emerson',
    reviewed_at = datetime('now'),
    updated_at = datetime('now')
WHERE id IN (...);
```

Consumir os rotulos e ajustar pesos:

```powershell
python pipeline\main.py learn-feedback
```

Esse passo tambem sincroniza confirmacoes por segmento:

- `human_confirmed`: revisao humana, fica bloqueada para mudancas automaticas.
- `auto_confirmed`: revisao automatica, conta como alta confianca, mas pode ser revisada depois.

Consultar a cobertura total:

```powershell
python pipeline\main.py confirmations
```

Gerar um relatorio conservador de confirmacoes automaticas possiveis:

```powershell
python pipeline\main.py auto-validate
```

Por padrao esse comando nao grava nada no banco. Para gravar `auto_confirmed`, use somente depois de revisar o relatorio:

```powershell
python pipeline\main.py auto-validate --auto-apply
```

Para nomes proprios e dinastias, use o trilho dedicado. Ele e mais rapido e mais seguro porque so olha `names/` e `dynasties/`, sem misturar textos humanos:

```powershell
python pipeline\main.py auto-validate-names --auto-limit 5000
```

Se o relatorio estiver limpo:

```powershell
python pipeline\main.py auto-validate-names --auto-limit 5000 --auto-apply
```

Regra inicial desse trilho: `english_text`, `spanish_text` e `old_text` precisam ser iguais apos normalizacao, sem tokens CK3 e com ate 4 palavras visiveis.

Depois rode outro lote:

```powershell
python pipeline\main.py learn-local --learn-source positive --learn-focus core --learn-limit 20
```

## Rotulos Locais

Escolha o rotulo pelo principal motivo de a sugestao nao poder ser aplicada como esta. Nao e necessario preencher `corrected_text` na maioria dos casos.

- `correct`: pronto como esta. Tokens, estrutura, sentido e portugues estao bons.
- `minor_fix`: traducao, tokens e estrutura estao certos; falta apenas limpeza superficial, como `¿`, `¡`, `«`, `»` ou espaco simples.
- `major_fix`: estrutura aproveitavel e boa parte traduzida, mas ainda precisa reescrita relevante.
- `residual_spanish`: muito espanhol residual ou texto quase todo em espanhol.
- `structure_error`: problema de token, comando CK3, literal dentro de comando, macro de genero, tag ou markup.
- `semantic_error`: portugues fluente, mas sentido errado.
- `wrong`: sugestao inutil, fora de contexto, igual ao texto antigo ruim ou quase igual.
- `harmful`: pioraria texto bom ou quebraria estrutura importante.

Regra de desempate:

- Token, comando CK3, literal, macro ou markup: `structure_error`.
- Espanhol residual dominante: `residual_spanish`.
- Limpeza superficial sem risco estrutural: `minor_fix`.
- Pronto para uso: `correct`.

Exemplos de `structure_error`:

```text
[house.GetBaseName]abandonou
[taster.Custom('ES_OA')]a
[Select_CString(...)]fala
[Select_CString( hosted_child.IsFemale, 'Esta cria', 'Este crio' )]
[Concept('decision', 'decisiones')|E]
token removido, token duplicado, #EMP/#! quebrado
```

## Corpus Prioritario

Para aumentar a confiabilidade do aprendizado, comece validando arquivos centrais do jogo. Eles contem termos que aparecem em menus, tooltips e referencias globais.

Prioridade sugerida:

1. Conceitos, glossario e interface principal.
2. Titulos, nomes, casas, culturas e religioes.
3. Menus recorrentes: cortes, situacoes, decisoes, conselheiros, personagens.
4. Traits, modifiers, buildings, laws e war/interaction.
5. Eventos narrativos longos.

Motivo: corrigir primeiro termos como `cortesanos`, `situaciones`, `decisiones`, `rechaza` nos arquivos centrais cria memoria/glossario mais confiavel para o restante da traducao.

Grupos disponiveis no `learn-local`:

```text
all
core
titles
world
ui
events
```

Use `--learn-source positive` para mapear exemplos bons e `--learn-source pending` para revisar sugestoes de correcao.

## Regras CK3 Importantes

Tokens e comandos normalmente ficam em ingles e devem ser preservados.

Exemplos com texto interno traduzivel:

```text
[Concept('head_of_faith', 'cabeza de tu fe')|E]
[Concept('decision', 'decisiones')|E]
[Select_CString( CHARACTER.IsLocalPlayer, 'tu', 'su' )]
[CHARACTER.LocalPlayerString( 'robaste', 'robo' )]
```

O identificador tecnico deve ser preservado, mas textos exibidos ao jogador podem precisar traducao.

Pontuacao espanhola deve ser removida no pt-BR:

```text
¿Pergunta? -> Pergunta?
¡Texto! -> Texto!
«Texto» -> Texto
```

Macros de genero como `Custom('ES_OA')` ja geram a letra necessaria:

```text
enjoad[taster.Custom('ES_OA')]a -> enjoad[taster.Custom('ES_OA')]
```

## Scripts Principais

- `pipeline/db.py`: banco e migracoes.
- `pipeline/index_source.py`: extrai segmentos dos pacotes.
- `pipeline/index_inline_fragments.py`: cataloga textos traduziveis dentro de comandos CK3.
- `pipeline/analyze_segments.py`: classifica qualidade dos segmentos.
- `pipeline/build_translation_memory.py`: monta memoria.
- `pipeline/suggest_translations.py`: gera sugestoes.
- `pipeline/local_quality_validator.py`: valida residuos, pontuacao, espacos e estrutura.
- `pipeline/local_learning_cycle.py`: cria fila local de aprendizado.
- `pipeline/apply_local_learning_feedback.py`: consome rotulos humanos e ajusta pesos.
- `pipeline/segment_confirmation_report.py`: mede cobertura humana/automatica confirmada.
- `pipeline/auto_validate_segments.py`: estima e opcionalmente grava confirmacoes automaticas.
- `pipeline/evaluate_suggestions.py`: gera metricas.
- `pipeline/apply_safe_output_updates.py`: reescreve `output/spanish`.
- `pipeline/main.py`: orquestra os fluxos.

## API

O fluxo com API ainda existe como apoio opcional:

```powershell
python pipeline\main.py full-api --api-limit 200 --api-min-confidence 0.95 --api-concurrency 4
```

No momento, o foco do projeto e evoluir o aprendizado local para reduzir a dependencia da API.
