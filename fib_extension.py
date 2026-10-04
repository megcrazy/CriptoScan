# ============================================================
#  fib_extension.py — Extensão de Fibonacci 3 pontos (A-B-C)
#  Porte do indicador Pine "Extensão Fibonacci 3 Pontos Dinâmica"
#  (fibonacci_extensao_3p_dinamica_v1.pine)
#
#  Lógica idêntica ao Pine:
#   - Detecta pivôs de topo/fundo confirmados (pivotLeft/pivotRight = 5/5)
#   - Guarda os 3 últimos pivôs alternados (A, B, C)
#   - Padrão de alta:  A fundo -> B topo -> C fundo, com B > A e C < B
#   - Padrão de baixa: A topo  -> B fundo -> C topo,  com B < A e C > B
#   - Filtra o impulso B-A pelo ATR no momento do pivô B (evita perna fraca)
#   - Zona de reação = entre a extensão 0,500 e 0,618 a partir de C
#
#  Diferença proposital em relação ao Pine: o alerta dispara só na
#  TRANSIÇÃO de fora -> dentro da zona (edge trigger).
#
#  v2 — Confluência de momentum (EMA8 + RSI + vela do toque) contra a
#  direção projetada. Não suprime o alerta, só adiciona contexto.
#
#  v3
#   - pattern_id estável: timestamp da vela C (antes era o índice no df,
#     que mudava a cada vela nova). O estado do edge-trigger agora guarda
#     {pattern_id, dentro}: zona nova = zona nova, mesmo com preço dentro.
#   - Partida a frio: na 1ª leitura após reiniciar o bot o estado só é
#     semeado (não alerta zona velha).
#   - GATILHO M5 DENTRO DA ZONA M15/H1 (verificar_rejeicao_m5):
#       1) toque na zona M15/H1 abre uma "janela de observação"
#       2) se nessa janela o M5 formar um padrão A-B-C de sentido OPOSTO
#          à projeção da zona, com C dentro/perto da zona -> alerta
#          de REJEIÇÃO (é o M5 dizendo qual dos dois cenários aconteceu).
#   - PADRÃO NOVO (verificar_padrao_novo): detector "solto" pensado para
#     rodar em MODO SOMBRA (só grava no research, não manda Telegram),
#     pra medir se tem edge antes de virar alerta.
# ============================================================
from datetime import timedelta

import pandas as pd

try:
    import config
except Exception:  # permite testar o módulo isolado
    config = None

PIVOT_LEFT = 5
PIVOT_RIGHT = 5
ATR_PERIODO_PIVO = 14
ATR_MULTIPLO_IMPULSO = 1.0

# ── Confluência de momentum (v2) ──────────────────────────────
EMA_CONFLUENCIA_PERIODO = 8
RSI_PERIODO_CONFLUENCIA = 14
REJEICAO_WICK_BODY_MULT = 1.15
REJEICAO_CLOSE_POS_MAX = 0.4
REJEICAO_CLOSE_POS_MIN = 0.6

# Minutos por vela M5 (só usado pra calcular o atraso de confirmação do pivô)
_MIN_POR_VELA_M5 = 5


def _cfg(nome: str, padrao):
    return getattr(config, nome, padrao) if config is not None else padrao


# ── Estados em memória ────────────────────────────────────────
# Edge-trigger da zona: chave -> {"pattern_id": str|None, "dentro": bool}
_estado_zona: dict[str, dict] = {}
# Janelas de observação abertas por um toque em M15/H1
_janelas: dict[str, dict] = {}
# Último padrão M5 que já gerou gatilho, por janela
_estado_m5: dict[str, str] = {}
# Último padrão "novo" visto por (símbolo, tf) — modo sombra
_estado_padrao: dict[str, str] = {}


# ── Indicadores auxiliares ────────────────────────────────────
def _atr(df: pd.DataFrame, periodo: int = ATR_PERIODO_PIVO) -> pd.Series:
    high, low, prev = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(com=periodo - 1, min_periods=periodo).mean()


def _ema(df: pd.DataFrame, periodo: int) -> pd.Series:
    return df["close"].ewm(span=periodo, adjust=False).mean()


def _rsi(df: pd.DataFrame, periodo: int = RSI_PERIODO_CONFLUENCIA) -> pd.Series:
    delta = df["close"].diff()
    ganho = delta.clip(lower=0)
    perda = (-delta).clip(lower=0)
    media_ganho = ganho.ewm(com=periodo - 1, min_periods=periodo).mean()
    media_perda = perda.ewm(com=periodo - 1, min_periods=periodo).mean()
    rs = media_ganho / media_perda
    return 100 - (100 / (1 + rs))


