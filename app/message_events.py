"""Silent HTTP event stream for Agents: no heartbeat frames or idle DB polling."""

import asyncio
import base64
import json
import time
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse


def install_message_events(app, *, get_db, require_user, require_membership,
                           fetch_messages, room_event, user_by_id):
    @app.get("/api/rooms/{room_name}/message-events")
    async def message_events(
        room_name: str,
        request: Request,
        user: Annotated[dict, Depends(require_user)],
        after_id: Annotated[int, Query(alias="afterId", ge=0)],
    ):
        authorization = request.headers.get("authorization")
        with get_db() as conn:
            room, _ = require_membership(conn, room_name, user["id"])
            room_id = room["id"]
        # require_user already verified this token's signature and expiry. Use the
        # expiry only for a single local timer, not for network heartbeat traffic.
        body = authorization.removeprefix("Bearer ").strip().split(".", 1)[0]
        expiry = int(json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))["exp"])

        async def events():
            cursor = after_id
            pending = set()
            while True:
                # Subscribe before taking the snapshot: an event between snapshot
                # and await must not disappear or strand a newly arrived message.
                ev = room_event(room_id)
                ready = set()
                try:
                    current = require_user(authorization)
                    with get_db() as conn:
                        room, member = require_membership(conn, room_name, current["id"])
                        if room["id"] != room_id:
                            raise HTTPException(status_code=410, detail="Room changed")

                        def consider(row):
                            mid = int(row["id"])
                            if row.get("_blanked") or row.get("recalled") or row["user_id"] == current["id"]:
                                pending.discard(mid)
                                return
                            author = user_by_id(conn, row["user_id"])
                            if not author or author["kind"] != "human":
                                pending.discard(mid)
                                return
                            if row.get("streaming"):
                                pending.add(mid)
                            else:
                                pending.discard(mid)
                                ready.add(mid)

                        # Revisit unfinished streams even after newer messages have
                        # advanced the notification cursor. Completion keeps its ID.
                        waiting = sorted(pending)
                        for offset in range(0, len(waiting), 200):
                            for row in fetch_messages(conn, room, member, current["id"], 200, cursor,
                                                      stream_ids=waiting[offset:offset + 200]):
                                if int(row["id"]) <= cursor:
                                    consider(row)
                        while True:
                            rows = fetch_messages(conn, room, member, current["id"], 200, cursor)
                            for row in rows:
                                cursor = max(cursor, int(row["id"]))
                                consider(row)
                            if len(rows) < 200:
                                break
                except HTTPException as exc:
                    yield 'event: stop\ndata: ' + json.dumps({"status": exc.status_code}) + '\n\n'
                    return
                if ready:
                    # IDs only: inbox remains the sole reader of message contents
                    # and the sole owner of the durable processing watermark.
                    yield 'event: messages\ndata: ' + json.dumps({"messageIds": sorted(ready)}) + '\n\n'
                remaining = expiry - time.time()
                if remaining <= 0:
                    yield 'event: stop\ndata: {"status":401}\n\n'
                    return
                try:
                    await asyncio.wait_for(ev.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    yield 'event: stop\ndata: {"status":401}\n\n'
                    return

        return StreamingResponse(events(), media_type="text/event-stream", headers={
            "Cache-Control": "no-store", "X-Accel-Buffering": "no",
        })

    return message_events
