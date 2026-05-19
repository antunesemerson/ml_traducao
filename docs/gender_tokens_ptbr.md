# Tokens de gênero e adaptação PT-BR

Esta frente trata casos em que a tradução literal do espanhol preserva construções como `o/a`, `tu`, `su` ou sufixos `Custom('ES_OA')`, mas o resultado visual em português fica artificial, quebrado ou ambíguo.

## Princípio

Preservar a estrutura do jogo, mas preferir uma renderização natural em PT-BR quando o próprio motor de localização do CK3 permite isso.

Exemplos bons:

```text
[Select_CString( winner.IsFemale, 'Sua vizinha', 'Seu vizinho' )]
[Select_CString( CHARACTER.IsFemale, 'a Tímida', 'o Tímido' )]
```

Exemplos a evitar:

```text
vizinho[winner.Custom('ES_OA')]      -> vizinhoo / vizinha
o/a Tímid[CHARACTER.Custom('ES_OA')] -> o/a Tímido
conquistado[target.Custom('ES_OA')]  -> conquistadoo / conquistadoa
```

## Uso dos tokens

- `ES_OA`: normalmente devolve `o` ou `a`. Use com raiz sem vogal final: `conquistad[CHAR.Custom('ES_OA')]`.
- `ES_XA`: costuma completar nomes de agente como `Vingador/Vingadora`. Quando o contexto for fixo e visível, prefira `Select_CString`.
- `ES_ElLa`, `ES_DelDela`, `ES_AlAla`: artigos/preposições dinâmicas. Só devem ser mantidos quando combinam com o texto renderizado.
- `LocalPlayerString` e `Select_CString`: podem melhorar o português quando uma forma neutra do espanhol (`Tu`, `su`) não funciona em PT-BR.

## Regras de decisão

- Em apelidos de personagem, prefira `Select_CString` a `o/a`.
- Em características ou propriedades abstratas, prefira `sua` quando o substantivo oculto for feminino: `sua coragem`, `sua lealdade`.
- Em nomes de relação no `core`, testar formas com `Seu/Sua` antes de nomes como Filho, Pai, Mãe, Primo, Vassalo e Cortesão.
- Se o token gerar palavra duplicada ou colada, corrigir a raiz ou reescrever com `Select_CString`.
- Se o contexto for incerto ou o token referenciar outro personagem, manter para revisão humana.

## Fila de investigação

- Revisar todos os `nicknames_l_spanish.yml` com `o/a`.
- Auditar `ES_OA` quando a palavra antes do token já termina em `o` ou `a`.
- Retestar `RELATION_LIST` no `core_l_spanish.yml` para recuperar `Seu/Sua` sem quebrar a UI.
- Investigar convites de guerra em que o alvo/inimigo aparece como o próprio aliado.
