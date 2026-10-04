# ============================================================
#  store.py — Cache SQLite das divergências e zonas de fibo
#  Só muda quando um candle fecha; o painel/alertas leem daqui.
#
#  Duas camadas:
#   - retrato do agora: `divergences` e `zones` (apagadas e reescritas a cada scan);
#   - histórico append-only: `div_eventos` (uma linha por evento de divergência,
#     identificado pelo ts2) e `sinais` (cada gatilho notificado, já cruzado com
#     o log de divergências). É o que permite responder "esse gatilho tinha
#     divergência, e há quanto tempo ela nasceu?".
# ============================================================
import re
import sqlite3
import statistics
import time

import settings

_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings.DB_PATH, timeout=10, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")      # scanner escreve, painel lê, sem travar
        _conn.execute("PRAGMA busy_timeout=10000")
        _conn.execute("PRAGMA synchronous=NORMAL")
    return _conn


def init():
    c = _db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS divergences (
        symbol TEXT NOT NULL, tf TEXT NOT NULL, direction TEXT NOT NULL,
        ts1 INTEGER, ts2 INTEGER, cci1 REAL, cci2 REAL, cci_atual REAL, forca REAL,
        estado TEXT, candles_desde_swing2 INTEGER,
        volume_ratio REAL, taker_buy_ratio REAL, preco REAL, boundary_ts INTEGER,
        PRIMARY KEY (symbol, tf, direction)
    );
    CREATE TABLE IF NOT EXISTS zones (
        symbol TEXT NOT NULL, tf TEXT NOT NULL, direcao TEXT,
        a REAL, b REAL, c REAL, impulso REAL, zona_fundo REAL, zona_topo REAL,
        alvo_1000 REAL, alvo_1618 REAL, pattern_id TEXT, c_time INTEGER, idade_c INTEGER,
        preco REAL, boundary_ts INTEGER,
        PRIMARY KEY (symbol, tf)
    );
    CREATE TABLE IF NOT EXISTS scan_state (
        tf TEXT PRIMARY KEY, boundary_ts INTEGER, finished_at INTEGER,
        n_symbols INTEGER, n_div INTEGER, n_zones INTEGER, n_erros INTEGER
    );
    CREATE TABLE IF NOT EXISTS universe (
        symbol TEXT PRIMARY KEY, quote_volume REAL, updated_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS oi_snapshots (
        symbol TEXT NOT NULL, tf TEXT NOT NULL, boundary_ts INTEGER NOT NULL,
        observed_at INTEGER NOT NULL, price REAL NOT NULL,
        open_interest REAL NOT NULL, oi_notional REAL,
        price_change_pct REAL, oi_change_pct REAL, relation TEXT NOT NULL,
        PRIMARY KEY (symbol, tf, boundary_ts)
    );
    CREATE INDEX IF NOT EXISTS idx_oi_latest ON oi_snapshots(symbol, tf, boundary_ts DESC);

    -- ── Histórico (append-only) ──────────────────────────────
    -- Uma linha por EVENTO de divergência. O ts2 (swing mais novo) identifica a
    -- divergência; o estado (armada/confirmada/ativa/...) evolui e é atualizado
    -- na própria linha. `estado_inicial` e `preco_em` são o registro de nascença
    -- e nunca são reescritos.
    CREATE TABLE IF NOT EXISTS div_eventos (
        symbol TEXT NOT NULL, tf TEXT NOT NULL, direction TEXT NOT NULL,
        ts2 INTEGER NOT NULL, ts1 INTEGER NOT NULL,
        detectado_em INTEGER NOT NULL, visto_em INTEGER NOT NULL,
        cci1 REAL, cci2 REAL, forca REAL,
        volume_ratio REAL, taker_buy_ratio REAL,
        estado_inicial TEXT, estado_final TEXT,
        preco_em REAL,
        PRIMARY KEY (symbol, tf, direction, ts2)
    );
    CREATE INDEX IF NOT EXISTS idx_div_eventos_sym ON div_eventos(symbol, ts2 DESC);
    CREATE INDEX IF NOT EXISTS idx_div_eventos_visto ON div_eventos(visto_em);

    -- Gatilhos do Fusion realmente notificados, já com o cruzamento com o log de
    -- divergências calculado NA HORA do gatilho (o painel não precisa fazer join).
    CREATE TABLE IF NOT EXISTS sinais (
        symbol TEXT NOT NULL, tf TEXT NOT NULL, tipo TEXT NOT NULL,
        lado INTEGER NOT NULL,
        ts INTEGER NOT NULL,              -- boundary do candle, em ms
        boundary_ts INTEGER NOT NULL,     -- o mesmo boundary, em segundos
        preco REAL, score REAL, pattern_id TEXT,
        zona_fundo REAL, zona_topo REAL, direcao TEXT,
        chave TEXT NOT NULL, enviado_em INTEGER,
        div_tf TEXT, div_idade_min INTEGER, div_forca REAL, div_estado TEXT,
        div_conflito INTEGER, n_divs_conf INTEGER, n_divs_contra INTEGER,
        -- A chave TEM que incluir o boundary: a `chave` do setup é o pattern_id, que
        -- vive horas. Sem o boundary, cada novo alerta do MESMO padrão sobrescrevia a
        -- linha anterior — o histórico sumia e a linha ficava com o `lado` do primeiro
        -- disparo e o preço do último. Caso real (EVAA, 02/10): o bot COMPROU o fundo
        -- em 0.6834 e o banco guardou como VENDA, contando −20% no lugar de +22%.
        PRIMARY KEY (symbol, tf, tipo, chave, boundary_ts)
    );
    CREATE INDEX IF NOT EXISTS idx_sinais_sym ON sinais(symbol, ts DESC);
    CREATE INDEX IF NOT EXISTS idx_sinais_ts ON sinais(ts DESC);
    """)
    _migrar(c)
    _migrar_pk_sinais(c)
    c.commit()


# Colunas acrescentadas depois que as tabelas já existiam em produção.
# `ALTER TABLE ADD COLUMN` é barato no SQLite e não mexe nas linhas existentes.
_COLUNAS_NOVAS = {
    "oi_snapshots": (("high", "REAL"), ("low", "REAL")),
    "sinais": (("max_fav_pct", "REAL"), ("max_adv_pct", "REAL"),
               ("r_1h", "REAL"), ("r_4h", "REAL"), ("r_12h", "REAL"), ("r_24h", "REAL"),
               ("n_candles", "INTEGER"), ("atualizado_em", "INTEGER"),
               ("suprimido", "INTEGER"), ("motivo", "TEXT"),
               # sem o desconto do mercado o placar mede a direção do mercado, não
               # o gatilho: numa perna de baixa todo short "acerta"
               ("ret_pct", "REAL"), ("bench_pct", "REAL"), ("excesso_pct", "REAL")),
}


def _migrar(c: sqlite3.Connection) -> None:
    for tabela, colunas in _COLUNAS_NOVAS.items():
        try:
            existentes = {r[1] for r in c.execute(f"PRAGMA table_info({tabela})")}
        except sqlite3.OperationalError:
            continue
        if not existentes:
            continue
        for nome, tipo in colunas:
            if nome not in existentes:
                c.execute(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}")
                print(f"[Store] {tabela}.{nome} criada")
    # As linhas gravadas antes de a coluna `suprimido` existir ficaram NULL, e as
    # contas usavam `suprimido=0` — que no SQL NÃO casa com NULL. Resultado: o
    # placar mostrava "medidos 0" para todo mundo. Normaliza uma vez; depois o
    # WHERE não pega mais nada.
    try:
        n = c.execute("UPDATE sinais SET suprimido=0 WHERE suprimido IS NULL").rowcount
        if n:
            print(f"[Store] {n} sinais antigos normalizados para suprimido=0")
    except sqlite3.OperationalError:
        pass


def _migrar_pk_sinais(c: sqlite3.Connection) -> None:
    """Recria `sinais` com a PK por EVENTO (incluindo o boundary).

    A PK antiga era (symbol, tf, tipo, chave) e a `chave` do setup é o `pattern_id`,
    que identifica o PADRÃO de fib e vive horas. Então cada novo alerta do mesmo
    padrão sobrescrevia a linha anterior em vez de criar outra: o histórico sumia
    (o placar contava menos gatilhos do que foram enviados) e, como `lado` não estava
    no DO UPDATE, a linha ficava com o lado do PRIMEIRO disparo e o preço do último.

    Caso que revelou isso (EVAA, 02/10): o bot mandou 🟢 COMPRA em 0.6834 no fundo e
    o banco guardou `lado=-1`, contabilizando −20% no lugar de +22%.

    Reconstruir é a única forma de trocar a PK no SQLite. Roda dentro da transação do
    `init()`, então ou troca inteira ou não troca nada."""
    try:
        colunas = [r[1] for r in c.execute("PRAGMA table_info(sinais)")]
    except sqlite3.OperationalError:
        return
    if not colunas:
        return
    pk = {r[1] for r in c.execute("PRAGMA table_info(sinais)") if r[5]}
    # Comparação por CONJUNTO: o PRAGMA devolve na ordem de definição da tabela, não na
    # ordem da PK. Comparar a ordem fazia a tabela ser reconstruída em TODO start.
    if pk == {"symbol", "tf", "tipo", "chave", "boundary_ts"}:
        return
    antes = c.execute("SELECT COUNT(*) FROM sinais").fetchone()[0]
    # Deriva o CREATE do DDL que já existe, trocando só a PK: assim a tabela nova tem
    # EXATAMENTE as colunas da antiga (inclusive as que o _migrar foi adicionando ao
    # longo do tempo) — escrever o CREATE à mão já quebrou uma vez, na validação.
    ddl = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='sinais'"
                    ).fetchone()[0]
    ddl = re.sub(r"PRIMARY\s+KEY\s*\([^)]*\)",
                 "PRIMARY KEY (symbol, tf, tipo, chave, boundary_ts)", ddl)
    ddl = re.sub(r"boundary_ts\s+INTEGER(?!\s+NOT\s+NULL)", "boundary_ts INTEGER NOT NULL", ddl)
    ddl = ddl.replace("CREATE TABLE sinais", "CREATE TABLE sinais_nova", 1)
    c.execute("ALTER TABLE sinais RENAME TO _sinais_pk_antiga")
    c.execute(ddl)
    # boundary_ts NULL (nunca deveria, mas a coluna antiga permitia) cai no ts
    expr = ", ".join("COALESCE(boundary_ts, ts/1000)" if x == "boundary_ts" else x
                     for x in colunas)
    c.execute(f"INSERT INTO sinais_nova ({', '.join(colunas)}) "
              f"SELECT {expr} FROM _sinais_pk_antiga")
    depois = c.execute("SELECT COUNT(*) FROM sinais_nova").fetchone()[0]
    c.execute("DROP TABLE _sinais_pk_antiga")
    c.execute("ALTER TABLE sinais_nova RENAME TO sinais")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sinais_sym ON sinais(symbol, ts DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sinais_ts ON sinais(ts DESC)")
    print(f"[Store] sinais reconstruída com PK por evento ({depois} de {antes} linhas "
          f"preservadas)")


def salvar_universo(lista: list[tuple[str, float]]):
    c = _db()
    agora = int(time.time())
    c.execute("DELETE FROM universe")
    c.executemany("INSERT INTO universe VALUES (?,?,?)", [(s, v, agora) for s, v in lista])
    c.commit()


def salvar_scan(tf: str, boundary_ts: int, resultados: list[tuple], n_erros: int):
    """Salva sinais e, quando disponível, um snapshot de OI do fechamento.

    Aceita o formato antigo de 4 itens e o novo formato de 5 itens:
    ``(symbol, divs, zona, preco, oi_raw)``.
    """
    c = _db()
    n_div = n_zones = 0
    agora = int(time.time())
    with c:
        for resultado in resultados:
            symbol, divs, zona, preco = resultado[:4]
            oi_raw = resultado[4] if len(resultado) > 4 else None
            vela = resultado[6] if len(resultado) > 6 else None      # {"high","low"}
            c.execute("DELETE FROM divergences WHERE symbol=? AND tf=?", (symbol, tf))
            c.execute("DELETE FROM zones WHERE symbol=? AND tf=?", (symbol, tf))
            for d in divs:
                c.execute(
                    "INSERT INTO divergences VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (symbol, tf, d["direction"], d["ts1"], d["ts2"], d["cci1"], d["cci2"],
                     d["cci_atual"], d["forca"], d["estado"], d["candles_desde_swing2"],
                     d["volume_ratio"], d["taker_buy_ratio"], preco, boundary_ts),
                )
                # `divergences` acima é o retrato do agora; aqui fica a história
                _registrar_div_evento(c, symbol, tf, d, preco, agora)
                n_div += 1
            if zona:
                c.execute(
                    "INSERT INTO zones VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (symbol, tf, zona["direcao"], zona["a"], zona["b"], zona["c"], zona["impulso"],
                     zona["zona_fundo"], zona["zona_topo"], zona["alvo_1000"], zona["alvo_1618"],
                     zona["pattern_id"], zona["c_time"], zona["idade_c"], preco, boundary_ts),
                )
                n_zones += 1
            if preco is not None and oi_raw is not None:
                oi = float(oi_raw.get("openInterest", 0))
                if oi > 0:
                    anterior = c.execute(
                        "SELECT price, open_interest FROM oi_snapshots "
                        "WHERE symbol=? AND tf=? AND boundary_ts<? "
                        "ORDER BY boundary_ts DESC LIMIT 1",
                        (symbol, tf, boundary_ts),
                    ).fetchone()
                    p_pct = oi_pct = None
                    if anterior and anterior["price"] and anterior["open_interest"]:
                        p_pct = (float(preco) / anterior["price"] - 1) * 100
                        oi_pct = (oi / anterior["open_interest"] - 1) * 100
                    relation = classificar_preco_oi(p_pct, oi_pct)
                    c.execute(
                        "INSERT OR REPLACE INTO oi_snapshots "
                        "(symbol, tf, boundary_ts, observed_at, price, open_interest, "
                        " oi_notional, price_change_pct, oi_change_pct, relation, high, low) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (symbol, tf, boundary_ts, int(time.time()), float(preco), oi,
                         oi * float(preco), p_pct, oi_pct, relation,
                         None if vela is None else vela.get("high"),
                         None if vela is None else vela.get("low")),
                    )
        # remove símbolos que saíram do universo (senão ficam como "dado novo" para sempre)
        for tabela in ("divergences", "zones"):
            c.execute(f"DELETE FROM {tabela} WHERE tf=? AND symbol NOT IN (SELECT symbol FROM universe)", (tf,))
        c.execute(
            "INSERT OR REPLACE INTO scan_state VALUES (?,?,?,?,?,?,?)",
            (tf, boundary_ts, int(time.time()), len(resultados), n_div, n_zones, n_erros),
        )
    _podar_div_eventos(agora)
    return n_div, n_zones


# ── Histórico de divergências ────────────────────────────────
def _registrar_div_evento(c, symbol: str, tf: str, d: dict, preco, agora: int) -> None:
    """Upsert append-only: o evento é identificado pelo ts2 (swing mais novo).

    `detectado_em`, `estado_inicial` e `preco_em` são o registro de nascença e não
    são reescritos; `estado_final`/`visto_em` acompanham a evolução a cada scan e
    congelam quando a divergência deixa de ser reportada.
    """
    c.execute(
        """INSERT INTO div_eventos
             (symbol, tf, direction, ts2, ts1, detectado_em, visto_em,
              cci1, cci2, forca, volume_ratio, taker_buy_ratio,
              estado_inicial, estado_final, preco_em)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(symbol, tf, direction, ts2) DO UPDATE SET
              visto_em        = excluded.visto_em,
              estado_final    = excluded.estado_final,
              forca           = excluded.forca,
              cci2            = excluded.cci2,
              volume_ratio    = excluded.volume_ratio,
              taker_buy_ratio = excluded.taker_buy_ratio""",
        (symbol, tf, d["direction"], d["ts2"], d["ts1"], agora, agora,
         d["cci1"], d["cci2"], d["forca"], d["volume_ratio"], d["taker_buy_ratio"],
         d["estado"], d["estado"], preco),
    )


# ── Resultado dos gatilhos ───────────────────────────────────
HORIZONTES = (("r_1h", 1), ("r_4h", 4), ("r_12h", 12), ("r_24h", 24))
_JANELA_RESULTADO_S = 26 * 3600          # sinais mais velhos que isso congelam
_cache_mercado: dict[tuple, dict] = {}


def _serie_mercado(tf: str) -> dict[int, float]:
    """Índice equal-weighted do universo: em cada candle, a mediana do retorno de
    cada símbolo desde o primeiro preço visto dele.

    É a referência para separar "o gatilho acertou" de "o mercado andou". Sem isso
    o placar mede a direção do mercado: numa perna de baixa todo short acerta e
    toda compra perde, e parece que o gatilho é bom (ou ruim) quando não é.
    Guardado em cache por (tf, último candle) — só recalcula quando fecha vela."""
    ult = _db().execute("SELECT MAX(boundary_ts) m FROM oi_snapshots WHERE tf=?",
                        (tf,)).fetchone()["m"]
    if not ult:
        return {}
    chave = (tf, int(ult))
    if chave in _cache_mercado:
        return _cache_mercado[chave]
    rows = _db().execute(
        "SELECT boundary_ts, symbol, price FROM oi_snapshots "
        "WHERE tf=? AND price>0 AND boundary_ts>=? ORDER BY boundary_ts",
        (tf, int(ult) - 10 * 86400)).fetchall()
    base: dict[str, float] = {}
    por_ts: dict[int, list[float]] = {}
    for r in rows:
        base.setdefault(r["symbol"], r["price"])
        if base[r["symbol"]]:
            por_ts.setdefault(int(r["boundary_ts"]), []).append(r["price"] / base[r["symbol"]])
    idx = {t: statistics.median(v) for t, v in por_ts.items() if len(v) >= 20}
    _cache_mercado.clear()
    _cache_mercado[chave] = idx
    return idx


def _fechamento(c, symbol: str, tf: str, alvo_ts: int) -> float | None:
    """Preço do primeiro candle fechado em/ou depois do alvo (o candle que contém o alvo)."""
    r = c.execute(
        "SELECT price FROM oi_snapshots WHERE symbol=? AND tf=? AND boundary_ts>=? "
        "ORDER BY boundary_ts LIMIT 1", (symbol, tf, alvo_ts)).fetchone()
    return r["price"] if r else None


def calcular_resultados_sinais(agora: float | None = None, limite: int = 800) -> int:
    """Mede o que aconteceu depois de cada gatilho, usando a série de preços que o
    próprio scanner grava em `oi_snapshots` (close + high/low por candle fechado).

    Zero requisição nova. Máxima favorável e máxima adversa são medidos no primeiro
    dia (cap de 24 h) e os horizontes de 1/4/12/24 h são o candle que contém o alvo.

    Também grava o retorno até agora (`ret_pct`), o que o mercado andou na mesma
    janela (`bench_pct`, pelo índice equal-weighted do universo) e a diferença
    (`excesso_pct`), que é o único número que diz se o gatilho tem valor próprio.
    Sinais com mais de 26 h ficam de fora: o resultado deles já está fechado.
    """
    agora = int(agora or time.time())
    c = _db()
    pend = c.execute(
        """SELECT symbol, tf, tipo, chave, lado, boundary_ts, preco
             FROM sinais
            WHERE preco IS NOT NULL AND preco > 0 AND boundary_ts >= ?
            ORDER BY boundary_ts ASC LIMIT ?""",
        (agora - _JANELA_RESULTADO_S, limite)).fetchall()
    indices = {tf: _serie_mercado(tf) for tf in {s["tf"] for s in pend}}
    n = 0
    with c:
        for s in pend:
            base, preco, lado = s["boundary_ts"], float(s["preco"]), int(s["lado"])
            # COALESCE: as linhas gravadas antes desta versão não têm high/low, então
            # caem no close. Degrada para uma medida mais conservadora em vez de NULL.
            m = c.execute(
                "SELECT MAX(COALESCE(high, price)) h, MIN(COALESCE(low, price)) l, COUNT(*) n, "
                "       MAX(boundary_ts) ult FROM oi_snapshots "
                "WHERE symbol=? AND tf=? AND boundary_ts>? AND boundary_ts<=?",
                (s["symbol"], s["tf"], base, base + 86400)).fetchone()
            fav = adv = None
            if m["h"] is not None and m["l"] is not None:
                fav = (m["h"] - preco) if lado == 1 else (preco - m["l"])
                adv = (preco - m["l"]) if lado == 1 else (m["h"] - preco)
            vals = [_fechamento(c, s["symbol"], s["tf"], base + h * 3600) for _, h in HORIZONTES]
            if not m["n"] and all(v is None for v in vals):
                continue                                   # ainda não há série desse ativo
            # retorno até o último candle medido, e o mercado na MESMA janela
            ult = c.execute(
                "SELECT price FROM oi_snapshots WHERE symbol=? AND tf=? AND boundary_ts>? "
                "AND boundary_ts<=? ORDER BY boundary_ts DESC LIMIT 1",
                (s["symbol"], s["tf"], base, base + 86400)).fetchone()
            ret = bench = exc = None
            idx = indices.get(s["tf"]) or {}
            if ult and ult["price"]:
                ret = (ult["price"] / preco - 1) * 100 * lado
                i0, i1 = idx.get(int(base)), idx.get(int(m["ult"]))
                if i0 and i1:
                    bench = (i1 / i0 - 1) * 100
                    exc = ret - lado * bench
            c.execute(
                """UPDATE sinais SET max_fav_pct=?, max_adv_pct=?,
                        r_1h=?, r_4h=?, r_12h=?, r_24h=?, n_candles=?, atualizado_em=?,
                        ret_pct=?, bench_pct=?, excesso_pct=?
                   WHERE symbol=? AND tf=? AND tipo=? AND chave=?""",
                (None if fav is None else round(fav / preco * 100, 3),
                 None if adv is None else round(adv / preco * 100, 3),
                 *[None if v is None else round((v / preco - 1) * 100 * (1 if lado == 1 else -1), 3)
                   for v in vals],
                 int(m["n"]), agora,
                 None if ret is None else round(ret, 3),
                 None if bench is None else round(bench, 3),
                 None if exc is None else round(exc, 3),
                 s["symbol"], s["tf"], s["tipo"], s["chave"]),
            )
            n += 1
    return n


def sinais_com_resultado(limite: int = 60, desde: int | None = None) -> list[dict]:
    """Gatilhos com o resultado, do mais novo para o mais velho."""
    if desde:
        rows = _db().execute(
            "SELECT * FROM sinais WHERE enviado_em>=? ORDER BY ts DESC LIMIT ?", (desde, limite))
    else:
        rows = _db().execute("SELECT * FROM sinais ORDER BY ts DESC LIMIT ?", (limite,))
    return [dict(r) for r in rows]


def contar_sinais(desde: int | None = None, suprimidos: bool | None = None) -> int:
    """Quantos gatilhos na janela. `suprimidos=True` conta só os cortados por regra;
    `False` só os enviados; `None` todos."""
    onde, args = [], []
    if desde:
        onde.append("enviado_em >= ?")
        args.append(desde)
    if suprimidos is not None:
        onde.append(f"COALESCE(suprimido,0) = {1 if suprimidos else 0}")
    sql = "SELECT COUNT(*) FROM sinais" + (" WHERE " + " AND ".join(onde) if onde else "")
    return _db().execute(sql, args).fetchone()[0]


# Quando dois gatilhos caem na mesma vela, o setup é o mais informativo.
_PRIO_TIPO = {"setup": 0, "breakout": 1, "sweep": 2}


def ultimos_sinais_por_simbolo(desde: int | None = None) -> dict[str, dict]:
    """Gatilho mais recente de cada símbolo (a partir de `desde`, epoch segundos)."""
    onde = "WHERE enviado_em >= ?" if desde else ""
    args = (desde,) if desde else ()
    rows = _db().execute(
        f"SELECT s.* FROM sinais s "
        f"  JOIN (SELECT symbol, MAX(ts) ts FROM sinais {onde} GROUP BY symbol) m "
        f"    ON s.symbol = m.symbol AND s.ts = m.ts", args).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        atual = out.get(d["symbol"])
        chave = (d["ts"], -_PRIO_TIPO.get(d["tipo"], 9))
        if atual is None or chave > (atual["ts"], -_PRIO_TIPO.get(atual["tipo"], 9)):
            out[d["symbol"]] = d
    return out


def placar_por_tipo(desde: int | None = None) -> list[dict]:
    """Acerto por tipo de gatilho, lado e confluência — o que responde 'isso paga?'.

    O agrupamento é por PREDOMÍNIO da divergência (a favor / empatada / contra /
    sem nenhuma), que é o corte que os dados mostraram informativo: "existe alguma
    divergência contrária" é grosseiro demais, o que importa é se ela domina.

    Enviados e suprimidos vêm lado a lado (`*_sup`), que é como se confere se a
    regra de alerta cortou o que devia.

    **O número que vale é `media_excesso`**, não `media_ret`: o retorno bruto inclui
    a direção do mercado (numa perna de baixa todo short "acerta"), e o excesso
    desconta o índice equal-weighted do universo na mesma janela. `r_24h` só existe
    depois de 24 h; `fav_maior` e as médias de favorável/adversa valem desde o
    primeiro minuto."""
    onde = "WHERE enviado_em >= ?" if desde else ""
    args = (desde,) if desde else ()
    return [dict(r) for r in _db().execute(f"""
        SELECT tipo, tf, lado,
               CASE WHEN COALESCE(n_divs_conf,0)+COALESCE(n_divs_contra,0)=0 THEN 'sem div'
                    WHEN COALESCE(n_divs_contra,0)>COALESCE(n_divs_conf,0) THEN 'div contra predomina'
                    WHEN COALESCE(n_divs_conf,0)>COALESCE(n_divs_contra,0) THEN 'div a favor'
                    ELSE 'div empatada' END grupo,
               COUNT(*) n,
               SUM(CASE WHEN COALESCE(suprimido,0)=1 THEN 1 ELSE 0 END) n_sup,
               COUNT(CASE WHEN COALESCE(suprimido,0)=0 THEN max_fav_pct END) n_medido,
               COUNT(CASE WHEN COALESCE(suprimido,0)=1 THEN max_fav_pct END) n_medido_sup,
               SUM(CASE WHEN COALESCE(suprimido,0)=0 AND max_fav_pct > max_adv_pct THEN 1 ELSE 0 END) fav_maior,
               SUM(CASE WHEN COALESCE(suprimido,0)=1 AND max_fav_pct > max_adv_pct THEN 1 ELSE 0 END) fav_maior_sup,
               COUNT(r_24h) n_r24,
               SUM(CASE WHEN r_24h > 0 THEN 1 ELSE 0 END) acertos,
               COUNT(CASE WHEN COALESCE(suprimido,0)=0 THEN excesso_pct END) n_exc,
               SUM(CASE WHEN COALESCE(suprimido,0)=0 AND excesso_pct>0 THEN 1 ELSE 0 END) exc_pos,
               ROUND(AVG(CASE WHEN COALESCE(suprimido,0)=0 THEN r_24h END), 3) media_r24,
               ROUND(AVG(CASE WHEN COALESCE(suprimido,0)=0 THEN ret_pct END), 3) media_ret,
               ROUND(AVG(CASE WHEN COALESCE(suprimido,0)=0 THEN excesso_pct END), 3) media_excesso,
               ROUND(AVG(CASE WHEN COALESCE(suprimido,0)=1 THEN excesso_pct END), 3) media_excesso_sup,
               ROUND(AVG(CASE WHEN COALESCE(suprimido,0)=0 THEN max_fav_pct END), 3) media_fav,
               ROUND(AVG(CASE WHEN COALESCE(suprimido,0)=0 THEN max_adv_pct END), 3) media_adv,
               ROUND(AVG(CASE WHEN COALESCE(suprimido,0)=1 THEN max_fav_pct END), 3) media_fav_sup,
               ROUND(AVG(CASE WHEN COALESCE(suprimido,0)=1 THEN max_adv_pct END), 3) media_adv_sup
          FROM sinais {onde}
         GROUP BY tipo, tf, lado, grupo
        HAVING COUNT(*) > 0
         ORDER BY n DESC""", args)]


def contexto_mercado(desde: int | None = None, horas: int = 8, tf: str = "15m") -> dict:
    """Quanto o mercado andou no período e para que lado o scanner está inclinado.

    Existe para o placar ser lido em contexto: se 70% dos gatilhos são de venda e o
    universo caiu 5%, o retorno bruto está inflado pela direção do mercado e não
    diz quase nada sobre o gatilho."""
    idx = _serie_mercado(tf)
    out: dict = {"janela_h": horas, "indice_pct": None, "btc_pct": None, "simbolos": len(idx)}
    if idx:
        ult = max(idx)
        ant = [k for k in idx if k <= ult - horas * 3600]
        if ant:
            ini = max(ant)
            out["indice_pct"] = round((idx[ult] / idx[ini] - 1) * 100, 2)
        btc = _db().execute(
            "SELECT price FROM oi_snapshots WHERE symbol='BTCUSDT' AND tf=? AND boundary_ts<=? "
            "ORDER BY boundary_ts DESC LIMIT 1", (tf, ult)).fetchone()
        if btc and ant:
            b0 = _db().execute(
                "SELECT price FROM oi_snapshots WHERE symbol='BTCUSDT' AND tf=? AND boundary_ts<=? "
                "ORDER BY boundary_ts DESC LIMIT 1", (tf, ini)).fetchone()
            if b0 and b0["price"]:
                out["btc_pct"] = round((btc["price"] / b0["price"] - 1) * 100, 2)

    onde = "WHERE enviado_em >= ?" if desde else ""
    args = (desde,) if desde else ()
    r = _db().execute(f"""SELECT SUM(CASE WHEN lado=1 THEN 1 ELSE 0 END) c,
                                 SUM(CASE WHEN lado=-1 THEN 1 ELSE 0 END) v,
                                 COUNT(*) n FROM sinais {onde}""", args).fetchone()
    n, compra = (r["n"] or 0), (r["c"] or 0)
    out.update({"compra": compra, "venda": (r["v"] or 0), "n": n})
    out["aviso"] = None
    if n >= 30:
        pv = out["venda"] / n * 100
        if pv >= 65:
            out["pct_venda"] = round(pv)
            out["aviso"] = (f"{pv:.0f}% dos gatilhos do período são de VENDA"
                            + (f" e o universo caiu {abs(out['indice_pct']):.1f}%"
                               if out.get("indice_pct") is not None and out["indice_pct"] < 0 else "")
                            + " — o retorno bruto está inflado pela direção do mercado. "
                              "Olhe a coluna de excesso.")
        elif pv <= 35:
            pc = 100 - pv
            out["pct_compra"] = round(pc)
            out["aviso"] = (f"{pc:.0f}% dos gatilhos do período são de COMPRA"
                            + (f" e o universo subiu {abs(out['indice_pct']):.1f}%"
                               if out.get("indice_pct") is not None and out["indice_pct"] > 0 else "")
                            + " — o retorno bruto está inflado pela direção do mercado. "
                              "Olhe a coluna de excesso.")
    return out


_ultima_poda = 0.0


def _podar_div_eventos(agora: int) -> None:
    """Retenção do log e da série de preços. Roda no máximo a cada 6 h, dentro do scan."""
    global _ultima_poda
    if agora - _ultima_poda < 6 * 3600:
        return
    _ultima_poda = agora
    corte = agora - settings.DIV_EVENTOS_DIAS * 86400
    corte_oi = agora - settings.OI_DIAS * 86400
    with _db() as c:
        n = c.execute("DELETE FROM div_eventos WHERE visto_em < ?", (corte,)).rowcount
        c.execute("DELETE FROM sinais WHERE enviado_em < ?", (corte,))
        # oi_snapshots é a tabela que mais cresce (~44 mil linhas/dia no universo cheio)
        oi = c.execute("DELETE FROM oi_snapshots WHERE boundary_ts < ?", (corte_oi,)).rowcount
    if n or oi:
        print(f"[Store] poda: {n} div_eventos (>{settings.DIV_EVENTOS_DIAS}d), "
              f"{oi} oi_snapshots (>{settings.OI_DIAS}d)")


# ── Gatilhos do Fusion ───────────────────────────────────────
def _direcao_do_lado(lado: int) -> str | None:
    return "bullish" if lado == 1 else "bearish" if lado == -1 else None


def cruzar_divergencia(ev: dict, boundary_ts: int) -> dict:
    """Cruza o gatilho com o log de divergências SEM gravar nada.

    Fica separado para o scanner poder decidir a regra de alerta antes de escrever
    (e gravar o veredito junto). Devolve os campos `div_*`/`n_divs_*`."""
    ts = int(boundary_ts) * 1000                       # ms, igual a div_eventos.ts2
    direcao = _direcao_do_lado(int(ev.get("lado") or 0))
    janela = settings.SINAL_DIV_JANELA_MIN * 60_000
    c = _db()
    out = {"div_tf": None, "div_idade_min": None, "div_forca": None, "div_estado": None,
           "div_conflito": 0, "n_divs_conf": 0, "n_divs_contra": 0}
    if not direcao:
        return out
    oposta = "bearish" if direcao == "bullish" else "bullish"
    base = "FROM div_eventos WHERE symbol=? AND ts2<=? AND ts2>=?"
    args = (ev["symbol"], ts, ts - janela)
    out["n_divs_conf"] = c.execute(f"SELECT COUNT(*) {base} AND direction=?",
                                   (*args, direcao)).fetchone()[0]
    out["n_divs_contra"] = c.execute(f"SELECT COUNT(*) {base} AND direction=?",
                                     (*args, oposta)).fetchone()[0]
    out["div_conflito"] = int(out["n_divs_contra"] > 0)
    conf = c.execute(
        f"SELECT tf, ts2, forca, estado_final {base} AND direction=? ORDER BY ts2 DESC LIMIT 1",
        (*args, direcao)).fetchone()
    if conf:
        out["div_tf"] = conf["tf"]
        out["div_idade_min"] = int((ts - conf["ts2"]) // 60_000)
        out["div_forca"] = conf["forca"]
        out["div_estado"] = conf["estado_final"]
    return out


def salvar_sinal(ev: dict, boundary_ts: int, cruzamento: dict | None = None,
                 suprimido: int = 0, motivo: str | None = None,
                 agora: float | None = None) -> dict | None:
    """Grava um gatilho — enviado OU suprimido por uma regra.

    Guardar os suprimidos é o que permite conferir depois se o corte valeu a pena:
    se só os enviados ficassem no banco, o Placar viraria um espelho e não haveria
    como saber o que a regra evitou (nem o que ela custou)."""
    agora = int(agora or time.time())
    ts = int(boundary_ts) * 1000
    cruz = cruzamento if cruzamento is not None else cruzar_divergencia(ev, boundary_ts)
    linha = {
        "symbol": ev["symbol"], "tf": ev["tf"], "tipo": ev["tipo"],
        "lado": int(ev.get("lado") or 0),
        "ts": ts, "boundary_ts": int(boundary_ts),
        "preco": ev.get("preco"), "score": ev.get("score"),
        "pattern_id": ev.get("pattern_id"),
        "zona_fundo": ev.get("zona_fundo"), "zona_topo": ev.get("zona_topo"),
        "direcao": ev.get("direcao"), "chave": ev["chave"], "enviado_em": agora,
        "suprimido": int(suprimido), "motivo": motivo,
        **cruz,
    }
    with _db() as c:
        c.execute(
            """INSERT INTO sinais
                 (symbol, tf, tipo, lado, ts, boundary_ts, preco, score, pattern_id,
                  zona_fundo, zona_topo, direcao, chave, enviado_em, suprimido, motivo,
                  div_tf, div_idade_min, div_forca, div_estado,
                  div_conflito, n_divs_conf, n_divs_contra)
               VALUES (:symbol,:tf,:tipo,:lado,:ts,:boundary_ts,:preco,:score,:pattern_id,
                       :zona_fundo,:zona_topo,:direcao,:chave,:enviado_em,:suprimido,:motivo,
                       :div_tf,:div_idade_min,:div_forca,:div_estado,
                       :div_conflito,:n_divs_conf,:n_divs_contra)
               ON CONFLICT(symbol, tf, tipo, chave, boundary_ts) DO UPDATE SET
                  lado=excluded.lado, ts=excluded.ts, boundary_ts=excluded.boundary_ts,
                  preco=excluded.preco, score=excluded.score, pattern_id=excluded.pattern_id,
                  zona_fundo=excluded.zona_fundo, zona_topo=excluded.zona_topo,
                  direcao=excluded.direcao, enviado_em=excluded.enviado_em,
                  suprimido=excluded.suprimido, motivo=excluded.motivo,
                  div_tf=excluded.div_tf, div_idade_min=excluded.div_idade_min,
                  div_forca=excluded.div_forca, div_estado=excluded.div_estado,
                  div_conflito=excluded.div_conflito, n_divs_conf=excluded.n_divs_conf,
                  n_divs_contra=excluded.n_divs_contra""",
            linha,
        )
    return linha


def classificar_preco_oi(price_change_pct: float | None, oi_change_pct: float | None) -> str:
    """Classifica o cruzamento usando um filtro de ruído de 0,10%."""
    if price_change_pct is None or oi_change_pct is None:
        return "sem_historico"
    eps = 0.10
    p = 1 if price_change_pct > eps else -1 if price_change_pct < -eps else 0
    o = 1 if oi_change_pct > eps else -1 if oi_change_pct < -eps else 0
    return {
        (1, 1): "preco_alta_oi_alta",
        (1, -1): "preco_alta_oi_baixa",
        (-1, 1): "preco_baixa_oi_alta",
        (-1, -1): "preco_baixa_oi_baixa",
        (0, 0): "neutro",
    }.get((p, o), "misto")


def ultimas_oi() -> list[dict]:
    """Retorna o último snapshot de OI por ativo e timeframe."""
    rows = _db().execute(
        "SELECT o.* FROM oi_snapshots o "
        "JOIN (SELECT symbol, tf, MAX(boundary_ts) boundary_ts "
        "      FROM oi_snapshots GROUP BY symbol, tf) x "
        "ON o.symbol=x.symbol AND o.tf=x.tf AND o.boundary_ts=x.boundary_ts"
    ).fetchall()
    return [dict(r) for r in rows]


def boundary_do_tf(tf: str) -> int:
    row = _db().execute("SELECT boundary_ts FROM scan_state WHERE tf=?", (tf,)).fetchone()
    return row["boundary_ts"] if row else 0


def todas_divergencias() -> list[dict]:
    return [dict(r) for r in _db().execute("SELECT * FROM divergences")]


def todas_zonas() -> list[dict]:
    return [dict(r) for r in _db().execute("SELECT * FROM zones")]


def estado_scans() -> dict[str, dict]:
    return {r["tf"]: dict(r) for r in _db().execute("SELECT * FROM scan_state")}


# ── Leituras do histórico ────────────────────────────────────
def div_eventos(symbol: str | None = None, limite: int = 200) -> list[dict]:
    """Eventos de divergência, do mais novo para o mais velho."""
    if symbol:
        rows = _db().execute(
            "SELECT * FROM div_eventos WHERE symbol=? ORDER BY ts2 DESC LIMIT ?",
            (symbol.upper(), limite))
    else:
        rows = _db().execute(
            "SELECT * FROM div_eventos ORDER BY detectado_em DESC LIMIT ?", (limite,))
    return [dict(r) for r in rows]


def sinais(symbol: str | None = None, limite: int = 200, desde: int | None = None) -> list[dict]:
    """Gatilhos notificados, do mais novo para o mais velho (`desde` em epoch segundos)."""
    if symbol:
        rows = _db().execute(
            "SELECT * FROM sinais WHERE symbol=? ORDER BY ts DESC LIMIT ?",
            (symbol.upper(), limite))
    elif desde:
        rows = _db().execute(
            "SELECT * FROM sinais WHERE enviado_em>=? ORDER BY ts DESC LIMIT ?", (desde, limite))
    else:
        rows = _db().execute("SELECT * FROM sinais ORDER BY ts DESC LIMIT ?", (limite,))
    return [dict(r) for r in rows]


def resumo_confluencia() -> dict:
    """Placar do cruzamento divergência x gatilho — o denominador que faltava."""
    c = _db()
    total = c.execute("SELECT COUNT(*) FROM sinais").fetchone()[0]
    sup = c.execute("SELECT COUNT(*) FROM sinais WHERE COALESCE(suprimido,0)=1").fetchone()[0]
    com = c.execute("SELECT COUNT(*) FROM sinais WHERE div_tf IS NOT NULL").fetchone()[0]
    conflito = c.execute("SELECT COUNT(*) FROM sinais WHERE div_conflito=1").fetchone()[0]
    idade = c.execute(
        "SELECT AVG(div_idade_min) m, MIN(div_idade_min) mi, MAX(div_idade_min) ma "
        "FROM sinais WHERE div_idade_min IS NOT NULL").fetchone()
    eventos = c.execute("SELECT COUNT(*) FROM div_eventos").fetchone()[0]
    return {
        "div_eventos": eventos, "sinais": total,
        "enviados": total - sup, "suprimidos": sup,
        "com_div": com, "sem_div": total - com, "com_div_conflito": conflito,
        "div_idade_min_media": None if idade["m"] is None else round(idade["m"]),
        "div_idade_min_min": idade["mi"], "div_idade_min_max": idade["ma"],
    }
