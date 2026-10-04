# ============================================================
#  settings.py — Configurações do scanner cripto (Binance Futures)
# ============================================================
import os
from pathlib import Path


def _carregar_env(caminho: Path) -> None:
    """Lê o .env da pasta do projeto sem depender do python-dotenv.

    Rodando direto da pasta (`python run_scanner.py`) as variáveis do .env não
    entram no ambiente sozinhas — quem fazia isso era o `EnvironmentFile` do
    systemd. Nunca sobrescreve o que já existe no ambiente, então systemd e
    `export` continuam tendo prioridade."""
    try:
        linhas = caminho.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for linha in linhas:
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        chave, valor = chave.strip(), valor.strip().strip('"').strip("'")
        if chave and chave not in os.environ:
            os.environ[chave] = valor


_carregar_env(Path(__file__).parent / ".env")

BASE_URL = os.getenv("BINANCE_FAPI_URL", "https://fapi.binance.com")
DB_PATH = Path(__file__).parent / "cripto.db"

# ── Universo ─────────────────────────────────────────────────
# Só perpétuos USDT em TRADING. Filtra por volume 24h (em USDT) e corta nos N maiores.
MIN_QUOTE_VOLUME_24H = float(os.getenv("MIN_QUOTE_VOLUME_24H", 2_000_000))
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", 0))       # 0 = sem limite
UNIVERSE_REFRESH_S = 3600

# ── Timeframes ───────────────────────────────────────────────
# div: calcula divergência CCI | fib: calcula zona de extensão ABC
TIMEFRAMES = {
    "15m": {"seconds": 900,   "div": True, "fib": True},
    "1h":  {"seconds": 3600,  "div": True, "fib": True},
    "4h":  {"seconds": 14400, "div": True, "fib": True},
    "12h": {"seconds": 43200, "div": True, "fib": False},
}
# Ordem de prioridade quando vários fecham juntos (ex.: 00:00 UTC fecha os 4)
SCAN_ORDER = ["15m", "1h", "4h", "12h"]
# 15m é o gatilho operacional; 1h/4h ficam disponíveis para contexto.
ALERT_TIMEFRAMES = tuple(os.getenv("ALERT_TIMEFRAMES", "15m,1h,4h").split(","))

# ── Fusion (Titanium Fusion v2.1, sem sessões) ───────────────
# Mesmos nomes de variável de ambiente que você já usava.
FUSION_SCORE_MIN = float(os.getenv("ALERT_SCORE_MIN", 60))
FUSION_SWEEP_VOL_MULT = float(os.getenv("SWEEP_VOLUME_MULT", 1.5))
FUSION_SWEEP_VALIDADE = int(os.getenv("SWEEP_EXPIRE_BARS", 30))
# No serviço, use caminho absoluto (ex.: /opt/cripto/fusion_state.json).
FUSION_STATE_PATH = os.getenv("FUSION_STATE_PATH", "fusion_state.json")
FUSION_EXIGIR_C_DENTRO = True       # C acima do A na alta / abaixo na baixa
FUSION_ARMADO_BARRAS = 12           # SW e BUY/SELL só avisam se a zona foi tocada nas últimas N velas
FUSION_SWEEP_JANELA = 0             # 0 = sweep só vale na própria vela (igual ao Pine)
FUSION_SWEEP_SILENCIOSO = True      # SW chega sem som no Telegram
FUSION_RSI_TFS = ["1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"]   # mesmos 8 do Pine
# Alinhamento do OI histórico. O Pine usa oiChange = close - close[1] da série _OI,
# ou seja a variação DENTRO da vela atual. Com "abertura" o alinhar_oi mede a vela
# anterior (1 vela de atraso) e o contexto de OI sai trocado — era a causa do
# "VENDA" falso do DOGE em 2026-10-02 01:30 UTC. Não voltar para "abertura".
FUSION_OI_REF = "fechamento"        # "abertura" ou "fechamento" da vela
# De onde vem a liquidez varrida: "sessao" = a sessão anterior (Ásia 00-06,
# Londres 07-10, NY 13-16 UTC), que é o que o Fusion_v2.pine usa | "dia" =
# máxima/mínima do dia UTC anterior (adaptação antiga do scanner).
# Com "dia" o nível fica tipicamente 3-4x mais longe do preço, então o componente
# sweep (25 dos 100 pontos) quase nunca pontua. Foi o que deixou o ZAMA 1h em
# 35/100 sem avisar nada em 2026-10-02 00:00 UTC, enquanto o gráfico mostrava
# COMPRA (sweep da mínima de NY em 0.07661). Com "sessao" esse alerta aparece.
# Para voltar ao comportamento antigo: FUSION_LIQUIDEZ=dia no ambiente.
FUSION_LIQUIDEZ = os.getenv("FUSION_LIQUIDEZ", "sessao")
# Idade máxima do C (em velas) para o fib ativo continuar valendo. O Pine não tem
# esse corte, mas a janela do scanner é de 99 velas e o painel já esconde zonas
# com C mais velho que ZONA_MAX_IDADE_C.
FUSION_FIB_IDADE_MAX = int(os.getenv("FUSION_FIB_IDADE_MAX", 93))
# False desliga a invalidação do fib pelo rompimento do C (fibInvalidate do Pine).
FUSION_INVALIDAR_NO_C = True

