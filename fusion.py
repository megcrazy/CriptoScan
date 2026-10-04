# ============================================================
#  fusion.py — Titanium Fusion (Pine v2.1) no scanner cripto
#
#  O que replica do Pine:
#    Fibo ABC (zona 0,5–0,618) com o papel pelo LADO DE CHEGADA do preço,
#    sweep de liquidez da SESSÃO anterior, BUY/SELL após o sweep, FVG, volume,
#    OI, RSI Power (PWR + convergência) e o score 0–100 com o mesmo teto.
#  Diferenças que sobraram (conscientes):
#    - a janela do scanner é de 99 velas, o Pine usa até 1500 (max_bars_back);
#    - SW e BUY/SELL só avisam se a zona foi tocada nas últimas N velas
#      (FUSION_ARMADO_BARRAS) — o Pine plota sem esse filtro.
#
#  Fluxo em 2 estágios, para não pesar na Binance:
#    estágio 1 (barato, usa o df que o scanner já baixou): zona, lado, sweeps,
#               FVG, volume. Roda para todos os símbolos.
#    estágio 2 (só quem está NA zona): RSI multi-TF e OI.
#
#  O fib ATIVO tem memória (fib_ativo), como as variáveis activeFib* do Pine:
#    não é descartado quando o último triplo de pivôs deixa de ser um ABC válido;
#    só é trocado por um pattern_id novo e válido, e só é apagado quando um
#    fechamento confirmado rompe o C (a partir da barra seguinte à confirmação).
#
#  Alertas (por borda, uma vez por evento):
#    SETUP  -> score >= mínimo dentro da zona (COMPRA/VENDA); uma vez por PADRÃO,
#              igual ao scoreSignalNew (virar de lado não redispara)
#    SWEEP  -> sweep alinhado ao lado da zona (compra = sweep de MÍNIMA; venda = de MÁXIMA)
#    BUY/SELL -> fechamento além do extremo do sweep oposto (como os marcadores do Pine)
# ============================================================
from __future__ import annotations

import asyncio
import html
import json
import os
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

import fib_extension as fibo
import fib_papel

try:
    import settings
except Exception:          # permite testar o módulo isolado
    settings = None


def _c(nome: str, padrao):
    return getattr(settings, nome, padrao) if settings is not None else padrao


_PESOS_PADRAO = {"fib": 20.0, "sweep": 25.0, "oi": 20.0, "vol": 10.0, "rsi": 10.0, "conv": 5.0, "fvg": 10.0}
_RSI_TFS_PADRAO = ["1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"]
_MIN_POR_TF = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120,
               "4h": 240, "6h": 360, "12h": 720, "1d": 1440}
_TV_INTERVALO = {"15m": "15", "1h": "60", "4h": "240"}
_DIA_MS = 86_400_000


def _pesos() -> dict:
    return {**_PESOS_PADRAO, **_c("FUSION_PESOS", {})}


def _py(x):
    """numpy -> tipos nativos (o resultado vai para JSON/SQLite)."""
    if isinstance(x, (np.bool_,)):
        return bool(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating,)):
        return float(x)
    return x


def _ms(ts: pd.Timestamp) -> int:
    return int(pd.Timestamp(ts).timestamp() * 1000)


# ============================================================
#  Liquidez: dia UTC anterior (padrão) ou a sessão anterior (como o Pine)
# ============================================================
_cache_diario: dict[str, dict] = {}

# Sessões do Fusion_v2.pine (UTC): Ásia 00-06, Londres 07-10, NY 13-16.
_SESSOES = (("ASIA", 0, 6), ("LONDRES", 7, 10), ("NY", 13, 16))


