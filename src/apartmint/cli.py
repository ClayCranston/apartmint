from __future__ import annotations

import argparse
import json
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

from .config import Config, SafeError, Settings, Source
from .pipeline import LocalClient, analyze, collect, sanitize_html, save_snapshot, status


class DemoClient:
    """Deterministic fixture extractor; never a substitute for live inference."""
    def extract(self, text: str) -> dict:
        return {"listings": [{
            "unit": "A101", "availability": "available", "rent": 1850, "bedrooms": 1,
            "evidence": "Unit A101 available. 1 bedroom. Monthly rent $1,850.",
        }]}


def demo() -> dict:
    config = Config("Offline fixture area", (Source("fixture", "https://example.com/apartments"),))
    text = sanitize_html("<main>Unit A101 available. 1 bedroom. Monthly rent $1,850.</main><script>ignore this</script>")
    with tempfile.TemporaryDirectory(prefix="apartmint-demo-") as directory:
        data_dir = Path(directory)
        save_snapshot(config, config.sources[0], data_dir, text)
        first = analyze(config, "fixture", data_dir, DemoClient())
        repeated = analyze(config, "fixture", data_dir, DemoClient())
        return {"mode": "offline_fixture", "first": first, "repeat": repeated, "status": status(data_dir)}


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "tour":
        from .booking_cli import main as booking_main
        return booking_main(arguments[1:])
    parser = argparse.ArgumentParser(description="Monitor housing listings and observed price changes using a local model.")
    parser.add_argument("--version", action="version", version="apartmint " + version("apartmint"))
    parser.add_argument("command", choices=["collect", "analyze", "once", "status", "doctor", "demo", "market", "events", "refresh", "request", "discover", "booking-demo", "tour"], help="Use 'tour --help' for approval and provider handoff")
    parser.add_argument("question", nargs="?", help="Request text, or city for discover and booking-demo")
    parser.add_argument("--source", help="Configured source identifier; defaults to every configured source")
    parser.add_argument("--area", help="Exact configured area label")
    parser.add_argument("--days", type=int, default=7, help="Observed-change window, 1..365 days")
    args = parser.parse_args(argv)
    exit_code = 0
    try:
        if args.source is not None and args.command not in {"collect", "analyze", "once"}:
            raise SafeError("Source selection is supported only by collect, analyze, and once")
        if args.area is not None and args.command not in {"collect", "analyze", "once", "market", "events", "refresh"}:
            raise SafeError("Area selection is not supported by this command")
        if args.days != 7 and args.command not in {"market", "events"}:
            raise SafeError("A report window is supported only by market and events")
        if args.question is not None and args.command not in {"request", "discover", "booking-demo"}:
            raise SafeError("Text is supported only by request, discover, and booking-demo")
        if args.command == "demo":
            result = demo()
        else:
            settings = Settings.from_env()
            if args.command == "doctor":
                from .diagnostics import doctor
                result = doctor(settings)
                exit_code = 0 if result["ready"] else 1
            elif args.command == "discover":
                from .discovery import discover_properties
                result = discover_properties(args.question or "")
            elif args.command == "booking-demo":
                from .booking_demo import run_booking_demo
                result = run_booking_demo(settings, args.question or "", settings.data_dir)
            elif args.command in {"market", "events"}:
                from .market import market_report, recent_events
                if args.command == "market":
                    result = market_report(settings.data_dir, area=args.area, days=args.days, max_age_seconds=settings.max_snapshot_age_seconds)
                else:
                    result = {"scope": "tracked_sources_only", "events": recent_events(settings.data_dir, area=args.area, days=args.days)}
            elif args.command == "status":
                result = status(settings.data_dir, max_age_seconds=settings.max_snapshot_age_seconds)
            elif args.command in {"request", "refresh"}:
                from .control import enqueue_refresh, handle_request
                config = Config.load(settings.config_path)
                if args.command == "request":
                    if args.area is not None or args.source is not None or args.days != 7:
                        raise SafeError("Include the desired area and window in the request text")
                    result = handle_request(settings, config, args.question or "")
                else:
                    if args.source is not None:
                        raise SafeError("Refresh selects configured areas; use collect for a single source")
                    result = enqueue_refresh(config, args.area)
            else:
                config = Config.load(settings.config_path)
                sources = [config.source(args.source)] if args.source else config.sources
                if args.area is not None:
                    if args.area not in config.areas:
                        raise SafeError("Area is not configured")
                    sources = [source for source in sources if config.area_for(source) == args.area]
                totals: dict[str, int] = {}
                for source in sources:
                    if args.command in {"collect", "once"}:
                        for key, count in collect(config, source.id, settings.data_dir).items():
                            totals[key] = totals.get(key, 0) + count
                    if args.command in {"analyze", "once"}:
                        client = LocalClient(settings, source)
                        for key, count in analyze(config, source.id, settings.data_dir, client, max_age_seconds=settings.max_snapshot_age_seconds).items():
                            totals[key] = totals.get(key, 0) + count
                result = totals
        print(json.dumps(result, sort_keys=True))
        return exit_code
    except SafeError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"error": "Operation failed; no document or model payload was logged"}), file=sys.stderr)
        return 1
