import psycopg

DATABASE_URL = "DATABASE_URL = "postgresql://postgres.foiepzxytaohquzutwkf:Cachncha5791@aws-1-us-west-2.pooler.supabase.com:5432/postgres""

try:
    conn = psycopg.connect(DATABASE_URL, sslmode="require")

    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM users")

    print("Usuarios:", cur.fetchone()[0])

    conn.close()

    print("✅ Conexión correcta")

except Exception as e:
    print(e)
