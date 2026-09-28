import os
import re
import time
import xml.etree.ElementTree as ET
import requests

SIFEN_NS = "http://ekuatia.set.gov.py/sifen/xsd"
SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"

def validar_cdc(cdc):
    cdc = re.sub(r"\s+", "", str(cdc or ""))
    if not re.fullmatch(r"\d{44}", cdc):
        raise ValueError("El CDC debe contener exactamente 44 dígitos numéricos.")
    return cdc

def _local(tag):
    return tag.rsplit("}", 1)[-1]

def _text(root, name):
    for node in root.iter():
        if _local(node.tag) == name:
            return (node.text or "").strip()
    return ""

def _serialize_element(node):
    if node is None:
        return None
    return ET.tostring(node, encoding="unicode")

def consultar_cdc_sifen(cdc, ambiente=None):
    """
    Consulta oficial SIFEN por CDC mediante WS Consulta DE (siConsDE).
    Requiere certificado digital de cliente con autenticación TLS mutua.
    Variables:
      SIFEN_AMBIENTE=test|produccion
      SIFEN_CERT_PATH=/ruta/cert.pem
      SIFEN_KEY_PATH=/ruta/key.pem
      SIFEN_CA_BUNDLE=/ruta/ca.pem (opcional)
      SIFEN_TIMEOUT=30 (opcional)
    """
    cdc = validar_cdc(cdc)
    ambiente = (ambiente or os.environ.get("SIFEN_AMBIENTE", "test")).strip().lower()
    if ambiente in ("prod", "produccion", "production"):
        endpoint = "https://sifen.set.gov.py/de/ws/consultas/consulta.wsdl"
    else:
        endpoint = "https://sifen-test.set.gov.py/de/ws/consultas/consulta.wsdl"

    cert_path = os.environ.get("SIFEN_CERT_PATH", "").strip()
    key_path = os.environ.get("SIFEN_KEY_PATH", "").strip()
    ca_bundle = os.environ.get("SIFEN_CA_BUNDLE", "").strip() or True
    if not cert_path or not key_path:
        raise RuntimeError(
            "La consulta SIFEN requiere configurar SIFEN_CERT_PATH y SIFEN_KEY_PATH "
            "con un certificado digital de cliente y su clave privada."
        )

    # dId: identificador de control de llamada. Usamos un valor numérico de 14 dígitos.
    d_id = str(time.time_ns() // 1_000_000)[-14:]
    body = f'''<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="{SOAP_NS}">
  <soap:Header/>
  <soap:Body>
    <rEnviConsDe xmlns="{SIFEN_NS}">
      <dId>{d_id}</dId>
      <dCDC>{cdc}</dCDC>
    </rEnviConsDe>
  </soap:Body>
</soap:Envelope>'''

    headers = {
        "Content-Type": "application/soap+xml; charset=utf-8",
        "Accept": "application/soap+xml, application/xml, text/xml",
        "User-Agent": "Kakuaa-ERP/1.0",
    }

    try:
        response = requests.post(
            endpoint,
            data=body.encode("utf-8"),
            headers=headers,
            cert=(cert_path, key_path),
            verify=ca_bundle,
            timeout=float(os.environ.get("SIFEN_TIMEOUT", "30")),
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"No se pudo conectar con SIFEN: {exc}") from exc

    if response.status_code >= 400:
        raise RuntimeError(
            f"SIFEN respondió HTTP {response.status_code}. "
            "Verificá certificado, ambiente y conectividad."
        )

    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise RuntimeError("SIFEN devolvió una respuesta XML inválida.") from exc

    codigo = _text(root, "dCodRes")
    mensaje = _text(root, "dMsgRes")
    fecha_proceso = _text(root, "dFecProc")

    # 0422 = CDC encontrado. El XML del DE viene dentro de xContenDE/rContDe/rDE.
    de_node = None
    for node in root.iter():
        if _local(node.tag) == "rDE":
            de_node = node
            break

    resultado = {
        "ok": codigo == "0422",
        "cdc": cdc,
        "codigo": codigo,
        "mensaje": mensaje,
        "fecha_proceso": fecha_proceso,
        "endpoint": endpoint,
        "d_id": d_id,
    }

    if codigo == "0420":
        resultado["estado"] = "NO_ENCONTRADO"
        return resultado
    if codigo == "0421":
        resultado["estado"] = "SIN_PERMISO"
        return resultado
    if codigo != "0422":
        resultado["estado"] = "ERROR_SIFEN"
        return resultado

    resultado["estado"] = "ENCONTRADO"
    resultado["xml_de"] = _serialize_element(de_node) if de_node is not None else None

    if de_node is not None:
        # Extraemos un resumen tolerante a cambios de versión: el XML completo queda intacto.
        campos = {}
        aliases = {
            "fecha_emision": ("dFeEmiDE",),
            "ruc_emisor": ("dRucEm",),
            "razon_social_emisor": ("dNomEmi",),
            "ruc_receptor": ("dRucRec",),
            "razon_social_receptor": ("dNomRec",),
            "moneda": ("cMoneOpe",),
            "total": ("dTotGralOpe",),
            "total_iva": ("dTotIVA",),
            "timbrado": ("dNumTim",),
            "establecimiento": ("dEst",),
            "punto_expedicion": ("dPunExp",),
            "numero_documento": ("dNumDoc",),
        }
        for key, names in aliases.items():
            for name in names:
                value = _text(de_node, name)
                if value:
                    campos[key] = value
                    break
        resultado["documento"] = campos

    return resultado
