# ============================================================
#  binance_client.py — Cliente REST assíncrono da Binance Futures
#  Respeita o rate limit lendo o peso real usado (header do servidor),
#  então não depende de eu ter acertado o peso de cada endpoint.
# ============================================================
import asyncio
import time

import httpx

import settings


class BinanceClient:
    def __init__(self):
        self._http = httpx.AsyncClient(base_url=settings.BASE_URL, timeout=15)
        self._sem = asyncio.Semaphore(settings.MAX_CONCURRENCY)
        self._peso_usado = 0
        self._bloqueado_ate = 0.0
        self._offset_s = 0.0          # relógio do servidor - relógio local

    def agora(self) -> float:
        """Hora corrigida pelo relógio da Binance (protege contra VPS fora de hora)."""
        return time.time() + self._offset_s

    async def sincronizar_relogio(self):
        t0 = time.time()
        data = await self._get("/fapi/v1/time")
        t1 = time.time()
        self._offset_s = data["serverTime"] / 1000 - (t0 + t1) / 2
        if abs(self._offset_s) > 1:
            print(f"[Binance] relógio local difere {self._offset_s:+.1f}s do servidor — corrigindo")

    async def close(self):
        await self._http.aclose()

    async def _respeitar_limite(self):
        agora = time.time()
        if agora < self._bloqueado_ate:
            await asyncio.sleep(self._bloqueado_ate - agora)
        if self._peso_usado >= settings.WEIGHT_SOFT_LIMIT:
            # janela de peso da Binance zera a cada minuto
            await asyncio.sleep(60 - (time.time() % 60) + 0.5)
            self._peso_usado = 0

    async def _get(self, path: str, params: dict | None = None):
        ultimo_erro = None
        for tentativa in range(4):
            await self._respeitar_limite()
            async with self._sem:
                try:
                    r = await self._http.get(path, params=params)
                except httpx.HTTPError as e:
                    ultimo_erro = e
                    await asyncio.sleep(1 + tentativa)
                    continue
            peso = r.headers.get("x-mbx-used-weight-1m")
            if peso:
                self._peso_usado = int(peso)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (418, 429):
                espera = int(r.headers.get("retry-after", 120 if r.status_code == 418 else 30))
                self._bloqueado_ate = time.time() + espera
                ultimo_erro = f"HTTP {r.status_code}"
                print(f"[Binance] {r.status_code} — pausando {espera}s")
                continue
            if r.status_code >= 500:                      # 502/503/504: instabilidade, tenta de novo
                ultimo_erro = f"HTTP {r.status_code}"
                await asyncio.sleep(1 + tentativa * 2)
                continue
            r.raise_for_status()                          # 4xx restantes: erro do pedido, não repete
        raise RuntimeError(f"Falha em {path} após 4 tentativas: {ultimo_erro}")

    # ── Endpoints ───────────────────────────────────────────
    async def universo(self) -> list[tuple[str, float]]:
        """[(symbol, quoteVolume24h)] dos perpétuos USDT líquidos, do maior pro menor."""
        info = await self._get("/fapi/v1/exchangeInfo")
        perp = {
            s["symbol"] for s in info["symbols"]
            if s["status"] == "TRADING" and s["contractType"] == "PERPETUAL" and s["quoteAsset"] == "USDT"
        }
        tick = await self._get("/fapi/v1/ticker/24hr")
        vols = {t["symbol"]: float(t["quoteVolume"]) for t in tick if t["symbol"] in perp}
        lista = sorted(
            ((s, v) for s, v in vols.items() if v >= settings.MIN_QUOTE_VOLUME_24H),
            key=lambda x: x[1], reverse=True,
        )
        if settings.MAX_SYMBOLS > 0:
            lista = lista[: settings.MAX_SYMBOLS]
        return lista

    async def klines(self, symbol: str, interval: str, limit: int | None = None) -> list:
        return await self._get(
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": limit or settings.KLINES_LIMIT},
        )

    async def precos(self) -> dict[str, float]:
        data = await self._get("/fapi/v1/ticker/price")
        return {d["symbol"]: float(d["price"]) for d in data}

    async def ticker24h(self, symbol: str) -> dict:
        return await self._get("/fapi/v1/ticker/24hr", {"symbol": symbol})

    async def open_interest(self, symbol: str) -> dict:
        """Open Interest atual do perpétuo, em quantidade da moeda base."""
        return await self._get("/fapi/v1/openInterest", {"symbol": symbol})

    async def open_interest_hist(self, symbol: str, period: str, limit: int = 30) -> list:
        """Histórico de OI (period: 5m,15m,30m,1h,2h,4h,6h,12h,1d). Usado só para ativos na zona."""
        return await self._get("/futures/data/openInterestHist",
                               {"symbol": symbol, "period": period, "limit": limit})