async def niveis_diarios(client, symbol: str) -> dict | None:
    """{dia_utc (epoch//86400): (máxima, mínima)} só de dias FECHADOS. Uma chamada por símbolo por dia."""
    hoje = int(client.agora() // 86400)
    c = _cache_diario.get(symbol)
    if c and c["dia"] == hoje:
        return c["niveis"]
    try:
        raw = await client.klines(symbol, "1d", _c("FUSION_DIARIOS_LIMIT", 30))
        agora_ms = client.agora() * 1000
        # O dict tem de ficar DENTRO do try: um payload de 1d estranho derrubava
        # a fusão do símbolo inteiro em vez de só ficar sem nível diário.
        niveis = {int(k[0]) // _DIA_MS: (float(k[2]), float(k[3])) for k in raw if k[6] < agora_ms}
    except Exception as e:
        print(f"[Fusion] níveis diários de {symbol} indisponíveis: {e}")
        return c["niveis"] if c else None
    _cache_diario[symbol] = {"dia": hoje, "niveis": niveis}
    return niveis


def alvos_sessao(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """targetHigh/targetLow do Pine (Fusion_v2.pine:289-321): a sessão seguinte
    observa o range da sessão anterior. Fora de sessão o alvo não muda."""
    horas = df["time"].dt.hour.to_numpy()
    H, L = df["high"].to_numpy(float), df["low"].to_numpy(float)
    faixa = np.full(len(df), -1)
    for k, (_, ini, fim) in enumerate(_SESSOES):
        faixa[(horas >= ini) & (horas < fim)] = k
    alvo_h = np.full(len(df), np.nan)
    alvo_l = np.full(len(df), np.nan)
    anterior = {k: [np.nan, np.nan] for k in range(len(_SESSOES))}
    atual = [np.nan, np.nan]
    cur = -1
    for i in range(len(df)):
        f = int(faixa[i])
        if f != cur:
            if cur >= 0:
                anterior[cur] = [atual[0], atual[1]]
            cur = f
            atual = [H[i], L[i]]
            if f >= 0:                                  # início de sessão: alvo = sessão anterior
                fonte = (f - 1) % len(_SESSOES)
                alvo_h[i], alvo_l[i] = anterior[fonte]
        elif f >= 0:
            atual[0] = max(atual[0], H[i])
            atual[1] = min(atual[1], L[i])
        if i > 0 and np.isnan(alvo_h[i]):               # o alvo vale até a próxima troca
            alvo_h[i], alvo_l[i] = alvo_h[i - 1], alvo_l[i - 1]
    return alvo_h, alvo_l


def alvos_liquidez(df: pd.DataFrame, niveis: dict | None) -> tuple[np.ndarray, np.ndarray]:
    """(alvo_high, alvo_low) por vela, no modo de FUSION_LIQUIDEZ."""
    if _c("FUSION_LIQUIDEZ", "dia") == "sessao":
        return alvos_sessao(df)
    n = len(df)
    alvo_h = np.full(n, np.nan)
    alvo_l = np.full(n, np.nan)
    if not niveis:
        return alvo_h, alvo_l
    dias = (df["time"].map(_ms).to_numpy() // _DIA_MS).astype(np.int64)
    for i in range(n):
        par = niveis.get(int(dias[i]) - 1)
        if par:
            alvo_h[i], alvo_l[i] = par
    return alvo_h, alvo_l


# ============================================================
#  Fibo ativo: com a memória que o Pine tem (Fusion_v2.pine:147-160)
# ============================================================
def _num(x) -> float | None:
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else float(x)


def _ext_valido(ext: dict | None) -> dict | None:
    """Um ABC recém-detectado que o Pine aceitaria como fib NOVO."""
    if not ext:
        return None
    if _c("FUSION_EXIGIR_C_DENTRO", True):
        if ext["direcao"] == "alta" and not ext["c"] > ext["a"]:
            return None
        if ext["direcao"] == "baixa" and not ext["c"] < ext["a"]:
            return None
    return ext


def _snapshot(ext: dict, df: pd.DataFrame) -> dict:
    """O que o Pine guarda em activeFib* quando o padrão vira ativo."""
    idade = int(ext["idade_c"])
    idx_c = max(len(df) - 1 - idade, 0)
    # O Pine ignora a barra em que o padrão virou ativo ("not fibPatternChangedEff"):
    # a vigia do rompimento do C começa na barra SEGUINTE à confirmação do pivô.
    idx_check = min(idx_c + fibo.PIVOT_RIGHT, len(df) - 1)
    tem_tempo = "time" in df.columns
    return {
        "pattern_id": str(ext["pattern_id"]), "direcao": ext["direcao"],
        "a": ext["a"], "b": ext["b"], "c": ext["c"], "impulso": ext.get("impulso"),
        "zona_fundo": ext["zona_fundo"], "zona_topo": ext["zona_topo"],
        "alvo_1000": ext["alvo_1000"], "alvo_1618": ext["alvo_1618"],
        "idade_c": idade,
        "c_ms": int(df["time"].iloc[idx_c].timestamp() * 1000) if tem_tempo else None,
        "checado_ms": int(df["time"].iloc[idx_check].timestamp() * 1000) if tem_tempo else None,
    }


def fib_ativo(symbol: str, tf: str, df: pd.DataFrame, ext: dict | None) -> dict | None:
    """Mantém o padrão ABC ativo por symbol:tf, como o Pine:

    - troca o fib quando aparece um pattern_id NOVO e válido;
    - apaga o fib SÓ quando um fechamento confirmado rompe o C, e só a partir da
      barra seguinte à confirmação do pivô C (Fusion_v2.pine:157-160).

    Sem isso o detector é "sem memória": assim que o último triplo de pivôs deixa
    de ser um ABC válido, `detectar_extensao` devolve None e o ativo some dos
    alertas, enquanto o Pine continua com a fib anterior desenhada e alertando.
    """
    chave = f"{symbol}:{tf}"
    with _lock:
        est = _carregar()
        ativo = est.setdefault("fib", {}).get(chave)

    novo = _ext_valido(ext)
    if novo is not None and (ativo is None or str(novo["pattern_id"]) != ativo.get("pattern_id")):
        ativo = _snapshot(novo, df)
    if ativo is None:
        return None

    ativo = dict(ativo)                       # nunca mutar o objeto que está no estado
    tem_tempo = "time" in df.columns
    if tem_tempo:
        ms = df["time"].map(_ms).to_numpy()
        if ativo.get("c_ms") is not None:
            pos = np.flatnonzero(ms == int(ativo["c_ms"]))
            if pos.size:
                ativo["idade_c"] = int(len(df) - 1 - pos[-1])
            else:                             # C saiu da janela: envelhece pelo relógio
                ativo["idade_c"] = int(ativo.get("idade_c", fibo.PIVOT_RIGHT)) + 1
    if int(ativo.get("idade_c", 0)) > int(_c("FUSION_FIB_IDADE_MAX", 93)):
        with _lock:
            _carregar().setdefault("fib", {}).pop(chave, None)
        return None

    if _c("FUSION_INVALIDAR_NO_C", True) and tem_tempo:
        depois = df["close"].to_numpy(float)[ms > int(ativo.get("checado_ms") or 0)]
        if len(depois):
            rompeu = ((depois < ativo["c"]).any() if ativo["direcao"] == "alta"
                      else (depois > ativo["c"]).any())
            if rompeu:
                with _lock:
                    _carregar().setdefault("fib", {}).pop(chave, None)
                return None
            ativo["checado_ms"] = int(ms[-1])

    with _lock:
        _carregar().setdefault("fib", {})[chave] = ativo
    return ativo


def lado_chegada(df: pd.DataFrame, zona: dict) -> int:
    """+1 = chegou por cima (SUPORTE, compra); -1 = chegou por baixo (RESISTÊNCIA, venda); 0 = indefinido.
    Igual ao fibZoneSide do Pine: último fechamento FORA da zona desde a confirmação do padrão."""
    ci = len(df) - 1 - int(zona["idade_c"])
    ini = max(ci + fibo.PIVOT_RIGHT, 0)
    fechos = df["close"].to_numpy(float)
    lado = 0
    for i in range(ini, len(df)):
        if fechos[i] > zona["zona_topo"]:
            lado = 1
        elif fechos[i] < zona["zona_fundo"]:
            lado = -1
    return lado


# ============================================================
#  Sweep + BUY/SELL (máquina de estados do Pine, sobre o df)
# ============================================================
def razao_volume(df: pd.DataFrame) -> float | None:
    n = _c("FUSION_VOL_LEN", 50)
    v = df["volume"].to_numpy(float)
    if len(v) < n + 1:
        return None
    media = v[-(n + 1):-1].mean()
    return float(v[-1] / media) if media > 0 else None


def analisar_sweeps(df: pd.DataFrame, niveis: dict | None) -> dict | None:
    """Reproduz sweepHigh/sweepLow, validade e BUY/SELL do Pine.
    O alvo vem de `alvos_liquidez` (dia UTC anterior ou sessão anterior)."""
    if not niveis and _c("FUSION_LIQUIDEZ", "dia") != "sessao":
        return None
    n = len(df)
    H, L, C, V = (df[c].to_numpy(float) for c in ("high", "low", "close", "volume"))
    alvo_h, alvo_l = alvos_liquidez(df, niveis)
    media_prev = pd.Series(V).rolling(_c("FUSION_VOL_LEN", 50)).mean().shift(1).to_numpy()
    mult = _c("FUSION_SWEEP_VOL_MULT", 1.5)
    validade = _c("FUSION_SWEEP_VALIDADE", 30)
    ult_h = ult_l = None
    i_h = i_l = None
    eventos: list[tuple[int, str]] = []
    for i in range(n):
        vol_ok = (not np.isnan(media_prev[i])) and media_prev[i] > 0 and V[i] / media_prev[i] >= mult
        if not np.isnan(alvo_h[i]) and vol_ok and H[i] > alvo_h[i] and C[i] < alvo_h[i]:
            ult_h, i_h = H[i], i
            eventos.append((i, "sweep_high"))
        if not np.isnan(alvo_l[i]) and vol_ok and L[i] < alvo_l[i] and C[i] > alvo_l[i]:
            ult_l, i_l = L[i], i
            eventos.append((i, "sweep_low"))
        if ult_h is not None and i - i_h > validade:
            ult_h = None
        if ult_l is not None and i - i_l > validade:
            ult_l = None
        if ult_h is not None and i > i_h and C[i] > ult_h:       # sweep de máxima + fechamento acima = BUY
            eventos.append((i, "buy"))
            ult_h = None
        if ult_l is not None and i > i_l and C[i] < ult_l:       # sweep de mínima + fechamento abaixo = SELL
            eventos.append((i, "sell"))
            ult_l = None
    janela = _c("FUSION_SWEEP_JANELA", 0)

    def recente(tipo):
        return any(idx >= n - 1 - janela and tp == tipo for idx, tp in eventos)

    return {"alvo_high": _num(alvo_h[-1]), "alvo_low": _num(alvo_l[-1]),
            "sweep_high": recente("sweep_high"), "sweep_low": recente("sweep_low"),
            "buy": recente("buy"), "sell": recente("sell")}


# ============================================================
#  FVG (lista de gaps não preenchidos, como no Pine)
# ============================================================
def listar_fvgs(df: pd.DataFrame) -> list[dict]:
    H, L = df["high"].to_numpy(float), df["low"].to_numpy(float)
    atr = fibo._atr(df).to_numpy(float)
    mult = _c("FUSION_FVG_ATR_MIN", 0.3)
    maximo = _c("FUSION_FVG_MAX", 20)
    lista: list[dict] = []
    for i in range(2, len(df)):
        a = atr[i]
        if not np.isnan(a):
            if L[i] > H[i - 2] and (L[i] - H[i - 2]) >= a * mult:
                lista.append({"top": float(L[i]), "bottom": float(H[i - 2]), "dir": 1})
            if H[i] < L[i - 2] and (L[i - 2] - H[i]) >= a * mult:
                lista.append({"top": float(L[i - 2]), "bottom": float(H[i]), "dir": -1})
        while len(lista) > maximo:
            lista.pop(0)
        lista = [f for f in lista
                 if not ((L[i] <= f["bottom"]) if f["dir"] == 1 else (H[i] >= f["top"]))]
    return lista


# ============================================================
#  Estágio 1 (síncrono, barato)
# ============================================================
def estagio1(df: pd.DataFrame, tf: str, zona: dict | None, niveis: dict | None) -> dict | None:
    # `zona` já vem do fib_ativo (com a memória do Pine); não revalidar o ext cru
    # aqui, senão a zona ativa seria descartada de novo.
    if zona is None:
        return None
    preco = float(df["close"].iloc[-1])
    ultimo = df.iloc[-1]
    atr_s = fibo._atr(df)
    atr = float(atr_s.iloc[-1]) if not np.isnan(atr_s.iloc[-1]) else None
    papel = fib_papel.papel_zona(zona, preco)
    lado = lado_chegada(df, zona)

    fundo, topo = zona["zona_fundo"], zona["zona_topo"]
    em_zona = bool(ultimo["low"] <= topo and ultimo["high"] >= fundo)
    k = int(_c("FUSION_ARMADO_BARRAS", 12))
    rec = df.iloc[-k:]
    armado = bool(((rec["low"] <= topo) & (rec["high"] >= fundo)).any())

    sw = analisar_sweeps(df, niveis)
    sweep_alinhado = None
    breakout_alinhado = None
    if sw is not None:
        sweep_alinhado = bool((lado == 1 and sw["sweep_low"]) or (lado == -1 and sw["sweep_high"]))
        breakout_alinhado = bool((lado == 1 and sw["buy"]) or (lado == -1 and sw["sell"]))

    ratio = razao_volume(df)
    vol_alto = bool(ratio is not None and ratio >= _c("FUSION_VOL_MULT", 1.5))
    sentido_fvg = lado
    fvg = bool(sentido_fvg != 0 and any(
        f["top"] >= fundo and f["bottom"] <= topo and f["dir"] == sentido_fvg for f in listar_fvgs(df)))

    dist_atr = (papel["dist_pct"] / 100 * preco / atr) if atr else None
    est = {
        "tf": tf, "pattern_id": str(zona["pattern_id"]), "direcao": zona["direcao"],
        "a": zona["a"], "b": zona["b"], "c": zona["c"],
        "impulso": zona.get("impulso"), "c_ms": zona.get("c_ms"),
        "idade_c": int(zona.get("idade_c", fibo.PIVOT_RIGHT)),
        "zona_fundo": fundo, "zona_topo": topo,
        "alvo_1000": zona["alvo_1000"], "alvo_1618": zona["alvo_1618"],
        "preco": preco, "atr": atr, "lado": int(lado), "papel_atual": papel["papel"],
        "dist_pct": papel["dist_pct"], "dist_atr": dist_atr,
        "em_zona": em_zona, "armado": armado,
        "nivel_max_dia": sw["alvo_high"] if sw else None, "nivel_min_dia": sw["alvo_low"] if sw else None,
        "sweep_high": sw["sweep_high"] if sw else None, "sweep_low": sw["sweep_low"] if sw else None,
        "buy": sw["buy"] if sw else None, "sell": sw["sell"] if sw else None,
        "sweep_alinhado": sweep_alinhado, "breakout_alinhado": breakout_alinhado,
        "vol_ratio": ratio, "vol_alto": vol_alto, "fvg": fvg,
        "t_ref": _ms(ultimo["time"]), "rsi": None, "oi": None, "completo": False,
    }
    return {k: _py(v) for k, v in est.items()}


# ============================================================
#  Estágio 2 (assíncrono): RSI multi-TF e OI — só para quem está na zona
# ============================================================
async def rsi_multi_tf(client, symbol: str, tf: str, df: pd.DataFrame) -> dict | None:
    per = _c("FUSION_RSI_LEN", 14)
    tfs = list(_c("FUSION_RSI_TFS", _RSI_TFS_PADRAO))
    lim = _c("FUSION_RSI_KLINES", 99)

    async def um(tf_i: str):
        if tf_i == tf:
            return tf_i, float(fibo._rsi(df, per).iloc[-1])
        raw = await client.klines(symbol, tf_i, lim)          # inclui a vela em formação, como o Pine ao vivo
        fechos = pd.DataFrame({"close": [float(k[4]) for k in raw]})
        return tf_i, float(fibo._rsi(fechos, per).iloc[-1])

    res = await asyncio.gather(*[um(t) for t in tfs], return_exceptions=True)
    valores = {}
    for r in res:
        if isinstance(r, Exception):
            continue
        t, v = r
        if not np.isnan(v):
            valores[t] = round(v, 2)
    if len(valores) < _c("FUSION_RSI_MIN_TFS", 5):
        return None
    arr = np.array(list(valores.values()))
    power = float(arr.mean())
    disp = float(arr.std())                                    # população, igual ao array.stdev do Pine
    return {"valores": valores, "power": power, "disp": disp,
            "bull": power >= _c("FUSION_RSI_BULL", 55), "bear": power <= _c("FUSION_RSI_BEAR", 45),
            "conv_boa": disp <= _c("FUSION_CONV_MODERADA", 18.0)}


def alinhar_oi(hist: list, df: pd.DataFrame, tf: str, n_velas: int) -> list[float] | None:
    """OI de cada uma das últimas velas fechadas, pelo timestamp mais próximo."""
    if not hist:
        return None
    per_ms = _MIN_POR_TF[tf] * 60_000
    ts = np.array([int(h["timestamp"]) for h in hist], dtype=np.int64)
    oi = np.array([float(h["sumOpenInterest"]) for h in hist])
    ordem = np.argsort(ts)
    ts, oi = ts[ordem], oi[ordem]
    n = min(len(df), n_velas)
    desloc = per_ms if _c("FUSION_OI_REF", "abertura") == "fechamento" else 0
    saida = []
    for t in df["time"].iloc[-n:].map(_ms).to_numpy():
        alvo = int(t) + desloc
        j = int(np.argmin(np.abs(ts - alvo)))
        if abs(int(ts[j]) - alvo) > per_ms * 0.6:
            return None
        saida.append(float(oi[j]))
    return saida


def contexto_oi(oi_vals: list[float], fechos: np.ndarray) -> dict:
    n = _c("FUSION_OI_LEN", 20)
    # "sem_historico" distingue "o ativo não tem OI" (o Pine tira o peso do teto)
    # de "o dado não veio/desalinhou" (o peso FICA no teto, senão o score infla).
    indef = {"disponivel": False, "contexto": "OI INDISP.", "sem_historico": False}
    if len(oi_vals) < 3 or len(fechos) < 2:
        return indef
    diffs = np.diff(np.asarray(oi_vals, float))
    atual = diffs[-1]
    prev = np.abs(diffs[:-1])[-n:]
    if len(prev) < n:
        return indef
    limiar = max(prev.mean() * _c("FUSION_OI_MULT", 2.5), 0.0)
    ext_up, ext_dn = atual > limiar, atual < -limiar
    sobe, desce = fechos[-1] > fechos[-2], fechos[-1] < fechos[-2]
    long_build, short_build = bool(ext_up and sobe), bool(ext_up and desce)
    long_liq, short_cover = bool(ext_dn and desce), bool(ext_dn and sobe)
    ctx = ("LONG LIQ PROVÁVEL" if long_liq else "SHORT COVER" if short_cover else "LONG BUILD" if long_build
           else "SHORT BUILD" if short_build else "OI SUBINDO" if ext_up else "OI CAINDO" if ext_dn else "OI NEUTRO")
    return {"disponivel": True, "contexto": ctx, "long_build": long_build, "short_build": short_build,
            "long_liq": long_liq, "short_cover": short_cover, "sem_historico": False}


async def oi_do_simbolo(client, symbol: str, tf: str, df: pd.DataFrame) -> dict:
    sem_oi = {"disponivel": False, "contexto": "OI INDISP.", "sem_historico": True}
    indef = {"disponivel": False, "contexto": "OI INDISP.", "sem_historico": False}
    if not hasattr(client, "open_interest_hist"):
        return sem_oi
    n = _c("FUSION_OI_LEN", 20)
    try:
        hist = await client.open_interest_hist(symbol, tf, n + 8)
    except Exception as e:
        print(f"[Fusion] OI de {symbol} {tf} falhou (não é 'ativo sem OI'): {e}")
        return indef
    if not hist:
        return sem_oi                      # a Binance não tem histórico de OI para este ativo
    vals = alinhar_oi(hist, df, tf, n + 3)
    if vals is None:
        return indef
    return contexto_oi(vals, df["close"].to_numpy(float)[-len(vals):])


# ============================================================
#  Score (Pesos e normalização do Pine v2.1)
# ============================================================
def aplicar_score(est: dict) -> dict:
    lado, pesos = est["lado"], _pesos()
    oi, rsi = est.get("oi"), est.get("rsi")
    oi_ok = bool(isinstance(oi, dict) and oi.get("disponivel"))
    comp = {
        "fib": bool(est["em_zona"] and lado != 0),
        "sweep": bool(est["sweep_alinhado"]),        # None (sem nível) = não pontuou
        "oi": None if not oi_ok else bool((lado == 1 and (oi["long_liq"] or oi["long_build"]))
                                          or (lado == -1 and (oi["short_cover"] or oi["short_build"]))),
        "vol": bool(est["vol_alto"]),
        "rsi": None if not rsi else bool((lado == 1 and rsi["bull"]) or (lado == -1 and rsi["bear"])),
        "conv": None if not rsi else bool(rsi["conv_boa"]),
        "fvg": bool(est["fvg"]),
    }
    # Fusion_v2.pine:544 -> scoreMaxAvail = fib + sweep + (oiAvailable ? oi : 0)
    #                                  + vol + rsi + conv + fvg
    # O teto só perde o peso do OI quando o ativo COMPROVADAMENTE não tem OI.
    # Qualquer outro componente sem dado conta como "não pontuou", mas o peso
    # continua no denominador — senão o score infla sozinho (falha de rede,
    # rate limit, ativo novo sem 30 diários) e o mínimo de 60 deixa de valer.
    oi_fora_do_teto = bool(isinstance(oi, dict) and oi.get("sem_historico"))
    maximo = (pesos["fib"] + pesos["sweep"] + (0.0 if oi_fora_do_teto else pesos["oi"])
              + pesos["vol"] + pesos["rsi"] + pesos["conv"] + pesos["fvg"])
    bruto = sum(pesos[k] for k, v in comp.items() if v)
    score = min(bruto / maximo * 100, 100) if maximo > 0 else 0.0
    minimo = _c("FUSION_SCORE_MIN", 60.0)
    est["comp"] = comp
    est["score"] = round(score, 1)
    est["score_min"] = minimo
    est["sinal"] = bool(est["completo"] and lado != 0 and est["em_zona"] and score >= minimo)
    return est


async def analisar(client, symbol: str, tf: str, df: pd.DataFrame, ext: dict | None) -> dict | None:
    """Ponto de entrada: devolve o dict da fusão (JSON-safe) ou None se não há fib ativo.

    `ext` é o ABC recém-detectado. O padrão que vale é o fib ATIVO (fib_ativo), que
    sobrevive a uma vela em que o último triplo de pivôs deixa de ser um ABC válido —
    exatamente como o Pine faz.
    """
    zona = await asyncio.to_thread(fib_ativo, symbol, tf, df, ext)
    if zona is None:
        return None
    niveis = await niveis_diarios(client, symbol)
    est = await asyncio.to_thread(estagio1, df, tf, zona, niveis)
    if est is None:
        return None
    if est["em_zona"] and est["lado"] != 0:
        rsi, oi = await asyncio.gather(rsi_multi_tf(client, symbol, tf, df),
                                       oi_do_simbolo(client, symbol, tf, df), return_exceptions=True)
        est["rsi"] = None if isinstance(rsi, Exception) else rsi
        est["oi"] = None if isinstance(oi, Exception) else oi
        # "completo" = o estágio 2 rodou E trouxe dado. Marcar True com os dois
        # estágios vazios fazia o score passar como se estivesse conferido.
        est["completo"] = bool(est["rsi"] or (est["oi"] and est["oi"].get("disponivel")))
    return aplicar_score(est)


# ============================================================
#  Referência de stop/alvo por ATR (informativo)
# ============================================================
def referencia_risco(f: dict) -> dict | None:
    atr, preco, lado = f.get("atr"), f["preco"], f["lado"]
    if not atr or lado == 0:
        return None
    k = _c("FUSION_STOP_ATR", 0.5)
    stop = f["zona_fundo"] - k * atr if lado == 1 else f["zona_topo"] + k * atr
    niveis = [f["alvo_1000"], f["alvo_1618"], f["a"], f["b"], f["c"]]
    if lado == 1:
        alvos = sorted(x for x in niveis if x > preco + atr)[:2]
    else:
        alvos = sorted((x for x in niveis if x < preco - atr), reverse=True)[:2]
    risco = abs(preco - stop)
    rr = abs(alvos[0] - preco) / risco if alvos and risco > 0 else None
    return {"stop": stop, "alvos": alvos, "rr": rr}


# ============================================================
#  Alertas: decisão por borda, estado persistente, mensagem
# ============================================================
_lock = threading.Lock()
_ARQ = Path(os.getenv("FUSION_STATE_PATH", str(_c("FUSION_STATE_PATH", "fusion_state.json"))))
_estado: dict | None = None


def _carregar() -> dict:
    global _estado
    if _estado is None:
        try:
            _estado = json.loads(_ARQ.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            _estado = {}
        _estado.setdefault("seen", {})
        _estado.setdefault("tf_ultimo", {})
        _estado.setdefault("fib", {})         # fib ATIVO por symbol:tf (memória do Pine)
    return _estado


def salvar_estado() -> None:
    with _lock:
        est = _carregar()
        corte = time.time() - _c("FUSION_SEEN_DIAS", 7) * 86400
        est["seen"] = {k: v for k, v in est["seen"].items() if v >= corte}
        _ARQ.parent.mkdir(parents=True, exist_ok=True)
        tmp = _ARQ.with_name(f".{_ARQ.stem}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(est, ensure_ascii=True), encoding="utf-8", newline="\n")
        for tentativa in range(6):
            try:
                os.replace(tmp, _ARQ)
                return
            except PermissionError:                           # antivírus/OneDrive no Windows
                if tentativa == 5:
                    print(f"[Fusion] aviso: estado ocupado; tentará de novo no próximo scan ({_ARQ})")
                    return
                time.sleep(0.05 * (tentativa + 1))


def marcar_enviado(ev: dict) -> None:
    with _lock:
        _carregar()["seen"][ev["chave"]] = int(time.time())


def _norm_chave(k) -> tuple:
    """Aceita ('setup','15m',1), 'setup:15m:1', '*', ('sweep',)… sempre vira 3-tupla.

    Config tolerante de propósito: uma chave mal escrita que não casa em silêncio é
    o mesmo tipo de armadilha que já custou caro neste projeto (o `api2.py`)."""
    if isinstance(k, str):
        partes: list = [p.strip() for p in k.split(":")] if ":" in k else [k.strip()]
    else:
        partes = list(k)
    partes = (partes + ["*", "*", "*"])[:3]
    out = []
    for p in partes:
        if p is None or (isinstance(p, str) and p.strip() in ("", "*")):
            out.append("*")
        elif isinstance(p, str) and p.strip().lstrip("+-").isdigit():
            out.append(int(p.strip()))
        else:
            out.append(p)
    return tuple(out)


def _regra_para(tipo: str, tf: str, lado: int) -> dict:
    """Junta as regras da menos para a mais específica (a específica vence campo a campo)."""
    regras = _c("FUSION_REGRAS", {}) or {}
    if not regras:
        return {}
    norm = {_norm_chave(k): v for k, v in regras.items() if v}
    chaves = (("*", "*", "*"), (tipo, "*", "*"), (tipo, tf, "*"),
              ("*", tf, "*"), ("*", "*", lado), (tipo, "*", lado),
              ("*", tf, lado), (tipo, tf, lado))
    out: dict = {}
    for k in chaves:
        r = norm.get(k)
        if r:
            out.update(r)
    return out


def dist_zona_pct(ev: dict) -> float | None:
    """Distância do preço de entrada até a borda mais próxima da zona, em %.

    Positivo = preço ACIMA da zona, negativo = abaixo, 0 = dentro. Existe porque o
    `inFibZone` do Pine testa se a VELA tocou a zona (`low <= topo and high >= fundo`),
    não se o preço está nela: numa vela que abre na zona e dispara, o alerta sai com o
    preço já longe. Caso real (AINUSDT 1h, 03/10): COMPRA a 63,9% acima da zona, com
    stop de ATR a 42% de distância e sem alvo nenhum (o preço já passou de todos)."""
    p, zf, zt = ev.get("preco"), ev.get("zona_fundo"), ev.get("zona_topo")
    if not p or not zf or not zt:
        return None
    if p > zt:
        return (p / zt - 1) * 100
    if p < zf:
        return -(1 - p / zf) * 100
    return 0.0


def avaliar_regra(ev: dict, cruzamento: dict | None = None) -> tuple[bool, str | None]:
    """(enviar?, motivo da supressão). Sem regra configurada, envia tudo.

    `ev` é o evento do gatilho (tipo/tf/lado/score) e `cruzamento` é o resultado do
    join com o log de divergências (`store.cruzar_divergencia`), que traz
    `div_tf`/`n_divs_contra`. O veredito vai para a tabela `sinais` mesmo quando
    suprime — o Placar precisa dos dois lados para dizer se o corte valeu.
    """
    cruz = cruzamento or {}
    r = _regra_para(str(ev.get("tipo", "")), str(ev.get("tf", "")), int(ev.get("lado") or 0))
    if not r:
        return True, None
    if r.get("bloquear"):
        return False, "regra:bloqueado"
    score = ev.get("score")
    if r.get("min_score") is not None and (score is None or score < float(r["min_score"])):
        return False, f"regra:score<{float(r['min_score']):g}"
    if r.get("sem_conflito") and (cruz.get("n_divs_contra") or 0) > 0:
        return False, "regra:div_contraria"
    if r.get("sem_conflito_predominante") and \
            (cruz.get("n_divs_contra") or 0) > (cruz.get("n_divs_conf") or 0):
        # corte mais fino que `sem_conflito`: só cai quando as contrárias DOMINAM.
        # É o que os dados mostraram — ter alguma contrária é comum e não separa,
        # ter mais contrárias que a favor separa (excesso -1,3% contra +0,1%).
        return False, "regra:div_contra_predomina"
    if r.get("exigir_div") and not cruz.get("div_tf"):
        return False, "regra:sem_div"
    if r.get("dist_zona_max") is not None:
        lim = float(r["dist_zona_max"])
        d = dist_zona_pct(ev)
        if d is None or abs(d) > lim:
            return False, f"regra:zona>{lim:g}%"
    return True, None


def _fmt(x) -> str:
    return "—" if x is None else f"{float(x):.6g}"


def _evento(tipo: str, symbol: str, tf: str, f: dict, chave: str) -> dict:
    lado = f["lado"]
    ev = {"tipo": tipo, "symbol": symbol, "tf": tf, "lado": lado, "chave": chave,
          "score": f.get("score"), "silencioso": tipo == "sweep" and _c("FUSION_SWEEP_SILENCIOSO", True),
          # contexto que o scanner grava na tabela `sinais` (auditoria do que foi
          # enviado). Sem isso só sobraria o texto da mensagem.
          "preco": f.get("preco"), "direcao": f.get("direcao"),
          "pattern_id": f.get("pattern_id"),
          "zona_fundo": f.get("zona_fundo"), "zona_topo": f.get("zona_topo"),
          # cruzamento com o log de divergências, calculado pelo scanner
          "div": f.get("div")}
    ev["texto"] = formatar_mensagem(ev, f)
    return ev


def decidir_alertas(tf: str, boundary_ts: int, itens: list[tuple[str, dict | None]]) -> list[dict]:
    """itens: [(symbol, fusao|None)] do scan. Partida a frio (primeiro scan ou depois de uma pausa
    longa) só 'semeia' o estado: nada é enviado como se fosse novo."""
    seg = settings.TIMEFRAMES[tf]["seconds"] if settings is not None else 900
    with _lock:
        est = _carregar()
        ultimo = est["tf_ultimo"].get(tf)
        fria = ultimo is None or boundary_ts - ultimo > _c("FUSION_GAP_CANDLES", 2) * seg
        est["tf_ultimo"][tf] = int(boundary_ts)
    candidatos = []
    for symbol, f in itens:
        if not f or f["lado"] == 0:
            continue
        # Igual ao scoreSignalNew do Pine: o sinal é por PADRÃO, não por lado.
        # scoreSignal não depende da direção, então virar de SUPORTE para
        # RESISTÊNCIA com o score ainda >= mínimo NÃO redispara. Com a chave
        # antiga (…:{pattern_id}:{lado}) o mesmo padrão mandava VENDA e COMPRA
        # em velas seguidas — foi o que aconteceu no DOGE em 2026-10-02.
        chave_setup = f"{symbol}:{tf}:setup:{f['pattern_id']}"
        if f["sinal"]:
            if chave_setup not in est["seen"]:
                candidatos.append(_evento("setup", symbol, tf, f, chave_setup))
        else:
            est["seen"].pop(chave_setup, None)      # sinal caiu -> rearma o padrão
        if f["armado"] and f["sweep_alinhado"]:
            candidatos.append(_evento("sweep", symbol, tf, f, f"{symbol}:{tf}:sweep:{f['t_ref']}:{f['lado']}"))
        if f["armado"] and f["breakout_alinhado"]:
            candidatos.append(_evento("breakout", symbol, tf, f, f"{symbol}:{tf}:breakout:{f['t_ref']}:{f['lado']}"))
    if fria:
        with _lock:
            for ev in candidatos:
                est["seen"][ev["chave"]] = int(time.time())
        return []
    novos = [ev for ev in candidatos if ev["chave"] not in est["seen"]]
    prioridade = {"setup": 0, "breakout": 1, "sweep": 2}
    novos.sort(key=lambda e: (prioridade[e["tipo"]], -(e["score"] or 0)))
    return novos


def _idade_curta(minutos: int | None) -> str:
    if minutos is None:
        return "—"
    if minutos < 60:
        return f"{minutos}min"
    if minutos < 1440:
        return f"{minutos // 60}h{minutos % 60:02d}"
    return f"{minutos // 1440}d{(minutos % 1440) // 60:02d}"


def _linha_divergencia(ev: dict) -> str:
    """O cruzamento com o log de divergências, do ponto de vista DESTE alerta.

    É a informação que amarra as duas ferramentas: um setup no suporte vale mais se
    tiver divergência bullish por trás, e vale menos se tiver uma bearish contra."""
    d = ev.get("div") or {}
    conf, contra = d.get("div_tf"), d.get("n_divs_contra") or 0
    lado_div = "alta" if ev["lado"] == 1 else "baixa"
    if conf:
        txt = f"<b>✔ {lado_div} {conf}</b>, nasceu {_idade_curta(d.get('div_idade_min'))} atrás"
        if contra:
            txt += f" · <b>⚠ {contra} contrária{'s' if contra > 1 else ''}</b>"
        if d.get("div_estado"):
            txt += f" ({d['div_estado']}"
            if d.get("div_forca") is not None:
                txt += f", força {d['div_forca']:.0f}"
            txt += ")"
        return "Divergência: " + txt
    if contra:
        return (f"Divergência: <b>⚠ {contra} contrária{'s' if contra > 1 else ''}</b> "
                f"ao lado deste alerta (nenhuma a favor)")
    return "Divergência: <b>✗ nenhuma</b> na janela"


def formatar_mensagem(ev: dict, f: dict) -> str:
    compra = ev["lado"] == 1
    icone = "🟢" if compra else "🔴"
    lado_txt = "COMPRA" if compra else "VENDA"
    titulo = {"setup": f"{lado_txt} (setup)", "sweep": f"SW — sweep de {'mínima' if compra else 'máxima'}",
              "breakout": "BUY" if compra else "SELL"}[ev["tipo"]]
    papel = "SUPORTE" if compra else "RESISTÊNCIA"
    # O mínimo de score é portão do SETUP. SW e BUY/SELL saem pelo sweep e o Pine
    # também os plota sem olhar score — dizer "(mín 60)" neles sugere um portão que
    # não existe (91% dos breakouts saem com score < 60, e está certo assim).
    minimo = f.get("score_min", 60)
    sufixo_score = (f" (mín {minimo:.0f})" if ev["tipo"] == "setup"
                    else " (só contexto — o mínimo de "
                         f"{minimo:.0f} é do setup)")
    linhas = [f"{icone} <b>{html.escape(ev['symbol'])} — {titulo}</b> [{ev['tf']}]",
              f"Preço <code>{_fmt(f['preco'])}</code> | Score <b>{f.get('score', 0):.0f}/100</b>{sufixo_score}"]
    # O Pine deixa o setup disparar quando a VELA tocou a zona (inFibZone), então numa
    # vela violenta o alerta sai com o preço já longe. Sem avisar, a mensagem sugere
    # "compra no suporte 0.0242" com o preço em 0.0400 e stop de 42% — engana.
    dz = dist_zona_pct(ev)
    aviso_zona = ""
    if dz is not None and abs(dz) >= 3:
        aviso_zona = (f" · <b>⚠ preço {abs(dz):.1f}% {'acima' if dz > 0 else 'abaixo'} "
                      f"da zona</b> (a vela tocou a zona, o preço não está nela)")
    linhas.append(f"Zona {papel} <code>{_fmt(f['zona_fundo'])}–{_fmt(f['zona_topo'])}</code> · "
                  f"Fibo {f['direcao'].upper()}{aviso_zona}")
    linhas.append(_linha_divergencia(ev))
    comp = f.get("comp") or {}
    partes = []
    if comp.get("fib"):
        partes.append("zona ✔")
    if comp.get("sweep"):
        nivel = f["nivel_min_dia"] if compra else f["nivel_max_dia"]
        de_onde = ("da sessão anterior" if _c("FUSION_LIQUIDEZ", "dia") == "sessao"
                   else "do dia anterior")
        partes.append(f"sweep ✔ ({'mínima' if compra else 'máxima'} {de_onde} {_fmt(nivel)})")
    oi = f.get("oi")
    if oi and oi.get("disponivel"):
        partes.append(f"{oi['contexto']}" + (" ✔" if comp.get("oi") else ""))
    if comp.get("vol"):
        partes.append(f"vol {f['vol_ratio']:.1f}x")
    rsi = f.get("rsi")
    if rsi:
        partes.append(f"PWR {rsi['power']:.0f} (disp {rsi['disp']:.1f})" + (" ✔" if comp.get("rsi") else ""))
    if comp.get("fvg"):
        partes.append("FVG ✔")
    if partes:
        linhas.append("Confluências: " + " · ".join(partes))
    ref = referencia_risco(f)
    if ref and ev["tipo"] == "setup":
        alvos = " / ".join(_fmt(a) for a in ref["alvos"]) or "—"
        rr = f" · R:R {ref['rr']:.1f}" if ref["rr"] else ""
        linhas.append(f"Ref. por ATR: stop <code>{_fmt(ref['stop'])}</code> · alvos <code>{alvos}</code>{rr} (não é ordem)")
    simbolo_tv = f"BINANCE:{ev['symbol']}.P"
    linhas.append(f"https://www.tradingview.com/chart/?symbol={simbolo_tv}&interval={_TV_INTERVALO.get(ev['tf'], '15')}")
    return "\n".join(linhas)
