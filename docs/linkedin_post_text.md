Como Crusader Kings III não possui tradução oficial para português, os jogadores brasileiros dependem de mods feitos pela comunidade — e traduzir um jogo inteiro, acompanhar atualizações e corrigir bugs gratuitamente dá muito mais trabalho do que parece.

Traduzir um jogo de nicho parece simples... até você perceber que não são apenas 288 mil linhas de texto.

São 288 mil segmentos com variáveis dinâmicas, condições, tokens, gênero, títulos, cultura, fé, personagens, eventos e contexto medieval se combinando em milhares de situações possíveis dentro do jogo.

Esse é o desafio por trás do mod PT-BR de Crusader Kings III que venho desenvolvendo para Steam Workshop e Paradox Mods.

Hoje o projeto já está com 811 inscritos somando as duas plataformas, com jogadores reais usando, testando e mandando feedback.

Porém o mais interessante não foi só traduzir o jogo.

Foi construir um sistema para traduzir, validar, comparar e melhorar essa tradução com segurança.

A pipeline combina:

- Machine Learning e NLP;
- regras e validadores especializados;
- score de qualidade por segmento;
- comparação entre versão antiga e nova;
- filas de promoção, regressão, apply e revisão manual;
- aprendizado com decisões humanas e feedback visual do jogo;
- dry-run, snapshots e apply protegido antes de publicar mudanças.

A lógica é simples:

🧠 ML recomenda.<br>
🛡️ Regras protegem.<br>
✅ Humano confirma.

No fim, virou um laboratório real de engenharia, dados e Machine Learning responsável: nada de automação cega, e sim um sistema que ajuda a decidir o que pode ser melhorado sem quebrar o que já está bom.

Conheça o projeto:

🔗 GitHub: https://github.com/antunesemerson/ml_traducao<br>
🎮 Steam Workshop: https://steamcommunity.com/sharedfiles/filedetails/?id=3728653302<br>
🏰 Paradox Mods: https://mods.paradoxplaza.com/mods/144303/Any

#MachineLearning #NLP #DataEngineering #Python #GameLocalization #Automation #MLOps #CrusaderKingsIII
