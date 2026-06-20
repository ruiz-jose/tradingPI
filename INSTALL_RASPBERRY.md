# Guía de Instalación en Raspberry Pi

Bot de trading Binance con estrategia EMA Crossover (EMA9/EMA21).

---

## Requisitos previos

- Raspberry Pi con Raspberry Pi OS (Bullseye o superior)
- Python 3.10 o superior (`python3 --version`)
- Conexión a internet estable
- Cuenta en Binance con API Key generada

---

## 1. Actualizar el sistema

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git python3-pip python3-venv
```

---

## 2. Clonar el proyecto

```bash
cd /home/pi
git clone <URL_DE_TU_REPO> tradingPI
cd tradingPI
```

> Si no usas Git, copia los archivos manualmente con `scp` o un USB y colócalos en `/home/pi/tradingPI/`.

---

## 3. Crear entorno virtual e instalar dependencias

Si la carpeta `venv` ya existe, elimínala primero:

```bash
rm -rf venv
```

Luego crea el entorno virtual:

```bash
python3 -m venv venv
```

Activa el entorno:

```bash
source venv/bin/activate
```

Instala las dependencias:

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

> **Nota:** El `pip install --upgrade pip` garantiza que tienes la última versión de pip.

---

## 4. Configurar las variables de entorno

```bash
cp .env.example .env
nano .env
```

Rellena el archivo `.env` con tus datos:

```env
# Spot — usado por backtest.py, backtest_multi.py, optimize.py
BINANCE_API_KEY=tu_api_key_aqui
BINANCE_API_SECRET=tu_api_secret_aqui

# true = Testnet (dinero ficticio) | false = cuenta real
TESTNET=true

# Par a tradear en los scripts de backtest single-symbol
SYMBOL=BTCUSDT

# Capital inicial disponible (en USDT)
INITIAL_CAPITAL=100

# Futures (USD-M) — usado por bot.py EN VIVO (largos y cortos, multi-símbolo).
# Son keys DISTINTAS a las de Spot: generarlas en https://testnet.binancefuture.com
BINANCE_FUTURES_API_KEY=tu_futures_api_key_aqui
BINANCE_FUTURES_API_SECRET=tu_futures_api_secret_aqui
FUTURES_TESTNET=true
LEVERAGE=2
MARGIN_TYPE=ISOLATED
SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT
MAX_CONCURRENT_POSITIONS=3
```

> **Recomendado:** empieza con `TESTNET=true` y `FUTURES_TESTNET=true` para probar sin riesgo.
> Spot testnet: https://testnet.binance.vision — Futures testnet: https://testnet.binancefuture.com

Guarda el archivo: `Ctrl+O`, `Enter`, `Ctrl+X`.

---

## 5. Probar que el bot arranca correctamente

```bash
source venv/bin/activate
python3 bot.py
```

Deberías ver en consola algo como:

```
2026-06-12 10:00:00 [INFO] bot: Conectado a Binance [TESTNET] | Par: BTCUSDT | Intervalo: 1h
2026-06-12 10:00:01 [INFO] bot: Warmup completo: 59 velas | EMA9=... | EMA21=...
2026-06-12 10:00:01 [INFO] bot: WebSocket activo. Esperando señales...
```

Para detenerlo: `Ctrl+C`.

---

## 6. Instalar como servicio systemd (ejecución automática)

Esto hace que el bot arranque solo al encender la Raspberry Pi.

### 6.1 Ajustar la ruta del servicio

Abre el archivo del servicio y verifica que `ExecStart` apunte al Python correcto:

```bash
nano tradingpi.service
```

Cambia la línea `ExecStart` para usar el entorno virtual:

```ini
ExecStart=/home/pi/tradingPI/venv/bin/python3 /home/pi/tradingPI/bot.py
```

Guarda: `Ctrl+O`, `Enter`, `Ctrl+X`.

### 6.2 Copiar e instalar el servicio

```bash
sudo cp tradingpi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tradingpi
sudo systemctl start tradingpi
```

### 6.3 Verificar que está corriendo

```bash
sudo systemctl status tradingpi
```

Debes ver `Active: active (running)`.

---

## 7. Ver los logs en tiempo real

```bash
sudo journalctl -u tradingpi -f
```

Los logs también se guardan en `/home/pi/tradingPI/bot.log`.

---

## 8. Evitar que la Raspberry Pi se suspenda o apague sola

Para mantener el bot funcionando todo el tiempo, desactiva las acciones de energía automáticas.

1. Edita el archivo de configuración de logind:

```bash
sudo nano /etc/systemd/logind.conf
```

Agrega o modifica estas líneas:

```ini
HandlePowerKey=ignore
HandleSuspendKey=ignore
HandleHibernateKey=ignore
HandleLidSwitch=ignore
HandleLidSwitchDocked=ignore
IdleAction=ignore
IdleActionSec=0
```

Guarda y cierra con `Ctrl+O`, `Enter`, `Ctrl+X`.

2. Reinicia el servicio de logind:

```bash
sudo systemctl restart systemd-logind
```

3. Bloquea los targets de suspensión/hibernación para evitar que se activen:

```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

4. Si usas Raspberry Pi OS con escritorio, desactiva también el apagado/pantalla en el configurador:

```bash
sudo raspi-config
```

Luego ve a:
- `Display Options`
- `Screen Blanking` → `Disable`

---

## Comandos útiles de gestión

| Acción | Comando |
|---|---|
| Ver estado | `sudo systemctl status tradingpi` |
| Detener el bot | `sudo systemctl stop tradingpi` |
| Reiniciar el bot | `sudo systemctl restart tradingpi` |
| Deshabilitar autoarranque | `sudo systemctl disable tradingpi` |
| Ver logs en vivo | `sudo journalctl -u tradingpi -f` |

---

## Solución de problemas

**Error: `Unable to symlink... Text file busy: venv/bin/python3`**
```bash
# Esto ocurre cuando la venv existe o está corrupta.
rm -rf venv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**Error: `No module named 'binance'`**
```bash
source venv/bin/activate
pip install -r requirements.txt
```

**Error: `APIError: Invalid API-key`**
Revisa que las claves en `.env` sean correctas y que la IP de tu Raspberry esté permitida en la configuración de la API de Binance.

**El servicio no arranca**
```bash
sudo journalctl -u tradingpi --no-pager -n 50
```
Lee el error y verifica rutas en el archivo `.service`.

---

## Estructura de archivos

```
/home/pi/tradingPI/
├── bot.py              # Punto de entrada principal
├── config.py           # Configuración (lee .env)
├── strategy.py         # Lógica EMA Crossover
├── risk_manager.py     # Gestión de riesgo
├── logger.py           # Registro de operaciones
├── requirements.txt    # Dependencias Python
├── .env                # Tus claves y configuración (no subir a Git)
├── .env.example        # Plantilla de configuración
├── tradingpi.service   # Servicio systemd
└── bot.log             # Log de operaciones (se crea al ejecutar)
```
