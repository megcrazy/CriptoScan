# ============================================================
#  fib_papel.py — Papel da zona de fibo em relação ao preço ATUAL
#
#  Regra do trader: a zona 0,5–0,618 acima do preço age como RESISTÊNCIA
#  (mesmo numa projeção de alta); abaixo do preço age como SUPORTE
#  (um repique pode ir além). O papel muda conforme o preço anda, então
#  é calculado na hora da leitura, não gravado.
# ============================================================


def papel_zona(zona: dict, preco: float) -> dict:
    """
    zona: dict com direcao ('alta'|'baixa'), c, zona_fundo, zona_topo.
    Retorna: papel ('resistencia'|'suporte'|'dentro'), vies_rejeicao ('short'|'long'|None),
             dist_pct (distância do preço até a borda mais próxima da zona, em %),
             invalidada (preço já perdeu o ponto C do padrão).
    """
    fundo, topo = zona["zona_fundo"], zona["zona_topo"]

    if preco < fundo:
        papel, vies, dist = "resistencia", "short", (fundo - preco) / preco * 100
    elif preco > topo:
        papel, vies, dist = "suporte", "long", (preco - topo) / preco * 100
    else:
        papel, vies, dist = "dentro", None, 0.0

    if zona["direcao"] == "alta":
        invalidada = preco < zona["c"]
    else:
        invalidada = preco > zona["c"]

    return {"papel": papel, "vies_rejeicao": vies, "dist_pct": round(dist, 2), "invalidada": invalidada}
