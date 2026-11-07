"""Cerrojo por móvil entre los procesos del HP (API, captura y trabajador de sondas).

- Uso normal (compartido): cada petición de un trabajador por el 3proxy de un móvil se hace dentro de
  en_uso(mid). Varios trabajadores pueden usar el mismo móvil a la vez.
- Rotación (exclusivo): rotacion(mid) marca el móvil como "rotando" (los trabajadores dejan de entrar),
  espera a que terminen las peticiones en vuelo y solo entonces deja rotar.

Los cerrojos son flock() sobre archivos en PROXI_GRANJA_LOCKS: el sistema los libera solo si un proceso
muere. La marca de rotación caduca a los MARCA_MAX_EDAD segundos por si el proceso que rotaba se cayó.
"""
import contextlib
import fcntl
import os
import time

LOCK_DIR = os.environ.get("PROXI_GRANJA_LOCKS", "/var/lib/proxi-granja/locks")
MARCA_MAX_EDAD = 120


class Rotando(Exception):
    """El móvil está rotando (o a punto): no se usa ahora."""


class Ocupado(Exception):
    """No se pudo rotar: hay peticiones en vuelo que no terminaron a tiempo, u otra rotación en curso."""


def _ruta(mid, ext):
    if not mid.replace("_", "").isalnum():
        raise ValueError(f"id de móvil no válido: {mid!r}")
    os.makedirs(LOCK_DIR, exist_ok=True)
    return os.path.join(LOCK_DIR, f"{mid}.{ext}")


def rotando(mid):
    try:
        return time.time() - os.path.getmtime(_ruta(mid, "rotating")) < MARCA_MAX_EDAD
    except FileNotFoundError:
        return False


@contextlib.contextmanager
def en_uso(mid):
    """Cerrojo compartido para una petición por el móvil. Lanza Rotando si está rotando."""
    if rotando(mid):
        raise Rotando(mid)
    with open(_ruta(mid, "lock"), "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Rotando(mid) from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


@contextlib.contextmanager
def rotacion(mid, espera=30.0, paso=0.25):
    """Cerrojo exclusivo para rotar. Lanza Ocupado si no lo consigue en `espera` segundos."""
    marca = _ruta(mid, "rotating")
    with open(_ruta(mid, "lock"), "a") as f:
        # Primero el turno de rotación (otra rotación del mismo móvil → Ocupado), luego la marca.
        guardia = open(_ruta(mid, "rotlock"), "a")
        try:
            try:
                fcntl.flock(guardia, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Ocupado(f"{mid}: rotación ya en curso") from None
            with open(marca, "w"):
                pass
            try:
                limite = time.monotonic() + espera
                while True:
                    try:
                        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= limite:
                            raise Ocupado(f"{mid}: peticiones en vuelo") from None
                        time.sleep(paso)
                try:
                    yield
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.remove(marca)
        finally:
            guardia.close()