def _posicao_fechamento(candle: pd.Series) -> float:
    high, low = float(candle["high"]), float(candle["low"])
    candle_range = high - low
    if candle_range <= 0:
        return 0.5
    return float((float(candle["close"]) - low) / candle_range)


def _candle_rejeitou(candle: pd.Series, direcao: str) -> bool:
    """Pavio dominante contra a direção projetada + fechamento fraco."""
    abertura, fechamento = float(candle["open"]), float(candle["close"])
    high, low = float(candle["high"]), float(candle["low"])
    corpo = max(abs(fechamento - abertura), (high - low) * 0.05)
    pos_close = _posicao_fechamento(candle)

    if direcao == "alta":
        pavio_topo = high - max(abertura, fechamento)
        return pavio_topo >= corpo * REJEICAO_WICK_BODY_MULT and pos_close < REJEICAO_CLOSE_POS_MAX

    pavio_fundo = min(abertura, fechamento) - low
    return pavio_fundo >= corpo * REJEICAO_WICK_BODY_MULT and pos_close > REJEICAO_CLOSE_POS_MIN


def avaliar_confluencia(df: pd.DataFrame, direcao: str) -> dict:
    """
    Compara momentum de curtíssimo prazo (EMA8 + RSI) e qualidade da vela
    atual CONTRA a direção projetada pela zona.
    vies: "alinhado" (continuação), "divergente" (rejeição) ou "neutro".
    """
    if df is None or len(df) < EMA_CONFLUENCIA_PERIODO + RSI_PERIODO_CONFLUENCIA:
        return {"vies": "neutro", "motivos": []}

    candle = df.iloc[-1]
    ema8 = _ema(df, EMA_CONFLUENCIA_PERIODO).iloc[-1]
    rsi_atual = _rsi(df).iloc[-1]
    preco = float(candle["close"])

    if pd.isna(ema8) or pd.isna(rsi_atual):
        return {"vies": "neutro", "motivos": []}

    momentum_alta = preco > ema8 and rsi_atual > 50
    momentum_baixa = preco < ema8 and rsi_atual < 50
    rejeitou = _candle_rejeitou(candle, direcao)

    motivos = []
    momentum_alinhado = (direcao == "alta" and momentum_alta) or (direcao == "baixa" and momentum_baixa)
    momentum_contrario = (direcao == "alta" and momentum_baixa) or (direcao == "baixa" and momentum_alta)

    if momentum_contrario:
        motivos.append(f"preço/RSI ({round(rsi_atual, 1)}) contra a direção da zona")
    elif momentum_alinhado:
        motivos.append(f"preço/RSI ({round(rsi_atual, 1)}) a favor da direção da zona")

    if rejeitou:
        motivos.append("vela do toque já mostra pavio de rejeição + fechamento fraco")

    if momentum_contrario or rejeitou:
        vies = "divergente"
    elif momentum_alinhado:
        vies = "alinhado"
    else:
        vies = "neutro"

    return {"vies": vies, "motivos": motivos}


# ── Pivôs / padrão ABC ────────────────────────────────────────
def _pivos_confirmados(df: pd.DataFrame, esquerda: int = PIVOT_LEFT, direita: int = PIVOT_RIGHT) -> list[dict]:
    """
    Replica ta.pivothigh/ta.pivotlow do Pine. Em caso de empate no mesmo
    candle prioriza o fundo. tipo: 1 = topo, -1 = fundo.
    """
    pivos = []
    high, low = df["high"], df["low"]
    n = len(df)
    for i in range(esquerda, n - direita):
        janela_h = high.iloc[i - esquerda: i + direita + 1]
        janela_l = low.iloc[i - esquerda: i + direita + 1]
        eh_fundo = low.iloc[i] == janela_l.min()
        eh_topo = high.iloc[i] == janela_h.max()
        if eh_fundo:
            pivos.append({"indice": i, "tipo": -1, "preco": float(low.iloc[i])})
        elif eh_topo:
            pivos.append({"indice": i, "tipo": 1, "preco": float(high.iloc[i])})
    return pivos


def _ultimos_3_alternados(pivos: list[dict]) -> list[dict]:
    """Mantém os 3 últimos pivôs ALTERNADOS; dois iguais seguidos -> fica o mais extremo."""
    seq: list[dict] = []
    for p in pivos:
        if not seq:
            seq.append(p)
            continue
        if p["tipo"] == seq[-1]["tipo"]:
            mais_extremo = (
                p["preco"] > seq[-1]["preco"] if p["tipo"] == 1
                else p["preco"] < seq[-1]["preco"]
            )
            if mais_extremo:
                seq[-1] = p
        else:
            seq.append(p)
            if len(seq) > 3:
                seq = seq[-3:]
    return seq


