"""Notificaciones por Telegram.

Usa urllib de la libreria estandar a proposito: una dependencia menos que
mantener en un servicio que corre desatendido.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .config import ConfigTelegram

TIMEOUT = 15


class Notificador:
    def __init__(self, cfg: ConfigTelegram):
        self.cfg = cfg

    @property
    def activo(self) -> bool:
        return bool(
            self.cfg.token
            and self.cfg.chat_id
            and self.cfg.modo != "ninguno"
            and not self.cfg.token.startswith("TU_")
        )

    def enviar(self, texto: str) -> bool:
        """Nunca lanza: que falle Telegram no debe tumbar el respaldo."""
        if not self.activo:
            return False

        url = f"https://api.telegram.org/bot{self.cfg.token}/sendMessage"
        datos = urllib.parse.urlencode(
            {"chat_id": self.cfg.chat_id, "text": texto, "parse_mode": "Markdown"}
        ).encode()

        try:
            with urllib.request.urlopen(url, data=datos, timeout=TIMEOUT) as resp:
                cuerpo = json.loads(resp.read().decode("utf-8", errors="replace"))
                return bool(cuerpo.get("ok"))
        except (urllib.error.URLError, OSError, ValueError):
            return False

    # --- Mensajes concretos -------------------------------------------------

    def fallo_equipo(self, nombre: str, ip: str, tipo: str, motivo: str) -> None:
        """Un equipo concreto. Solo en modo detalle: con 300 equipos, un
        troncal caido generaria decenas de mensajes."""
        if self.cfg.modo != "detalle":
            return
        self.enviar(
            f"❌ *Fallo de respaldo*\n"
            f"*Equipo:* {nombre}\n*IP:* {ip}\n*Motivo:* {tipo} — {motivo}"
        )

    def cambio_equipo(self, nombre: str, commit: str) -> None:
        if self.cfg.modo != "detalle":
            return
        self.enviar(f"✏️ *Cambio detectado*\n*Equipo:* {nombre}\n*Commit:* `{commit}`")

    def resumen(
        self,
        total: int,
        ok: int,
        cambios: int,
        fallidos: list[tuple[str, str]],
        duracion: float,
    ) -> None:
        """Un mensaje por ciclo. Es el modo por defecto."""
        if self.cfg.modo == "ninguno":
            return

        if not fallidos:
            # Sin fallos y sin cambios no hay nada que contar: avisar de esto
            # cada ciclo es la forma mas rapida de que dejen de leerse.
            if cambios == 0:
                return
            self.enviar(
                f"✅ *Respaldo completo*\n"
                f"Equipos: {ok}/{total}   Cambios: {cambios}\n"
                f"Duracion: {duracion:.0f}s"
            )
            return

        lista = "\n".join(f"• {n}: {m}" for n, m in fallidos[:15])
        if len(fallidos) > 15:
            lista += f"\n• ... y {len(fallidos) - 15} mas"

        self.enviar(
            f"⚠️ *Respaldo con fallos*\n"
            f"*OK:* {ok}/{total}   *Fallidos:* {len(fallidos)}   *Cambios:* {cambios}\n"
            f"*Duracion:* {duracion:.0f}s\n\n{lista}"
        )
