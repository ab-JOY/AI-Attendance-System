import mysql.connector

def get_connection():
    return mysql.connector.connect(
        host="localhost",
        user="root",
        password="",  # put your MySQL password here
        database="attendancesystem_db"
    )