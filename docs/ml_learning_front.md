# Frente de Aprendizado ML e Rede de Agentes

Este documento define a frente dedicada a coletar feedback, treinar, auditar e evoluir a rede local de agentes do projeto CK3 PT-BR.

## Objetivo

Evoluir o sistema local/offline de traducao e revisao para reduzir trabalho manual sem perder seguranca estrutural.

Esta frente cuida do laboratorio:

- revisar evidencias humanas;
- identificar falsos seguros;
- criar regras, especialistas e subespecialistas;
- treinar e comparar modelos;
- promover apenas o que passar por travas;
- gerar conhecimento reutilizavel para futuras versoes do jogo.

Ela nao deve ser responsavel por publicar uma versao do mod. Publicacao e processamento de pacote completo ficam na frente de Producao.

## Ideia Central

A arquitetura desejada e um sistema neuro-simbolico local:

- regras deterministicas fazem o papel de guarda;
- modelo geral avalia o pacote inteiro;
- coordenador decide quando usar especialistas;
- especialistas e subespecialistas votam dentro do seu escopo;
- memoria de traducao e confirmacoes humanas fornecem exemplos confiaveis;
- validadores estruturais impedem propagacao de erro.

Esta arquitetura e parecida com uma `mixture of experts`: varios modelos pequenos e regras especializadas trabalham sob um coordenador. Ela nao deve virar uma rede neural unica e opaca cedo demais. Primeiro amadurecemos dados, rotas, guardrails e metricas; depois podemos testar transformer local.

## Principios

1. Travas deterministicas sempre vencem o ML.
2. Segmento `locked` por humano nao e sobrescrito.
3. Modelo experimental nao autoriza output.
4. Autoalimentacao nunca transforma previsao em verdade sem validacao.
5. Falso seguro operacional precisa ser zero para promocao.
6. Falso seguro experimental e sinal de calibracao, nao falha de producao.
7. Regras grandes demais devem ser quebradas em subespecialistas.
8. Todo ganho deve aparecer em relatorio e dashboard.

## Fluxo de Aprendizado

Antes de iniciar um ciclo que possa mudar modelo, politica, gate ou criterio de promocao, registrar o semaforo:

```powershell
python pipeline\learning_status.py start --objective "Descricao curta do ciclo" --phase context
```

Ao mudar de fase:

```powershell
python pipeline\learning_status.py phase --phase audit --phase-status running --summary "Rodando auditoria"
```

Ao concluir e liberar a frente de Producao:

```powershell
python pipeline\learning_status.py release --report reports/relatorio.txt
```

Enquanto `memory/training_lock.json` existir, a tela de Producao deve bloquear execucao geral. O detalhe completo fica em `memory/learning_status.json` e no endpoint `GET /api/learning/status`.

1. Rodar diagnostico de cobertura e gargalos.
2. Gerar fila pequena de evidencia para um grupo/subtipo.
3. Revisar casos como:
   - `accept_policy_candidate`;
   - `keep_manual_exception_only`;
   - `needs_subpolicy`;
   - `reject_policy_candidate`;
   - `encoding_cleanup_required`;
   - `fix_confirmed_text`.
4. Ingerir decisoes no banco.
5. Recalcular progresso e diagnostico.
6. Auditar promocao por regra estreita.
7. Se uma regra tiver evidencia positiva limpa, criar guard profile.
8. Gerar overlay guardado com `apply_allowed = 0`.
9. Rodar checkpoint e shadow validation.
10. Promover somente se:
    - blockers = 0;
    - invalid releases = 0;
    - hygiene warnings = 0 ou resolvidos;
    - auto-apply continua 0.

## Autoalimentacao Controlada

O sistema pode aprender com suas proprias analises, mas em niveis diferentes:

- Evidencia fraca: previsoes do modelo, filas, scores e divergencias.
- Evidencia media: consenso entre modelo geral, especialista e regras.
- Evidencia forte: revisao humana, output testado em jogo, feedback de comunidade.

Somente evidencia forte deve virar confirmacao final ou treino supervisionado confiavel. Evidencia fraca pode gerar fila, nunca verdade final.

## Estado Atual Resumido

Em 2026-05-28, apos a promocao guardada do perfil `ES_OA -> ES_XA`:

