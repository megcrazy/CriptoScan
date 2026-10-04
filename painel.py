# ============================================================
#  painel.py — Lógica compartilhada por ver_cache.py (CLI) e api.py (web)
#  Lê o cache (store), cruza com preços ao vivo e monta os candidatos.
#  Score de confluência multi-TF: ver `_avaliar()`.
# ============================================================
import time

import fib_extension as fibo
import fib_papel
import scanner
import settings
import store

VIVOS = {"ativa", "confirmada", "armada"}
ORDEM_TF = {"15m": 0, "1h": 1, "4h": 2, "12h": 3}


# ── Idade / validade dos dados ───────────────────────────────
def dado_velho(boundary_ts: int, tf: str, agora: float) -> bool:
    """True se o dado não foi renovado nos últimos N candles do TF (ex.: símbolo com erro no scan)."""
    return agora - boundary_ts > settings.DADO_VELHO_FATOR * settings.TIMEFRAMES[tf]["seconds"]


def zona_info(z: dict | None, preco: float, agora: float) -> dict | None:
    """Zona + papel calculado na hora + flags de visibilidade. None se não há zona."""
    if not z:
        return None
    info = fib_papel.papel_zona(z, preco)
    velha = z["idade_c"] > settings.ZONA_MAX_IDADE_C
    return {
        **{k: z[k] for k in ("tf", "direcao", "a", "b", "c", "zona_fundo", "zona_topo",
                              "alvo_1000", "alvo_1618", "idade_c", "c_time", "boundary_ts")},
        **info,
        "velha": velha,
        "dado_velho": dado_velho(z["boundary_ts"], z["tf"], agora),
        "visivel": not info["invalidada"] and not velha,
    }


def _div_out(d: dict, agora: float) -> dict:
    return {**{k: d[k] for k in ("tf", "direction", "estado", "cci1", "cci2", "cci_atual", "forca",
                                  "candles_desde_swing2", "volume_ratio", "taker_buy_ratio",
                                  "ts1", "ts2", "boundary_ts")},
            "dado_velho": dado_velho(d["boundary_ts"], d["tf"], agora)}


def _oi_out(d: dict, agora: float) -> dict:
    return {**{k: d[k] for k in ("tf", "boundary_ts", "price", "open_interest", "oi_notional",
                                  "price_change_pct", "oi_change_pct", "relation")},
            "dado_velho": dado_velho(d["boundary_ts"], d["tf"], agora)}


