import os
from flask import Flask, request, jsonify, send_from_directory, session
# chat_service deve esportare:
# - handle_ii (NM + actions)
from chat_service import handle_ii
from openai import OpenAI

app = Flask(__name__, static_folder="static", static_url_path="/static")


# ----- SESSION (necessaria per NM) -----
# Usa env var se c'e', altrimenti fallback locale (ok per prototipo)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-change-me")
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")
_image_client = OpenAI()


# ---------- FRONTEND ----------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------- API DI TEST ----------
@app.route("/api/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok"}), 200


# ---------- API II (NM) ----------
@app.route("/api/ii", methods=["POST"])
def ii():
    """
    Endpoint dedicato a Intelligenze Imperfette (NM).
    Pattern A: frontend manda solo { message, action }.
    Backend (session) decide turn/state e chiama II (quando sara' OpenAI).
    """
    data = request.get_json(silent=True) or {}

    # Nuovo pattern
    message = (data.get("message") or "").strip()
    action = (data.get("action") or "").strip()

    # Compatibilita' con vecchio pattern (se ti arriva last_user_msg)
    if not message and data.get("last_user_msg"):
        message = (data.get("last_user_msg") or "").strip()

    result = handle_ii(message=message, action=action, session=session)
    return jsonify(result), 200


@app.route("/api/mirror-card-image", methods=["POST"])
def mirror_card_image():
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"ok": False, "error": "Prompt mancante."}), 400

    if not os.getenv("OPENAI_API_KEY"):
        return jsonify({"ok": False, "error": "OPENAI_API_KEY non configurata."}), 503

    try:
        img = _image_client.images.generate(
            model=OPENAI_IMAGE_MODEL,
            prompt=prompt,
            size="1024x1024",
        )
        first = (img.data or [None])[0]
        if not first:
            return jsonify({"ok": False, "error": "Nessuna immagine generata."}), 502

        b64_json = getattr(first, "b64_json", None)
        if b64_json:
            return jsonify({"ok": True, "image_url": f"data:image/png;base64,{b64_json}"}), 200

        image_url = getattr(first, "url", None)
        if image_url:
            return jsonify({"ok": True, "image_url": image_url}), 200

        return jsonify({"ok": False, "error": "Formato risposta immagine non supportato."}), 502
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True)
