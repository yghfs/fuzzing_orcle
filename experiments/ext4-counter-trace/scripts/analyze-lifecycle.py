#!/usr/bin/env python3
"""Generate raw/derived timeline, gap episodes and boundary/stable reports."""
import argparse
import json
from trace_lifecycle import analyze

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory')
    parser.add_argument('--output')
    args = parser.parse_args()
    report = analyze(args.directory, args.output)
    print(json.dumps({k: report[k] for k in ('passed', 'blockers', 'classification_counts')}, indent=2))
    raise SystemExit(0 if report['passed'] else 1)
