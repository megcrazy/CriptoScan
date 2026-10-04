# ============================================================
#  scanner.py — Camada lenta: recalcula um timeframe quando ele fecha
# ============================================================
import asyncio
import sys
import time

import pandas as pd

import cci_div
import fib_extension as fibo
import fusion
import settings
import store
from telegram_notifier import TelegramNotifier


if hasattr(sys.stdout, "reconfigure"):
    # Não deixa o encoding do console Windows (charmap/cp1252) derrubar o scan.
    sys.stdout.reconfigure(errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(errors="replace")


_telegram = TelegramNotifier()


def klines_para_df(raw: list, agora_ms: float | None = None) -> pd.DataFrame:
    """Converte klines da Binance em DataFrame só com candles FECHADOS."""
    agora_ms = agora_ms if agora_ms is not None else time.time() * 1000
    linhas = [k for k in raw if k[6] < agora_ms]          # k[6] = closeTime
    df = pd.DataFrame(linhas, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_buy_volume", "taker_buy_quote", "ignore",
    ])
    for col in ("open", "high", "low", "close", "volume", "taker_buy_volume"):
        df[col] = df[col].astype(float)
    df["time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df[["time", "open", "high", "low", "close", "volume", "taker_buy_volume"]].reset_index(drop=True)


def _calcular(df: pd.DataFrame, tf: str):
    tfcfg = settings.TIMEFRAMES[tf]
    divs = cci_div.analisar_divergencias(df, settings.div_cfg(tf)) if tfcfg["div"] else []
    zona = None
    ext = None
    if tfcfg["fib"]:
        ext = fibo.detectar_extensao(df, casas=None)   # cripto: sem arredondar
        if ext:
            zona = {k: ext[k] for k in (
                "direcao", "a", "b", "c", "impulso", "zona_fundo", "zona_topo",
                "alvo_1000", "alvo_1618", "pattern_id", "idade_c")}
            zona["c_time"] = int(ext["c_time"].timestamp() * 1000)
    return divs, zona, ext


async def analisar_simbolo(client, symbol: str, tf: str):
    tarefas = [client.klines(symbol, tf)]
    tem_oi = hasattr(client, "open_interest")
    if tem_oi:
        tarefas.append(client.open_interest(symbol))
    respostas = await asyncio.gather(*tarefas, return_exceptions=True)
    raw = respostas[0]
    if isinstance(raw, Exception):
        raise raw
    oi_raw = respostas[1] if tem_oi else None
    if isinstance(oi_raw, Exception):
        print(f"[OI {symbol}] indisponível neste ciclo: {oi_raw}")
        oi_raw = None
    df = klines_para_df(raw, client.agora() * 1000)
    if len(df) < 40:
        return symbol, [], None, None, oi_raw
    divs, zona, ext = await asyncio.to_thread(_calcular, df, tf)
    fusao = None
    # Roda mesmo quando `ext` é None: o fib ATIVO tem memória (fusion.fib_ativo),
    # igual ao Pine, que continua com a fib anterior quando o último triplo de
    # pivôs deixa de ser um ABC válido. Sem isso o ativo some dos alertas.
    if tf in settings.ALERT_TIMEFRAMES:
        try:
            fusao = await fusion.analisar(client, symbol, tf, df, ext)
        except Exception as e:                      # um símbolo ruim não derruba o scan
            print(f"[Fusion {symbol} {tf}] erro: {e}")
    if zona is None and fusao:
        # o padrão que vale é o ATIVO: o painel não pode divergir do alerta
        zona = {k: fusao.get(k) for k in (
            "direcao", "a", "b", "c", "impulso", "zona_fundo", "zona_topo",
            "alvo_1000", "alvo_1618", "pattern_id", "idade_c")}
        zona["c_time"] = fusao.get("c_ms")
    if fusao and fusao.get("lado"):
        # Cruzamento com o log de divergências, uma vez por símbolo (antes era uma
        # consulta por gatilho). Entra na mensagem do Telegram, na decisão da regra
        # de alerta e na gravação — os três usam o mesmo número.
        # O boundary é o horário de FECHAMENTO da última vela: abertura + duração.
        boundary = int(df["time"].iloc[-1].timestamp()) + settings.TIMEFRAMES[tf]["seconds"]
        fusao["div"] = store.cruzar_divergencia(
            {"symbol": symbol, "lado": fusao["lado"]}, boundary)
    # high/low da vela fechada: é o que permite medir máxima favorável/adversa
    # do gatilho depois, sem precisar baixar klines de novo
    vela = {"high": float(df["high"].iloc[-1]), "low": float(df["low"].iloc[-1]),
            "open": float(df["open"].iloc[-1])}
    return symbol, divs, zona, float(df["close"].iloc[-1]), oi_raw, fusao, vela


def _registrar_sinal(ev: dict, boundary_ts: int, cruz: dict, suprimido: int,
                     motivo: str | None) -> None:
    """Grava o gatilho (enviado ou suprimido). Falha aqui não pode impedir o
    alerta de sair nem travar a decisão de borda."""
    try:
        store.salvar_sinal(ev, boundary_ts, cruz, suprimido, motivo)
    except Exception as e:
        print(f"[Store] não gravei o sinal {ev.get('symbol')} {ev.get('tipo')}: {e}")


async def _enviar_alertas(tf: str, boundary_ts: int, resultados: list) -> int:
    """Decide por borda, aplica a regra de alerta, envia e só então marca como
    decidido (falha de rede = tenta de novo no próximo scan).

    Todo candidato entra na tabela `sinais`, com `suprimido`/`motivo` quando a
    regra corta. É o que permite conferir depois se o corte valeu a pena."""
    itens = [(r[0], r[5] if len(r) > 5 else None) for r in resultados]
    alertas = fusion.decidir_alertas(tf, boundary_ts, itens)
    limite = getattr(settings, "FUSION_MAX_ALERTAS_POR_SCAN", 25)
    enviados = sup = pendentes = 0
    for ev in alertas:
        try:
            # o cruzamento já veio calculado do analisar_simbolo (uma consulta por
            # símbolo); se faltar, calcula aqui para não perder o alerta
            cruz = ev.get("div") or store.cruzar_divergencia(ev, boundary_ts)
            enviar, motivo = fusion.avaliar_regra(ev, cruz)
            if not enviar:
                sup += 1                           # suprimido não conta no teto: não gasta envio
                fusion.marcar_enviado(ev)          # decidido: não repete no próximo scan
                _registrar_sinal(ev, boundary_ts, cruz, 1, motivo)
                continue
            if enviados >= limite:
                pendentes += 1                     # fica para o próximo scan (não marca como visto)
                continue
            if not _telegram.enabled:              # sem credenciais: só registra no console
                print(ev["texto"])
                fusion.marcar_enviado(ev)
                _registrar_sinal(ev, boundary_ts, cruz, 0, None)
                continue
            if await _telegram.send_text(ev["texto"], ev["silencioso"]):
                fusion.marcar_enviado(ev)
                _registrar_sinal(ev, boundary_ts, cruz, 0, None)
                enviados += 1
                print(f"[Telegram] {ev['symbol']} {tf} {ev['tipo']}")
            await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[Telegram] falha ao enviar {ev.get('symbol')}: {e}")
    if sup:
        print(f"[Regras] {sup} gatilho(s) {tf} suprimido(s) — gravados no histórico")
    if pendentes > 0 and _telegram.enabled:
        try:
            await _telegram.send_text(f"+{pendentes} alertas {tf} acima do limite desta rodada ({limite}).", True)
        except Exception as e:
            print(f"[Telegram] falha ao enviar resumo: {e}")
    fusion.salvar_estado()
    return enviados


async def escanear_tf(client, tf: str, symbols: list[str], boundary_ts: int):
    t0 = time.time()
    tarefas = [asyncio.create_task(analisar_simbolo(client, s, tf)) for s in symbols]
    resultados, erros = [], 0
    for i, fut in enumerate(asyncio.as_completed(tarefas), 1):
        try:
            resultados.append(await fut)
        except Exception as e:
            erros += 1
            print(f"[Scan {tf}] erro: {e}")
        if i % 50 == 0:
            print(f"[Scan {tf}] {i}/{len(symbols)}")
    n_div, n_zones = store.salvar_scan(tf, boundary_ts, resultados, erros)
    n_alertas = await _enviar_alertas(tf, boundary_ts, resultados)
    print(f"[Scan {tf}] ok em {time.time() - t0:.0f}s - {len(resultados)} simbolos, "
          f"{n_div} divergências, {n_zones} zonas, {n_alertas} alertas Telegram, {erros} erros")
