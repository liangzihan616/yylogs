"""Observed healing candidates, separate from damage and effective healing."""
import csv
from decimal import Decimal
from pathlib import Path

from yylogs.damage_effects import display_identity
from yylogs.member_status import MemberHealingBins
from yylogs.encounter_timeline import unix_ms


class HealingSummary:
    def __init__(self, report, players, window=None):
        self.scene = report.get('scene', {})
        self.players = players
        self.members = {}
        self.timeline=MemberHealingBins(unix_ms(report['origin']),window)
        self.received_timeline=MemberHealingBins(unix_ms(report['origin']),window,
                                               entity_key='receiver_entity_id')
        self.received_record_count=0
        self.received_by_recipient={}
        self.rejected = {'different_scene': 0, 'unresolved_source': 0,
                         'unresolved_recipient': 0, 'negative_amount': 0}

    def observe(self, row):
        if row.get('event_kind') != 'healing':
            return
        if any(row.get(key) != self.scene.get(key) for key in ('scene_id', 'dungeon_sid')):
            self.rejected['different_scene'] += 1
            return
        source = row['fromer_id']
        if source not in self.players:
            self.rejected['unresolved_source'] += 1
            return
        recipient = row.get('receiver_entity_id')
        if row.get('receiver_identity_verified') is not True or recipient not in self.players:
            self.rejected['unresolved_recipient'] += 1
            return
        value = Decimal(str(row['damage']))
        if value < 0:
            self.rejected['negative_amount'] += 1
            return
        self.timeline.observe(row,value)
        if self.received_timeline.observe(row,value):
            self.received_record_count += 1
            self.received_by_recipient[recipient]=self.received_by_recipient.get(recipient,0)+1
        member = self.members.setdefault(source, {**self.players[source], 'healing': Decimal(0),
            'record_count': 0, 'skills': {}})
        key, name = display_identity(row)
        if key[0] == 'unresolved_effect':
            name = name.replace('未识别伤害', '未识别治疗')
        skill = member['skills'].setdefault(key, {'name': name, 'healing': Decimal(0),
            'record_count': 0, 'skill_ids': set(), 'calcpoint_ids': set(), 'recipients': set()})
        member['healing'] += value
        member['record_count'] += 1
        skill['healing'] += value
        skill['record_count'] += 1
        skill['skill_ids'].add(row['skill_id'])
        if type(row.get('calcpoint_id')) is int:
            skill['calcpoint_ids'].add(row['calcpoint_id'])
        skill['recipients'].add(recipient)

    def finish(self, seconds):
        total = sum((m['healing'] for m in self.members.values()), Decimal(0))
        members = []
        for source in self.members.values():
            member = {**source, 'skills': []}
            member['hps'] = member['healing'] / seconds if seconds else None
            member['share'] = member['healing'] / total if total else None
            for original in source['skills'].values():
                skill = {**original}
                for field in ('skill_ids', 'calcpoint_ids', 'recipients'):
                    skill[field] = sorted(skill[field])
                skill['hps'] = skill['healing'] / seconds if seconds else None
                skill['share'] = skill['healing'] / member['healing'] if member['healing'] else None
                member['skills'].append(skill)
            member['skills'].sort(key=lambda s: s['healing'], reverse=True)
            members.append(member)
        return {'members': sorted(members, key=lambda m: m['healing'], reverse=True),
            'timeline':self.timeline.finish(self.players.values()),
            'received_timeline':self.received_timeline.finish(self.players.values()),
            'received_record_count':self.received_record_count,
            'received_by_recipient':dict(self.received_by_recipient),
            'total_healing': total, 'record_count': sum(m['record_count'] for m in members),
            'duration_seconds': seconds, 'rejected': dict(self.rejected),
            'target_mode': 'recognized_players', 'hps_timing_basis': 'observation_window',
            'amount_basis': 'healing_candidate.damage', 'effective_healing': None,
            'overhealing': None, 'effective_healing_verified': False,
            'deduplication_verified': False, 'complete_event_coverage_verified': False,
            'game_settlement_match_verified': False}


def write_healing_csv(path, summary, member_id=None):
    with Path(path).open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['人物', '来源实体 ID', '技能', '原始 Skill ID', '原始 Calcpoint ID',
            '已采治疗合计', '治疗记录数', '占个人治疗', '观测 HPS', '观测用时秒',
            '受治疗实体 ID', '有效治疗', '过量治疗', '统计口径'])
        for member in summary['members']:
            if member_id is not None and member['id'] != member_id:
                continue
            for skill in member['skills']:
                writer.writerow([member['name'] or member['id'], member['id'], skill['name'],
                    ';'.join(map(str, skill['skill_ids'])), ';'.join(map(str, skill['calcpoint_ids'])),
                    skill['healing'], skill['record_count'], skill['share'], skill['hps'],
                    summary['duration_seconds'], ';'.join(skill['recipients']), '', '',
                    '已采治疗候选；有效/过量未知；覆盖与去重未核验'])
