import streamlit as st
import psycopg

try:
    DATABASE_URL = st.secrets["DATABASE_URL"]

    with psycopg.connect(DATABASE_URL, sslmode="require") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            total = cur.fetchone()[0]

    st.success(f"✅ Conexión correcta. Usuarios encontrados: {total}")

except Exception as e:
    st.error(f"❌ Error: {e}")
