'''
server.py 檔案自己獨立出來:
    它是一個「工具提供者（外場廚房）」。它專門負責跟特定的資料源互動（例如讀寫資料庫、操作檔案）。
    它被設計成一個獨立的子行程（Subprocess）。當你的主程式需要它時，主程式會透過指令（這裡是用 uv run server.py）把它叫起來。
'''
import sqlite3
from mcp.server.mcpserver import MCPServer

# 初始化 MCP Server
mcp = MCPServer("CampusScheduleServer")

def get_db_connection():
    conn = sqlite3.connect("schedule.db")
    conn.row_factory = sqlite3.Row
    return conn

@mcp.tool()
def get_schedule(user_id: str, day_of_week: str = "") -> str:
    """
    查詢使用者的課表。
    :param user_id: 使用者的唯一識別碼 (例如 LINE user_id)
    :param day_of_week: 星期幾 (例如 '週一', '週三')。若留空則回傳整週課表。
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    if day_of_week:
        cursor.execute(
            "SELECT * FROM courses WHERE user_id = ? AND day_of_week = ?",
            (user_id, day_of_week)
        )
    else:
        cursor.execute(
            "SELECT * FROM courses WHERE user_id = ?",
            (user_id,)
        )

    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return f"找不到 {day_of_week if day_of_week else '任何'} 的課程資料。"

    result = []
    for row in rows:
        note_str = f" (備註: {row['notes']})" if row['notes'] else ""
        result.append(f"[{row['day_of_week']} {row['time_slot']}] {row['course_name']} @ {row['location']}{note_str}")
    
    return "\n".join(result)

@mcp.tool()
def add_course(user_id: str, course_name: str, day_of_week: str, time_slot: str, location: str = "未定") -> str:
    """
    為使用者新增單筆課程到課表中（也可用於 AI 解析文字後的批次匯入）。
    :param user_id: 使用者的唯一識別碼
    :param course_name: 課程名稱
    :param day_of_week: 上課星期 (請統一格式為 '週一', '週二', '週三', '週四', '週五')
    :param time_slot: 上課時間 (例如 '09:10-12:10')
    :param location: 上課教室地點
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO courses (user_id, course_name, day_of_week, time_slot, location, notes) VALUES (?, ?, ?, ?, ?, '')",
        (user_id, course_name, day_of_week, time_slot, location)
    )
    conn.commit()
    conn.close()
    return f"已成功新增課程：{day_of_week} {time_slot}《{course_name}》於 {location}。"

@mcp.tool()
def update_course_note(user_id: str, course_name: str, note: str) -> str:
    """
    更新特定課程的備註（例如作業提醒、調課通知、考試時間）。
    :param user_id: 使用者的唯一識別碼
    :param course_name: 課程名稱
    :param note: 要寫入的備註內容
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE courses SET notes = ? WHERE user_id = ? AND course_name LIKE ?",
        (note, user_id, f"%{course_name}%")
    )
    affected = cursor.rowcount
    conn.commit()
    conn.close()

    if affected == 0:
        return f"找不到名稱包含「{course_name}」的課程，無法更新備註。"
    return f"已將《{course_name}》的備註更新為：{note}"

if __name__ == "__main__":
    mcp.run()
