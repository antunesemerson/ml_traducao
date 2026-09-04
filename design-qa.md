# Design QA — limpeza e simetria dos módulos operacionais

## Evidência

- Fonte visual — Produção: `C:\Users\Emerson\AppData\Local\Temp\codex-clipboard-576c9e25-8b23-4913-981d-19e123911613.png` (1878 × 930 px).
- Fonte visual — Avaliação: `C:\Users\Emerson\AppData\Local\Temp\codex-clipboard-2821e20c-6fa5-454c-b006-e8438fd61d09.png` (1876 × 927 px).
- Fonte visual — Calibração: `C:\Users\Emerson\AppData\Local\Temp\codex-clipboard-b163b116-3698-4994-9701-58363d93c52a.png` (1871 × 928 px).
- Refinamentos — subtítulo, tooltips e estados: `C:\Users\Emerson\AppData\Local\Temp\codex-clipboard-8680f6da-e6a8-4939-a40c-05e9b255a4f9.png`, `C:\Users\Emerson\AppData\Local\Temp\codex-clipboard-340b9f52-e738-4851-91a5-6e7341ae8613.png` e `C:\Users\Emerson\AppData\Local\Temp\codex-clipboard-b9f9eea4-9e58-49a3-aaa6-2722c27ad1ce.png`.
- Refinamentos — menu, tema e identidade: `C:\Users\Emerson\AppData\Local\Temp\codex-clipboard-1a6bedc7-ea2c-4284-804d-fb392d8eb5cf.png`, `C:\Users\Emerson\AppData\Local\Temp\codex-clipboard-20d24acd-c321-4660-98f7-9c55e51b3b01.png`, `C:\Users\Emerson\AppData\Local\Temp\codex-clipboard-6313d971-fdd4-43b0-be54-28796b5151ba.png` e `C:\Users\Emerson\AppData\Local\Temp\codex-clipboard-e8a31efa-7ab8-4528-8204-6ded479fff0f.png`.
- Implementação — Produção: `C:\Users\Emerson\OneDrive\Área de Trabalho\Docs\Projetos\0001 - ML Tradução\qa-production-clean.png` (1880 × 928 px).
- Implementação — Avaliação: `C:\Users\Emerson\OneDrive\Área de Trabalho\Docs\Projetos\0001 - ML Tradução\qa-evaluation-clean.png` (1880 × 928 px).
- Implementação — Calibração: `C:\Users\Emerson\OneDrive\Área de Trabalho\Docs\Projetos\0001 - ML Tradução\qa-calibration-clean.png` (1880 × 928 px).
- Implementação — sistema escuro, menu recolhido: `C:\Users\Emerson\OneDrive\Área de Trabalho\Docs\Projetos\0001 - ML Tradução\qa\system-dark-collapsed.png`.
- Implementação — sistema claro: `C:\Users\Emerson\OneDrive\Área de Trabalho\Docs\Projetos\0001 - ML Tradução\qa\system-light-production.png`.
- Implementação — novo ícone no Dashboard: `C:\Users\Emerson\OneDrive\Área de Trabalho\Docs\Projetos\0001 - ML Tradução\qa\dashboard-new-icon.png`.
- Viewport CSS: 1880 × 928; densidade 1×. As referências variam até 9 px de largura, sem redimensionamento necessário para avaliar os blocos indicados.
- Estado: temas escuro e claro; menu recolhido por padrão; Avaliação em Descobertas; Calibração com score #24 ativo e monitorado; Produção na lista Pacote.

## Comparação visual

As três referências e as três capturas finais foram abertas juntas e comparadas no mesmo viewport. Os blocos circulados foram removidos sem alterar navegação, progresso, tabelas, paleta ou hierarquia do sistema.

Não foi necessário um recorte adicional: títulos, botões, trilhos de progresso, abas e primeiras linhas das tabelas permanecem legíveis nas capturas originais de 1880 px. A comparação focada foi feita diretamente nas regiões de cabeçalho, trilho e início do conteúdo de cada tela.

