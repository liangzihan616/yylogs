"""Separate residual-counter ledger. No synthetic hits or skill counts."""
from collections import defaultdict
from yylogs.packet_records import timestamp, _identity, number, MAX_ROWS


def snapshot_statistics(rows, session, start, end, npc_no):
    lower, upper = timestamp(start), timestamp(end)
    output = []
    for row in rows:
        context = row.get('encounterClock') or {}
        if ((row.get('source') or {}).get('sessionId') not in (None, session)
                or context.get('startObservedAt') != start or row.get('npcNo') != npc_no
                or not lower <= timestamp(row['observedAt']) < upper):
            continue
        output.append({'observedAt': row['observedAt'],
                       'transportRecordOrdinal': row.get('transportRecordOrdinal'),
                       'actors': [{'id': a['idHash'], 'damage': a['damage']} for a in row.get('actors', [])
                                  if _identity(a.get('idHash')) and number(a.get('damage'))]})
    return output


def validate_statistics(rows, lower, upper):
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError('RD 累计统计无效')
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'observedAt','transportRecordOrdinal','actors'}
                or not lower <= timestamp(row['observedAt']) < upper
                or not isinstance(row['actors'], list) or len(row['actors']) > 500):
            raise ValueError('RD 累计统计字段无效')
        seen = set()
        for a in row['actors']:
            if (set(a) != {'id','damage'} or not _identity(a['id']) or not number(a['damage']) or a['id'] in seen):
                raise ValueError('RD 累计统计人物无效')
            seen.add(a['id'])


def reconstruct_windows(statistics, hits, origin, players, *, final_totals=None):
    events = []
    hit_times = defaultdict(list)
    for row, amount in hits:
        actor = row['fromer_id']
        at = (row['observed_unix_ms'] - origin, row.get('transportRecordOrdinal') or 0)
        events.append((at, 0, actor, amount))
        hit_times[actor].append((at, row['receiver_entity_id']))
    for row in statistics:
        at = (timestamp(row['observedAt']) - origin, row.get('transportRecordOrdinal') or 0)
        for a in row['actors']:
            if a['id'] in players:
                events.append((at, 1, a['id'], a['damage']))
    totals, state, windows = defaultdict(float), {}, defaultdict(list)
    issues = defaultdict(int)
    def retract(actor, amount):
        for window in reversed(windows[actor]):
            removed = min(window['amount'], amount)
            window['amount'] -= removed
            amount -= removed
            if amount <= 1e-8:
                break
    for at, kind, actor, amount in sorted(events, key=lambda r: (r[0], r[1])):
        if kind == 0:
            totals[actor] += amount
            # Wait for the next counter before reconciling this hit. Ordinary
            # activity in a later interval must not move an earlier settlement.
            continue
        if actor not in state:
            state[actor] = {'counter': amount, 'baseline': amount, 'observed': totals[actor], 'at': at, 'invalid':False}
            issues['gap_before_first_statistic_skipped'] += 1
            continue
        s = state[actor]
        if amount < s['counter']:
            s['invalid'] = True
            windows[actor].clear()
            issues['gap_counter_reset_skipped'] += 1
        if s['invalid']:
            continue
        desired = max(0, amount - s['baseline'] - (totals[actor] - s['observed']))
        existing = sum(w['amount'] for w in windows[actor])
        if desired > existing + 1e-8:
            windows[actor].append({'actor':actor, 'from':s['at'], 'to':at, 'amount':desired-existing})
        elif desired < existing:
            retract(actor, existing - desired)
        s.update(counter=amount, at=at)
    # Hits received after the final counter can be delayed notifications already
    # included in that counter. Retract conservatively without relocating gaps.
    for actor, s in state.items():
        if not s['invalid']:
            final = (final_totals or {}).get(actor)
            # A verified final total includes genuine activity after the last tick.
            # It can cap retractions, but never creates another timed window.
            counter = max(s['counter'], final) if type(final) in (int, float) else s['counter']
            desired = max(0, counter - s['baseline'] - (totals[actor] - s['observed']))
            existing = sum(w['amount'] for w in windows[actor])
            if existing > desired:
                retract(actor, existing - desired)
    output = []
    for actor, rows in windows.items():
        for row in rows:
            if row['amount'] <= 1e-8:
                continue
            targets = {target for at,target in hit_times[actor] if row['from'] < at <= row['to']}
            output.append({**row, 'target': next(iter(targets)) if len(targets) == 1 else None})
    return output, dict(issues)