- Gate composto ativo: overlay 33.
- Policy run ativa: 34.
- Itens ativos no gate composto: 877.
- Revisados: 845.
- Pendentes: 32.
- Cobertura de revisao do gate composto: 96.35%.
- Aprovados para aplicar: 640.
- Releases guardados ativos: 285.
- Guard profile `same_scope_es_oa_to_es_xa_article_candidate`: promovido como gate guardado.
- Agentes registrados: 19.
- Agentes operacionais: 14.
- Subagentes experimentais de religiao: 4.
- Falso seguro operacional: 0.
- Falso seguro experimental conhecido: 2, no legado `specialist_titles`.

## Estado dos Agentes

### Autoritativos

- `deterministic_guards`: hard gate estrutural.

### Operacionais

- `general_macro`
- `coordinator_ensemble_v1`
- `religion`
- subespecialistas de titulos:
  - `culture_title_labels`
  - `title_adjectives`
  - `title_baronies`
  - `title_counties`
  - `title_cultural_names`
  - `title_duchies`
  - `title_empires`
  - `title_kingdoms`
  - `title_names`
  - `title_prefixes`

### Experimentais

- `titles`, especialista legado amplo com 2 falsos seguros.
- `religion_bosnian_terms`
- `religion_possessive_gods`
- `religion_preserved_terms`
- `religion_sufri`

## Como Tratar Falsos Seguros

Falso seguro operacional:

- bloqueia promocao;
- exige fila de regressao;
- deve virar negativo forte;
- pode exigir lowering threshold ou subagente.

Falso seguro experimental:

- nao bloqueia producao;
- deve aparecer no dashboard como laboratorio;
- deve gerar fila de calibracao;
- pode indicar que o agente e amplo demais.

Caso atual:

- `specialist_titles` tem 2 falsos seguros.
- Como ele e legado experimental, nao deve comandar decisao.
- O caminho correto e continuar usando subagentes de titulo menores, que estao mais seguros.

## Pendencias Atuais Mais Importantes

Pelo diagnostico atual, os gargalos do gate composto estao concentrados em:

- `gender_article_custom_rewrite`: 7 pendentes;
- `name_form_rewrite`: 7 pendentes;
- `pronoun_removed_or_literalized`: 6 pendentes;
- `glossary_label_translation`: 5 pendentes;
- `dynamic_gender_pronoun_rewrite`: 5 pendentes;
- `dynamic_relation_custom_rewrite`: 1 pendente.

Tambem existem grupos revisados que precisam desenho de subpolitica:

- `pronoun_perspective_rewrite`: 26/26 revisados, pronto para desenhar subespecialistas;
- `gender_custom_form_swap`: split em subfamilias; uma regra positiva esta pronta, mas ainda sem guard profile.

## Quando Criar Novo Neuronio

Criar subagente/subpolitica quando houver:

- pelo menos 5 exemplos positivos ou negativos semelhantes;
- uma assinatura estrutural clara;
- limite claro do que nao pode ser generalizado;
- impacto recorrente no pacote;
- regra pequena o bastante para ser auditavel.

Evitar criar subagente quando:

- ha apenas 1 ou 2 exemplos;
- a regra depende de semantica profunda nao capturada;
- a familia mistura muitos escopos/tokens;
- o ganho de cobertura e pequeno demais.

## Entregaveis Desta Frente

- novos datasets supervisionados;
- revisoes humanas e negativas fortes;
- modelos `.joblib` treinados localmente;
- politicas por grupo;
- auditorias de falso seguro;
- registros de agentes;
- filas de revisao;
- relatorios para dashboard;
- recomendacoes para a frente de Producao.

## Foco Imediato

1. Reduzir pendencias restantes do gate composto por impacto:
   - `mixed_token_change_review`;
   - `dynamic_scope_token_review`;
   - `gender_token_subspecialist_review`.
2. Investigar os 254 outputs confirmados ainda bloqueados por token policy/stale hash.
3. Revisar pendencias restantes por impacto.
4. Diagnosticar se mais ganhos virao de:
   - token composite subpolicies;
   - output apply;
   - modelos especialistas;
   - ou nova ferramenta local de traducao.
