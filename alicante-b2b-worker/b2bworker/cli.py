"""CLI del worker. Ejecución bajo demanda por SSH; un solo proceso, prioridad baja y ritmo pausado.

  worker procesar --input archivo.xlsx [--lote NOMBRE] [--limit 20] [--resume] [--reiniciar]
  worker borme-indexar --desde 2025-11-01            índice local del BORME de Alicante (una vez; luego se pone al día)
  worker borme-probar --fecha 2026-11-11             prueba el lector con un día (sin tocar ningún lote)
  worker actualizar --excel salida/xxx.xlsx           relee empleados/sector/teléfono rellenados y regenera el Excel
  worker excel [--lote NOMBRE]                        regenera el Excel sin consultar nada
  worker excluir CIF [--motivo "..."]                 oposición: no vuelve a salir en ningún lote
  worker estado [--lote NOMBRE]
"""
import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from . import bdns, borme, cif as cifmod, db, entrada, reglas as R, telefonos
from .red import Cliente, ErrorConsulta
from .zonas import Zonas

DATOS = Path(os.environ.get("B2B_DATA", Path.home() / "alicante-b2b-worker"))
log = logging.getLogger("b2b")


def preparar():
    for d in ("entrada", "salida", "logs"):
        (DATOS / d).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S", handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(DATOS / "logs" / f"worker-{date.today():%Y%m%d}.log", encoding="utf-8")])
    try:
        os.nice(10)  # no compite con la granja
    except OSError:
        pass
    return db.abrir(DATOS / "b2b.sqlite")


# --------------------------------------------------------------------------- procesado

def cargar_lote(con, lote, ruta, reiniciar=False):
    existe = con.execute("SELECT 1 FROM lotes WHERE nombre = ?", (lote,)).fetchone()
    if existe and not reiniciar:
        return False
    con.execute("DELETE FROM empresas WHERE lote = ?", (lote,))
    con.execute("INSERT OR REPLACE INTO lotes VALUES (?, ?, ?)", (lote, str(ruta), datetime.now().isoformat(timespec="seconds")))
    excluidos = {r[0] for r in con.execute("SELECT cif FROM exclusiones")}
    vistos = set()
    for reg in entrada.leer(ruta):
        cif, tipo, motivo = cifmod.clasificar(reg["cif"])
        if cif in vistos:
            continue
        vistos.add(cif)
        if cif in excluidos:
            motivo = "Excluida a petición (oposición al tratamiento)"
        persona = tipo == "Persona física"
        con.execute("INSERT INTO empresas (lote, cif, fila, razon_social, direccion, municipio, cp, tipo, motivo_descarte, bdns_estado, borme_estado) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                        lote, cif, reg["fila"],
                        None if persona else reg.get("razon_social"), None if persona else reg.get("direccion"),  # de personas físicas no se guarda nada más
                        reg.get("municipio"), reg.get("cp"), tipo, motivo,
                        "omitida" if motivo else "pendiente", "omitida" if motivo else "pendiente"))
    con.commit()
    return True


def procesar(con, lote, limit=None, cliente=None, hoy=None):
    cliente = cliente or Cliente(pausa=(1.0, 2.5))
    hoy = hoy or date.today()
    filas = con.execute("SELECT * FROM empresas WHERE lote = ? ORDER BY fila", (lote,)).fetchall()
    if limit:
        filas = filas[:limit]
    pendientes = [f for f in filas if f["bdns_estado"] in ("pendiente", "error")]
    log.info("Lote %s: %d empresas en este pase, %d pendientes de BDNS", lote, len(filas), len(pendientes))
    cobertura = borme.cobertura(con)
    if not cobertura[2]:
        log.warning("Índice del BORME vacío: estado, antigüedad y administradores quedarán sin dato. Ejecuta antes: worker borme-indexar --desde AAAA-MM-DD")
    for n, f in enumerate(pendientes, start=1):
        try:
            concesiones = bdns.consultar(cliente, f["cif"])
            con.execute("UPDATE empresas SET bdns_estado='ok', bdns=?, bdns_error=NULL, actualizado=? WHERE lote=? AND cif=?",
                        (json.dumps(bdns.resumen(concesiones, hoy)), datetime.now().isoformat(timespec="seconds"), lote, f["cif"]))
            log.info("[%d/%d] %s: %d ayudas en BDNS", n, len(pendientes), f["cif"], len(concesiones))
        except ErrorConsulta as e:
            con.execute("UPDATE empresas SET bdns_estado='error', bdns_error=? WHERE lote=? AND cif=?", (str(e)[:200], lote, f["cif"]))
            log.warning("[%d/%d] %s: BDNS sin respuesta (%s); se reintentará con --resume", n, len(pendientes), f["cif"], e)
        con.commit()
    # BORME: consulta local, se recalcula siempre (rápido) por si el índice se amplió.
    for f in con.execute("SELECT * FROM empresas WHERE lote = ? AND cif IN (%s)" % ",".join("?" * len(filas)), [lote, *[x["cif"] for x in filas]]).fetchall():
        if f["borme_estado"] == "omitida":
            continue
        beneficiario = (db.cargar_json(f["bdns"], {}) or {}).get("beneficiario") or ""
        nombres = [f["razon_social"], beneficiario.split(" ", 1)[-1] if " " in beneficiario else ""]
        ficha = next((x for x in (borme.ficha(con, n, hoy) for n in nombres if n) if x), None)
        con.execute("UPDATE empresas SET borme_estado=?, borme=? WHERE lote=? AND cif=?",
                    ("encontrada" if ficha else "no_encontrada", json.dumps(ficha) if ficha else None, lote, f["cif"]))
    con.commit()


