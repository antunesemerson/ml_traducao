![CK3 PT-BR localization pipeline](assets/thumbnail.png)

# CK3 PT-BR Localization Pipeline

Sistema local/offline para traduzir, revisar e refinar a localizacao do Crusader Kings III para Portugues do Brasil. O projeto preserva a estrutura dos arquivos `.yml` do CK3, combina regras deterministicas, memoria confiavel, revisao humana e aprendizado local, e gera um mod que substitui o pacote espanhol.

## Estrutura

```text
source/      fontes do jogo e referencia historica local
output/      saida final do mod
pipeline/    scripts e orquestracao do pipeline
memory/      banco SQLite, modelos e dados locais
docs/        documentacao estavel do projeto
prompts/     prompts temporarios ignorados pelo Git
reports/     relatorios gerados
logs/        logs de execucao
dashboard/   interface local, backend e dashboards
```

`source/`, `output/`, `memory/`, `reports/`, `logs/` e `prompts/` sao dados locais/temporarios e nao sao o foco do versionamento Git.

## Instalacao

Python:

```powershell
pip install -r requirements.txt
```

Dashboard:

```powershell
cd dashboard
npm install
npm run build
```

## Start Local

Backend:

```powershell
python dashboard/backend.py --host 127.0.0.1 --port 8765
```

Frontend estatico:

```powershell
python -m http.server 5173 --bind 127.0.0.1 -d dashboard/dist
```

URLs:

```text
http://127.0.0.1:5173
http://127.0.0.1:8765/api/dashboard
```

## Aprendizado

O projeto aprende com revisoes humanas, feedback de jogo, memoria confiavel e checkpoints locais. Para estudar a arquitetura, comece por:

- [Aprendizado guiado do ML local](docs/ml_guided_learning.md)
- [Arquitetura neuro-simbolica](docs/neurosymbolic_network_architecture.md)
- [Arquitetura do fluxo de producao](docs/production_flow_architecture.md)

Regra de ouro:

```text
ML recomenda.
Regras bloqueiam.
Humano confirma.
```
