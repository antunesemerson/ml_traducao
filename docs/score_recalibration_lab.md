# Laboratório de Recalibração e Ciclo Vivo de Qualidade

## Objetivo

Criar uma superfície própria para recalibrar a confiança dos segmentos sem sobrescrever o score bruto, comparar o candidato com a versão vigente e promover uma nova versão de score apenas quando ela representar melhor a qualidade observada.

O laboratório deve responder quatro perguntas:

1. Existe evidência humana suficiente e comparável para recalibrar?
2. O candidato melhorou a fidelidade do score sem criar falso seguro?
3. Quais segmentos melhoraram, pioraram ou mudaram de faixa?
4. O candidato está apto a se tornar a versão vigente do score calibrado?

Calibração altera a interpretação da confiança. Ela não altera tradução, output, apply, confirmação humana ou lifecycle.

## Posição operacional no sistema

Adicionar `Recalibrar score` como módulo executável do `Production Control`, junto de `Diagnóstico`, `Avaliação`, `Publicável`, `Hotfix` e `Nova versão`.

A recalibração não deve ficar em `Inteligência do Projeto`, porque possui autoridade para:

- consumir decisões humanas;
- congelar insumos e holdout;
- gerar um candidato de score;
- materializar scores candidatos para todo o pacote;
- executar gates;
- promover ou reverter a versão vigente do score calibrado.

Organização de responsabilidades:

- `Production Control > Recalibrar score`: preparação, auditoria, geração, comparação e promoção do candidato;
- `Feedback e pós-release > Revisar`: avaliação humana que alimenta a Auditoria de score;
- `Feedback e pós-release > Ciclo vivo`: Descobertas, providers, propostas e filas especializadas de correção;
- `Inteligência do Projeto > Métricas`: observabilidade read-only do score bruto, vigente e histórico de calibradores;
- `Visão Geral`: exibe somente o score vigente e identifica claramente sua versão/fonte.

`Métricas` pode oferecer um link para abrir o módulo operacional, mas não pode gerar, promover, rejeitar ou reverter candidatos.

## Fluxo dentro do Production Control

Ao selecionar `Recalibrar score`, o painel operacional deve trocar para um workspace próprio com quatro etapas executáveis:

1. `Preparar insumo`: valida snapshot, decisões, hashes, classes e holdout.
2. `Gerar candidato`: ajusta o calibrador e materializa o overlay candidato sem autoridade operacional.
3. `Validar discrepâncias`: abre maiores altas, quedas e mudanças de faixa na revisão focada.
4. `Promover score`: executa gates e atualiza o registry, mantendo rollback.

Gates de entrada do módulo:

- nenhuma produção concorrente em execução;
- avaliação e diagnóstico concluídos para o mesmo snapshot;
- score base e segment-state fixados;
- decisões da Auditoria consumidas;
- holdout válido e sem vazamento;
- pacote sem mudança posterior ao snapshot.

O workspace de avaliação de segmentos é parte do motor operacional: as decisões tomadas ali classificam o que está realmente bom ou ruim e permitem distinguir defeito de tradução de erro de confiança do score.

Embora operacional, `Recalibrar score` não precisa executar em todo ciclo nem bloquear automaticamente a publicação do pacote. Ele é habilitado quando há evidência nova suficiente. Um candidato em shadow não altera os gates de publicação; somente uma regressão de segurança confirmada ou uma versão de score promovida pode influenciar execuções futuras.

## Estrutura da tela

### 1. Preparação

Mostra se há insumo suficiente para gerar um candidato.

Cards principais:

- `Base vigente`: model run, score run, pacote, segment-state, versão do calibrador e hashes.
- `Evidência utilizável`: decisões totais, exatas, positivas, negativas e cobertura do pacote.
- `Evidência excluída`: hash antigo, score ausente, rótulo incompatível, duplicata e conflito.
- `Holdout`: quantidade reservada, classes, recortes cobertos e data de congelamento.
- `Prontidão`: apto, atenção ou bloqueado, sempre acompanhado dos motivos.

Recortes obrigatórios:

- complexidade;
- comprimento;
- densidade de tokens;
- origem/família;
- texto predominantemente dinâmico;
- presença de locks e estruturas protegidas;
- classes positiva e negativa.

O botão `Gerar candidato` somente fica ativo quando a base, os hashes, o holdout e a cobertura mínima forem válidos. Gerar candidato nunca promove nem altera o score vigente.

