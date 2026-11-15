# 撰寫 FastAPI + MCP Client
'''
main.py 是一個對外提供服務的網站後端（讓前端或使用者傳訊息進來）。它要接收使用者的請求、呼叫 Google Gemini AI、並且在 AI 需要工具時，
由內建的 Client 去跟獨立的 Server 溝通。所以 Client 自然會和你的網站主邏輯（main.py）寫在一起。
'''

import asyncio
import os
import time
import traceback
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from pydantic import BaseModel

from google import genai
from google.genai import errors, types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# 新增：引入 LINE Bot SDK v3 相關模組
from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

load_dotenv()

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
MAX_TOOL_ROUNDS = 10
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL")
MAX_RETRIES = 2
GEMINI_TIMEOUT = 30 # 單次 Gemini 呼叫最長等待秒數
TOOL_TIMEOUT = 20   # 單次 MCP 工具最長等待秒數
TOTAL_TIMEOUT = 75  # 一次 /chat 請求的總上限秒數

# 新增：讀取 .env 中的 LINE 金鑰
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")

line_configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
line_parser = WebhookParser(LINE_CHANNEL_SECRET)

gemini_client = genai.Client(
    http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT * 1000)
)

server_params = StdioServerParameters(
    command="uv",
    args=["run", "server.py"],
)

mcp_session: ClientSession | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global mcp_session
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_session = session
            print("✅ FastAPI 已成功連接到 CampusSchedule MCP Server！")
            yield
            mcp_session = None


app = FastAPI(title="Campus Schedule AI Agent", lifespan=lifespan)


class ChatRequest(BaseModel):
    user_id: str
    message: str


async def build_gemini_tools() -> list[types.Tool]:
    """向 MCP Server 取得工具清單，轉成 Gemini 的格式。"""
    resp = await mcp_session.list_tools()
    declarations = [
        types.FunctionDeclaration(
            name=t.name,
            description=t.description or "",
            parameters_json_schema=getattr(t, "input_schema", None)
            or getattr(t, "inputSchema", None)
            or {"type": "object", "properties": {}},
        )
        for t in resp.tools
    ]
    return [types.Tool(function_declarations=declarations)]


async def generate_with_retry(contents, config):
    """呼叫 Gemini；遇到 503/429 等暫時性錯誤時自動重試。"""
    models = [GEMINI_MODEL] + ([GEMINI_FALLBACK_MODEL] if GEMINI_FALLBACK_MODEL else [])
    last_err: Exception | None = None
    for model in models:
        for attempt in range(MAX_RETRIES):
            try:
                t0 = time.perf_counter()
                result = await asyncio.wait_for(
                    gemini_client.aio.models.generate_content(
                        model=model, contents=contents, config=config
                    ),
                    timeout=GEMINI_TIMEOUT + 5,
                )
                print(f"⏱️ Gemini 回應耗時 {time.perf_counter() - t0:.1f}s ({model})")
                return result
            except errors.APIError as e:
                if e.code not in (429, 500, 503, 504):
                    raise
                last_err = e
                wait = 2 ** attempt
                print(f"⚠️ {model} 暫時不可用 ({e.code})，{wait} 秒後重試 ({attempt + 1}/{MAX_RETRIES})")
                await asyncio.sleep(wait)
    raise last_err


