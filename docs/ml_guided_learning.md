# Aprendizado Guiado: ML Local para Localização de Games

Este documento explica, em linguagem de estudo, a camada de aprendizado local do projeto CK3 PT-BR.

A ideia é servir como material de consulta para entender o que estamos construindo, por que estamos fazendo dessa forma e como acompanhar a evolução estatística do sistema.

## 1. Objetivo Do Projeto

Estamos criando um sistema local/offline para traduzir, revisar e refinar a localização do Crusader Kings III para Português do Brasil.

O sistema trabalha com arquivos `.yml` de localização do CK3 e precisa preservar:

- estrutura dos arquivos;
- chaves;
- quantidade de linhas;
- tokens;
- placeholders;
- comandos internos do jogo;
- decisões manuais já confirmadas.

O objetivo de ML nesta fase não é "traduzir tudo sozinho". O objetivo inicial é classificar risco e qualidade.

As ações principais são:

```text
auto_safe
needs_human
needs_autofix
blocked_structure
```

Significado:

- `auto_safe`: o segmento parece seguro para ser mantido/promovido.
- `needs_human`: precisa de revisão humana.
- `needs_autofix`: há problema provável, mas corrigível por regra.
- `blocked_structure`: há risco estrutural, como token quebrado.

## 2. Por Que Começar Com Classificação

Tradução automática livre pode ser perigosa em localização de jogos.

O CK3 usa elementos como:

```text
[Character.GetName]
$game_concept_title$
#EMP texto#!
@icon!
\n
```

Se um modelo alterar esses elementos, o jogo pode exibir texto errado, quebrar tooltip ou perder referência dinâmica.

Por isso, começamos com uma tarefa mais segura:

```text
O modelo aprende a decidir risco, não a reescrever tudo.
```

Essa abordagem deixa o pipeline mais auditável:

```text
ML recomenda.
Regras bloqueiam.
Humano confirma.
```

## 3. Fontes E Saídas

Estrutura conceitual do projeto:

```text
source/spanish_source
  pacote original em espanhol, usado como espelho estrutural

source/english_source
  pacote original em inglês, usado como referência semântica

source/spanish_old
  melhor versão PT-BR conhecida até agora, usada como base histórica

output/spanish
  saída final do mod, preservando a estrutura do pacote espanhol
```

Regra importante:

```text
O output não deve ser alterado por experimentos de ML.
```

Treino, score, holdout, auditoria e filas humanas podem acontecer sem mexer nos arquivos finais do mod.

## 4. Banco SQLite

O SQLite é a base de conhecimento local:

```text
memory/translation_engine.sqlite
```

Ele armazena:

- segmentos fonte;
- output atual;
- sugestões;
- feedback humano;
- confirmações;
- memória de tradução;
- datasets supervisionados;
- runs de modelo;
- scores por segmento;
- políticas por grupo;
- histórico de promoção;
- relatórios de progresso.

O banco é local e pode ser reconstruído a partir das fontes, mas revisões humanas e modelos treinados devem ter backup externo quando forem importantes.

## 5. Dataset Supervisionado

O dataset é gerado por:

```powershell
python pipeline\main.py ml-dataset
```

Ele consolida exemplos positivos e negativos a partir de:

- confirmações humanas;
- revisões locais;
- filas de auditoria;
- correções manuais;
- exemplos rejeitados;
- falsos seguros encontrados em holdout;
- regressões entre modelos.

Labels humanos comuns:

```text
correct
contextual_exception
minor_fix
major_fix
semantic_error
structure_error
residual_spanish
rejected
```

Interpretação geral:

- `correct` e `contextual_exception` viram exemplos positivos.
- `semantic_error`, `structure_error`, `residual_spanish`, `rejected` viram exemplos negativos.
- `minor_fix` exige cuidado: o texto original pode ser fraco, mas o texto corrigido pode virar positivo.

## 6. Modelo Local

O treino atual usa modelos clássicos locais, salvos em `.joblib`.

Comando:

```powershell
python pipeline\main.py ml-train-risk
```

Ferramentas principais:

- `scikit-learn`;
- `TfidfVectorizer`;
- `SGDClassifier`;
- `joblib`.

### scikit-learn

Biblioteca Python para machine learning clássico.

Usamos para:

- transformar texto em números;
- treinar classificador;
- calcular métricas;
- montar pipeline de treino.

### TfidfVectorizer

Transforma texto em matriz numérica.

O modelo passa a enxergar padrões de caracteres e palavras, por exemplo:

