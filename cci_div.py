# ============================================================
#  cci_div.py — Porte para Python da divergência CCI do scanner Node
#  (calculateCCI, findSwings, findBestDivergence, classificarEstado)
#  Mesma lógica; acrescenta taker buy ratio no swing2.
# ============================================================
import numpy as np
import pandas as pd


def calcular_cci(high, low, close, periodo: int) -> np.ndarray:
    tp = (np.asarray(high) + np.asarray(low) + np.asarray(close)) / 3.0
    if len(tp) < periodo:
        return np.array([])
    janelas = np.lib.stride_tricks.sliding_window_view(tp, periodo)
    sma = janelas.mean(axis=1)
    desvio = np.abs(janelas - sma[:, None]).mean(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        cci = np.where(desvio == 0, 0.0, (tp[periodo - 1:] - sma) / (0.015 * desvio))
    return cci


def achar_swings(dados, janela: int):
    picos, vales = [], []
    n = len(dados)
    for i in range(janela, n - janela):
        eh_pico = eh_vale = True
        for j in range(i - janela, i + janela + 1):
            if j == i:
                continue
            if dados[j] >= dados[i]:
                eh_pico = False
            if dados[j] <= dados[i]:
                eh_vale = False
        if eh_pico:
            picos.append(i)
        if eh_vale:
            vales.append(i)
    return picos, vales


def melhor_divergencia(swings, cci, preco, direcao: str, total: int, cfg: dict):
    """Recência antes da força (swing2 mais novo vence; empate -> maior força)."""
    melhor = None
    for a in range(len(swings)):
        for b in range(a + 1, len(swings)):
            i1, i2 = swings[a], swings[b]
            if i2 - i1 < cfg["min_swing_distance"]:
                continue
            idade = (total - 1) - i2
            if idade > cfg["recency_window_candles"] or idade > cfg["max_signal_age_candles"]:
                continue
            p1, p2, c1, c2 = preco[i1], preco[i2], cci[i1], cci[i2]
            if direcao == "bearish":
                valido, forca = (p2 > p1 and c2 < c1), c1 - c2
            else:
                valido, forca = (p2 < p1 and c2 > c1), c2 - c1
            if not valido:
                continue
            if melhor is None or i2 > melhor["idx2"] or (i2 == melhor["idx2"] and forca > melhor["forca"]):
                melhor = {"idx1": i1, "idx2": i2, "cci1": float(c1), "cci2": float(c2), "forca": float(forca)}
    return melhor


def classificar_estado(direcao: str, div: dict, cci, total: int, cfg: dict) -> dict:
    cci_atual = float(cci[total - 1])
    idade = (total - 1) - div["idx2"]
    base = {"cci_atual": cci_atual, "candles_desde_swing2": idade}
    if idade > cfg["max_signal_age_candles"]:
        return {**base, "estado": "expirada"}
    if idade <= cfg["armada_max_candles"]:
        return {**base, "estado": "armada"}
    if direcao == "bearish":
        if cci_atual > div["cci1"]:
            return {**base, "estado": "invalidada"}
        mov = div["cci2"] - cci_atual
    else:
        if cci_atual < div["cci1"]:
            return {**base, "estado": "invalidada"}
        mov = cci_atual - div["cci2"]
    if mov >= cfg["ativa_min_cci_move"]:
        return {**base, "estado": "ativa"}
    if mov > 0:
        return {**base, "estado": "confirmada"}
    return {**base, "estado": "fraca"}


def analisar_divergencias(df: pd.DataFrame, cfg: dict) -> list[dict]:
    """
    df: candles FECHADOS (colunas time, open, high, low, close, volume, taker_buy_volume).
    Retorna até 2 sinais (bearish e bullish).
    """
    per = cfg["cci_period"]
    if len(df) < per + cfg["swing_window"] * 2 + cfg["min_swing_distance"]:
        return []

    high, low, close = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    cci = calcular_cci(high, low, close, per)

    a_high, a_low = high[per - 1:], low[per - 1:]
    a_vol = df["volume"].to_numpy()[per - 1:]
    a_taker = df["taker_buy_volume"].to_numpy()[per - 1:]
    a_time = df["time"].iloc[per - 1:].reset_index(drop=True)
    total = len(a_high)

    def razao_volume(idx):
        ini = max(0, idx - cfg["volume_avg_window"])
        ref = a_vol[ini:idx]
        if len(ref) == 0 or ref.mean() == 0:
            return 1.0
        return float(a_vol[idx] / ref.mean())

    def razao_taker(idx):
        return float(a_taker[idx] / a_vol[idx]) if a_vol[idx] > 0 else 0.5

    picos, _ = achar_swings(a_high, cfg["swing_window"])
    _, vales = achar_swings(a_low, cfg["swing_window"])

    sinais = []
    for direcao, swings, preco in (("bearish", picos, a_high), ("bullish", vales, a_low)):
        div = melhor_divergencia(swings, cci, preco, direcao, total, cfg)
        if not div:
            continue
        estado = classificar_estado(direcao, div, cci, total, cfg)
        sinais.append({
            "direction": direcao,
            "ts1": int(a_time.iloc[div["idx1"]].timestamp() * 1000),
            "ts2": int(a_time.iloc[div["idx2"]].timestamp() * 1000),
            "cci1": div["cci1"], "cci2": div["cci2"], "forca": div["forca"],
            "volume_ratio": razao_volume(div["idx2"]),
            "taker_buy_ratio": razao_taker(div["idx2"]),
            **estado,
        })
    return sinais
