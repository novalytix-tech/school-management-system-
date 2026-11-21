import os
from app import create_app
from app.config import env_flag
from app.extensions import socketio

# Production config unless explicitly told otherwise (secure cookies, no debug).
env = os.getenv("FLASK_ENV", "production")
app = create_app(env)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    # The Werkzeug debugger allows running code from the browser, so it is only
    # enabled on explicit request (FLASK_DEBUG=1) and then bound to this machine.
    debug = env == "development" and env_flag("FLASK_DEBUG", False)
    host = "127.0.0.1" if debug else os.getenv("HOST", "0.0.0.0")
    socketio.run(app, host=host, port=port, debug=debug, allow_unsafe_werkzeug=True)
