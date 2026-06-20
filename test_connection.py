"""
Script de diagnóstico de conexión con Binance.
Ejecutar: python test_connection.py
"""
import asyncio
import sys
from dotenv import load_dotenv
from binance import AsyncClient
from binance.exceptions import BinanceAPIException
from config import config


async def test():
    ok = True

    print("=" * 55)
    print("  DIAGNÓSTICO DE CONEXIÓN BINANCE")
    print("=" * 55)

    # 1. Variables de entorno
    print("\n[1] Configuración cargada desde .env")
    print(f"    TESTNET : {config.TESTNET}")
    print(f"    SYMBOL  : {config.SYMBOL}")
    has_key = bool(config.API_KEY)
    has_secret = bool(config.API_SECRET)
    print(f"    API_KEY : {'✓ presente' if has_key else '✗ VACÍA'}")
    print(f"    SECRET  : {'✓ presente' if has_secret else '✗ VACÍA'}")

    if not has_key or not has_secret:
        print("\n  ERROR: Completa BINANCE_API_KEY y BINANCE_API_SECRET en .env")
        return

    # 2. Conexión al servidor
    print("\n[2] Conectando a Binance...")
    try:
        client = await AsyncClient.create(
            config.API_KEY,
            config.API_SECRET,
            testnet=config.TESTNET,
        )
        mode = "TESTNET" if config.TESTNET else "LIVE"
        print(f"    ✓ Conectado [{mode}]")
    except Exception as exc:
        print(f"    ✗ Error de conexión: {exc}")
        return

    # 3. Ping al servidor
    print("\n[3] Ping al servidor...")
    try:
        await client.ping()
        print("    ✓ Servidor responde")
    except Exception as exc:
        print(f"    ✗ {exc}")
        ok = False

    # 4. Hora del servidor vs hora local
    print("\n[4] Sincronización de tiempo...")
    try:
        server_time = await client.get_server_time()
        import time
        diff_ms = abs(server_time["serverTime"] - int(time.time() * 1000))
        status = "✓" if diff_ms < 1000 else "⚠"
        print(f"    {status} Diferencia con servidor: {diff_ms} ms")
        if diff_ms >= 1000:
            print("      Diferencia alta — puede causar errores de firma. Sincroniza el reloj del sistema.")
    except Exception as exc:
        print(f"    ✗ {exc}")
        ok = False

    # 5. Validar API key con datos de cuenta
    print("\n[5] Validando API Key (datos de cuenta)...")
    try:
        account = await client.get_account()
        balances = {
            a["asset"]: float(a["free"])
            for a in account["balances"]
            if float(a["free"]) > 0 or a["asset"] in ("USDT", "BTC", "BNB")
        }
        print("    ✓ API Key válida")
        print(f"    Balances disponibles:")
        for asset, amount in balances.items():
            print(f"      {asset:6s}: {amount:.8f}")
    except BinanceAPIException as exc:
        print(f"    ✗ Error de API: {exc}")
        if exc.code == -2014:
            print("      → API Key con formato inválido")
        elif exc.code == -2015:
            print("      → API Key inválida, IP no permitida o permisos insuficientes")
        ok = False
    except Exception as exc:
        print(f"    ✗ {exc}")
        ok = False

    # 6. Precio del símbolo configurado
    print(f"\n[6] Precio actual de {config.SYMBOL}...")
    try:
        ticker = await client.get_symbol_ticker(symbol=config.SYMBOL)
        print(f"    ✓ {config.SYMBOL}: {float(ticker['price']):,.2f} USDT")
    except BinanceAPIException as exc:
        print(f"    ✗ Error obteniendo precio: {exc}")
        ok = False

    await client.close_connection()

    # 7. Conexión a Futures Testnet (usada por bot.py en vivo)
    print("\n[7] Conectando a Binance Futures...")
    has_fut_key = bool(config.FUTURES_API_KEY) and config.FUTURES_API_KEY != config.API_KEY
    if not has_fut_key:
        print("    ⚠ BINANCE_FUTURES_API_KEY no configurada (distinta de la de Spot).")
        print("      Genera unas en https://testnet.binancefuture.com y añádelas a .env")
        print("      antes de ejecutar bot.py. Saltando pruebas de Futures.")
    else:
        try:
            fut_client = await AsyncClient.create(
                config.FUTURES_API_KEY, config.FUTURES_API_SECRET,
            )
            if config.FUTURES_TESTNET:
                fut_client.FUTURES_URL = fut_client.FUTURES_TESTNET_URL
            fut_mode = "TESTNET" if config.FUTURES_TESTNET else "LIVE"
            print(f"    ✓ Conectado [Futures {fut_mode}]")

            balances = await fut_client.futures_account_balance()
            usdt = next((b for b in balances if b["asset"] == "USDT"), None)
            if usdt:
                print(f"    ✓ Balance Futures USDT: {float(usdt['availableBalance']):.2f}")
            else:
                print("    ⚠ No se encontró balance USDT en la cuenta de Futures.")

            for symbol in config.SYMBOLS:
                ticker = await fut_client.futures_symbol_ticker(symbol=symbol)
                print(f"    ✓ {symbol}: {float(ticker['price']):,.2f} USDT")

            await fut_client.close_connection()
        except BinanceAPIException as exc:
            print(f"    ✗ Error de API Futures: {exc}")
            ok = False
        except Exception as exc:
            print(f"    ✗ {exc}")
            ok = False

    print("\n" + "=" * 55)
    if ok:
        print("  RESULTADO: Todo correcto — el bot puede conectarse.")
    else:
        print("  RESULTADO: Hay errores — revisa los puntos marcados con ✗")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    asyncio.run(test())
