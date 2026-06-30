# TradingPI

## Dashboard web

Panel de monitoreo en vivo del bot (solo lectura): lee el historial de
operaciones desde `trades.db` y consulta el API de Binance Futures
(misma cuenta/keys de `config.py`) para balance y posiciones abiertas.
Corre como proceso independiente y no interfiere con el loop de trading
de `bot.py` — se puede tener ambos corriendo en paralelo.

### Requisitos

- Python 3.13
- Dependencias del proyecto (incluye Flask y python-binance):

```powershell
pip install -r requirements.txt
```

- `config.py` con las credenciales de Binance Futures configuradas
  (`FUTURES_API_KEY`, `FUTURES_API_SECRET`, `FUTURES_TESTNET`, etc.)

### Ejecutar

```powershell
python dashboard.py
```

El servidor queda escuchando en el puerto `5000`:

- Local: http://127.0.0.1:5000
- Red local: http://<tu-ip-local>:5000

Abrir esa URL en el navegador para ver el panel. Detener el proceso con
`Ctrl+C`.

### Endpoints de la API

| Endpoint         | Descripción                                              |
|------------------|-----------------------------------------------------------|
| `/api/overview`  | Balance, posiciones abiertas y configuración del bot (vivo, desde Binance) |
| `/api/stats`     | Estadísticas agregadas (win rate, profit factor, equity curve) desde `trades.db` |
| `/api/trades`    | Últimas 50 operaciones registradas en `trades.db` |
| `/api/log`       | Últimas 80 líneas de `bot.log` |

Nota: usa `flask run` en modo desarrollo (servidor no apto para
producción). Es solo para monitoreo personal del bot.
