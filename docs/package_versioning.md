# Versionamento de pacotes

O histórico de qualidade dos pacotes é persistido no banco sem copiar os arquivos de localização nem duplicar o banco completo.

## Versões iniciais

- **v1 - Pacote estável anterior:** conteúdo de `source/spanish_old` no congelamento.
- **v2 - Pacote validado atual:** conteúdo de `output/spanish` no congelamento.
- A v2 é filha da v1 e registra todas as diferenças textuais entre os dois pacotes.

O manifesto inicial está em `docs/package_versions/package_versions_v1_v2.json`.

## Métricas

- `full_average_score`: média bruta de todos os segmentos medidos pelo score run associado à versão.
- `change_cohort_score`: score ponderado dos segmentos alterados e prontos para o pacote.
- `change_cohort_delta`: diferença ponderada do conjunto alterado em relação à versão pai.

Essas métricas respondem perguntas diferentes e não devem ser combinadas como se fossem o mesmo indicador. O dashboard futuro deve apresentar qualidade global e ganho do conjunto alterado separadamente.

## Estrutura

- `package_versions`: identidade, hashes, métricas e relação entre versões.
- `package_version_items`: hash, score e estado de cada segmento em cada versão.
- `package_version_changes`: textos e scores somente dos segmentos que mudaram em relação à versão pai.

## Congelamento

```powershell
python pipeline\package_version_snapshot.py --old-version 1 --output-version 2
```

Os números de versão são imutáveis. Uma nova execução com o mesmo número e outro hash é bloqueada.
