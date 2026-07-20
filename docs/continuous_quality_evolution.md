# Evolucao continua da qualidade

## Estado atual

O fluxo ja possui a espinha dorsal necessaria para deixar de depender de uma investigacao manual no chat:

1. o **Diagnostico** abre a epoch, mede o pacote, atualiza o segment-state e executa todos os provedores registrados;
2. cada provedor descobre candidatos em shadow, produz evidencia pairwise e passa pelo gate monotonico;
3. a **Avaliacao** transforma apenas evidencias elegiveis em confirmacoes e `needs apply`;
4. **Publicavel** escreve as confirmacoes aprovadas no output com snapshot e validacao;
5. a calibracao pairwise registra onde o score bruto nao concorda com uma melhoria validada;
6. **Nova versao** congela o estado aprovado no banco e no pacote fisico.

Os provedores sao genericos e independentes do numero da versao. Portanto, qualquer investigacao nova pode entrar no proximo Diagnostico apenas com scripts de shadow/evidencia, manifest e testes.

## Qualidade dos dados de baixo score

Na score run `#376`, os 63.423 segmentos abaixo de 50% se dividem assim:

| Coorte | Segmentos | Interpretacao |
|---|---:|---|
| Defeito textual explicito | 3.348 | Acionavel: existe issue detectada no texto. |
| Bloqueio estrutural sem issue | 2.508 | Acionavel: token ou estrutura impede promocao segura. |
| Seguro deterministico com score baixo | 10.988 | Informativo: o contrato deterministico considera o texto seguro. |
| Texto inalterado ou preservado | 46.576 | Informativo: score baixo nao foi causado por uma nova correcao. |
| Baixa confianca sem evidencia especifica | 3 | Investigacao exploratoria. |

Assim, somente 5.856 casos, ou 9,23% da fila, possuem evidencia acionavel. Score baixo continua sendo um sinal util de busca, mas nao deve funcionar sozinho como defeito, bloqueio de publicacao ou pedido de revisao humana.

## Ciclo continuo proposto

```text
medir pacote
  -> descobrir familias de problema
  -> gerar transformacoes candidatas
  -> executar shadow no pacote inteiro
  -> validar integridade e score pairwise
  -> promover alta certeza / revisar incerteza
  -> aplicar em modo Publicavel
  -> decidir calibracao por politica
  -> aprender historico do provedor
  -> repetir na proxima epoch
```

### 1. Descoberta automatica de padroes

Status: **implementada** por `pipeline/quality_pattern_discovery.py` e executada no inicio de `quality_promotion_cycle diagnostic`, depois do score e antes dos provedores. Ela:

- analisar o pacote completo, nao apenas os segmentos abaixo de 50%;
- agrupar issues por tipo, vizinhanca de tokens protegidos, familia de arquivo e diferenca textual;
- calcular prevalencia, alcance potencial, severidade, novidade e confianca;
- comparar grupos com o historico para nao reabrir familias ja tratadas;
- gravar familias candidatas em uma fila auditavel, sem alterar output ou confirmacoes.

Cada execucao gera uma run idempotente em `ml_quality_pattern_discovery_runs`, atualiza o catalogo estavel `ml_quality_pattern_families` e grava as metricas daquela epoch em `ml_quality_pattern_observations`. Uma repeticao da mesma epoch substitui a observacao da run em vez de inflar o historico. O painel expoe as familias na aba **Descobertas**, separando `nova`, `recorrente`, `coberta por provedor` e `observacao`.

O ranking combina severidade, alcance logaritmico no pacote, confianca da evidencia, novidade e concentracao abaixo de 50%. Linhas de score baixo sem issue explicita nem bloqueio estrutural sao contadas como informativas, mas nao criam familia candidata.

O baixo score serve para aumentar a prioridade de uma familia, mas uma regra descoberta deve ser pesquisada em todos os segmentos ativos. Isso permite que um padrao encontrado na cauda abaixo de 50% corrija tambem casos de score moderado ou alto.

### 2. Geracao de provedores

Cada familia candidata passa por um contrato declarativo:

- seletor reproduzivel do problema;
- transformacao deterministica ou gerador limitado de candidatos;
- invariantes de tokens e estrutura;
- validadores posteriores;
- amostras positivas, negativas e de fronteira;
- versao da regra e evidencia de origem.

