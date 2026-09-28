import os
import re
import time
import json
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
    names = name if isinstance(name, (list, tuple, set)) else (name,)
    wanted = set(names)
    for node in root.iter():
        if _local(node.tag) in wanted:
            value = (node.text or "").strip()
            if value:
                return value
    return ""

def _number(root, names):
    raw = _text(root, names)
    if not raw:
        return 0.0
    try:
        return float(raw.replace(",", "."))
    except (ValueError, TypeError):
        return 0.0

def _serialize_element(node):
    if node is None:
        return None
    return ET.tostring(node, encoding="unicode")

def _first_child(node, names):
    if node is None:
        return None
    wanted = set(names if isinstance(names, (list, tuple, set)) else (names,))
    for child in node.iter():
        if child is node:
            continue
        if _local(child.tag) in wanted:
            return child
    return None

def _parse_dte(root):
    """
    Normaliza el XML oficial de SIFEN al mismo tipo de información que
    observamos en la API pública de Consulta Factura, sin depender de ese
    servicio de terceros.
    """
    # El CDC normalmente viene en Id del rDE.
    cdc = ""
    for node in root.iter():
        ident = str(node.attrib.get("Id", "")).strip()
        if re.fullmatch(r"\d{44}", ident):
            cdc = ident
            break
    if not cdc:
        cdc = _text(root, ["CDC"])
    if not re.fullmatch(r"\d{44}", cdc or ""):
        cdc = ""

    g_timb = _first_child(root, ["gTimb"])
    g_emi = _first_child(root, ["gEmis"])
    g_datrec = _first_child(root, ["gDatRec"])
    g_tot = _first_child(root, ["gTotSub"])
    g_cam = _first_child(root, ["gCamFE"])
    g_dtip = _first_child(root, ["gDtipDE"])
    g_cam_fe = _first_child(root, ["gCamFuFD"])

    def t(node, names):
        return _text(node or root, names)

    # Datos de timbrado/documento.
    timbrado = {
        "idTipoDocumento": t(g_timb, ["iTiDE", "dTiDE"]),
        "tipoDocumento": t(g_timb, ["dDesTiDE", "dTipoDE"]),
        "numeroTimbrado": t(g_timb, ["dNumTim"]),
        "establecimiento": t(g_timb, ["dEst"]),
        "puntoExpedicion": t(g_timb, ["dPunExp"]),
        "numeroDocumento": t(g_timb, ["dNumDoc"]),
        "fechaTimbrado": t(g_timb, ["dFeIniT"]),
    }

    emisor = {
        "ruc": t(g_emi, ["dRucEm"]),
        "digitoVerificador": t(g_emi, ["dDVEmi"]),
        "razonSocial": t(g_emi, ["dNomEmi", "dRazSocEm"]),
        "direccion": t(g_emi, ["dDirEmi"]),
        "telefono": t(g_emi, ["dTelEmi"]),
        "email": t(g_emi, ["dEmailE", "dEmail"]),
    }

    receptor = {
        "ruc": t(g_datrec, ["dRucRec"]),
        "digitoVerificador": t(g_datrec, ["dDVRec"]),
        "razonSocial": t(g_datrec, ["dNomRec"]),
        "tipoDocumento": t(g_datrec, ["iNatRec", "iTiOpe"]),
    }

    # Totales. Se toman directamente del DTE, no se reconstruyen desde los ítems.
    totalDocumento = {
        "subtotalExcenta": t(g_tot, ["dSubExe"]),
        "subTotalExonerado": t(g_tot, ["dSubExo"]),
        "subTotal05": t(g_tot, ["dSub5"]),
        "subTotal10": t(g_tot, ["dSub10"]),
        "totalOperacionBruto": t(g_tot, ["dTotOpe"]),
        "totalDescuento": t(g_tot, ["dTotDesc"]),
        "totalDescuentoGlobal": t(g_tot, ["dTotDescGlotem", "dTotDescGlobal"]),
        "totalAnticipoItem": t(g_tot, ["dTotAntItem"]),
        "totalAnticipo": t(g_tot, ["dTotAnt"]),
        "descuentoTotalOperacion": t(g_tot, ["dPorcDescTotal"]),
        "descuentoTotal": t(g_tot, ["dDescTotal"]),
        "anticipo": t(g_tot, ["dAnticipo"]),
        "redondeo": t(g_tot, ["dRedon"]),
        "totalNeto": t(g_tot, ["dTotGralOpe"]),
        "iva05": t(g_tot, ["dIVA5"]),
        "iva10": t(g_tot, ["dIVA10"]),
        "totalGravada05": t(g_tot, ["dTGrav5"]),
        "totalGravada10": t(g_tot, ["dTGrav10"]),
        "totalGravada": t(g_tot, ["dTotGrav"]),
        "totalIva": t(g_tot, ["dLiqTotIVA", "dTotIVA"]),
    }

    # En algunos XML los totales están directamente bajo el árbol principal.
    fallback_total = {
        "totalNeto": "dTotGralOpe",
        "iva05": "dIVA5",
        "iva10": "dIVA10",
        "totalIva": "dLiqTotIVA",
    }
    for key, tag in fallback_total.items():
        if not totalDocumento[key]:
            totalDocumento[key] = _text(root, tag)

    formasPago = []
    # Cada gPaConE1 corresponde a una forma de pago; toleramos variantes.
    for node in root.iter():
        if _local(node.tag) not in {"gPaConE1", "gPaConE"}:
            continue
        forma = {
            "idFormaPago": _text(node, ["iTiPago"]),
            "formaPago": _text(node, ["dDesTiPag", "dTiPago"]),
            "monto": _text(node, ["dMonTiPag"]),
            "moneda": _text(node, ["cMoneTiPag"]),
        }
        if any(forma.values()):
            formasPago.append(forma)

    # Si el XML usa un contenedor gPaConE con hijos repetidos, el recorrido
    # anterior ya los cubre; si no hay formas, dejamos lista vacía.
    if not formasPago and g_dtip is not None:
        for node in g_dtip.iter():
            if _local(node.tag) in {"gPaConE1", "gPaConE"}:
                forma = {
                    "idFormaPago": _text(node, ["iTiPago"]),
                    "formaPago": _text(node, ["dDesTiPag", "dTiPago"]),
                    "monto": _text(node, ["dMonTiPag"]),
                    "moneda": _text(node, ["cMoneTiPag"]),
                }
                if any(forma.values()):
                    formasPago.append(forma)

    detalleFactura = []
    for node in root.iter():
        if _local(node.tag) != "gCamItem":
            continue
        detalleFactura.append({
            "codigoInterno": _text(node, ["dCodInt"]),
            "descripcion": _text(node, ["dDesProSer"]),
            "cantidad": _text(node, ["dCantProSer"]),
            "unidad": _text(node, ["dDesUniMed", "dDesUniMed"]),
            "precioUnitario": _text(node, ["dPUniProSer"]),
            "tasaIva": _text(node, ["dTasaIVA"]),
            "totalBruto": _number(node, ["dTotBruOpeItem"]),
            "totalOperacionItem": _text(node, ["dTotOpeItem"]),
        })

    tipoOperacion = {
        "idTipoOperacion": t(g_dtip, ["iTipTra"]),
        "tipoOperacion": t(g_dtip, ["dDesTipTra", "dCondOpe"]),
    }

    qr = t(g_cam_fe, ["dCarQR"]) or _text(root, ["dCarQR"])
    protocolo = _text(root, ["dProtAut", "dProtAutorizacion"])

    fecha_emision = _text(root, ["dFeEmiDE"])
    moneda = _text(root, ["cMoneOpe"])

    documento = {
        "cdc": cdc,
        "fecha_emision": fecha_emision,
        "ruc_emisor": emisor["ruc"],
        "razon_social_emisor": emisor["razonSocial"],
        "ruc_receptor": receptor["ruc"],
        "razon_social_receptor": receptor["razonSocial"],
        "moneda": moneda,
        "total": totalDocumento["totalNeto"] or _text(root, ["dTotGralOpe"]),
        "total_iva": totalDocumento["totalIva"] or _text(root, ["dLiqTotIVA", "dTotIVA"]),
        "timbrado": timbrado["numeroTimbrado"],
        "establecimiento": timbrado["establecimiento"],
        "punto_expedicion": timbrado["puntoExpedicion"],
        "numero_documento": timbrado["numeroDocumento"],
    }

    return {
        "CDC": cdc,
        "fechaEmision": fecha_emision,
        "timbrado": timbrado,
        "emisor": emisor,
        "totalDocumento": totalDocumento,
        "protocoloAutorizacion": protocolo,
        "receptor": receptor,
        "tipoOperacion": tipoOperacion,
        "formasPago": formasPago,
        "detalleFactura": detalleFactura,
        "qr": qr,
        "documento": documento,
        "items": detalleFactura,
    }

