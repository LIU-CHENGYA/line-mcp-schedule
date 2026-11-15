import sqlite3 # Python 內建的 SQLite 驅動程式

def init_database():
    conn = sqlite3.connect("schedule.db") # sqlite3.connect()類似 Session(engine)
    cursor = conn.cursor() # cursor 是 SQL 執行器 (類似session.exec() 的查詢介面) # conn 是資料庫連線物件 (類似Session)

    # 建立課程資料表（加入 user_id 以支援多使用者）
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS courses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        course_name TEXT NOT NULL,
        day_of_week TEXT NOT NULL,
        time_slot TEXT NOT NULL,
        location TEXT,
        notes TEXT
    )
    """)

    # 清空舊測試資料並寫入預設資料
    cursor.execute("DELETE FROM courses WHERE user_id = 'test_user_01'")
    
    sample_courses = [
        ("test_user_01", "機器學習", "週一", "14:20-17:20", "德田館 101", "下週交作業一"),
        ("test_user_01", "演算法設計", "週三", "09:10-12:10", "博理館 201", ""),
        ("test_user_01", "資料庫系統", "週五", "13:20-16:20", "管理學院一號館 405", "期中考範圍 Ch1-Ch5")
    ]

    cursor.executemany("""
    INSERT INTO courses (user_id, course_name, day_of_week, time_slot, location, notes)
    VALUES (?, ?, ?, ?, ?, ?)
    """, sample_courses)

    conn.commit()
    conn.close()
    print("✅ 資料庫 schedule.db 初始化完成！")

if __name__ == "__main__":
    init_database()