## Superfícies de fidelidade

- Tipografia: família, pesos e hierarquia existentes foram preservados; não houve mudança de tokens tipográficos.
- Espaçamento e ritmo: cabeçalho, ação e trilho agora compartilham exatamente `top=65`, `actionTop=89`, `actionRight=1848`, `railTop=193` e `headerHeight=249` nos três módulos.
- Cores e tokens: estados azul, rosa, âmbar, verde e superfícies do tema permanecem ligados aos tokens existentes.
- Imagens e ativos: a identidade antiga foi substituída, conforme solicitado, pelo mesmo hexágono com pulso operacional usado no sistema. O Dashboard usa o componente vetorial existente e o favicon recebeu a versão SVG correspondente.
- Conteúdo: foram removidos somente os resumos redundantes solicitados. A verificação automática do source continua no fluxo de execução, sem faixa própria na interface.

## Histórico da iteração

1. A primeira captura mostrou um desvio P2: o trilho de Produção ficava 13 px abaixo dos demais por causa da descrição em duas linhas.
2. Correção aplicada: a área de texto do cabeçalho passou a ter altura fixa e limite de duas linhas.
3. Pós-correção: Avaliação, Calibração e Produção mediram as mesmas posições e altura de cabeçalho; nenhuma sobreposição ou corte de controle foi observado.
4. As listas foram isoladas em uma região rolável: `body` e `html` permaneceram em 720/720 px, enquanto a lista mediu 277 px de viewport para 6.152 px de conteúdo.
5. O tema claro foi validado com superfícies `rgb(248, 250, 252)`/`rgb(255, 255, 255)` e texto principal `rgb(15, 23, 42)`, sem herdar os fundos pretos do tema escuro.
6. O backend confirmou o candidato #24 promovido no registro `operational_calibrated_score`, em `active_monitoring`, com zero falso seguro e nenhuma revisão pendente; o menu passa a mostrá-lo como `alta fidelidade` até surgir novo contexto, quando muda para `nova evidência`.

## Interações e console

- Alternância Avaliação → Calibração → Produção testada.
- Ação da Calibração confirmada no cabeçalho, alinhada à direita: `Iniciar nova calibração`. O comando direto `Desativar score calibrado` foi removido.
- A área inferior da Calibração agora apresenta o histórico versionado: score calibrado #24 ativo e score bruto original, com Score médio, ECE, Brier e Falsos seguros para comparação.
- As métricas do histórico explicam em tooltip como influenciam a confiança e as travas; cada versão inativa e compatível oferece `Restaurar`, inclusive o score bruto original.
- Cores operacionais confirmadas na interface visível: Avaliação em verde (`rgb(5, 150, 105)`), Calibração em rosa (`rgb(219, 39, 119)`) e Produção em azul (`rgb(37, 99, 235)`), com as mesmas cores nas linhas ativas do menu.
- Botões habilitados não expõem tooltip; botões bloqueados recebem motivo operacional via `data-disabled-reason`, com fallback global para travas não especializadas.
- Menu confirmado recolhido no primeiro carregamento e expansível pelo controle inferior.
- Subtítulo de Produção confirmado como: `Aplica promoções confirmadas, valida o output e materializa uma nova versão.`
- Novo ícone confirmado no cabeçalho do Dashboard e em `/favicon.svg`.
- Abas e tabelas permaneceram disponíveis depois da limpeza.
- Console do navegador: 0 erros.
- Build Vite concluído com sucesso.

## Findings

Nenhum P0, P1 ou P2 permanece. A diferença entre referência e implementação é intencional e corresponde à limpeza solicitada e à substituição dos controles antigos pelo histórico restaurável.

## Follow-up polish

Nenhum P3 necessário para esta solicitação.

final result: passed
