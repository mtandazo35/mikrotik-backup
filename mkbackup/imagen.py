"""Ajusta la imagen de fondo del login al tamano que hace falta.

Antes habia que subir una imagen ya recortada y comprimida, y la pista del
formulario lo decia: "por debajo de 500 KB no se nota, y 1920 px de ancho
sobran". Eso es pedirle a quien administra una red que abra un editor de fotos
para poner un logo. Ahora se sube lo que se tenga -la foto del movil, el JPEG
de 12 MB que mando el cliente- y el sistema la deja como debe.

Por que importa el tamano y no es una mania: esta imagen se sirve en la
pantalla de ENTRADA, o sea antes de que nadie haya escrito su clave, y se
descarga entera antes de que aparezca el formulario. Una foto de 12 MB son
varios segundos mirando una pantalla en blanco cada vez que alguien entra al
panel, y desde el movil de un tecnico en la calle, bastantes mas.

Ademas se sirve SIN pedir login (es el fondo de la propia pantalla de entrada),
asi que la ve cualquiera que llegue al panel. Reencodear la imagen tiene aqui
un segundo efecto que no es accesorio: se van los metadatos. Una foto sacada
con el movil lleva EXIF, y el EXIF lleva coordenadas GPS. Publicar sin querer
donde esta la oficina, en una pagina que no pide contrasena, es justo el tipo
de fuga que nadie audita porque "solo es el fondo".

Pillow es OPCIONAL, igual que openpyxl para los .xlsx. Sin ella todo sigue
funcionando como antes: se acepta la imagen tal cual y se avisa. No se convierte
en obligatoria porque este proyecto se instala en servidores de clientes donde
a veces no hay ni salida a internet, y quedarse sin panel por no poder
redimensionar un fondo seria un mal negocio.
"""

from __future__ import annotations

import io
import logging

log = logging.getLogger("mkbackup.imagen")

# Ancho al que se lleva la imagen. 1920 es el ancho de una pantalla grande; por
# encima de eso el navegador la reduce igual y solo se ha pagado la descarga.
ANCHO_MAXIMO = 1920

# A lo que se apunta en peso. No es un limite duro: si una imagen no baja de
# aqui ni bajando la calidad, se guarda lo mejor que se haya conseguido, porque
# un fondo grande es mejor que ningun fondo.
PESO_OBJETIVO = 400 * 1024

# Calidades que se prueban, de mejor a peor. Por debajo de 55 el JPEG empieza a
# ensuciar los degradados de forma visible, y un fondo con bandas se ve peor que
# uno un poco mas pesado.
CALIDADES = (85, 78, 70, 62, 55)

# Tope de pixeles que se acepta DESCOMPRIMIR. Un PNG de 4 KB puede declarar
# 50000x50000 y ocupar 10 GB al abrirlo: es una bomba de descompresion, y sin
# este tope tumba el panel con un archivo que pasa cualquier control de tamano.
# 80 megapixeles dan de sobra para cualquier camara real (una de 60 MP no
# llega).
MAXIMO_PIXELES = 80_000_000


class ErrorImagen(Exception):
    """La imagen no se puede procesar. Lleva un texto para ensenar tal cual."""


def disponible() -> bool:
    """Si se puede ajustar el tamano. False = Pillow no esta instalada."""
    try:
        import PIL  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _abrir(datos: bytes):
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = MAXIMO_PIXELES
    try:
        img = Image.open(io.BytesIO(datos))
        # load() es lo que descomprime de verdad; hasta aqui Pillow solo ha
        # leido la cabecera, asi que una bomba no salta en open() sino aqui.
        img.load()
    except Exception as exc:  # noqa: BLE001
        # Se traga cualquier cosa: los decodificadores de imagen son codigo C
        # comiendo bytes de un desconocido y lanzan de todo, no solo lo que
        # esta documentado.
        if type(exc).__name__ == "DecompressionBombError":
            raise ErrorImagen(
                "Esa imagen declara un tamano descomunal al descomprimirla "
                f"(el tope son {MAXIMO_PIXELES // 1_000_000} megapixeles). "
                "Si es una foto normal, vuelve a exportarla."
            ) from exc
        raise ErrorImagen(
            "No se pudo leer la imagen: puede estar a medias o danada."
        ) from exc

    # La foto de un movil viene con la orientacion en el EXIF y no en los
    # pixeles: sin esto se guarda tumbada, que es como se veria en el login.
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:  # noqa: BLE001
        pass
    return img