def consultar_cdc_sifen(cdc, ambiente=None, cert_path=None, key_path=None, ca_bundle=None):
    """
    Consulta oficial SIFEN por CDC mediante WS Consulta DE (siConsDE).
    Requiere certificado digital de cliente con autenticación TLS mutua.
    """
    cdc = validar_cdc(cdc)
    ambiente = (ambiente or os.environ.get("SIFEN_AMBIENTE", "test")).strip().lower()
    if ambiente in ("prod", "produccion", "production"):
        endpoint = "https://sifen.set.gov.py/de/ws/consultas/consulta.wsdl"
    else:
        endpoint = "https://sifen-test.set.gov.py/de/ws/consultas/consulta.wsdl"

    cert_path = str(cert_path or os.environ.get("SIFEN_CERT_PATH", "")).strip()
    key_path = str(key_path or os.environ.get("SIFEN_KEY_PATH", "")).strip()
    ca_bundle = str(ca_bundle or os.environ.get("SIFEN_CA_BUNDLE", "")).strip() or True
    if not cert_path or not key_path:
        raise RuntimeError(
            "La consulta SIFEN requiere configurar SIFEN_CERT_PATH y SIFEN_KEY_PATH "
            "con un certificado digital de cliente y su clave privada."
        )

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
        try:
            normalizado = _parse_dte(de_node)
            # Mantener ambos nombres: CDC y cdc, para facilitar consumo del
            # frontend y compatibilidad con la estructura existente.
            resultado.update(normalizado)
        except Exception as exc:
            # La consulta sigue siendo válida aunque un campo nuevo del XML no
            # sea reconocido por el normalizador.
            resultado["parse_warning"] = f"No se pudo normalizar todo el DTE: {exc}"

        # Resumen compatible con la respuesta histórica de Kakuaa.
        if "documento" not in resultado:
            resultado["documento"] = {}
        resultado["documento"].update({
            "cdc": cdc,
            "fecha_emision": _text(de_node, ["dFeEmiDE"]),
            "ruc_emisor": _text(de_node, ["dRucEm"]),
            "razon_social_emisor": _text(de_node, ["dNomEmi", "dRazSocEm"]),
            "ruc_receptor": _text(de_node, ["dRucRec"]),
            "razon_social_receptor": _text(de_node, ["dNomRec"]),
            "moneda": _text(de_node, ["cMoneOpe"]),
            "total": _text(de_node, ["dTotGralOpe"]),
            "total_iva": _text(de_node, ["dLiqTotIVA", "dTotIVA"]),
            "timbrado": _text(de_node, ["dNumTim"]),
            "establecimiento": _text(de_node, ["dEst"]),
            "punto_expedicion": _text(de_node, ["dPunExp"]),
            "numero_documento": _text(de_node, ["dNumDoc"]),
        })

    return resultado
