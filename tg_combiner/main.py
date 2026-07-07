import asyncio
import logging
import signal
import uvicorn
from pyrogram import Client, filters
import config
from config import API_ID, API_HASH, BOT_TOKEN

# Using bot_interface locally down below

# Import FastAPI App
from webapp.main import app as fastapi_app, running_clients, broadcast_new_message
from modules.sender import get_session_files
from proxy_manager import proxy_to_pyrogram
from device_spoof import get_device_for_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-25s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("tg_combiner")
logging.getLogger("pyrogram").setLevel(logging.WARNING)

async def load_sessions_for_webapp():
    """
    Pre-load all Pyrogram Client sessions so the WebApp can interact with them immediately.
    We store them in webapp.main.running_clients.
    """
    sessions = get_session_files()
    for s_path in sessions:
        session_name = s_path.stem
        try:
            device = get_device_for_session(session_name)
            client = Client(
                name=str(s_path.with_suffix("")),
                api_id=API_ID,
                api_hash=API_HASH,
                device_model=device["device_model"],
                system_version=device["system_version"],
                app_version=device["app_version"],
                proxy=config.PYROGRAM_PROXY
            )
            
            @client.on_message(~filters.me | filters.me)
            async def on_new_msg(cli, msg, s_name=session_name):
                # Don't block message processing
                asyncio.create_task(broadcast_new_message(s_name, msg))
            
            await client.start()
            running_clients[session_name] = client
            logger.info(f"Loaded session for WebApp: {session_name}")
        except Exception as e:
            logger.error(f"Could not load session {session_name} for WebApp: {e}")
    
    logger.info(f"Session loading complete. {len(running_clients)} sessions active.")

async def run_bot():
    """Run the Pyrogram bot for the Admin interface."""
    bot = Client(
        "tg_combiner_bot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        workdir="sessions",
        proxy=config.PYROGRAM_PROXY
    )
    
    # We pass the bot instance so it can register handlers
    # We can't use decorators cleanly across files without passing the app
    import bot_interface
    bot_interface.register_handlers(bot)

    logger.info("Starting Telegram Combiner Admin Bot...")
    await bot.start()
    
    # Share bot instance with webapp for notifications
    from webapp.main import bot_client
    import webapp.main as webapp_module
    webapp_module.bot_client = bot
    
    # Set Menu Button URL with auth token so it works without initData
    try:
        from bot_interface import get_app_url
        from config import ADMIN_ID
        from pyrogram.types import MenuButtonWebApp, WebAppInfo
        menu_url = get_app_url(ADMIN_ID)
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="🚀 Пульт", web_app=WebAppInfo(url=menu_url))
        )
        logger.info(f"Menu Button URL set with auth token")
    except Exception as e:
        logger.warning(f"Could not set Menu Button: {e}")
    
    logger.info("Admin Bot started. Waiting for commands...")
    
    # Keep it running
    await asyncio.Event().wait()

async def run_fastapi():
    """Run the FastAPI server via Uvicorn programmatically."""
    config = uvicorn.Config(
        app=fastapi_app, 
        host="0.0.0.0", 
        port=8080, 
        log_level="warning"
    )
    server = uvicorn.Server(config)
    logger.info("Starting FastAPI WebApp Server on port 8080...")
    await server.serve()

async def shutdown_all_clients():
    """Gracefully stop all running Pyrogram sessions."""
    logger.info(f"Shutting down {len(running_clients)} running client(s)...")
    for name, client in list(running_clients.items()):
        try:
            await client.stop()
            logger.info(f"Stopped client: {name}")
        except Exception as e:
            logger.warning(f"Error stopping client {name}: {e}")
    running_clients.clear()

async def main():
    logger.info("Starting TG Combiner v2 Ecosystem...")
    
    # Start sessions in background with exception logging
    def _on_session_load_done(task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"Session loader failed: {exc}")
    
    session_task = asyncio.create_task(load_sessions_for_webapp())
    session_task.add_done_callback(_on_session_load_done)

    # Ловим SIGTERM (docker stop) — иначе finally недостижим и клиенты Pyrogram
    # убиваются жёстко (риск повреждения .session и переавторизации).
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # Windows

    runner = asyncio.gather(run_bot(), run_fastapi())
    try:
        stop_task = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait({runner, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done:
            logger.info("Получен сигнал остановки — graceful shutdown...")
    finally:
        runner.cancel()
        try:
            await runner
        except BaseException:
            pass
        await shutdown_all_clients()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down...")
