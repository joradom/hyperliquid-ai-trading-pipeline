---
description: Revisión obligatoria de privacidad para archivos y comunicaciones publicables de R102
globs: ["**/*"]
---

# Privacidad antes de publicar

La política obligatoria está en
[AGENTS.md: Privacy and information disclosure](../../AGENTS.md#privacy-and-information-disclosure).
Esta regla concreta cómo aplicarla; no presupone carga automática por todos los
agentes. También se aplica a textos generados, comentarios, commits y PR.

## Clasificar antes de sustituir o excluir

| Categoría | Tratamiento |
| --- | --- |
| Identificadores técnicos legítimos | Conservar nombres de módulos, símbolos de mercado, versiones, contratos, rutas relativas de datos y el identificador del proyecto. Endpoints como `/info`, rutas estándar como `/tmp` o `/proc` y versiones necesarias para reproducir un ensayo no identifican por sí mismos a una persona. |
| Datos personales o propios del entorno | Anonimizar nombres, login del sistema, hostname, IP privada, identificadores únicos del equipo y estructura personal del disco. Sustituir rutas por referencias relativas, `$HOME` o `<repo>`. Revisar tanto texto visible como enlaces, metadatos y comentarios. |
| Archivos privados | Excluir configuraciones reales de cuentas, secretos, autenticación/sesiones, logs, trazas, SQLite y capturas/resultados reales. Conservar las copias locales; informar antes de retirar del índice un archivo ya rastreado. |

Las fixtures publicables deben ser inequívocamente sintéticas y revisadas.
Un placeholder como `<PUBLIC_ACCOUNT_ADDRESS>` no es una dirección real;
constantes ficticias reconocibles usadas para validar formatos tampoco deben
confundirse con una cuenta personal. No convertir un valor real en "fixture"
ni declararlo público porque su formato coincide con el de un ejemplo.
Ante una coincidencia ambigua, revisar su finalidad antes de permitirla.

## Procedimiento breve de revisión

1. Enumerar archivos rastreados, nuevos publicables y staging. Revisar también
   nombres de archivos, destinos de enlaces y documentos generados; un archivo
   ignorado puede seguir rastreado o haber sido incluido anteriormente.
2. Buscar rutas personales, variantes de mayúsculas/minúsculas, rutas abreviadas,
   direcciones de wallets, secretos e identificadores privados. Examinar además
   mensajes de error, metadatos de informes, imágenes y adjuntos que una búsqueda
   de texto no pueda revisar. No copiar valores sensibles al informe de auditoría:
   indicar únicamente archivo, línea y categoría.
3. Separar referencias técnicas, placeholders y datos reales. Anonimizar solo
   lo identificable; excluir los archivos privados completos. Mantener datos
   sintéticos y documentación reproducible fuera de las carpetas de resultados.
4. Verificar el contenido exacto del staging, `.gitignore`, enlaces y ejemplos.
   Ejecutar `git diff --cached --check` y los tests pertinentes. Revisar por
   separado el texto final del commit/PR antes de publicarlo.
5. Si hay un secreto o una duda pendiente, detener la publicación e informar
   sin mostrar su valor. Revisar el historial que vaya a compartirse; no
   reescribirlo ni forzar un push sin autorización expresa. Publicar exige la
   autorización de la tarea, además de superar esta revisión.

## Control local del staging

Desde la raíz del repositorio, después de preparar los archivos autorizados:

```bash
python3 -B tools/check_privacy.py
git diff --cached --check
```

[El script](../../tools/check_privacy.py) usa únicamente la biblioteca estándar
y Git. Examina los blobs completos añadidos o modificados del staging, incluidos
los destinos de renombrados. No lee copias del directorio de trabajo ni archivos
ignorados/no preparados, no sigue symlinks ni escanea el historial. No cambia
archivos o índice ni consulta red, wallets o bases de datos; no instala hooks.

Cada hallazgo es una línea JSON con solo `file`, `line` y `category`, sin el
valor detectado. Los nombres de archivo que contengan valores sensibles se
anonimizan también. Línea 0 indica un hallazgo sobre el archivo o el índice.
Salida 0 sin texto: sin hallazgos; 1: revisar hallazgos; 2: error de lectura de
Git/argumentos o interrupción. Un staging vacío devuelve 0 y no certifica los
archivos aún no preparados.

El control reconoce patrones de rutas personales, wallets y secretos, junto
con ubicaciones/tipos de archivos privados. Distingue rutas estándar, URLs sin
credenciales, placeholders explícitos y constantes inequívocamente ficticias
de tests/fixtures; no exime directorios enteros de tests. No sustituye la revisión
humana de nombres, imágenes, metadatos o secretos/direcciones de formato no
reconocido. Detener la publicación ante hallazgos pendientes de revisión.

Referencia complementaria: [Seguridad](../../docs/SECURITY.md).
