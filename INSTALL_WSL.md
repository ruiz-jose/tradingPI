# Guía de Instalación en WSL (Windows Subsystem for Linux)

Bot de trading Binance con estrategia EMA Crossover — ejecución sobre Windows vía WSL 2.

---

## Requisitos previos

- Windows 10 (build 19041+) o Windows 11
- WSL 2 instalado con Ubuntu 22.04 o superior
- Python 3.10+ dentro de la distro WSL
- Cuenta en Binance con API Key generada

### Instalar WSL 2 (si aún no lo tienes)

Abre PowerShell como Administrador:

```powershell
wsl --install
```

Reinicia el equipo cuando se te pida. Al volver, Ubuntu se configura automáticamente y te pide usuario y contraseña.

Verifica la versión:

```bash
wsl --list --verbose
```

La columna `VERSION` debe mostrar `2`. Si muestra `1`:

```powershell
wsl --set-version Ubuntu 2
```

---

## 1. Actualizar el sistema e instalar dependencias

Abre la terminal de Ubuntu/WSL y ejecuta:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git python3-pip python3-venv
```

---

## 2. Copiar o clonar el proyecto

**Opción A — Clonar desde Git:**

```bash
cd ~
git clone <URL_DE_TU_REPO> tradingPI
cd tradingPI
```

**Opción B — Copiar desde Windows:**

Los archivos de Windows están disponibles en `/mnt/c/`. Cópialos al home de WSL
para evitar problemas de rendimiento con I/O de disco:

```bash
cp -r /mnt/c/temp/2026/Claude/tradingPI ~/tradingPI
cd ~/tradingPI
```

---

## 3. Crear entorno virtual e instalar dependencias

```bash
cd ~/tradingPI
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

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

# Futures (USD-M) — usado por bot.py EN VIVO (largos y cortos, multi-símbolo).
# Son keys DISTINTAS a las de Spot: generarlas en https://testnet.binancefuture.com
# (botón "API Key" tras loguearte con tu cuenta de GitHub/Binance).
BINANCE_FUTURES_API_KEY=tu_futures_api_key_aqui
BINANCE_FUTURES_API_SECRET=tu_futures_api_secret_aqui

# true = Futures Testnet (dinero ficticio) | false = Futures real (dinero real)
FUTURES_TESTNET=true

LEVERAGE=2
MARGIN_TYPE=ISOLATED
SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT
MAX_CONCURRENT_POSITIONS=3
```

Guarda: `Ctrl+O`, `Enter`, `Ctrl+X`.

---

## 5. Probar que el bot arranca correctamente

```bash
source ~/tradingPI/venv/bin/activate
python3 ~/tradingPI/bot.py
```

Deberías ver en consola:

```
2026-06-12 10:00:00 [INFO] bot: Conectado a Binance Futures [TESTNET] | Pares: BTCUSDT, ETHUSDT, SOLUSDT | Intervalo: 4h
2026-06-12 10:00:01 [INFO] bot: [BTCUSDT] WebSocket Futures activo. Esperando señales...
```

Para detenerlo: `Ctrl+C`.

---

## 6. Ejecución automática al iniciar Windows

Hay dos métodos según si tu WSL tiene systemd activo o no.

### Método A — Task Scheduler de Windows (funciona siempre, recomendado)

Este método usa el archivo `start_wsl.bat` incluido en el proyecto.

#### 6.1 Editar el script `.bat`

Abre `start_wsl.bat` en un editor de texto y ajusta las dos variables:

```batch
set DISTRO=Ubuntu          ← nombre exacto de tu distro (wsl --list)
set USUARIO=tuusuario      ← tu usuario WSL (en WSL ejecuta: whoami)
```

Guarda el archivo en una ruta accesible desde Windows, por ejemplo:
`C:\Users\TuUsuarioWindows\tradingPI\start_wsl.bat`

#### 6.2 Crear la tarea programada

Abre **Programador de tareas** (`taskschd.msc`) y sigue estos pasos:

1. **Acción → Crear tarea básica**
2. Nombre: `TradingBot WSL`
3. Desencadenador: **Al iniciar sesión**
4. Acción: **Iniciar un programa**
   - Programa: `C:\Users\TuUsuarioWindows\tradingPI\start_wsl.bat`
5. En **Propiedades → General**, marca:
   - ☑ Ejecutar tanto si el usuario inició sesión como si no
   - ☑ Ejecutar con los privilegios más altos
6. En **Condiciones**, desmarca "Iniciar la tarea solo si el equipo está en CA"

> El `.bat` mantiene la ventana abierta mientras el bot corre. Task Scheduler la
> gestiona en segundo plano; si el bot falla, puedes configurar el reinicio en
> la pestaña **Configuración → Si la tarea falla, reiniciar cada: 1 minuto**.

---

### Método B — systemd dentro de WSL 2 (Windows 11 + Ubuntu 22.04+)

#### 6.1 Habilitar systemd en WSL

Dentro de la terminal WSL:

```bash
sudo nano /etc/wsl.conf
```

Añade o edita el contenido:

```ini
[boot]
systemd=true
```

Guarda y **reinicia WSL** desde PowerShell:

```powershell
wsl --shutdown
```

Abre de nuevo la terminal WSL y verifica:

```bash
systemctl --version
```

Si muestra una versión, systemd está activo.

#### 6.2 Instalar el servicio

Sustituye `USUARIO` por tu nombre de usuario WSL (`whoami`):

```bash
# Sustituir USUARIO en el archivo de servicio
sed "s/USUARIO/$(whoami)/g" ~/tradingPI/tradingpi-wsl.service \
  | sudo tee /etc/systemd/system/tradingpi.service

