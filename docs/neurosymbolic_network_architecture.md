# Arquitetura Neuro-Simbolica

Este documento descreve a rede local de aprendizado do projeto CK3 PT-BR: uma combinacao de regras deterministicas, memoria de traducao, modelos ML, coordenador, especialistas e subespecialistas.

## Ideia Central

A arquitetura nao e uma rede neural unica e opaca. Ela e uma rede neuro-simbolica local, parecida com uma `mixture of experts`:

- regras simbolicas protegem estrutura e tokens;
- memoria de traducao preserva conhecimento aprovado;
- modelo geral observa o pacote inteiro;
- coordenador decide quando chamar especialistas;
- especialistas e subespecialistas votam em escopos menores;
- revisao humana vira evidencia forte;
- validadores impedem que previsao vire verdade sem prova.

Os nossos "neuroniozinhos" sao agentes pequenos e auditaveis. A borboleta nasce quando eles trabalham coordenados, nao quando todos viram um modelo gigante cedo demais.

## Camadas

### 1. Travas Deterministicas

Camada autoritativa.

Responsabilidades:

- preservar chaves, linhas e estrutura;
- detectar tokens removidos/adicionados;
- detectar placeholders quebrados;
- bloquear encoding/mojibake perigoso;
- proteger `human_locked`;
- classificar blanks validos e intencionais.

Se esta camada bloqueia, o ML nao libera.

### 2. Memoria Confiavel

Camada de conhecimento aprovado.

Fontes:

- confirmacoes humanas;
- locked humanos;
- output testado em jogo;
- feedback revisado;
- pares confiaveis de `spanish_source`, `english_source`, `spanish_old` e output.

Memoria antiga nao deve ser reaplicada cegamente quando source ou referencia inglesa mudam.

### 3. Modelo Geral Macro

Camada ampla.

Responsabilidades:

- classificar risco geral;
- detectar segmentos aparentemente seguros;
- levantar pendencias;
- servir de baseline para comparar especialistas.

Ele ajuda a cobrir o pacote inteiro, mas nao deve decidir sozinho excecoes estruturais.

### 4. Coordenador

Camada de roteamento e arbitragem.

Responsabilidades:

- escolher quais especialistas consultar;
- comparar modelo geral vs especialista;
- priorizar a decisao mais cautelosa em conflito;
- registrar divergencias;
- recomendar novo subagente quando uma familia recorrente nao esta bem explicada;
- produzir decisao operacional auditavel.

O coordenador e o "cerebro" da rede: ele nao substitui os neuroniozinhos, ele organiza quando cada um deve falar.

### 5. Especialistas

Camada por dominio.

Exemplos:

- `religion`;
- `culture_title_labels`;
- `title_adjectives`;
- `title_names`;
- `title_baronies`;
- `title_counties`;
- `title_cultural_names`.

Especialistas amplos demais podem virar fonte de ambiguidade. Quando isso acontece, quebramos em subespecialistas menores.

### 6. Subespecialistas

Camada estreita.

Exemplos atuais:

- `religion_bosnian_terms`;
- `religion_possessive_gods`;
- `religion_preserved_terms`;
- `religion_sufri`;
- subfamilias de titulos.

Subespecialistas nascem quando existe uma assinatura clara: tokens, caminho, chave, padrao linguistico ou familia de erro.

### 6.1. Microagentes por Habilidade

Camada transversal.

Esta e a proxima evolucao importante da rede. Nem todo problema deve ser fechado por um especialista de dominio inteiro. Um mesmo segmento pode ter varios problemas pequenos:

- token de genero usado em uma palavra;
- literal espanhol dentro de `Select_CString`;
- espaco ou pontuacao colada em token;
- texto curto de UI com estilo ruim;
- trecho longo que precisa preservar semantica;
- titulo, cultura ou religiao que exige criterio de contexto.

Nessa camada, cada neuroniozinho trata uma habilidade especifica e registra sua evidencia. O segmento so fecha quando o coordenador junta as evidencias e uma auditoria final valida a composicao.

Microagentes prioritarios:

- `short_label_style_microagent`: revisa labels curtas, tooltips compactos e textos de UI.
- `gender_token_microagent`: valida e sugere uso de `ES_OA`, `ES_AO`, `ES_ElLa`, `Select_CString` e parentes.
- `dynamic_ck3_expression_microagent`: entende expressoes dinamicas do CK3 e literals internos.
- `spanish_residual_microagent`: detecta e repara espanhol residual real, sem confundir nome de pacote com erro.
- `surface_boundary_microagent`: corrige espacos, pontuacao e fronteiras visiveis ao redor de tokens.
- `long_text_composer`: coordena reparos em textos longos onde varios microagentes podem atuar ao mesmo tempo.

