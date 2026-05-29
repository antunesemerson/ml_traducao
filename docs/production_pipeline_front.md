# Frente de Producao do Mod CK3 PT-BR

Documento detalhado complementar: `docs/production_flow_architecture.md`.

Este documento define a frente separada para processar uma versao nova do jogo e gerar um output de mod publicavel.

## Objetivo

Receber fontes atualizadas do CK3, reaplicar o conhecimento local e produzir `output/spanish` completo, validado e pronto para teste/publicacao.

Esta frente e operacional. Ela deve usar o aprendizado ja promovido, mas nao deve treinar modelos novos como atividade principal.

## Entradas

- `source/spanish_source`: pacote espanhol original atualizado.
- `source/english_source`: pacote ingles original atualizado.
- `source/spanish_old`: melhor PT-BR conhecido, usado como memoria/apoio.
- `output/spanish`: espelho do pacote espanhol, reescrito com PT-BR.
- `memory/translation_engine.sqlite`: banco local com memoria, confirmacoes, modelos e historico.

## Saidas

- `output/spanish` com estrutura igual ao pacote espanhol.
- relatorio de processamento da versao.
- lista de segmentos finalizados.
- lista de pendencias nao resolvidas.
- lista de bloqueios estruturais.
- sugestoes de novos agentes/subagentes para a frente de Aprendizado.

## Invariantes de Producao

1. Estrutura, nomes de arquivos, chaves e quantidade de linhas seguem `source/spanish_source`.
2. Tokens CK3, placeholders, comandos e variaveis sao preservados ou alterados apenas por politica aprovada.
3. Segmentos humanos `locked` nao sao sobrescritos.
4. Segmentos vazios, tecnicos ou identicos devem receber estado final, nao ficar invisiveis.
5. Todo segmento ativo precisa terminar em um estado claro:
   - aplicado;
   - valido em branco;
   - intencionalmente vazio;
   - bloqueado;
   - pendente humano;
   - pendente autofix;
   - pendente novo agente.
6. Auto-apply de gate guardado permanece desligado ate promocao explicita.

## Fluxo de Producao

### 0. Preparacao

- registrar versao do jogo/DLC;
- salvar hash das fontes;
- criar snapshot do banco e output, se necessario;
- garantir que nenhuma revisao manual recente sera perdida.

### 1. Reindexacao

- detectar arquivos novos, alterados e removidos;
- reindexar `spanish_source`, `english_source`, `spanish_old` e `output`;
- marcar segmentos descontinuados;
- identificar novos segmentos sem memoria.

### 2. Espelho Estrutural

- restaurar ou verificar `output/spanish` como espelho estrutural de `source/spanish_source`;
- garantir que todos os arquivos esperados existem;
- garantir contagem de linhas por arquivo;
- garantir que nenhum arquivo fora do espelho foi criado indevidamente.

### 3. Reaplicacao de Memoria Confiavel

- reaplicar locked human;
- reaplicar confirmacoes humanas ainda compativeis;
- reaplicar memorias confiaveis por chave/texto/contexto;
- evitar propagar erro antigo se source/english mudou semanticamente.

### 4. Validacao Deterministica

- tokens e placeholders;
- variaveis CK3;
- Select_CString e Custom;
- genero e artigos dinamicos;
- residuos espanhois;
- mojibake;
- falta de espaco apos token;
- aspas espanholas;
- linhas vazias validas.

### 5. Classificacao ML e Roteamento

- rodar modelo macro;
- rodar coordenador;
- acionar especialistas/subespecialistas relevantes;
- classificar cada segmento em:
  - seguro;
  - aplicar confirmacao;
  - autofix;
  - revisao humana;
  - bloqueio estrutural;
  - precisa novo subagente.

### 6. Aplicacao Controlada

- aplicar apenas segmentos com autorizacao clara;
- preservar locked human;
- marcar blanks validos/intencionais como finalizados;
- marcar tokens/linhas identicas como finalizados quando estruturalmente corretos;
- nao deixar segmento correto sem estado final.

### 7. Validacao Final do Output

- comparar estrutura final contra `source/spanish_source`;
- validar chaves e linhas;
- validar tokens;
- rodar relatorios de risco;
- gerar pacote pronto para jogo;
- listar pendencias restantes.

### 8. Relatorio de Release

O relatorio final deve responder:

- quantos segmentos foram processados;
- quantos foram aplicados;
- quantos ficaram pendentes;
- quais pacotes concentram pendencia;
- quantos locked humanos foram preservados;
- quantos novos segmentos surgiram;
- quantos segmentos antigos desapareceram;
- quais agentes ajudaram;
- quais agentes falharam;
- quais novos agentes sao recomendados;
- se a versao esta pronta para teste em jogo.

### 9. Feedback Pos-Release

- coletar bugs em jogo;
- coletar feedback da comunidade;
- mapear cada feedback para segmentos;
- registrar como positivo, negativo, correcao ou nova regra;
- enviar para a frente de Aprendizado.

## Papel do Chat de Producao

O chat bifurcado de Producao deve:

- ler este documento antes de agir;
- verificar se fontes mudaram;
- decidir se precisa reindexar;
- executar o fluxo operacional;
- aplicar output apenas quando seguro;
- produzir resumo gerencial;
- entregar pendencias para a frente de Aprendizado.

Ele nao deve:

- treinar modelos experimentais sem pedido explicito;
- promover modelo sem auditoria;
- sobrescrever locked human;
- apagar memoria;
- alterar escopo de agentes sem registrar recomendacao.

## Definicao de Pronto Para Publicacao

Uma versao do mod pode ir para teste/publicacao quando:

- estrutura do output e valida;
- todos os segmentos ativos tem estado final;
- pendencias remanescentes sao conhecidas e aceitaveis;
- bloqueios criticos sao zero;
- locked human preservados;
- relatorio de release foi gerado;
- pacote abre no jogo sem erro estrutural conhecido.

## Estado Operacional Atual

Referencia em 2026-05-28:

- Gate composto ativo: overlay 33.
- Auto-apply do gate composto: desligado.
- Guard profile `ES_OA -> ES_XA`: promovido em modo guardado.
- Segmentos ativos: 288,100.
- Segmentos consolidados: 273,270.
- Pendencias operacionais: 14,830.
- Outputs aplicados no ultimo ciclo seguro: 59.
- Outputs confirmados ainda pendentes: 254.
- Pendencias restantes de apply exigem politica de token ou revalidacao de hash.

## Relacao com a Frente de Aprendizado

Producao gera evidencias reais.

Aprendizado transforma essas evidencias em:

- memoria;
- regras;
- negativos;
- modelos melhores;
- novos subagentes.

Depois, Producao usa a versao promovida desse conhecimento na proxima atualizacao do jogo.
