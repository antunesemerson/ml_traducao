# CK3 PT-BR BI

Dashboard local para ler indicadores consolidados do `memory/translation_engine.sqlite`.

## Backend

```powershell
python dashboard/backend.py --host 127.0.0.1 --port 8765
```

API:

```text
http://127.0.0.1:8765/api/dashboard
http://127.0.0.1:8765/api/health
```

O backend é somente leitura e usa apenas a biblioteca padrão do Python.

## Frontend

```powershell
cd dashboard
npm install
npm run build
cd ..
python -m http.server 5173 --bind 127.0.0.1 -d dashboard/dist
```

URL padrão:

```text
http://127.0.0.1:5173
```

O frontend padrao e estatico; use `npm run dev` apenas para desenvolvimento visual ativo.
