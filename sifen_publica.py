import os
import re
import requests
from urllib.parse import urlparse, parse_qs, unquote

CONSULTA_FACTURA_BASE = os.environ.get("CONSULTA_FACTURA_BASE", "https://consultame-factura-9pu85.ondigitalocean.app").rstrip("/")
CONSULTA_FACTURA_DOCUMENT_PATH = "/v1/sifen/document/{cdc}"

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

def _consulta_factura_externa(cdc):
    """Consulta el DTE por CDC mediante el proveedor externo configurado.

    Esta es una integración servidor-a-servidor. El frontend nunca conoce
    credenciales ni llama directamente al proveedor.
    """
    url = CONSULTA_FACTURA_BASE + CONSULTA_FACTURA_DOCUMENT_PATH.format(cdc=cdc)
    timeout = float(os.environ.get("CONSULTA_FACTURA_TIMEOUT", "30"))
    headers = {"Accept": "application/json", "User-Agent": "Kakuaa-ERP/1.0"}
    api_key = os.environ.get("CONSULTA_FACTURA_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = api_key
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        return {"ok": False, "estado": "ERROR_PROVEEDOR", "mensaje": f"No se pudo conectar con ConsultaMe Factura: {exc}"}
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text[:5000]}
    if response.status_code >= 400:
        mensaje = payload.get("message") if isinstance(payload, dict) else None
        return {"ok": False, "estado": "NO_ENCONTRADO" if response.status_code == 404 else "ERROR_PROVEEDOR", "mensaje": mensaje or f"ConsultaMe Factura respondió HTTP {response.status_code}", "proveedor_http": response.status_code}
    if not isinstance(payload, dict):
        return {"ok": False, "estado": "RESPUESTA_INVALIDA", "mensaje": "El proveedor externo no devolvió un objeto JSON."}
    payload.setdefault("cdc", cdc)
    payload["fuente"] = "CONSULTA_FACTURA_API"
    payload["proveedor"] = "consultame-factura"
    payload["proveedor_url"] = url
    payload["certificado_requerido"] = False
    payload["automatico_desde_backend"] = True
    payload["public_only"] = False
    return payload

def consulta_publica_info(cdc):
    cdc = validar_cdc(cdc)

    # Mientras KAKUAA no tenga su propio certificado, la consulta automática
    # se realiza mediante la API externa. El día que se configure el .p12 de
    # KAKUAA, esa vía pasa a ser prioritaria y el proveedor externo deja de
    # participar en las consultas.
    p12_path = os.environ.get("KAKUAA_SIFEN_P12_PATH", "").strip()
    if p12_path and os.path.isfile(p12_path):
        try:
            from sifen_consulta import consultar_cdc_sifen
            resultado = consultar_cdc_sifen(
                cdc,
                ambiente=os.environ.get("KAKUAA_SIFEN_AMBIENTE", os.environ.get("SIFEN_AMBIENTE", "prod")),
                p12_path=p12_path,
                p12_password=os.environ.get("KAKUAA_SIFEN_P12_PASSWORD", ""),
                ca_bundle=os.environ.get("SIFEN_CA_BUNDLE", "") or None,
            )
            resultado["fuente"] = "SIFEN_KAKUAA_P12"
            resultado["proveedor_externo_desactivado"] = True
            resultado["public_only"] = False
            return resultado
        except Exception as exc:
            # No hacemos fallback silencioso al proveedor externo cuando ya
            # existe un certificado de KAKUAA: evitamos mezclar identidades.
            return {
                "ok": False,
                "cdc": cdc,
                "fuente": "SIFEN_KAKUAA_P12",
                "estado": "ERROR_CERTIFICADO_KAKUAA",
                "certificado_requerido": True,
                "automatico_desde_backend": True,
                "public_only": False,
                "mensaje": f"KAKUAA tiene configurado su certificado SIFEN, pero la consulta falló: {exc}",
            }

    # Fase actual: API externa. Si falla, devolvemos además el enlace oficial
    # de DNIT como respaldo manual; nunca resolvemos CAPTCHA automáticamente.
    externo = _consulta_factura_externa(cdc)
    if externo.get("ok") is True or externo.get("estado") not in {"ERROR_PROVEEDOR", "RESPUESTA_INVALIDA"}:
        externo["public_url"] = _public_url(cdc)
        externo["captcha_required_by_dnit"] = False
        return externo

    return {
        "cdc": cdc,
        "fuente": "CONSULTA_FACTURA_API",
        "estado": externo.get("estado", "ERROR_PROVEEDOR"),
        "public_url": _public_url(cdc),
        "captcha_required_by_dnit": True,
        "certificado_requerido": False,
        "automatico_desde_backend": False,
        "public_only": True,
        "mensaje": externo.get("mensaje") or "No fue posible obtener el DTE mediante la API externa. Podés verificarlo manualmente en DNIT.",
        "proveedor_http": externo.get("proveedor_http"),
    }
