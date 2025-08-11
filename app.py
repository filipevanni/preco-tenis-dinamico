import os
import csv
import io
from typing import Dict, Tuple, List
from flask import Flask, request, jsonify
import requests
import unidecode

app = Flask(__name__)

# --------- Config ---------
CSV_URL = os.getenv("FONTE_MATERIAIS_CSV_URL")  # planilha publicada como CSV (materiais,preco)
TIMEOUT = 15  # segundos para baixar o CSV

# --------- Normalização única (planilha e entrada usam a mesma regra) ---------
def norm(txt: str) -> str:
    """
    Normaliza qualquer texto para comparação:
    - minúsculas
    - remove acentos
    - troca hífen por espaço
    - colapsa espaços
    """
    t = unidecode.unidecode((txt or "").strip().lower())
    t = t.replace("-", " ")
    t = " ".join(t.split())
    return t

# --------- Cache em memória ---------
# chave normalizada -> (nome_canonico, preco_int)
PRECOS: Dict[str, Tuple[str, int]] = {}

def carregar_precos_do_csv() -> None:
    """Baixa o CSV publicado e povoa o dicionário PRECOS normalizado."""
    global PRECOS
    if not CSV_URL:
        raise RuntimeError("Variável de ambiente FONTE_MATERIAIS_CSV_URL não configurada.")

    resp = requests.get(CSV_URL, timeout=TIMEOUT)
    resp.raise_for_status()

    content = resp.content.decode("utf-8", errors="ignore")
    reader = csv.DictReader(io.StringIO(content))

    precos_tmp: Dict[str, Tuple[str, int]] = {}
    for row in reader:
        nome = (row.get("materiais") or "").strip()
        preco_raw = (row.get("preco") or "").strip()

        if not nome or not preco_raw:
            continue

        # converte preço para int (remove possíveis separadores)
        preco_num = int(str(preco_raw).replace(".", "").replace(",", "").strip())

        chave = norm(nome)
        precos_tmp[chave] = (nome, preco_num)

    if not precos_tmp:
        raise RuntimeError("Nenhuma linha válida encontrada no CSV (colunas esperadas: 'materiais', 'preco').")

    PRECOS = precos_tmp

# carrega na inicialização
try:
    carregar_precos_do_csv()
except Exception as e:
    # Se falhar na subida, a primeira chamada tenta recarregar também
    print(f"[AVISO] Não foi possível carregar os preços ao iniciar: {e}")

def garantir_precos() -> None:
    """Recarrega o cache se estiver vazio (ex.: após cold start)."""
    if not PRECOS:
        carregar_precos_do_csv()

# --------- Regras de precificação ---------
def preco_media_simples(precos: List[int]) -> int:
    return round(sum(precos) / max(len(precos), 1))

REGRAS = {
    "media_simples": preco_media_simples,
    # se quiser, adicione outras regras aqui depois (ex.: soma, margem, etc.)
}

# --------- Endpoint ---------
@app.route("/preco", methods=["GET"])
def preco():
    """
    /preco?materiais=Couro%20Bovino,Couro%20de%20Til%C3%A1pia,Jeans&regra=media_simples
    """
    garantir_precos()

    materias_qs = request.args.get("materiais", "").strip()
    regra = request.args.get("regra", "media_simples").strip() or "media_simples"

    if not materias_qs:
        return jsonify({"erro": "Parâmetro 'materiais' é obrigatório."}), 400

    if regra not in REGRAS:
        return jsonify({"erro": f"Regra inválida. Use uma destas: {list(REGRAS.keys())}"}), 400

    # divide por vírgula
    pedidos = [m.strip() for m in materias_qs.split(",") if m.strip()]
    if not pedidos:
        return jsonify({"erro": "Nenhum material informado."}), 400

    itens_precificados = []
    precos_encontrados: List[int] = []
    nao_encontrados: List[str] = []

    for m in pedidos:
        k = norm(m)
        if k in PRECOS:
            nome_canonico, p = PRECOS[k]
            itens_precificados.append({"material": nome_canonico, "preco": p})
            precos_encontrados.append(p)
        else:
            nao_encontrados.append(m)

    if nao_encontrados:
        # sugiro opções válidas (os nomes canônicos do CSV)
        sugestoes = sorted(list({canon for (canon, _) in PRECOS.values()}))
        return jsonify({
            "erro": "Materiais desconhecidos.",
            "nao_encontrados": nao_encontrados,
            "sugestoes_disponiveis": sugestoes
        }), 400

    preco_final = REGRAS[regra](precos_encontrados)

    return jsonify({
        "materiais": [item["material"] for item in itens_precificados],
        "itens_precificados": itens_precificados,
        "preco": preco_final,
        "regra": regra
    })

@app.route("/")
def alive():
    return jsonify({"ok": True, "materiais_disponiveis": sorted(list({canon for (canon, _) in PRECOS.values()}))})

if __name__ == "__main__":
    # Para rodar local: python app.py
    app.run(host="0.0.0.0", port=5000)