# ── Regras de alerta (o que vira mensagem no Telegram) ───────
# Nada configurado = alerta TUDO (comportamento original). Todo candidato é
# gravado na tabela `sinais` de qualquer jeito, com `suprimido` e `motivo`, então
# o Placar mostra lado a lado o que foi enviado e o que a regra cortou — é assim
# que se descobre se o corte valeu a pena, em vez de acreditar nele.
#
# Chave: (tipo, tf, lado) com "*" de curinga; lado usa +1/-1.
# O que a regra mais específica disser vence, campo a campo.
# Campos aceitos:
#   bloquear    True  -> nunca alerta
#   min_score   float -> exige score >= isso
#   sem_conflito True -> não alerta se existir QUALQUER divergência contrária
#   sem_conflito_predominante True -> só cai quando as contrárias SUPERAM as a favor
#   exigir_div  True  -> exige divergência confluente na janela
#   dist_zona_max float -> corta se o preço estiver mais que isso (%) fora da zona
#
# ── O que a medição diz hoje (03/10) ─────────────────────────
# ATENÇÃO: o número que circulou antes (t=3,0 para `sem_conflito_predominante`) era
# FALSO. Ele saiu de linhas corrompidas pelo bug da PK, que deixava o `lado` de um
# disparo e o preço/divergência de outro. Refazendo só com dado confiável:
#   sem_conflito_predominante: t=0,38 no que é comprovadamente limpo (n=266) -> NADA.
#   Por isso essa regra NÃO está recomendada e nada está ligado.
#
# O que apareceu com significância foi outra coisa — a DISTÂNCIA ATÉ A ZONA no
# BREAKOUT. O `inFibZone` do Pine testa se a VELA tocou a zona (`low <= topo and
# high >= fundo`), não se o preço está nela; numa vela que abre na zona e dispara, o
# alerta sai com o preço já longe (caso extremo: AINUSDT 1h, COMPRA a 63,9% acima da
# zona, stop de ATR a 42% e nenhum alvo). Medido em 419 gatilhos confiáveis:
#   breakout perto (<3%): n=83  excesso −0,27%  acerto 49%
#   breakout longe (≥3%): n=20  excesso −2,48%  acerto 20%   t = −2,75  SIGNIFICATIVO
#   setup  longe: n=12  +0,80%  t=0,48  (nada)   |   sweep longe: n=15  +1,49%  t=0,88
# Setups longe são raros (6% passam de 3%, 0,5% de 10%) e não foram piores. A mensagem
# já avisa quando o preço está ≥3% fora da zona, em qualquer tipo.
#
# ── LIGADO em 03/10: breakout longe da zona ──────────────────
# Único filtro deste projeto que passou na auditoria (tirar outliers -> só período
# limpo -> mediana em vez de média). Corta 34 de 239 breakouts (14%) que somam
# excesso −4,13% com 18% de acerto; o que fica dá +0,43% com 47%.
# O limiar de 2% cortaria mais (65) e deixaria o resto em +0,48%, mas NÃO foi
# auditado — a diferença no que fica é 0,05pp, ruído. Fica 3% e, se valer a pena
# cortar mais, o 2% é testado como se deve antes.
# Tudo que for suprimido entra em `sinais` com motivo `regra:zona>3%`, então em
# dois dias o Placar mostra se as colunas `sup` ficaram mesmo piores.
FUSION_REGRAS: dict = {
    ("breakout", "*", "*"): {"dist_zona_max": 3.0},
}
FUSION_MAX_ALERTAS_POR_SCAN = 25    # teto por rodada (evita rajada/429 do Telegram)
FUSION_GAP_CANDLES = 2              # pausa maior que isso = partida a frio (não envia o que já existia)

