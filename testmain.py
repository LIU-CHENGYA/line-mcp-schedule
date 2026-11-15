# 撰寫 FastAPI + MCP Client
'''
main.py 是一個對外提供服務的網站後端（讓前端或使用者傳訊息進來）。它要接收使用者的請求、呼叫 Google Gemini AI、並且在 AI 需要工具時，
由內建的 Client 去跟獨立的 Server 溝通。所以 Client 自然會和你的網站主邏輯（main.py）寫在一起。
'''
import os
import traceback
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

# Google GenAI SDK 與 MCP Client 套件
from google import genai
from google.genai import types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()
# 模型名稱放在 .env，之後換模型不用改程式碼，例如：GEMINI_MODEL=gemini-3.8-flash
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")


# 初始化 Gemini Client (會自動讀取 .env 中的 GEMINI_API_KEY)
## 建立 Gemini 的連線物件 : gemini_client
## 代表向 Google 的 Gemini 伺服器發送聊天請求、接收 AI 的思考結果
gemini_client = genai.Client()

# 設定要啟動的 MCP Server 指令 (使用 uv 執行 server.py)
server_params = StdioServerParameters(
    command="uv",
    args=["run", "server.py"],
)

# 全域變數用來保存 MCP Session
## 當你的程式與 server.py 建立好通道後，這個 mcp_session 就代表了「一條活著的雙向通訊對話框」。
# 透過它，你的主程式可以隨時去問 MCP Server：「你有哪些工具可以用？」或者「請幫我執行 add_course 這個函數」。
mcp_session: ClientSession | None = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global mcp_session # 透過宣告 global mcp_session，可以讓背景啟動的那條連線被全域共用
    async with stdio_client(server_params) as (read, write): #stdio_client: MCP 協議裡的一種「通訊管道（Transport）載體」，讓兩個獨立的FastAPI main.py 和 server.py 對話
        
        async with ClientSession(read, write) as session: 
            await session.initialize()# Initialize the connection between client and server
            mcp_session = session
            print("✅ FastAPI 已成功連接到 CampusSchedule MCP Server！")
            yield
            mcp_session = None

app = FastAPI(title="Campus Schedule AI Agent", lifespan=lifespan)

class ChatRequest(BaseModel):
    user_id: str
    message: str

@app.post("/chat")
async def chat_with_agent(req: ChatRequest):
    if not mcp_session:
        raise HTTPException(status_code=500, detail="MCP Server 尚未連線")

    # 設計 System Prompt，把 user_id 告訴 AI，並規範它的行為
    system_instruction = f"""
    你是一個親切的校園課表與學習助理。
    目前與你對話的使用者 ID 是：'{req.user_id}'。
    
    【重要規則】
    1. 當使用者查詢課表、新增課程或更新備註時，務必呼叫對應的工具，並將 user_id 參數設為 '{req.user_id}'。
    2. 如果使用者貼上一整段亂七八糟的課表文字要求匯入，請幫他解析出每堂課的「課名、星期(格式必須是週一~週五)、時間、教室」，並逐一呼叫 add_course 工具寫入。
    3. 回答請簡潔明瞭，適合在手機聊天軟體上閱讀。
    """


    try:
        # 直接把 mcp_session 傳給 tools：
        # SDK 會自動取得工具清單、執行 call_tool，並處理多輪（含多次）函數呼叫，
        # 最後回傳整理好的文字回答。
        response = await gemini_client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=req.message,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=[mcp_session],
                temperature=0.2,  # 低溫度讓工具呼叫更精準 # 降低溫度讓工具呼叫更精準 ## 控制 AI 「發揮創意（亂蓋）的程度」（數值通常在 0 到 2 之間）。
            ),
        )
        return {"reply": response.text}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"AI 處理發生錯誤: {str(e)}")