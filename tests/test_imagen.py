"""Prueba del ajuste de la imagen de fondo del login.

Lo que se comprueba aqui:

  - que una foto grande baja de verdad a lo que hace falta. Esta imagen se
    descarga ENTERA antes de que aparezca el formulario de entrada, asi que lo
    que pese se paga en cada login, y desde el movil de un tecnico en la calle
    se paga caro.
  - que se van los metadatos. La imagen se sirve SIN pedir clave (es el fondo de
    la propia pantalla de entrada), y una foto de movil lleva EXIF con
    coordenadas GPS. Publicar donde esta la oficina en una pagina sin
    contrasena es el tipo de fuga que nadie audita porque "solo es el fondo".
  - que una bomba de descompresion no tumba el panel. Un PNG de unos pocos KB
    puede declarar 50000x50000 y ocupar gigas al abrirlo: pasa cualquier control
    de tamano de archivo y revienta el servidor al decodificarlo.
  - que sin Pillow todo sigue funcionando, porque es una dependencia OPCIONAL y
    hay servidores de clientes donde no se puede instalar.

Ejecutar:  python -m tests.test_imagen
"""

import io
import zlib

from mkbackup import imagen as im

FALLOS = []


def comprobar(descripcion: str, condicion: bool) -> None:
    print(f"[{'OK  ' if condicion else 'FALLA'}] {descripcion}")
    if not condicion:
        FALLOS.append(descripcion)


def _foto(ancho, alto, con_exif=False, modo="RGB"):
    """Una imagen con ruido, para que comprima como una foto y no como un liso."""
    from PIL import Image

    # Ruido determinista: un color plano se comprime a nada y no probaria nada
    # de lo que hay que probar.
    img = Image.new(modo, (ancho, alto))
    pix = img.load()
    semilla = 12345
    for y in range(0, alto, 3):
        for x in range(0, ancho, 3):
            semilla = (semilla * 1103515245 + 12345) & 0x7FFFFFFF
            c = (semilla % 255, (semilla >> 8) % 255, (semilla >> 16) % 255)
            pix[x, y] = c if modo == "RGB" else c + (255,)

    buz = io.BytesIO()
    if con_exif:
        from PIL import Image as I

        exif = I.Exif()
        # 0x8825 = GPSInfo. Es el dato que no puede sobrevivir al ajuste.
        exif[0x8825] = {1: "N", 2: (4.0, 0.0, 0.0)}
        exif[0x010E] = "una descripcion que tampoco tiene que quedar"
        img.save(buz, format="JPEG", quality=95, exif=exif)
    else:
        img.save(buz, format="JPEG", quality=95)
    return buz.getvalue()


def _bomba_png(lado=30000):
    """PNG diminuto que declara una imagen enorme. Bomba de descompresion."""
    def trozo(tipo, datos):
        return (len(datos).to_bytes(4, "big") + tipo + datos
                + zlib.crc32(tipo + datos).to_bytes(4, "big"))

    cabecera = (lado.to_bytes(4, "big") + lado.to_bytes(4, "big")
                + bytes([8, 0, 0, 0, 0]))          # 8 bits, escala de grises
    # Una fila por linea, toda a cero: comprime a practicamente nada.
    crudo = b"".join(b"\x00" + b"\x00" * lado for _ in range(lado))
    return (b"\x89PNG\r\n\x1a\n" + trozo(b"IHDR", cabecera)
            + trozo(b"IDAT", zlib.compress(crudo, 9)) + trozo(b"IEND", b""))


