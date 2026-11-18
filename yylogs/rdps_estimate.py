"""Observed encounter history adapter for the pool-based RD estimate."""
from bisect import bisect_right
from collections import Counter, defaultdict
from decimal import Decimal
import math

from yylogs.encounter_timeline import unix_ms
from yylogs.rdps_model import MODEL, finite, hit_credit, policy, school, lifetime_seconds, same_effect_family, renews_expired

D = Decimal
ZERO = D(0)
LIMITATIONS = (
    'RDPS 为估算值。',
    '来源、层数或触发条件未确认的增益不计入。',
)


class StateIndex:
    def __init__(self, rows):
        rows = sorted(rows, key=lambda row: row[0])
        self.times = [r[0] for r in rows]
        self.values = [r[1] for r in rows]

    def at(self, time):
        i = bisect_right(self.times, time) - 1
        return self.values[i] if i >= 0 else None


def _coord(row, origin, key='observedAt'):
    return (unix_ms(row[key]) - origin, row.get('transportRecordOrdinal') or 0)


def _packet_spans(snapshot, origin, lower, upper):
    """Close actual lifecycle changes; never resurrect an overwritten instance."""
    spans, opened, retired, expired_states = [], {}, set(), {}
    floor, ceiling = (lower, -math.inf), (upper, -math.inf)
    def close(key, at):
        e, start = opened.pop(key)
        end = min(at, ceiling, e.get('_expires', ceiling))
        if start < end:
            spans.append((max(start, floor), end, e))
    for row in sorted(snapshot.get('buffs', []), key=lambda r: _coord(r, origin)):
        at = _coord(row, origin)
        if at >= ceiling:
            break
        target, kind = row.get('targetActorHash'), row.get('kind')
        if kind in ('invalidation', 'scope_lost', 'unavailable'):
            for key in list(opened):
                if not target or key[0] == target:
                    close(key, at)
            continue
        if str(row.get('buffNo')) not in policy()['rules'] and str(row.get('buffNo')) not in policy()['personal_rules']:
            continue
        key = (target, row.get('buffInstanceHash') or str(row.get('buffNo')))
        if kind in ('remove', 'remove_candidate'):
            if key in opened:
                close(key, at)
            continue
        if kind not in ('snapshot', 'apply_candidate', 'update', 'field_patch'):
            continue
        if key in retired and kind not in ('snapshot', 'apply_candidate'):
            continue
        retired.discard(key)
        rule = policy()['rules'].get(str(row.get('buffNo')), {})
        if rule.get('maxObservedDurationSeconds') and row.get('initial'):
            continue
        before = opened.get(key)
        if before and before[0].get('_expires', ceiling) <= at:
            expired = before[0]
            close(key, at)
            expired_states[key] = expired
            before = None
        if before is None and key in expired_states:
            if not renews_expired(row, expired_states[key], rule):
                continue
            expired_states.pop(key)
        e = {**(before[0] if before else {}), **row}
        e['sourceChanged'] = row.get('sourceChanged', False)
        rule = policy()['rules'].get(str(e.get('buffNo')), {})
        refresh = kind == 'apply_candidate' or (before and any(
            e.get(k) != before[0].get(k) for k in ('rawStartTimestamp', 'durationSeconds', 'buffNo')))
        if rule.get('maxObservedDurationSeconds') and before and any(
                e.get(k) != before[0].get(k) for k in ('charge_level', 'sourceActorHash')):
            refresh = True
        ambiguous = bool(before and before[0].get('ambiguousStackOwner') and not refresh)
        if e.get('sourceChanged') and rule.get('maxStacks', 1) > 1 and (e.get('charge_level') or 0) > 1:
            ambiguous = True
        e['ambiguousStackOwner'] = ambiguous
        duration = lifetime_seconds(rule, e.get('durationSeconds'))
        if finite(duration) and duration <= 0:
            if before:close(key, at)
            continue
        if finite(duration) and (not before or refresh):
            e['_expires'] = (at[0] + duration * 1000, -math.inf)
        # A new same-ID application replaces the older provider. Simultaneous
        # independent applications remain ambiguous for the per-hit selector.
        if kind == 'apply_candidate' or row.get('sourceChanged'):
            for other, (prior, start) in list(opened.items()):
                if other != key and start < at and other[0] == target and same_effect_family(prior, e):
                    close(other, at)
                    retired.add(other)
        if before:
            close(key, at)
        opened[key] = (e, max(at, floor))
    for key in list(opened):
        close(key, ceiling)
    return spans


