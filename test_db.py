import mysql.connector

try:
    conn = mysql.connector.connect(
        host="127.0.0.1",
        user="root",
        password=""
    )

    print("Database Connected Successfully")
    conn.close()

except Exception as e:
    print("Database Connection Failed:", e)