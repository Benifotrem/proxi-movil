"""Controladores de los dispositivos de la granja (patrón Driver).

  AndroidDriver   ADB: rotación por modo avión o datos móviles, SMS con `content query`.
  IosDriver       iPhone con jailbreak por SSH a través de iproxy (libimobiledevice) en 127.0.0.1:<ssh_port>:
                  rotación reiniciando CommCenter, SMS desde /private/var/mobile/Library/SMS/sms.db.

modems.conf: columnas opcionales 6 (driver: adb | ssh_jailbreak, por defecto adb) y 7 (ssh_port).
"""
import re
import subprocess
import time
from abc import ABC, abstractmethod

ADB_TIMEOUT = 15
SSH_TIMEOUT = 15
ROTATE_PAUSE = 3
IOS_SSH_KEY = "/etc/proxi-granja/ios_ssh_key"
IOS_KNOWN_HOSTS = "/var/lib/proxi-granja/ios_known_hosts"
APPLE_EPOCH_MS = 978307200000  # 2001-01-01 en milisegundos Unix


class DriverError(Exception):
    pass


class DeviceDriver(ABC):
    """Operaciones que la plataforma necesita de un dispositivo, sea cual sea su sistema."""

    os_type = ""

    @abstractmethod
    def rotate_ip(self):
        """Fuerza una nueva conexión de datos (IP pública nueva). Bloquea hasta lanzar la reconexión."""

    @abstractmethod
    def read_latest_sms(self, limit=10):
        """Últimos SMS recibidos, del más nuevo al más viejo: [{id, sender, body, date_ms}]."""

    @abstractmethod
    def reboot_radio(self):
        """Reinicio completo de la radio celular (más lento que rotate_ip)."""

    def read_incoming(self):
        """Mensajes entrantes visibles ahora: [{key, sender, body, when_ms|None}]. Por defecto, la bandeja de SMS."""
        return [{"key": f"sms:{m['id']}", "sender": m["sender"], "body": m["body"], "when_ms": m["date_ms"]}
                for m in self.read_latest_sms(20)]


class AndroidDriver(DeviceDriver):
    os_type = "android"

    def __init__(self, serial, rotacion="airplane", runner=subprocess.run, dormir=time.sleep):
        self.serial, self.rotacion, self.run, self.dormir = serial, rotacion, runner, dormir

    def _sh(self, *args):
        out = self.run(["adb", "-s", self.serial, "shell", *args], capture_output=True, text=True, timeout=ADB_TIMEOUT)
        if out.returncode != 0:
            lineas = (out.stderr or out.stdout).strip().splitlines()
            raise DriverError(f"adb {' '.join(args)}: {lineas[0] if lineas else f'código {out.returncode}'}")
        return out.stdout

    def _avion(self, pausa):
        # Que el modo avión apague solo la radio celular. Algunas ROM (MIUI) no dejan cambiar este
        # ajuste por ADB; el anclaje USB no suele estar en la lista por defecto.
        try:
            self._sh("settings", "put", "global", "airplane_mode_radios", "cell")
        except DriverError:
            pass
        self._sh("cmd", "connectivity", "airplane-mode", "enable")
        self.dormir(pausa)
        self._sh("cmd", "connectivity", "airplane-mode", "disable")

    def rotate_ip(self):
        if self.rotacion == "airplane":
            self._avion(ROTATE_PAUSE)
        else:
            self._sh("svc", "data", "disable")
            self.dormir(ROTATE_PAUSE)
            self._sh("svc", "data", "enable")

    def reboot_radio(self):
        self._avion(ROTATE_PAUSE * 3)

    # Apps de mensajes cuyas notificaciones se leen (SMS y RCS). Android 10+ no deja leer la bandeja de SMS
    # por ADB, pero sí las notificaciones (dumpsys notification --noredact), también en Android 16.
    APPS_MENSAJES = ("com.google.android.apps.messaging", "com.samsung.android.messaging", "com.android.mms",
                     "com.android.messaging")

    def read_incoming(self):
        return self.parse_notifications(self._sh("dumpsys", "notification", "--noredact"), self.APPS_MENSAJES)

    REGISTRO = re.compile(r"^\s*NotificationRecord\(.*?pkg=([\w.]+)")
    EXTRA = re.compile(r"^\s*(android\.(?:title|text|bigText))=String \((.*)$")

    @classmethod
    def parse_notifications(cls, salida, paquetes):
        """Notificaciones de las apps de mensajes: remitente (título), texto completo y hora si la hay."""
        avisos, actual, pendiente = [], None, None
        for linea in salida.splitlines():
            m = cls.REGISTRO.match(linea)
            if m:
                actual = {"pkg": m.group(1), "key": None, "when_ms": None} if m.group(1) in paquetes else None
                if actual is not None:
                    avisos.append(actual)
                pendiente = None
                continue
            if actual is None:
                continue
            if pendiente:  # valor de varias líneas
                campo, texto = pendiente
                if linea.rstrip().endswith(")"):
                    actual[campo] = texto + "\n" + linea.strip()[:-1]
                    pendiente = None
                else:
                    pendiente = (campo, texto + "\n" + linea.strip())
                continue
            e = cls.EXTRA.match(linea)
            if e:
                valor = e.group(2).rstrip()
                if valor.endswith(")"):
                    actual[e.group(1)] = valor[:-1].strip()
                else:
                    pendiente = (e.group(1), valor)
                continue
            k = re.match(r"^\s*key=(\S+)", linea)
            if k and actual["key"] is None:
                actual["key"] = k.group(1)
            w = re.search(r"\bwhen=(\d{12,})", linea)
            if w and actual["when_ms"] is None:
                actual["when_ms"] = int(w.group(1))
        res = []
        for a in avisos:
            cuerpo = a.get("android.bigText") or a.get("android.text") or ""
            remitente = a.get("android.title") or ""
            if remitente and cuerpo and a["key"]:
                res.append({"key": a["key"], "sender": remitente, "body": cuerpo, "when_ms": a["when_ms"]})
        return res

    FILA = re.compile(r"^Row: \d+ _id=(\d+), address=(.*?), date=(\d+), body=(.*)$")

    def read_latest_sms(self, limit=10):
        # En Android 10+ el usuario shell puede no tener permiso de lectura de SMS: se informa el error.
        salida = self._sh("content", "query", "--uri", "content://sms/inbox",
                          "--projection", "_id:address:date:body", "--sort", "'date DESC'")
        if "Error" in salida[:200] or "Permission Denial" in salida:
            raise DriverError(f"content query: {salida.strip().splitlines()[0][:200]}")
        return self.parse_content_query(salida)[:limit]

    @classmethod
    def parse_content_query(cls, salida):
        mensajes = []
        for linea in salida.splitlines():
            m = cls.FILA.match(linea)
            if m:
                mensajes.append({"id": m.group(1), "sender": m.group(2), "body": m.group(4), "date_ms": int(m.group(3))})
            elif mensajes and linea and not linea.startswith("No result"):
                mensajes[-1]["body"] += "\n" + linea  # cuerpo con saltos de línea
        return mensajes


