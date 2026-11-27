from datetime import datetime, timedelta
from functools import wraps
import io
import math
import os
import re
import zipfile
import fitz  # PyMuPDF
import pandas as pd
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from PIL import Image
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from werkzeug.utils import secure_filename

app = Flask(__name__)

IS_VERCEL = bool(os.environ.get("VERCEL"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORK_BASE = "/tmp" if IS_VERCEL else BASE_DIR

PASTA_UPLOAD = os.path.join(WORK_BASE, "uploads")
PASTA_SAIDA = os.path.join(WORK_BASE, "cartazes_gerados")
PASTA_TEMP = os.path.join(WORK_BASE, "temp_zip")
PASTA_FUNDOS = os.path.join(BASE_DIR, "fundos_prontos")
PASTA_FONTES = os.path.join(BASE_DIR, "fontes")

for pasta in (PASTA_UPLOAD, PASTA_SAIDA, PASTA_TEMP):
    os.makedirs(pasta, exist_ok=True)

CARTAZ_LOGIN = (os.environ.get("CARTAZ_LOGIN") or "").strip()
CARTAZ_PASSWORD = (os.environ.get("CARTAZ_PASSWORD") or "").strip()
CARTAZ_USERS_RAW = (os.environ.get("CARTAZ_USERS") or "").strip()
CARTAZ_SECRET_KEY = (os.environ.get("CARTAZ_SECRET_KEY") or "").strip()

if CARTAZ_SECRET_KEY:
    app.secret_key = CARTAZ_SECRET_KEY
elif not IS_VERCEL:
    app.secret_key = "dev-local-secret-key-change-in-production"
else:
    app.secret_key = "cartaz-vercel-session-fallback-chave-fixa"

app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

EXTENSOES_IMAGEM = (".jpg", ".jpeg", ".png", ".webp")

LARGURA_CARTAZ = 1240
ALTURA_CARTAZ = 1754

LARGURA_PAISAGEM = 1754
ALTURA_PAISAGEM = 1240

ALTURA_DOBRA_A4_PT = 4.0 * cm
ALTURA_A4_RETRATO_PT = 29.7 * cm
ALTURA_A4_PAISAGEM_PT = 21.0 * cm


def obter_usuarios_permitidos():
    usuarios = {}

    if CARTAZ_LOGIN and CARTAZ_PASSWORD:
        usuarios[CARTAZ_LOGIN] = CARTAZ_PASSWORD

    if CARTAZ_USERS_RAW:
        for entrada in re.split(r"[,;\n]+", CARTAZ_USERS_RAW):
            item = entrada.strip()
            if not item or ":" not in item:
                continue
            usuario_item, senha_item = item.split(":", 1)
            usuario_item = usuario_item.strip()
            senha_item = senha_item.strip()
            if usuario_item and senha_item:
                usuarios[usuario_item] = senha_item

    return usuarios


def _encontrar_fonte(nomes_preferenciais):
    candidatos = []
    if os.path.isdir(PASTA_FONTES):
        for nome in nomes_preferenciais:
            caminho_local = os.path.join(PASTA_FONTES, nome)
            if os.path.isfile(caminho_local):
                candidatos.append(caminho_local)
        for arquivo in sorted(os.listdir(PASTA_FONTES)):
            if arquivo.lower().endswith(".ttf"):
                c = os.path.join(PASTA_FONTES, arquivo)
                if c not in candidatos:
                    candidatos.append(c)

    if os.name == "nt":
        for nome in nomes_preferenciais:
            c_win = os.path.join("C:/Windows/Fonts", nome)
            if os.path.isfile(c_win):
                candidatos.append(c_win)

    for caminho_ttf in candidatos:
        try:
            pdfmetrics.registerFont(TTFont("_temp_font_check", caminho_ttf))
            return caminho_ttf
        except Exception:
            continue

    return None


def registrar_fontes():
    fontes_map = {
        "FonteTitulo": (["impact.ttf", "Impact.ttf", "arialbd.ttf", "Arial-Bold.ttf"], "Helvetica-Bold"),
        "FontePrecoNormal": (["impact.ttf", "Impact.ttf", "arial.ttf", "Arial.ttf"], "Helvetica"),
        "FontePrecoOferta": (["impact.ttf", "Impact.ttf", "arialbd.ttf", "Arial-Bold.ttf"], "Helvetica-Bold"),
        "FonteDetalhes": (["impact.ttf", "Impact.ttf", "arial.ttf", "Arial.ttf"], "Helvetica"),
    }
    for nome_reg, (preferencias, fallback) in fontes_map.items():
        caminho_ttf = _encontrar_fonte(preferencias)
        if caminho_ttf:
            try:
                pdfmetrics.registerFont(TTFont(nome_reg, caminho_ttf))
                continue
            except Exception:
                pass
        if nome_reg not in pdfmetrics.getRegisteredFontNames():
            try:
                from reportlab.pdfbase.pdfmetrics import registerFontFamily

                registerFontFamily(nome_reg, normal=fallback, bold="Helvetica-Bold")
            except Exception:
                pass


registrar_fontes()


def obter_nome_fonte(alias_fonte, fallback_padrao):
    if alias_fonte in pdfmetrics.getRegisteredFontNames():
        return alias_fonte
    return fallback_padrao


def autenticado():
    return bool(session.get("autenticado") is True)


def auth_configurada():
    return bool(obter_usuarios_permitidos())


def login_obrigatorio_rota(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not autenticado():
            is_ajax = (
                request.path.startswith("/api/")
                or request.headers.get("X-Requested-With") == "XMLHttpRequest"
                or "application/json" in (request.headers.get("Accept") or "")
                or request.method == "POST"
            )
            if is_ajax:
                return jsonify({"erro": "Sessao expirada ou nao autenticado"}), 401
            return render_template("login.html", erro=None)
        return f(*args, **kwargs)

    return decorated


def parse_bool_form(valor, padrao=False):
    if valor is None:
        return padrao
    texto = str(valor).strip().lower()
    if texto in ("1", "true", "sim", "yes", "on", "ativo", "ativado"):
        return True
    if texto in ("0", "false", "nao", "não", "no", "off", "inativo", "desativado"):
        return False
    return padrao


def aplicar_caixa_texto(valor, modo="livre"):
    texto = "" if valor is None else str(valor).strip()
    if not texto or texto.lower() == "nan":
        return ""
    modo_limpo = str(modo or "livre").strip().lower()
    if modo_limpo in ("maiuscula", "maiúscula", "upper", "uppercase"):
        return texto.upper()
    if modo_limpo in ("minuscula", "minúscula", "lower", "lowercase"):
        return texto.lower()
    if modo_limpo in ("capitalizada", "capitalize", "title"):
        return texto[:1].upper() + texto[1:].lower()
    return texto


def normalizar_forma_valor(valor, legado_paralelogramo=False):
    mapa_aliases = {
        "nenhuma": "nenhuma",
        "nenhum": "nenhuma",
        "sem": "nenhuma",
        "none": "nenhuma",
        "paralelogramo": "paralelogramo",
        "retangulo": "retangulo",
        "retângulo": "retangulo",
        "retangulo_arredondado": "retangulo_arredondado",
        "retângulo_arredondado": "retangulo_arredondado",
        "pilula": "pilula",
        "pílula": "pilula",
        "trapezio": "trapezio",
        "trapézio": "trapezio",
        "estrela_20_pontas": "estrela_20_pontas",
        "estrela_20": "estrela_20_pontas",
        "estrela20": "estrela_20_pontas",
        "estrela": "estrela_20_pontas",
        "elipse": "elipse",
    }
    texto = str(valor or "").strip().lower()
    if texto in mapa_aliases:
        return mapa_aliases[texto]
    if legado_paralelogramo:
        return "paralelogramo"
    return "nenhuma"


def parse_cor_rgb(valor, padrao=(255, 215, 0)):
    if valor is None:
        return padrao
    if isinstance(valor, (tuple, list)) and len(valor) >= 3:
        try:
            return (
                max(0, min(255, int(valor[0]))),
                max(0, min(255, int(valor[1]))),
                max(0, min(255, int(valor[2]))),
            )
        except Exception:
            return padrao

    texto = str(valor).strip()
    if not texto:
        return padrao

    if texto.startswith("#"):
        hex_limpo = texto[1:].strip()
        if len(hex_limpo) == 3:
            hex_limpo = "".join(ch * 2 for ch in hex_limpo)
        if re.fullmatch(r"[0-9a-fA-F]{6}", hex_limpo):
            return (
                int(hex_limpo[0:2], 16),
                int(hex_limpo[2:4], 16),
                int(hex_limpo[4:6], 16),
            )

    m_rgb = re.match(r"^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})", texto, re.IGNORECASE)
    if m_rgb:
        return (
            max(0, min(255, int(m_rgb.group(1)))),
            max(0, min(255, int(m_rgb.group(2)))),
            max(0, min(255, int(m_rgb.group(3)))),
        )

    return padrao


def cor_forma_eh_preta(cor_rgb):
    r, g, b = parse_cor_rgb(cor_rgb, padrao=(255, 215, 0))
    return r <= 20 and g <= 24 and b <= 39


def obter_config_formato(formato):
    fmt = str(formato or "vertical").strip().lower()

    if fmt in ("horizontal_dobra", "paisagem_dobra", "landscape_dobra"):
        fracao_dobra = ALTURA_DOBRA_A4_PT / ALTURA_A4_PAISAGEM_PT
        return {
            "largura": LARGURA_PAISAGEM,
            "altura": ALTURA_PAISAGEM,
            "formato": "horizontal_dobra",
            "eh_horizontal": True,
            "margem_topo_dobra": ALTURA_PAISAGEM * fracao_dobra,
        }

    if fmt in ("vertical_dobra", "retrato_dobra", "portrait_dobra"):
        fracao_dobra = ALTURA_DOBRA_A4_PT / ALTURA_A4_RETRATO_PT
        return {
            "largura": LARGURA_CARTAZ,
            "altura": ALTURA_CARTAZ,
            "formato": "vertical_dobra",
            "eh_horizontal": False,
            "margem_topo_dobra": ALTURA_CARTAZ * fracao_dobra,
        }

    if fmt in ("horizontal", "paisagem", "landscape"):
        return {
            "largura": LARGURA_PAISAGEM,
            "altura": ALTURA_PAISAGEM,
            "formato": "horizontal",
            "eh_horizontal": True,
            "margem_topo_dobra": 0.0,
        }

    return {
        "largura": LARGURA_CARTAZ,
        "altura": ALTURA_CARTAZ,
        "formato": "vertical",
        "eh_horizontal": False,
        "margem_topo_dobra": 0.0,
    }


def listar_imagens_pasta_fundos():
    if not os.path.isdir(PASTA_FUNDOS):
        return []
    arquivos = []
    for nome in sorted(os.listdir(PASTA_FUNDOS), key=str.lower):
        caminho = os.path.join(PASTA_FUNDOS, nome)
        if os.path.isfile(caminho) and nome.lower().endswith(EXTENSOES_IMAGEM):
            arquivos.append({
                "nome": nome,
                "url": f"/fundos/{nome}",
            })
    return arquivos


def resolver_imagem_fundo(request_obj, fallback_ultimo_upload=False):
    file_fundo = request_obj.files.get("fundo")
    if file_fundo and file_fundo.filename:
        nome_seguro = secure_filename(file_fundo.filename) or "fundo_upload.png"
        caminho_fundo = os.path.join(PASTA_UPLOAD, nome_seguro)
        file_fundo.save(caminho_fundo)
        return caminho_fundo

    fundo_preset = (request_obj.form.get("fundo_preset") or "").strip()
    if fundo_preset:
        nome_preset = os.path.basename(fundo_preset)
        caminho_preset = os.path.abspath(os.path.join(PASTA_FUNDOS, nome_preset))
        base_abs = os.path.abspath(PASTA_FUNDOS)
        if caminho_preset.startswith(base_abs + os.sep) and os.path.isfile(caminho_preset):
            return caminho_preset

    if fallback_ultimo_upload and os.path.isdir(PASTA_UPLOAD):
        arquivos_upload = [
            os.path.join(PASTA_UPLOAD, f)
            for f in os.listdir(PASTA_UPLOAD)
            if f.lower().endswith(EXTENSOES_IMAGEM)
        ]
        if arquivos_upload:
            return max(arquivos_upload, key=os.path.getmtime)

    return None


def formatar_preco(valor):
    try:
        if pd.isna(valor) or str(valor).strip() == "":
            return "0,00"
        val_limpo = str(valor).replace("R$", "").strip()
        if "." in val_limpo and "," in val_limpo:
            val_limpo = val_limpo.replace(".", "").replace(",", ".")
        elif "," in val_limpo:
            val_limpo = val_limpo.replace(",", ".")
        val_float = float(val_limpo)
        s = f"{val_float:,.2f}"
        return s.replace(",", "v").replace(".", ",").replace("v", ".")
    except Exception:
        return str(valor)


def tem_valor_preco_valido(valor):
    if valor is None or pd.isna(valor):
        return False
    texto = str(valor).strip()
    if not texto or texto.lower() == "nan":
        return False
    val_limpo = texto.replace("R$", "").strip()
    if "." in val_limpo and "," in val_limpo:
        val_limpo = val_limpo.replace(".", "").replace(",", ".")
    elif "," in val_limpo:
        val_limpo = val_limpo.replace(",", ".")
    try:
        return float(val_limpo) > 0
    except Exception:
        return bool(val_limpo)


def formatar_data_br(valor):
    if valor is None or pd.isna(valor):
        return ""

    texto = str(valor).strip()
    if not texto or texto.lower() == "nan":
        return ""

    texto_data = texto.split()[0].strip()

    if re.match(r"^\d{2}/\d{2}/\d{4}$", texto_data):
        return texto_data

    formatos_entrada = (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d-%m-%Y",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
    )
    for fmt in formatos_entrada:
        for candidato in (texto, texto_data):
            try:
                return datetime.strptime(candidato, fmt).strftime("%d/%m/%Y")
            except ValueError:
                pass

    return texto_data


def interpretar_intervalo_validade(validade_bruta, data_inicio_bruta="", data_termino_bruta=""):
    ini = formatar_data_br(data_inicio_bruta)
    fim = formatar_data_br(data_termino_bruta)
    bruto = "" if validade_bruta is None or pd.isna(validade_bruta) else str(validade_bruta).strip()
    if bruto.lower() == "nan":
        bruto = ""

    if bruto:
        if ":" in bruto:
            partes = bruto.split(":", 1)
            p_ini = formatar_data_br(partes[0])
            p_fim = formatar_data_br(partes[1])
            if p_ini and p_fim:
                ini, fim = p_ini, p_fim
            elif p_fim and not p_ini:
                ini, fim = "", p_fim
            elif p_ini and not p_fim:
                ini, fim = "", p_ini
        else:
            p_unica = formatar_data_br(bruto)
            if p_unica and not ini and not fim:
                ini, fim = "", p_unica

    if ini and not fim:
        fim = ini
        ini = ""

    if ini and fim:
        return {
            "tem_validade": True,
            "linha_topo": "OFERTAS VALIDAS DE:",
            "linha_data": f"{ini} A {fim}",
        }

    if fim:
        return {
            "tem_validade": True,
            "linha_topo": "OFERTA VALIDA PARA:",
            "linha_data": fim,
        }

    return {
        "tem_validade": False,
        "linha_topo": "",
        "linha_data": "",
    }


def obter_unidade_medida(dados, modo_caixa="livre"):
    chaves_possiveis = ["unidade_medida", "unidade de medida", "unidade", "unid", "un", "um"]
    if hasattr(dados, "index"):
        for col in dados.index:
            col_limpa = re.sub(r"\s+", " ", str(col).strip().lower())
            if col_limpa in chaves_possiveis:
                val = dados.get(col, "")
                if pd.notna(val) and str(val).strip() != "" and str(val).strip().lower() != "nan":
                    return aplicar_caixa_texto(val, modo_caixa)
    elif isinstance(dados, dict):
        for chave in chaves_possiveis:
            val = dados.get(chave, "")
            if pd.notna(val) and str(val).strip() != "" and str(val).strip().lower() != "nan":
                return aplicar_caixa_texto(val, modo_caixa)
    return ""


def obter_campo_importa(dados):
    chaves_possiveis = ["importa", "importado", "origem", "procedencia", "procedência"]
    if hasattr(dados, "index"):
        for col in dados.index:
            col_limpa = str(col).strip().lower()
            if col_limpa in chaves_possiveis:
                val = dados.get(col, "")
                if pd.notna(val) and str(val).strip() != "" and str(val).strip().lower() != "nan":
                    return str(val).strip()
    elif isinstance(dados, dict):
        for chave in chaves_possiveis:
            val = dados.get(chave, "")
            if pd.notna(val) and str(val).strip() != "" and str(val).strip().lower() != "nan":
                return str(val).strip()
    return ""


def obter_observacao(dados):
    chaves_possiveis = ["observacao", "observação", "obs", "rodape", "rodapé"]
    if hasattr(dados, "index"):
        for col in dados.index:
            col_limpa = str(col).strip().lower()
            if col_limpa in chaves_possiveis:
                val = dados.get(col, "")
                if pd.notna(val) and str(val).strip() != "" and str(val).strip().lower() != "nan":
                    return str(val).strip()
    elif isinstance(dados, dict):
        for chave in chaves_possiveis:
            val = dados.get(chave, "")
            if pd.notna(val) and str(val).strip() != "" and str(val).strip().lower() != "nan":
                return str(val).strip()
    return "ou até enquanto durar o estoque"


def desenhar_texto_centralizado(c, x_centro, y, texto, fonte, tamanho, cor=(0, 0, 0)):
    c.setFont(fonte, tamanho)
    c.setFillColorRGB(cor[0] / 255, cor[1] / 255, cor[2] / 255)
    c.drawCentredString(x_centro, y, texto)


def quebrar_palavra_longa(c, palavra, fonte, tamanho, largura_max):
    if c.stringWidth(palavra, fonte, tamanho) <= largura_max:
        return [palavra]
    partes = []
    atual = ""
    for ch in palavra:
        teste = atual + ch
        if c.stringWidth(teste, fonte, tamanho) <= largura_max or not atual:
            atual = teste
        else:
            partes.append(atual)
            atual = ch
    if atual:
        partes.append(atual)
    return partes


def montar_linhas_centralizadas(c, texto, fonte, tamanho, largura_caixa):
    texto_limpo = str(texto or "").replace("\r\n", " ").replace("\n", " ").strip()
    if not texto_limpo:
        return []

    c.setFont(fonte, tamanho)
    tokens = re.split(r"(\s+)", texto_limpo)
    linhas = []
    linha_atual = ""

    for token in tokens:
        if token == "":
            continue

        if token.isspace():
            if not linha_atual:
                continue
            teste_espaco = linha_atual + token
            if c.stringWidth(teste_espaco, fonte, tamanho) <= largura_caixa:
                linha_atual = teste_espaco
            else:
                linhas.append(linha_atual.rstrip())
                linha_atual = ""
            continue

        partes_palavra = quebrar_palavra_longa(c, token, fonte, tamanho, largura_caixa)
        for parte in partes_palavra:
            candidato = linha_atual + parte
            if c.stringWidth(candidato, fonte, tamanho) <= largura_caixa or not linha_atual.strip():
                linha_atual = candidato
            else:
                linhas.append(linha_atual.rstrip())
                linha_atual = parte

    if linha_atual.strip():
        linhas.append(linha_atual.rstrip())

    return linhas


def desenhar_bloco_centralizado(
    c,
    x_esq,
    y_inicial,
    texto,
    fonte,
    tamanho,
    largura_caixa,
    cor=(0, 0, 0),
    espacamento_linha=1.2,
):
    linhas = montar_linhas_centralizadas(c, texto, fonte, tamanho, largura_caixa)
    if not linhas:
        return y_inicial

    c.setFont(fonte, tamanho)
    c.setFillColorRGB(cor[0] / 255, cor[1] / 255, cor[2] / 255)

    y = y_inicial
    passo_y = tamanho * espacamento_linha
    centro_x = float(x_esq) + (largura_caixa / 2.0)

    for linha_str in linhas:
        c.drawCentredString(centro_x, y, linha_str)
        y -= passo_y

    return y


def desenhar_bloco_alinhado_direita(
    c,
    x_dir,
    y_base,
    texto,
    fonte,
    tamanho,
    largura_caixa,
    cor=(0, 0, 0),
    espacamento_linha=1.2,
):
    linhas = montar_linhas_centralizadas(c, texto, fonte, tamanho, largura_caixa)
    if not linhas:
        return y_base

    c.setFont(fonte, tamanho)
    c.setFillColorRGB(cor[0] / 255, cor[1] / 255, cor[2] / 255)

    passo_y = tamanho * espacamento_linha
    for idx, linha_str in enumerate(linhas):
        deslocamento_linhas_abaixo = (len(linhas) - 1 - idx) * passo_y
        y_linha = y_base + deslocamento_linhas_abaixo
        c.drawRightString(x_dir, y_linha, linha_str)

    return y_base + ((len(linhas) - 1) * passo_y) + tamanho


def _tracar_poligono_arredondado(c, vertices, raio):
    n = len(vertices)
    if n < 3:
        return
    path = c.beginPath()
    for i in range(n):
        prev_pt = vertices[(i - 1) % n]
        curr_pt = vertices[i]
        next_pt = vertices[(i + 1) % n]

        vx_in, vy_in = prev_pt[0] - curr_pt[0], prev_pt[1] - curr_pt[1]
        len_in = max(1e-6, (vx_in * vx_in + vy_in * vy_in) ** 0.5)
        u_in = (vx_in / len_in, vy_in / len_in)

        vx_out, vy_out = next_pt[0] - curr_pt[0], next_pt[1] - curr_pt[1]
        len_out = max(1e-6, (vx_out * vx_out + vy_out * vy_out) ** 0.5)
        u_out = (vx_out / len_out, vy_out / len_out)

        r_local = min(float(raio), len_in * 0.42, len_out * 0.42)
        start_arc = (curr_pt[0] + u_in[0] * r_local, curr_pt[1] + u_in[1] * r_local)
        end_arc = (curr_pt[0] + u_out[0] * r_local, curr_pt[1] + u_out[1] * r_local)

        if i == 0:
            path.moveTo(start_arc[0], start_arc[1])
        else:
            path.lineTo(start_arc[0], start_arc[1])

        path.curveTo(
            curr_pt[0],
            curr_pt[1],
            curr_pt[0],
            curr_pt[1],
            end_arc[0],
            end_arc[1],
        )

    path.close()
    c.drawPath(path, stroke=0, fill=1)


def desenhar_estrela_20_pontas(c, x0, yb, w, h):
    cx = x0 + (w * 0.5)
    ym = yb + (h * 0.5)
    rx_ext = w * 0.52
    ry_ext = h * 0.56
    rx_int = w * 0.44
    ry_int = h * 0.44

    pontas = 20
    total_vertices = pontas * 2
    path = c.beginPath()

    for idx in range(total_vertices):
        angulo = (2.0 * math.pi * idx) / total_vertices
        eh_ponta = (idx % 2) == 0
        rx = rx_ext if eh_ponta else rx_int
        ry = ry_ext if eh_ponta else ry_int
        px = cx + (rx * math.cos(angulo))
        py = ym + (ry * math.sin(angulo))
        if idx == 0:
            path.moveTo(px, py)
        else:
            path.lineTo(px, py)

    path.close()
    c.drawPath(path, stroke=0, fill=1)


def desenhar_forma_atras_valor(
    c,
    x_esq_forma,
    y_baixo_forma,
    largura_forma,
    altura_forma,
    forma="paralelogramo",
    cor_preenchimento=(255, 215, 0),
):
    tipo = normalizar_forma_valor(forma)
    if tipo == "nenhuma":
        return

    w = max(24.0, float(largura_forma))
    h = max(16.0, float(altura_forma))
    x0 = float(x_esq_forma)
    x1 = x0 + w
    yb = float(y_baixo_forma)
    yt = yb + h

    c.saveState()
    c.setFillColorRGB(
        cor_preenchimento[0] / 255.0,
        cor_preenchimento[1] / 255.0,
        cor_preenchimento[2] / 255.0,
    )

    if tipo == "retangulo":
        c.rect(x0, yb, w, h, stroke=0, fill=1)

    elif tipo == "retangulo_arredondado":
        raio = max(6.0, min(h * 0.16, w * 0.08))
        c.roundRect(x0, yb, w, h, raio, stroke=0, fill=1)

    elif tipo == "pilula":
        raio = h * 0.45
        c.roundRect(x0, yb, w, h, raio, stroke=0, fill=1)

    elif tipo == "paralelogramo":
        skew = min(w * 0.08, h * 0.35)
        vertices = [
            (x0 - (skew * 0.5), yb),
            (x1 - (skew * 0.5), yb),
            (x1 + (skew * 0.5), yt),
            (x0 + (skew * 0.5), yt),
        ]
        raio = max(6.0, min(h * 0.14, w * 0.06))
        _tracar_poligono_arredondado(c, vertices, raio)

    elif tipo == "trapezio":
        recuo = min(w * 0.08, h * 0.25)
        vertices = [
            (x0, yb),
            (x1, yb),
            (x1 - recuo, yt),
            (x0 + recuo, yt),
        ]
        raio = max(6.0, min(h * 0.14, w * 0.06))
        _tracar_poligono_arredondado(c, vertices, raio)

    elif tipo == "estrela_20_pontas":
        desenhar_estrela_20_pontas(c, x0, yb, w, h)

    elif tipo == "elipse":
        c.ellipse(x0, yb, x1, yt, stroke=0, fill=1)

    c.restoreState()


def calcular_geometria_preco_oferta(
    c,
    x_esq,
    largura_caixa,
    y,
    preco_str,
    fonte,
    tamanho_inteiro,
    forma_valor="nenhuma",
    cor_forma_valor=(255, 215, 0),
    unidade_medida="",
    fonte_unidade="Helvetica",
    modo_caixa_unidade="livre",
):
    if "," in preco_str:
        partes = preco_str.split(",")
        inteiro = partes[0]
        centavos = "," + partes[1]
    else:
        inteiro = preco_str
        centavos = ",00"

    simbolo = "R$"
    tamanho_simbolo = int(tamanho_inteiro * 0.35)
    tamanho_centavos = int(tamanho_inteiro * 0.45)

    c.setFont(fonte, tamanho_simbolo)
    largura_simbolo = c.stringWidth(simbolo, fonte, tamanho_simbolo)
    c.setFont(fonte, tamanho_inteiro)
    largura_inteiro = c.stringWidth(inteiro, fonte, tamanho_inteiro)
    c.setFont(fonte, tamanho_centavos)
    largura_centavos = c.stringWidth(centavos, fonte, tamanho_centavos)

    espaco = int(tamanho_inteiro * 0.05)
    largura_total_preco = largura_simbolo + espaco + largura_inteiro + largura_centavos
    x_inicial = float(x_esq) + ((largura_caixa - largura_total_preco) / 2.0)

    y_simbolo = y + (tamanho_inteiro * 0.50)
    y_centavos = y + (tamanho_inteiro * 0.35)

    topo_simbolo = y_simbolo + (tamanho_simbolo * 0.78)
    topo_centavos = y_centavos + (tamanho_centavos * 0.78)
    topo_inteiro = y + (tamanho_inteiro * 0.80)
    y_topo_total = max(topo_simbolo, topo_centavos, topo_inteiro)
    y_base_total = y - (tamanho_inteiro * 0.06)

    unidade_limpa = aplicar_caixa_texto(unidade_medida, modo_caixa_unidade)
    tem_unidade = bool(unidade_limpa)
    tamanho_unidade = max(14, int(tamanho_inteiro * 0.19))
    espaco_unidade = max(6.0, tamanho_inteiro * 0.05)

    x_centro_centavos = (
        x_inicial
        + largura_simbolo
        + espaco
        + largura_inteiro
        + (largura_centavos / 2.0)
    )
    x_centro_preco = x_inicial + (largura_total_preco / 2.0)
    x_unidade = x_centro_centavos if largura_centavos >= tamanho_unidade else x_centro_preco
    y_unidade = y_centavos - espaco_unidade - (tamanho_unidade * 0.85) if tem_unidade else y_base_total

    tipo_forma = normalizar_forma_valor(forma_valor)
    tem_forma_valor = tipo_forma != "nenhuma"
    pad_x = max(14.0, tamanho_inteiro * 0.10)
    pad_y = max(8.0, tamanho_inteiro * 0.06)
    x_esq_forma = x_inicial - pad_x
    y_baixo_forma = y_base_total - pad_y
    largura_forma = largura_total_preco + (pad_x * 2.0)
    altura_forma = (y_topo_total - y_base_total) + (pad_y * 2.0)

    return {
        "simbolo": simbolo,
        "inteiro": inteiro,
        "centavos": centavos,
        "tamanho_simbolo": tamanho_simbolo,
        "tamanho_centavos": tamanho_centavos,
        "largura_simbolo": largura_simbolo,
        "largura_inteiro": largura_inteiro,
        "largura_total_preco": largura_total_preco,
        "espaco": espaco,
        "x_inicial": x_inicial,
        "y_simbolo": y_simbolo,
        "y_centavos": y_centavos,
        "y_topo_total": y_topo_total,
        "y_base_total": y_base_total,
        "unidade_medida": unidade_limpa,
        "tem_unidade": tem_unidade,
        "tamanho_unidade": tamanho_unidade,
        "fonte_unidade": fonte_unidade,
        "x_unidade": x_unidade,
        "y_unidade": y_unidade,
        "forma_valor": tipo_forma,
        "tem_forma_valor": tem_forma_valor,
        "cor_forma_valor": parse_cor_rgb(cor_forma_valor),
        "x_esq_forma": x_esq_forma,
        "y_baixo_forma": y_baixo_forma,
        "largura_forma": largura_forma,
        "altura_forma": altura_forma,
    }


def desenhar_preco_oferta_fracionado(
    c,
    x_esq,
    largura_caixa,
    y,
    preco_str,
    fonte,
    tamanho_inteiro,
    cor=(255, 0, 0),
    forma_valor="nenhuma",
    cor_forma_valor=(255, 215, 0),
    geom=None,
    unidade_medida="",
    fonte_unidade="Helvetica",
    cor_unidade=(0, 0, 0),
    modo_caixa_unidade="livre",
):
    if geom is None:
        geom = calcular_geometria_preco_oferta(
            c,
            x_esq,
            largura_caixa,
            y,
            preco_str,
            fonte,
            tamanho_inteiro,
            forma_valor=forma_valor,
            cor_forma_valor=cor_forma_valor,
            unidade_medida=unidade_medida,
            fonte_unidade=fonte_unidade,
            modo_caixa_unidade=modo_caixa_unidade,
        )

    if geom["tem_forma_valor"]:
        desenhar_forma_atras_valor(
            c,
            x_esq_forma=geom["x_esq_forma"],
            y_baixo_forma=geom["y_baixo_forma"],
            largura_forma=geom["largura_forma"],
            altura_forma=geom["altura_forma"],
            forma=geom["forma_valor"],
            cor_preenchimento=geom["cor_forma_valor"],
        )

    c.setFillColorRGB(cor[0] / 255, cor[1] / 255, cor[2] / 255)
    c.setFont(fonte, geom["tamanho_simbolo"])
    c.drawString(geom["x_inicial"], geom["y_simbolo"], geom["simbolo"])
    c.setFont(fonte, tamanho_inteiro)
    c.drawString(geom["x_inicial"] + geom["largura_simbolo"] + geom["espaco"], y, geom["inteiro"])
    c.setFont(fonte, geom["tamanho_centavos"])
    c.drawString(
        geom["x_inicial"] + geom["largura_simbolo"] + geom["espaco"] + geom["largura_inteiro"],
        geom["y_centavos"],
        geom["centavos"],
    )

    if geom["tem_unidade"]:
        c.setFont(geom["fonte_unidade"], geom["tamanho_unidade"])
        c.setFillColorRGB(cor_unidade[0] / 255, cor_unidade[1] / 255, cor_unidade[2] / 255)
        c.drawCentredString(geom["x_unidade"], geom["y_unidade"], geom["unidade_medida"])


def criar_cartaz_pdf(
    c,
    dados,
    img_reader,
    largura_orig=LARGURA_CARTAZ,
    altura_orig=ALTURA_CARTAZ,
    x_offset=0,
    y_offset=0,
    fator_escala=1.0,
    formato="vertical",
    fundo_paralelogramo=False,
    forma_valor=None,
    cor_forma_valor=(255, 215, 0),
    modo_caixa_unidade="livre",
):
    cfg = obter_config_formato(formato)
    largura = largura_orig * fator_escala
    altura = altura_orig * fator_escala
    eh_horizontal = cfg["eh_horizontal"]
    margem_topo_dobra = cfg["margem_topo_dobra"] * fator_escala
    tem_dobra = margem_topo_dobra > 0

    forma_escolhida = normalizar_forma_valor(
        forma_valor if forma_valor is not None else dados.get("forma_valor", ""),
        legado_paralelogramo=fundo_paralelogramo,
    )
    cor_forma_rgb = parse_cor_rgb(
        cor_forma_valor if cor_forma_valor is not None else dados.get("cor_forma_valor", "#FFD700"),
        padrao=(255, 215, 0),
    )
    cor_unidade_rgb = (255, 255, 255) if cor_forma_eh_preta(cor_forma_rgb) else (0, 0, 0)

    c.saveState()
    c.translate(x_offset, y_offset)

    c.setFillColorRGB(1, 1, 1)
    c.rect(0, 0, largura, altura, stroke=0, fill=1)

    topo_util = altura - margem_topo_dobra
    altura_topo = 0
    if img_reader is not None:
        img_w, img_h = img_reader.getSize()
        proporcao = img_h / float(img_w) if img_w else 0.25
        largura_img = largura
        altura_img = largura_img * proporcao

        if tem_dobra:
            max_topo = altura * (0.14 if eh_horizontal else 0.19)
        else:
            max_topo = altura * (0.17 if eh_horizontal else 0.26)

        if altura_img > max_topo:
            altura_img = max_topo

        y_img = topo_util - altura_img
        c.drawImage(img_reader, 0, y_img, width=largura_img, height=altura_img, mask="auto")
        altura_topo = altura_img

    f_produto = obter_nome_fonte("FonteTitulo", "Helvetica-Bold")
    f_preco_normal = obter_nome_fonte("FontePrecoNormal", "Helvetica")
    f_preco_oferta = obter_nome_fonte("FontePrecoOferta", "Helvetica-Bold")
    f_detalhes = obter_nome_fonte("FonteDetalhes", "Helvetica")

    margem_x = int(largura * (0.08 if eh_horizontal else 0.06))
    largura_caixa = largura - (margem_x * 2)
    x_dir = largura - margem_x
    centro_x = float(margem_x) + (largura_caixa / 2.0)

    largura_bloco_preco = largura_caixa
    x_esq_preco = float(margem_x)
    centro_preco_x = centro_x
    largura_bloco_rodape_dir = largura_caixa * 0.36 if eh_horizontal else largura_caixa

    y_obs = int(altura * (0.026 if eh_horizontal else 0.020))
    tam_obs = int(altura * (0.026 if eh_horizontal else 0.020))
    cor_obs = (255, 0, 0)

    tamanho_fonte_limite = int(altura * (0.036 if eh_horizontal else 0.038))
    y_limite = int(altura * (0.026 if eh_horizontal else 0.118))

    bruto_nome = dados.get("descricao_produtos", "PRODUTO")
    nome_prod = str(bruto_nome if pd.notna(bruto_nome) else "PRODUTO").strip().upper() or "PRODUTO"
    if tem_dobra:
        tam_nome = int(altura * (0.058 if eh_horizontal else 0.055))
        espaco_pos_imagem = int(altura * 0.022)
    else:
        tam_nome = int(altura * (0.065 if eh_horizontal else 0.062))
        espaco_pos_imagem = int(altura * 0.028)

    y_nome_inicial = topo_util - altura_topo - espaco_pos_imagem - tam_nome
    passo_fixo_nome = int(tam_nome * 1.22)

    linhas_nome = montar_linhas_centralizadas(c, nome_prod, f_produto, tam_nome, largura_caixa)

    y_nome_cursor = y_nome_inicial
    c.setFont(f_produto, tam_nome)
    c.setFillColorRGB(0, 0, 0)
    for linha_str in linhas_nome:
        c.drawCentredString(centro_x, y_nome_cursor, linha_str)
        y_nome_cursor -= passo_fixo_nome

    if linhas_nome:
        y_base_produto = y_nome_inicial - ((len(linhas_nome) - 1) * passo_fixo_nome)
    else:
        y_base_produto = y_nome_inicial

    texto_extra = dados.get("campo_extra", "")
    obs_extra = dados.get("observacao_extra", "")
    texto_importa = obter_campo_importa(dados)

    tem_extra = pd.notna(texto_extra) and str(texto_extra).strip() != "" and str(texto_extra).strip().lower() != "nan"
    tem_obs_extra = pd.notna(obs_extra) and str(obs_extra).strip() != "" and str(obs_extra).strip().lower() != "nan"
    tem_importa = bool(texto_importa)

    tamanho_fonte_extra = int(altura * (0.042 if eh_horizontal else 0.042))
    espaco_antes_extra = int(altura * (0.024 if tem_dobra else 0.032))
    y_cursor = y_base_produto - espaco_antes_extra - (tamanho_fonte_extra if tem_extra else 0)
    tam_sub_extra = int(altura * (0.023 if eh_horizontal else 0.019))

    if tem_extra:
        texto_inicio = "LEVE "
        texto_meio = str(texto_extra).strip().upper()
        texto_fim = " POR:"
        c.setFont(f_preco_oferta, tamanho_fonte_extra)

        largura_inicio = c.stringWidth(texto_inicio, f_preco_oferta, tamanho_fonte_extra)
        largura_meio = c.stringWidth(texto_meio, f_preco_oferta, tamanho_fonte_extra)
        largura_fim = c.stringWidth(texto_fim, f_preco_oferta, tamanho_fonte_extra)
        largura_total_extra = largura_inicio + largura_meio + largura_fim

        x_inicial_extra = (largura - largura_total_extra) / 2.0

        c.setFillColorRGB(0, 0, 0)
        c.drawString(x_inicial_extra, y_cursor, texto_inicio)
        c.drawString(x_inicial_extra + largura_inicio + largura_meio, y_cursor, texto_fim)
        c.setFillColorRGB(1, 0, 0)
        c.drawString(x_inicial_extra + largura_inicio, y_cursor, texto_meio)

        y_cursor -= int(altura * 0.042)

    if tem_obs_extra:
        y_cursor = desenhar_bloco_centralizado(
            c,
            margem_x,
            y_cursor,
            str(obs_extra).strip(),
            f_preco_oferta,
            tamanho_fonte_limite,
            largura_caixa,
            (255, 0, 0),
            espacamento_linha=1.18,
        )
        y_cursor -= int(altura * 0.018)

    if tem_importa:
        y_cursor = desenhar_bloco_centralizado(
            c,
            margem_x,
            y_cursor,
            texto_importa,
            f_detalhes,
            tam_sub_extra,
            largura_caixa,
            (60, 60, 60),
            espacamento_linha=1.20,
        )
        y_cursor -= int(altura * 0.015)

    limite_cliente = dados.get("limite_por_cliente", "")
    tem_limite = pd.notna(limite_cliente) and str(limite_cliente).strip() != "" and str(limite_cliente).strip().lower() != "nan"

    unidade_medida = obter_unidade_medida(dados, modo_caixa=modo_caixa_unidade)

    p_normal = dados.get("preco_normal", "")
    p_oferta_bruto = dados.get("preco_oferta", "0")
    p_oferta = formatar_preco(p_oferta_bruto)
    tem_preco_de = tem_valor_preco_valido(p_normal)
    tem_preco_por = tem_valor_preco_valido(p_oferta_bruto)
    mostrar_por_apenas = tem_preco_de and tem_preco_por

    tam_base_amostra = 100.0
    geom_amostra = calcular_geometria_preco_oferta(
        c,
        x_esq_preco,
        largura_bloco_preco,
        0.0,
        p_oferta,
        f_preco_oferta,
        tam_base_amostra,
        forma_valor=forma_escolhida,
        cor_forma_valor=cor_forma_rgb,
        unidade_medida=unidade_medida,
        fonte_unidade=f_detalhes,
        modo_caixa_unidade=modo_caixa_unidade,
    )
    largura_referencia_amostra = max(1.0, geom_amostra["largura_total_preco"])
    largura_desejada_preco = largura_bloco_preco * (0.78 if eh_horizontal else 0.84)
    tam_por_largura = int(tam_base_amostra * (largura_desejada_preco / largura_referencia_amostra))

    piso_preco = int(altura * (0.14 if eh_horizontal else 0.11))
    teto_preco = int(altura * (0.34 if eh_horizontal else 0.28))
    tamanho_fonte_preco = max(piso_preco, min(teto_preco, tam_por_largura))

    if eh_horizontal:
        y_pos_preco = int(altura * 0.175) if tem_limite else int(altura * 0.130)
    else:
        y_pos_preco = (y_limite + int(altura * 0.125)) if tem_limite else int(altura * 0.220)

    tamanho_fonte_por_apenas = int(altura * (0.026 if eh_horizontal else 0.023))
    tamanho_fonte_normal = int(altura * (0.044 if eh_horizontal else 0.042))
    folga_topo_por_apenas = int(altura * 0.012)
    distancia_de_acima = int(altura * 0.038)

    def calcular_cotas_preco(tam_preco_cand):
        geom_cand = calcular_geometria_preco_oferta(
            c,
            x_esq_preco,
            largura_bloco_preco,
            y_pos_preco,
            p_oferta,
            f_preco_oferta,
            tam_preco_cand,
            forma_valor=forma_escolhida,
            cor_forma_valor=cor_forma_rgb,
            unidade_medida=unidade_medida,
            fonte_unidade=f_detalhes,
            modo_caixa_unidade=modo_caixa_unidade,
        )
        base_referencia_topo = (
            (geom_cand["y_baixo_forma"] + geom_cand["altura_forma"])
            if geom_cand["tem_forma_valor"]
            else geom_cand["y_topo_total"]
        )
        y_por_cand = base_referencia_topo + folga_topo_por_apenas if mostrar_por_apenas else base_referencia_topo
        y_de_cand = (y_por_cand + distancia_de_acima) if mostrar_por_apenas else (base_referencia_topo + folga_topo_por_apenas)

        if tem_preco_de:
            topo_bloco_cand = y_de_cand + tamanho_fonte_normal + int(altura * 0.012)
        elif mostrar_por_apenas:
            topo_bloco_cand = y_por_cand + tamanho_fonte_por_apenas + int(altura * 0.018)
        else:
            topo_bloco_cand = base_referencia_topo + int(altura * 0.012)

        return geom_cand, y_por_cand, y_de_cand, topo_bloco_cand

    geom_preco, y_por_apenas, y_de, topo_bloco_preco = calcular_cotas_preco(tamanho_fonte_preco)

    limite_superior_preco = (y_limite + int(altura * 0.095)) if eh_horizontal else (y_limite + int(altura * 0.125))
    if y_cursor < topo_bloco_preco and topo_bloco_preco > limite_superior_preco:
        disponivel = max(1, y_cursor - limite_superior_preco)
        necessario = max(1, topo_bloco_preco - limite_superior_preco)
        fator_ajuste = max(0.55, min(1.0, disponivel / float(necessario)))
        piso_ajuste = int(altura * (0.12 if eh_horizontal else 0.088))
        tamanho_fonte_preco = max(piso_ajuste, int(tamanho_fonte_preco * fator_ajuste))
        geom_preco, y_por_apenas, y_de, topo_bloco_preco = calcular_cotas_preco(tamanho_fonte_preco)

    if mostrar_por_apenas:
        desenhar_texto_centralizado(
            c,
            centro_preco_x,
            y_por_apenas,
            "POR APENAS",
            f_detalhes,
            tamanho_fonte_por_apenas,
            (0, 0, 0),
        )

    if tem_preco_de:
        p_normal_formatado = formatar_preco(p_normal)
        texto_de = f"De: R$ {p_normal_formatado}"

        desenhar_texto_centralizado(c, centro_x, y_de, texto_de, f_preco_normal, tamanho_fonte_normal, (0, 0, 0))

        c.setFont(f_preco_normal, tamanho_fonte_normal)
        largura_texto_de = c.stringWidth(texto_de, f_preco_normal, tamanho_fonte_normal)
        x_inicio_linha = centro_x - (largura_texto_de / 2.0)
        x_fim_linha = x_inicio_linha + largura_texto_de
        y_traco = y_de + (tamanho_fonte_normal * 0.35)
        c.setStrokeColorRGB(1, 0, 0)
        c.setLineWidth(max(1, int(3.5 * fator_escala)))
        c.line(x_inicio_linha, y_traco, x_fim_linha, y_traco)

    desenhar_preco_oferta_fracionado(
        c,
        x_esq_preco,
        largura_bloco_preco,
        y_pos_preco,
        p_oferta,
        f_preco_oferta,
        tamanho_fonte_preco,
        (255, 0, 0),
        forma_valor=forma_escolhida,
        cor_forma_valor=cor_forma_rgb,
        geom=geom_preco,
        unidade_medida=unidade_medida,
        fonte_unidade=f_detalhes,
        cor_unidade=cor_unidade_rgb,
        modo_caixa_unidade=modo_caixa_unidade,
    )

    if tem_limite:
        txt_limite_ini = "LIMITE DE: "
        txt_limite_val = str(limite_cliente).strip()
        txt_limite_fim = " POR CLIENTE"

        c.setFont(f_preco_oferta, tamanho_fonte_limite)

        w_ini = c.stringWidth(txt_limite_ini, f_preco_oferta, tamanho_fonte_limite)
        w_val = c.stringWidth(txt_limite_val, f_preco_oferta, tamanho_fonte_limite)
        w_fim = c.stringWidth(txt_limite_fim, f_preco_oferta, tamanho_fonte_limite)
        w_total_limite = w_ini + w_val + w_fim

        x_inicial_limite = centro_x - (w_total_limite / 2.0)

        c.setFillColorRGB(0, 0, 0)
        c.drawString(x_inicial_limite, y_limite, txt_limite_ini)
        c.setFillColorRGB(1, 0, 0)
        c.drawString(x_inicial_limite + w_ini, y_limite, txt_limite_val)
        c.setFillColorRGB(0, 0, 0)
        c.drawString(x_inicial_limite + w_ini + w_val, y_limite, txt_limite_fim)

    validade_info = interpretar_intervalo_validade(
        dados.get("validade", ""),
        dados.get("data_inicio", ""),
        dados.get("data_termino", ""),
    )
    obs_texto = obter_observacao(dados) or "*Ou enquanto durar o estoque*"
    tam_validade = int(altura * (0.021 if eh_horizontal else 0.017))
    passo_validade = int(tam_validade * 1.25)

    if eh_horizontal:
        topo_obs_dir = desenhar_bloco_alinhado_direita(
            c,
            x_dir,
            y_obs,
            obs_texto,
            f_detalhes,
            tam_obs,
            largura_bloco_rodape_dir,
            cor_obs,
        )
        if validade_info["tem_validade"]:
            y_data_paisagem = topo_obs_dir + int(altura * 0.012)
            y_topo_paisagem = y_data_paisagem + passo_validade

            c.setFont(f_detalhes, tam_validade)
            c.setFillColorRGB(0, 0, 0)
            c.drawRightString(x_dir, y_topo_paisagem, validade_info["linha_topo"])

            c.setFillColorRGB(255 / 255, 0, 0)
            c.drawRightString(x_dir, y_data_paisagem, validade_info["linha_data"])
    else:
        if validade_info["tem_validade"]:
            y_data_vert = int(altura * 0.046)
            y_topo_vert = y_data_vert + passo_validade

            desenhar_texto_centralizado(
                c, centro_x, y_topo_vert, validade_info["linha_topo"], f_detalhes, tam_validade, (0, 0, 0)
            )
            desenhar_texto_centralizado(
                c, centro_x, y_data_vert, validade_info["linha_data"], f_detalhes, tam_validade, (255, 0, 0)
            )
            desenhar_bloco_centralizado(
                c, margem_x, y_obs, obs_texto, f_detalhes, tam_obs, largura_caixa, cor_obs
            )
        else:
            desenhar_bloco_centralizado(
                c, margem_x, int(altura * 0.026), obs_texto, f_detalhes, tam_obs, largura_caixa, cor_obs
            )

    c.restoreState()


def _extrair_estilo_forma_request(req):
    legado = parse_bool_form(req.form.get("fundo_paralelogramo"), padrao=False)
    forma_raw = (
        req.form.get("forma_valor")
        or req.form.get("forma_abaixo_valor")
        or req.form.get("forma_geometrica")
        or req.form.get("forma")
        or ""
    ).strip()
    forma_limpa = normalizar_forma_valor(forma_raw, legado_paralelogramo=legado)
    cor_raw = (
        req.form.get("cor_forma_valor")
        or req.form.get("cor_forma_abaixo")
        or req.form.get("cor_forma")
        or req.form.get("cor_paralelogramo")
        or "#FFD700"
    ).strip()
    cor_rgb = parse_cor_rgb(cor_raw, padrao=(255, 215, 0))
    return forma_limpa, cor_rgb


def _extrair_modo_caixa_unidade_request(req):
    return (
        req.form.get("caixa_unidade_medida")
        or req.form.get("modo_caixa_unidade")
        or req.form.get("caixa_unidade")
        or "livre"
    ).strip().lower()


@app.route("/login", methods=["GET", "POST"])
def login():
    if autenticado():
        return redirect(url_for("index"))

    erro = None
    if request.method == "POST":
        usuarios_validos = obter_usuarios_permitidos()
        if not usuarios_validos:
            erro = "Configure CARTAZ_LOGIN/CARTAZ_PASSWORD ou CARTAZ_USERS nas variaveis de ambiente."
        else:
            usuario = (request.form.get("usuario") or "").strip()
            senha = request.form.get("senha") or ""
            if usuario in usuarios_validos and usuarios_validos[usuario] == senha:
                session.clear()
                session.permanent = True
                session["autenticado"] = True
                session["usuario"] = usuario
                return redirect(url_for("index"))
            erro = "Usuario ou senha invalidos."

    return render_template("login.html", erro=erro)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    if not autenticado():
        return render_template("login.html", erro=None)
    return render_template("index.html", usuario=session.get("usuario", ""))


@app.route("/api/fundos", methods=["GET"])
@login_obrigatorio_rota
def api_listar_fundos():
    return jsonify({"fundos": listar_imagens_pasta_fundos()})


@app.route("/fundos/<path:filename>", methods=["GET"])
@login_obrigatorio_rota
def servir_fundo_pasta(filename):
    nome_limpo = os.path.basename(filename)
    return send_from_directory(os.path.abspath(PASTA_FUNDOS), nome_limpo)


@app.route("/preview", methods=["POST"])
@login_obrigatorio_rota
def preview():
    try:
        if "planilha" not in request.files:
            return "Planilha ausente", 400

        file_planilha = request.files["planilha"]
        caminho_fundo = resolver_imagem_fundo(request, fallback_ultimo_upload=False)
        codigo_buscado = request.form.get("codigo_produto", "").strip()
        cfg = obter_config_formato(request.form.get("formato"))
        largura_fmt, altura_fmt, formato_limpo = cfg["largura"], cfg["altura"], cfg["formato"]
        forma_val, cor_forma = _extrair_estilo_forma_request(request)
        modo_caixa_unidade = _extrair_modo_caixa_unidade_request(request)

        if not codigo_buscado or file_planilha.filename == "" or not caminho_fundo:
            return "Dados incompletos (selecione planilha, fundo e código)", 400

        caminho_planilha = os.path.join(PASTA_UPLOAD, secure_filename(file_planilha.filename) or "planilha.xlsx")
        file_planilha.save(caminho_planilha)

        try:
            df = pd.read_excel(caminho_planilha, dtype=str)
        except Exception as e:
            return f"Erro ao ler planilha: {e}", 400

        coluna_codigo = None
        for col in df.columns:
            col_lower = col.lower()
            if any(termo in col_lower for termo in ["codigo", "cod", "id", "sku", "referencia"]):
                coluna_codigo = col
                break

        if not coluna_codigo:
            coluna_codigo = df.columns[0]

        produto_encontrado = df[df[coluna_codigo].astype(str).str.strip() == codigo_buscado]

        if produto_encontrado.empty:
            return "Produto não encontrado", 404

        try:
            with Image.open(caminho_fundo) as im_check:
                im_check.verify()
        except Exception:
            return "Imagem inválida", 400

        pdf_buffer = io.BytesIO()
        c = canvas.Canvas(pdf_buffer, pagesize=(largura_fmt, altura_fmt))
        dados = produto_encontrado.iloc[0]
        criar_cartaz_pdf(
            c,
            dados,
            ImageReader(caminho_fundo),
            largura_fmt,
            altura_fmt,
            x_offset=0,
            y_offset=0,
            fator_escala=1.0,
            formato=formato_limpo,
            forma_valor=forma_val,
            cor_forma_valor=cor_forma,
            modo_caixa_unidade=modo_caixa_unidade,
        )
        c.showPage()
        c.save()
        pdf_buffer.seek(0)

        doc = fitz.open(stream=pdf_buffer.read(), filetype="pdf")
        page = doc.load_page(0)
        pix = page.get_pixmap(dpi=110)
        doc.close()
        img_byte_arr = io.BytesIO(pix.tobytes("png"))
        img_byte_arr.seek(0)
        return send_file(img_byte_arr, mimetype="image/png")

    except Exception as e:
        return str(e), 500


@app.route("/preview_avulso", methods=["POST"])
@login_obrigatorio_rota
def preview_avulso():
    try:
        form = request.form
        nome_prod = form.get("descricao_produtos") or form.get("nome_produto") or "PRODUTO EM OFERTA"
        preco_oferta = (form.get("preco_oferta") or form.get("preco_por") or "0").strip()
        cfg = obter_config_formato(form.get("formato"))
        largura_fmt, altura_fmt, formato_limpo = cfg["largura"], cfg["altura"], cfg["formato"]
        forma_val, cor_forma = _extrair_estilo_forma_request(request)
        modo_caixa_unidade = _extrair_modo_caixa_unidade_request(request)

        validade_val = (form.get("validade") or "").strip()
        data_inicio_val = (form.get("data_inicio") or "").strip()
        data_termino_val = (form.get("data_termino") or "").strip()
        obs_val = (
            form.get("observacao")
            or form.get("observação")
            or form.get("obs")
            or form.get("rodape")
            or "ou até enquanto durar o estoque"
        ).strip()

        dados = {
            "descricao_produtos": nome_prod,
            "campo_extra": (form.get("campo_extra") or "").strip(),
            "observacao_extra": (form.get("observacao_extra") or "").strip(),
            "importa": (form.get("importa") or form.get("importado") or form.get("origem") or "").strip(),
            "preco_normal": (form.get("preco_normal") or form.get("preco_de") or "").strip(),
            "preco_oferta": preco_oferta,
            "unidade_medida": (
                form.get("unidade_medida")
                or form.get("unidade")
                or form.get("un")
                or ""
            ).strip(),
            "limite_por_cliente": (form.get("limite_por_cliente") or "").strip(),
            "validade": validade_val,
            "data_inicio": data_inicio_val,
            "data_termino": data_termino_val,
            "observacao": obs_val,
        }

        caminho_fundo = resolver_imagem_fundo(request, fallback_ultimo_upload=False)

        if caminho_fundo and os.path.exists(caminho_fundo):
            try:
                with Image.open(caminho_fundo) as im_check:
                    im_check.verify()
                img_reader = ImageReader(caminho_fundo)
            except Exception:
                img_reader = None
        else:
            img_reader = None

        pdf_buffer = io.BytesIO()
        c = canvas.Canvas(pdf_buffer, pagesize=(largura_fmt, altura_fmt))
        criar_cartaz_pdf(
            c,
            dados,
            img_reader,
            largura_fmt,
            altura_fmt,
            x_offset=0,
            y_offset=0,
            fator_escala=1.0,
            formato=formato_limpo,
            forma_valor=forma_val,
            cor_forma_valor=cor_forma,
            modo_caixa_unidade=modo_caixa_unidade,
        )
        c.showPage()
        c.save()
        pdf_buffer.seek(0)

        doc = fitz.open(stream=pdf_buffer.read(), filetype="pdf")
        page = doc.load_page(0)
        pix = page.get_pixmap(dpi=115)
        doc.close()
        img_byte_arr = io.BytesIO(pix.tobytes("png"))
        img_byte_arr.seek(0)
        return send_file(img_byte_arr, mimetype="image/png")

    except Exception as e:
        return f"Erro ao gerar prévia do cartaz: {str(e)}", 500


@app.route("/gerar", methods=["POST"])
@login_obrigatorio_rota
def gerar():
    try:
        if "planilha" not in request.files:
            return "Planilha ausente no formulário!", 400

        file_planilha = request.files["planilha"]
        caminho_fundo = resolver_imagem_fundo(request, fallback_ultimo_upload=False)
        cfg = obter_config_formato(request.form.get("formato"))
        largura_fmt, altura_fmt, formato_limpo = cfg["largura"], cfg["altura"], cfg["formato"]
        forma_val, cor_forma = _extrair_estilo_forma_request(request)
        modo_caixa_unidade = _extrair_modo_caixa_unidade_request(request)

        if file_planilha.filename == "" or not caminho_fundo:
            return "Selecione a planilha (.xlsx) e uma imagem de fundo (da pasta ou por upload).", 400

        caminho_planilha = os.path.join(PASTA_UPLOAD, secure_filename(file_planilha.filename) or "planilha.xlsx")
        file_planilha.save(caminho_planilha)

        try:
            df = pd.read_excel(caminho_planilha, dtype=str)
        except Exception as e:
            return f"Erro ao ler a planilha Excel: {e}", 400

        for f in os.listdir(PASTA_SAIDA):
            try:
                os.remove(os.path.join(PASTA_SAIDA, f))
            except Exception:
                pass

        try:
            with Image.open(caminho_fundo) as im_check:
                im_check.verify()
        except Exception:
            return "A imagem de fundo enviada ou selecionada é inválida ou está corrompida.", 400

        img_reader = ImageReader(caminho_fundo)
        caminhos_pdfs_individuais = []
        registros = list(df.iterrows())

        for i, dados in registros:
            nome_prod = str(dados.get("descricao_produtos", f"produto_{i}"))
            nome_limpo = "".join(c for c in nome_prod if c.isalnum() or c in (" ", "_", "-")).strip()[:30]
            if not nome_limpo:
                nome_limpo = f"item_{i}"

            caminho_pdf = os.path.join(PASTA_SAIDA, f"cartaz_{i+1}_{nome_limpo}.pdf")

            c = canvas.Canvas(caminho_pdf, pagesize=(largura_fmt, altura_fmt))
            criar_cartaz_pdf(
                c,
                dados,
                img_reader,
                largura_fmt,
                altura_fmt,
                x_offset=0,
                y_offset=0,
                fator_escala=1.0,
                formato=formato_limpo,
                forma_valor=forma_val,
                cor_forma_valor=cor_forma,
                modo_caixa_unidade=modo_caixa_unidade,
            )
            c.showPage()
            c.save()

            caminhos_pdfs_individuais.append(caminho_pdf)

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        nome_zip = f"cartaz_{timestamp}.zip"
        pdf_unico_path = os.path.join(PASTA_TEMP, f"todos_os_cartazes_{timestamp}.pdf")
        documento_final = fitz.open()

        for caminho_pdf in caminhos_pdfs_individuais:
            with fitz.open(caminho_pdf) as m_pdf:
                documento_final.insert_pdf(m_pdf)

        documento_final.save(pdf_unico_path)
        documento_final.close()

        zip_path = os.path.join(PASTA_TEMP, nome_zip)

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zipf:
            zipf.write(pdf_unico_path, arcname="todos_os_cartazes.pdf")
            for root, _, files in os.walk(PASTA_SAIDA):
                for file in files:
                    zipf.write(os.path.join(root, file), arcname=f"individuais/{file}")

        return send_file(zip_path, as_attachment=True, download_name=nome_zip)

    except Exception as e:
        return f"Erro interno no servidor: {str(e)}", 500


@app.route("/gerar_unico", methods=["POST"])
@login_obrigatorio_rota
def gerar_unico():
    try:
        if "planilha" not in request.files:
            return "Planilha ausente no formulário!", 400

        file_planilha = request.files["planilha"]
        caminho_fundo = resolver_imagem_fundo(request, fallback_ultimo_upload=False)
        codigo_buscado = request.form.get("codigo_produto", "").strip()
        cfg = obter_config_formato(request.form.get("formato"))
        largura_fmt, altura_fmt, formato_limpo = cfg["largura"], cfg["altura"], cfg["formato"]
        forma_val, cor_forma = _extrair_estilo_forma_request(request)
        modo_caixa_unidade = _extrair_modo_caixa_unidade_request(request)

        if not codigo_buscado:
            return "Digite o código do produto que deseja buscar.", 400
        if not caminho_fundo:
            return "Escolha uma imagem da pasta ou envie um arquivo de fundo.", 400

        caminho_planilha = os.path.join(PASTA_UPLOAD, secure_filename(file_planilha.filename) or "planilha.xlsx")
        file_planilha.save(caminho_planilha)

        try:
            df = pd.read_excel(caminho_planilha, dtype=str)
        except Exception as e:
            return f"Erro ao ler a planilha Excel: {e}", 400

        coluna_codigo = None
        for col in df.columns:
            col_lower = col.lower()
            if any(termo in col_lower for termo in ["codigo", "cod", "id", "sku", "referencia"]):
                coluna_codigo = col
                break

        if not coluna_codigo:
            coluna_codigo = df.columns[0]

        produto_encontrado = df[df[coluna_codigo].astype(str).str.strip() == codigo_buscado]

        if produto_encontrado.empty:
            return f"Produto com o código '{codigo_buscado}' não foi encontrado na planilha.", 404

        try:
            with Image.open(caminho_fundo) as im_check:
                im_check.verify()
        except Exception:
            return "A imagem de fundo enviada ou selecionada é inválida.", 400

        img_reader = ImageReader(caminho_fundo)
        dados = produto_encontrado.iloc[0]
        caminho_pdf = os.path.join(PASTA_TEMP, f"cartaz_{codigo_buscado}.pdf")

        c = canvas.Canvas(caminho_pdf, pagesize=(largura_fmt, altura_fmt))
        criar_cartaz_pdf(
            c,
            dados,
            img_reader,
            largura_fmt,
            altura_fmt,
            x_offset=0,
            y_offset=0,
            fator_escala=1.0,
            formato=formato_limpo,
            forma_valor=forma_val,
            cor_forma_valor=cor_forma,
            modo_caixa_unidade=modo_caixa_unidade,
        )
        c.showPage()
        c.save()

        return send_file(caminho_pdf, as_attachment=True, download_name=f"cartaz_{codigo_buscado}.pdf")

    except Exception as e:
        return f"Erro interno no servidor: {str(e)}", 500


@app.route("/gerar_avulso", methods=["POST"])
@login_obrigatorio_rota
def gerar_avulso():
    try:
        form = request.form
        nome_prod = (form.get("descricao_produtos") or form.get("nome_produto") or "").strip()
        preco_oferta = (form.get("preco_oferta") or form.get("preco_por") or "").strip()
        cfg = obter_config_formato(form.get("formato"))
        largura_fmt, altura_fmt, formato_limpo = cfg["largura"], cfg["altura"], cfg["formato"]
        forma_val, cor_forma = _extrair_estilo_forma_request(request)
        modo_caixa_unidade = _extrair_modo_caixa_unidade_request(request)

        if not nome_prod or not preco_oferta:
            return "Preencha o Nome do Produto e o Preço 'Por'.", 400

        validade_val = (form.get("validade") or "").strip()
        data_inicio_val = (form.get("data_inicio") or "").strip()
        data_termino_val = (form.get("data_termino") or "").strip()
        obs_val = (
            form.get("observacao")
            or form.get("observação")
            or form.get("obs")
            or form.get("rodape")
            or "ou até enquanto durar o estoque"
        ).strip()

        dados = {
            "descricao_produtos": nome_prod,
            "campo_extra": (form.get("campo_extra") or "").strip(),
            "observacao_extra": (form.get("observacao_extra") or "").strip(),
            "importa": (form.get("importa") or form.get("importado") or form.get("origem") or "").strip(),
            "preco_normal": (form.get("preco_normal") or form.get("preco_de") or "").strip(),
            "preco_oferta": preco_oferta,
            "unidade_medida": (
                form.get("unidade_medida")
                or form.get("unidade")
                or form.get("un")
                or ""
            ).strip(),
            "limite_por_cliente": (form.get("limite_por_cliente") or "").strip(),
            "validade": validade_val,
            "data_inicio": data_inicio_val,
            "data_termino": data_termino_val,
            "observacao": obs_val,
        }

        caminho_fundo = resolver_imagem_fundo(request, fallback_ultimo_upload=False)

        if caminho_fundo and os.path.exists(caminho_fundo):
            try:
                with Image.open(caminho_fundo) as im_check:
                    im_check.verify()
                img_reader = ImageReader(caminho_fundo)
            except Exception:
                img_reader = None
        else:
            img_reader = None

        nome_limpo = "".join(c for c in nome_prod if c.isalnum() or c in (" ", "_", "-")).strip()[:30] or "cartaz"
        caminho_pdf = os.path.join(PASTA_TEMP, f"avulso_{nome_limpo}.pdf")

        c = canvas.Canvas(caminho_pdf, pagesize=(largura_fmt, altura_fmt))
        criar_cartaz_pdf(
            c,
            dados,
            img_reader,
            largura_fmt,
            altura_fmt,
            x_offset=0,
            y_offset=0,
            fator_escala=1.0,
            formato=formato_limpo,
            forma_valor=forma_val,
            cor_forma_valor=cor_forma,
            modo_caixa_unidade=modo_caixa_unidade,
        )
        c.showPage()
        c.save()

        return send_file(caminho_pdf, as_attachment=True, download_name=f"cartaz_{nome_limpo}.pdf")

    except Exception as e:
        return f"Erro interno no servidor: {str(e)}", 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=True, port=2000, threaded=True, use_reloader=False)
