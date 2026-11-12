"""Excel de salida: pestañas de trabajo comercial con cabeceras fijas, filtros, colores por prioridad y
fórmulas en la base completa (segmento, palanca y prioridad se recalculan al rellenar las columnas ✎)."""
from collections import Counter
from datetime import datetime

from openpyxl import Workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from . import reglas as R

CABECERA = PatternFill("solid", fgColor="1F2937")
EDITABLE = PatternFill("solid", fgColor="FFF7D6")
COLOR = {"A": "FDE2E1", "B": "FFF1CC", "C": "E8EEF6", "DESCARTADA": "EEEEEE"}
FONDO = {k: PatternFill("solid", fgColor=v) for k, v in COLOR.items()}

BASE = [
    ("CIF", "cif"), ("Razón social", "razon_social"), ("Tipo de entidad", "tipo"), ("Estado sociedad", "estado"),
    ("Fecha constitución", "constitucion"), ("Antigüedad (meses)", "antiguedad"), ("Dirección", "direccion"), ("CP", "cp"),
    ("Municipio", "municipio"), ("Zona", "zona"), ("Administradores (BORME)", "administradores"),
    ("Kit Digital fecha", "kit_fecha"), ("Kit Digital importe (€)", "kit_importe"), ("Kit Digital segmento", "kit_segmento"),
    ("BONO_KIT_VIVO_URGENTE", "kit_urgente"), ("Kit Consulting fecha", "kitc_fecha"), ("Ayudas IVACE (nº)", "ivace"),
    ("Ayudas Cámara Alicante (nº)", "camara"), ("SOLVENCIA_FISCAL_OK (indicio)", "solvencia"), ("Ayudas BDNS (nº)", "n_ayudas"), ("Detalle ayudas (últimas)", "detalle_ayudas"),
    ("Sector estimado", "sector_est"), ("Sector (confirmado) ✎", "sector_conf"), ("Segmento estimado", "seg_est"),
    ("Empleados (confirmado) ✎", "emp_conf"), ("Teléfono ✎", "telefono"),
    ("Segmento", "segmento"), ("Palanca recomendada", "palanca"), ("Prioridad", "prioridad"),
    ("Motivo descarte", "descarte"), ("Estado consulta", "consulta"), ("Fuentes", "fuentes"),
]
EDITABLES = {"sector_conf", "emp_conf", "telefono"}


def _cabecera(ws, titulos):
    ws.append(titulos)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = CABECERA
        c.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    ws.row_dimensions[1].height = 32


def _anchos(ws):
    for col in ws.columns:
        largo = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(max(10, largo + 2), 60)


def _tabla(ws, titulos, filas, prioridad_col=None):
    _cabecera(ws, titulos)
    for f in filas:
        ws.append(f)
        if prioridad_col is not None and f[prioridad_col] in FONDO:
            for c in ws[ws.max_row]:
                c.fill = FONDO[f[prioridad_col]]
    ws.auto_filter.ref = ws.dimensions
    _anchos(ws)


def _formulas(col):
    """Fórmulas de segmento, palanca y prioridad de la fila {r} (equivalentes a reglas.py)."""
    c = {k: get_column_letter(i + 1) for i, (_, k) in enumerate(BASE)}
    emp, seg_est, sec = f"{c['emp_conf']}{{r}}", f"{c['seg_est']}{{r}}", f"IF({c['sector_conf']}{{r}}<>\"\",{c['sector_conf']}{{r}},{c['sector_est']}{{r}})"
    seg = f"{c['segmento']}{{r}}"
    segmento = (f'=IF(ISNUMBER({emp}),IF({emp}<=2,"{R.S3}",IF({emp}<=9,"{R.S2}",IF({emp}<=49,"{R.S1}",'
                f'IF({emp}<=249,"{R.S45}","{R.S250}")))),{seg_est})')
    palanca = (f'=IF({c["descarte"]}{{r}}<>"","—",IF({seg}="{R.S3}","{R.P_MICRO}",IF({seg}="{R.S2}","{R.P_CAMARA}",'
               f'IF({seg}="{R.S1}",IF({sec}="{R.INDUSTRIAL}","{R.P_IVACE}",IF({sec}="{R.SERVICIOS}","{R.P_CAMARA}","{R.P_I_SIN_SECTOR}")),'
               f'IF({seg}="{R.S45}",IF({sec}="{R.INDUSTRIAL}","{R.P_IVACE}",IF({sec}="{R.SERVICIOS}","{R.P_45_SERVICIOS}","{R.P_45_SIN_SECTOR}")),'
               f'IF({seg}="{R.S250}","—","{R.P_PENDIENTE}"))))))')
    tel = f'LEN({c["telefono"]}{{r}})>=9'
    prioridad = (f'=IF(OR({c["descarte"]}{{r}}<>"",{seg}="{R.S250}"),"DESCARTADA",IF({c["kit_urgente"]}{{r}}="SÍ","A",'
                 f'IF(AND(OR({seg}="{R.S2}",{seg}="{R.S1}"),OR(N({c["ivace"]}{{r}})>0,N({c["camara"]}{{r}})>0),{tel}),"A",'
                 f'IF(AND(OR({seg}="{R.S2}",{seg}="{R.S1}",{seg}="{R.S45}"),{tel}),"B","C"))))')
    return {"segmento": segmento, "palanca": palanca, "prioridad": prioridad}, c