class IosDriver(DeviceDriver):
    os_type = "ios"
    SEP = "\x1f"

    def __init__(self, udid, ssh_port, usuario="root", clave=IOS_SSH_KEY, runner=subprocess.run):
        if not ssh_port:
            raise DriverError(f"iPhone {udid}: falta ssh_port en modems.conf")
        self.udid, self.port, self.usuario, self.clave, self.run = udid, int(ssh_port), usuario, clave, runner

    def _ssh(self, comando):
        cmd = ["ssh", "-p", str(self.port), "-i", self.clave, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
               "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={IOS_KNOWN_HOSTS}",
               f"{self.usuario}@127.0.0.1", comando]
        out = self.run(cmd, capture_output=True, text=True, timeout=SSH_TIMEOUT)
        if out.returncode not in (0, 1) or (out.returncode == 1 and out.stderr.strip()):
            raise DriverError(f"ssh {self.udid}: {(out.stderr or out.stdout).strip()[:200] or f'código {out.returncode}'}")
        return out.stdout

    def rotate_ip(self):
        # CommCenter gestiona la radio celular; launchd lo relanza y la conexión de datos se renegocia.
        self._ssh("killall -9 CommCenter")

    def reboot_radio(self):
        self._ssh("killall -9 CommCenter; sleep 5; killall -9 CommCenter")

    def read_latest_sms(self, limit=10):
        limit = max(1, min(int(limit), 100))
        sql = ("SELECT m.ROWID, COALESCE(h.id, ''), m.date, replace(replace(COALESCE(m.text, ''), char(10), ' '), char(31), ' ') "
               "FROM message m LEFT JOIN handle h ON m.handle_id = h.ROWID WHERE m.is_from_me = 0 "
               f"ORDER BY m.date DESC LIMIT {limit};")
        salida = self._ssh(f"sqlite3 -readonly -separator \"$(printf '\\037')\" /private/var/mobile/Library/SMS/sms.db \"{sql}\"")
        return self.parse_sqlite(salida)

    @classmethod
    def parse_sqlite(cls, salida):
        mensajes = []
        for linea in salida.splitlines():
            campos = linea.split(cls.SEP, 3)
            if len(campos) != 4 or not campos[0].isdigit():
                continue
            fecha = int(campos[2] or 0)
            # iOS 11+: nanosegundos desde 2001; antes, segundos.
            date_ms = APPLE_EPOCH_MS + (fecha // 1_000_000 if fecha > 10**11 else fecha * 1000)
            mensajes.append({"id": campos[0], "sender": campos[1], "body": campos[3], "date_ms": date_ms})
        return mensajes


def driver_para(modem, **kw):
    """Driver de un móvil del inventario (dict de api.inventario())."""
    tipo = modem.get("driver", "adb")
    if tipo == "adb":
        return AndroidDriver(modem["serial"], modem.get("rotation", "airplane"), **kw)
    if tipo == "ssh_jailbreak":
        return IosDriver(modem["serial"], modem.get("ssh_port"), **kw)
    raise DriverError(f"driver desconocido: {tipo}")
