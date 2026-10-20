"""Owner host CLI that records grok's verdict for one exact CLAUDE_EXTERNAL_REVIEW candidate.

Server-only: no HTTP route, tool registry or chat tool imports this module.

    python -m core.external_review record-review <candidate_id> --sha <fingerprint> \\
        --verdict passed|needs_revision --reviewer grok
"""
import argparse
import json
import os
import sys

from tools.policy import ToolDenied
from .candidate_store import CandidateStore
from .database import timestamp
from .engineering_operator import (EXTERNAL_REVIEWER, EXTERNAL_VERDICT, external_review,
                                   require_mode_allowed)


def record_review(database, candidate_id, *, sha, verdict, reviewer):
    """Bind the verdict to the sealed bytes; apply requires sha == reviewed_fingerprint."""
    if verdict not in {'passed', 'needs_revision'} or reviewer != EXTERNAL_REVIEWER:
        raise ToolDenied('Invalid external review')
    store = CandidateStore(database)
    record = store.get(candidate_id, internal=True, recover=False)
    if record is None:
        raise ToolDenied('Candidate unavailable')
    mode = (record.get('operator') or {}).get('mode')
    if not external_review(mode):
        raise ToolDenied('Candidate is not in CLAUDE_EXTERNAL_REVIEW mode')
    require_mode_allowed(mode)
    try:
        store.claim(candidate_id)
    except ValueError:
        raise ToolDenied('Candidate is busy') from None
    try:
        record = store.get(candidate_id, internal=True, recover=False)
        if (record['state'] != 'passed' or not sha
                or (record.get('reviewer_result') or {}).get('verdict') != EXTERNAL_VERDICT
                or sha != record.get('fingerprint') or sha != record.get('reviewed_fingerprint')):
            raise ToolDenied('Reviewed sha does not match the sealed candidate')
        record['external_review'] = dict(verdict=verdict, reviewer=reviewer, sha=sha, recorded_at=timestamp())
        store.save(record, 'external_review_' + verdict)
        return record['external_review']
    finally:
        store.release(candidate_id)


def main(argv=None, *, database=None):
    parser = argparse.ArgumentParser(prog='python -m core.external_review')
    commands = parser.add_subparsers(dest='command', required=True)
    record = commands.add_parser('record-review')
    record.add_argument('candidate_id')
    record.add_argument('--sha', required=True)
    record.add_argument('--verdict', required=True, choices=['passed', 'needs_revision'])
    record.add_argument('--reviewer', required=True, choices=[EXTERNAL_REVIEWER])
    args = parser.parse_args(argv)
    if database is None:
        if os.environ.get('JARVIS_MODE') != 'development':
            raise ToolDenied('External review recording requires the host development runtime')
        from .candidate_broker import STATE
        from .database import Database
        database = Database(STATE / 'candidates.sqlite3')
    result = record_review(database, args.candidate_id, sha=args.sha,
                           verdict=args.verdict, reviewer=args.reviewer)
    print(json.dumps(dict(candidate_id=args.candidate_id, external_review=result)))
    return result


if __name__ == '__main__':
    try:
        main()
    except ToolDenied as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
