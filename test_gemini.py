"""最小測試：不經過 FastAPI / MCP，只確認 Gemini 本身穩不穩、哪些模型可用。
用法：  uv run test_gemini.py            -> 列出可用模型
        uv run test_gemini.py 模型名稱   -> 計時測試該模型 3 次
"""
import sys
import time

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()
client = genai.Client(http_options=types.HttpOptions(timeout=30_000))

if len(sys.argv) < 2:
    print("你的 API key 可用的模型：")
    for m in client.models.list():
        if "generateContent" in (m.supported_actions or []):
            print(" -", m.name.replace("models/", ""))
    print("\n挑一個後執行： uv run test_gemini.py <模型名稱>")
    sys.exit()

model = sys.argv[1]
for i in range(3):
    t0 = time.perf_counter()
    try:
        r = client.models.generate_content(model=model, contents="你好，請用一句話自我介紹")
        print(f"[{i+1}] {time.perf_counter()-t0:.1f}s  OK  {r.text[:40]!r}")
    except Exception as e:
        print(f"[{i+1}] {time.perf_counter()-t0:.1f}s  失敗  {type(e).__name__}: {str(e)[:120]}")