def _gatilho_out(s: dict | None, agora: float) -> dict | None:
    """Resumo do último gatilho do ativo, com o cruzamento, o resultado e se uma
    regra de alerta o suprimiu."""
    if not s:
        return None
    return {
        "tipo": s["tipo"], "tf": s["tf"], "lado": s["lado"], "score": s.get("score"),
        "ts": s["ts"], "boundary_ts": s.get("boundary_ts"),
        "idade_min": max(0, int((agora * 1000 - s["ts"]) // 60_000)),
        "preco": s.get("preco"), "direcao": s.get("direcao"),
        "zona_fundo": s.get("zona_fundo"), "zona_topo": s.get("zona_topo"),
        "div_tf": s.get("div_tf"), "div_idade_min": s.get("div_idade_min"),
        "div_forca": s.get("div_forca"), "div_estado": s.get("div_estado"),
        "div_conflito": s.get("div_conflito"),
        "n_divs_conf": s.get("n_divs_conf"), "n_divs_contra": s.get("n_divs_contra"),
        "suprimido": s.get("suprimido") or 0, "motivo": s.get("motivo"),
        "fav": s.get("max_fav_pct"), "adv": s.get("max_adv_pct"),
        "ret": s.get("ret_pct"), "bench": s.get("bench_pct"), "excesso": s.get("excesso_pct"),
        "r_1h": s.get("r_1h"), "r_4h": s.get("r_4h"), "r_12h": s.get("r_12h"),
        "r_24h": s.get("r_24h"), "n_candles": s.get("n_candles"),
    }


def _passa_filtro_gatilho(g: dict | None, so_div: str | None) -> bool:
    """`so_div`: gatilho | confluente | conflito | sem_div (None = sem filtro)."""
    if not so_div or so_div == "todos":
        return True
    if not g:
        return False
    confluente = bool(g["div_tf"])
    conflito = bool(g["div_conflito"])
    if so_div == "confluente":
        return confluente and not conflito
    if so_div == "conflito":
        return conflito
    if so_div == "sem_div":
        return not confluente and not conflito
    return True


# ── Score (transparente: cada ponto tem um motivo) ───────────
# Peso por TF: o TF maior define o viés, o menor dá o gatilho.
PESO_TF = {"15m": 1, "1h": 2, "4h": 3, "12h": 3}
PTS_ESTADO = {"armada": 1, "confirmada": 2, "ativa": 3, "fraca": 0.5}
CONFLITO_MIN = 0.35     # dominância mínima (0-1) para o ativo aparecer com um lado só
PENALIDADE_CONTRA = 0.5  # fração dos pontos do lado oposto que desconta do lado dominante


def _lado_div(d: dict) -> str:
    return "short" if d["direction"] == "bearish" else "long"


def _bonus_zona(z: dict | None, lado: str) -> tuple[float, str | None]:
    """Zona de fibo perto e a favor do lado vale mais que zona longe."""
    if not z or not z["visivel"]:
        return 0.0, None
    if z["vies_rejeicao"] == lado:
        if z["dist_pct"] <= 1:
            return 3.0, f"zona {z['tf']} ({z['papel']}) a {z['dist_pct']}%: +3"
        if z["dist_pct"] <= 3:
            return 2.0, f"zona {z['tf']} ({z['papel']}) a {z['dist_pct']}%: +2"
        return 1.0, f"zona {z['tf']} ({z['papel']}) a {z['dist_pct']}%: +1"
    if z["papel"] == "dentro":
        return 1.0, f"preço dentro da zona {z['tf']}: +1"
    return 0.0, None


def _avaliar(lista, z1h, z15, oi_por_tf=None) -> dict:
    """Consolida TODAS as divergências do ativo em UM lado (ou conflito)."""
    pts = {"long": 0.0, "short": 0.0}
    mot = {"long": [], "short": []}
    tfs = {"long": set(), "short": set()}
    for d in lista:
        lado = _lado_div(d)
        p = PTS_ESTADO[d["estado"]] * PESO_TF[d["tf"]]
        pts[lado] += p
        tfs[lado].add(d["tf"])
        mot[lado].append(f"div {d['tf']} {d['estado']}: +{p:g}")
        if d["estado"] != "fraca" and d["volume_ratio"] < 0.8:
            pts[lado] += 0.5
            mot[lado].append(f"volume seco no swing {d['tf']} ({d['volume_ratio']:.2f}x): +0.5")
    dom = "long" if pts["long"] > pts["short"] else "short"
    opp = "short" if dom == "long" else "long"
    total = pts[dom] + pts[opp]
    dominancia = (pts[dom] - pts[opp]) / total if total else 0.0

    score, motivos = pts[dom], list(mot[dom])
    if pts[opp]:
        pen = PENALIDADE_CONTRA * pts[opp]
        score -= pen
        motivos.append(f"sinais {opp} ({', '.join(sorted(tfs[opp], key=ORDEM_TF.get))}): -{pen:g}")
    for z in (z1h, z15):
        b, txt = _bonus_zona(z, dom)
        if txt:
            score += b * (1 if z is z1h else 0.5)
            motivos.append(txt + ("" if z is z1h else " (x0.5)"))
    oi_por_tf = oi_por_tf or {}
    oi_pts = 0.0
    for tf, oi in oi_por_tf.items():
        rel = oi["relation"]
        favoravel = ((dom == "long" and rel == "preco_alta_oi_alta") or
                     (dom == "short" and rel == "preco_baixa_oi_alta"))
        contrario = ((dom == "long" and rel == "preco_baixa_oi_alta") or
                     (dom == "short" and rel == "preco_alta_oi_alta"))
        peso = 1.0 if tf in ("1h", "4h") else 0.5
        if favoravel:
            oi_pts += peso
            motivos.append(f"OI confirma {tf} ({rel}): +{peso:g}")
        elif contrario:
            oi_pts -= peso
            motivos.append(f"OI contradiz {tf} ({rel}): -{peso:g}")
    score += oi_pts

    if not tfs[opp]:
        tipo = "alinhado"
    elif dominancia < CONFLITO_MIN:
        tipo = "conflito"
    elif max(ORDEM_TF[t] for t in tfs[opp]) < min(ORDEM_TF[t] for t in tfs[dom]):
        tipo = "pullback"          # TF maior manda, TF menor é só o repique
    else:
        tipo = "misto"
    return {"lado": dom, "score": round(score, 1), "motivos": motivos, "tipo": tipo,
            "dominancia": round(dominancia, 2), "tfs_lado": tfs[dom]}


# ── Candidatos ───────────────────────────────────────────────
def montar_candidatos(divs, zonas, precos, lado="ambos", min_tfs=2, todas=False,
                      agora=None, incluir_zonas_ocultas=False, oi_snapshots=None,
                      sinais=None, so_gatilho_h=0, so_div=None) -> list[dict]:
    """UM candidato por ativo (antes saía um por ativo+direção -> mesma moeda em long e short).

    A lista é guiada pelas divergências; o último gatilho do Fusion entra colado no
    candidato (com o cruzamento e o resultado) para o painel mostrar sem fazer join.
    """
    agora = agora or time.time()
    aceitos = VIVOS | ({"fraca"} if todas else set())
    por_ativo: dict[str, list] = {}
    for d in divs:
        if d["estado"] in aceitos and d["symbol"] in precos:
            por_ativo.setdefault(d["symbol"], []).append(d)

    zmap = {(z["symbol"], z["tf"]): z for z in zonas}
    omap = {}
    for oi in oi_snapshots or []:
        omap.setdefault(oi["symbol"], {})[oi["tf"]] = oi
    corte_gatilho = agora - so_gatilho_h * 3600 if so_gatilho_h else None
    out = []
    for symbol, lista in por_ativo.items():
        preco = precos[symbol]
        z1h_full = zona_info(zmap.get((symbol, "1h")), preco, agora)
        z15_full = zona_info(zmap.get((symbol, "15m")), preco, agora)
        oi_full = omap.get(symbol, {})
        gat = _gatilho_out((sinais or {}).get(symbol), agora)
        if corte_gatilho and (not gat or gat["ts"] < corte_gatilho * 1000):
            continue
        if not _passa_filtro_gatilho(gat, so_div):
            continue
        av = _avaliar(lista, z1h_full, z15_full, oi_full)
        if lado != "ambos" and av["lado"] != lado:
            continue
        if len(av["tfs_lado"]) < min_tfs or (av["tipo"] == "conflito" and not todas):
            continue
        z1h, z15 = z1h_full, z15_full
        if not incluir_zonas_ocultas:
            z1h = z1h if z1h and z1h["visivel"] else None
            z15 = z15 if z15 and z15["visivel"] else None
        out.append({
            "symbol": symbol, "lado": av["lado"], "preco": preco, "score": av["score"],
            "tipo": av["tipo"], "dominancia": av["dominancia"], "motivos": av["motivos"],
            "compat": bool(z1h and z1h["vies_rejeicao"] == av["lado"]), "n_tfs": len(av["tfs_lado"]),
            "divs": [_div_out(d, agora) for d in sorted(lista, key=lambda d: ORDEM_TF[d["tf"]])],
            "oi": [_oi_out(oi, agora) for oi in sorted(oi_full.values(), key=lambda x: ORDEM_TF.get(x["tf"], 99))],
            "zona_1h": z1h, "zona_15m": z15, "gatilho": gat,
            "tv": f"https://www.tradingview.com/chart/?symbol=BINANCE:{symbol}.P",
            "binance": f"https://www.binance.com/en/futures/{symbol}",
        })
    out.sort(key=lambda c: (c["score"], c["n_tfs"]), reverse=True)
    return out


def detalhe_ativo(symbol, divs, zonas, precos, agora=None) -> dict:
    agora = agora or time.time()
    s = symbol.upper()
    preco = precos.get(s)
    return {
        "symbol": s, "preco": preco,
        "divs": [_div_out(d, agora) for d in sorted((d for d in divs if d["symbol"] == s),
                                                     key=lambda d: ORDEM_TF[d["tf"]])],
        "zonas": ([zona_info(z, preco, agora) for z in sorted((z for z in zonas if z["symbol"] == s),
                                                              key=lambda z: ORDEM_TF[z["tf"]])]
                  if preco else []),
        "oi": [_oi_out(oi, agora) for oi in sorted((oi for oi in store.ultimas_oi() if oi["symbol"] == s),
                                                    key=lambda oi: ORDEM_TF.get(oi["tf"], 99))],
        # histórico: o que o painel antigo não tinha como mostrar
        "eventos": store.div_eventos(s, 40),
        "sinais": store.sinais(s, 40),
    }


# ── Contexto do BTC (trend, volume, momentum) ────────────────
def _trend(df) -> dict:
    close = df["close"]
    e20, e50 = close.ewm(span=20, adjust=False).mean().iloc[-1], close.ewm(span=50, adjust=False).mean().iloc[-1]
    p = float(close.iloc[-1])
    tendencia = "alta" if p > e50 and e20 > e50 else "baixa" if p < e50 and e20 < e50 else "lateral"
    rsi = fibo._rsi(df).iloc[-1]
    vol = df["volume"]
    ref = vol.iloc[-21:-1].mean()
    return {"tendencia": tendencia, "ema20": float(e20), "ema50": float(e50),
            "rsi": None if rsi != rsi else round(float(rsi), 1),
            "volume_ratio": round(float(vol.iloc[-1] / ref), 2) if ref else None}


async def contexto_btc(client, symbol="BTCUSDT") -> dict:
    """Consulta ao vivo (1h e 4h + ticker 24h). Chamado com cache curto pela API."""
    agora_ms = client.agora() * 1000 if hasattr(client, "agora") else time.time() * 1000
    tfs = {}
    for tf in ("1h", "4h"):
        df = scanner.klines_para_df(await client.klines(symbol, tf, 99), agora_ms)
        tfs[tf] = _trend(df)
    t = await client.ticker24h(symbol)
    return {"symbol": symbol, "preco": float(t["lastPrice"]), "var_24h_pct": float(t["priceChangePercent"]),
            "high_24h": float(t["highPrice"]), "low_24h": float(t["lowPrice"]),
            "volume_24h_usdt": float(t["quoteVolume"]), "tfs": tfs}


def snapshot(precos, lado="ambos", min_tfs=2, todas=False, limite=60, agora=None,
             so_gatilho_h=0, so_div=None) -> dict:
    agora = agora or time.time()
    desde = int(agora - max(so_gatilho_h, 24) * 3600)      # 24 h de folga p/ o filtro
    cands = montar_candidatos(store.todas_divergencias(), store.todas_zonas(), precos,
                              lado, min_tfs, todas, agora, oi_snapshots=store.ultimas_oi(),
                              sinais=store.ultimos_sinais_por_simbolo(desde),
                              so_gatilho_h=so_gatilho_h, so_div=so_div)
    scans = store.estado_scans()
    return {"gerado_em": int(agora), "total": len(cands), "candidatos": cands[:limite],
            "placar": store.placar_por_tipo(),
            "confluencia": store.resumo_confluencia(),
            "scans": {tf: {"boundary_ts": s["boundary_ts"], "finished_at": s["finished_at"],
                           "n_erros": s["n_erros"], "n_symbols": s["n_symbols"]} for tf, s in scans.items()}}