# =====================================================================
# 核心 AI 對話邏輯（抽成共用函式，讓 /chat 和 LINE Webhook 都能呼叫）
# =====================================================================
async def process_ai_chat(user_id: str, message: str) -> str:
    if not mcp_session:
        return "系統維護中（MCP Server 尚未連線），請稍後再試。"

    system_instruction = f"""
    你是一個親切的校園課表與學習助理。
    目前與你對話的使用者 ID 是：'{user_id}'。

    【重要規則】
    1. 當使用者查詢課表、新增課程或更新備註時，務必呼叫對應的工具，並將 user_id 參數設為 '{user_id}'。
    2. 如果使用者貼上一整段亂七八糟的課表文字要求匯入，請幫他解析出每堂課的「課名、星期(格式必須是週一~週五)、時間、教室」，並逐一呼叫 add_course 工具寫入。
    3. 回答請簡潔明瞭，適合在手機聊天軟體上閱讀。
    """

    try:
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=await build_gemini_tools(),
            temperature=0.2,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        contents: list[types.Content] = [
            types.Content(role="user", parts=[types.Part(text=message)])
        ]

        started = time.perf_counter()
        for _ in range(MAX_TOOL_ROUNDS):
            if time.perf_counter() - started > TOTAL_TIMEOUT:
                return "處理時間過長，請稍後再試一次。"
            response = await generate_with_retry(contents, config)

            calls = response.function_calls
            if not calls:
                return response.text or "已完成處理。"

            contents.append(response.candidates[0].content)

            result_parts: list[types.Part] = []
            for call in calls:
                args = dict(call.args or {})
                print(f"AI 呼叫工具: {call.name}, 參數: {args}")
                try:
                    t0 = time.perf_counter()
                    result = await asyncio.wait_for(
                        mcp_session.call_tool(call.name, arguments=args),
                        timeout=TOOL_TIMEOUT,
                    )
                    print(f"⏱️ 工具 {call.name} 耗時 {time.perf_counter() - t0:.1f}s")
                    output = "\n".join(
                        c.text for c in result.content if getattr(c, "text", None)
                    ) or "完成"
                    is_error = bool(
                        getattr(result, "is_error", None) or getattr(result, "isError", False)
                    )
                    payload = {"error": output} if is_error else {"result": output}
                    flag = "❌ 工具回傳錯誤" if is_error else "📦 工具回傳"
                    print(f"{flag} [{call.name}]: {output[:500]}")
                except asyncio.TimeoutError:
                    print(f"❌ 工具 {call.name} 超過 {TOOL_TIMEOUT}s 沒有回應")
                    payload = {"error": "工具執行逾時"}
                except Exception as tool_err:
                    print(f"❌ 呼叫工具 {call.name} 發生例外: {tool_err!r}")
                    payload = {"error": f"{type(tool_err).__name__}: {tool_err}"}

                result_parts.append(
                    types.Part.from_function_response(name=call.name, response=payload)
                )

            contents.append(types.Content(role="user", parts=result_parts))

        return "處理步驟過多，請把需求拆小一點再試一次。"

    except asyncio.TimeoutError:
        print("❌ Gemini 呼叫逾時")
        return "AI 思考時間過長，請稍後再試一次。"
    except Exception as e:
        traceback.print_exc()
        return f"處理時發生一點小問題，請稍後再試。（錯誤：{type(e).__name__}）"


# 保留原本的 /chat 路由，方便你繼續用 Swagger UI 或 curl 測試
@app.post("/chat")
async def chat_with_agent(req: ChatRequest):
    if not mcp_session:
        raise HTTPException(status_code=500, detail="MCP Server 尚未連線")
    reply_text = await process_ai_chat(req.user_id, req.message)
    return {"reply": reply_text}


# =====================================================================
# 新增：給 LINE 呼叫的 Webhook 路由 (/callback)
# =====================================================================
async def handle_line_text_event(event: MessageEvent):
    """在背景非同步處理 AI 思考與工具呼叫，完成後用 reply_token 回覆 LINE"""
    user_id = event.source.user_id  # 自動抓出這位 LINE 使用者的專屬 ID！
    user_message = event.message.text
    print(f"📩 收到來自 LINE ({user_id}) 的訊息: {user_message}")

    reply_text = await process_ai_chat(user_id, user_message)

    with ApiClient(line_configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)],
            )
        )
    print(f"📤 已回覆 LINE ({user_id}): {reply_text[:100]}...")


@app.post("/callback")
async def line_webhook(request: Request, background_tasks: BackgroundTasks):
    # get X-Line-Signature header value
    
    signature = request.headers.get("X-Line-Signature", "")

    # get request body as text
    body = (await request.body()).decode("utf-8")

    # handle webhook body
    try:
        events = line_parser.parse(body, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid LINE signature")

    for event in events:
        if isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
            # 交給背景任務執行，立刻回傳 200 OK 給 LINE 避免 Webhook 逾時
            background_tasks.add_task(handle_line_text_event, event)

    return "OK"