def detectar_extensao(df: pd.DataFrame, atr_mult: float | None = None,
                      casas: int | None = 2) -> dict | None:
    """
    Retorna a extensão ativa (padrão ABC válido nos últimos pivôs).
    pattern_id = timestamp da vela C (estável entre rodadas).
    idade_c    = quantas velas se passaram desde C (>= PIVOT_RIGHT).
    casas      = arredondamento dos preços. 2 mantém o comportamento do bot MT5;
                 None = sem arredondamento (use em cripto: PEPE, USUAL etc.).
    """
    def _r(x: float) -> float:
        return x if casas is None else round(x, casas)

    if df is None or len(df) < (PIVOT_LEFT + PIVOT_RIGHT + 30):
        return None
    atr_mult = ATR_MULTIPLO_IMPULSO if atr_mult is None else atr_mult

    pivos = _pivos_confirmados(df)
    seq = _ultimos_3_alternados(pivos)
    if len(seq) < 3:
        return None

    a, b, c = seq[-3], seq[-2], seq[-1]

    bull = (
        a["tipo"] == -1 and b["tipo"] == 1 and c["tipo"] == -1
        and b["preco"] > a["preco"] and c["preco"] < b["preco"]
    )
    bear = (
        a["tipo"] == 1 and b["tipo"] == -1 and c["tipo"] == 1
        and b["preco"] < a["preco"] and c["preco"] > b["preco"]
    )
    if not (bull or bear):
        return None

    atr = _atr(df)
    if b["indice"] >= len(atr):
        return None
    atr_no_pivo_b = atr.iloc[b["indice"]]
    if pd.isna(atr_no_pivo_b):
        return None

    impulso = (b["preco"] - a["preco"]) if bull else (a["preco"] - b["preco"])
    if impulso < atr_no_pivo_b * atr_mult:
        return None

    direcao = "alta" if bull else "baixa"
    sinal = 1 if bull else -1
    z1 = c["preco"] + sinal * impulso * 0.500
    z2 = c["preco"] + sinal * impulso * 0.618
    alvo_1000 = c["preco"] + sinal * impulso * 1.000
    alvo_1618 = c["preco"] + sinal * impulso * 1.618

    if "time" in df.columns:
        c_time = df["time"].iloc[c["indice"]]
        pattern_id = str(c_time)
    else:
        c_time = None
        pattern_id = str(c["indice"])

    return {
        "direcao": direcao,
        "a": _r(a["preco"]), "b": _r(b["preco"]), "c": _r(c["preco"]),
        "impulso": _r(impulso),
        "zona_topo": _r(max(z1, z2)),
        "zona_fundo": _r(min(z1, z2)),
        "alvo_1000": _r(alvo_1000),
        "alvo_1618": _r(alvo_1618),
        "pattern_id": pattern_id,
        "c_time": c_time,
        "idade_c": len(df) - 1 - c["indice"],
    }


# ── Janela de observação (M15/H1 -> M5) ───────────────────────
def _abrir_janela(chave: str, timeframe: str, ext: dict, vies: str,
                  t_ref, prefixo_tipo: str, simbolo: str | None) -> None:
    minutos = _cfg("FIB_M5_JANELA_MIN", {"M15": 45, "H1": 90}).get(timeframe)
    if not minutos or t_ref is None:
        return
    _janelas[chave] = {
        "tf": timeframe,
        "prefixo": prefixo_tipo,
        "simbolo": simbolo,
        "direcao": ext["direcao"],
        "zona_topo": ext["zona_topo"],
        "zona_fundo": ext["zona_fundo"],
        "vies": vies,
        "aberta_em": t_ref,
        "ate": t_ref + timedelta(minutes=minutos),
        "pattern_id": ext["pattern_id"],
    }
    _estado_m5.pop(chave, None)


def verificar_toque(
    df: pd.DataFrame,
    timeframe: str,
    prefixo_tipo: str = "",
    simbolo: str | None = None,
    t_ref=None,
) -> dict | None:
    """
    Dispara na TRANSIÇÃO de fora -> dentro da zona 0,5-0,618.
    t_ref: horário de referência (use o último candle M5 se tiver) para
    abrir a janela de observação do gatilho M5; se None, usa o último
    candle do próprio df.
    """
    ext = detectar_extensao(df)
    chave_estado = f"{prefixo_tipo}{simbolo or ''}_{timeframe}"

    if ext is None:
        _estado_zona[chave_estado] = {"pattern_id": None, "dentro": False}
        return None

    candle = df.iloc[-1]
    dentro_agora = bool(candle["low"] <= ext["zona_topo"] and candle["high"] >= ext["zona_fundo"])

    anterior = _estado_zona.get(chave_estado)
    _estado_zona[chave_estado] = {"pattern_id": ext["pattern_id"], "dentro": dentro_agora}

    if anterior is None:
        # Partida a frio: só semeia o estado, não alerta zona velha.
        return None

    estava_dentro = anterior["dentro"] if anterior["pattern_id"] == ext["pattern_id"] else False
    if not dentro_agora or estava_dentro:
        return None

    preco = float(candle["close"])
    direcao = ext["direcao"]
    emoji = "🟢" if direcao == "alta" else "🔴"

    confluencia = avaliar_confluencia(df, direcao)
    if confluencia["vies"] == "divergente":
        selo = f" ⚠️ [MOMENTUM DIVERGENTE — {'; '.join(confluencia['motivos'])}. Viés local de REJEIÇÃO.]"
        emoji = "🟡"
    elif confluencia["vies"] == "alinhado":
        selo = f" ✅ [MOMENTUM ALINHADO — {'; '.join(confluencia['motivos'])}. Reforça viés de continuação.]"
    else:
        selo = ""

    if t_ref is None and "time" in df.columns:
        t_ref = df["time"].iloc[-1]
    _abrir_janela(chave_estado, timeframe, ext, confluencia["vies"], t_ref, prefixo_tipo, simbolo)

    alerta = {
        "tipo": f"{prefixo_tipo}fib_extensao_{direcao}",
        "emoji": emoji,
        "titulo": f" ZONA DE REAÇÃO ({direcao.upper()})",
        "valor": round(preco, 2),
        "descricao": (
            f"Preço entrou na zona de reação "
            f"(${ext['zona_fundo']} – ${ext['zona_topo']}), projetada a partir do padrão "
            f"A(${ext['a']}) → B(${ext['b']}) → C(${ext['c']}). "
            f"Historicamente essa é uma região de reação — pode rejeitar ou romper com continuação. "
            f"Alvo 1,0: ${ext['alvo_1000']} | Alvo 1,618: ${ext['alvo_1618']}."
            f"{selo}"
        ),
        "confluencia": confluencia["vies"],
    }
    if simbolo:
        alerta["simbolo"] = simbolo
    return alerta


# ── Gatilho M5 dentro da zona M15/H1 ──────────────────────────
def verificar_rejeicao_m5(
    df_m5: pd.DataFrame,
    simbolo: str | None = None,
    prefixo_tipo: str = "",
) -> list[dict]:
    """
    Para cada janela aberta (toque em zona M15/H1), procura no M5 um padrão
    A-B-C de sentido OPOSTO ao da zona, com C dentro (ou perto) da zona.
    Zona de ALTA (projeta subida) + padrão M5 de BAIXA = rejeição de topo.
    Zona de BAIXA (projeta queda) + padrão M5 de ALTA  = rejeição de fundo.
    Retorna lista (pode haver janela M15 e H1 ao mesmo tempo).
    """
    if df_m5 is None or not _janelas or "time" not in df_m5.columns:
        return []

    agora = df_m5["time"].iloc[-1]
    atraso_confirmacao = timedelta(minutes=PIVOT_RIGHT * _MIN_POR_VELA_M5 + _MIN_POR_VELA_M5)

    minhas = []
    for chave, j in list(_janelas.items()):
        if j["prefixo"] != prefixo_tipo or j["simbolo"] != simbolo:
            continue
        # Janela expira depois de `ate` + tempo de confirmação do pivô M5
        if agora > j["ate"] + atraso_confirmacao:
            _janelas.pop(chave, None)
            _estado_m5.pop(chave, None)
            continue
        minhas.append((chave, j))
    if not minhas:
        return []

    ext = detectar_extensao(df_m5, atr_mult=_cfg("FIB_M5_ATR_MULT", 1.0))
    if ext is None or ext["c_time"] is None:
        return []
    atr_m5 = _atr(df_m5).iloc[-1]
    if pd.isna(atr_m5) or atr_m5 <= 0:
        return []
    tol = atr_m5 * _cfg("FIB_M5_TOL_ATR", 0.5)

    alertas = []
    for chave, j in minhas:
        if ext["direcao"] == j["direcao"]:
            continue  # precisa ser sentido oposto à zona
        if ext["c_time"] < j["aberta_em"] - timedelta(minutes=10) or ext["c_time"] > j["ate"]:
            continue  # C fora da janela
        if not (j["zona_fundo"] - tol <= ext["c"] <= j["zona_topo"] + tol):
            continue  # C longe da zona (rompimento, ou nada a ver)
        if _estado_m5.get(chave) == ext["pattern_id"]:
            continue
        _estado_m5[chave] = ext["pattern_id"]

        dir_m5 = ext["direcao"]
        dir_pai = j["direcao"]
        emoji = "🟢" if dir_m5 == "alta" else "🔴"
        lado_inval = "abaixo" if dir_m5 == "alta" else "acima"
        vies_pai = {
            "divergente": "momentum DIVERGENTE no toque ✅ (reforça rejeição)",
            "alinhado": "momentum ALINHADO no toque ⚠️ (contra a rejeição)",
        }.get(j["vies"], "momentum neutro no toque")

        conf_m5 = avaliar_confluencia(df_m5, dir_m5)
        if conf_m5["vies"] == "alinhado":
            m5_txt = "M5 a favor do padrão ✅"
        elif conf_m5["vies"] == "divergente":
            m5_txt = "M5 já perdendo força ⚠️"
        else:
            m5_txt = "M5 neutro"

        preco = float(df_m5["close"].iloc[-1])
        alerta = {
            "tipo": f"{prefixo_tipo}fib_rejeicao_{dir_m5}_{j['tf'].lower()}",
            "emoji": emoji,
            "titulo": f"REJEIÇÃO NA ZONA {j['tf']} — GATILHO M5 ({dir_m5.upper()})",
            "valor": round(preco, 2),
            "descricao": (
                f"Preço está na zona de reação {j['tf']} de {dir_pai.upper()} "
                f"(${j['zona_fundo']} – ${j['zona_topo']}). "
                f"No M5 formou padrão de {dir_m5.upper()}: "
                f"A(${ext['a']}) → B(${ext['b']}) → C(${ext['c']}), com C na zona. "
                f"Alvo M5 (0,5–0,618): ${ext['zona_fundo']} – ${ext['zona_topo']} | "
                f"Alvo 1,0 M5: ${ext['alvo_1000']}. "
                f"Invalidação: {lado_inval} de ${ext['c']}. "
                f"Contexto: {vies_pai}; {m5_txt}. "
                f"O padrão M5 só é confirmado ~{PIVOT_RIGHT * _MIN_POR_VELA_M5} min depois de C — "
                f"parte do movimento já pode ter andado."
            ),
            "confluencia": j["vies"],
        }
        if simbolo:
            alerta["simbolo"] = simbolo
        alertas.append(alerta)
    return alertas


# ── Padrão novo "solto" (pensado para MODO SOMBRA) ────────────
def verificar_padrao_novo(
    df: pd.DataFrame,
    timeframe: str,
    prefixo_tipo: str = "",
    simbolo: str | None = None,
) -> dict | None:
    """
    Retorna um "alerta" quando um padrão A-B-C acabou de ser confirmado.
    Direção do tracking = direção do padrão (bull = espera subida a partir
    de C), ou seja, mede a tese "C é começo de reversão".
    Sugestão: NÃO enviar pro Telegram — só registrar no research (sombra).
    """
    chave = f"padrao_{prefixo_tipo}{simbolo or ''}_{timeframe}"
    primeira_leitura = chave not in _estado_padrao
    if primeira_leitura:
        _estado_padrao[chave] = None  # partida a frio: semeia mesmo sem padrão ainda

    ext = detectar_extensao(df, atr_mult=_cfg("FIB_M5_ATR_MULT", 1.0) if timeframe == "M5" else None)
    if ext is None:
        return None
    if _estado_padrao.get(chave) == ext["pattern_id"]:
        return None

    _estado_padrao[chave] = ext["pattern_id"]
    # Só conta padrão RECÉM-confirmado (ignora padrão velho / partida a frio)
    if primeira_leitura or ext["idade_c"] > PIVOT_RIGHT + 2:
        return None

    direcao = ext["direcao"]
    preco = float(df["close"].iloc[-1])
    alerta = {
        "tipo": f"{prefixo_tipo}fib_padrao_{direcao}",
        "emoji": "🧭",
        "titulo": f"PADRÃO A-B-C FORMADO ({direcao.upper()})",
        "valor": round(preco, 2),
        "descricao": (
            f"A(${ext['a']}) → B(${ext['b']}) → C(${ext['c']}). "
            f"Zona 0,5–0,618: ${ext['zona_fundo']} – ${ext['zona_topo']}."
        ),
    }
    if simbolo:
        alerta["simbolo"] = simbolo
    return alerta
