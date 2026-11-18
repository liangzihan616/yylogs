"""Present one actor's cumulative settlement skills without creating hit events."""
from decimal import Decimal


def actor_snapshot(summary, entity):
    if not entity or summary.get('status') != 'available':
        return None
    eligible = [s for s in summary.get('snapshots', [])
                if s.get('actor_entity_id', s.get('own_entity_id')) == entity]
    # Sequence is validated journal order, not a damage event counter.
    return eligible[-1] if eligible else None


def detail_rows(summary, entity):
    snapshot = actor_snapshot(summary, entity)
    if snapshot is None:
        return []
    total = snapshot.get('total_damage_candidate')
    return [{**skill, 'sequence': snapshot['sequence'],
             'actor_entity_id': entity, 'source': snapshot['source'],
             'source_file': summary.get('source_file'),
             'source_sha256': summary.get('source_sha256'),
             'share': skill['damage'] / total if total and total > Decimal(0) else None,
             'included_in_damage': False, 'fight_link_verified': False}
            for skill in snapshot['skills']]
