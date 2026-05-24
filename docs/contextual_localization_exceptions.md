# Excecoes Contextuais de Localizacao

Este documento registra uma classe especial de decisoes do projeto: traducoes que fogem do espelho literal espanhol/output por razoes de fluidez, estetica ou limitacoes contextuais do CK3.

Esses casos nao devem ser tratados automaticamente como erro pelo ML.

## Principio

Nem toda diferenca estrutural e uma falha.

Algumas diferencas sao:

- adaptacoes manuais intencionais;
- solucoes para limitacoes do motor de localizacao;
- ajustes de genero em PT-BR;
- remocao de texto que ficaria estranho no jogo;
- reescritas para soar natural em contexto.

O objetivo do ML nao e forcar o output a ser um espelho literal. O objetivo e aprender quando uma divergencia e:

```text
erro
risco
adaptacao aceitavel
adaptacao excelente
excecao manual protegida
```

## Exemplo: Relacoes Familiares

Em certos textos de relacao familiar, o jogo exibia formas como:

```text
Tu Pai
Tu Mae
```

O ideal em PT-BR seria:

```text
Seu Pai
Sua Mae
```

Mas, quando nao ha um caminho seguro para rastrear dinamicamente o genero do familiar e alternar `Seu/Sua`, a solucao manual pode ser deixar o possessivo vazio:

```text
Pai
Mae
```

Essa decisao melhora a exibicao no jogo, mesmo fugindo da estrutura literal.

Classificacao recomendada:

```text
exception_category: contextual_omission
reason: possessive_gender_unavailable
status: protected_manual_exception
```

## Exemplo: Titulos E Genero Dinamico

Alguns titulos de governantes podem precisar de tratamento dinamico de genero:

```text
[Select_CString( CHARACTER.IsFemale, 'Rainha', 'Rei' )]
[Select_CString( CHARACTER.IsFemale, 'Duquesa', 'Duque' )]
```

Esses casos podem alterar a forma do texto em relacao ao espanhol, mas preservam ou melhoram o comportamento em jogo.

Classificacao recomendada:

```text
exception_category: gender_dynamic_rewrite
reason: ptbr_gender_agreement
status: protected_manual_exception
```

## Como O ML Deve Tratar

O ML deve aprender que essas excecoes sao exemplos positivos especiais, nao erros.

Quando um segmento tiver confirmacao humana travada e uma fonte/label indicando excecao contextual, ele deve ser usado como:

```text
label: positive
action_label: protected_contextual_exception
trust_level: 5
```

Mas o modelo nao deve aplicar automaticamente novas excecoes desse tipo sem regra deterministica ou revisao humana.

Ou seja:

```text
Pode reconhecer.
Pode priorizar.
Pode sugerir revisao.
Nao deve inventar sozinho.
```

## Politica De Aplicacao

Uma excecao contextual so deve ser considerada segura quando:

- foi confirmada manualmente;
- esta travada ou marcada como protegida;
- preserva tokens obrigatorios;
- foi testada no jogo ou deriva de padrao ja testado;
- tem justificativa registrada.

Caso contrario, o ML deve classificar como:

```text
needs_human
```

## Categorias Sugeridas

```text
contextual_omission
gender_dynamic_rewrite
ui_fluency_rewrite
engine_limitation_workaround
proper_name_preservation
title_gender_agreement
relationship_label_simplification
```

## Onde Registrar

Hoje, essas decisoes podem ser registradas em:

```text
segment_confirmations.confirmation_source
segment_confirmations.confirmation_label
local_learning_candidates.human_label
local_learning_candidates.reason
```

Padroes recomendados:

```text
confirmation_source = manual_context_exception
confirmation_label = contextual_omission
confirmation_label = gender_dynamic_rewrite
confirmation_label = relationship_label_simplification
confirmation_label = title_gender_agreement
```

No futuro, podemos criar uma tabela dedicada:

```text
contextual_exceptions
- segment_id
- exception_category
- rationale
- source_key
- relative_path
- before_text
- final_text
- tested_in_game
- reviewer
- status
```

## Aprendizado Incremental

Esses casos devem entrar no dataset como exemplos positivos de alta confianca, mas separados dos positivos comuns.

Isso permite medir:

- quantas excecoes existem;
- quais categorias sao mais frequentes;
- se o modelo esta confundindo excecao com erro;
- se novas sugestoes parecem semelhantes a excecoes antigas.

Meta futura:

```text
O ML reconhece padroes de excecao e sugere revisao contextual,
mas so aplica quando uma regra segura ou um humano confirma.
```