### 2. Comparativo

Compara `score bruto`, `score calibrado vigente` e `candidato`.

KPIs:

- ECE;
- Brier;
- falso seguro;
- safe precision;
- safe recall/cobertura segura;
- confiança média versus qualidade observada;
- cobertura da calibração;
- quantidade de segmentos que mudaram de faixa;
- índice global bruto, vigente e candidato.

Visualizações:

- curva de confiabilidade com as três versões;
- distribuição dos deltas de score;
- matriz de migração entre faixas;
- comparação por complexidade, família e origem;
- evolução por versão de calibrador;
- cobertura da evidência por recorte.

As métricas devem sempre informar denominador, amostra, versão e data de corte.

### 3. Discrepâncias

Lista auditável dos segmentos que mais explicam a mudança.

Visões rápidas:

- `Maiores melhorias`;
- `Maiores quedas`;
- `Cruzaram faixa segura`;
- `Divergem da decisão humana`;
- `Longos e dinâmicos`;
- `Baixo suporte no recorte`;
- `Regressões críticas`.

Cada linha mostra:

- segmento e arquivo/chave;
- score bruto, vigente e candidato;
- delta absoluto;
- faixa anterior e nova;
- decisão humana mais recente;
- fonte e quantidade de evidências;
- recorte de calibração;
- risco, locks e padrões relevantes.

Ao clicar, abre a revisão focada com os textos de referência, histórico de decisões e justificativa do calibrador. A decisão retorna à fila de auditoria e só participa da próxima rodada; não deve alterar o candidato já congelado.

A classificação da variação é estruturada e dispensa comentário livre:

- `coherent_change`: direção e magnitude coerentes;
- `correct_direction_excessive`: direção correta, magnitude excessiva;
- `correct_direction_insufficient`: direção correta, magnitude insuficiente;
- `incorrect_direction`: movimento na direção errada;
- `needs_review`: evidência insuficiente para decidir agora.

As três discordâncias estruturadas bloqueiam a promoção do candidato atual, mas
permanecem separadas para que o ciclo seguinte possa corrigir direção e magnitude
como problemas distintos.

### 4. Decisão e materialização

O candidato deve ser imutável depois de congelado. A tela exibe todos os gates e o motivo de cada aprovação ou bloqueio.

Gates obrigatórios:

- base e hashes ainda correspondem ao pacote vigente;
- holdout não foi usado no ajuste;
- ECE e Brier melhoraram no holdout;
- falso seguro permaneceu em zero;
- não há regressão crítica sem revisão;
- cobertura mínima geral e por recorte prioritário;
- amostra mínima de maiores altas e maiores quedas revisada;
- nenhum conflito de decisão humana pendente;
- candidato não está obsoleto após nova avaliação/diagnóstico.

Ações:

- `Rejeitar candidato`: preserva histórico e motivo.
- `Manter em shadow`: continua comparável, sem efeito operacional.
- `Promover score calibrado`: cria uma nova versão vigente do overlay calibrado.
- `Reverter`: volta o registry para a versão anterior sem recalcular o score bruto.

A promoção nunca deve apagar ou sobrescrever o score bruto. Ela atualiza apenas o registry da camada calibrada.

## Modelo de dados recomendado

Reutilizar as execuções supervisionadas e shadow existentes e acrescentar uma camada materializada versionada:

- `ml_score_calibration_candidate_runs`
  - base model run, base score run, pacote/segment-state, calibrador, holdout, status e hashes;
- `ml_score_calibration_candidate_items`
  - segment id, text hash, score bruto, score vigente, score candidato, delta, faixas e recortes;
- `ml_score_calibration_candidate_gates`
  - gate, valor, meta, status e motivo;
- `ml_score_calibration_discrepancy_reviews`
  - revisão das altas, quedas e mudanças de faixa;
- `ml_score_calibration_registry`
  - versão vigente, versão anterior, promoção, reversão e autoridade;
- `ml_supervised_label_ledger` ou view equivalente
  - contrato canônico que deduplica decisões por segmento, hash, alvo e origem.

Textos não devem ser duplicados nessas tabelas. Os itens guardam identificadores, hashes, métricas e referências para as fontes atuais.

## Duas filas principais

### Auditoria — até 100 itens

