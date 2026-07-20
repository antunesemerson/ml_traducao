# Auditoria de legibilidade dos tooltips

## Escopo

- Superfície: tooltip global do Production Control, compartilhado com Dashboard e Neural Atlas.
- Objetivo: preservar o destaque do título em negrito sem o aspecto borrado observado no Windows/Opera.
- Referências fornecidas: `codex-clipboard-f6cfc2aa-f2dd-4b5d-ad6e-8b6627bb48f8.png` e `codex-clipboard-ab6079bb-27c1-4e0c-9b69-799951693bb1.png`.
- Evidência final: `assets/validation-screenshots/tooltip-audit-02-after.png`, capturada em viewport `1920 × 1080`.

## Etapas auditadas

1. **Print com dois monitores — atenção necessária.** A imagem combinada mede dois desktops lado a lado e é reduzida para caber no visualizador. Com isso, texto de `12 px` pode aparecer próximo de `6 px` na prévia, ampliando a sensação de desfoque.
2. **Tooltip anterior — ajuste necessário.** A inspeção no navegador confirmou título em peso `900`, `letter-spacing: -0.3px`, `filter: blur(0px)` após a animação e `backdrop-filter: blur(12px)` na superfície.
3. **Tooltip corrigido — saudável.** O título permanece destacado com peso `700`, espaçamento normal, `font-synthesis: none`, sem `filter` e sem `backdrop-filter`. O tooltip nativo continua removido e a camada global permanece acima dos cards.

## Resultado

- Título mais nítido, ainda visualmente hierárquico.
- Corpo e status preservados.
- Mesmo tratamento aplicado aos tooltips globais e aos tooltips dos gráficos Recharts.
- `0` atributos `title` nativos no estado validado.
- Console do navegador sem erros.
- `npm.cmd run build` aprovado; apenas o aviso conhecido de chunk acima de `500 kB`.

## Limites da evidência

- A captura confirma o resultado renderizado em Chromium a `1920 × 1080` e `devicePixelRatio = 1`.
- Nitidez percebida também depende de escala do Windows, zoom do navegador, painel físico e ClearType; isso não equivale a uma certificação completa de acessibilidade visual.