def generar(ruta, registros, contexto):
    """registros: dicts ya clasificados (ver cli.clasificar). contexto: datos para el resumen."""
    wb = Workbook()
    validos = [x for x in registros if x["prioridad"] != "DESCARTADA"]
    orden_ruta = lambda x: (x["zona_idx"], x["cp"] or "", x["municipio"] or "", x["prioridad"])  # noqa: E731

    # 1. Resumen ejecutivo
    ws = wb.active
    ws.title = "Resumen_Ejecutivo"
    ws.append(["Resumen ejecutivo", ""])
    ws["A1"].font = Font(bold=True, size=14)
    filas = [("Lote", contexto["lote"]), ("Generado", datetime.now().strftime("%Y-%m-%d %H:%M")),
             ("CIF procesados", len(registros)), ("Válidos (A/B/C)", len(validos)),
             ("Descartados", len(registros) - len(validos)), ("", "")]
    filas.append(("Descartes por motivo", ""))
    filas += [(f"  {m}", n) for m, n in Counter(x["descarte"] or R.S250 for x in registros if x["prioridad"] == "DESCARTADA").most_common()]
    filas += [("", ""), ("Reparto por prioridad", "")]
    filas += [(f"  Prioridad {p}", sum(1 for x in validos if x["prioridad"] == p)) for p in "ABC"]
    filas += [("", ""), ("Reparto por zona (válidos)", "")]
    filas += [(f"  {z}", sum(1 for x in validos if x["zona"] == z)) for z in contexto["zonas"]]
    filas += [("", ""), ("Candidatas por palanca 2027", "")]
    filas += [("  IVACE (DIGITALIZA-CV)", sum(1 for x in validos if x["palanca"] == R.P_IVACE)),
              ("  Cámara de Alicante (Pyme Digital / Cibersegura)", sum(1 for x in validos if x["palanca"] == R.P_CAMARA)),
              ("  Microempresas (Auditoría + Alerta Cámara)", sum(1 for x in validos if x["palanca"] == R.P_MICRO)),
              ("  Por cualificar (empleados o sector sin confirmar)", sum(1 for x in validos if x["palanca"] in (R.P_PENDIENTE, R.P_I_SIN_SECTOR, R.P_45_SIN_SECTOR))),
              ("", ""), ("Alertas", ""),
              ("  BONO_KIT_VIVO_URGENTE (Kit Digital < 6 meses)", sum(1 for x in validos if x["kit_urgente"] == "SÍ")),
              ("  Con historial IVACE o Cámara", sum(1 for x in validos if (x["ivace"] or 0) + (x["camara"] or 0) > 0)),
              ("", ""), ("Fuentes y cobertura", ""),
              ("  BDNS (infosubvenciones.es)", f"consultados {contexto['bdns_ok']} CIF; errores {contexto['bdns_error']}"),
              ("  BORME Alicante (datos abiertos BOE)", contexto["borme_cobertura"]),
              ("", ""), ("Uso de los datos", ""),
              ("  Email", "No enviar publicidad por email sin consentimiento previo (art. 21 LSSI). Usar teléfono o visita."),
              ("  Administradores (BORME)", "Informar en el primer contacto: quién eres, finalidad y cómo oponerse (art. 14 RGPD)."),
              ("  Oposición", "Si alguien se opone: worker excluir <CIF> (no vuelve a aparecer en lotes futuros)."),
              ("  Columnas ✎", "Rellena Empleados, Sector y Teléfono en la base completa: segmento, palanca y prioridad se recalculan.")]
    for f in filas:
        ws.append(list(f))
        if f[0] and not f[0].startswith("  ") and f[1] == "":
            ws.cell(ws.max_row, 1).font = Font(bold=True)
    ws.column_dimensions["A"].width = 58
    ws.column_dimensions["B"].width = 90

    # 2. Prioridad A
    col_a = ["Zona", "Municipio", "CP", "CIF", "Razón social", "Dirección", "Teléfono", "Administradores (BORME)", "Motivo prioridad",
             "Kit Digital fecha", "Segmento", "Palanca recomendada"]
    a = sorted([x for x in validos if x["prioridad"] == "A"], key=lambda x: (x["zona_idx"], x["municipio"] or ""))
    _tabla(wb.create_sheet("Prioridad_A_Urgentes"), col_a, [
        [x["zona"], x["municipio"], x["cp"], x["cif"], x["razon_social"], x["direccion"], x["telefono"], x["administradores"],
         x["motivo_prioridad"], x["kit_fecha"], x["segmento"], x["palanca"]] for x in a])

    # 3. Ruta por zonas (A y B)
    col_r = ["Zona", "CP", "Municipio", "Prioridad", "CIF", "Razón social", "Dirección", "Teléfono", "Segmento", "Palanca recomendada", "Administradores (BORME)"]
    _tabla(wb.create_sheet("Ruta_Por_Zonas_Alicante"), col_r, [
        [x["zona"], x["cp"], x["municipio"], x["prioridad"], x["cif"], x["razon_social"], x["direccion"], x["telefono"], x["segmento"],
         x["palanca"], x["administradores"]] for x in sorted([x for x in validos if x["prioridad"] in "AB"], key=orden_ruta)], prioridad_col=3)

    # 3b. Por cualificar en la llamada: sin empleados o sin teléfono, con prioridad provisional por subvenciones
    col_q = ["Zona", "CP", "Municipio", "Pista", "CIF", "Razón social", "Dirección", "Administradores (BORME)", "Kit Digital segmento",
             "Detalle ayudas (últimas)", "Qué preguntar"]
    # Solo las ya consultadas: en un piloto (--limit) el resto aún no tiene datos.
    pendientes = [x for x in validos if x["prioridad"] == "C" and x["segmento"] != R.S3 and x["consulta"] == "ok"]
    pista = lambda x: 0 if (x["ivace"] or x["camara"]) else 1 if x["kit_fecha"] else 2 if x["n_ayudas"] else 3  # noqa: E731
    _tabla(wb.create_sheet("Por_Cualificar_Llamada"), col_q, [
        [x["zona"], x["cp"], x["municipio"], ["Historial IVACE/Cámara", "Tuvo Kit Digital", "Otras ayudas públicas", "Sin historial de ayudas"][pista(x)], x["cif"], x["razon_social"],
         x["direccion"], x["administradores"], x["kit_segmento"], x["detalle_ayudas"],
         "; ".join(p for p, falta in (("nº de empleados", not x["emp_conf"]), ("sector (industrial o servicios)", not x["sector_conf"] and not x["sector_est"]),
                                      ("teléfono de contacto", not x["telefono"])) if falta)]
        for x in sorted(pendientes, key=lambda x: (pista(x), x["zona_idx"], x["cp"] or ""))])

    # 4. Base completa con fórmulas
    ws = wb.create_sheet("Base_Completa_Enriquecida")
    _cabecera(ws, [t for t, _ in BASE])
    formulas, letras = _formulas(None)
    for r, x in enumerate(registros, start=2):
        fila = []
        for _, k in BASE:
            fila.append(formulas[k].format(r=r) if k in formulas else x.get(k))
        ws.append(fila)
        for k in EDITABLES:
            ws[f"{letras[k]}{r}"].fill = EDITABLE
    ultima = max(2, len(registros) + 1)
    rango = f"A2:{get_column_letter(len(BASE))}{ultima}"
    for p, fill in FONDO.items():
        ws.conditional_formatting.add(rango, FormulaRule(formula=[f'${letras["prioridad"]}2="{p}"'], fill=fill))
    dv = DataValidation(type="list", formula1=f'"{R.INDUSTRIAL},{R.SERVICIOS}"', allow_blank=True)
    dv.add(f"{letras['sector_conf']}2:{letras['sector_conf']}{ultima}")
    dn = DataValidation(type="whole", operator="between", formula1="0", formula2="100000", allow_blank=True)
    dn.add(f"{letras['emp_conf']}2:{letras['emp_conf']}{ultima}")
    ws.add_data_validation(dv)
    ws.add_data_validation(dn)
    ws.auto_filter.ref = ws.dimensions
    _anchos(ws)
    for k in ("segmento", "palanca", "prioridad"):
        ws.column_dimensions[letras[k]].width = {"segmento": 26, "palanca": 58, "prioridad": 11}[k]

    # 5. Descartadas y errores
    _tabla(wb.create_sheet("Descartadas_y_Errores"), ["CIF", "Razón social", "Municipio", "Motivo", "Estado consulta"], [
        [x["cif"], x["razon_social"], x["municipio"], x["descarte"] or ("250 o más empleados" if x["segmento"] == R.S250 else ""), x["consulta"]]
        for x in registros if x["prioridad"] == "DESCARTADA" or x["consulta"].startswith("error")])

    wb.calculation.fullCalcOnLoad = True
    wb.save(ruta)