```text
decisiones
preguiçosoa
[token]texto
#!texto
```

N-grams de caracteres ajudam muito em localização, porque erros pequenos de sufixo, acento, gênero ou token grudado podem ser relevantes.

### SGDClassifier

Classificador linear rápido e leve.

Ele aprende pesos para sinais do texto. Exemplo conceitual:

```text
residual_spanish -> aumenta risco
token quebrado -> aumenta blocked_structure
confirmação humana -> aumenta segurança
correção manual -> aumenta cautela
```

### joblib

Formato usado para salvar modelos do scikit-learn.

Modelos ficam em:

```text
memory/models/*.joblib
```

O `.joblib` guarda:

- vetorizador treinado;
- classificador treinado;
- pipeline;
- metadados necessários para reutilizar o modelo.

Não editamos `.joblib` manualmente. Para melhorar o modelo, geramos um novo treino.

## 7. Métricas Principais

A métrica de segurança mais importante é:

```text
false safe
```

Isso significa:

```text
O modelo disse "seguro", mas o exemplo era de risco.
```

Nesta fase, `false_safe = 0` vale mais que cobertura alta.

Outras métricas:

- `accuracy`: acerto geral.
- `macro_f1`: média equilibrada entre classes.
- `safe_precision`: entre os marcados como seguros, quantos eram realmente seguros.
- `safe_recall`: de todos os seguros reais, quantos o modelo conseguiu aceitar.
- cobertura operacional: quantos segmentos reais viram `auto_safe`.

Princípio atual:

```text
É melhor mandar um caso bom para revisão do que liberar um caso ruim como seguro.
```

## 8. Holdout

O holdout testa generalização por arquivo/pacote:

```powershell
python pipeline\main.py ml-holdout-eval
```

Ele separa arquivos inteiros para teste. Isso simula uma atualização ou DLC nova, onde o modelo encontra padrões que não viu diretamente.

Se o holdout aponta falso seguro, criamos fila de revisão:

```powershell
python pipeline\main.py ml-holdout-review-queue
```

Esses exemplos são muito valiosos porque mostram onde o modelo parecia confiante, mas estava errado.

## 9. Score Operacional

O score operacional roda o modelo sobre segmentos reais:

```powershell
python pipeline\main.py ml-score --ml-model-run-id ID_DO_MODELO
```

Ele mede o impacto real no corpus:

- quantos segmentos seriam `auto_safe`;
- quantos iriam para revisão humana;
- quantos seriam autofix;
- quantos seriam bloqueados por estrutura.

Runs incompletos têm:

```sql
finished_at IS NULL
```

Esses runs devem ser ignorados em dashboard, promoção e análise principal.

## 10. Promoção De Modelo

Promover modelo significa trocar o modelo ativo por um candidato.

Comando:

```powershell
python pipeline\main.py ml-promote-model --ml-active-score-run-id X --ml-candidate-score-run-id Y
```

A promoção é conservadora.

O candidato precisa:

- manter falso seguro zero;
- manter precisão segura;
- passar holdout;
- não perder cobertura operacional demais;
- não quebrar regras determinísticas.

Um modelo pode ser melhor em segurança, mas ainda não ser promovido se ficar tímido demais.

## 11. Política Por Grupo

Como o CK3 tem muitos tipos de texto, usamos política por grupo.

Exemplos de grupos:

```text
cultural_title_reviewed
culture_title_reviewed
religion_possessive_lowercase
religion_old_name_reviewed
title_directional_north
```

A política permite limiares diferentes por família, mas com travas.

Exemplo:

```text
religion_possessive_lowercase
  só pode virar seguro se houver learned_positive
```

Isso evita generalização apressada.

Comando:

```powershell
python pipeline\main.py ml-group-threshold-policy --ml-active-score-run-id ID_DO_SCORE
```

Para auditar novos seguros:

```powershell
python pipeline\main.py ml-policy-audit-queue --policy-audit-focus new_safe
```

## 12. Regressão Entre Modelos

Quando um modelo novo fica mais tímido que o modelo ativo, criamos uma fila de regressão:

```powershell
python pipeline\main.py ml-score-regression-queue --ml-active-score-run-id X --ml-candidate-score-run-id Y
```

Essa fila mostra segmentos que o modelo ativo considerava seguros, mas o candidato rebaixou.

Ela serve para duas coisas:

1. recuperar bons padrões que o modelo novo esqueceu;
2. descobrir falsos seguros antigos que o modelo ativo deixava passar.

Exemplos úteis:

