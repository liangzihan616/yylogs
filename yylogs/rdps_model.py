"""User-supplied effective rates, applied to observed hits and Buff lifetimes.

Estimated rates already include dilution. No defense/resistance inversion or
unobserved personal stats are applied on top. Self effects stay in the baseline.
"""
from functools import lru_cache
from pathlib import Path
import hashlib
import json
import math

MODEL = 'yylogs.user-rd/1'
JINGSHEN_CALCPOINTS = frozenset((230003211, 230003212))


@lru_cache(maxsize=1)
def policy():
    root = Path(__file__).parent / 'resources'
    classifiers_raw = (root / 'rd_policy.json').read_bytes()
    classifiers = json.loads(classifiers_raw.decode('utf-8-sig'))
    raw = (root / 'rd_user_policy.json').read_bytes()
    data = json.loads(raw.decode('utf-8-sig'))
    data['calcpoints'] = classifiers['calcpoints']
    data['channels'] = classifiers['channels']
    data['personal_rules'] = {}
    data['sha256'] = hashlib.sha256(raw + classifiers_raw).hexdigest()
    return data


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def lifetime_seconds(rule, duration):
    cap = rule.get('maxObservedDurationSeconds')
    if cap:
        return min(duration, cap) if finite(duration) and duration > 0 else cap
    return duration


def same_effect_family(a, b):
    rules = policy()['rules']
    first, second = rules.get(str(a.get('buffNo'))), rules.get(str(b.get('buffNo')))
    return bool(first and second and first['family'] == second['family'])


def renews_expired(row, prior, rule):
    if row.get('kind') in ('apply_candidate', 'snapshot'):
        return True
    if row.get('rawStartTimestamp') is not None and row['rawStartTimestamp'] != prior.get('rawStartTimestamp'):
        return True
    return bool(rule.get('maxObservedDurationSeconds') and any(
        k in row and row[k] != prior.get(k) for k in ('charge_level', 'sourceActorHash')))


def school(skill=None, calcpoint=None, kungfu=None):
    data = policy()['channels']
    candidates = [row for row in (data['bySkill'].get(str(skill)),
                                  data['byCalcpoint'].get(str(calcpoint))) if row]
    ids = {k for row in candidates for k in row['kungfuIds']
           if k not in data['placeholderKungfuIds']}
    if not candidates and kungfu:
        ids = {kungfu}
    rows = [data['byKungfu'].get(str(k), {}) for k in ids]
    channels = {r.get('channel') for r in rows}
    schools = {r.get('liupaiId') for r in rows}
    return {'channel': next(iter(channels)) if len(channels) == 1 else None,
            'liupaiId': next(iter(schools)) if len(schools) == 1 else None}


def settlement_hit(skill, calcpoint):
    from yylogs.final_count_ownership import point_owner, skill_root
    allowed = policy()['settlement_skill_ids']
    owner = point_owner(calcpoint)
    return (type(skill) is int and skill > 0 and skill_root(skill) in allowed
            or owner is not None and skill_root(owner) in allowed)


def hit_credit(amount, attacker, target, effects, *, attributes=None, kungfu=None,
               weak=None, calcpoint=None, skill=None, encounter_school=None, team_schools=(),
               settlement_window_verified=False):
    if not finite(amount) or amount < 0:
        raise ValueError('Invalid RD damage')
    data = policy()
    issues, selected = set(), {}
    # Keep one effective variant per family. Equal conflicting providers remain
    # unknown; the lifetime builders resolve ordered source overwrites first.
    for raw in effects:
        rule = data['rules'].get(str(raw.get('buffNo')))
        if not rule or raw.get('targetActorHash') != (attacker if rule['holder'] == 'source' else target):
            continue
        if rule.get('requiresResourceSuccess') and raw.get('resourceChangeSuccess') is not True:
            if raw.get('resourceChangeSuccess') is not False:
                issues.add('resource_success_unknown:' + str(raw['buffNo']))
            continue
        stacks = raw.get('charge_level') if rule.get('maxStacks') else 1
        if type(stacks) is not int or not 1 <= stacks <= rule.get('maxStacks', 1):
            issues.add('unknown_stacks:' + str(raw['buffNo']))
            continue
        if raw.get('ambiguousStackOwner'):
            issues.add('ambiguous_stack_owner:' + str(raw['buffNo']))
            continue
        provider = rule.get('creditProviderId') or raw.get('sourceActorHash')
        if rule.get('providerOnly') and provider != attacker:
            issues.add('provider_only:' + str(raw['buffNo']))
            continue
        if rule.get('requiresWeak') and weak is not True:
            if weak is None:
                issues.add('target_weak_unknown:' + str(raw['buffNo']))
            continue
        rate = rule.get('ratePerStack', 0) * stacks + rule.get('rate', 0)
        if stacks < rule.get('minStacks', 1):
            rate = 0
        if rule.get('burnOnly') and calcpoint not in JINGSHEN_CALCPOINTS:
            rate = 0
        if rule.get('settlementOnly') and not (settlement_window_verified or settlement_hit(skill, calcpoint)):
            rate = 0
        if rule.get('teamStyleRate') and set(team_schools).intersection(data['shadow_rainbow_school_ids']):
            rate = rule['teamStyleRate']
        e = dict(raw, rule=rule, rate=rate, sourceActorHash=provider)
        score = (rule.get('priority', 0), rate)
        old = selected.get(rule['family'])
        if old is None or score > old[0]:
            selected[rule['family']] = [score, e, False]
        elif score == old[0] and provider != old[1]['sourceActorHash']:
            old[2] = True
    active = {}
    for family, (_, e, ambiguous) in selected.items():
        if ambiguous or not e['sourceActorHash']:
            issues.add(('ambiguous_provider:' if ambiguous else 'missing_provider:') + family)
        else:
            active[family] = e
    items, pools = [], []
    for family, e in active.items():
        r, rate = e['rule'], e['rate']
        required = active.get(r.get('requiresFamily'))
        if r.get('requiresFamily') and (not required or required['sourceActorHash'] != e['sourceActorHash']):
            continue
        if r.get('weakCastRate') and weak is True:
            marker = active.get('lin-weak')
            if marker and marker['sourceActorHash'] == e['sourceActorHash']:
                rate = r['weakCastRate']
        if rate <= 0:
            continue
        if r.get('independent'):
            if e['sourceActorHash'] != attacker:
                pools.append((family, math.log1p(rate), [(e, 1.0)]))
        else:
            items.append((e, rate))
    external = [(e, rate) for e, rate in items if e['sourceActorHash'] != attacker]
    ext = sum(rate for _, rate in external)
    if ext:
        total = sum(rate for _, rate in items)
        log = math.log1p(ext / (1 + total - ext))
        pools.append(('effective', log, [(e, rate / ext) for e, rate in external]))
    log_gain = sum(log for _, log, _ in pools)
    gain = amount * -math.expm1(-log_gain)
    credit = [{'providerId': e['sourceActorHash'], 'beneficiaryId': attacker,
               'ruleId': str(e['buffNo']), 'effect': e['rule']['family'],
               'name': e['rule']['name'], 'poolId': pool,
               'creditedDamage': gain * log / log_gain * weight}
              for pool, log, entries in pools if log_gain for e, weight in entries]
    if not 0 <= gain <= amount or not math.isclose(sum(e['creditedDamage'] for e in credit), gain, abs_tol=1e-6, rel_tol=1e-10):
        raise ValueError('RD allocation did not conserve damage')
    return {'gain': gain, 'credit': credit, 'issues': sorted(issues)}