def main() -> None:
    comprobar("Pillow esta instalada en esta maquina (si no, casi nada se prueba)",
              im.disponible())

    if not im.disponible():
        # Sin Pillow lo unico que se puede exigir es que NO rompa nada.
        datos = b"\xff\xd8\xff\xe0 lo que sea"
        salida, ext, nota = im.ajustar(datos)
        comprobar("sin Pillow se devuelve la imagen tal cual", salida == datos)
        comprobar("sin Pillow no se cambia la extension", ext == "")
        comprobar("sin Pillow se avisa de que no se ajusto", "Pillow" in nota)
        print("\nTodas las pruebas pasaron (sin Pillow: comprobacion reducida).")
        return

    from PIL import Image

    # --- 1. Una foto grande baja a lo que hace falta ------------------------
    grande = _foto(4000, 3000)
    salida, ext, nota = im.ajustar(grande)
    img = Image.open(io.BytesIO(salida))
    comprobar(f"una foto de 4000 px se reduce a {im.ANCHO_MAXIMO} "
              f"(quedo en {img.width})", img.width == im.ANCHO_MAXIMO)
    comprobar(f"se mantiene la proporcion (quedo {img.width}x{img.height})",
              abs(img.width / img.height - 4000 / 3000) < 0.01)
    comprobar(f"pesa menos que el original ({len(grande) // 1024} KB -> "
              f"{len(salida) // 1024} KB)", len(salida) < len(grande))
    comprobar(f"y baja del objetivo de {im.PESO_OBJETIVO // 1024} KB "
              f"({len(salida) // 1024} KB)", len(salida) <= im.PESO_OBJETIVO)
    comprobar("se guarda como .jpg", ext == ".jpg")
    comprobar("se cuenta lo que se hizo", "Ajustada" in nota)

    # --- 2. Una imagen que ya esta bien no se estropea ----------------------
    justa = _foto(1200, 800)
    salida2, _, nota2 = im.ajustar(justa)
    img2 = Image.open(io.BytesIO(salida2))
    comprobar(f"una imagen de 1200 px NO se agranda (quedo en {img2.width})",
              img2.width == 1200)

    # --- 3. Los metadatos se van ------------------------------------------
    # Esto es lo importante de verdad: la imagen se sirve sin pedir clave.
    con_gps = _foto(2400, 1600, con_exif=True)
    antes = Image.open(io.BytesIO(con_gps))
    comprobar("la foto de prueba lleva EXIF con GPS, como la de un movil",
              bool(antes.getexif()) and 0x8825 in antes.getexif())

    limpia, _, nota3 = im.ajustar(con_gps)
    despues = Image.open(io.BytesIO(limpia))
    comprobar("tras ajustarla no queda EXIF", not dict(despues.getexif()))
    comprobar("ni rastro del GPS", 0x8825 not in despues.getexif())
    comprobar("y no queda la descripcion tampoco",
              b"una descripcion que tampoco" not in limpia)
    comprobar("se avisa de que se quitaron los metadatos",
              "metadatos" in nota3)

    # --- 4. Transparencia: se aplana, no se rompe --------------------------
    from PIL import Image as I

    buz = io.BytesIO()
    I.new("RGBA", (2200, 1400), (200, 30, 30, 128)).save(buz, format="PNG")
    con_alfa, ext4, _ = im.ajustar(buz.getvalue())
    img4 = Image.open(io.BytesIO(con_alfa))
    comprobar("un PNG con transparencia se acepta", img4.width == im.ANCHO_MAXIMO)
    comprobar("y sale sin canal alfa, que un fondo no necesita",
              img4.mode == "RGB")
    comprobar("y como .jpg, que para una foto pesa mucho menos que PNG",
              ext4 == ".jpg")

    # --- 5. La bomba de descompresion --------------------------------------
    bomba = _bomba_png()
    comprobar(f"la bomba de prueba pesa poco ({len(bomba) // 1024} KB) "
              f"y declara 30000x30000", len(bomba) < 2 * 1024 * 1024)
    try:
        im.ajustar(bomba)
        comprobar("una bomba de descompresion tiene que rechazarse", False)
    except im.ErrorImagen as exc:
        comprobar("una bomba de descompresion se rechaza con un mensaje claro",
                  "megapixeles" in str(exc) or "descomunal" in str(exc))
    except Exception as exc:  # noqa: BLE001
        comprobar(f"la bomba se rechaza SIN dejar escapar {type(exc).__name__}",
                  False)

    comprobar("el tope de pixeles es alto para una camara real pero acotado",
              50_000_000 <= im.MAXIMO_PIXELES <= 200_000_000)

    # --- 6. Basura -----------------------------------------------------------
    for basura, que in (
        (b"", "vacio"),
        (b"\xff\xd8\xff\xe0 esto no llega a ser un jpeg", "un JPEG cortado"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 40, "un PNG truncado"),
        (bytes(range(256)) * 8, "bytes al azar"),
    ):
        try:
            im.ajustar(basura)
            comprobar(f"{que}: tiene que dar error", False)
        except im.ErrorImagen:
            comprobar(f"{que}: se rechaza con un mensaje para ensenar", True)
        except Exception as exc:  # noqa: BLE001
            comprobar(f"{que}: escapo un {type(exc).__name__} sin envolver",
                      False)

    print()
    if FALLOS:
        print(f"{len(FALLOS)} prueba(s) fallaron:")
        for f in FALLOS:
            print("  -", f)
        raise SystemExit(1)
    print("Todas las pruebas pasaron.")


if __name__ == "__main__":
    main()
