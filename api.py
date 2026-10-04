# ============================================================
#  api.py — Painel web (FastAPI). Rode:
#  uvicorn api:app --host 127.0.0.1 --port 8000
#  Lê o SQLite (escrito pelo run_scanner.py) e cruza com preços ao vivo.
#
#  É ESTE o app que o serviço carrega. Se um dia aparecer outro arquivo com um
#  `app` igual, apague: já aconteceu de editar o arquivo errado e o painel novo
#  ficar servindo payload velho em silêncio.
#
#  Autenticação: define PAINEL_AUTH_USER e PAINEL_AUTH_PASSWORD para exigir
#  HTTP Basic. Sem as duas variáveis o painel fica aberto (como sempre foi).
# ============================================================
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import painel
import store
from binance_client import BinanceClient

WEB = Path(__file__).parent / "web"
TTL_PRECOS, TTL_BTC = 10, 30

AUTH_USER = os.getenv("PAINEL_AUTH_USER", "")
AUTH_PASSWORD = os.getenv("PAINEL_AUTH_PASSWORD", "")
PROTEGER = bool(AUTH_USER and AUTH_PASSWORD)
security = HTTPBasic()


def exigir_autenticacao(credentials: HTTPBasicCredentials = Depends(security)):
    usuario_ok = secrets.compare_digest(credentials.username, AUTH_USER)
    senha_ok = secrets.compare_digest(credentials.password, AUTH_PASSWORD)
    if not (usuario_ok and senha_ok):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Credenciais inválidas",
                            headers={"WWW-Authenticate": "Basic"})
    return credentials.username


class _Cache:
    def __init__(self):
        self.v = {}

    async def get(self, chave, ttl, fn):
        t, val = self.v.get(chave, (0, None))
        if time.time() - t > ttl:
            try:
                val = await fn()
                self.v[chave] = (time.time(), val)
            except Exception:
                if val is None:        # sem valor antigo para devolver
                    raise
        return val


def criar_app(client=None) -> FastAPI:
    """client injetável (testes usam um cliente falso)."""
    propria = client is None

    @asynccontextmanager
    async def lifespan(app):
        store.init()
        app.state.client = client or BinanceClient()
        app.state.cache = _Cache()
        yield
        if propria:
            await app.state.client.close()

    # A dependência só entra quando as credenciais estão configuradas.
    app = FastAPI(title="Scanner Cripto", lifespan=lifespan,
                  dependencies=[Depends(exigir_autenticacao)] if PROTEGER else None)

    async def _precos():
        c = app.state.client
        return await app.state.cache.get("precos", TTL_PRECOS, c.precos)

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    @app.get("/api/painel")
    async def api_painel(lado: str = Query("ambos", pattern="^(short|long|ambos)$"),
                         min_tfs: int = Query(2, ge=1, le=4), todas: bool = False,
                         limite: int = Query(60, ge=1, le=300),
                         so_gatilho_h: int = Query(0, ge=0, le=720),
                         so_div: str = Query("todos",
                                             pattern="^(todos|gatilho|confluente|conflito|sem_div)$")):
        try:
            precos = await _precos()
        except Exception as e:
            raise HTTPException(503, f"Binance indisponível: {e}")
        return painel.snapshot(precos, lado, min_tfs, todas, limite,
                               so_gatilho_h=so_gatilho_h, so_div=so_div)

    @app.get("/api/placar")
    async def api_placar(horas: int = Query(168, ge=1, le=24 * 365),
                         horas_mercado: int = Query(8, ge=1, le=168)):
        """Acerto por tipo de gatilho x lado x conflito de divergência, com o
        contexto de mercado (quanto o universo andou e para que lado o scanner
        está inclinado)."""
        desde = int(time.time()) - horas * 3600
        return {"horas": horas, "por_tipo": store.placar_por_tipo(desde),
                "confluencia": store.resumo_confluencia(),
                "mercado": store.contexto_mercado(desde, horas_mercado),
                "ultimos": store.sinais_com_resultado(60, desde)}

    @app.get("/api/gatilhos")
    async def api_gatilhos(limite: int = Query(80, ge=1, le=500),
                           horas: int = Query(24, ge=1, le=24 * 90)):
        """Feed cronológico dos gatilhos — TODOS, inclusive os suprimidos por regra.

        É diferente do painel: lá a lista nasce das divergências e só entra ativo com
        2+ TFs concordando. Aqui é "o que o bot disparou", na ordem em que disparou,
        sem depender de o ativo estar qualificado agora. `total` conta a janela
        inteira, mesmo quando o limite corta a lista."""
        desde = int(time.time()) - horas * 3600
        return {"horas": horas, "limite": limite,
                "total": store.contar_sinais(desde),
                "suprimidos": store.contar_sinais(desde, suprimidos=True),
                "gatilhos": store.sinais_com_resultado(limite, desde)}

    @app.get("/api/btc")
    async def api_btc():
        c = app.state.client
        try:
            ctx = await app.state.cache.get("btc", TTL_BTC, lambda: painel.contexto_btc(c))
        except Exception as e:
            raise HTTPException(503, f"Binance indisponível: {e}")
        precos = await _precos()
        det = painel.detalhe_ativo("BTCUSDT", store.todas_divergencias(), store.todas_zonas(), precos)
        return {**ctx, "divs": det["divs"], "zonas": det["zonas"], "oi": det.get("oi", [])}

    @app.get("/api/ativo/{symbol}")
    async def api_ativo(symbol: str):
        precos = await _precos()
        if symbol.upper() not in precos:
            raise HTTPException(404, "símbolo desconhecido")
        return painel.detalhe_ativo(symbol, store.todas_divergencias(), store.todas_zonas(), precos)

    return app


app = criar_app()