No primeiro estagio, o sistema pode gerar uma **proposta de provedor** e seus casos de teste, mas deixa sua habilitacao para uma revisao no painel. Depois que o gerador demonstrar estabilidade, familias puramente deterministicas podem ser habilitadas automaticamente em shadow.

### 3. Gate de promocao por confianca

O resultado do shadow deve cair em tres faixas:

- **promocao automatica**: transformacao deterministica, integridade total, validacao limpa, evidencia pairwise positiva e provedor maduro;
- **revisao curta**: regra nova, baixa amostragem, fronteira linguistica ou score bruto igual/regressivo;
- **bloqueado**: token alterado, issue residual, conflito humano, candidato ambiguo ou regressao efetiva.

A faixa deve ser registrada em banco com os motivos. Nenhum candidato deve desaparecer apenas porque foi bloqueado; ele alimenta a proxima investigacao da familia.

## Calibracao condicional

Status: **implementada** por `pipeline/quality_calibration_policy.py` e integrada ao ciclo de Diagnostico. Cada decisao fica persistida em `ml_pairwise_calibration_policy_decisions` com metricas, motivos e quantidade recomendada de controles.

A fila de calibracao nao deve ser criada incondicionalmente. Antes dela, uma politica `quality_calibration_policy` deve emitir uma decisao auditavel: `skip`, `sample` ou `required`.

### Calibracao obrigatoria

Gerar a fila completa quando ocorrer qualquer uma destas condicoes:

- provedor novo ou com menos de 50 decisoes humanas distribuidas em tres epochs;
- mudanca do contrato, modelo ou regra de score;
- algum ajuste aplicado com score bruto igual ou regressivo;
- falha de integridade de token, validacao posterior ou controle cego;
- lote grande: pelo menos 250 segmentos ou 0,1% do pacote, o que for maior;
- queda relevante de precisao historica, confianca ou distribuicao do delta;
- familia linguistica nova ou fora do dominio ja aprendido pelo provedor.

### Calibracao por amostra

Gerar somente controles e uma amostra estratificada quando o provedor for maduro, mas o lote tiver volume ou distribuicao diferente do historico. A amostra deve cobrir arquivos, faixas de score, tipos de token e valores extremos de delta.

### Calibracao dispensada

Pular a fila quando todos forem verdadeiros:

- nenhum par aplicado ficou igual ou regrediu no score bruto;
- 100% de integridade de tokens e validacao posterior;
- contrato de score inalterado;
- provedor maduro e sem degradacao recente;
- lote abaixo do limite de alto volume;
- nenhuma familia, token ou contexto novo.

Mesmo ao pular, a epoch deve guardar a decisao e os valores que a justificaram. Isso impede um `skip` silencioso e permite auditar ou recalcular a politica.

## Automacao recomendada por etapa

| Etapa | Agora | Evolucao segura |
|---|---|---|
| Descoberta | Scripts e analise direcionada | Mineracao automatica por familia em cada Diagnostico |
| Shadow | Automatico por manifest | Manter automatico e expandir para o pacote inteiro |
| Evidencia/gate | Automaticos | Incorporar maturidade e confianca historica |
| Promocao | Automatica para evidencia elegivel | Tres faixas: automatica, revisao curta e bloqueada |
| Calibracao | Gerada a cada ciclo | Politica `skip/sample/required` |
| Apply/publicacao | Acao explicita no painel | Manter explicita inicialmente; automatizar apenas apos historico suficiente |
| Nova versao | Acao explicita | Manter explicita como checkpoint material |

## Ordem de implementacao

1. **Concluido:** persistir a politica de calibracao e trocar a chamada incondicional do Diagnostico por `skip/sample/required`.
2. **Parcialmente concluido:** medir maturidade por provedor com decisoes, epochs e acuracia supervisionada; a proxima evolucao e persistir uma serie historica propria de saude.
3. **Concluido:** materializar a fila generica de familias descobertas, usando issues e bloqueios estruturais do pacote inteiro, com deduplicacao historica e cobertura declarada pelos provedores.
4. Criar o gerador assistido de propostas de provedor e testes de fronteira.
5. Adicionar um modo de piloto automatico apenas para provedores maduros; Publicavel e Nova versao continuam como checkpoints explicitos ate haver historico suficiente.

Essa ordem reduz trabalho manual sem misturar descoberta, decisao de score e escrita fisica. Cada etapa continua reversivel, mensuravel e explicavel no painel.
