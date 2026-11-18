"""Allowlisted, portable RD input evidence; no raw game IDs or private state."""
from yylogs.packet_records import timestamp, _identity, MAX_ROWS
from yylogs.rdps_model import finite

BUFF_FIELDS = ('kind','observedAt','targetActorHash','sourceActorHash','buffInstanceHash',
               'buffNo','level','charge_level','durationSeconds','initial','sourceChanged',
               'resourceChangeSuccess','resourceChangeRemaining','resourceChangeResource',
               'rawStartTimestamp','transportRecordOrdinal')
ATTR_FIELDS = {'actor','observedAt','attribute','value','available','initial',
               'invalidated','transportRecordOrdinal'}
ATTRIBUTES = {'W_ATK_PEN_RDC','W_DEF', *('PRO_DEF_' + c for c in 'ABCDE')}


def snapshot_rd_history(buffs, encounters, session, start, end):
    lower, upper = timestamp(start), timestamp(end)
    prior, output = {}, []
    for row in buffs:
        if (row.get('source') or {}).get('sessionId') not in (None, session):
            continue
        at = timestamp(row['observedAt'])
        if at >= upper:
            continue
        target = row.get('targetActorHash')
        if row.get('kind') == 'invalidation':
            if at < lower:
                for key in list(prior):
                    if target is None or key[0] == target:
                        prior.pop(key)
            if at >= lower:
                output.append({k: row[k] for k in BUFF_FIELDS if k in row})
            continue
        if not row.get('buffNo') or not target:
            continue
        key = (target, row.get('buffInstanceHash') or str(row['buffNo']))
        if at < lower:
            if row.get('kind') in ('remove_candidate','remove','scope_lost','unavailable'):
                prior.pop(key, None)
            else:
                old, expiry = prior.get(key, ({}, None))
                duration = row.get('durationSeconds')
                if finite(duration) and (not old or row.get('rawStartTimestamp') != old.get('rawStartTimestamp') or
                                         duration != old.get('durationSeconds')):
                    expiry = at + duration * 1000
                prior[key] = (row, expiry)
        else:
            output.append({k: row[k] for k in BUFF_FIELDS if k in row})
    seeds = []
    for row, expiry in prior.values():
        if expiry is not None and expiry <= lower:
            continue
        seed = {k: row[k] for k in BUFF_FIELDS if k in row}
        seed.update(observedAt=start, initial=True)
        if expiry is not None:
            seed['durationSeconds'] = (expiry - lower) / 1000
        seeds.append(seed)
    attr_prior, attrs = {}, []
    for row in encounters:
        if row.get('kind') != 'rd-attribute':
            continue
        at = timestamp(row['observedAt'])
        if at >= upper:
            continue
        item = {k: row[k] for k in ATTR_FIELDS - {'actor'} if k in row}
        item['actor'] = row.get('actorHash')
        key = (item['actor'], item.get('attribute'))
        if at < lower:
            if item.get('invalidated'):
                attr_prior = {k: v for k, v in attr_prior.items() if k[0] != item['actor']}
            elif item.get('attribute') not in ('W_DEF','W_ATK_PEN_RDC') or item.get('initial'):
                attr_prior[key] = item
        else:
            attrs.append(item)
    return (seeds + output, [{**r, 'observedAt': start} for r in attr_prior.values()] + attrs)


def validate_buff_evidence(row):
    success = row.get('resourceChangeSuccess')
    remaining = row.get('resourceChangeRemaining')
    if success is not None and type(success) is not bool:
        raise ValueError('血祭成功字段无效')
    if remaining is not None and (not finite(remaining) or remaining < 0):
        raise ValueError('血祭资源观测无效')
    if success is not None and (row.get('buffNo') != 1870156 or row.get('resourceChangeResource') != -2
                                or remaining is None or success != (remaining == 0)):
        raise ValueError('血祭成功字段缺少匹配的资源证据')
    for field in ('durationSeconds','rawStartTimestamp'):
        if row.get(field) is not None and not finite(row[field]):
            raise ValueError('Buff 时钟字段无效')


def validate_attributes(rows, lower, upper):
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError('RD 属性列表无效')
    for row in rows:
        if (not isinstance(row, dict) or not set(row) <= ATTR_FIELDS or not _identity(row.get('actor'))
                or not lower <= timestamp(row['observedAt']) < upper):
            raise ValueError('RD 属性观测无效')
        if row.get('invalidated') is True:
            if row.get('value') is not None:
                raise ValueError('RD 属性失效事件含数值')
            continue
        if row.get('attribute') not in ATTRIBUTES or type(row.get('available')) is not bool:
            raise ValueError('RD 属性字段无效')
        if row['available'] and (not finite(row.get('value')) or abs(row['value']) > 1e12):
            raise ValueError('RD 属性数值无效')