# --------------------------------------------------------------------------- clasificación para el Excel

def clasificar(f, zonas, hoy):
    b = db.cargar_json(f["bdns"], {}) or {}
    m = db.cargar_json(f["borme"], {}) or {}
    descarte = f["motivo_descarte"] or R.descarte_mercantil(m, hoy)
    seg_est = R.segmento_por_rango(b.get("kit_segmento"))
    emp = f["emp_conf"]
    segmento = R.segmento_por_empleados(emp) if emp is not None else seg_est
    keys = f.keys()
    web, categoria, tel_fuente = (f[k] if k in keys else None for k in ("web", "categoria", "tel_fuente"))
    sector_est = R.sector_estimado(f["razon_social"], m.get("objeto"), categoria)
    sector = f["sector_conf"] or sector_est
    telefono = f["telefono"] if R.telefono_valido(f["telefono"]) else ""
    prioridad = R.prioridad(descarte, segmento, b.get("kit_urgente"), b.get("ivace"), b.get("camara"), telefono)
    zona = zonas.zona(f["cp"], f["municipio"])
    antig = R.meses_entre(date.fromisoformat(m["constitucion"]), hoy) if m.get("constitucion") else None
    if f["borme_estado"] == "omitida":
        estado_soc = ""
    elif m:
        estado_soc = m["estado"]
    elif f["tipo"] in ("Comunidad de bienes", "Sociedad civil", "Asociación / fundación", "Comunidad de propietarios"):
        estado_soc = "No inscrita en el Registro Mercantil"
    else:
        estado_soc = "Sin actos en el BORME del periodo indexado"
    consulta = ("omitida" if f["bdns_estado"] == "omitida" else
                f"error BDNS: {f['bdns_error']}" if f["bdns_estado"] == "error" else
                "pendiente" if f["bdns_estado"] == "pendiente" else "ok")
    motivo_prio = ("BONO_KIT_VIVO_URGENTE" if b.get("kit_urgente") else
                   "Historial IVACE/Cámara + teléfono" if prioridad == "A" else "")
    return {
        "cif": f["cif"], "razon_social": f["razon_social"] or "", "tipo": f["tipo"] or "", "estado": estado_soc,
        "constitucion": m.get("constitucion") or "", "antiguedad": antig, "direccion": f["direccion"] or "", "cp": f["cp"] or "",
        "municipio": f["municipio"] or "", "zona": zona, "zona_idx": zonas.indice(zona),
        "administradores": "; ".join(f"{a['nombre']} ({a['cargo']}, {a['desde']})" for a in m.get("administradores", [])),
        "kit_fecha": b.get("kit_fecha") or "", "kit_importe": b.get("kit_importe"), "kit_segmento": b.get("kit_segmento") or "",
        "kit_urgente": "SÍ" if b.get("kit_urgente") else "", "kitc_fecha": b.get("kitc_fecha") or "",
        "ivace": b.get("ivace") or 0, "camara": b.get("camara") or 0,
        "solvencia": "SÍ" if (b.get("recientes") or 0) > 0 else "", "n_ayudas": b.get("n_concesiones") or 0,
        "detalle_ayudas": b.get("detalle") or "", "web": web or "", "categoria": categoria or "",
        "tel_fuente": (tel_fuente or "confirmado") if telefono else "",
        "diputacion": b.get("diputacion") or 0, "importe_3a": b.get("importe_3a") or 0, "ultima_organismo": b.get("ultima_organismo"),
        "sector_est": sector_est, "sector_conf": f["sector_conf"] or "", "seg_est": seg_est,
        "emp_conf": emp, "telefono": telefono, "segmento": segmento, "palanca": "—" if descarte else R.palanca(segmento, sector),
        "prioridad": prioridad, "motivo_prioridad": motivo_prio, "descarte": descarte or "", "consulta": consulta,
        "fuentes": ", ".join(s for s, ok in (("BDNS", f["bdns_estado"] == "ok"), ("BORME", bool(m)), ("archivo de entrada", True)) if ok),
    }


