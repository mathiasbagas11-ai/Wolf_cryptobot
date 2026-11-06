"""Telegram long-polling command listener.

Runs a daemon thread that polls ``getUpdates`` and dispatches ``/commands`` to a
:class:`~wolf.notify.commands.CommandRouter`, replying in the same chat/topic.
Long-polling needs no public webhook URL, so it works unchanged on Railway, a
laptop, or CI. Only messages from allowed chats are honoured. Network and decode
errors are caught and backed off — a Telegram hiccup must never kill the thread.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

from wolf.notify.commands import CommandRouter

log = logging.getLogger("wolf.poller")

#: Store key for evidence that another process is polling this bot token.
CONFLICT_KEY = "telegram_poll_conflict"


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
        allowed = set(tg.allowed_chat_ids)
        if tg.chat_id:
            allowed.add(str(tg.chat_id))
        self._allowed = allowed
        self._conflicts = 0
        self._conflict_saved = 0.0

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
                if resp.status_code == 409:
                    self._record_conflict()
                    time.sleep(3)
                    continue
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

    def _record_conflict(self) -> None:
        """Note that Telegram refused this poll because another one is live.

        Telegram serves ``getUpdates`` to one poller per token and answers any
        other with 409. A second copy of the bot running on the same token
        therefore shows up here, and nowhere else: it posts to the same chat,
        and a command typed there is answered by whichever copy won that poll.
        That copy was found once only because its daily cards disagreed about a
        gate setting. The count goes to the store so the diag card can say so,
        at most once a minute.
        """
        self._conflicts += 1
        now = time.time()
        if now - self._conflict_saved < 60:
            return
        self._conflict_saved = now
        log.warning("Telegram getUpdates 409: another process is polling this bot token "
                    "(%d conflicts since start)", self._conflicts)
        store = getattr(self._app, "store", None)
        if store is None:
            return
        from datetime import datetime, timezone

        stamp = datetime.now(timezone.utc).isoformat()
        prev = store.read(CONFLICT_KEY, default={}) or {}
        store.write(CONFLICT_KEY, {
            "first_at": prev.get("first_at") or stamp,
            "last_at": stamp,
            "count": int(prev.get("count") or 0) + self._conflicts,
        })
        self._conflicts = 0

    def _dispatch(self, message: dict) -> None:
        text = message.get("text", "")
        if not text.startswith("/") and not text.strip().isalnum():
            return
        chat_id = str(message.get("chat", {}).get("id", ""))
        if self._allowed and chat_id not in self._allowed:
            log.debug("Ignoring command from unauthorised chat %s", chat_id)
            return
        thread_id = str(message.get("message_thread_id", "") or "")
        try:
            reply = self._router.handle(text)
        except Exception:
            log.exception("Command handler failed for %r", text)
            reply = "⚠️ Sorry, that command failed."
        if reply:
            self._app.notifier.send_raw(chat_id, reply, thread_id)
