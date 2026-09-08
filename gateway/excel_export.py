"""Exportación XLSX sin dependencias externas.

XLSX es un conjunto de documentos XML dentro de un ZIP. Este escritor pequeño
genera una hoja ejecutiva con KPIs arriba y el detalle auditable debajo.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime
from typing import Any
from xml.sax.saxutils import escape


def _cell(ref: str, value: Any, style: int = 0) -> str:
    """Genera una celda OOXML escapada y conserva números como números."""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
    text = escape("" if value is None else str(value))
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def _column(index: int) -> str:
    """Convierte índices humanos (1, 27) en columnas Excel (A, AA)."""

    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def build_logs_xlsx(model: str, day: str, rows: list[dict[str, Any]], kpis: dict[str, Any]) -> bytes:
    """Construye un XLSX autocontenido sin depender de una suite ofimática.

    Un fichero ``.xlsx`` es un ZIP de piezas XML relacionadas. Generarlo aquí
    mantiene la instalación ligera y permite controlar cabeceras, filtros,
    anchos y tipos de dato de forma completamente determinista.
    """

    labels = ["Peticiones", "Éxito (%)", "Errores", "Latencia media (ms)", "P95 (ms)", "TTFT medio (ms)", "Alertas guardrail", "Tokens entrada", "Tokens salida", "Tokens totales"]
    values = [kpis["requests"], kpis["success_rate"], kpis["errors"], kpis["avg_duration_ms"],
              kpis["p95_duration_ms"], kpis["avg_ttft_ms"], kpis["guardrail_warnings"], kpis["prompt_tokens"], kpis["completion_tokens"], kpis["total_tokens"]]
    xml_rows = [f'<row r="1" ht="28">{_cell("A1", "IA Gateway · Informe diario", 1)}</row>',
                f'<row r="2">{_cell("A2", f"Modelo: {model} · Fecha Europe/Madrid: {day}", 2)}</row>']
    for number, (label, value) in enumerate(zip(labels, values), start=4):
        xml_rows.append(f'<row r="{number}">{_cell(f"A{number}", label, 3)}{_cell(f"B{number}", value if value is not None else "n.a.", 4)}</row>')
    headers = ["Fecha Madrid", "Estado", "Origen", "Proveedor", "TTFT ms", "Duración ms", "Guardrail", "Motivo guardrail", "Pregunta / entrada", "Respuesta / salida", "Error"]
    header_row = 15
    xml_rows.append(f'<row r="{header_row}">' + "".join(_cell(f"{_column(i)}{header_row}", value, 5) for i, value in enumerate(headers, 1)) + "</row>")
    for row_number, item in enumerate(rows, start=header_row + 1):
        request = json.dumps(item.get("request"), ensure_ascii=False, default=str)
        response = json.dumps(item.get("response"), ensure_ascii=False, default=str)
        values = [item.get("started_at") or item.get("created_at"), item.get("status"), item.get("origin_ip"),
                  item.get("provider_ip"), item.get("ttft_ms"), item.get("duration_ms"), item.get("guardrail_status"),
                  item.get("guardrail_reason"), request, response, item.get("error")]
        xml_rows.append(f'<row r="{row_number}">' + "".join(_cell(f"{_column(i)}{row_number}", value, 6 if i >= 8 else 0) for i, value in enumerate(values, 1)) + "</row>")
    last_row = max(header_row, header_row + len(rows))
    sheet = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="15" topLeftCell="A16" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols><col min="1" max="1" width="24" customWidth="1"/><col min="2" max="8" width="18" customWidth="1"/><col min="9" max="11" width="48" customWidth="1"/></cols><sheetData>{''.join(xml_rows)}</sheetData><autoFilter ref="A15:K{last_row}"/></worksheet>'''
    styles = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="4"><font><sz val="11"/><name val="Aptos"/></font><font><b/><sz val="18"/><color rgb="FFFFFFFF"/><name val="Aptos Display"/></font><font><i/><color rgb="FF44546A"/></font><font><b/><color rgb="FFFFFFFF"/></font></fonts><fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFEA580C"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FF102034"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="7"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0"/><xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="3" fillId="3" borderId="0" xfId="0"/><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="right"/></xf><xf numFmtId="0" fontId="3" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment wrapText="1"/></xf><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf></cellXfs></styleSheet>'''
    files = {"[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>',
             "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
             "xl/workbook.xml": '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Logs" sheetId="1" r:id="rId1"/></sheets></workbook>',
             "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
             "xl/worksheets/sheet1.xml": sheet, "xl/styles.xml": styles}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items(): archive.writestr(name, content)
    return output.getvalue()
