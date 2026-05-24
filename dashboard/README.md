# CK3 PT-BR BI

Dashboard local para ler indicadores consolidados do `memory/translation_engine.sqlite`.

## Backend

```powershell
python dashboard/backend.py
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
npm run dev
```

URL padrão:

```text
http://127.0.0.1:5173
```

Se a API estiver em outra porta:

```powershell
$env:VITE_DASHBOARD_API='http://127.0.0.1:8765/api'
npm run dev
```
