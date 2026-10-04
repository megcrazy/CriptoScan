# Fusion — alertas do Titanium Fusion v2.1 no scanner cripto

Substitui o `liquidity_alerts.py` (que deixa de ser importado). O `alert_state.json` antigo não é mais usado.

## O que cada alerta significa
| Alerta | Condição |
|---|---|
| **COMPRA / VENDA (setup)** | score ≥ mínimo **com o candle tocando a zona 0,5–0,618** e papel definido pelo lado de chegada (veio por cima = suporte = compra; veio por baixo = resistência = venda) |
| **SW** (silencioso) | sweep alinhado ao papel da zona (compra = sweep da **mínima** da sessão anterior; venda = sweep da **máxima**). Só avisa se a zona foi tocada nas últimas 12 velas |
| **BUY / SELL** | fechamento além do extremo do sweep oposto (como os marcadores do Pine). Mesma regra de "zona tocada" |

## Liquidez: sessões, como no Pine
Liquidez = máxima e mínima da **sessão anterior** — Ásia 00–06, Londres 07–10, NY 13–16 UTC;
a sessão seguinte observa o range da anterior (`Fusion_v2.pine:289-321`), igual ao gráfico.
`FUSION_LIQUIDEZ=dia` volta ao modo antigo (máx/mín do dia UTC anterior), mas aí o nível fica
3–4x mais longe do preço e o componente sweep quase nunca pontua — era a causa de sinais que o
gráfico mostrava e o bot não mandava (ZAMA 1h, 2026-10-02 00:00 UTC).

## O fib ATIVO tem memória (`fusion.fib_ativo`)
O padrão ABC ativo é guardado por `symbol:tf` em `fusion_state.json`, como as variáveis
`activeFib*` do Pine:
- troca quando aparece um `pattern_id` **novo** e válido;
- **não** é descartado quando o último triplo de pivôs deixa de ser um ABC válido (o
  `detectar_extensao` devolve `None` nesse caso, mas a fib anterior continua valendo);
- é apagado só quando um fechamento confirmado rompe o C, e a vigia começa na barra
  **seguinte** à confirmação do pivô C (o Pine isenta a barra em que o padrão virou ativo).

## Alerta de setup: um por PADRÃO, não por lado
O `scoreSignalNew` do Pine não depende da direção: enquanto o score ficar ≥ mínimo na zona,
virar de SUPORTE para RESISTÊNCIA **não** redispara. A chave é `symbol:tf:setup:{pattern_id}`
e ela é rearmada quando o sinal cai.

## Score (pesos do Pine)
Fib/zona 20 · sweep 25 · OI 20 · volume 10 · PWR 10 · convergência 5 · FVG 10 → normalizado 0–100.
O **teto só perde o peso do OI** quando o ativo comprovadamente não tem OI (o Pine faz o mesmo
com o ticker `_OI`). Falha de rede, rate limit ou ativo novo sem 30 diários **não** tiram peso do
denominador: o componente apenas não pontua. Sem isso o score infla sozinho e o mínimo de 60
deixa de valer justamente quando a rede está ruim.
PWR = média do RSI14 em 1m,3m,5m,15m,30m,1h,4h,1d; convergência = desvio-padrão ≤ 18.

## OI alinhado pelo FECHAMENTO da vela
`FUSION_OI_REF="fechamento"` (não voltar para `"abertura"`). O Pine usa
`oiChange = close - close[1]` da série `_OI`, ou seja a variação **dentro** da vela atual.
Alinhando pela abertura, o `alinhar_oi` mede a variação da vela **anterior** e o contexto sai
com 1 vela de atraso — foi isso que gerou o "VENDA" falso do DOGE em 2026-10-02 01:30 UTC.

## Proteções
- Partida a frio: primeiro scan, ou volta depois de >2 velas de pausa (PC desligado), só registra o estado — não envia nada como se fosse novo.
- Só marca como enviado depois que o Telegram confirma. Falhou = tenta de novo no próximo scan. 429 respeita `retry_after`.
- Teto de 25 alertas por rodada (`FUSION_MAX_ALERTAS_POR_SCAN`); o excedente vira uma linha de resumo silenciosa.
- Estado em `fusion_state.json` (use caminho absoluto no serviço: `FUSION_STATE_PATH`), com poda de 7 dias.
- `FUSION_FIB_IDADE_MAX` (93 velas) limita a idade do C do fib ativo: a janela do scanner é de 99 velas e o painel já esconde zonas mais velhas que `ZONA_MAX_IDADE_C`.

## Histórico: `div_eventos` e `sinais`
O `divergences` é um retrato do agora (`PRIMARY KEY (symbol, tf, direction)`, apagado a
cada scan). Duas tabelas append-only guardam a história:

- **`div_eventos`** — uma linha por evento de divergência. O `ts2` (swing mais novo) é a
  identidade; `estado_inicial` e `preco_em` são o registro de nascença e nunca são
  reescritos, `estado_final`/`visto_em` acompanham a evolução e congelam quando a
  divergência deixa de ser reportada.
