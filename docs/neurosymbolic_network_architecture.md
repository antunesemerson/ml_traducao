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

