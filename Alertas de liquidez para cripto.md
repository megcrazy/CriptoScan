# Alertas de liquidez para cripto

## Fluxo

1. **SETUP**: score do Titanium Fusion atinge o mínimo dentro de uma zona válida.
2. **SWEEP**: candle fechado rompe a máxima/mínima do **dia UTC anterior** ou do **range 4h anterior**, fechando novamente para dentro.
3. **ROMPIMENTO CONFIRMADO**: candle posterior fecha além da máxima/mínima do candle do sweep.

Para manter a convenção do Pine atual:

- sweep de máxima + rompimento acima da máxima do sweep = **COMPRA**;
- sweep de mínima + rompimento abaixo da mínima do sweep = **VENDA**.

As sessões não são usadas como pontos do score nem como níveis de liquidez.

## Configuração

Copie o conteúdo de `alerts.env.example` para as variáveis do serviço ou do shell:

```bash
export TELEGRAM_BOT_TOKEN="token_do_bot"
export TELEGRAM_CHAT_ID="@meu_canal_ou_id"
export ALERT_SCORE_MIN=60
export SWEEP_VOLUME_MULT=1.5
export SWEEP_EXPIRE_BARS=30
export ALERT_TIMEFRAMES="15m,1h,4h"
export ALERT_STATE_PATH="/opt/cripto/alert_state.json"
```

Sem `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`, o scanner continua funcionando e apenas não envia mensagens.

O bot precisa ser administrador do canal para publicar. O `TELEGRAM_CHAT_ID` pode ser um `@username` público ou o ID numérico do chat.

## Execução

```bash
python run_scanner.py
```

O estado de deduplicação é salvo em `alert_state.json`. Não apagar esse arquivo durante a operação, pois ele impede o reenvio de setup/sweep antigos após reinício.

## Score

O score replica os pesos do Pine, sem sessão:

- Fibonacci: 20
- Sweep: 25
- OI: 20 quando disponível
- Volume: 10
- RSI/PWR: 10
- Convergência: 5
- FVG: 10

O score é normalizado somente pelos componentes disponíveis, como no Pine quando OI não existe.

## Observação operacional

A primeira leitura após a partida não deve ser tratada como uma entrada retroativa: o estado é persistido e os alertas são emitidos em transições novas. Sempre validar o comportamento em paper trade antes de usar uma mensagem como decisão operacional.
