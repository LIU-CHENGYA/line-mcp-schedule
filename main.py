# 撰寫 FastAPI + MCP Client
'''
main.py 是一個對外提供服務的網站後端（讓前端或使用者傳訊息進來）。它要接收使用者的請求、呼叫 Google Gemini AI、並且在 AI 需要工具時，
由內建的 Client 去跟獨立的 Server 溝通。所以 Client 自然會和你的網站主邏輯（main.py）寫在一起。
'''
import asyncio
import os
import time,json
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
from linebot.v3 import WebhookParser,WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    AsyncApiClient,
    AsyncMessagingApi,
    Configuration,
    AsyncMessagingApi,
    ReplyMessageRequest,
    TextMessage,
    FlexMessage,
    FlexContainer
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
# 工具函式：解析 AI 的 JSON 回覆 / 產生 Flex Message
# =====================================================================
def _reply(text: str) -> dict:
    """統一的回傳格式：只有文字、沒有課程卡片。"""
    return {"reply": text, "courses": []}


def parse_ai_json(text: str) -> dict:
    """把 AI 回的文字解析成 {"reply": str, "courses": list[dict]}。
    解析失敗時，把整段文字當成一般回覆，不會讓程式壞掉。"""
    cleaned = (text or "").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(cleaned[start : end + 1])
            if isinstance(data, dict):
                courses = data.get("courses") or []
                return {
                    "reply": str(data.get("reply") or ""),
                    "courses": [c for c in courses if isinstance(c, dict)],
                }
        except json.JSONDecodeError:
            pass
    return _reply(cleaned or "已完成處理。")


def build_course_bubble(course: dict) -> dict:
    """依照單一課程資料產生一張 Flex bubble（直接組 dict，不靠寫死的索引）。"""

    def row(label: str, value) -> dict:
        return {
            "type": "box",
            "layout": "baseline",
            "spacing": "sm",
            "contents": [
                {"type": "text", "text": label, "color": "#aaaaaa", "size": "sm", "flex": 1},
                {
                    "type": "text",
                    # LINE 不接受空字串，所以一定要給預設值
                    "text": str(value).strip() if value else "未填寫",
                    "wrap": True,
                    "color": "#666666",
                    "size": "sm",
                    "flex": 4,
                },
            ],
        }

    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": str(course.get("course_name") or "未命名課程"),
                    "weight": "bold",
                    "size": "xl",
                    "wrap": True,
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "margin": "lg",
                    "spacing": "sm",
                    "contents": [
                        row("星期", course.get("day")),
                        row("時間", course.get("time")),
                        row("教室", course.get("location")),
                        row("備註", course.get("note")),
                    ],
                },
            ],
        },
    }


def build_flex_message(courses: list[dict]) -> FlexMessage | None:
    """一堂課 -> bubble；多堂課 -> carousel（LINE 上限 12 張）。"""
    if not courses:
        return None
    bubbles = [build_course_bubble(c) for c in courses[:12]]
    container = bubbles[0] if len(bubbles) == 1 else {"type": "carousel", "contents": bubbles}
    return FlexMessage(
        alt_text="您的課程資訊",
        contents=FlexContainer.from_dict(container),
    )


# =====================================================================
# 核心 AI 對話邏輯：一律回傳 dict {"reply": str, "courses": list}
# =====================================================================
async def process_ai_chat_to_json(user_id: str, message: str) -> dict:
    if not mcp_session:
        return "系統維護中（MCP Server 尚未連線），請稍後再試。"

    system_instruction =  f"""
    你是一個校園課表助手。目前與你對話的使用者 ID 是：'{user_id}'。

    【重要規則】
    1. 當使用者查詢課表、新增課程或更新備註時，務必呼叫對應的工具，並將 user_id 參數設為 '{user_id}'。
    2. 如果使用者貼上一整段亂七八糟的課表文字要求匯入，請幫他解析出每堂課的「課名、星期(格式必須是週一~週五)、時間、教室」，並逐一呼叫 add_course 工具寫入。
    3. 回答請簡潔明瞭，適合在手機聊天軟體上閱讀。
    4. 當你不需要再呼叫任何工具、要給出最終回覆時，必須只輸出一個 JSON 物件，
    不要有任何多餘文字或 markdown 標籤（例如 ```json），格式如下：
    {{"reply": "給使用者的簡短文字", "courses": [{{"course_name": "課程名稱", "day": "週一", "time": "10:00-12:00", "location": "教室", "note": "備註"}}]}}
    5. courses 只放「這次要顯示給使用者看」的課程（例如剛新增或剛查到的）；沒有的話請給空陣列 []。
    欄位沒有資料時請填空字串 ""。
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
                return parse_ai_json(response.text or "")

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

        return _reply("處理步驟過多，請把需求拆小一點再試一次。")

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
    result = await process_ai_chat_to_json(req.user_id, req.message)
    return {"reply": result["reply"], "courses": result["courses"]}


# =====================================================================
# # LINE Webhook
# =====================================================================
async def handle_text_message(event: MessageEvent):
    """在背景非同步處理 AI 思考與工具呼叫，完成後用 reply_token 回覆 LINE"""
    user_id = event.source.user_id  # 自動抓出這位 LINE 使用者的專屬 ID！
    user_message = event.message.text
    print(f"📩 收到來自 LINE ({user_id}) 的訊息: {user_message}")
    try:
        result = await process_ai_chat_to_json(user_id, user_message)

        messages = []
        if result["reply"]:
            messages.append(TextMessage(text=result["reply"][:5000]))
        flex = build_flex_message(result["courses"])
        if flex:
            messages.append(flex)
        if not messages:
            messages.append(TextMessage(text="已完成處理。"))

        async with AsyncApiClient(line_configuration) as api_client:
            line_bot_api = AsyncMessagingApi(api_client)
            await line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=messages,
                )
            )
    except Exception:
        # 背景任務的錯誤不會顯示在回應裡，一定要自己印出來
        traceback.print_exc()



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
            background_tasks.add_task(handle_text_message, event)

    return "OK"