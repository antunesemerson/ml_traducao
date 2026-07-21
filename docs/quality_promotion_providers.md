# Provedores de promocao de qualidade

O fluxo de qualidade nao conhece numeros de versao nem nomes de investigacoes. Cada nova melhoria e registrada como um provedor em `pipeline/quality_promotion_providers/`.

## Contrato do ciclo

1. **Diagnostico** abre ou reutiliza o quality epoch, garante os scores comparaveis de `source/spanish_old` e `output/spanish`, atualiza o segment-state e executa todos os provedores habilitados.
2. Cada provedor produz um shadow sem alterar o pacote, materializa evidencia pareada e passa pelo gate monotonico. A evidencia registra baseline, candidato, score old, score new, delta, integridade e elegibilidade de promocao.
3. O painel lista toda evidencia `promotion_eligible` na fila **Promocoes**, independentemente do provedor que a originou.
4. **Avaliacao** descobre todos os tipos de evidencia elegiveis e cria ou atualiza confirmacoes somente para os itens que continuam prontos. Em seguida, o segment-state transforma essas confirmacoes em `needs apply`.
5. **Publicavel** e o unico modo que escreve as confirmacoes aprovadas em `output/spanish`. O comparativo old/new permanece associado a evidencia original.
6. Depois de consumir decisoes anteriores, a politica de calibracao registra `skip`, `sample` ou `required`. A fila supervisionada so e materializada para `sample` ou `required`; uma epoch ja calibrada nao e reaberta.

O Diagnostico nunca escreve confirmacoes nem arquivos do output. A Avaliacao nunca escreve arquivos do output.

Shadow, evidencia, gate e fila usam os snapshots e decisoes do SQLite como contrato
autoritativo. Na trilha `spanish_dynamic_literal` e nos estagios compartilhados de gate/fila,
artefatos em `reports/` sao opcionais e so sao gerados com `--report`. Excluir a pasta de
relatorios nao remove evidencia, nao invalida uma run concluida e nao bloqueia o ciclo.

Uma evidencia permanece auditavel depois de aplicada, mas deixa de ser uma fila ativa. O consumidor considera apenas a evidencia mais recente por segmento/tipo cujo output ainda seja igual ao baseline e diferente do candidato. Evidencia ja aplicada ou tornada obsoleta por outra alteracao nao reaparece como bloqueio.

## Metricas operacionais

O painel deriva a saude dos provedores diretamente do SQLite. Para a epoch atual, ele apresenta o funil `casos inspecionados -> elegiveis no shadow -> evidencias pareadas -> promocoes prontas`, alem das familias ativas cobertas e das familias acionaveis ainda sem provedor.

Uma execucao com zero elegiveis e saudavel quando todos os provedores terminaram: significa que o pacote foi inspecionado e nenhum caso passou pelos filtros, nao que o diagnostico deixou de executar. Runs produtivas anteriores permanecem visiveis como historico, mas nao voltam para a fila depois de aplicadas.

O fechamento operacional do pacote e a divida de qualidade sao estados independentes. `segment-state` fechado, zero pendencias e zero `needs apply` definem o fechamento operacional. Somente evidencia especifica ainda nao resolvida — familia acionavel sem provedor, promocao pronta, regressao efetiva, feedback aberto ou calibracao pendente — entra na divida de qualidade. Score baixo isolado permanece como sinal de monitoramento porque o classificador atual e sensivel a baseline.

Depois da escrita, o bridge de lifecycle resolve a evidencia pelo `evidence_type` registrado e pela correspondencia exata `baseline -> candidato -> confirmacao/output`; o rótulo da confirmacao nao participa do roteamento. Um ajuste aplicado cujo score bruto regrediu ou permaneceu igual entra na fila de revisao de Regressoes mesmo quando o score efetivo pairwise e favoravel. Isso preserva simultaneamente a decisao aprovada e a evidencia de que o modelo bruto pode precisar de calibracao.

## Registrar uma nova investigacao

Quando a descoberta encontra uma familia acionavel sem cobertura, `quality_provider_proposal_generator.py` cria primeiro uma proposta desabilitada no SQLite. A proposta agrupa familias compativeis por issue/contexto, sugere a identidade do provedor e materializa casos positivos, negativos e de fronteira. Ela nunca cria um provedor ativo, nao escreve confirmacoes e nao altera score ou output.

O painel exibe esses rascunhos em **Propostas**. Somente depois da revisao do contrato e da implementacao dos scripts abaixo o manifest pode ser habilitado.

Uma investigacao nova precisa de:

- um script de shadow que descubra candidatos e gere artefatos auditaveis;
- um script de evidencia que grave linhas em `ml_pairwise_quality_evidence` com um `evidence_type` exclusivo;
- um manifest JSON com `schema_version`, `provider_id`, `evidence_type`, `shadow_script` e `evidence_script`;
- o contrato opcional `discovery.issue_types`, declarando quais issues da mineracao generica ja sao cobertas pelo provedor;
- testes focados no reparo, integridade de tokens e classificacao dos casos rejeitados.

Os scripts declarados precisam estar diretamente em `pipeline/`, e manifests nao podem injetar `--apply`. O orquestrador concede escrita apenas nas etapas controladas de evidencia, gate e aprovacao.

Exemplo:

```json
{
  "schema_version": 1,
  "provider_id": "nome_estavel_do_padrao",
  "label": "Descricao humana",
  "enabled": true,
  "priority": 300,
  "evidence_type": "deterministic_nome_estavel_do_padrao",
  "discovery": {
    "issue_types": ["issue_detectada_pelo_score"]
  },
  "shadow_script": "pipeline/quality_nome_estavel_shadow.py",
  "shadow_args": [],
  "evidence_script": "pipeline/quality_nome_estavel_pairwise_evidence.py",
  "evidence_args": []
}
```

Adicionar esse manifest basta para o padrao entrar no proximo Diagnostico e na Avaliacao generica; nenhuma alteracao adicional no backend ou no frontend deve ser necessaria.

## Provedores atuais

- `token_punctuation_boundary`: remove apenas espaco indevido entre token protegido e pontuacao.
- `gender_token_prefix`: remove prefixo duplicado antes de token de genero quando o radical possui suporte confiavel.
- `spanish_dynamic_literal`: traduz a subfamilia lexical invariavel e pares verbais no passado dentro de comandos dinamicos. Os verbos so entram quando guardas contextuais rejeitam preposicao incompatível, verbo finito concorrente, composicao reflexiva/causativa, final vazio, reparo parcial, residuo espanhol, alteracao de token ou conflito humano. Os demais casos continuam no shadow sem promocao automatica.
- `mojibake_lexicon`: reconstroi palavras com `?` apenas quando existe um unico candidato lexical com suporte no corpus. Casos ambiguos, sem suporte, com outros problemas, bloqueio humano, alteracao de token ou validacao residual permanecem somente no shadow.
- `dynamic_name_de_prefix`: completa `d [nome dinamico]` para `de [nome dinamico]` somente diante de getters de primeiro nome ou nome completo. Titulos, relacoes e helpers de genero permanecem bloqueados porque podem exigir `de`, `do` ou `da`.

## Identidade e versao

`provider_id` e `evidence_type` descrevem o padrao e nao a versao do pacote. Evolucoes internas pertencem ao `source_rule_version` da evidencia. Se a semantica do reparo mudar a ponto de tornar a comparacao incompatível, deve ser criado um novo `evidence_type`; o numero da versao materializada do pacote nunca deve ser usado como roteamento.