def generar_excel(con, lote, hoy=None):
    hoy = hoy or date.today()
    zonas = Zonas(DATOS / "zonas.json")
    filas = con.execute("SELECT * FROM empresas WHERE lote = ? ORDER BY fila", (lote,)).fetchall()
    registros = [clasificar(f, zonas, hoy) for f in filas]
    desde, hasta, dias = borme.cobertura(con)
    contexto = {"lote": lote, "zonas": zonas.orden,
                "bdns_ok": sum(1 for f in filas if f["bdns_estado"] == "ok"), "bdns_error": sum(1 for f in filas if f["bdns_estado"] == "error"),
                "borme_cobertura": f"{dias} días publicados entre {desde} y {hasta}" if dias else "sin índice (estado y antigüedad sin dato)"}
    from .excel import generar
    ruta = DATOS / "salida" / f"{lote}_{datetime.now():%Y%m%d-%H%M}.xlsx"
    generar(ruta, registros, contexto)
    log.info("Excel generado: %s", ruta)
    return ruta


def actualizar(con, lote, ruta_excel):
    """Lee las columnas ✎ de la base completa y las guarda en el lote."""
    from openpyxl import load_workbook
    wb = load_workbook(ruta_excel, read_only=True, data_only=False)
    ws = wb["Base_Completa_Enriquecida"]
    filas = ws.iter_rows(values_only=True)
    # Columnas por su cabecera en ESTE archivo (puede venir de una versión anterior del worker).
    titulos = {str(t).strip(): i for i, t in enumerate(next(filas)) if t}
    faltan = [t for t in ("CIF", "Empleados (confirmado) ✎", "Sector (confirmado) ✎", "Teléfono ✎") if t not in titulos]
    if faltan:
        raise SystemExit(f"El Excel no tiene las columnas: {', '.join(faltan)}")
    n = 0
    for fila in filas:
        cif = fila[titulos["CIF"]]
        if not cif:
            continue
        emp = fila[titulos["Empleados (confirmado) ✎"]]
        try:
            emp = int(float(emp)) if emp not in (None, "") else None
        except (TypeError, ValueError):
            emp = None
        sector = fila[titulos["Sector (confirmado) ✎"]]
        sector = sector if sector in (R.INDUSTRIAL, R.SERVICIOS) else None
        tel = str(fila[titulos["Teléfono ✎"]] or "").strip() or None
        n += con.execute("UPDATE empresas SET emp_conf=?, sector_conf=?, telefono=? WHERE lote=? AND cif=?", (emp, sector, tel, lote, cif)).rowcount
    con.commit()
    wb.close()
    return n


