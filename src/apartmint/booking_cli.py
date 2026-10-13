"""Human-reviewed tour proposals; no generic real-provider submission endpoint."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys

from .config import SafeError, Settings


def _read_local(path: Path, *, private: bool) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor) as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                raise ValueError
            if private and info.st_mode & 0o077:
                raise SafeError("Contact file must be private: run chmod 600 on that file")
            text = handle.read(65537)
            if len(text) > 65536:
                raise ValueError
            return text
    except SafeError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise SafeError("Input file must be a readable regular file of at most 65536 bytes") from None


def _json_file(path: Path, *, private: bool = False) -> dict:
    try:
        value = json.loads(_read_local(path, private=private))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, RecursionError):
        raise SafeError("Input file must contain a JSON object") from None


def _env_contact(path: Path, variables: dict[str, str]) -> dict:
    """Read only named scalar assignments. No sourcing, interpolation, or execution."""
    if len(set(variables.values())) != 3 or any(not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name) for name in variables.values()):
        raise SafeError("Provide three distinct valid contact variable names")
    values = {}
    for line in _read_local(path, private=True).splitlines():
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.*)$", line)
        if not match or match[1] not in variables.values():
            continue
        if match[1] in values:
            raise SafeError("Contact variables must have one assignment each")
        try:
            parts = shlex.split(match[2], comments=True, posix=True)
            if len(parts) != 1 or "$" in parts[0] or "`" in parts[0]:
                raise ValueError
            values[match[1]] = parts[0]
        except ValueError:
            raise SafeError("Contact variables must be literal scalar values; quote values containing spaces") from None
    if set(values) != set(variables.values()):
        raise SafeError("One or more named contact variables is missing")
    return {field: values[name] for field, name in variables.items()}


def requirements() -> dict:
    return {
        "real_provider_submission_available": False,
        "needed_from_user": [
            "City/area and rental filters, or a selected listing URL",
            "Acceptable dates/time windows, timezone, and guided/video/self-guided tour preference",
            "Genuine name, email, and phone in a private local JSON or named environment-file fields",
            "Required screening answers and provider login/SMS/identity verification in the provider UI",
            "Approval of the exact property, offered slot, contact fields, and material terms",
        ],
        "needed_from_provider": [
            "Verified property identity and official scheduling URL",
            "Live offered slot and timezone; a tour request is not a confirmed appointment",
            "Authorized API access or a supported browser flow with user handoff",
            "A provider-readable receipt and a way to reconcile an uncertain submission",
        ],
        "privacy": "Inference stays local; booking necessarily sends the approved contact fields to the selected provider. No contacts are sent by these preparation commands.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apartmint tour", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("requirements", help="Show information required for a real tour")
    commands.add_parser("approval-demo", help="Exercise approval/receipt flow against a local synthetic provider")
    prepare = commands.add_parser("prepare", help="Save an immutable local proposal; no booking or contact transmission")
    prepare.add_argument("--offer", type=Path, required=True, help="Public offer JSON including verified property and offered slot")
    contacts = prepare.add_mutually_exclusive_group(required=True)
    contacts.add_argument("--contact", type=Path, help="Private JSON with name, email, phone")
    contacts.add_argument("--contact-env", type=Path, help="Private environment file; never executed or sent to a model")
    prepare.add_argument("--name-var", default="APARTMINT_TOUR_NAME")
    prepare.add_argument("--email-var", default="APARTMINT_TOUR_EMAIL")
    prepare.add_argument("--phone-var", default="APARTMINT_TOUR_PHONE")
    prepare.add_argument("--expires-at", required=True, help="Approval expiry, ISO timestamp with offset")
    for name in ("show", "approve", "reject", "revoke", "handoff", "submit", "reconcile"):
        command = commands.add_parser(name)
        command.add_argument("proposal_id")
        if name in ("approve", "reject", "revoke"):
            command.add_argument("--digest", required=True, help="Exact digest of the reviewed proposal")
    args = parser.parse_args(argv)
    try:
        from .booking import BookingOffer, BookingStore
        settings = Settings.from_env()
        if args.command == "requirements":
            result = requirements()
        elif args.command == "approval-demo":
            from .booking_acceptance import run_approval_demo
            result = run_approval_demo(settings.data_dir)
        else:
            store = BookingStore(settings.data_dir / "tours")
            if args.command == "prepare":
                offer_data = _json_file(args.offer)
                contact = (_json_file(args.contact, private=True) if args.contact else
                           _env_contact(args.contact_env, {"name": args.name_var, "email": args.email_var, "phone": args.phone_var}))
                try:
                    offer = BookingOffer(**offer_data)
                except TypeError:
                    raise SafeError("Offer fields must match config/tour-offer.example.json") from None
                result = store.propose(offer, contact=contact, expires_at=args.expires_at)
            elif args.command in ("approve", "reject", "revoke"):
                result = getattr(store, args.command)(args.proposal_id, args.digest)
            else:
                result = store.status(args.proposal_id)
                if args.command in ("submit", "reconcile"):
                    raise SafeError("No real provider adapter is registered; use the official provider flow. An approval alone is not a booking")
                if args.command == "handoff":
                    if result["state"] != "approved" or datetime.fromisoformat(result["expires_at"]) <= datetime.now(timezone.utc):
                        raise SafeError("Review and approve an unexpired proposal before provider handoff")
                    result = dict(result, next_action="Complete the official provider flow; re-review if any property, time, contact, screening answer, or term changes",
                                  real_appointment_booked=False, provider_verified=False,
                                  submission_mode="manual_provider_handoff", automatic_submission=False)
            result = dict(result, real_appointment_booked=False, real_provider_adapter_available=False)
        print(json.dumps(result, sort_keys=True))
        return 0
    except SafeError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"error": "Tour operation failed; no private contact or provider payload was logged"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
