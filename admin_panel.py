import csv
from datetime import datetime
from db_connection import get_connection

# =========================
# ADMIN LOGIN
# =========================
def admin_login():
    username = input("Enter admin username: ")
    password = input("Enter admin password: ")

    conn = get_connection()
    cursor = conn.cursor()

    sql = """
    SELECT * FROM admin
    WHERE username = %s AND password = %s
    """

    cursor.execute(sql, (username, password))
    result = cursor.fetchone()

    cursor.close()
    conn.close()

    if result:
        print("\nLogin successful. Welcome Admin!")
        return True
    else:
        print("\nInvalid username or password.")
        return False


# =========================
# VIEW ALL ATTENDANCE
# =========================
def view_attendance():
    conn = get_connection()
    cursor = conn.cursor()

    sql = """
    SELECT 
        attendance.id,
        students.student_id,
        students.name,
        students.course,
        students.section,
        attendance.date,
        attendance.time,
        attendance.status
    FROM attendance
    JOIN students ON attendance.student_id = students.student_id
    ORDER BY attendance.date DESC, attendance.time DESC
    """

    cursor.execute(sql)
    records = cursor.fetchall()

    print("\n===== ATTENDANCE RECORDS =====")

    if not records:
        print("No attendance records found.")
    else:
        for row in records:
            print(
                f"ID: {row[0]} | Student ID: {row[1]} | Name: {row[2]} | "
                f"Course: {row[3]} | Section: {row[4]} | Date: {row[5]} | "
                f"Time: {row[6]} | Status: {row[7]}"
            )

    cursor.close()
    conn.close()


# =========================
# VIEW ALL STUDENTS
# =========================
def view_students():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM students")
    students = cursor.fetchall()

    print("\n===== STUDENT LIST =====")

    if not students:
        print("No students found.")
    else:
        for student in students:
            print(
                f"Student ID: {student[0]} | Name: {student[1]} | "
                f"Course: {student[2]} | Section: {student[3]}"
            )

    cursor.close()
    conn.close()


# =========================
# UPDATE ATTENDANCE STATUS
# =========================
def update_attendance():
    attendance_id = input("Enter Attendance Record ID to update: ")
    new_status = input("Enter new status (Present/Absent/Late): ")

    conn = get_connection()
    cursor = conn.cursor()

    sql = """
    UPDATE attendance
    SET status = %s
    WHERE id = %s
    """

    cursor.execute(sql, (new_status, attendance_id))
    conn.commit()

    if cursor.rowcount > 0:
        print("Attendance record updated successfully.")
    else:
        print("Attendance record not found.")

    cursor.close()
    conn.close()


# =========================
# DELETE ATTENDANCE RECORD
# =========================
def delete_attendance():
    attendance_id = input("Enter Attendance Record ID to delete: ")

    conn = get_connection()
    cursor = conn.cursor()

    sql = "DELETE FROM attendance WHERE id = %s"

    cursor.execute(sql, (attendance_id,))
    conn.commit()

    if cursor.rowcount > 0:
        print("Attendance record deleted successfully.")
    else:
        print("Attendance record not found.")

    cursor.close()
    conn.close()

# =========================
# EXPORT ATTENDANCE REPORT
# =========================
def export_attendance_report():
    conn = get_connection()
    cursor = conn.cursor()

    sql = """
    SELECT 
        attendance.id,
        students.student_id,
        students.name,
        students.course,
        students.section,
        attendance.date,
        attendance.time,
        attendance.status
    FROM attendance
    JOIN students ON attendance.student_id = students.student_id
    ORDER BY attendance.date DESC, attendance.time DESC
    """

    cursor.execute(sql)
    records = cursor.fetchall()

    if not records:
        print("No attendance records to export.")
        cursor.close()
        conn.close()
        return

    filename = f"attendance_report_{datetime.now().strftime('%Y%m%d_%I%M%S %p')}.csv"

    with open(filename, "w", newline="") as file:
        writer = csv.writer(file)

        writer.writerow([
            "Record ID",
            "Student ID",
            "Name",
            "Course",
            "Section",
            "Date",
            "Time",
            "Status"
        ])

        for row in records:
            writer.writerow(row)

    cursor.close()
    conn.close()

    print(f"Attendance report exported successfully: {filename}")


