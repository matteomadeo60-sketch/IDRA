import json
import os
import re
from typing import Any, Dict, Optional, List

# OpenAI SDK (Responses API)
from openai import OpenAI
from prompts import (
    SYSTEM_NM_T1,
    SYSTEM_NM_T2,
    SYSTEM_NM_T3,
    SYSTEM_VIVERE_REFLECTION,
    SYSTEM_VIVERE_FINAL,
    SYSTEM_VIVERE_CHOICES,
    SYSTEM_VIVERE_SCENE,
    SYSTEM_VIVERE_TITLE_HELP,
    SYSTEM_COCREARE_EXERCISE,
    SYSTEM_COCREARE_FINAL,
)


# =========================
# OpenAI client + config
# =========================

_client = OpenAI()

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
OPENAI_COCREARE_MODEL = os.getenv("OPENAI_COCREARE_MODEL", "gpt-5-mini")


def _openai_configured() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def _default_provider() -> str:
    return "openai" if _openai_configured() else "fallback"


def _set_runtime_status(
    session: Dict[str, Any],
    provider: str,
    fallback_reason: str = "",
    error: str = "",
) -> None:
    if session is None:
        return
    session["_runtime"] = {
        "provider": provider,
        "fallback_reason": fallback_reason,
        "last_error": error,
    }


def _debug_payload(session: Dict[str, Any]) -> Dict[str, Any]:
    runtime = session.get("_runtime", {}) if session else {}
    return {
        "openai_configured": _openai_configured(),
        "provider": runtime.get("provider") or _default_provider(),
        "fallback_reason": runtime.get("fallback_reason") or "",
        "last_error": runtime.get("last_error") or "",
    }




