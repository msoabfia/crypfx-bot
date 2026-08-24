# فقط بخش run_bot را با این کد جایگزین کنید (برای اطمینان از اجرا):

def run_bot():
    """اجرای اصلی ربات"""
    global application
    
    logger.info("🚀 Starting bot...")
    
    # ایجاد Application
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    setup_handlers(application)
    
    # انتخاب بین Webhook و Polling
    use_webhook = os.environ.get('USE_WEBHOOK', 'true').lower() == 'true'
    
    if use_webhook:
        # استفاده از Webhook
        import asyncio
        try:
            asyncio.run(setup_webhook())
            logger.info("🤖 Bot running with webhook")
            # Flask در ترد جداگانه اجرا می‌شود، پس اینجا نگه می‌داریم
            import time
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
        except Exception as e:
            logger.error(f"Webhook error: {e}")
            # Fallback به Polling
            logger.info("🔄 Falling back to polling...")
            application.run_polling(
                drop_pending_updates=True,
                poll_interval=1.0,
                timeout=30
            )
    else:
        # استفاده از Polling
        logger.info("🤖 Bot running with polling")
        application.run_polling(
            drop_pending_updates=True,
            poll_interval=1.0,
            timeout=30
        )
