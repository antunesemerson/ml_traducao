# Documentação Do Projeto

Esta pasta guarda documentação estável do projeto CK3 PT-BR.

Use `docs/` para materiais que devem acompanhar o repositório:

- explicações técnicas;
- decisões arquiteturais;
- guias de estudo;
- regras de localização;
- exceções contextuais;
- documentação do pipeline.

Não use `docs/` para prompts temporários de chats paralelos. Esses arquivos ficam em:

```text
prompts/
```

A pasta `prompts/` é ignorada no Git porque os prompts podem ser criados, recriados e removidos conforme a necessidade.

## Arquivos Atuais

- `commands.md`: comandos principais do pipeline.
- `ml_guided_learning.md`: guia de estudo da camada de ML local.
- `contextual_localization_exceptions.md`: exceções contextuais de localização.
- `gender_tokens_ptbr.md`: notas sobre tokens de gênero em PT-BR.
