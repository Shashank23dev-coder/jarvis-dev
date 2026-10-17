"""Test-failure feedback for an author's retry: what failed first, never a blind tail.

Works on stored test evidence (self_edit._tests). Each evidence item lists its
runners (pytest, node) with their own return codes and their spans of the combined
stdout/stderr; evidence recorded before runners existed is treated as one runner.
Locals that pytest prints with a failure (fixture values, tokens) are never copied.
"""
import re

from tools.repository import runner_results

CAP = 6000
FALLBACK_LINES = 30  # only when a failing runner printed no recognisable failure markers
NODE_KEYS = ('location:', 'failureType:', 'error:', 'expected:', 'actual:', 'operator:')


def failure_feedback(results, cap=CAP):
    """Failing tests and key error lines for each failing runner in `results`, within `cap` characters."""
    names, details = [], []
    for evidence in results:
        if not (evidence['returncode'] or evidence['timed_out'] or evidence.get('truncated')):
            continue
        runners = evidence.get('runners') or runner_results(evidence, evidence.get('paths') or ())
        if not any(r['returncode'] or r['timed_out'] for r in runners):
            names.append(f"[{evidence['phase']}: output exceeded the 16 KB evidence limit; "
                         'make the tests print less]')
        passed = [r['runner'] for r in runners if not (r['returncode'] or r['timed_out'])]
        if passed and len(passed) < len(runners):
            names.append(f"[{evidence['phase']}: {' and '.join(passed)} passed]")
        for runner in runners:
            if runner['runner'] in passed:
                continue
            stdout = _span(evidence['stdout'], runner['stdout_span'])
            stderr = _span(evidence.get('stderr', ''), runner['stderr_span'])
            failed, blocks = (_node if runner['runner'] == 'node' else _pytest)(stdout)
            state = 'timed out' if runner['timed_out'] else f"rc={runner['returncode']}"
            header = f"[{evidence['phase']}: {runner['runner']} failed, {state}]"
            names.append('\n'.join([header] + (failed or ['(no failing test names reported)'])))
            if not blocks:
                lines = (stdout.strip() or stderr.strip()).splitlines()[-FALLBACK_LINES:]
                blocks = [f"(no {runner['runner']} failure markers found; its last {len(lines)} lines)\n"
                          + '\n'.join(lines)] if lines else []
            elif stderr.strip():
                blocks.append('stderr:\n' + '\n'.join(stderr.strip().splitlines()[-10:]))
            details.append('\n\n'.join([header] + blocks))
    if not names:
        return ''
    text = 'Failing tests:\n' + '\n\n'.join(names) + '\n\nDetails:\n' + '\n\n'.join(details)
    return text if len(text) <= cap else text[:cap - 40].rstrip() + '\n[feedback cut at the size limit]'


def _span(text, span):
    start, end = span
    return text[start:end]


def _pytest(output):
    lines = output.splitlines()
    failed = [line for line in lines if re.match(r'(FAILED|ERROR) \S', line)]
    summary = [line for line in lines if re.match(r'=+ .*\b(failed|error|errors|passed|no tests ran)\b.* =+$', line)]
    blocks, current = [], None
    for line in lines:
        title = re.match(r'_{3,} (.+?) _{3,}$', line)
        if title or re.match(r'=+ (short test summary info|warnings summary) =+$', line):
            current = [title.group(1)] if title else None
            if current is not None:
                blocks.append(current)
        elif current is not None:
            current.append(line)
    return failed + summary[-1:], [_pytest_block(block) for block in blocks]


def _pytest_block(block):
    # The failing source line ('>'), up to three code lines before it, the E lines, and
    # 'path:line: Error' locations. Locals ('name = value') and blank lines are dropped.
    kept = []
    for index, line in enumerate(block[1:], 1):
        if line.startswith('>'):
            context = [l for l in block[max(1, index - 3):index] if l.startswith('    ')]
            kept += context + [line]
        elif line.startswith('E ') or re.match(r'\S+:\d+: \S', line):
            kept.append(line.rstrip())
    kept = [line for i, line in enumerate(kept) if line.strip() or (i and kept[i - 1].strip())]
    return '\n'.join([f'{block[0]}:'] + (kept[:40] or [l for l in block[1:] if l.strip()][-10:]))


def _node(output):
    lines = output.splitlines()
    failed, blocks = [], []
    for index, line in enumerate(lines):
        match = re.match(r'(\s*)not ok \d+ - (.*)', line)
        if not match:
            continue
        indent, body = match.group(1), []
        end = index + 1
        while end < len(lines) and lines[end].strip() != '...':
            end += 1
        yaml = lines[index + 1:end]
        if any("failureType: 'subtestsFailed'" in l for l in yaml):
            continue  # a parent whose subtests failed; the subtests are listed themselves
        failed.append(line.strip())
        keep, in_stack = False, 0
        for entry in yaml:
            key = entry.strip()
            top = len(entry) - len(entry.lstrip()) == len(indent) + 2
            if top:
                keep = key.startswith(NODE_KEYS)
                in_stack = 2 if key.startswith('stack:') else 0
                if keep:
                    body.append(entry.rstrip())
            elif keep:
                body.append(entry.rstrip())
            elif in_stack and not key.startswith(('node:', 'async node:')) and '(node:' not in key:
                body.append(entry.rstrip())
                in_stack -= 1
        blocks.append('\n'.join([line.strip()] + [b[len(indent):] for b in body][:40]))
    summary = [line for line in lines if re.match(r'# (fail|pass) \d+$', line)]
    return failed + summary, blocks