def _track_spans(report, lower, upper):
    tracks = list(report.get('tracks', [])) + list(report.get('boss_buff_events', {}).get('tracks', []))
    native = {t.get('buff_no') for t in tracks if t.get('channel') == 'boss_events'}
    tracks += [t for t in report.get('boss_debuff', {}).get('tracks', []) if t.get('buff_no') not in native]
    for track in tracks:
        if str(track.get('buff_no')) not in policy()['rules'] and str(track.get('buff_no')) not in policy()['personal_rules']:
            continue
        if track.get('channel') not in ('replicated', 'boss_events', 'boss_sampled', 'packet_self', 'packet_boss'):
            continue
        target = track.get('target_entity_id') or track.get('entity_id')
        if track.get('channel') == 'replicated':
            target = target or report.get('own_entity_id')
        for segment in track.get('segments', []):
            a, b = segment.get('start_ms'), segment.get('end_ms')
            if not finite(a) or not finite(b) or max(a, lower) >= min(b, upper):
                continue
            yield ((max(a, lower), 0), (min(b, upper), 0),
                   {'buffNo': track['buff_no'], 'targetActorHash': target,
                    'sourceActorHash': segment.get('source_entity_id'),
                    'buffInstanceHash': str(track.get('key', track['buff_no'])),
                    **segment.get('fields', {})})


def _effect_indexes(spans):
    """Sweep once; hit lookups do not scan the full Buff history."""
    edges = defaultdict(list)
    for i, (a, b, e) in enumerate(spans):
        edges[e['targetActorHash']].extend(((a, 1, i, e), (b, 0, i, e)))
    indexes = {}
    for target, events in edges.items():
        active, samples = {}, []
        for at, add, i, e in sorted(events, key=lambda r: (r[0], r[1], r[2])):
            if add:
                active[i] = e
            else:
                active.pop(i, None)
            samples.append((at, tuple(active.values())))
        indexes[target] = StateIndex(samples)
    return indexes


