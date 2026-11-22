'''
server.py 檔案自己獨立出來:
    它是一個「工具提供者（外場廚房）」。它專門負責跟特定的資料源互動（例如讀寫資料庫、操作檔案）。
    它被設計成一個獨立的子行程（Subprocess）。當你的主程式需要它時，主程式會透過指令（這裡是用 uv run server.py）把它叫起來。
'''
import os
import psycopg2
import re
from psycopg2.extras import RealDictCursor
from mcp.server.mcpserver import MCPServer

# 初始化 MCP Server
mcp = MCPServer("CampusScheduleServer")

def get_db_connection():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise ValueError("找不到 DATABASE_URL 環境變數，請確認已正確設定！")
    
    # RealDictCursor 讓查詢結果像字典一樣可用 row['欄位名'] 存取，對應原本的 sqlite3.Row
    conn = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
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

    try:
        if day_of_week:
            cursor.execute(
                "SELECT * FROM courses WHERE user_id = %s AND day_of_week = %s",
                (user_id, day_of_week)
            )
        else:
            cursor.execute(
                "SELECT * FROM courses WHERE user_id = %s",
                (user_id,)
            )

        rows = cursor.fetchall()
    finally:
        cursor.close()
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
    try:
        cursor.execute(
            "INSERT INTO courses (user_id, course_name, day_of_week, time_slot, location, notes) VALUES (%s, %s, %s, %s, %s, '')",
            (user_id, course_name, day_of_week, time_slot, location)
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()
        
    return f"已成功新增課程：{day_of_week} {time_slot}《{course_name}》於 {location}。"

VALID_DAYS = {"週一", "週二", "週三", "週四", "週五", "週六", "週日"}

@mcp.tool()
def update_course(
    user_id: str,
    course_name: str,
    new_course_name: str = "",
    new_day_of_week: str = "",
    new_time_slot: str = "",
    new_location: str = "",
    new_note: str = "",
) -> str:
    """
    更新特定課程的資訊，可修改課程名稱、上課星期、上課時間、上課地點、備註。
    只需要提供要修改的欄位，沒提供（留空）的欄位不會被更動。
    :param user_id: 使用者的唯一識別碼
    :param course_name: 要修改的「目前」課程名稱，請盡量使用完整課名
    :param new_course_name: 新的課程名稱（不改名請留空）
    :param new_day_of_week: 新的上課星期，格式必須是 '週一'~'週日'（不改請留空）
    :param new_time_slot: 新的上課時間，格式為 'HH:MM-HH:MM'，例如 '13:20-16:20'（不改請留空）
    :param new_location: 新的上課地點（不改地點請留空）
    :param new_note: 新的備註內容，會覆蓋舊備註（不改備註請留空）
    """
    if not any([new_course_name, new_day_of_week, new_time_slot, new_location, new_note]):
        return "沒有提供任何要修改的內容，未做任何更動。"

    # 格式驗證：不合格就回報錯誤，讓 Agent 修正後重試
    if new_day_of_week and new_day_of_week not in VALID_DAYS:
        return f"星期格式錯誤：「{new_day_of_week}」，請使用 週一~週日，本次未做任何更動。"
    if new_time_slot and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d", new_time_slot):
        return f"時間格式錯誤：「{new_time_slot}」，請使用 HH:MM-HH:MM（例如 13:20-16:20），本次未做任何更動。"

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # 1. 找出符合的課程（同名課程可能有多個時段，所以取完整資料）
        cursor.execute(
            "SELECT id, course_name, day_of_week, time_slot FROM courses WHERE user_id = %s AND course_name LIKE %s",
            (user_id, f"%{course_name}%"),
        )
        rows = cursor.fetchall()

        # 2. 精確比對優先，其次唯一模糊命中
        exact = [r for r in rows if r["course_name"] == course_name]
        candidates = exact if exact else rows
        distinct_names = sorted({r["course_name"] for r in candidates})

        if not candidates:
            return f"找不到名稱包含「{course_name}」的課程，無法更新。"
        if len(distinct_names) > 1:
            return f"找到多門課程：{'、'.join(distinct_names)}，請指定要修改哪一門，本次未做任何更動。"
        if len(candidates) > 1:
            slots = "、".join(f"{r['day_of_week']} {r['time_slot']}" for r in candidates)
            return f"《{distinct_names[0]}》有多個上課時段：{slots}，請指定要修改哪一個時段，本次未做任何更動。"

        target = candidates[0]

        # 3. 只更新有提供的欄位（欄位名稱為寫死，值走參數化）
        fields, params = [], []
        if new_course_name:
            fields.append("course_name = %s"); params.append(new_course_name)
        if new_day_of_week:
            fields.append("day_of_week = %s"); params.append(new_day_of_week)
        if new_time_slot:
            fields.append("time_slot = %s"); params.append(new_time_slot)
        if new_location:
            fields.append("location = %s"); params.append(new_location)
        if new_note:
            fields.append("notes = %s"); params.append(new_note)

        params.extend([user_id, target["id"]])
        cursor.execute(
            f"UPDATE courses SET {', '.join(fields)} WHERE user_id = %s AND id = %s",
            params,
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    changes = []
    if new_course_name: changes.append(f"課名改為《{new_course_name}》")
    if new_day_of_week: changes.append(f"星期改為 {new_day_of_week}")
    if new_time_slot: changes.append(f"時間改為 {new_time_slot}")
    if new_location: changes.append(f"地點改為 {new_location}")
    if new_note: changes.append(f"備註改為：{new_note}")
    return f"已更新《{target['course_name']}》：" + "、".join(changes) + "。"


@mcp.tool()
def delete_course(
    user_id: str,
    course_name: str,
    day_of_week: str = "",
    confirm: bool = False,
) -> str:
    """
    刪除使用者課表中的一筆課程。此操作無法復原。
    必須先以 confirm=False 呼叫取得預覽，並向使用者確認後，才能以 confirm=True 再次呼叫。
    :param user_id: 使用者的唯一識別碼
    :param course_name: 要刪除的課程名稱，請盡量使用完整課名
    :param day_of_week: 該課程的上課星期（'週一'~'週日'）。同一門課有多個時段時，用來指定要刪哪一個，否則留空
    :param confirm: 使用者已明確表示確認刪除時才設為 True，預設 False 只做預覽
    """
    if day_of_week and day_of_week not in VALID_DAYS:
        return f"星期格式錯誤：「{day_of_week}」，請使用 週一~週日，本次未做任何更動。"

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT id, course_name, day_of_week, time_slot, location FROM courses "
            "WHERE user_id = %s AND course_name LIKE %s",
            (user_id, f"%{course_name}%"),
        )
        rows = cursor.fetchall()

        # 精確比對優先
        exact = [r for r in rows if r["course_name"] == course_name]
        candidates = exact if exact else rows

        # 有指定星期就再過濾
        if day_of_week:
            candidates = [r for r in candidates if r["day_of_week"] == day_of_week]

        if not candidates:
            return f"找不到符合「{course_name}」{day_of_week}的課程，無法刪除。"

        distinct_names = sorted({r["course_name"] for r in candidates})
        if len(distinct_names) > 1:
            return f"找到多門課程：{'、'.join(distinct_names)}，請指定要刪除哪一門，本次未做任何更動。"
        if len(candidates) > 1:
            slots = "、".join(f"{r['day_of_week']} {r['time_slot']}" for r in candidates)
            return f"《{distinct_names[0]}》有多個上課時段：{slots}，請指定要刪除哪一個時段，本次未做任何更動。"

        target = candidates[0]
        desc = f"{target['day_of_week']} {target['time_slot']}《{target['course_name']}》@ {target['location']}"

        # 第一次呼叫只預覽，不刪除
        if not confirm:
            return f"【待確認】即將刪除：{desc}。請先詢問使用者是否確定刪除，確認後再以 confirm=True 呼叫。目前尚未刪除。"

        cursor.execute(
            "DELETE FROM courses WHERE user_id = %s AND id = %s",
            (user_id, target["id"]),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    return f"已刪除：{desc}。"
if __name__ == "__main__":
    mcp.run()
