"""Container entrypoint: configure logging first, then serve."""
import uvicorn

import logs

if __name__ == "__main__":
    logs.setup()
    # log_config=None leaves uvicorn's own loggers to propagate into the structured handler; requests are logged by the app.
    uvicorn.run("server:app", host="0.0.0.0", port=8000, log_config=None, access_log=False)
