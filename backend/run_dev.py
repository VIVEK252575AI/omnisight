import asyncio, sys
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
import uvicorn
uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
