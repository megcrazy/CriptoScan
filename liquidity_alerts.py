"""
Motor de alertas do Titanium Fusion para cripto.

Regras:
- Liquidez: máxima/mínima do dia UTC anterior e do bloco 4h anterior.
- Sweep: candle fechado rompe o nível e fecha de volta para dentro do range.
- Compra: sweep de máxima seguido de fechamento acima da máxima do sweep.
- Venda: sweep de mínima seguido de fechamento abaixo da mínima do sweep.
- Score: mesma ideia do Pine, sem pontos de sessão; normalizado pelos componentes disponíveis.
- Alertas por borda: setup, sweep e rompimento são emitidos uma única vez por evento.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pandas as pd

import fib_papel


SCORE_MIN = float(os.getenv("ALERT_SCORE_MIN", "60"))
SWEEP_VOLUME_MULT = float(os.getenv("SWEEP_VOLUME_MULT", "1.5"))
SWEEP_EXPIRE_BARS = int(os.getenv("SWEEP_EXPIRE_BARS", "30"))
STATE_PATH = Path(os.getenv("ALERT_STATE_PATH", "alert_state.json"))

# Pontuação do Pine, sem sessão.
SCORE_WEIGHTS = {
    "fib": 20.0,
    "sweep": 25.0,
    "oi": 20.0,
    "volume": 10.0,
    "rsi": 10.0,
    "convergencia": 5.0,
    "fvg": 10.0,
}


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"seen": {}, "sweeps": {}}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # O Windows pode usar cp1252/charmap como encoding padrão. O estado
    # precisa ser sempre UTF-8 e ASCII-safe para não quebrar o scanner.
    payload = json.dumps(state, ensure_ascii=True, indent=2)
    # Um nome fixo (alert_state.tmp) colide facilmente com outra instância,
    # antivírus/OneDrive ou uma tentativa de rename ainda em andamento.
    unique = f".{STATE_PATH.stem}.{os.getpid()}.{threading.get_ident()}.tmp"
    tmp = STATE_PATH.with_name(unique)
    tmp.write_text(payload, encoding="utf-8", newline="\n")
    for tentativa in range(6):
        try:
            os.replace(tmp, STATE_PATH)
            return
        except PermissionError:
            if tentativa == 5:
                # Não derruba o scan por bloqueio transitório do Windows.
                # O arquivo temporário fica disponível para recuperação e o
                # próximo candle tentará persistir novamente.
                print(f"[Alertas] aviso: estado ocupado; mantendo scan ativo ({STATE_PATH})")
                return
            time.sleep(0.05 * (tentativa + 1))


def _key(symbol: str, tf: str) -> str:
    return f"{symbol}:{tf}"


def _candle_time(candle) -> str:
    return pd.Timestamp(candle["time"]).isoformat()


def _fmt(v: float | None) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"{float(v):.8g}"


def _rsi(df: pd.DataFrame, period: int = 14) -> float | None:
    delta = df["close"].diff()
    gain = delta.clip(lower=0).ewm(com=period - 1, min_periods=period).mean()
    loss = (-delta).clip(lower=0).ewm(com=period - 1, min_periods=period).mean()
    if loss.iloc[-1] == 0:
        return 100.0
    value = 100 - (100 / (1 + gain.iloc[-1] / loss.iloc[-1]))
    return None if pd.isna(value) else float(value)


def _atr(df: pd.DataFrame, period: int = 14) -> float | None:
    prev = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"] - prev).abs(),
    ], axis=1).max(axis=1)
    value = tr.ewm(com=period - 1, min_periods=period).mean().iloc[-1]
    return None if pd.isna(value) else float(value)


def _levels(df: pd.DataFrame) -> list[dict]:
    """Retorna os níveis do dia anterior e do bloco 4h anterior, em UTC."""
    d = df.copy()
    d["time"] = pd.to_datetime(d["time"], utc=True)
    d["day"] = d["time"].dt.floor("D")
    d["block4h"] = d["time"].dt.floor("4h")
    current_day = d["day"].iloc[-1]
    current_block = d["block4h"].iloc[-1]
    out = []
    for name, col, current, delta in (
        ("dia_anterior", "day", current_day, pd.Timedelta(days=1)),
        ("range_4h_anterior", "block4h", current_block, pd.Timedelta(hours=4)),
    ):
        previous = current - delta
        group = d[d[col] == previous]
        if group.empty:
            continue
        out.extend([
            {"origem": name, "tipo": "maxima", "preco": float(group["high"].max())},
            {"origem": name, "tipo": "minima", "preco": float(group["low"].min())},
        ])
    return out


def _fvg(df: pd.DataFrame, direction: str) -> bool:
    if len(df) < 3:
        return False
    a, _, c = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    atr = _atr(df)
    if atr is None:
        return False
    minimum = atr * float(os.getenv("FVG_MIN_ATR", "0.3"))
    if direction == "long":
        return float(c["low"] - a["high"]) >= minimum
    return float(a["low"] - c["high"]) >= minimum


def _volume_ratio(df: pd.DataFrame) -> float | None:
    if len(df) < 21:
        return None
    ref = float(df["volume"].iloc[-21:-1].mean())
    return None if ref <= 0 else float(df["volume"].iloc[-1] / ref)


def _score(df: pd.DataFrame, zona: dict | None, direction: str, sweep: bool = False,
           oi_aligned: bool = False) -> tuple[float, list[str], dict]:
    if not zona or len(df) < 30:
        return 0.0, [], {}
    price = float(df["close"].iloc[-1])
    papel = fib_papel.papel_zona(zona, price)
    if papel["vies_rejeicao"] != direction and papel["papel"] != "dentro":
        return 0.0, [], {"papel": papel}

    raw = 0.0
    available = SCORE_WEIGHTS["fib"] + SCORE_WEIGHTS["volume"] + SCORE_WEIGHTS["rsi"] + SCORE_WEIGHTS["convergencia"] + SCORE_WEIGHTS["fvg"]
    reasons = []
    raw += SCORE_WEIGHTS["fib"]
    reasons.append("zona Fibonacci")
    ratio = _volume_ratio(df)
    if ratio is not None and ratio >= 1.5:
        raw += SCORE_WEIGHTS["volume"]
        reasons.append(f"volume {ratio:.2f}x")
    rsi = _rsi(df)
    if rsi is not None and ((direction == "long" and rsi >= 55) or (direction == "short" and rsi <= 45)):
        raw += SCORE_WEIGHTS["rsi"]
        reasons.append(f"RSI {rsi:.1f}")
    # Proxy equivalente ao convGood do Pine: RSI não excessivamente disperso entre 1h/4h.
    if rsi is not None and 35 <= rsi <= 65:
        raw += SCORE_WEIGHTS["convergencia"]
        reasons.append("convergência moderada")
    if _fvg(df, direction):
        raw += SCORE_WEIGHTS["fvg"]
        reasons.append("FVG alinhado")
    if oi_aligned:
        raw += SCORE_WEIGHTS["oi"]
        available += SCORE_WEIGHTS["oi"]
        reasons.append("OI alinhado")
    if sweep:
        raw += SCORE_WEIGHTS["sweep"]
        available += SCORE_WEIGHTS["sweep"]
        reasons.append("sweep alinhado")
    score = min(raw / available * 100, 100) if available else 0.0
    return round(score, 1), reasons, {"papel": papel, "rsi": rsi, "volume_ratio": ratio}


def _event(kind: str, symbol: str, tf: str, candle, direction: str, level: dict,
           score: float, reasons: list[str], extra: dict | None = None) -> dict:
    data = {
        "kind": kind, "symbol": symbol, "tf": tf, "direction": direction,
        "candle_time": _candle_time(candle), "price": float(candle["close"]),
        "level_origin": level["origem"], "level_type": level["tipo"],
        "level_price": level["preco"], "score": score, "reasons": reasons,
    }
    if extra:
        data.update(extra)
    data["dedupe_key"] = ":".join([
        symbol, tf, kind, level["origem"], level["tipo"], data["candle_time"]
    ])
    return data


class LiquidityAlertEngine:
    def __init__(self, state_path: str | Path | None = None):
        global STATE_PATH
        if state_path is not None:
            STATE_PATH = Path(state_path)
        self.state = _load_state()
        self.state.setdefault("seen", {})
        self.state.setdefault("sweeps", {})
        self._lock = threading.Lock()

    def _once(self, event: dict) -> dict | None:
        key = event["dedupe_key"]
        if key in self.state["seen"]:
            return None
        self.state["seen"][key] = int(time.time())
        return event

    def process(self, symbol: str, tf: str, df: pd.DataFrame, zona: dict | None,
                oi_aligned: bool = False) -> list[dict]:
        # O scanner usa asyncio.to_thread para vários símbolos ao mesmo tempo.
        with self._lock:
            return self._process(symbol, tf, df, zona, oi_aligned)

    def _process(self, symbol: str, tf: str, df: pd.DataFrame, zona: dict | None,
                 oi_aligned: bool = False) -> list[dict]:
        if df is None or len(df) < 40 or zona is None:
            return []
        papel_atual = fib_papel.papel_zona(zona, float(df["close"].iloc[-1]))
        # Igual ao Pine: scoreSignal exige um papel definido (suporte/resistência).
        if papel_atual["vies_rejeicao"] is None or papel_atual["invalidada"]:
            return []
        direction = papel_atual["vies_rejeicao"]
        score, reasons, context = _score(df, zona, direction, False, oi_aligned)
        candle = df.iloc[-1]
        events = []
        setup_key = f"{symbol}:{tf}:setup:{zona.get('pattern_id', zona.get('c_time', 'none'))}:{direction}"
        if score >= SCORE_MIN and setup_key not in self.state["seen"]:
            setup = _event("setup", symbol, tf, candle, direction,
                           {"origem": "fibonacci", "tipo": "zona", "preco": float(candle["close"])},
                           score, reasons, {"context": context})
            setup["dedupe_key"] = setup_key
            events.append(self._once(setup))

        levels = _levels(df)
        vr = _volume_ratio(df)
        for level in levels:
            p = level["preco"]
            high_sweep = level["tipo"] == "maxima" and float(candle["high"]) > p and float(candle["close"]) < p and (vr is not None and vr >= SWEEP_VOLUME_MULT)
            low_sweep = level["tipo"] == "minima" and float(candle["low"]) < p and float(candle["close"]) > p and (vr is not None and vr >= SWEEP_VOLUME_MULT)
            aligned = (direction == "long" and high_sweep) or (direction == "short" and low_sweep)
            skey = f"{symbol}:{tf}:{level['origem']}:{level['tipo']}"
            if aligned:
                state = self.state["sweeps"].get(skey)
                if not state or state.get("candle_time") != _candle_time(candle):
                    state = {"candle_time": _candle_time(candle), "price": float(candle["high"] if high_sweep else candle["low"]), "bars": 0, "direction": direction}
                    self.state["sweeps"][skey] = state
                    sc, rs, ctx = _score(df, zona, direction, True, oi_aligned)
                    ev = _event("sweep", symbol, tf, candle, direction, level, sc, rs, {"sweep_price": state["price"], "context": ctx})
                    events.append(self._once(ev))
            state = self.state["sweeps"].get(skey)
            if not state or state.get("direction") != direction:
                continue
            state["bars"] = int(state.get("bars", 0)) + 1
            sweep_price = float(state["price"])
            broke = (direction == "long" and float(candle["close"]) > sweep_price and _candle_time(candle) != state["candle_time"]) or (direction == "short" and float(candle["close"]) < sweep_price and _candle_time(candle) != state["candle_time"])
            if broke:
                sc, rs, ctx = _score(df, zona, direction, True, oi_aligned)
                ev = _event("breakout", symbol, tf, candle, direction, level, sc, rs,
                            {"sweep_price": sweep_price, "context": ctx})
                events.append(self._once(ev))
                self.state["sweeps"].pop(skey, None)
            elif state["bars"] > SWEEP_EXPIRE_BARS:
                self.state["sweeps"].pop(skey, None)
        events = [e for e in events if e is not None]
        # Se o mesmo candle varrer simultaneamente o nível diário e o 4h,
        # Telegram recebe um único aviso; o primeiro nível é a referência.
        unique = {}
        for event in events:
            unique.setdefault((event["kind"], event["direction"], event["candle_time"]), event)
        events = list(unique.values())
        _save_state(self.state)
        return events


def format_alert(event: dict) -> str:
    arrow = "🟢" if event["direction"] == "long" else "🔴"
    side = "COMPRA" if event["direction"] == "long" else "VENDA"
    stage = {"setup": "SETUP", "sweep": "SWEEP", "breakout": "ROMPIMENTO CONFIRMADO"}[event["kind"]]
    lines = [
        f"{arrow} <b>{event['symbol']} — {stage} DE {side}</b>",
        f"Timeframe: {event['tf']} | Preço: <code>{_fmt(event['price'])}</code>",
        f"Liquidez: {event['level_origin']} / {event['level_type']} <code>{_fmt(event['level_price'])}</code>",
        f"Score: <b>{event['score']:.1f}/100</b>",
    ]
    if event.get("sweep_price") is not None:
        lines.append(f"Preço do sweep: <code>{_fmt(event['sweep_price'])}</code>")
    if event.get("reasons"):
        lines.append("Confluências: " + ", ".join(event["reasons"]))
    if event["kind"] == "setup":
        lines.append("Aguardando sweep alinhado.")
    elif event["kind"] == "sweep":
        lines.append("Aguardando rompimento posterior do sweep.")
    else:
        lines.append("Rompimento confirmado após sweep.")
    return "\n".join(lines)
