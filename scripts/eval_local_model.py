#!/usr/bin/env python3
"""Measure local model output against hand-labelled synthetic listing facts."""
import argparse
import json
from pathlib import Path
from apartmint.config import Settings
from apartmint.evaluation import evaluate


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, default=root/'tests/evals/housing_cases.json')
    parser.add_argument('--output', type=Path, default=root/'.runtime/evaluation/latest.json')
    parser.add_argument('--repeats', type=int, default=1)
    args = parser.parse_args()
    report = evaluate(Settings.from_env(), args.corpus, args.output, repeats=args.repeats)
    print(json.dumps({'summary': report['summary'], 'report': str(args.output.resolve())}))
    return 0 if report['summary']['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