- `Oporto -> Porto`;
- falta de espaço depois de `@icon!`;
- tooltip em primeira pessoa quando deveria falar com o jogador.

## 13. Modelos Especialistas

A próxima evolução é criar modelos especialistas.

Ideia:

```text
modelo geral
  classifica risco amplo

especialista_titles
  aprende títulos, topônimos, adjetivos e nomes culturais

especialista_religion
  aprende nomes religiosos, possessivos, adherents, old/name/adj

especialista_ui_short
  futuramente aprende labels curtos, botões e tooltips
```

Isso pode ajudar porque o CK3 não tem um único tipo de linguagem.

Títulos culturais, religião, UI curta e eventos longos têm padrões muito diferentes.

## 14. Auditor

O auditor é a camada que combina sinais.

No início, ele deve ser determinístico/estatístico, não um transformer.

Ele pode considerar:

- validação estrutural;
- modelo geral;
- especialista do grupo;
- memória humana;
- positivos aprendidos;
- negativos aprendidos;
- histórico de falso seguro;
- política por grupo.

Fluxo:

```text
segmento
  -> regras determinísticas
  -> modelo geral
  -> especialista aplicável
  -> memória humana
  -> auditor
  -> ação final
```

Regras do auditor:

- token quebrado sempre bloqueia;
- negativo humano recente exige revisão;
- especialista e modelo geral divergentes geram fila humana;
- confirmação manual não deve ser sobrescrita;
- novo auto-safe precisa de trilha auditável.

## 15. Dashboard De BI

O dashboard acompanha a evolução do sistema.

Telas úteis:

- Cockpit executivo;
- Performance;
- Pipeline;
- Governança;
- Lab;
- Especialistas.

Indicadores importantes:

- total de segmentos;
- cobertura com output;
- cobertura auto-safe;
- revisão pendente;
- false safe;
- evolução de modelos;
- comparação ativo vs candidato;
- política por grupo;
- filas humanas;
- especialistas;
- divergências entre modelos;
- auditoria.

O dashboard deve ignorar runs incompletos:

```sql
WHERE finished_at IS NOT NULL
```

## 16. Backup E Versionamento

Não versionamos no Git comum:

```text
memory/*.sqlite
memory/models/
source/
output/
reports/
logs/
prompts/
```

Motivos:

- arquivos grandes;
- mudam muito;
- podem ser derivados;
- deixam o histórico pesado.

Versionamos:

```text
pipeline/
docs/
config/
README.md
.gitignore
```

Estratégia recomendada:

1. Git para scripts e documentação.
2. Backup externo do SQLite após ciclos importantes.
3. Backup externo dos melhores `.joblib`.
4. Guardar métricas junto do modelo.
5. Promover apenas modelos que passaram pelas travas.

## 17. Prompts Temporários

Prompts usados para chats paralelos, dashboard ou instruções temporárias ficam em:

```text
prompts/
```

Essa pasta é ignorada no Git porque esses arquivos são criados, recriados e apagados conforme a necessidade.

Documentação estável fica em:

```text
docs/
```

## 18. Perguntas Para Estudo

1. Qual é a diferença entre regra determinística e modelo estatístico?
2. Por que `false_safe` é mais importante que `accuracy`?
3. O que o `TfidfVectorizer` faz?
4. Por que n-grams de caracteres ajudam em localização?
5. O que torna um exemplo negativo útil?
6. Por que o modelo não deve aplicar output sozinho?
7. Qual a diferença entre score interno, holdout e score operacional?
8. Por que um modelo seguro pode não ser promovido?
9. Como política por grupo reduz risco?
10. Quando modelos especialistas fazem sentido?
11. O que o auditor deve bloquear sempre?
12. Quando faria sentido testar um transformer local?

## 19. Próximos Passos Técnicos

Curto prazo:

1. testar feature sets e estratégias de treino (`language_v4`, `dedup_weighted_v2`);
2. medir holdout e score operacional;
3. comparar contra modelo ativo;
4. continuar alimentando negativos úteis;
5. amadurecer política por grupo.

Médio prazo:

1. criar datasets por especialista;
2. treinar `specialist_titles`;
3. treinar `specialist_religion`;
4. criar auditor dry-run;
5. gerar fila de divergências;
6. mostrar tudo no dashboard.

Longo prazo:

1. testar transformer local para sugestões de correção;
2. manter regras como camada de segurança;
3. usar especialistas e auditor para decidir quando confiar;
4. preparar fluxo para novas DLCs e atualizações do CK3.