# ── Divergência CCI (mesmos números do scanner Node) ─────────
DIV_DEFAULT = {
    "cci_period": 20,
    "swing_window": 2,
    "min_swing_distance": 3,
    "volume_avg_window": 20,
    "recency_window_candles": 40,
    "max_signal_age_candles": 8,
    # No Node era 1, mas com swing_window=2 o swing2 só é confirmado 2 candles
    # depois — então "armada" nunca disparava. Aqui = 2 (recém-confirmada).
    "armada_max_candles": 2,
    "ativa_min_cci_move": 50,
}
DIV_OVERRIDE: dict[str, dict] = {}   # ex.: {"15m": {"ativa_min_cci_move": 60}}


def div_cfg(tf: str) -> dict:
    return {**DIV_DEFAULT, **DIV_OVERRIDE.get(tf, {})}


# ── Histórico (div_eventos / sinais) ─────────────────────────
# A tabela `divergences` é um retrato do agora (PK symbol,tf,direction, apagada a
# cada scan). `div_eventos` é append-only: uma linha por evento (o ts2 identifica
# a divergência) e o estado evolui no UPDATE. É o que permite, na hora do gatilho,
# saber se havia divergência e há quantos minutos ela nasceu.
# Medido com replay de 30 símbolos na janela real do scanner: nascem ~5.900
# eventos novos por dia nos 4 TFs, a 183 bytes/linha (com índices) = ~1 MB/dia.
DIV_EVENTOS_DIAS = int(os.getenv("DIV_EVENTOS_DIAS", 180))       # ~190 MB no regime
# A série de preços/OI por candle (`oi_snapshots`) é a tabela que mais cresce:
# ~44 mil linhas/dia no universo cheio (~350 símbolos x 4 TFs). Ela também é a
# base do cálculo de resultado dos gatilhos.
OI_DIAS = int(os.getenv("OI_DIAS", 120))
# Janela do cruzamento divergência -> gatilho. Generosa de propósito: no exemplo
# que motivou isso (CLO) a divergência nasceu ~2 dias antes do gatilho.
SINAL_DIV_JANELA_MIN = int(os.getenv("SINAL_DIV_JANELA_MIN", 7 * 24 * 60))

# ── Requisições ──────────────────────────────────────────────
KLINES_LIMIT = 99           # <100 costa peso 1 na Binance (100+ = peso 2); o candle em formação é descartado
CLOSE_DELAY_S = 3           # espera após o fechamento antes de escanear
MAX_CONCURRENCY = 8
# Se o peso usado no minuto (header x-mbx-used-weight-1m) passar disso, espera o próximo minuto.
# O teto da Binance é 2400/min por IP — confira na documentação atual.
WEIGHT_SOFT_LIMIT = int(os.getenv("WEIGHT_SOFT_LIMIT", 1800))

# ── Painel ───────────────────────────────────────────────────
ZONA_MAX_IDADE_C = int(os.getenv("ZONA_MAX_IDADE_C", 60))   # zonas com C mais velho que isso são escondidas
DADO_VELHO_FATOR = 2      # dado é "velho" se boundary_ts < agora - 2 candles do TF
RUN_RETRY_S = 15          # pausa após erro no loop do runner
