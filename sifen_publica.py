import re
from urllib.parse import urlparse, parse_qs, unquote

DNIT_PUBLIC_HOSTS = {"ekuatia.set.gov.py", "www.ekuatia.set.gov.py"}
DNIT_PUBLIC_PATH = "/consultas/"

class SifenPublicError(Exception):
    pass

def validar_cdc(cdc):
    cdc = re.sub(r"\s+", "", str(cdc or ""))
    if not re.fullmatch(r"\d{44}", cdc):
        raise ValueError("El CDC debe contener exactamente 44 dígitos numéricos.")
    return cdc

def _public_url(cdc):
    cdc = validar_cdc(cdc)
    return f"https://ekuatia.set.gov.py/consultas/?cdc={cdc}"

def extraer_cdc_de_qr(qr_url):
    if not qr_url:
        raise ValueError("No se recibió el contenido del QR.")

    raw = str(qr_url).strip()
    parsed = urlparse(raw)

    # El QR oficial puede contener parámetros URL. Solo aceptamos hosts
    # oficiales de e-Kuatia; nunca seguimos URLs arbitrarias.
    if parsed.scheme not in ("http", "https") or parsed.hostname not in DNIT_PUBLIC_HOSTS:
        raise ValueError("El QR no corresponde a un dominio público oficial de e-Kuatia.")

    params = parse_qs(parsed.query)
    candidatos = []
    for key in ("Id", "id", "CDC", "cdc"):
        candidatos.extend(params.get(key, []))

    for candidato in candidatos:
        candidato = unquote(candidato).strip()
        if re.fullmatch(r"\d{44}", candidato):
            return candidato

    # También soportamos un CDC escrito directamente como parte de la URL.
    match = re.search(r"(?<!\d)(\d{44})(?!\d)", raw)
    if match:
        return match.group(1)

    raise ValueError("No se encontró un CDC válido de 44 dígitos en el QR.")

def consulta_publica_info(cdc):
    cdc = validar_cdc(cdc)
    return {
        "cdc": cdc,
        "fuente": "DNIT_PUBLICA",
        "estado": "REQUIERE_VERIFICACION_PUBLICA",
        "public_url": _public_url(cdc),
        "captcha_required_by_dnit": True,
        "certificado_requerido": False,
        "automatico_desde_backend": False,
        "mensaje": (
            "La consulta pública de DNIT por CDC requiere reCAPTCHA. "
            "Kakuaa no intenta resolver ni evadir ese control. "
            "El certificado digital tampoco es necesario para esta vía pública."
        ),
    }
