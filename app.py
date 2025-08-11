# app.py
from __future__ import annotations
import csv
import io
import os
import time
from typing import Dict, Tuple, List

import requests
from flask import Flask, request, jsonify
import unidecode

app = Flask(__name__)

# =========================
# Config via ambiente
# =========================
# Link público do Google Sheets (export CSV) contendo as colunas:
#   material, preco
# ou  materiais, preco
MATERIAIS_URL = os.getenv("MATERIAIS_URL", "").strip()

# Cache para evitar baixar a planilha a cada request
CACHE_TTL = int(os.getenv("CACHE_TTL", "300"))  # 5 min padrão
_CACHE_DATA: Dict[str, int] = {}
_CACHE_RAW_NAMES: Dict[str, str] = {}
_CACHE_TS: float = 0.0


# =========================
# Normalização de nomes
# =========================
def normaliza_nome(txt: str) -> str:
    """
    - minúsculo
    - sem acento
    - remove ' de ' isolado
    - troca hífen por espaço
    - compacta espaços
    """
    t = unidecode.unidecode(txt.strip().lower())
    t = t.replace(" de ", " ")
    t = t.replace("-", " ")
    t = " ".join(t.split())
    return t


# =========================
# Carregar / cachear planilha
# =========================
def _baixar_planilha(url: str) -> Tuple[Dict[str, int], Dict[str, str]]:
    """
    Baixa CSV e devolve:
      - dict normalizado->preco_int
      - dict normalizado->nome_original (para mensagens)
    """
    if not url:
        raise RuntimeError("MATERIAIS_URL não configurada nas variáveis de ambiente.")

    r = requests.get(url, timeout=20)
    r.raise_for_status()

    data_norm_to_price: Dict[str, int] = {}
    data_norm_to_raw: Dict[str, str] = {}

    f = io.StringIO(r.text)
    reader = csv.DictReader(f)

    # aceita cabeçalhos: "material" ou "materiais" (singular/plural) e "preco"
    possible_name_cols = ["material", "materiais", "nome", "nome_material"]
    name_col = None
    for c in reader.fieldnames or []:
        c2 = c.strip().lower()
        if c2 in possible_name_cols:
            name_col = c
    if not name_col:
        raise RuntimeError(
            "Cabeçalho não encontrado. Esperado colunas 'material' (ou 'materiais') e 'preco'."
        )

    # acha coluna de preço
    price_col = None
    for c in reader.fieldnames or []:
        if c.strip().lower() in ("preco", "preço", "price"):
            price_col = c
    if not price_col:
        raise RuntimeError("Coluna de preço não encontrada (ex.: 'preco').")

    for row in reader:
        raw_name = (row.get(name_col) or "").strip()
        raw_price = (row.get(price_col) or "").strip()
        if not raw_name:
            continue
        try:
            # aceita 1497 ou 1497,00 ou 1.497 etc.
            p = (
                raw_price.replace(".", "")
                .replace("R$", "")
                .replace(" ", "")
                .replace(",", ".")
            )
            price_int = int(round(float(p)))
        except Exception:
            continue

        norm = normaliza_nome(raw_name)
        data_norm_to_price[norm] = price_int
        data_norm_to_raw[norm] = raw_name

    if not data_norm_to_price:
        raise RuntimeError("Nenhum material válido encontrado no CSV.")

    return data_norm_to_price, data_norm_to_raw


def _garantir_cache() -> None:
    global _CACHE_DATA, _CACHE_RAW_NAMES, _CACHE_TS
    now = time.time()
    if now - _CACHE_TS > CACHE_TTL or not _CACHE_DATA:
        data, raw = _baixar_planilha(MATERIAIS_URL)
        _CACHE_DATA = data
        _CACHE_RAW_NAMES = raw
        _CACHE_TS = now


# =========================
# Endpoints
# =========================
@app.route("/ping")
def ping():
    return jsonify({"ok": True, "service": "api-precos-dinamicos"})

@app.route("/materiais")
def materiais():
    """
    Lista materiais disponíveis e seus preços base.
    """
    try:
        _garantir_cache()
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

    itens = [
        {"material": _CACHE_RAW_NAMES[k], "preco": v}
        for k, v in sorted(_CACHE_DATA.items())
    ]
    return jsonify({"materiais": itens, "fonte": MATERIAIS_URL})


@app.route("/preco")
def preco():
    """
    Exemplo de chamada:
      /preco?materiais=Couro Bovino, Couro de Tilápia, Jeans
    Regra: preço = média simples dos preços base.
    """
    q = request.args.get("materiais", "").strip()
    if not q:
        return jsonify({"erro": "Parâmetro 'materiais' é obrigatório."}), 400

    try:
        _garantir_cache()
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

    orig_list = [m.strip() for m in q.split(",") if m.strip()]
    if not orig_list:
        return jsonify({"erro": "Nenhum material informado."}), 400

    norm_list = [normaliza_nome(m) for m in orig_list]

    desconhecidos: List[str] = []
    usados: List[Dict[str, int]] = []
    soma = 0

    for n, original in zip(norm_list, orig_list):
        if n not in _CACHE_DATA:
            desconhecidos.append(original)
            continue
        price = _CACHE_DATA[n]
        usados.append({"material": _CACHE_RAW_NAMES[n], "preco": price})
        soma += price

    if desconhecidos:
        # sugere catálogo disponível
        disponiveis = sorted(_CACHE_RAW_NAMES.values())
        return (
            jsonify(
                {
                    "erro": "Materiais desconhecidos.",
                    "nao_encontrados": desconhecidos,
                    "sugestoes_disponiveis": disponiveis,
                }
            ),
            400,
        )

    # Fórmula de precificação: média simples
    media = soma / max(len(usados), 1)
    preco_final = int(round(media))

    return jsonify(
        {
            "materiais": [u["material"] for u in usados],
            "itens_precificados": usados,  # detalha base usada
            "regra": "media_simples",
            "preco": preco_final,
        }
    )


# -------------------------
# Exec local
# -------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
