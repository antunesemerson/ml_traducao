# Design QA — sistema visual do Project Intelligence

- Fonte visual da Visão Geral: `assets/validation-screenshots/design-audit-project-intelligence-after.png`.
- Implementação da Visão Geral: `assets/validation-screenshots/design-qa-overview-after-dark.png`.
- Fonte visual da Network: `assets/validation-screenshots/design-qa-network-before.png`.
- Implementação da Network: `assets/validation-screenshots/design-qa-network-after-dark.png`.
- Comparações completas: `assets/validation-screenshots/design-qa-overview-token-comparison.png` e `assets/validation-screenshots/design-qa-network-token-comparison.png`.
- Evidência complementar: `assets/validation-screenshots/design-qa-overview-after-light.png` e `assets/validation-screenshots/design-qa-network-after-light.png`.
- Viewport: 1280 × 720.
- Estado: dados reais carregados, tema escuro como referência principal, layout favorito salvo na Network.

## Findings

- Nenhum P0, P1 ou P2 permaneceu após a comparação final.
- A Visão Geral mantém composição, densidade, hierarquia, conteúdo e proporção dos dois gráficos aprovados.
- A Network preserva a identidade do atlas e passa a usar as mesmas superfícies, bordas, elevação, controles e estados ativos do dashboard.
- A aba Network agora pode renderizar sua estrutura imediatamente, sem bloquear o atlas enquanto o payload geral do dashboard é carregado.

## Superfícies obrigatórias

- Fontes e tipografia: família Inter/system preservada; pesos, escala, altura de linha, rótulos e números tabulares permanecem legíveis e coerentes entre as abas.
- Espaçamento e ritmo: cabeçalho, navegação segmentada, botões de ícone, cartões e painel de métricas usam alturas e raios semânticos compartilhados.
- Cores e tokens: temas claro e escuro usam a mesma camada de tokens para canvas, superfície, superfície elevada, borda, texto, hover, foco e estados semânticos.
- Imagens e ativos: favicon e ícones Lucide existentes foram preservados; não houve substituição de ativos por placeholders.
- Copy e conteúdo: textos e métricas operacionais foram preservados; não houve alteração de significado dos dados.

## Interações verificadas

- Alternância Visão Geral ↔ Network.
- Alternância de tema claro ↔ escuro.
- Seleção de nó e abertura do painel de detalhe da Network.
- Salvar e restaurar o layout favorito do atlas.
- Renderização de 1 linha de evolução, 5 barras de qualidade e 31 nós com dados reais.
- Console: nenhum erro na navegação e nas interações finais; apenas mensagens informativas do Vite/React no ambiente de desenvolvimento.

## Comparação e histórico

1. Primeira comparação: a Network foi capturada com o favorito não salvo, diferente da referência.
2. Correção: o estado favorito foi salvo, a tela foi recarregada e a comparação foi refeita no mesmo viewport e tema.
3. Evidência pós-correção: `assets/validation-screenshots/design-qa-network-token-comparison.png`; o estado e a composição passaram a corresponder à referência.
4. A Visão Geral passou na primeira comparação após a tokenização; `assets/validation-screenshots/design-qa-overview-token-comparison.png` não mostrou drift acionável.

## Focused regions

Não foi necessário um recorte adicional: os controles, KPIs, eixos, rótulos dos nós e estados ativos permanecem legíveis nas comparações lado a lado em resolução original.

## Follow-up polish

- P3: a Network em tema claro mantém conexões deliberadamente discretas para não competir com os nós; pode receber um modo opcional de alto contraste no futuro.
- P3: um breakpoint estreito não foi capturado nesta rodada porque o produto-alvo é o desktop; a estrutura responsiva existente foi preservada.

final result: passed

---

# Design QA — gráfico de evolução dos pacotes

- Fonte visual: `assets/validation-screenshots/design-qa-package-evolution-source.png`.
- Implementação final: `assets/validation-screenshots/design-qa-package-evolution-area-dark.png`.
- Estado interativo: `assets/validation-screenshots/design-qa-package-evolution-tooltip-dark.png`.
- Viewport: 1866 × 918.
- Estado: Visão Geral, tema escuro, dados reais carregados, pacote V7 candidato.

## Findings

- Nenhum P0, P1, P2 ou P3 permaneceu após a comparação final.
- A linha contínua passa pelos cinco pontos de dados e usa o mesmo token azul dos marcadores.
- A área parte da curva e se dissolve verticalmente em gradiente até a base do gráfico, ocupando o espaço sem reduzir a legibilidade da grade.
- Os eixos, rótulos, cartões e o gráfico de distribuição lateral permaneceram visualmente estáveis.

## Interações verificadas

- Foco acessível no ponto V6 abriu o tooltip completo, com score, cobertura, faixas e contrato comparável.
- Os cinco pontos mantêm área de interação ampliada, `tabIndex` e texto acessível.
- Console do navegador sem erros ou avisos durante a validação final.

## Comparação e histórico

1. A referência foi capturada com pontos desconectados e sem preenchimento de área.
2. A implementação foi comparada no mesmo viewport e tema, em uma única entrada visual lado a lado.
3. A primeira comparação pós-implementação passou: a nova linha e o gradiente correspondem à direção solicitada e não introduziram drift acionável nas demais superfícies.

## Focused regions

Não foi necessário recorte adicional: o gráfico ocupa a maior área das capturas em resolução original, e linha, gradiente, pontos e eixos permanecem legíveis. O estado do tooltip foi capturado separadamente para verificar o conteúdo e a sobreposição.

final result: passed