def ajustar(datos: bytes) -> tuple[bytes, str, str]:
    """Deja la imagen lista para el login.

    Devuelve (bytes, extension, nota). `nota` es una frase para ensenar en el
    panel contando que se hizo, o "" si no se toco nada.

    Si Pillow no esta, devuelve la imagen tal cual y lo dice en la nota: quien
    la sube tiene que enterarse de que se guardo su archivo original, no una
    version ajustada.
    """
    if not disponible():
        return datos, "", (
            "Se guardo tal cual: falta la biblioteca Pillow para poder "
            "ajustarla. Instalala con `pip install Pillow` en el entorno del "
            "servicio si quieres que las imagenes se ajusten solas."
        )

    from PIL import Image

    original = len(datos)
    img = _abrir(datos)
    ancho_ini, alto_ini = img.size

    # Alfa: un fondo no lo necesita (va detras de una tarjeta opaca) y ademas
    # obliga a PNG, que para una foto pesa varias veces mas. Se aplana sobre
    # negro, que es lo que menos canta en los dos temas del panel.
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        fondo = Image.new("RGB", img.size, (0, 0, 0))
        fondo.paste(img, mask=img.split()[-1])
        img = fondo
    elif img.mode != "RGB":
        img = img.convert("RGB")

    if img.width > ANCHO_MAXIMO:
        alto = round(img.height * ANCHO_MAXIMO / img.width)
        img = img.resize((ANCHO_MAXIMO, alto), Image.LANCZOS)

    # Se guarda SIN exif ni icc: es lo que se lleva por delante los metadatos
    # (incluidas las coordenadas GPS de una foto de movil), y esta imagen se
    # sirve sin pedir contrasena.
    mejor = None
    for calidad in CALIDADES:
        buz = io.BytesIO()
        img.save(buz, format="JPEG", quality=calidad, optimize=True,
                 progressive=True)
        mejor = buz.getvalue()
        if len(mejor) <= PESO_OBJETIVO:
            break

    # Si ni con la peor calidad baja del objetivo, se estrecha la imagen. Antes
    # de repetir esto para siempre: dos vueltas bastan para cualquier foto real,
    # y a partir de ahi se guarda lo que haya salido.
    intentos = 0
    while len(mejor) > PESO_OBJETIVO and img.width > 900 and intentos < 2:
        intentos += 1
        nuevo = int(img.width * 0.75)
        img = img.resize((nuevo, round(img.height * nuevo / img.width)),
                         Image.LANCZOS)
        buz = io.BytesIO()
        img.save(buz, format="JPEG", quality=CALIDADES[-1], optimize=True,
                 progressive=True)
        mejor = buz.getvalue()

    partes = []
    if (ancho_ini, alto_ini) != img.size:
        partes.append(f"de {ancho_ini}x{alto_ini} a {img.width}x{img.height} px")
    if original != len(mejor):
        partes.append(f"de {_kb(original)} a {_kb(len(mejor))}")
    nota = ("Ajustada " + " y ".join(partes) + ".") if partes else ""
    if nota:
        nota += " Se quitaron los metadatos (una foto de movil lleva la ubicacion)."

    log.info(
        "Fondo ajustado: %dx%d %s -> %dx%d %s",
        ancho_ini, alto_ini, _kb(original), img.width, img.height, _kb(len(mejor)),
    )
    return mejor, ".jpg", nota


def _kb(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{max(1, n // 1024)} KB"