Objetivo exclusivo: medir se o score representa corretamente a qualidade observada e produzir rótulos confiáveis para recalibração.

A fila de Auditoria não cria correções, não valida providers e não promove padrões textuais. Ela responde apenas se a confiança atribuída ao segmento está coerente com a avaliação humana.

Composição estratificada:

- maiores discrepâncias entre score e decisão humana;
- alta incerteza calibrada;
- mudanças de faixa;
- recortes com pouca cobertura;
- textos longos/dinâmicos;
- controles positivos e negativos;
- segmentos conhecidos como bons com score baixo;
- segmentos conhecidos como ruins com score alto;
- regressões de calibração entre versões de score.

Saídas da Auditoria:

- qualidade humana observada;
- confiança esperada ou classe de qualidade;
- exceção válida quando o score não deve penalizar o padrão;
- recorte/família usado na calibração;
- evidência para aceitar, rejeitar ou manter em shadow um calibrador.

Se durante a Auditoria for percebido um defeito textual ainda não rastreado, o caso é encaminhado como novo sinal para Descobertas. Ele não vira automaticamente uma correção nem um rótulo negativo de calibração no mesmo ato.

Um item decidido sai imediatamente da fila visual. A fila só é reconstruída depois que o lote for consumido por avaliação e diagnóstico, evitando repetir casos antes do aprendizado entrar no próximo snapshot.

### Descobertas — até 100 itens

Objetivo exclusivo: rastrear possíveis falhas textuais ou estruturais, confirmar se constituem um padrão e direcioná-las para correção.

A fila de Descobertas alimenta detectores, providers, propostas de correção e promoções de regras. Ela não é usada diretamente para recalibrar o score.

Famílias iniciais:

- espanhol residual;
- espanhol dentro de estruturas;
- mojibake e Unicode;
- concordância dinâmica `meu/minha` e `seu/sua`;
- gênero fixo próximo de token dinâmico;
- fragmentos de helper/token;
- token redundante ou opções equivalentes;
- pontuação confundida com caractere corrompido;
- truncamento e estrutura incompleta;
- padrões ainda não classificados.

Amostragem por ganho informacional:

- novidade do padrão;
- recorrência e alcance;
- severidade;
- diversidade de arquivos e famílias;
- baixa confiança do detector;
- ausência de provider maduro;
- contradição humana recente.

Score baixo isolado não é descoberta. É apenas um sinal para amostragem quando combinado com evidência textual, estrutural ou discrepância calibrada.

Lifecycle de uma descoberta:

1. `Sinal`: detector encontra uma ocorrência suspeita.
2. `Amostra`: casos positivos, negativos e de fronteira são selecionados.
3. `Padrão confirmado`: decisões humanas confirmam recorrência e contrato.
4. `Hipótese de correção`: regra/provider propõe uma transformação.
5. `Fila especializada`: a proposta é avaliada em até 100 casos representativos.
6. `Shadow/dry-run`: mede cobertura, precisão, sentinelas alteradas e regressões.
7. `Sugestão de promoção`: somente quando os gates da correção forem satisfeitos.
8. `Coberto/monitoramento`: o padrão sai da fila ativa, mas pode reabrir se reaparecer ou regredir.

Saídas de Descobertas:

- padrão confirmado, rejeitado ou de fronteira;
- família e alcance estimado;
- provider/regra candidata;
- exemplos positivos, negativos e sentinelas;
- proposta de reparo;
- sugestão de promoção ou motivo de bloqueio;
- encaminhamento para fila especializada temporária.

## Filas especializadas temporárias

Quando uma descoberta acumular suporte suficiente, o sistema pode criar uma subfila temporária de correção/decisão. Exemplos:

- `Concordância possessiva dinâmica`;
- `Espanhol residual em Select_CString`;
- `Mojibake de fronteira`;
- `Estrutura truncada`.

Essas filas não viram abas permanentes. Elas aparecem sob Descobertas, têm versão, objetivo, tamanho limitado e encerram quando:

- o provider cobre o padrão;
- a evidência é rejeitada;
- o ganho marginal se esgota;
- o padrão volta para monitoramento.

## Ciclo regenerativo

As duas filas compartilham o snapshot e a execução, mas possuem ciclos de aprendizado independentes.

### Ciclo de qualidade textual