def _unique_window_subset(amounts, target, tolerance):
    """Bounded meet-in-the-middle match; two possible subsets mean unknown.

    Keep raw amounts rather than rounding each window to cents. Floating-point
    reception-counter noise is tolerated only in the final budget comparison.
    """
    from bisect import bisect_left, bisect_right
    if len(amounts) > 32:
        return None
    middle = len(amounts) // 2
    def sums(values, offset):
        rows = [(0.0, 0)]
        for index, amount in enumerate(values, offset):
            rows += [(total + amount, mask | (1 << index)) for total, mask in rows
                     if total + amount <= target + tolerance]
        return rows
    left = sums(amounts[:middle], 0)
    right = sorted(sums(amounts[middle:], middle))
    totals = [total for total, _ in right]
    matched = None
    for total, mask in left:
        lo = bisect_left(totals, target - total - tolerance)
        hi = bisect_right(totals, target - total + tolerance)
        for index in range(lo, hi):
            candidate = mask | right[index][1]
            if not candidate:
                continue
            if matched is not None and candidate != matched:
                return None
            matched = candidate
    return None if matched is None else {i for i in range(len(amounts)) if matched & (1 << i)}


def classify_settlement_windows(windows, hits, details):
    """Conserve the final missing budget, then isolate settlement windows.

    Ordinary missing hits may coexist with settlements. Use only an unambiguous
    subset whose sum matches the missing settlement total, leaving the ordinary
    remainder unclassified. This is a budget-based timing inference, not a
    decoded per-window skill identity; mixed settlements retain all candidate IDs.
    """
    from yylogs.final_count_ownership import skill_root, point_owner
    from yylogs.rdps_model import policy
    allowed = set(policy()['settlement_skill_ids'])
    observed, grouped = defaultdict(lambda: defaultdict(float)), defaultdict(list)
    for row, amount in hits:
        skill = point_owner(row.get('calcpoint_id')) or row.get('skill_id')
        root = skill_root(skill) if type(skill) is int and skill > 0 else None
        observed[row['fromer_id']][root] += amount
    for index, window in enumerate(windows):
        grouped[window['actor']].append((index, window['amount']))
    verified, bases = {}, {}
    for actor, rows in grouped.items():
        residual = sum(amount for _, amount in rows)
        detail = details.get(actor) or {}
        final = defaultdict(float)
        for skill, amount in (detail.get('skills') or {}).items():
            final[skill_root(int(skill))] += float(amount)
        if not final:
            continue
        missing, valid = {}, True
        for skill in final.keys() | observed[actor].keys():
            delta = final[skill] - observed[actor][skill]
            if delta < -.01:
                valid = False
                break
            if delta > .01:
                missing[skill] = delta
        tolerance = max(.01, residual * 1e-9)
        if not valid or abs(sum(missing.values()) - residual) > tolerance:
            continue
        settlements = {skill: amount for skill, amount in missing.items() if skill in allowed}
        if not settlements:
            continue
        if set(missing) == set(settlements):
            selected = set(range(len(rows)))
            basis = 'final-budget-all-windows'
        else:
            selected = _unique_window_subset([amount for _, amount in rows], sum(settlements.values()), tolerance)
            basis = 'final-budget-unique-window-subset'
        if selected is not None:
            for local_index in selected:
                index = rows[local_index][0]
                verified[index] = sorted(settlements)
                bases[index] = basis
    return [{**window, 'settlement_skill_ids': verified.get(index, []),
             'settlement_match_basis': bases.get(index)} for index, window in enumerate(windows)]
