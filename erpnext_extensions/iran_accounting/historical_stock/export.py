# Copyright (c) 2026, ERPNext Extensions contributors
"""Read-only grid export (CSV/XLSX). No stock writes."""

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape


def xlsx_bytes(headers: list[str], rows: list[list]) -> bytes:
	def col_letter(idx: int) -> str:
		out = ""
		idx += 1
		while idx:
			idx, rem = divmod(idx - 1, 26)
			out = chr(65 + rem) + out
		return out

	def cell(r, c, value) -> str:
		text = escape("" if value is None else str(value))
		return f'<c r="{col_letter(c)}{r}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'

	sheet_rows = []
	sheet_rows.append("<row r=\"1\">" + "".join(cell(1, i, h) for i, h in enumerate(headers)) + "</row>")
	for r_i, row in enumerate(rows, 2):
		sheet_rows.append(
			f'<row r="{r_i}">' + "".join(cell(r_i, i, v) for i, v in enumerate(row)) + "</row>"
		)
	sheet = (
		'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
		'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
		"<sheetData>" + "".join(sheet_rows) + "</sheetData></worksheet>"
	)
	workbook = (
		'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
		'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
		'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
		'<sheets><sheet name="Historical Repair" sheetId="1" r:id="rId1"/></sheets></workbook>'
	)
	rels = (
		'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
		'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
		'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
		"</Relationships>"
	)
	ctypes = (
		'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
		'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
		'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
		'<Default Extension="xml" ContentType="application/xml"/>'
		'<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
		'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
		"</Types>"
	)
	root_rels = (
		'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
		'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
		'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
		"</Relationships>"
	)
	buf = io.BytesIO()
	with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
		zf.writestr("[Content_Types].xml", ctypes)
		zf.writestr("_rels/.rels", root_rels)
		zf.writestr("xl/workbook.xml", workbook)
		zf.writestr("xl/_rels/workbook.xml.rels", rels)
		zf.writestr("xl/worksheets/sheet1.xml", sheet)
	return buf.getvalue()
