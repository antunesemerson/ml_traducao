# Arquitetura do Fluxo de Producao

Este documento descreve o fluxo operacional para receber uma versao nova do CK3, reaplicar o conhecimento local e gerar um `output/spanish` publicavel como mod PT-BR.

## Objetivo

Transformar o pipeline atual em um ciclo de producao com inicio, meio e fim:

1. validar estado das fontes;
2. reconstruir ou verificar o espelho estrutural;
3. reaplicar memoria e confirmacoes confiaveis;
4. classificar riscos com regras, modelo geral, coordenador e especialistas;
5. aplicar somente o que estiver autorizado;
6. gerar relatorio de release;
7. devolver pendencias para a frente de aprendizado.

O fluxo de producao usa o conhecimento promovido, mas nao treina modelos por padrao.

## Principios de Seguranca

- `source/spanish_source` continua sendo o espelho estrutural autoritativo.
- `output/spanish` deve preservar arquivos, chaves, ordem e quantidade de linhas do pacote espanhol.
- Segmentos `locked` por humano nunca sao sobrescritos automaticamente.
- Alteracao de tokens CK3 so pode ser aplicada com politica aprovada.
- `segment-apply` deve rodar em dry-run antes de qualquer escrita.
- `auto_apply_allowed` do gate composto deve permanecer `0` enquanto a politica estiver guardada.
- Segmentos vazios, tecnicos ou identicos tambem precisam de estado final: aplicado, blank valido, blank intencional ou bloqueado.

## Estados de Release

- `source_ready`: fontes presentes e hashes conhecidos.
- `mirror_ready`: output existe como espelho estrutural.
- `indexed`: source, old, english e output foram indexados.
- `memory_applied`: memoria e confirmacoes confiaveis foram reaplicadas.
- `deterministic_validated`: tokens, placeholders, linhas, chaves e encoding foram auditados.
- `ml_scored`: modelo geral e rede de agentes avaliaram os segmentos.
- `gate_ready`: gate composto ativo esta sem bloqueios criticos.
- `output_applied`: confirmacoes autorizadas foram gravadas no output.
- `final_validated`: output final passou nas validacoes estruturais.
- `release_ready`: pacote pronto para teste em jogo ou publicacao com pendencias conhecidas.
- `learning_handoff`: pendencias e feedbacks foram enviados para a frente de aprendizado.

## Fluxo Operacional

### 1. Source Audit

Verifica se `source/spanish_source`, `source/english_source`, `source/spanish_old` e `output/spanish` existem e se houve alteracao desde o ultimo snapshot.

Saidas esperadas:

- lista de arquivos novos, alterados e removidos;
- hash do pacote;
- recomendacao: reindexar, restaurar espelho ou seguir.

### 2. Mirror Check

Compara `output/spanish` com `source/spanish_source`.

Validacoes:

- todos os arquivos esperados existem;
- nenhum arquivo estranho foi criado fora do espelho;
- quantidade de linhas por arquivo preservada;
- chaves na mesma ordem;
- segmentos sem output sao classificados como blank valido, blank intencional ou pendencia real.

### 3. Indexacao

Executa a indexacao completa quando fontes ou output mudam.

Comando base:

```powershell
python pipeline\main.py setup
```

Ou, quando o fluxo ja estiver orquestrado, usar os estagios internos `index`, `inline`, `analyze`, `memory`, `suggest` e `evaluate`.

### 4. Memoria e Confirmacoes

Reaplica conhecimento confiavel em camadas:

- `human_locked`;
- `human_confirmed`;
- confirmacoes automaticas com score atual seguro;
- memoria de traducao confiavel;
- excecoes contextuais documentadas.

Confirmacao antiga que conflita com o modelo atual nao e apagada. Ela vira reabertura ou fila de auditoria.

### 5. Validacao Deterministica

Hard gates antes do ML:

- tokens e placeholders;
- comandos CK3;
- variaveis `$...$`;
- `[Select_CString(...)]`, `[Custom(...)]` e escopos dinamicos;
- linhas vazias;
- aspas espanholas;
- encoding/mojibake;
- residuos de espanhol;
- genero e artigo dinamico.

Se a validacao deterministica bloquear, nenhum modelo pode liberar sozinho.

### 6. Rede de Agentes

Roda a avaliacao neuro-simbolica:

- travas deterministicas;
- modelo geral macro;
- coordenador;
- especialistas e subespecialistas;
- subpoliticas guardadas;
- checkpoint e shadow validation quando houver nova liberacao.

