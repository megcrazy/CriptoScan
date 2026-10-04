# ============================================================
#  run_scanner.py — Agendador alinhado ao fechamento dos candles
#  Execute: python run_scanner.py
#  15m a cada 15 min; 1h a cada hora; 4h às 00/04/08/12/16/20 UTC;
#  12h às 00 e 12 UTC (09:00 e 21:00 BRT). Ao reiniciar, só escaneia
#  o que ainda não foi feito para o candle mais recente.
# ============================================================
import asyncio
import time

import scanner
import settings
import store
from binance_client import BinanceClient


ULTIMO_RESULTADO = 0.0
INTERVALO_RESULTADO_S = 120      # mede o resultado dos gatilhos a cada 2 min


def _atualizar_resultados() -> None:
    """Preenche máxima favorável/adversa e os horizontes de 1/4/12/24 h dos
    gatilhos recentes. É tudo SQLite sobre a série de preços que o scan já grava,
    então não gasta requisição na Binance."""
    global ULTIMO_RESULTADO
    if time.time() - ULTIMO_RESULTADO < INTERVALO_RESULTADO_S:
        return
    ULTIMO_RESULTADO = time.time()
    try:
        n = store.calcular_resultados_sinais()
        if n:
            print(f"[Resultado] {n} gatilhos com resultado atualizado")
    except Exception as e:                      # não pode derrubar o agendador
        print(f"[Resultado] falha ao medir: {e!r}")


async def carregar_universo(client):
    lista = await client.universo()
    store.salvar_universo(lista)
    print(f"[Universo] {len(lista)} símbolos (vol 24h ≥ {settings.MIN_QUOTE_VOLUME_24H:,.0f} USDT)")
    return [s for s, _ in lista]


async def main():
    store.init()
    client = BinanceClient()
    try:
        await _com_retry(client.sincronizar_relogio, "relógio")
        symbols = await _com_retry(lambda: carregar_universo(client), "universo")
        ultimo_universo = time.time()
        ultimo_relogio = time.time()
        print("[Run] Ativo. Ctrl+C para parar.")
        while True:
            try:
                if time.time() - ultimo_universo > settings.UNIVERSE_REFRESH_S:
                    symbols = await carregar_universo(client)
                    ultimo_universo = time.time()
                if time.time() - ultimo_relogio > 3600:
                    await client.sincronizar_relogio()
                    ultimo_relogio = time.time()

                for tf in settings.SCAN_ORDER:
                    seg = settings.TIMEFRAMES[tf]["seconds"]
                    agora = client.agora()
                    boundary = int(agora // seg) * seg          # último fechamento (epoch UTC)
                    if agora < boundary + settings.CLOSE_DELAY_S:
                        continue
                    if store.boundary_do_tf(tf) >= boundary:
                        continue
                    await scanner.escanear_tf(client, tf, symbols, boundary)
                _atualizar_resultados()
            except (KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as e:      # rede, SQLite travado etc.: registra e segue vivo
                print(f"[Run] erro no ciclo: {e!r} — tentando de novo em {settings.RUN_RETRY_S}s")
                await asyncio.sleep(settings.RUN_RETRY_S)
            await asyncio.sleep(2)
    finally:
        await client.close()


async def _com_retry(fn, nome):
    """Partida: insiste até funcionar (rede fora do ar no boot não deve matar o processo)."""
    while True:
        try:
            return await fn()
        except Exception as e:
            print(f"[Run] falha ao obter {nome}: {e!r} — tentando de novo em {settings.RUN_RETRY_S}s")
            await asyncio.sleep(settings.RUN_RETRY_S)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Run] Encerrado.")