sudo systemctl daemon-reload
sudo systemctl enable tradingpi
sudo systemctl start tradingpi
```

#### 6.3 Verificar que está corriendo

```bash
sudo systemctl status tradingpi
```

Debes ver `Active: active (running)`.

---

## 7. Ver los logs

**Desde WSL:**

```bash
# Log del bot
tail -f ~/tradingPI/bot.log

# Con systemd
sudo journalctl -u tradingpi -f
```

**Desde PowerShell/CMD de Windows (acceso directo a los logs):**

```powershell
Get-Content "\\wsl$\Ubuntu\home\tuusuario\tradingPI\bot.log" -Wait -Tail 50
```

---

## Comandos útiles de gestión

| Acción | Comando (en WSL) |
|---|---|
| Ver estado (systemd) | `sudo systemctl status tradingpi` |
| Detener el bot | `sudo systemctl stop tradingpi` |
| Reiniciar el bot | `sudo systemctl restart tradingpi` |
| Deshabilitar autoarranque | `sudo systemctl disable tradingpi` |
| Ver logs en vivo | `sudo journalctl -u tradingpi -f` |
| Ver logs del archivo | `tail -f ~/tradingPI/bot.log` |

---

## Solución de problemas

**Error: `No module named 'binance'`**
```bash
source ~/tradingPI/venv/bin/activate
pip install -r requirements.txt
```

**El `.bat` se abre y cierra sin hacer nada**
Verifica que `DISTRO` coincide exactamente con el nombre de tu distro:
```powershell
wsl --list
```

**Error de WSL: `A connection attempt failed...`**
La interfaz de red de WSL puede tardar en estar disponible al arrancar Windows.
Añade en `start_wsl.bat` antes de la línea `wsl`:
```batch
timeout /t 15 /nobreak >nul
```

**El servicio systemd no arranca**
```bash
sudo journalctl -u tradingpi --no-pager -n 50
```
Verifica que las rutas en `/etc/systemd/system/tradingpi.service` son correctas.

**`wsl --shutdown` interrumpe el bot en mitad de una operación**
Usa `sudo systemctl stop tradingpi` antes de apagar WSL para dar tiempo al bot
a cerrar la conexión con Binance limpiamente.

---

## Estructura de archivos

```
~/tradingPI/
├── bot.py                  # Punto de entrada principal
├── config.py               # Configuración (lee .env)
├── strategy.py             # Lógica EMA Crossover
├── risk_manager.py         # Gestión de riesgo
├── logger.py               # Registro de operaciones en SQLite
├── requirements.txt        # Dependencias Python
├── .env                    # Tus claves y configuración (no subir a Git)
├── .env.example            # Plantilla de configuración
├── tradingpi-wsl.service   # Servicio systemd para WSL
├── start_wsl.bat           # Script de arranque para Task Scheduler
└── bot.log                 # Log de operaciones (se crea al ejecutar)
```