### 6.2. Issue Ledger

Camada de memoria operacional por problema.

Em vez de salvar apenas `segmento -> decisao`, a rede deve passar a salvar `segmento -> problema -> evidencia -> reparo -> validacao`.

Cada item do ledger deve guardar:

- `segment_id`;
- familia do problema;
- trecho ou assinatura detectada;
- agente responsavel;
- proposta de reparo, quando houver;
- impacto em tokens;
- validacao local;
- decisao humana, se revisado;
- resultado do coordenador;
- status de promocao ou descarte.

Isso permite que varios microagentes trabalhem no mesmo segmento sem pisar uns nos outros.

Checkpoint inicial do ledger em 2026-06-03:

- 13.736 segmentos pendentes foram inspecionados;
- 25.422 issues foram materializadas;
- media de 1,85 issues por segmento pendente;
- 100% dos pendentes receberam pelo menos uma linha no ledger;
- `Needs output apply = 0`, portanto o backlog atual nao e fila de escrita: e fila de decisao, reparo, politica e validacao.

Principais familias detectadas:

- `short_label_style_microagent`: 10.442 issues;
- `dynamic_ck3_expression_microagent`: 5.137 issues;
- `gender_token_microagent`: 3.300 issues;
- `semantic_review_router`: 1.689 issues;
- `autofix_unknown_microagent`: 1.237 issues;
- `spanish_residual_microagent`: 834 issues;
- `title_policy_microagent`: 772 issues.

A consequencia pratica e que a rede nao deve exigir que um especialista feche o segmento inteiro sozinho. O fluxo correto passa a ser:

```text
segmento pendente
  -> ledger de issues
  -> microagentes por habilidade
  -> especialistas de dominio como contexto
  -> compositor/coordenador
  -> auditoria final
  -> fechamento ou fila humana
```

### 7. Politicas Guardadas

Camada de promocao segura.

Fluxo de uma subpolitica:

1. evidencia humana;
2. diagnostico de subtipo;
3. fila de validacao;
4. regra pequena e explicavel;
5. overlay guardado;
6. checkpoint;
7. shadow validation;
8. promocao como gate ativo;
9. apply continua desabilitado ate etapa propria.

Uma politica guardada pode reduzir risco operacional sem aplicar output automaticamente.

### 8. Aplicacao de Output

Camada separada da classificacao.

Mesmo quando o gate aprova uma mudanca, a escrita em `output/spanish` exige:

- confirmacao compativel;
- hash atual;
- validacao de tokens;
- dry-run limpo;
- backup;
- novo snapshot apos apply.

Classificar como seguro nao e a mesma coisa que gravar no output.

## Autoridade dos Agentes

- `authoritative`: trava deterministica. Vence todos.
- `operational`: participa do fluxo ativo.
- `dry_run`: calcula impacto sem decidir sozinho.
- `experimental`: aprende, audita e gera evidencia, mas nao promove release.
- `planned`: recomendado, ainda nao implementado.

## Promocao de Conhecimento

Uma descoberta evolui assim:

```text
erro observado
  -> fila de revisao
  -> decisao humana
  -> evidencia forte
  -> subtipo
  -> subpolitica ou subagente
  -> overlay guardado
  -> shadow validation
  -> gate ativo
  -> producao
```

## Confianca vs Seguranca

Confianca e a estimativa do modelo.

Seguranca e a decisao operacional depois de:

- travas deterministicas;
- politicas de token;
- revisoes humanas;
- false safe zero;
- contexto de output;
- protecao de locked human.

Um modelo pode ter confianca alta e ainda assim ser bloqueado. Isso e esperado.

## Estado de Referencia em 2026-05-28

- Agentes registrados: 19.
- Agentes operacionais: 14.
- Subagentes experimentais: 4.
- Falso seguro operacional: 0.
- Falso seguro experimental conhecido: 2 no especialista legado amplo de titulos.
- Gate composto ativo: overlay 33.
- Releases guardados ativos: 285.
- Guard profile novo `same_scope_es_oa_to_es_xa_article_candidate`: promovido.
- Aplicacao automatica pelo gate: desligada.

## Estado de Referencia em 2026-06-03

A producao ja zerou `needs_apply`, entao o gargalo principal deixou de ser escrita de output e passou a ser reabertura por qualidade.