def buscar_telefonos(con, lote, limite, cliente=None, hoy=None, clave=None, prov=None):
    """Teléfono, web y categoría en Google Maps para las válidas sin teléfono, por orden de encaje y zona."""
    from .fichas import evaluar
    hoy = hoy or date.today()
    if not clave:
        prov, clave = telefonos.proveedor(DATOS)
    prov = prov or "serpapi"
    if not clave:
        raise SystemExit(f"Falta SERPER_API_KEY o SERPAPI_KEY en {DATOS / '.env'}")
    usadas = telefonos.usadas_mes(con, prov, hoy=hoy)
    disponibles = max(0, min(limite, telefonos.TOPES[prov] - usadas))
    if not disponibles:
        log.info("Búsquedas de %s de este mes agotadas (%d). El mes que viene sigue por donde lo dejó.", prov, usadas)
        return 0
    zonas = Zonas(DATOS / "zonas.json")
    candidatas = []
    for f in con.execute("SELECT * FROM empresas WHERE lote=? AND telefono IS NULL AND tel_estado IS NULL", (lote,)):
        x = clasificar(f, zonas, hoy)
        e = evaluar(x, hoy) if x["consulta"] == "ok" else None
        if e:
            candidatas.append(({"Alto": 0, "Medio": 1, "Base": 2}[e["encaje"]], x["zona_idx"], f["fila"], f))
    candidatas.sort(key=lambda c: c[:3])
    cliente = cliente or Cliente(pausa=(1.0, 2.0))
    log.info("Teléfonos (%s): %d empresas sin teléfono que encajan; se buscan %d", prov, len(candidatas), min(disponibles, len(candidatas)))
    hechas = 0
    for _, _, _, f in candidatas[:disponibles]:
        empresa = {"razon_social": f["razon_social"], "municipio": f["municipio"], "cp": f["cp"]}
        try:
            r = telefonos.buscar(cliente, clave, empresa, prov)
        except ErrorConsulta as e:
            if "401" in str(e) or "403" in str(e):
                raise SystemExit(f"La clave de {prov} no es válida o no le quedan búsquedas.") from None
            log.warning("  %s: búsqueda fallida (%s); se reintentará", f["cif"], e)
            continue
        telefonos.contar(con, prov, hoy=hoy)
        hechas += 1
        if r:
            con.execute("UPDATE empresas SET telefono=COALESCE(telefono, ?), tel_fuente=?, web=?, categoria=?, tel_estado=? WHERE lote=? AND cif=?",
                        (r["telefono"], telefonos.FUENTES[prov] if r["telefono"] else None, r["web"], r["categoria"],
                         "encontrado" if r["telefono"] else "sin_telefono", lote, f["cif"]))
        else:
            con.execute("UPDATE empresas SET tel_estado='no_encontrado' WHERE lote=? AND cif=?", (lote, f["cif"]))
        con.commit()
        log.info("[%d] %s: %s", hechas, f["cif"], "teléfono encontrado" if r and r["telefono"] else "ficha sin teléfono" if r else "no localizada")
    return hechas


def excluir(con, cif, motivo):
    cif = cifmod.normalizar(cif)
    con.execute("INSERT OR REPLACE INTO exclusiones VALUES (?, ?, ?)", (cif, motivo, date.today().isoformat()))
    # Se borran los datos enriquecidos de esa empresa en todos los lotes.
    con.execute("UPDATE empresas SET motivo_descarte='Excluida a petición (oposición al tratamiento)', borme=NULL, borme_estado='omitida', "
                "telefono=NULL WHERE cif=?", (cif,))
    con.commit()


def ultimo_lote(con):
    r = con.execute("SELECT nombre FROM lotes ORDER BY creado DESC LIMIT 1").fetchone()
    if not r:
        raise SystemExit("No hay lotes. Empieza con: worker procesar --input archivo.xlsx")
    return r[0]


# --------------------------------------------------------------------------- CLI

