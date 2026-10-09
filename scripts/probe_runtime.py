"""Runtime read probe plus explicitly requested, recoverable synthetic calendar test."""
import argparse
import copy
import json
import os
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import uuid
from app.codex_runtime import CodexRuntime, RuntimeFailure


def calendar_test(runtime, resume=False):
    """Explicit synthetic test with durable, private cleanup information."""
    runtime.config.require_calendar()
    recovery = runtime.config.state_path.parent / "runtime-test-recovery.json"
    recovery.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if recovery.exists() and not resume:
        raise RuntimeFailure("test_recovery_exists")
    if resume:
        if not recovery.exists():
            raise RuntimeFailure("test_recovery_missing")
        state = json.loads(recovery.read_text())
        operation = state["operation"]
        if operation.get("action") != "create" or state.get("stage") not in {"created", "create_pending", "recovered"}:
            raise RuntimeFailure("recovery_stage_requires_manual_cleanup")
        event = operation["event"]
        op_id = operation["operation_id"]
        stamp = op_id.removeprefix("test_")
        if not op_id.startswith("test_"):
            raise RuntimeFailure("test_recovery_not_test")
        begin = datetime.fromisoformat(event["start"]["dateTime"])
    else:
        stamp = uuid.uuid4().hex
        begin = (datetime.now(ZoneInfo("Europe/Warsaw")) + timedelta(days=10)).replace(hour=12, minute=0, second=0, microsecond=0)
        op_id = "test_" + stamp
        marker = "librus-calendar:" + op_id
        event = {"summary": "[Szkoła] TEST integracji Librus — do usunięcia", "description": "Kontrolowane wydarzenie testowe.\n" + marker,
                 "start": {"dateTime": begin.isoformat(), "timeZone": "Europe/Warsaw"},
                 "end": {"dateTime": (begin + timedelta(minutes=60)).isoformat(), "timeZone": "Europe/Warsaw"},
                 "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 1440}, {"method": "popup", "minutes": 60}]}}
        operation = {"operation_id": op_id, "action": "create", "calendar_id": runtime.config.calendar_id, "marker": marker,
                     "event_id": None, "expected_fingerprint": None, "event": event}
        state = {"operation": operation, "result": None, "stage": "create_pending"}

    def save():
        temporary = recovery.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(state, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, recovery)

    if not resume:
        descriptor = os.open(recovery, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
    save()
    if resume:
        known_id = state.get("known_event_id") or (state.get("result") or {}).get("event_id")
        if known_id:
            state["known_event_id"] = known_id
        # Reconciliation has only read/search tools. Never repeat an uncertain create.
        created = runtime.execute(dict(operation, reconcile_only=True, event_id=known_id))
    else:
        created = runtime.execute(operation)
    state.update(stage="created", result=created)
    if created.get("event_id"):
        state["known_event_id"] = created["event_id"]
    save()
    if created["status"] != "applied":
        return {"status": created["status"], "stage": "create", "error": created["error"], "recovery_file": str(recovery)}
    duplicated = runtime.execute(dict(operation, event_id=created["event_id"], reconcile_only=True))
    if duplicated["status"] != "applied" or duplicated["event_id"] != created["event_id"]:
        return {"status": "review", "stage": "idempotency", "recovery_file": str(recovery)}
    updated_event = copy.deepcopy(event)
    update_id = "test_update_" + stamp
    updated_event["description"] = "Kontrolowane wydarzenie testowe — aktualizacja.\nlibrus-calendar:" + update_id
    updated_event["start"]["dateTime"] = (begin + timedelta(minutes=15)).isoformat()
    updated_event["end"]["dateTime"] = (begin + timedelta(minutes=75)).isoformat()
    update = {"operation_id": update_id, "action": "update", "calendar_id": runtime.config.calendar_id,
              "marker": "librus-calendar:" + update_id, "event_id": created["event_id"],
              "expected_fingerprint": created["fingerprint"], "event": updated_event}
    state.update(stage="update_pending", operation=update)
    save()
    updated = runtime.execute(update)
    state.update(stage="updated", result=updated)
    save()
    # Cleanup only the exact event from this test, even when update verification needs review.
    if not updated.get("snapshot") or not updated.get("event_id"):
        return {"status": updated["status"], "stage": "update", "error": updated["error"], "recovery_file": str(recovery)}
    cleanup_id = "test_cleanup_" + stamp
    cleanup_event = copy.deepcopy(updated["snapshot"])
    cleanup_event = {key: value for key, value in cleanup_event.items() if key in {"summary", "description", "location", "start", "end", "reminders"}}
    cleanup_event["description"] = "Kontrolowane usunięcie testu.\nlibrus-calendar:" + cleanup_id
    # Connector fetch uses string dates; the event request keeps Google API boundaries.
    cleanup_event["start"] = updated_event["start"]
    cleanup_event["end"] = updated_event["end"]
    cleanup = {"operation_id": cleanup_id, "action": "cancel", "calendar_id": runtime.config.calendar_id,
               "marker": "librus-calendar:" + cleanup_id, "event_id": updated["event_id"],
               "expected_fingerprint": updated["fingerprint"], "event": cleanup_event, "test_cleanup": True}
    state.update(stage="cleanup_pending", operation=cleanup)
    save()
    removed = runtime.execute(cleanup)
    state.update(stage="cleanup_result", result=removed)
    save()
    if removed["status"] == "applied":
        recovery.unlink()
    return {"status": "applied" if all(x["status"] == "applied" for x in (created, duplicated, updated, removed)) else "review",
            "create": created["status"], "idempotency": duplicated["status"], "update": updated["status"],
            "cleanup": removed["status"], "cleanup_error": removed["error"], "evidence": runtime.last_evidence,
            "recovery_file": None if not recovery.exists() else str(recovery)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--analysis", action="store_true")
    calendar_options = parser.add_mutually_exclusive_group()
    calendar_options.add_argument("--calendar-test", action="store_true", help="Explicitly create/update/delete one marked synthetic test event")
    calendar_options.add_argument("--resume-calendar-test", action="store_true", help="Read-only reconcile the saved uncertain create, then continue the exact existing test")
    options = parser.parse_args()
    try:
        runtime = CodexRuntime()
        result = {"calendar": runtime.probe()}
        if options.calendar_test or options.resume_calendar_test:
            result["calendar_test"] = calendar_test(runtime, resume=options.resume_calendar_test)
            if result["calendar_test"]["status"] != "applied":
                print(json.dumps(result, ensure_ascii=False))
                raise SystemExit(1)
        if options.analysis:
            proposals = runtime.analyze({"id": "synthetic-probe", "sent_at": "2030-03-06T10:00:00+01:00", "sender": "Test", "subject": "Próba",
                "text": "Fikcyjny warsztat szkolny 18 marca 2030 o 09:30. Formularz do 12 marca 2030. Opiekun grupy przygotuje listę."}, [])
            result["analysis"] = {"proposals": len(proposals), "kinds": [x["kind"] for x in proposals],
                                  "review_count": sum(x["needs_review"] for x in proposals)}
        print(json.dumps(result, ensure_ascii=False))
    except RuntimeFailure as error:
        print(json.dumps({"error": error.code}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