O resultado operacional nao e uma previsao crua. E uma decisao de gate: seguro, revisar, corrigir, bloquear, aplicar confirmacao ou mandar para aprendizado.

### 7. Aplicacao Controlada

Fluxo seguro para gravar no output:

```powershell
python dashboard\backend.py
# via POST /api/production/start, acionado pelo botao visual:
# 1. snapshot consistente antes de qualquer escrita
python pipeline\main.py cycle
python pipeline\main.py segment-state
python pipeline\main.py segment-apply --segment-include-auto-confirmed
python pipeline\main.py segment-apply --segment-include-auto-confirmed --segment-require-token-policy-decision
python pipeline\main.py segment-apply --segment-include-auto-confirmed --auto-apply
python pipeline\main.py segment-apply --segment-include-auto-confirmed --segment-require-token-policy-decision --auto-apply
python pipeline\main.py segment-apply --segment-review-states human_locked,human_confirmed --segment-include-intentional-blank --segment-allow-locked-token-override --auto-apply
python pipeline\main.py segment-state
python pipeline\main.py segment-token-policy --segment-include-auto-confirmed
python pipeline\main.py ml-composite-review-progress
```

Interpretacao:

- o executor web cria snapshot em `memory/production_snapshots/<run_id>` antes da escrita;
- `cycle` sincroniza indexacao/analise/memoria/sugestoes antes do primeiro `segment-state`;
- sem `--auto-apply`, nada e gravado;
- a primeira escrita aplica segmentos confirmados com tokens preservados;
- a segunda escrita aplica somente excecoes com politica de token aprovada;
- a terceira escrita reaplica excecoes humanas travadas, inclusive quando a correcao altera literais dentro de comandos CK3;
- `token_mismatch` sem politica aprovada fica bloqueado;
- `stale_token_policy_*` exige nova decisao ou revalidacao;
- backups sao criados automaticamente em `memory/backups`.

### 8. Validacao Final

Depois de aplicar output:

- reindexar se arquivos mudaram;
- gerar novo `segment-state`;
- validar estrutura do output;
- validar token policy;
- confirmar que pendencias restantes sao conhecidas.

### 9. Release Report

O relatorio de release deve responder:

- qual versao de fonte foi processada;
- quantos segmentos ativos existem;
- quantos estao consolidados;
- quantos foram aplicados neste ciclo;
- quantos ainda precisam aplicar output;
- quantos precisam revisao humana;
- quantos precisam autofix;
- quantos estao bloqueados por estrutura/token;
- quais pacotes concentram pendencias;
- quais agentes ou subpoliticas ajudaram;
- quais novos agentes/subagentes foram recomendados;
- se o mod esta pronto para teste em jogo.

### 10. Feedback Loop

Feedback de jogo ou comunidade volta para a frente de aprendizado como evidencia forte:

- bug confirmado;
- traducao melhorada;
- falso seguro;
- excecao manual;
- novo padrao linguistico;
- candidato a subagente.

## Web Local de Producao

A tela de producao futura deve ser um painel simples rodando localmente:

- estado das fontes;
- estado do output;
- botao `Start Production Run`;
- timeline visual do fluxo;
- progresso por etapa;
- logs resumidos;
- pendencias e bloqueios;
- link para dashboard analitico;
- link para dashboard gerencial;
- link para relatorio de release.

O backend executor deve usar uma allowlist de comandos, registrar cada execucao no banco e expor progresso por endpoint local. O frontend pode ser moderno e animado, mas a escrita real continua passando pelas mesmas travas do pipeline.

Execucao atual do botao:

- `GET /api/learning/status`: verifica semaforo de aprendizado;
- `POST /api/production/start`: inicia execucao real se `can_start_production = true`;
- `GET /api/production/runs/latest`: acompanha status, etapas, logs, relatorio e snapshot;
- escreve `output/spanish` somente na etapa `apply_token_policy_write`;
- gera log em `logs/<run_id>_production_run.log`;
- gera relatorio em `reports/<run_id>_production_run.txt`.

## Estado de Referencia em 2026-05-28

- Gate composto ativo: overlay 33.
- Guard profile `ES_OA -> ES_XA`: promovido em modo guardado.
- Auto-apply do gate composto: 0.
- Invalid releases: 0.
- Segmentos ativos: 288,100.
- Segmentos consolidados apos ultimo apply: 273,270 (94.85%).
- Pendencias operacionais: 14,830.
- Outputs confirmados ainda pendentes de aplicacao: 254.
- Outputs aplicados no ultimo ciclo seguro: 59.
- Pendencias de token restantes: bloqueadas ate nova decisao de politica.