def main(argv=None):
    p = argparse.ArgumentParser(prog="worker", description="Cualificación B2B de empresas de Alicante (BDNS + BORME).")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("procesar")
    a.add_argument("--input", required=True)
    a.add_argument("--lote")
    a.add_argument("--limit", type=int)
    a.add_argument("--resume", action="store_true", help="continúa el lote (es lo que hace por defecto si ya existe)")
    a.add_argument("--reiniciar", action="store_true", help="vuelve a empezar el lote desde cero")
    a.add_argument("--reconsultar", action="store_true", help="vuelve a consultar la BDNS de las ya hechas (p. ej. tras una mejora)")
    a = sub.add_parser("borme-indexar")
    a.add_argument("--desde", required=True)
    a.add_argument("--hasta")
    a = sub.add_parser("borme-probar")
    a.add_argument("--fecha", required=True)
    a = sub.add_parser("excel")
    a.add_argument("--lote")
    a = sub.add_parser("actualizar")
    a.add_argument("--excel", required=True)
    a.add_argument("--lote")
    a = sub.add_parser("telefonos")
    a.add_argument("--lote")
    a.add_argument("--limite", type=int, default=700)
    a = sub.add_parser("excluir")
    a.add_argument("cif")
    a.add_argument("--motivo", default="Oposición al tratamiento")
    a = sub.add_parser("estado")
    a.add_argument("--lote")
    args = p.parse_args(argv)
    con = preparar()

    if args.cmd == "procesar":
        lote = args.lote or Path(args.input).stem
        nuevo = cargar_lote(con, lote, args.input, reiniciar=args.reiniciar)
        log.info("%s lote %s", "Nuevo" if nuevo else "Reanudando", lote)
        if args.reconsultar:
            con.execute("UPDATE empresas SET bdns_estado='pendiente' WHERE lote=? AND bdns_estado='ok'", (lote,))
            con.commit()
        try:
            procesar(con, lote, args.limit)
        except KeyboardInterrupt:
            log.warning("Interrumpido: lo hecho está guardado. Reanuda con: worker procesar --input %s --lote %s --resume", args.input, lote)
            return 130
        generar_excel(con, lote)
    elif args.cmd == "borme-indexar":
        cliente = Cliente(pausa=(1.0, 2.0))
        desde = date.fromisoformat(args.desde)
        hasta = date.fromisoformat(args.hasta) if args.hasta else date.today() - timedelta(days=1)
        dias = list(borme.dias_pendientes(con, desde, hasta))
        log.info("BORME Alicante: %d días por descargar (%s → %s)", len(dias), desde, hasta)
        for n, d in enumerate(dias, start=1):
            try:
                r = borme.indexar_dia(con, cliente, d)
                log.info("[%d/%d] %s: %s", n, len(dias), d, "sin publicación" if r is None else f"{r} inscripciones")
            except ErrorConsulta as e:
                log.warning("[%d/%d] %s: error (%s); se reintentará en la próxima ejecución", n, len(dias), d, e)
            except KeyboardInterrupt:
                log.warning("Interrumpido: los días descargados quedan guardados; vuelve a lanzar el mismo comando para seguir.")
                return 130
        log.info("Cobertura del índice: %s", borme.cobertura(con))
    elif args.cmd == "borme-probar":
        d = date.fromisoformat(args.fecha)
        r = borme.indexar_dia(con, Cliente(pausa=(0.5, 1.0)), d)
        if r is None:
            print(f"{d}: no hubo BORME ese día")
        else:
            print(f"{d}: {r} inscripciones de Alicante. Primeras:")
            for f in con.execute("SELECT nombre, texto FROM borme_actos WHERE fecha = ? LIMIT 3", (d.isoformat(),)):
                print(f"  - {f['nombre']} | {f['texto'][:140]}…")
            primera = con.execute("SELECT nombre FROM borme_actos WHERE fecha = ? LIMIT 1", (d.isoformat(),)).fetchone()
            if primera:
                print("  Ficha de la primera:", json.dumps(borme.ficha(con, primera[0]), ensure_ascii=False)[:400])
    elif args.cmd == "excel":
        generar_excel(con, args.lote or ultimo_lote(con))
    elif args.cmd == "actualizar":
        lote = args.lote or ultimo_lote(con)
        log.info("Datos confirmados leídos de %d filas", actualizar(con, lote, args.excel))
        generar_excel(con, lote)
    elif args.cmd == "telefonos":
        lote = args.lote or ultimo_lote(con)
        buscar_telefonos(con, lote, args.limite)
        generar_excel(con, lote)
    elif args.cmd == "excluir":
        excluir(con, args.cif, args.motivo)
        log.info("%s excluida: no aparecerá en ningún lote. Regenera el Excel con: worker excel", cifmod.normalizar(args.cif))
    elif args.cmd == "estado":
        lote = args.lote or ultimo_lote(con)
        r = con.execute("SELECT COUNT(*), SUM(bdns_estado='ok'), SUM(bdns_estado='pendiente'), SUM(bdns_estado='error'), SUM(bdns_estado='omitida'), "
                        "SUM(borme_estado='encontrada') FROM empresas WHERE lote=?", (lote,)).fetchone()
        print(f"Lote {lote}: {r[0]} empresas · BDNS ok {r[1]}, pendientes {r[2]}, errores {r[3]}, omitidas {r[4]} · en BORME {r[5]}")
        print("Índice BORME (desde, hasta, días):", borme.cobertura(con))
    return 0


if __name__ == "__main__":
    sys.exit(main())
