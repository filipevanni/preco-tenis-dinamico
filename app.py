# app.py
import os
import csv
import io
from typing import Dict, Tuple, List
from flask import Flask, request, jsonify
import requests
import unidecode
import math

app = Flask(__name__)

# =========================
# Config
# =========================
CSV_URL = os.getenv("FONTE_MATERIAIS_CSV_URL")  # planilha publicada como CSV (materiais,preco)
TIMEOUT = 20  # segundos para baixar o CSV

# =========================
# Normalização única (planilha e entrada)
# =========================
def norm(txt: str) -> str:
    """
    Normaliza para comparação:
    - minúsculas
    - remove acentos
    - troca hífen por espaço
    - colapsa espaços
    """
    t = unidecode.unidecode((txt or "").strip().lower())
    t = t.replace("-", " ")
    t = " ".join(t.split())
    return t

# =========================
# Arredondamento: inteiro mais próximo que termina em 7
# Tie-break (empate) sempre para cima
# =========================
def arredonda_para_terminar_em_7(valor: float) -> int:
    """
    Retorna o inteiro mais próximo que termina em 7.
    Em caso de empate, escolhe o MAIOR (para cima).
    Ex.: 2764  -> 2767
         2768  -> 2767
         15.2  -> 17
         12.0 (entre 7 e 17, mais perto de 12) -> 7 (mas se ficar exatamente no meio, vai para 17)
    """
    # base7 inferior
    lower7 = (math.floor(valor) // 10) * 10 + 7
    # Se o lower7 ficou acima do valor mas ainda há um 7 anterior na dezena anterior:
    if lower7 > valor:
        lower7 -= 10
    upper7 = lower7 + 10

    dist_lower = abs(valor - lower7)
    dist_upper = abs(upper7 - valor)

    if dist_lower < dist_upper:
        return int(lower7)
    elif dist_upper < dist_lower:
        return int(upper7)
    else:
        # empate -> para cima
        return int(upper7)

# =========================
# Cache em memória
# chave normalizada -> (nome_canonico, preco_int_unitario)
# =========================
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
        nome = (row.get("materiais") or row.get("material") or "").strip()
        preco_raw = (row.get("preco") or row.get("preço") or "").strip()
        if not nome or not preco_raw:
            continue

        # converte preço para int tolerando "1.497", "1497,00", "R$ 1497"
        s = preco_raw.replace("R$", "").replace(".", "").replace(" ", "").replace(",", ".")
        try:
            preco_num = int(round(float(s)))
        except Exception:
            # fallback apenas dígitos
            digs = "".join(ch for ch in preco_raw if ch.isdigit())
            if not digs:
                continue
            preco_num = int(digs)

        chave = norm(nome)
        if not chave:
            continue

        precos_tmp[chave] = (nome, preco_num)

    if not precos_tmp:
        raise RuntimeError("Nenhuma linha válida encontrada no CSV (colunas esperadas: 'materiais' e 'preco').")

    PRECOS = precos_tmp

# carrega na inicialização (sem matar o processo se falhar)
try:
    carregar_precos_do_csv()
except Exception as e:
    print(f"[AVISO] Não foi possível carregar os preços ao iniciar: {e}")

def garantir_precos() -> None:
    """Recarrega o cache se estiver vazio (ex.: após cold start)."""
    if not PRECOS:
        carregar_precos_do_csv()

# =========================
# Regras de precificação
# =========================
def preco_media_simples(precos: List[int]) -> int:
    bruto = sum(precos) / max(len(precos), 1)
    return arredonda_para_terminar_em_7(bruto)

REGRAS = {
    "media_simples": preco_media_simples,
    # aqui dá pra adicionar outras regras no futuro
}

# =========================
# Endpoints
# =========================
@app.route("/")
def alive():
    try:
        garantir_precos()
        disponiveis = sorted({canon for (canon, _) in PRECOS.values()})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})
    return jsonify({"ok": True, "materiais_disponiveis": disponiveis})

@app.route("/materiais")
def materiais():
    try:
        garantir_precos()
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

    itens = [
        {"material": PRECOS[k][0], "preco": PRECOS[k][1]}
        for k in sorted(PRECOS.keys())
    ]
    return jsonify({"materiais": itens, "fonte_csv": CSV_URL})

@app.route("/reload", methods=["POST"])
def reload():
    try:
        carregar_precos_do_csv()
        return jsonify({"ok": True, "materiais_catalogo": len(PRECOS)})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 500

@app.route("/preco")
def preco():
    """
    /preco?materiais=Couro%20Bovino,Couro%20de%20Til%C3%A1pia,Jeans&regra=media_simples
    - aceita qualquer ordem, acentos/caixa/hífen (normaliza tudo)
    - arredonda para inteiro mais próximo que termina em 7 (tie-break para cima)
    """
    try:
        garantir_precos()
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

    materias_qs = request.args.get("materiais", "").strip()
    regra = (request.args.get("regra", "media_simples") or "media_simples").strip()

    if not materias_qs:
        return jsonify({"erro": "Parâmetro 'materiais' é obrigatório."}), 400
    if regra not in REGRAS:
        return jsonify({"erro": f"Regra inválida. Use uma destas: {list(REGRAS.keys())}"}), 400

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
        sugestoes = sorted({canon for (canon, _) in PRECOS.values()})
        return jsonify({
            "erro": "Materiais desconhecidos.",
            "nao_encontrados": nao_encontrados,
            "sugestoes_disponiveis": sugestoes
        }), 400

    preco_final = REGRAS[regra](precos_encontrados)

    return jsonify({
        "materiais": [item["material"] for item in itens_precificados],
        "itens_precificados": itens_precificados,
        "preco": int(preco_final),
        "regra": regra
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