- **`sinais`** — cada gatilho realmente notificado, já com o cruzamento com o log:
  `div_tf`, `div_idade_min`, `div_forca`, `div_estado`, `div_conflito`, `n_divs_conf`,
  `n_divs_contra`. O join é calculado na hora do gatilho e desnormalizado na linha, mas
  as tabelas cruas ficam — dá para refazer a janela depois.

`store.resumo_confluencia()` devolve o placar (quantos gatilhos com/sem divergência, com
conflito, e a idade média da divergência no gatilho). O `/api/ativo/{symbol}` já devolve
`eventos` e `sinais` do ativo.

Retenção: `DIV_EVENTOS_DIAS` (padrão 180). Medido com replay de 30 símbolos na janela real
do scanner: ~5.900 eventos novos/dia nos 4 TFs, a 183 bytes/linha com índices = ~1 MB/dia.
Janela do cruzamento: `SINAL_DIV_JANELA_MIN` (padrão 7 dias — no caso do CLO a divergência
nasceu ~2 dias antes do gatilho).

## Resultado dos gatilhos
`store.calcular_resultados_sinais()` mede o que aconteceu depois de cada gatilho
usando a série de preços que o próprio scan grava em `oi_snapshots` (close + high/low
por candle fechado): **zero requisição nova** — 197 gatilhos em 0,02 s nos testes.
Grava `max_fav_pct` (máxima favorável), `max_adv_pct` (máxima adversa) e os horizontes
`r_1h/4h/12h/24h`, todos com o sinal do lado (positivo = a favor do gatilho). Roda no
`run_scanner.py` a cada 2 min; sinais com mais de 26 h congelam.
Nas linhas gravadas antes desta versão não há high/low, então a medida cai no close
(`COALESCE`) — mais conservadora, e melhora sozinha conforme o scan vai gravando.

`store.placar_por_tipo()` é o placar que responde "isso paga?": agrupa por tipo de
gatilho, TF, lado e confluência (`confluente`, `confluente+conflito`, `so conflito`,
`sem div`) com `n`, quantas vezes a máxima favorável superou a adversa e as médias.
O `r_24h` só existe depois de 24 h; as colunas de favorável/adversa valem desde o
primeiro minuto.

## Painel (`web/index.html`)
Duas colunas novas: **Gatilho** (tipo, TF, há quanto tempo, score e o selo
`div ✔` / `div ✔⚠` / `div ⚠` / `div ✗`) e **Resultado** (▲favorável ▼adversa + os
horizontes já fechados). Filtros: `Gatilho ≤6h/≤24h/≤7d` e
`Div: qualquer / confluente / contrária / sem`. Clicar na linha abre um drawer com o
histórico do ativo (gatilhos com resultado, divergências atuais, linha do tempo dos
eventos, zonas e OI) via `/api/ativo/{symbol}`. O bloco 📊 Placar fica no topo
(`/api/placar`).

## Regras de alerta: registrar sempre, enviar por regra
`settings.FUSION_REGRAS` decide o que vira mensagem. **Nada configurado = alerta
tudo** (comportamento original). Chave = `(tipo, tf, lado)` com `"*"` de curinga
(aceita `"setup:15m:1"` também); a regra mais específica vence campo a campo.
Campos: `bloquear`, `min_score`, `sem_conflito`, `exigir_div`.

```python
FUSION_REGRAS = {
    ("*", "*", "*"):        {"sem_conflito": True},   # nenhum alerta com div contrária
    ("sweep", "*", 1):      {"bloquear": True},       # corta os sweep de compra
    ("setup", "15m", 1):    {"min_score": 75},        # compra no setup 15m exige 75
}
```

O ponto central: **todo candidato entra na tabela `sinais`, com `suprimido` e
`motivo`**, mesmo quando não vai para o Telegram. Se só os enviados ficassem no
banco, o Placar viraria um espelho (só mostraria sobreviventes) e não haveria como
saber o que a regra evitou — nem o que ela custou. O Placar traz as colunas `sup`
lado a lado: **se o grupo suprimido tiver ▲>▼ alto, a regra está jogando dinheiro
fora**.

## Testes

`python teste_fusion.py` — 92 verificações com dados sintéticos, sem rede. Cobre sweep de dia x
sessão, FVG, OI (incluindo o atraso de 1 vela do `"abertura"`), teto do score, memória e
invalidação do fib ativo, edge trigger do setup, partida a frio, o log append-only de
divergências, o cruzamento divergência x gatilho, a medição de resultado dos gatilhos,
as regras de alerta (inclusive gravar o suprimido com o resultado medido igual) e a
integração com `scanner.py`/`store.py`.

## Para conferir com dado real (não tenho acesso à Binance daqui)
1. Rode primeiro sem `TELEGRAM_*` — os alertas saem no console — e compare alguns com o Pine no gráfico.
2. `diagnostico/repro_fusion.py`, `diagnostico/diag_liq.py` e `diagnostico/pine_score.py` reproduzem
   o motor com o histórico real da Binance, barra a barra.
3. Valide em paper antes de usar como decisão.