# =========================
# ADD SUBJECT
# =========================
def add_subject():
    subject_code = input("Enter Subject Code: ")
    subject_name = input("Enter Subject Name: ")
    course = input("Enter Course: ")
    section = input("Enter Section: ")

    conn = get_connection()
    cursor = conn.cursor()

    sql = """
    INSERT INTO subjects (subject_code, subject_name, course, section)
    VALUES (%s, %s, %s, %s)
    """

    try:
        cursor.execute(sql, (subject_code, subject_name, course, section))
        conn.commit()
        print("Subject added successfully.")

    except Exception as e:
        print("Database Error:", e)

    finally:
        cursor.close()
        conn.close()


# =========================
# VIEW SUBJECTS
# =========================
def view_subjects():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM subjects")
    subjects = cursor.fetchall()

    print("\n===== SUBJECT LIST =====")

    if not subjects:
        print("No subjects found.")
    else:
        for subject in subjects:
            print(
                f"Subject ID: {subject[0]} | Code: {subject[1]} | "
                f"Name: {subject[2]} | Course: {subject[3]} | Section: {subject[4]}"
            )

    cursor.close()
    conn.close()

# =========================
# START ATTENDANCE SESSION
# =========================
def start_attendance_session():
    view_subjects()

    subject_id = input("Enter Subject ID to start session: ")

    conn = get_connection()
    cursor = conn.cursor()

    sql = """
    INSERT INTO attendance_sessions (subject_id, session_date, start_time, status)
    VALUES (%s, CURDATE(), CURTIME(), %s)
    """

    try:
        cursor.execute(sql, (subject_id, "Active"))
        conn.commit()

        session_id = cursor.lastrowid

        print(f"Attendance session started successfully.")
        print(f"Session ID: {session_id}")

        with open("active_session.txt", "w") as file:
            file.write(str(session_id))

        print("Active session saved. You can now run attendance_system.py")

    except Exception as e:
        print("Database Error:", e)

    finally:
        cursor.close()
        conn.close()

# =========================
# END ATTENDANCE SESSION
# =========================
def end_attendance_session():
    try:
        with open("active_session.txt", "r") as file:
            session_id = file.read().strip()

    except FileNotFoundError:
        print("No active session found.")
        return

    conn = get_connection()
    cursor = conn.cursor()

    sql = """
    UPDATE attendance_sessions
    SET status = %s
    WHERE session_id = %s
    """

    cursor.execute(sql, ("Ended", session_id))
    conn.commit()

    cursor.close()
    conn.close()

    import os
    os.remove("active_session.txt")

    print("Attendance session ended successfully.")
# =========================
# ADMIN MENU
# =========================
def admin_menu():
    while True:
        print("\n===== ADMIN PANEL =====")
        print("1. View Attendance")
        print("2. View Students")
        print("3. Update Attendance")
        print("4. Delete Attendance")
        print("5. Export Attendance Report")
        print("6. Add Subject")
        print("7. View Subjects")
        print("8. Start Attendance Session")
        print("9. End Attendance Session")
        print("10. Exit")

        choice = input("Choose option: ")

        if choice == "1":
            view_attendance()

        elif choice == "2":
            view_students()

        elif choice == "3":
            update_attendance()

        elif choice == "4":
            delete_attendance()

        elif choice == "5":
            export_attendance_report()

        elif choice == "6":
            add_subject()

        elif choice == "7":
            view_subjects()

        elif choice == "8":
            start_attendance_session()

        elif choice == "9":
            end_attendance_session()

        elif choice == "10":
            print("Exiting Admin Panel.")
            break

        else:
            print("Invalid choice. Please try again.")


# =========================
# MAIN PROGRAM
# =========================
if admin_login():
    admin_menu()