1. Congelar snapshot do pacote e das evidências.
2. Gerar até 100 itens de Descobertas.
3. Confirmar, rejeitar ou classificar fronteiras dos padrões.
4. Gerar filas especializadas, providers e propostas quando houver suporte.
5. Executar shadow/dry-run, validar reparos e sugerir promoções.
6. Aplicar somente promoções autorizadas no fluxo próprio.
7. Rodar avaliação e diagnóstico para medir o novo pacote.
8. Regenerar Descobertas sem itens cobertos ou sem mudança de evidência.

### Ciclo de confiabilidade do score

1. Usar o pacote e score fixados pela avaliação/diagnóstico.
2. Gerar até 100 itens de Auditoria por discrepância, incerteza e cobertura.
3. Registrar a qualidade humana observada com contrato de calibração.
4. Consumir exclusivamente esses rótulos no ledger de score.
5. Atualizar a prontidão de recalibração.
6. Quando apto, gerar e congelar um candidato de score calibrado.
7. Auditar maiores altas, quedas e mudanças de faixa.
8. Promover, manter em shadow ou rejeitar o candidato.
9. Regenerar Auditoria com os recortes ainda pouco confiáveis.

### Contrato de roteamento

| Decisão humana | Consumidor | Efeito permitido |
|---|---|---|
| Qualidade observada do segmento | calibrador/modelo de score | recalibrar confiança |
| Padrão confirmado ou negado | detector de descobertas | atualizar precisão e prioridade do padrão |
| Proposta correta ou incorreta | provider de correção | treinar/autorizar reparo |
| Pontuação legítima | classificador de pontuação | evitar falso positivo |
| Estrutura válida ou inválida | guard estrutural | proteger ou bloquear transformação |

Uma decisão de Descoberta nunca vira automaticamente rótulo de qualidade. Uma decisão de Auditoria nunca cria ou promove uma correção. O vínculo entre as frentes ocorre somente quando uma descoberta altera o pacote e a avaliação seguinte produz um novo score auditável.

## Tamanho dinâmico das filas e estado de maturidade

O limite é `até 100`, não uma obrigação de preencher 100.

Começar com 100 por frente e reduzir para 50, 25 ou apenas monitoramento quando houver estabilidade. A redução deve considerar três ciclos consecutivos, não uma única execução.

Indicadores de saturação:

- menos de 2% das descobertas geram padrão novo acionável;
- menos de 5% da auditoria altera a interpretação vigente;
- melhoria de ECE e Brier abaixo de 1% relativo por ciclo;
- nenhuma regressão crítica nova;
- falso seguro em zero;
- recortes prioritários com cobertura mínima;
- baixa recorrência de casos anteriormente resolvidos;
- poucos itens com grande discrepância ainda sem explicação.

Estado `qualidade aceitável / manutenção`:

- ECE abaixo de 10 p.p. como primeira meta e abaixo de 5 p.p. como meta madura;
- Brier estável e sem deterioração material;
- falso seguro zero no gate operacional;
- todas as regressões críticas resolvidas;
- score confiável nos recortes longos, dinâmicos e estruturados;
- descoberta acionável abaixo de 2% por três ciclos;
- filas regeneradas naturalmente menores do que o limite.

O objetivo não é zerar toda suspeita nem perseguir score de 100%. É reduzir erro desconhecido, manter o score honesto e direcionar revisão humana apenas para decisões que ainda podem mudar o sistema.

## Primeira implementação

### Fase 1 — dados e contrato

- criar ledger canônico de decisões;
- conectar decisões regenerativas aos consumidores corretos;
- materializar candidato calibrado para todo o pacote;
- criar registry e gates sem autoridade operacional.

### Fase 2 — tela de recalibração

- implementar Preparação, Comparativo, Discrepâncias e Decisão;
- reutilizar revisão focada de segmento;
- mostrar score bruto, vigente e candidato;
- implementar promoção e reversão versionadas.

### Fase 3 — filas regenerativas

- limitar Auditoria e Descobertas a 100 itens;
- implementar amostragem estratificada e deduplicação por hash/evidência;
- criar lifecycle de subfilas especializadas;
- impedir regeneração antes do consumo do lote anterior.

### Fase 4 — redução adaptativa

- calcular ganho marginal por ciclo;
- reduzir automaticamente os lotes quando o sistema entrar em manutenção;
- manter amostragem sentinela para detectar regressões e padrões novos.
