import asyncio
from btsbots.bizbots import BizBots

async def main():
    bot = BizBots()
    try:
        await bot.run()
        await bot.start_oauth_queue_listener()
        while True:
            await asyncio.sleep(1)

    except Exception as e:
        print(f"🚨 商务端守护进程运行异常退出: {e}", flush=True)
        import traceback
        traceback.print_exc()
    finally:
        await bot.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass