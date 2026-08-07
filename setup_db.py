"""
One-click MySQL Database Setup Script for AI Attendance System.
Creates 'attendancesystem_db' database, all required tables, and default admin user.
"""

import mysql.connector

def setup_database(host="127.0.0.1", user="root", password=""):
    print("=" * 60)
    print("AI ATTENDANCE SYSTEM - MYSQL DATABASE SETUP")
    print("=" * 60)
    
    # Connect to MySQL server (without specifying DB name first)
    try:
        conn = mysql.connector.connect(
            host=host,
            user=user,
            password=password
        )
        cursor = conn.cursor()
        print(f"[OK] Connected to MySQL server at {host}")
    except mysql.connector.Error as err:
        print(f"[ERROR] Could not connect to MySQL server: {err}")
        print("Make sure your MySQL Server (XAMPP/WAMP/MySQL Workbench) is running!")
        return False

    try:
        # Create database
        cursor.execute("CREATE DATABASE IF NOT EXISTS attendancesystem_db")
        print("[OK] Database 'attendancesystem_db' verified/created.")
        
        cursor.execute("USE attendancesystem_db")
        
        # 1. Admin Table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS admin (
            id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(100) NOT NULL UNIQUE,
            password VARCHAR(255) NOT NULL
        )
        """)
        print("  - Table 'admin' verified.")
        
        # 2. Instructors Table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS instructors (
            id INT AUTO_INCREMENT PRIMARY KEY,
            instructor_id VARCHAR(100) NOT NULL UNIQUE,
            fullname VARCHAR(255) NOT NULL,
            password VARCHAR(255) NOT NULL
        )
        """)
        print("  - Table 'instructors' verified.")
        
        # 3. Students Table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS students (
            student_id VARCHAR(100) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            college_department VARCHAR(255),
            program VARCHAR(255),
            year_level INT,
            section VARCHAR(100)
        )
        """)
        print("  - Table 'students' verified.")
        
        # 4. Subjects Table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS subjects (
            id INT AUTO_INCREMENT PRIMARY KEY,
            subject_code VARCHAR(100) NOT NULL,
            subject_name VARCHAR(255) NOT NULL,
            instructor VARCHAR(255),
            day VARCHAR(100),
            course VARCHAR(100),
            section VARCHAR(100),
            time_in VARCHAR(50),
            time_out VARCHAR(50)
        )
        """)
        print("  - Table 'subjects' verified.")
        
        # 5. Attendance Table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INT AUTO_INCREMENT PRIMARY KEY,
            student_id VARCHAR(100) NOT NULL,
            student_name VARCHAR(255) NOT NULL,
            subject_code VARCHAR(100) NOT NULL,
            attendance_date DATE NOT NULL,
            time_in TIME NOT NULL,
            status VARCHAR(50) NOT NULL,
            INDEX idx_student (student_id),
            INDEX idx_date_subject (attendance_date, subject_code)
        )
        """)
        print("  - Table 'attendance' verified.")
        
        # Seed Default Admin
        cursor.execute("SELECT * FROM admin WHERE id = 1")
        if cursor.fetchone() is None:
            cursor.execute("INSERT INTO admin (id, username, password) VALUES (1, 'admin', 'admin')")
            conn.commit()
            print("[OK] Seeded default Admin user (Username: 'admin' | Password: 'admin')")
        else:
            print("[OK] Admin user exists.")
            
        print("=" * 60)
        print("SUCCESS! Database setup completed cleanly.")
        print("You can now run 'python app.py'")
        print("=" * 60)
        return True
        
    except mysql.connector.Error as err:
        print(f"[ERROR] SQL execution error: {err}")
        return False
    finally:
        cursor.close()
        conn.close()

if __name__ == "__main__":
    setup_database()
