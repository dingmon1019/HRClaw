from app.api.app import create_app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    from app.config.settings import get_app_settings

    settings = get_app_settings()
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level,
    )

