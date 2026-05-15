![CK3 PT-BR localization pipeline](assets/cover.png)

# CK3 PT-BR Localization Pipeline

Pipeline local para transformar a localização do Crusader Kings III em um mod PT-BR que substitui o idioma espanhol. O projeto trabalha com arquivos `.yml`, mantém a estrutura original do jogo e usa SQLite, memória de tradução, regras conservadoras e aprendizado local para revisar, corrigir, traduzir e reescrever `output/spanish`.

A meta atual é ter um fluxo 100% local: sem API externa, sem depender de serviço online e com aprendizado acumulado a partir dos segmentos confirmados.

## Estrutura

```text
source/spanish_source   pacote espanhol original e espelho estrutural
source/english_source   referência semântica em inglês
source/spanish_old      melhor versão PT-BR conhecida
output/spanish          saída final do mod

config/settings.json    caminhos e limites padrão
memory/                 banco SQLite e modelos locais
pipeline/               scripts da pipeline
reports/                relatórios de execução
logs/                   logs completos por execução
```

`source/`, `output/`, `memory/*.sqlite`, `reports/`, `logs/`, `.env` e modelos locais grandes não são versionados.

## Fluxo Oficial

```text
setup
  db -> index_source -> index_inline_fragments

cycle
  analyze -> build_translation_memory -> suggest_translations -> evaluate_suggestions

learning
  learned_validation_report -> learned_autofix -> learned_apply -> auto_validate

apply
  apply_safe_output_updates -> refresh index/analyze/memory/suggestions
```

Comandos principais:

```powershell
python pipeline\main.py setup
python pipeline\main.py cycle
python pipeline\main.py learned-report --auto-limit 50000
python pipeline\main.py learned-autofix --auto-limit 500 --auto-apply
python pipeline\main.py learned-apply --auto-limit 5000 --auto-min-score 0.95 --auto-apply
python pipeline\main.py auto-validate --auto-limit 50000 --auto-min-score 0.95 --auto-apply
python pipeline\main.py confirmations
python pipeline\main.py apply
```

Para primeira geração completa do output a partir de `source/spanish_old`:

```powershell
python pipeline\main.py apply --bootstrap-old
```

Para rodar ciclo e reescrita em uma etapa:

```powershell
python pipeline\main.py full
```

## Scripts Essenciais

```text
pipeline/db.py                         schema e migrações não destrutivas
pipeline/index_source.py               extração de source/output para SQLite
pipeline/index_inline_fragments.py     literais traduzíveis dentro de comandos CK3
pipeline/analyze_segments.py           classificação inicial de confiabilidade
pipeline/build_translation_memory.py   memória de tradução confiável
pipeline/suggest_translations.py       sugestões por memória/regras
pipeline/evaluate_suggestions.py       métricas da fila de sugestões
pipeline/local_quality_validator.py    validação estrutural e linguística local
pipeline/learned_validation_report.py  classificação global por risco/ação
pipeline/apply_learned_autofix.py      correções mecânicas conservadoras
pipeline/apply_learned_validation.py   promoção de candidatos seguros
pipeline/auto_validate_segments.py     auto confirmação conservadora
pipeline/apply_safe_output_updates.py  reescrita do output
pipeline/segment_confirmation_report.py cobertura de segmentos e pacotes
pipeline/main.py                       orquestração
```

## Ferramentas Auxiliares

```text
package-priority       ranking de pacotes por impacto/pendência
focus-queue            fila de pacotes de alto impacto
closure-queue          fila otimizada de fechamento
package-autofix        correções por pacote
composite-autofix      correções compostas em linhas do learned-report
curated-fixes          correções humanas curadas para resíduos conhecidos
human-assisted-offline promoção cautelosa de propostas locais revisadas
offline-proposals      propostas locais por memória/glossário/regras
offline-apply          promoção de propostas locais seguras
offline-review         fila humana para propostas locais incertas
inline-literals        correções de literais dentro de Concept/Select/etc.
visual-rules           resíduos visuais comuns observados no jogo
relationship-rules     correções específicas de relações/personagens
name-queue/name-apply  equivalências históricas de nomes
title-name-rules       regras conservadoras para nomes/modelos de títulos
title-queue/title-apply revisão de títulos
batch-audit            auditoria de pacotes auto-confirmados
mojibake-audit         caça a caracteres quebrados
```

Todos os scripts geram progresso no console e relatório em `reports/`. A execução via `pipeline/main.py` também grava log em `logs/`.

## Regras CK3

Tokens e comandos devem ser preservados:

```text
[character.GetShortUIName]
$SOME_KEY$
#EMP texto#!
@icon!
```

Texto visível dentro de comandos pode precisar tradução:

```text
[Concept('decision', 'decisiones')|E]
[Select_CString( CHARACTER.IsLocalPlayer, 'tu', 'su' )]
[CHARACTER.LocalPlayerString( 'robaste', 'robó' )]
```

Problemas recorrentes:

```text
¿Pergunta? -> Pergunta?
¡Texto! -> Texto!
«Texto» -> "Texto" ou Texto, conforme o inglês
[token]texto -> [token] texto
texto#EMP -> texto #EMP
enjoad[taster.Custom('ES_OA')]a -> enjoad[taster.Custom('ES_OA')]
```

## Versionamento

Versionar:

```text
pipeline/
config/
assets/
README.md
requirements.txt
.gitignore
```

Não versionar:

```text
source/
output/
memory/translation_engine.sqlite
memory/models/
reports/
logs/
.env
execute_sql.py
execute_sql.sql
```

## Direção de Aprendizado

O aprendizado local deve usar os segmentos confirmados como base positiva e os erros auditados como base negativa. O próximo passo natural é consolidar um classificador local com `scikit-learn` para prever:

```text
auto_safe
needs_autofix
needs_suggestion
needs_human
blocked_structure
```

A tradução continua vindo de memória, glossário e regras; o modelo aprende a decidir confiança, risco e melhor ação para cada segmento.