Snapshot de referencia:

- Segment-state run: 139.
- Segmentos ativos: 288.100.
- Fechados/consolidados: 274.364 (95,23%).
- Pendentes operacionais: 13.736 (4,77%).
- `needs_apply`: 0.
- Quase toda a pendencia: `reopen_auto_confirmed_autofix`.

Diagnostico de arquitetura das pendencias:

- `short_label_style_microagent`: 5.720 primarios.
- `gender_token_microagent`: 3.297 primarios.
- `dynamic_ck3_expression_microagent`: 1.963 primarios.
- `autofix_unknown_microagent`: 1.232 primarios.
- `title_policy_microagent`: 750 primarios.
- `religion_semantic_microagent`: 322 primarios.
- `culture_semantic_microagent`: 223 primarios.
- `spanish_residual_microagent`: 181 primarios.

Conclusao: criar mais especialistas por arquivo ou dominio tera retorno limitado. A rede precisa amadurecer microagentes transversais e um compositor final.

Checkpoint de subclusterizacao em 2026-06-03:

- A fila `micro_short_label_style` provou que um microagente ainda pode ficar amplo demais.
- A subclusterizacao separou 120 itens em positivos limpos, textos longos/dinamicos, reparos de espanhol residual, contexto de dominio, token/genero e reparo de superficie.
- Apenas 32 itens ficaram como candidatos positivos limpos; 1 candidato aparentemente positivo foi removido por `space_before_punctuation`.
- Depois de ingerir os 32 positivos e treinar o macro 330, a cobertura em um recorte de 50.000 segmentos ficou igual ao modelo anterior.
- Conclusao: a evidencia positiva deve alimentar memoria e subpoliticas guardadas, mas a cobertura operacional precisa de coordenador/subpolitica, nao apenas de re-treino macro.

Primeira subpolitica de release positivo em 2026-06-03:

- `short_label_positive_release` converteu os 32 positivos limpos do `micro_short_label_style` em um release shadow.
- Resultado do run 1:
  - candidatos: 32;
  - liberados shadow: 32;
  - bloqueados: 0;
  - ganho estimado: 32 segmentos;
  - relatorio: `reports/20260603_191452_808576_issue_short_label_positive_release.txt`.
- Papel na rede:
  - o macro continua aprendendo com os exemplos, mas nao precisa ser promovido para este ganho estreito;
  - o coordenador pode consultar subpoliticas guardadas quando o issue ledger identifica um padrao conhecido;
  - a subpolitica nao corrige texto completo: ela valida uma decisao especifica do neuronio de short label e preserva os gates deterministas;
  - uma etapa futura de checkpoint/allowlist pode transformar shadow em fechamento de lifecycle, se continuar sem bloqueios.

Checkpoint guardado posterior:

- `short_label_positive_release_guarded_checkpoint_v1` revalidou o release shadow contra confirmacao e segment-state atuais.
- Resultado:
  - checkpoint run id: 1;
  - status: `ready_for_guarded_lifecycle_policy`;
  - promotion status: `guarded_candidate`;
  - allowlistados: 32/32;
  - bloqueados: 0;
  - `production_release_allowed=0`.
- Papel na rede:
  - cria uma camada clara entre "evidencia positiva" e "consumo por producao";
  - permite ao coordenador trabalhar com allowlists estreitas e auditaveis;
  - evita que o macro precise carregar sozinho toda a autoridade de fechamento;
  - preserva stale-check: mudanca futura no texto ou estado invalida a confianca antiga.

## Como Saber se a Estrategia Esta Funcionando

Sinais bons:

- false safe operacional permanece 0;
- cobertura consolidada aumenta;
- pendencias por subtipo diminuem;
- subagentes reduzem ambiguidades de especialistas amplos;
- filas humanas ficam menores e mais valiosas;
- output aplicado cresce sem aumentar bloqueios estruturais;
- feedback de jogo vira exemplos reaproveitaveis.

Sinais de ajuste:

- muitos falsos seguros no mesmo agente;
- especialista amplo discorda sem padrao claro;
- muitas pendencias em um subtipo sem assinatura;
- regras crescem demais;
- output apply fica bloqueado por hashes ou tokens sem fila de decisao.

## Proximas Evolucoes

- criar coordenador executor para producao;
- criar endpoint local de estado da rede;
- mostrar topologia interativa no dashboard;
- separar laboratorio de release;
- criar subagentes apenas quando houver impacto real;
- testar transformer local apenas depois que dados, rotas e guardrails estiverem maduros.