def _call_openai(
    system_prompt: str,
    user_prompt: str,
    previous_response_id: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """
    Esegue una singola chiamata alla Responses API
    e restituisce solo il testo di output.
    """
    payload = {
        "model": model or OPENAI_MODEL,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if previous_response_id:
        payload["previous_response_id"] = previous_response_id
    resp = _client.responses.create(**payload)
    return (resp.output_text or "").strip(), getattr(resp, "id", None)


# =========================
# SYSTEM PROMPTS (NM + VIVERE)
# =========================

def handle_ii(message: str, action: str, session: Dict[str, Any]) -> Dict[str, Any]:
    """
    Endpoint /api/ii per la modalita' NM.
    Il frontend invia un JSON con {message, action} e qui si decide
    cosa fare in base al turno della sessione e allo stato corrente.
    """
    try:
        _ensure_nm_session(session)
        session.modified = True
        _set_runtime_status(session, provider=_default_provider())

        message = (message or "").strip()
        action = (action or "").strip()

        if action == "nm_reset":
            return _handle_nm_action(action, session)

        if action.startswith("vivere_") or _vivere_active(session):
            return _handle_vivere(message, action, session)

        if action.startswith("cocreare_") or _cocreare_active(session):
            return _handle_cocreare(message, action, session)

        if action:
            return _handle_nm_action(action, session)

        turn = session["nm"]["turn"]

        if not message:
            return _ok_reply("Scrivimi qualcosa.", session)

        # testo libero valido solo in T1
        if turn == "T1":
            reply_text = _nm_t1_reply(message, session)
            _append_history(session, role="user", text=message)
            _append_history(session, role="assistant", text=reply_text)
            session["nm"]["ii_count_t1"] = session["nm"].get("ii_count_t1", 0) + 1
            print("NM ii_count_t1:", session["nm"]["ii_count_t1"])
            if session["nm"]["ii_count_t1"] >= 4:
                session["nm"]["t1_user_last2"] = _last_n_messages(session, role="user", n=2)
                session["nm"]["t1_ii_last2"] = _last_n_messages(session, role="assistant", n=2)
                session["nm"]["turn"] = "T2"
                session["nm"]["draft_description"] = _nm_t2_description(session)
                return _ok_reply(session["nm"]["draft_description"], session, provider=_provider(session))
            return _ok_reply(reply_text, session, provider=_provider(session))

        if turn == "T2":
            if message:
                reply_text = _nm_t2_description(session, hint=message)
                _append_history(session, role="user", text=message)
                _append_history(session, role="assistant", text=reply_text)
                return _ok_reply(reply_text, session, provider=_provider(session))
            return _ok_reply("Usa i bottoni della descrizione (Confermo/Modifica/Indietro).", session)

        if turn == "T3":
            if message:
                reply_text = _nm_t3_name(session, hint=message)
                session["nm"]["draft_name"] = reply_text
                _append_history(session, role="user", text=message)
                _append_history(session, role="assistant", text=reply_text)
                return _ok_reply(reply_text, session, provider=_provider(session))
            return _ok_reply("Usa i bottoni del nome (Confermo/Rigenera/Indietro).", session)

        # fallback safety
        session["nm"]["turn"] = "T1"
        return _ok_reply("Ripartiamo da T1.", session)

    except Exception as e:
        _set_runtime_status(session, provider="fallback", fallback_reason="handle_ii_exception", error=str(e))
        return {
            "reply": "C’è stato un errore interno, ma possiamo continuare.",
            "provider": "fallback",
            "ok": False,
            "error": str(e),
            "mode": _current_mode(session) if session else "NM",
            "nm": _nm_payload(session) if session else {},
            "vivere": _vivere_payload(session) if session else {},
            "cocreare": _cocreare_payload(session) if session else {},
            "debug": _debug_payload(session) if session else {},
        }


# =========================
# NM session + payload
# =========================

def _ensure_nm_session(session: Dict[str, Any]) -> None:
    if "nm" not in session or not isinstance(session.get("nm"), dict):
        session["nm"] = {
            "mode": "NM",
            "turn": "T1",

            # stati confermati
            "description_confirmed": False,
            "error_description": None,
            "name_confirmed": False,
            "error_name": None,

            # bozze (non-stato)
            "draft_description": None,
            "draft_name": None,

            # storico leggero (cookie-safe)
            "history": [],  # list of {role,text}
            "ii_count_t1": 0,
            "last_response_id": None,
        }


def _nm_payload(session: Dict[str, Any]) -> Dict[str, Any]:
    nm = session.get("nm", {}) if session else {}
    return {
        "turn": nm.get("turn", "—"),
        "draft_description": nm.get("draft_description") or "",
        "draft_name": nm.get("draft_name") or "",
        "description_confirmed": bool(nm.get("description_confirmed")),
        "name_confirmed": bool(nm.get("name_confirmed")),
        "ii_count_t1": int(nm.get("ii_count_t1", 0)),
    }


VIVERE_CHOICES = [
    ("1", "Voglio capirlo meglio."),
    ("2", "Vorrei ascoltarlo con calma."),
    ("3", "Provo a trasformarlo."),
]

COCREARE_CHOICES = [
    ("1", "Capire il problema"),
    ("2", "Cambiare prospettiva"),
    ("3", "Trovare un uso utile"),
]


def _current_mode(session: Dict[str, Any]) -> str:
    cocreare = session.get("cocreare", {})
    if cocreare.get("active") or cocreare.get("turn") == "DONE":
        return "COCREARE"
    vivere = session.get("vivere", {})
    if vivere.get("active") or vivere.get("turn") == "DONE":
        return "VIVERE"
    return "NM"


def _vivere_active(session: Dict[str, Any]) -> bool:
    return bool(session.get("vivere", {}).get("active"))


def _cocreare_active(session: Dict[str, Any]) -> bool:
    return bool(session.get("cocreare", {}).get("active"))


def _ensure_vivere_session(session: Dict[str, Any]) -> None:
    if "vivere" not in session or not isinstance(session.get("vivere"), dict):
        nm = session.get("nm", {})
        session["vivere"] = {
            "mode": "VIVERE",
            "turn": "V1",
            "active": True,
            "error_name": (nm.get("error_name") or "").strip(),
            "error_description": (nm.get("error_description") or "").strip(),
            "choice": None,
            "choices": [],
            "title": None,
            "mantra": None,
            "epithet": None,
            "artwork": None,
            "card_image_prompt": None,
            "history": [],
            "last_response_id": None,
        }


def _ensure_cocreare_session(session: Dict[str, Any]) -> None:
    if "cocreare" not in session or not isinstance(session.get("cocreare"), dict):
        nm = session.get("nm", {})
        vivere = session.get("vivere", {})
        session["cocreare"] = {
            "mode": "COCREARE",
            "turn": "C1",
            "active": True,
            "error_name": (nm.get("error_name") or "").strip(),
            "error_description": (nm.get("error_description") or "").strip(),
            "vivere_title": (vivere.get("title") or "").strip(),
            "choice": None,
            "activities": [label for _, label in COCREARE_CHOICES],
            "exercise": "",
            "question": "",
            "ai_use_tip": "",
            "handoff_prompt": "",
            "history": [],
            "last_response_id": None,
        }


def _vivere_payload(session: Dict[str, Any]) -> Dict[str, Any]:
    v = session.get("vivere", {}) if session else {}
    if not v:
        return {}
    return {
        "turn": v.get("turn", "â€”"),
        "active": bool(v.get("active")),
        "choice": v.get("choice"),
        "choices": v.get("choices") or [],
        "title": v.get("title") or "",
        "mantra": v.get("mantra") or "",
        "epithet": v.get("epithet") or "",
        "artwork": v.get("artwork") or "",
        "card_image_prompt": v.get("card_image_prompt") or "",
    }


def _cocreare_payload(session: Dict[str, Any]) -> Dict[str, Any]:
    c = session.get("cocreare", {}) if session else {}
    if not c:
        return {}
    return {
        "turn": c.get("turn", "-"),
        "active": bool(c.get("active")),
        "choice": c.get("choice"),
        "activities": c.get("activities") or [],
        "exercise": c.get("exercise") or "",
        "question": c.get("question") or "",
        "ai_use_tip": c.get("ai_use_tip") or "",
        "handoff_prompt": c.get("handoff_prompt") or "",
    }


def _append_vivere_history(session: Dict[str, Any], role: str, text: str, max_items: int = 10) -> None:
    v = session.get("vivere", {})
    hist: List[Dict[str, str]] = v.get("history", [])
    hist.append({"role": role, "text": (text or "").strip()})
    v["history"] = hist[-max_items:]


def _vivere_history_text(session: Dict[str, Any]) -> str:
    hist: List[Dict[str, str]] = session.get("vivere", {}).get("history", [])
    lines = []
    for item in hist:
        r = item.get("role", "")
        t = item.get("text", "")
        if not t:
            continue
        prefix = "UTENTE" if r == "user" else "VIVERE"
        lines.append(f"{prefix}: {t}")
    return "\n".join(lines).strip()


def _append_cocreare_history(session: Dict[str, Any], role: str, text: str, max_items: int = 10) -> None:
    c = session.get("cocreare", {})
    hist: List[Dict[str, str]] = c.get("history", [])
    hist.append({"role": role, "text": (text or "").strip()})
    c["history"] = hist[-max_items:]


def _cocreare_history_text(session: Dict[str, Any]) -> str:
    hist: List[Dict[str, str]] = session.get("cocreare", {}).get("history", [])
    lines = []
    for item in hist:
        role = item.get("role", "")
        text = item.get("text", "")
        if not text:
            continue
        prefix = "UTENTE" if role == "user" else "COCREARE"
        lines.append(f"{prefix}: {text}")
    return "\n".join(lines).strip()


def _provider(session: Optional[Dict[str, Any]] = None) -> str:
    if session:
        runtime = session.get("_runtime", {})
        if runtime.get("provider"):
            return runtime["provider"]
    return _default_provider()


def _ok_reply(text: str, session: Dict[str, Any], provider: str = "fallback", force_turn: Optional[str] = None) -> Dict[str, Any]:
    if force_turn:
        session["nm"]["turn"] = force_turn
    return {
        "reply": text,
        "mode": _current_mode(session),
        "provider": provider,
        "ok": True,
        "error": None,
        "nm": _nm_payload(session),
        "vivere": _vivere_payload(session),
        "cocreare": _cocreare_payload(session),
        "debug": _debug_payload(session),
    }


def _append_history(session: Dict[str, Any], role: str, text: str, max_items: int = 10) -> None:
    nm = session.get("nm", {})
    hist: List[Dict[str, str]] = nm.get("history", [])
    hist.append({"role": role, "text": (text or "").strip()})
    # keep last N
    nm["history"] = hist[-max_items:]


def _history_text(session: Dict[str, Any]) -> str:
    hist: List[Dict[str, str]] = session.get("nm", {}).get("history", [])
    lines = []
    for item in hist:
        r = item.get("role", "")
        t = item.get("text", "")
        if not t:
            continue
        prefix = "UTENTE" if r == "user" else "NM"
        lines.append(f"{prefix}: {t}")
    return "\n".join(lines).strip()


def _last_n_messages(session: Dict[str, Any], role: str, n: int) -> List[str]:
    hist: List[Dict[str, str]] = session.get("nm", {}).get("history", [])
    items = [item.get("text", "") for item in hist if item.get("role") == role]
    return [t for t in items if t][-n:]


# =========================
# NM actions
# =========================

def _handle_nm_action(action: str, session: Dict[str, Any]) -> Dict[str, Any]:
    nm = session["nm"]
    turn = nm["turn"]

    if action == "nm_reset":
        session.pop("nm", None)
        session.pop("vivere", None)
        session.pop("cocreare", None)
        _ensure_nm_session(session)
        return _ok_reply("NM resettato. Ripartiamo da T1.", session)

    # ---- T1 -> T2 ----
    if action == "nm_proceed_t2":
        if turn != "T1":
            return _ok_reply("Siamo già oltre T1: continuiamo dal turno corrente.", session)
        if nm.get("ii_count_t1", 0) < 3:
            return _ok_reply("Per passare a T2 servono almeno 3 messaggi di II in T1.", session)
        print("NM: entering T2 (nm_proceed_t2)")
        nm["t1_user_last2"] = _last_n_messages(session, role="user", n=2)
        nm["t1_ii_last2"] = _last_n_messages(session, role="assistant", n=2)
        nm["turn"] = "T2"
        nm["draft_description"] = _nm_t2_description(session)
        # T2 output: solo frase (mostrata come reply)
        return _ok_reply(nm["draft_description"], session, provider=_provider(session))

    # ---- T2 actions ----
    if action == "nm_modify_description":
        if turn != "T2":
            return _ok_reply("Questa azione vale in T2.", session)
        nm["draft_description"] = _nm_t2_description(session)
        return _ok_reply(nm["draft_description"], session, provider=_provider(session))

    if action == "nm_confirm_description":
        if turn != "T2":
            return _ok_reply("Questa conferma vale in T2.", session)
        if not nm.get("draft_description"):
            nm["draft_description"] = _nm_t2_description(session)

        nm["error_description"] = nm["draft_description"]
        nm["description_confirmed"] = True

        # move to T3 and propose name
        nm["turn"] = "T3"
        nm["draft_name"] = _nm_t3_name(session)

        return _ok_reply(nm["draft_name"], session, provider=_provider(session))

    if action == "nm_back_t1":
        if turn != "T2":
            return _ok_reply("Puoi tornare a T1 solo da T2.", session)
        nm["turn"] = "T1"
        nm["draft_description"] = None
        return _ok_reply("Ok. Restiamo in esplorazione (T1).", session)

    # ---- T3 actions ----
    if action == "nm_regen_name":
        if turn != "T3":
            return _ok_reply("Questa azione vale in T3.", session)
        if not nm.get("description_confirmed"):
            return _ok_reply("Manca la descrizione confermata: torna a T2.", session)
        nm["draft_name"] = _nm_t3_name(session)
        return _ok_reply(nm["draft_name"], session, provider=_provider(session))

    if action == "nm_confirm_name":
        if turn != "T3":
            return _ok_reply("Questa conferma vale in T3.", session)
        if not nm.get("draft_name"):
            nm["draft_name"] = _nm_t3_name(session)

        nm["error_name"] = _extract_error_name(nm["draft_name"])
        nm["name_confirmed"] = True
        nm["turn"] = "DONE"

        _ensure_vivere_session(session)
        session["vivere"]["active"] = True
        session["vivere"]["turn"] = "V1"
        session["vivere"]["error_name"] = nm.get("error_name") or ""
        session["vivere"]["error_description"] = nm.get("error_description") or ""
        session["vivere"]["choices"] = _vivere_generate_choices("", session)
        reply_text = _vivere_intro(session)
        _append_vivere_history(session, role="assistant", text=reply_text)
        return _ok_reply(reply_text, session, provider=_provider(session))

    if action == "nm_back_t2":
        if turn != "T3":
            return _ok_reply("Puoi tornare a T2 solo da T3.", session)
        nm["turn"] = "T2"
        nm["draft_name"] = None
        return _ok_reply("Ok. Torniamo alla descrizione (T2).", session)

    return _ok_reply("Azione non riconosciuta.", session)


# =========================
# VIVERE actions
# =========================

def _handle_vivere(message: str, action: str, session: Dict[str, Any]) -> Dict[str, Any]:
    _ensure_vivere_session(session)
    session.modified = True

    message = (message or "").strip()
    action = (action or "").strip()

    if action:
        return _handle_vivere_action(action, session)

    v = session["vivere"]
    turn = v.get("turn")
    if turn == "V2":
        # Compatibilita': il vecchio V2 non esiste piu nel nuovo flusso.
        v["turn"] = "V1"
        turn = "V1"

    if turn == "V1":
        if not message:
            return _ok_reply("Scrivimi qualcosa.", session)
        v["feeling_input"] = message
        reply_text = _vivere_reflection(message, session)
        if not (v.get("choices") and len(v.get("choices") or []) == 3):
            v["choices"] = _vivere_generate_choices(message, session)
        reply_text = f"{reply_text}\n\nCosa scegli di fare?\n{_vivere_choices_text(v)}"
        _append_vivere_history(session, role="user", text=message)
        _append_vivere_history(session, role="assistant", text=reply_text)
        return _ok_reply(reply_text, session, provider=_provider(session))

    if turn == "V3":
        if not message:
            return _ok_reply("Dammi un titolo anche breve.", session)
        final_card = _vivere_final_output(session, title=message)
        _append_vivere_history(session, role="user", text=message)
        _append_vivere_history(session, role="assistant", text=final_card)
        v["turn"] = "DONE"
        v["active"] = True
        session["nm"]["turn"] = "DONE"
        return _ok_reply(final_card, session, provider=_provider(session))

    if turn == "DONE":
        return _ok_reply("Percorso VIVERE completato. Premi Reset per ricominciare.", session)

    v["turn"] = "V1"
    return _ok_reply(_vivere_intro(session), session, provider=_provider(session))


def _handle_vivere_action(action: str, session: Dict[str, Any]) -> Dict[str, Any]:
    v = session["vivere"]
    turn = v.get("turn")

    if action == "vivere_reset":
        session.pop("vivere", None)
        return _ok_reply("VIVERE resettato.", session)

    if action in {"vivere_choice_1", "vivere_choice_2", "vivere_choice_3"}:
        if turn not in {"V1", "V2"}:
            return _ok_reply("Questa azione vale solo quando devi scegliere.", session)
        choice_id = action.split("_")[-1]
        v["choice"] = choice_id
        reply_text = _vivere_choice_scene(choice_id, session)
        _append_vivere_history(session, role="assistant", text=reply_text)
        v["turn"] = "V3"
        return _ok_reply(reply_text, session, provider=_provider(session))

    if action == "vivere_to_cocreare":
        if turn != "DONE":
            return _ok_reply("Puoi passare a CO-CREARE solo alla fine di VIVERE.", session)
        _ensure_cocreare_session(session)
        c = session["cocreare"]
        c["active"] = True
        c["turn"] = "C1"
        c["error_name"] = v.get("error_name") or ""
        c["error_description"] = v.get("error_description") or ""
        c["vivere_title"] = v.get("title") or ""
        c["activities"] = [label for _, label in COCREARE_CHOICES]
        v["active"] = False
        intro = _cocreare_intro(session)
        _append_cocreare_history(session, role="assistant", text=intro)
        return _ok_reply(intro, session, provider=_provider(session))

    if action == "vivere_title_help":
        if turn != "V3":
            return _ok_reply("Questo aiuto compare solo quando devi dare il titolo.", session)
        reply_text = _vivere_title_help(session)
        _append_vivere_history(session, role="assistant", text=reply_text)
        return _ok_reply(reply_text, session, provider=_provider(session))

    return _ok_reply("Azione non riconosciuta.", session)


def _vivere_choices_text(v: Optional[Dict[str, Any]] = None) -> str:
    choices = (v or {}).get("choices") or []
    if len(choices) == 3:
        return "\n".join([f"{idx + 1}. {text}" for idx, text in enumerate(choices)])
    return "\n".join([f"{cid}. {label}" for cid, label in VIVERE_CHOICES])


def _vivere_intro(session: Dict[str, Any]) -> str:
    v = session.get("vivere", {})
    error_name = (v.get("error_name") or "Errore").upper()
    return (
        "Sei in un laboratorio di scultura. Dopo mesi di lavoro, la tua statua e quasi perfetta. "
        f"Poi noti una crepa sul volto: {error_name}.\n"
        "Il tuo Maestro ti osserva in silenzio.\n\n"
        "Come decidi di affrontare questa crepa? Se preferisci, puoi anche scegliere subito una delle 3 azioni."
    )


def _vivere_reflection(message: str, session: Dict[str, Any]) -> str:
    if not _openai_configured():
        return _vivere_stub_reflection(message)

    v = session.get("vivere", {})
    user_prompt = _build_user_prompt(
        turn="V2",
        state={
            "error_name": v.get("error_name") or "",
            "error_description": v.get("error_description") or "",
        },
        constraints={"max_sentences": 2, "no_questions": True},
        user_input=message,
        history=_vivere_history_text(session),
        mode="VIVERE",
    )

    try:
        text, resp_id = _call_openai(
            SYSTEM_VIVERE_REFLECTION,
            user_prompt,
            previous_response_id=v.get("last_response_id"),
        )
        if resp_id:
            v["last_response_id"] = resp_id
        return _clean_text(text)
    except Exception:
        return _vivere_stub_reflection(message)


def _vivere_generate_choices(message: str, session: Dict[str, Any]) -> List[str]:
    if not _openai_configured():
        return [label for _, label in VIVERE_CHOICES]

    v = session.get("vivere", {})
    user_prompt = _build_user_prompt(
        turn="V2_CHOICES",
        state={
            "error_name": v.get("error_name") or "",
            "error_description": v.get("error_description") or "",
        },
        constraints={"json_only": True, "count": 3},
        user_input=message,
        history=_vivere_history_text(session),
        mode="VIVERE",
    )

    try:
        text, resp_id = _call_openai(
            SYSTEM_VIVERE_CHOICES,
            user_prompt,
            previous_response_id=v.get("last_response_id"),
        )
        if resp_id:
            v["last_response_id"] = resp_id
        data = _parse_json_response(text)
        choices = data.get("choices") if isinstance(data, dict) else None
        if isinstance(choices, list) and len(choices) == 3 and all(isinstance(c, str) for c in choices):
            return [c.strip() for c in choices]
    except Exception:
        pass

    return [label for _, label in VIVERE_CHOICES]


def _vivere_choice_scene(choice_id: str, session: Dict[str, Any]) -> str:
    v = session.get("vivere", {})
    choices = v.get("choices") or []
    choice_text = ""
    if choices and choice_id in {"1", "2", "3"}:
        idx = int(choice_id) - 1
        if 0 <= idx < len(choices):
            choice_text = choices[idx]

    if _openai_configured():
        response = _vivere_generate_scene(choice_text, session)
        if response:
            return response

    if choice_id == "1":
        response = (
            "Provi a contenere l'errore e a tenerlo fermo nella forma. "
            "Nel tuo linguaggio si sente il bisogno di rimettere subito ordine quando qualcosa sfugge. "
            "Qui affiora un filtro: se qualcosa si incrina, allora va corretto prima di essere guardato. "
            "Creative Twist: forse questa crepa non chiede controllo, ma una nuova lettura della forma. "
            "Se dovessi dare un titolo a questa scena, quale sarebbe?"
        )
    elif choice_id == "2":
        response = (
            "Rimani davanti alla crepa e la lasci esistere senza chiuderla subito. "
            "Nel tuo linguaggio si sente una disponibilita rara: restare nel punto fragile senza voltarti. "
            "Qui il pattern non e' il fallimento, ma la soglia tra difesa e ascolto. "
            "Creative Twist: quello che sembrava un difetto puo diventare il punto da cui entra visione. "
            "Se dovessi dare un titolo a questa scena, quale sarebbe?"
        )
    else:
        response = (
            "Scegli di rompere la forma e vedere cosa resta quando l'ordine cede. "
            "Nel tuo linguaggio si sente una spinta radicale: meglio aprire una frattura che restare in una forma falsa. "
            "Qui emerge un automatismo forte: per trasformare qualcosa, senti il bisogno di portarlo al limite. "
            "Creative Twist: la rottura puo diventare gesto creativo, non solo distruzione. "
            "Se dovessi dare un titolo a questa scena, quale sarebbe?"
        )
    return response


def _vivere_generate_scene(choice_text: str, session: Dict[str, Any]) -> str:
    v = session.get("vivere", {})
    user_prompt = _build_user_prompt(
        turn="V2_RESPONSE",
        state={
            "choice": choice_text,
            "error_name": v.get("error_name") or "",
            "error_description": v.get("error_description") or "",
            "feeling_input": v.get("feeling_input") or "",
        },
        constraints={"json_only": True},
        user_input=choice_text,
        history=_vivere_history_text(session),
        mode="VIVERE",
    )

    try:
        text, resp_id = _call_openai(
            SYSTEM_VIVERE_SCENE,
            user_prompt,
            previous_response_id=v.get("last_response_id"),
        )
        if resp_id:
            v["last_response_id"] = resp_id
        data = _parse_json_response(text)
        if isinstance(data, dict):
            response = (data.get("response") or "").strip()
            if response:
                return response
    except Exception:
        pass

    return ""


def _vivere_title_help(session: Dict[str, Any]) -> str:
    v = session.get("vivere", {})
    if not _openai_configured():
        return (
            "Prova a cercare l'immagine centrale della scena.\n"
            "La crepa che parla\n"
            "Dentro la forma\n"
            "Il punto che si apre"
        )

    user_prompt = _build_user_prompt(
        turn="V3_HELP",
        state={
            "error_name": v.get("error_name") or "",
            "error_description": v.get("error_description") or "",
            "choice": v.get("choice") or "",
            "selected_choice_text": ((v.get("choices") or [None, None, None])[int(v.get("choice") or 1) - 1] if str(v.get("choice") or "").isdigit() and 1 <= int(v.get("choice")) <= 3 else ""),
            "feeling_input": v.get("feeling_input") or "",
        },
        constraints={"max_lines": 4, "title_ideas": 3},
        user_input="Dammi uno spunto per il titolo.",
        history=_vivere_history_text(session),
        mode="VIVERE",
    )

    try:
        text, resp_id = _call_openai(
            SYSTEM_VIVERE_TITLE_HELP,
            user_prompt,
            previous_response_id=v.get("last_response_id"),
        )
        if resp_id:
            v["last_response_id"] = resp_id
        cleaned = _clean_text(text)
        if cleaned:
            return cleaned
    except Exception:
        pass

    return (
        "Prova a cercare l'immagine centrale della scena.\n"
        "La crepa che parla\n"
        "Dentro la forma\n"
        "Il punto che si apre"
    )


def _vivere_final_output(session: Dict[str, Any], title: str) -> str:
    v = session.get("vivere", {})
    title = (title or "").strip() or "Senza titolo"
    v["title"] = title

    if not _openai_configured():
        return _vivere_stub_final(v)

    state = {
        "error_name": v.get("error_name") or "",
        "error_description": v.get("error_description") or "",
        "choice": v.get("choice") or "",
        "title": title,
        "dialog": v.get("last_dialog") or "",
    }
    user_prompt = _build_user_prompt(
        turn="V4",
        state=state,
        constraints={"json_only": True},
        user_input=title,
        history=_vivere_history_text(session),
        mode="VIVERE",
    )

    try:
        text, resp_id = _call_openai(
            SYSTEM_VIVERE_FINAL,
            user_prompt,
            previous_response_id=v.get("last_response_id"),
        )
        if resp_id:
            v["last_response_id"] = resp_id
        data = _parse_json_response(text)
        if data:
            v["title"] = data.get("title") or title
            v["mantra"] = data.get("mantra") or ""
            v["epithet"] = data.get("epithet") or ""
            v["artwork"] = data.get("artwork") or ""
            v["card_image_prompt"] = _build_mirror_card_image_prompt(v)
            return _format_mirror_card(v)
    except Exception:
        pass

    return _vivere_stub_final(v)


def _parse_json_response(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


def _format_mirror_card(v: Dict[str, Any]) -> str:
    title = v.get("title") or "Senza titolo"
    mantra = v.get("mantra") or "Ogni crepa e' un respiro."
    epithet = v.get("epithet") or "Il Preciso, Viandante della Luce"
    artwork = v.get("artwork") or "Una scultura in marmo attraversata da un raggio di luce."
    return (
        f"Titolo: {title}\n"
        f"Mirror Card: \"{mantra}\"\n"
        f"{epithet}\n"
        f"Artwork: {artwork}"
    )


def _vivere_stub_reflection(message: str) -> str:
    msg = (message or "").strip()
    if msg:
        return f"Sento in te una crepa viva: {msg}."
    return "Sento in te una crepa viva che chiede ascolto."


def _vivere_stub_final(v: Dict[str, Any]) -> str:
    if not v.get("mantra"):
        v["mantra"] = "Ogni crepa e' un respiro."
    if not v.get("epithet"):
        v["epithet"] = "Il Preciso, Viandante della Luce"
    if not v.get("artwork"):
        v["artwork"] = "Una scultura in marmo attraversata da un raggio di luce."
    v["card_image_prompt"] = _build_mirror_card_image_prompt(v)
    return _format_mirror_card(v)


def _build_mirror_card_image_prompt(v: Dict[str, Any]) -> str:
    title = (v.get("title") or "Senza titolo").strip()
    mantra = (v.get("mantra") or "Ogni crepa e' un respiro.").strip()
    epithet = (v.get("epithet") or "Viandante").strip()
    artwork = (v.get("artwork") or "Scultura attraversata da luce.").strip()
    return (
        "Create a collectible monster-card style illustration, polished and iconic, "
        "single full-body character, centered, fantasy trading-card composition. "
        "Output for a square canvas and keep the full card safely inside frame: "
        "use about 85% of the canvas area, with clear margins on all sides. "
        "Include a decorative frame, rarity glow, elemental symbols, and three stat slots as visual panels. "
        "Do not include logos or copyrighted characters. "
        f"Card concept: {title}. Archetype: {epithet}. Emotional core: {mantra}. "
        f"Scene cues: {artwork}. "
        "Character represents the user as a symbolic hero-creature, high contrast lighting, "
        "clean silhouette, vivid details, premium card-art look."
    )


# =========================
# CO-CREARE actions
# =========================

def _handle_cocreare(message: str, action: str, session: Dict[str, Any]) -> Dict[str, Any]:
    _ensure_cocreare_session(session)
    session.modified = True

    message = (message or "").strip()
    action = (action or "").strip()
    c = session["cocreare"]
    turn = c.get("turn")

    if action == "cocreare_reset":
        session.pop("cocreare", None)
        return _ok_reply("CO-CREARE resettato.", session)

    if action in {"cocreare_choice_1", "cocreare_choice_2", "cocreare_choice_3"}:
        if turn != "C1":
            return _ok_reply("Questa scelta vale solo all'inizio di CO-CREARE.", session)
        choice_id = action.split("_")[-1]
        c["choice"] = choice_id
        reply = _cocreare_exercise(choice_id, session)
        c["turn"] = "C2"
        _append_cocreare_history(session, role="assistant", text=reply)
        return _ok_reply(reply, session, provider=_provider(session))

    if turn == "C1":
        return _ok_reply("Scegli una delle 3 attivita proposte.", session)

    if turn == "C2":
        if not message:
            return _ok_reply("Scrivi una risposta anche breve all'attivita.", session)
        reply = _cocreare_final_output(message, session)
        _append_cocreare_history(session, role="user", text=message)
        _append_cocreare_history(session, role="assistant", text=reply)
        c["turn"] = "DONE"
        c["active"] = False
        return _ok_reply(reply, session, provider=_provider(session))

    return _ok_reply("CO-CREARE completato. Premi Reset per ricominciare.", session)


def _cocreare_intro(session: Dict[str, Any]) -> str:
    c = session.get("cocreare", {})
    error_name = (c.get("error_name") or "ERRORE").upper()
    title = c.get("vivere_title") or "senza titolo"
    return (
        f"CO-CREARE. Ora prendiamo {error_name} e la storia \"{title}\" come materiale di lavoro.\n"
        "Ti propongo 3 micro attivita per usare l'AI in modo piu consapevole e antifragile.\n"
        "Qui l'AI serve a chiarire il problema, generare alternative e simulare passi possibili: la scelta resta tua."
    )


def _cocreare_exercise(choice_id: str, session: Dict[str, Any]) -> str:
    c = session.get("cocreare", {})
    activities = c.get("activities") or [label for _, label in COCREARE_CHOICES]
    choice_text = activities[int(choice_id) - 1] if choice_id in {"1", "2", "3"} else ""

    if _openai_configured():
        prompt = _build_user_prompt(
            turn="C2",
            state={
                "choice": choice_text,
                "error_name": c.get("error_name") or "",
                "error_description": c.get("error_description") or "",
                "vivere_title": c.get("vivere_title") or "",
            },
            constraints={"max_sentences": 5},
            user_input=choice_text,
            history=_cocreare_history_text(session),
            mode="COCREARE",
        )
        try:
            text, resp_id = _call_openai(
                SYSTEM_COCREARE_EXERCISE,
                prompt,
                previous_response_id=c.get("last_response_id"),
                model=OPENAI_COCREARE_MODEL,
            )
            if resp_id:
                c["last_response_id"] = resp_id
            cleaned = _clean_text(text)
            if cleaned:
                return cleaned
        except Exception:
            pass

    if choice_id == "1":
        return (
            "Capire il problema: prova a mettere a fuoco meglio la situazione che ti blocca o ti pesa. "
            "Puoi usare l'AI per descriverla con piu chiarezza, distinguerne le parti e vedere cosa la rende difficile. "
            "L'obiettivo non e' risolvere tutto subito, ma capire meglio che cosa hai davvero davanti. "
            "Quale problematica vorresti chiarire meglio con l'aiuto dell'AI?"
        )
    if choice_id == "2":
        return (
            "Cambiare prospettiva: prova a guardare la stessa situazione da un altro punto di vista. "
            "Puoi usare l'AI per riformulare il problema, cambiare cornice o immaginare una lettura diversa di quello che stai vivendo. "
            "L'obiettivo e' capire se cambia qualcosa quando cambia il modo in cui lo guardi. "
            "Quale situazione vorresti rileggere con uno sguardo diverso?"
        )
    return (
        "Trovare un uso utile: prova a trasformare questa difficolta in un punto da cui partire. "
        "Puoi usare l'AI per capire se dentro questo limite c'e un'indicazione utile, una regola, una risorsa o un modo diverso di agire. "
        "L'obiettivo e' vedere se da qualcosa che pesa puo nascere anche un orientamento. "
        "Quale difficolta vorresti provare a trasformare in qualcosa di utile?"
    )


def _cocreare_final_output(message: str, session: Dict[str, Any]) -> str:
    c = session.get("cocreare", {})
    if _openai_configured():
        prompt = _build_user_prompt(
            turn="C3",
            state={
                "choice": c.get("choice") or "",
                "activities": c.get("activities") or [],
                "error_name": c.get("error_name") or "",
                "vivere_title": c.get("vivere_title") or "",
            },
            constraints={"json_only": True},
            user_input=message,
            history=_cocreare_history_text(session),
            mode="COCREARE",
        )
        try:
            text, resp_id = _call_openai(
                SYSTEM_COCREARE_FINAL,
                prompt,
                previous_response_id=c.get("last_response_id"),
                model=OPENAI_COCREARE_MODEL,
            )
            if resp_id:
                c["last_response_id"] = resp_id
            data = _parse_json_response(text)
            if data:
                c["exercise"] = data.get("exercise") or ""
                c["question"] = data.get("question") or ""
                c["ai_use_tip"] = data.get("ai_use_tip") or ""
                c["handoff_prompt"] = data.get("handoff_prompt") or _build_cocreare_handoff_prompt(c)
                return _format_cocreare_output(c)
        except Exception:
            pass

    c["exercise"] = "Prova una versione piu piccola della stessa sfida e osserva cosa cambia."
    c["question"] = "Quale parte di questo errore puo diventare un metodo?"
    c["ai_use_tip"] = "Usa l'AI per generare alternative e confronti, non per decidere al posto tuo."
    c["handoff_prompt"] = _build_cocreare_handoff_prompt(c)
    return _format_cocreare_output(c)


def _format_cocreare_output(c: Dict[str, Any]) -> str:
    return (
        "CO-CREARE\n"
        f"Azione: {c.get('exercise') or ''}\n"
        f"Domanda: {c.get('question') or ''}\n"
        f"Uso consapevole dell'AI: {c.get('ai_use_tip') or ''}\n"
        "Ruolo dell'AI: supporto alla riflessione e alla progettazione, non sostituzione del tuo giudizio.\n\n"
        "Prompt pronto da copiare:"
    )


def _build_cocreare_handoff_prompt(c: Dict[str, Any]) -> str:
    title = (c.get("vivere_title") or "Senza titolo").strip()
    question = (c.get("question") or "").strip()
    ai_tip = (c.get("ai_use_tip") or "").strip()
    activity = ""
    choice = str(c.get("choice") or "")
    activities = c.get("activities") or []
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(activities):
            activity = activities[idx]
    activity = (activity or "affrontare meglio la situazione").strip()
    return (
        "Vorrei usare questa conversazione per riflettere in modo piu consapevole su una mia difficolta. "
        f"Sto lavorando sulla storia '{title}' e in questo momento vorrei {activity}. "
        f"Se utile, tieni presente questa domanda guida: {question}. "
        f"Aiutami seguendo questo principio: {ai_tip}. "
        "Rispondimi in modo breve e generale, con poche domande o spunti semplici, senza darmi subito un piano dettagliato o soluzioni troppo specifiche."
    )

# =========================
# Turn implementations (OpenAI + soft postprocess)
# =========================

def _nm_t1_reply(message: str, session: Dict[str, Any]) -> str:
    if not _openai_configured():
        _set_runtime_status(session, provider="fallback", fallback_reason="missing_openai_api_key")
        return _nm_t1_stub_reply(message)

    user_prompt = _build_user_prompt(
        turn="T1",
        state={},
        constraints={"max_sentences": 2, "one_question": True, "no_solutions": True},
        user_input=message,
        history=_history_text(session),
    )

    try:
        text, resp_id = _call_openai(
            SYSTEM_NM_T1,
            user_prompt,
            previous_response_id=session.get("nm", {}).get("last_response_id"),
        )
        if resp_id:
            session["nm"]["last_response_id"] = resp_id
        _set_runtime_status(session, provider="openai")
        reply = _clean_text(text)
    except Exception as e:
        _set_runtime_status(session, provider="fallback", fallback_reason="openai_request_failed", error=str(e))
        reply = _nm_t1_stub_reply(message)

    return reply


def _nm_t2_description(session: Dict[str, Any], hint: str = "") -> str:
    if not _openai_configured():
        _set_runtime_status(session, provider="fallback", fallback_reason="missing_openai_api_key")
        return _nm_t2_stub_description(session)

    nm = session.get("nm", {})
    t1_user_last2 = nm.get("t1_user_last2") or []
    t1_ii_last2 = nm.get("t1_ii_last2") or []
    if t1_user_last2 or t1_ii_last2:
        lines = []
        for text in t1_user_last2:
            lines.append(f"UTENTE: {text}")
        for text in t1_ii_last2:
            lines.append(f"NM: {text}")
        history = "\n".join(lines).strip()
    else:
        history = _history_text(session)

    user_prompt = _build_user_prompt(
        turn="T2",
        state={"t1_user_last2": t1_user_last2, "t1_ii_last2": t1_ii_last2},
        constraints={"one_sentence_only": True},
        user_input=hint,
        history=history,
    )

    try:
        text, resp_id = _call_openai(
            SYSTEM_NM_T2,
            user_prompt,
            previous_response_id=session.get("nm", {}).get("last_response_id"),
        )
        if resp_id:
            session["nm"]["last_response_id"] = resp_id
        _set_runtime_status(session, provider="openai")
        description = _force_one_sentence(_clean_text(text))
        return f"{description}\n\nTi rivedi in questa descrizione?"
    except Exception as e:
        _set_runtime_status(session, provider="fallback", fallback_reason="openai_request_failed", error=str(e))
        return _nm_t2_stub_description(session)


def _nm_t3_name(session: Dict[str, Any], hint: str = "") -> str:
    nm = session.get("nm", {})
    desc = (nm.get("error_description") or "").strip()

    if not _openai_configured():
        _set_runtime_status(session, provider="fallback", fallback_reason="missing_openai_api_key")
        return _nm_t3_stub_name(session)

    user_prompt = _build_user_prompt(
        turn="T3",
        state={"description_confirmed": True, "error_description": desc},
        constraints={"max_sentences": 2},
        user_input=hint,
        history=_history_text(session),
    )

    try:
        text, resp_id = _call_openai(
            SYSTEM_NM_T3,
            user_prompt,
            previous_response_id=session.get("nm", {}).get("last_response_id"),
        )
        if resp_id:
            session["nm"]["last_response_id"] = resp_id
        _set_runtime_status(session, provider="openai")
        return _clean_text(text)
    except Exception as e:
        _set_runtime_status(session, provider="fallback", fallback_reason="openai_request_failed", error=str(e))
        return _nm_t3_stub_name(session)


def _extract_error_name(text: str) -> str:
    if not text:
        return ""
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if not line:
        return ""
    match = re.search(r"(?i)chiamarlo\s+([A-Za-zÀ-ÖØ-öø-ÿ]+)", line)
    if match:
        return match.group(1)
    match = re.search(r"[\"“”']\s*([A-Za-zÀ-ÖØ-öø-ÿ]+)\s*[\"“”']", line)
    if match:
        return match.group(1)
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", line)
    if words:
        return words[-1]
    return line.strip()


def _build_user_prompt(
    turn: str,
    state: Dict[str, Any],
    constraints: Dict[str, Any],
    user_input: str,
    history: str,
    mode: str = "NM",
) -> str:
    return f"""[II_CONTEXT]
mode={mode}
turn={turn}
state={state}
constraints={constraints}

[USER_INPUT]
{user_input}

[HISTORY]
{history}
""".strip()


def _clean_text(text: str) -> str:
    t = (text or "").strip()
    if "[II_CONTEXT]" in t:
        t = re.sub(r"(?s)\[II_CONTEXT\].*?(?=\n\n|\Z)", "", t).strip()
    if "[USER_INPUT]" in t:
        t = re.sub(r"(?s)\[USER_INPUT\].*?(?=\n\n|\Z)", "", t).strip()
    if "[HISTORY]" in t:
        t = re.sub(r"(?s)\[HISTORY\].*?(?=\n\n|\Z)", "", t).strip()
    # remove surrounding quotes
    t = t.strip("“”\"' ")
    # collapse whitespace
    t = re.sub(r"\s+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _force_one_sentence(text: str) -> str:   # sicurezza extra dopo la risposta
    """
    Applicazione morbida della regola: prendi la prima riga non vuota
    e, se ci sono piu frasi, tieni solo la prima.
    """
    if not text:
        return text
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if not first_line:
        return text.strip()

    # If multiple sentences, keep first by punctuation.
    m = re.search(r"^(.+?[.!?])\s", first_line)
    if m:
        return m.group(1).strip()

    return first_line.strip()


# def _force_one_word(text: str) -> str:
#     """
#     Applicazione morbida della regola: prendi la prima parola
#     e rimuovi punteggiatura e spazi extra.
#     """
#     if not text:
#         return text
    # take first line, then first token
#     line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
#     token = (line.split()[:1] or [""])[0]
# 
    # remove punctuation except accented letters
#     token = re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ]+", "", token)
#     return token or "Nòmo"
# 
# 
# =========================
# Fallback stubs
# =========================
# 
def _nm_t1_stub_reply(message: str) -> str:
    msg = (message or "").strip()
    if msg.lower() in {"ciao", "hey", "hello", "salve"}:
        return ("Ti trovi nella Città Perfetta: oggi che piccola incrinatura vedi nello specchio?\n"
                "In quale momento concreto la noti di più?")
    return f"Sembra che tu stia portando questo: “{msg}”. In quale momento concreto lo noti di più?"


def _nm_t2_stub_description(session: Dict[str, Any]) -> str:
    return "Quando qualcosa conta, ti irrigidisci e perdi fluidità."


def _nm_t3_stub_name(session: Dict[str, Any]) -> str:
    return (
        "Rigido\n\n"
        "Se il nome del tuo errore e' questo, premi il tasto Conferma nome. "
        "Se vuoi un altro nome, premi Rigenera e poi dimmi come lo vorresti."
    )
