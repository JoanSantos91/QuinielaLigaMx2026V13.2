import streamlit as st
import psycopg


st.set_page_config(
    page_title="Prueba Supabase",
    page_icon="🔌",
)

st.title("Prueba de conexión con Supabase")

try:
    database_url = st.secrets["DATABASE_URL"]

    with psycopg.connect(
        database_url,
        sslmode="require",
        connect_timeout=15,
    ) as connection:

        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM users;")
            total_users = cursor.fetchone()[0]

    st.success("✅ Conexión correcta con Supabase")
    st.write(f"Usuarios encontrados: {total_users}")

except KeyError:
    st.error(
        "No se encontró DATABASE_URL en los Secrets de Streamlit."
    )

except Exception as error:
    st.error("❌ No se pudo conectar con Supabase")
    st.code(str(error))