class RdEstimate:
    def __init__(self, report, window, players):
        self.origin = unix_ms(report['origin'])
        self.players, self.report = players, report
        self.start = (window or {}).get('start_ms')
        self.end = (window or {}).get('end_ms')
        self.targets = set((window or {}).get('target_ids', []))
        if (window or {}).get('target_id'):
            self.targets.add(window['target_id'])
        self.enabled = finite(self.start) and finite(self.end) and self.start < self.end
        self.hits, self.reasons = [], Counter()
        self.ledger = defaultdict(lambda: ZERO)
        self.considered = 0
        self.spans, self.indexes, self.attributes, self.weak = [], {}, {}, {}
        if not self.enabled:
            return
        snapshot = report.get('packet_snapshot')
        self.spans = (_packet_spans(snapshot, self.origin, self.start, self.end) if snapshot is not None
                      else list(_track_spans(report, self.start, self.end)))
        self.indexes = _effect_indexes(self.spans)
        samples = defaultdict(list)
        state = {}
        for row in sorted((snapshot or {}).get('attributes', []), key=lambda r: _coord(r, self.origin)):
            actor = row.get('actor')
            current = state.setdefault(actor, {})
            if row.get('invalidated'):
                current.clear()
            elif row.get('available') is True:
                attr, value = row.get('attribute'), row.get('value')
                if attr in ('W_ATK_PEN_RDC', 'W_DEF') and row.get('initial'):
                    current['baseResistance' if attr == 'W_ATK_PEN_RDC' else 'baseDefense'] = value
                elif attr in ('PRO_DEF_' + c for c in 'ABCDE'):
                    current.setdefault('attributeResistanceWithEffects', {})[attr[-1]] = value
            else:
                attr = row.get('attribute', '')
                current.pop({'W_ATK_PEN_RDC': 'baseResistance', 'W_DEF': 'baseDefense'}.get(attr), None)
                current.get('attributeResistanceWithEffects', {}).pop(attr[-1:] or None, None)
            samples[actor].append((_coord(row, self.origin), {**current,
                'attributeResistanceWithEffects': dict(current.get('attributeResistanceWithEffects', {}))}))
        self.attributes = {a: StateIndex(rows) for a, rows in samples.items()}
        points = (report.get('boss_curves') or {}).get('qi', [])
        for target in self.targets:
            rows = []
            positive = False
            # An unlabelled curve only identifies a target when exactly one boss is known.
            for row in points:
                if row.get('actor', row.get('target_entity_id')) not in (target,) and len(self.targets) != 1:
                    continue
                value = row.get('current', row.get('qi'))
                if row.get('gap') or not finite(value):
                    weak, positive = None, False
                elif 'weak' in row:
                    weak = row['weak'] if type(row['weak']) is bool else None
                    positive = positive or value > 0
                else:
                    weak = False if value > 0 else True if positive else None
                    positive = positive or value > 0
                rows.append(((unix_ms(row['timestamp']) - self.origin, 0), weak))
            self.weak[target] = StateIndex(rows)

    def observe(self, row, damage):
        if not self.enabled or row.get('fromer_id') not in self.players:
            return
        when = row.get('observed_unix_ms')
        target = row.get('receiver_entity_id')
        if (not finite(when) or not self.start <= when - self.origin < self.end
                or row.get('receiver_identity_verified') is not True or target not in self.targets):
            self.reasons['unlocated_or_non_boss_hit'] += 1
            return
        amount = float(damage)
        if not finite(amount) or amount <= 0:
            return
        self.hits.append((row, amount))

    def finish(self, members, seconds):
        seconds = D(str(seconds)) if seconds is not None else None
        seconds = seconds if seconds is not None and seconds.is_finite() and seconds > 0 else None
        schools = defaultdict(set)
        for row, _ in self.hits:
            resolved = school(row.get('skill_id'), row.get('calcpoint_id'))
            if resolved.get('liupaiId') and resolved.get('channel'):
                schools[row['fromer_id']].add((resolved['liupaiId'], resolved['channel']))
        def evaluate(source, target, at, amount, row):
            effects = tuple(self.indexes[source].at(at) or ()) if source in self.indexes else ()
            effects += tuple(self.indexes[target].at(at) or ()) if target in self.indexes else ()
            fixed = next(iter(schools[source])) if len(schools[source]) == 1 else None
            result = hit_credit(amount, source, target, effects,
                attributes=self.attributes[target].at(at) if target in self.attributes else None,
                weak=self.weak[target].at(at) if target in self.weak else None,
                skill=row.get('skill_id'), calcpoint=row.get('calcpoint_id'),
                kungfu=row.get('currentKungfu'),
                settlement_window_verified=row.get('settlement_window_verified') is True,
                encounter_school={'liupaiId': fixed[0], 'channel': fixed[1]} if fixed else None,
                team_schools={s[0] for values in schools.values() for s in values})
            return result
        def credit(source, target, at, amount, row, kind):
            result = evaluate(source, target, at, amount, row)
            self.reasons.update(result['issues'])
            for entry in result['credit']:
                provider = entry['providerId']
                if provider not in self.players and provider not in policy()['mechanic_providers']:
                    self.reasons['provider_not_in_roster'] += 1
                    continue
                key = (provider, source, entry['effect'], entry['ruleId'], entry['poolId'], kind)
                self.ledger[key] += D(str(entry['creditedDamage']))
            return result
        observed = defaultdict(float)
        for row, amount in self.hits:
            source, target = row['fromer_id'], row['receiver_entity_id']
            at = (row['observed_unix_ms'] - self.origin, row.get('transportRecordOrdinal') or 0)
            credit(source, target, at, amount, row, 'observed')
            observed[source] += amount
            self.considered += 1
        snapshot = self.report.get('packet_snapshot') or {}
        from yylogs.rdps_gaps import reconstruct_windows, classify_settlement_windows
        windows, issues = reconstruct_windows(snapshot.get('rd_statistics', []), self.hits, self.origin, self.players,
            final_totals={actor: detail.get('damage') for actor, detail in (snapshot.get('details') or {}).items()})
        windows = classify_settlement_windows(windows, self.hits, snapshot.get('details') or {})
        self.reasons.update(issues)
        gap_amount, gap_windows = ZERO, []
        budgets = {m['id']: max(0, float(m['damage']) - observed[m['id']]) for m in members}
        for w in windows:
            if snapshot.get('partial') or any(e.get('kind') == 'invalidation' and
                    e.get('targetActorHash') in (None, w['actor'], w['target']) and
                    w['from'] < _coord(e, self.origin) <= w['to'] for e in snapshot.get('buffs', [])):
                self.reasons['gap_reconstruction_history_incomplete'] += 1
                continue
            amount = min(w['amount'], budgets.get(w['actor'], 0))
            if amount <= 0:
                continue
            skills = w['settlement_skill_ids']
            boundaries = {p for a,b,e in self.spans
                          if e['targetActorHash'] in (w['actor'], w['target'])
                          for p in (a,b) if w['from'] < p < w['to']}
            for index in (self.attributes.get(w['target']), self.weak.get(w['target'])):
                if index:
                    boundaries.update(t for t in index.times if w['from'] < t < w['to'])
            context = {'settlement_window_verified': bool(skills),
                       'skill_id': skills[0] if len(skills) == 1 else None}
            def allocation(at):
                result = evaluate(w['actor'], w['target'], at, 1.0, context)
                return {(e['providerId'], e['beneficiaryId'], e['ruleId'], e['effect'], e['poolId']):
                        e['creditedDamage'] for e in result['credit']}
            baseline = allocation(w['from'])
            def differs(at):
                current = allocation(at)
                return (current.keys() != baseline.keys() or any(
                    not math.isclose(current[k], baseline[k], rel_tol=1e-12, abs_tol=1e-14)
                    for k in baseline))
            # Repeated properties and a refresh by the same provider need not
            # change effective gain. Still inspect every intermediate boundary:
            # matching endpoints alone can hide removal/reapplication in between.
            changed = any(differs(at) for at in sorted(boundaries | {w['to']}))
            changes = {p[0] for p in boundaries}
            duration = w['to'][0] - w['from'][0]
            # Only verified settlements within the accepted ~one-second cadence
            # use a duration-weighted estimate when other effects change.
            weighted = bool(skills and 0 < duration <= 2000)
            if changed and not weighted:
                self.reasons['gap_buff_boundary_unknown_damage_timing'] += 1
                continue
            if weighted:
                points = sorted({w['from'][0], w['to'][0], *changes})
                slices = [(b-a, ((a+b)/2, 0)) for a,b in zip(points,points[1:]) if b>a]
            else:
                slices = [(1, w['to'])]
            credited_amount = ZERO
            for width, at in slices:
                portion = amount * width / duration if weighted else amount
                result = credit(w['actor'], w['target'], at, portion, context, 'counter-gap')
                if result['credit']:
                    credited_amount += D(str(portion))
            if credited_amount <= 0:
                self.reasons['gap_without_eligible_effects'] += 1
                continue
            budgets[w['actor']] -= amount
            gap_amount += credited_amount
            gap_windows.append({'actor': w['actor'], 'target': w['target'], 'amount': D(str(amount)),
                                'start_ms': w['from'][0], 'end_ms': w['to'][0],
                                'buff_changed_in_interval': changed,
                                'settlement_skill_ids': skills,
                                'settlement_match_basis': w['settlement_match_basis'],
                                'timing_basis': 'counter-reception-window',
                                'allocation_basis': 'duration-weighted-window' if weighted else 'stable-window',
                                'estimated_slice_count': len(slices)})
            if w['target'] is None:self.reasons['gap_target_unknown_source_effects_only'] += 1
        entries = [{'source_id': p, 'recipient_id': r, 'effect': f, 'rule_id': rule,
                    'pool': pool, 'damage': damage, 'basis': kind}
                   for (p, r, f, rule, pool, kind), damage in sorted(self.ledger.items()) if damage > 0]
        given, taken = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
        for entry in entries:
            given[entry['source_id']] += entry['damage']
            taken[entry['recipient_id']] += entry['damage']
        supported = bool(self.enabled and self.indexes and (self.considered or gap_windows) and seconds)
        present = {m['id'] for m in members}
        for actor in given:
            if actor in self.players and actor not in present:
                members.append({**self.players[actor], 'id': actor, 'damage': ZERO,
                                'record_count': 0, 'skills': [], 'dps': ZERO if seconds else None,
                                'estimated_dps': ZERO if seconds else None, 'share': ZERO})
        for member in members:
            actor = member['id']
            member.update(rd_given_damage=given[actor], rd_taken_damage=taken[actor])
            member['rd_damage_estimate'] = D(str(member['damage'])) + given[actor] - taken[actor]
            member['rdps_estimate'] = member['rd_damage_estimate'] / seconds if supported else None
            member['rd_given_dps'] = given[actor] / seconds if supported else None
            member['rd_taken_dps'] = taken[actor] / seconds if supported else None
            member['rd_provided_dps'] = member['rd_given_dps']
        coverage = defaultdict(list)
        layers = defaultdict(list)
        for a, b, e in self.spans:
            rule = policy()['rules'].get(str(e.get('buffNo')))
            if rule and e.get('sourceActorHash'):
                coverage[(e['sourceActorHash'], e['targetActorHash'], rule['family'])].append((a[0], b[0]))
                if rule['family'] in ('huanming', 'qingyimei') and not e.get('ambiguousStackOwner'):
                    stacks = e.get('charge_level')
                    if type(stacks) is int and 1 <= stacks <= rule['maxStacks']:
                        rate = (rule.get('rate', 0) + rule.get('ratePerStack', 0) * stacks
                                if stacks >= rule.get('minStacks', 1) else 0)
                        layers[(e['sourceActorHash'], e['targetActorHash'], rule['family'], stacks, rate)].append((a[0], b[0]))
        coverage_rows = []
        for (source, target, family), spans in sorted(coverage.items()):
            stop, uptime = -math.inf, 0
            for a, b in sorted(spans):
                uptime += max(0, b - max(stop, a))
                stop = max(stop, b)
            coverage_rows.append({'source_id': source, 'recipient_id': target, 'effect': family,
                                  'uptime_ms': uptime, 'coverage': uptime / (self.end - self.start)})
        layer_rows = []
        for (source, target, family, stacks, rate), spans in sorted(layers.items()):
            stop, uptime = -math.inf, 0
            for a, b in sorted(spans):
                uptime += max(0, b - max(stop, a))
                stop = max(stop, b)
            layer_rows.append({'source_id': source, 'recipient_id': target, 'effect': family,
                               'stacks': stacks, 'effective_rate': rate, 'uptime_ms': uptime,
                               'coverage': uptime / (self.end - self.start)})
        mechanisms = {}
        for actor, spec in policy()['mechanic_providers'].items():
            observed = [e for _, _, e in self.spans if str(e.get('buffNo')) in spec['ruleIds']]
            triggered = bool(observed) and (actor != 'mechanic:xueji' or any(e.get('resourceChangeSuccess') is True for e in observed))
            status = ('estimated' if triggered and supported else 'trigger_unconfirmed' if observed and not triggered
                      else 'damage_unobserved' if observed else 'unobserved')
            mechanisms[actor] = {'name': spec['name'], 'status': status,
                                 'damage': given[actor] if status == 'estimated' else None,
                                 'dps': given[actor] / seconds if status == 'estimated' else None}
        return {'model': MODEL, 'policy_id': policy()['id'], 'policy_sha256': policy()['sha256'],
                'supported': supported, 'estimated': True, 'display_metric': 'rdps_estimate',
                'profile': {'rate_basis': policy()['rate_basis'], 'source_sha256': policy()['source_sha256']},
                'limitations': list(LIMITATIONS), 'allocation': 'effective-rates-counterfactual-with-self-baseline',
                'duration_seconds': seconds, 'coverage': coverage_rows, 'layer_coverage': layer_rows,
                'ledger': [e for e in entries if e['basis'] == 'observed'],
                'gap_ledger': [e for e in entries if e['basis'] == 'counter-gap'],
                'gap_windows': gap_windows, 'gap_reconstructed_damage': gap_amount,
                'given_damage': sum(given.values(), ZERO), 'taken_damage': sum(taken.values(), ZERO),
                'mechanic_contributions': mechanisms, 'considered_hits': self.considered,
                'omissions': dict(self.reasons), 'precise_rdps_supported': False,
                'unavailable_reason': None if supported else '缺少命中时间、可观测增益或有效战斗用时'}


def estimate_summary(report, window, players, members, damage_rows, seconds):
    estimate = RdEstimate(report, window, players)
    for row in damage_rows:
        estimate.observe(row, row['damage'])
    return estimate.finish(members, seconds)


def estimate_tooltip(member, estimate):
    available = bool(estimate and estimate.get('supported'))
    def value(key):
        amount = member.get(key) if available else None
        return f'{amount:,.1f}' if amount is not None else '—'
    return f"每秒提供：{value('rd_given_dps')}\n每秒吃到：{value('rd_taken_dps')}"
