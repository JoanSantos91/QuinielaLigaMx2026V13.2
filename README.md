# Quiniela Joan Santos · Apertura 2026 · v13.2 optimizada

Versión rápida para Streamlit con almacenamiento SQLite local.

## Mejoras principales

- El panel de administrador ya no ejecuta todas las secciones a la vez.
- Captura manual dentro de un formulario: cambiar goles no recarga la app.
- Guardado de una jornada en una sola transacción.
- Reintentos breves cuando SQLite está ocupado, sin congelar durante 30 segundos.
- WAL se configura una sola vez al iniciar la base.
- Menús por sección para participantes y administrador.
- Escudos convertidos a memoria caché.
- Inicio y cierre de sesión más ligeros.

## Instalación

Sube todos los archivos a GitHub y reinicia la app desde Streamlit Cloud.
No requiere Supabase ni Secrets.

## Nota

SQLite local en Streamlit Community Cloud puede perderse si el contenedor se reconstruye. Esta versión prioriza velocidad y estabilidad, pero conviene conservar respaldos periódicos.
