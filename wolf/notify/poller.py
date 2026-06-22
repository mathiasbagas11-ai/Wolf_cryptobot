"""Telegram long-polling command listener.

Runs a daemon thread that polls ``getUpdates`` and dispatches ``/commands`` to a
:class:`~wolf.notify.commands.CommandRouter`, replying in the same chat/topic.
Long-polling needs no public webhook URL, so it works unchanged on Railway, a
laptop, or CI. Only messages from allowed chats are honoured.

Network and decoding errors are caught and backed off — a Telegram hiccup must
never kill the listener thread (or the worker).
"""

from __future__ import annotations

import logging
import threading
import time

import requests

from wolf.notify.commands import CommandRouter

log = logging.getLogger("wolf.poller")


class TelegramPoller:
    def __init__(self, app, router: CommandRouter | None = None) -> None:
        self._app = app
        self._router = router or CommandRouter(app)
        tg = app.settings.telegram
        self._token = tg.bot_token
        self._session = requests.Session()
        self._offset: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Restrict to explicitly-allowed chats, falling back to the main chat id.
        allowed = set(tg.allowed_chat_ids)
        if tg.chat_id:
            allowed.add(str(tg.chat_id))
        self._allowed = allowed

    def start(self) -> None:
        if not self._token:
            log.info("Telegram token missing; command listener not started")
            return
        self._thread = threading.Thread(target=self._run, name="tg-poller", daemon=True)
        self._thread.start()
        log.info("Telegram command listener started")

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        url = f"https://api.telegram.org/bot{self._token}/getUpdates"
        while not self._stop.is_set():
            try:
                params = {"timeout": 30, "allowed_updates": '["message"]'}
                if self._offset is not None:
                    params["offset"] = self._offset
                resp = self._session.get(url, params=params, timeout=40)
                resp.raise_for_status()
                for update in resp.json().get("result", []):
                    self._offset = update["update_id"] + 1
                    self._dispatch(update.get("message") or {})
            except requests.RequestException as exc:
                log.debug("getUpdates error: %s", exc)
                time.sleep(3)
            except (ValueError, KeyError, TypeError) as exc:
                log.debug("getUpdates decode error: %s", exc)
                time.sleep(3)

    def _dispatch(self, message: dict) -> None:
        text = message.get("text", "")
        if not text.startswith("/") and not text.strip().isalnum():
            return
        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))
        if self._allowed and chat_id not in self._allowed:
            log.debug("Ignoring command from unauthorised chat %s", chat_id)
            return
        thread_id = str(message.get("message_thread_id", "") or "")
        try:
            reply = self._router.handle(text)
        except Exception:  # a bad command must never kill the listener
            log.exception("Command handler failed for %r", text)
            reply = "⚠️ Sorry, that command failed."
        if reply:
            self._app.notifier.send_raw(chat_id, reply, thread_id)
