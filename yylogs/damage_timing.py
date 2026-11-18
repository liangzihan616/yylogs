"""HP-concordant callback timing estimates, never server combat timestamps."""
from decimal import Decimal
import math
from yylogs.encounter_timeline import unix_ms


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value)


class DamageTiming:
    def __init__(self, report, window, capture):
        self.valid = False
        self.count = 0
        self.previous = None
        self.edges = [[], []]
        self.sums = [Decimal(0), Decimal(0)]
        identity, scene = report.get('capture_identity', {}), report.get('scene', {})
        depletion = window.get('hp_depletion', {}) if window else {}
        start = window.get('start_evidence', {}) if window else {}
        sample_hash = report.get('source_sha256', {}).get('samples.jsonl')
        if (not depletion or start.get('kind') != 'observed_boss_hp_drop'
                or not isinstance(sample_hash, str) or len(sample_hash) != 64
                or report.get('cleanup_verified') is not True
                or capture.get('schema') != 'yylogs.joint-damage-probe/1'
                or capture.get('clock') != 'host_unix_epoch_ms'
                or capture.get('manual_joint_capture') is not True
                or any(capture.get(k) is not True for k in
                       ('cleanup_verified', 'script_unloaded', 'parent_session_detached'))
                or type(capture.get('damage_candidates')) is not int
                or capture['damage_candidates'] <= 0
                or (capture.get('agent_stop') or {}).get('reason') in ('record_budget', 'decode_budget')
                or capture.get('bounded_stop_reason') in ('record_budget', 'decode_budget')):
            return
        expected = {'game_pid': identity.get('game_pid'),
                    'process_creation_ticks': identity.get('process_creation_ticks'),
                    'scene_id': scene.get('scene_id'), 'dungeon_sid': scene.get('dungeon_sid'),
                    'own_entity_id': report.get('own_entity_id'), 'target_id': window.get('target_id')}
        if any(value is None or capture.get(key) != value for key, value in expected.items()):
            return
        values = [start.get('hp_before'), start.get('hp_after'), depletion.get('hp_before')]
        if not all(numeric(v) for v in values) or not values[0] > values[1] > 0 or values[2] <= 0:
            return
        try:
            origin = unix_ms(report['origin'])
            self.bounds = [[origin+v for v in window[key]]
                           for key in ('start_interval_ms', 'end_interval_ms')]
            if not all(numeric(v) for edge in self.bounds for v in edge):
                return
            if not self.bounds[0][0] <= self.bounds[0][1] < self.bounds[1][0] <= self.bounds[1][1]:
                return
        except (KeyError, TypeError, ValueError):
            return
        self.losses = [Decimal(str(values[0]))-Decimal(str(values[1])), Decimal(str(values[2]))]
        self.target = expected['target_id']
        self.scene = scene
        self.capture = capture
        self.valid = True

    def observe(self, row):
        if not self.valid:
            return
        self.count += 1
        if row.get('scene_id') != self.scene['scene_id'] or row.get('dungeon_sid') != self.scene['dungeon_sid']:
            self.valid = False
            return
        when = row.get('observed_unix_ms')
        if not numeric(when) or (self.previous is not None and when < self.previous):
            self.valid = False
            return
        self.previous = when
        if row.get('receiver_entity_id') != self.target:
            return
        if row.get('event_kind') in ('healing', 'conflict'):
            # HP recovery or ambiguous kinds cannot establish damage edges.
            self.valid = False
            return
        hurt = row.get('hurt')
        if row['damage'] <= 0 and hurt in (None, 0):
            return
        if (row.get('receiver_identity_verified') is not True
                or row.get('event_identity_bound') is not True
                or row.get('receiver_event_name') not in ('Behit', 'BehitWithoutAnim')
                or row['damage'] < 0
                or not numeric(hurt) or hurt < 0
                or row.get('absorbed_heal') != 0):
            self.valid = False
            return
        if hurt == 0:
            return
        if when < self.bounds[0][0] or when > self.bounds[1][1]:
            self.valid = False
            return
        for i, (lower, upper) in enumerate(self.bounds):
            if lower <= when <= upper:
                try:
                    if abs(unix_ms(row['received_at'])-when) > 100:
                        raise ValueError('callback clock mismatch')
                except (KeyError, ValueError):
                    self.valid = False
                    return
                self.sums[i] += Decimal(str(hurt))
                # Retain just first/last timestamps, regardless of hit volume.
                if not self.edges[i]:
                    self.edges[i] = [when, when]
                else:
                    self.edges[i][1] = when

    def finish(self, journal_sha256):
        if (not self.valid or self.count != self.capture['damage_candidates']
                or not all(self.edges)):
            return None
        for actual, expected in zip(self.sums, self.losses):
            if abs(actual-expected) > max(Decimal('.000001'), expected*Decimal('1e-12')):
                return None
        start, end = self.edges[0][0], self.edges[1][1]
        if self.capture.get('first_boss_candidate_observed_unix_ms') != start or end <= start:
            return None
        return {'basis': 'hp_delta_matched_damage_callbacks',
                'duration_seconds': (Decimal(str(end))-Decimal(str(start)))/1000,
                'start_unix_ms': start, 'end_unix_ms': end,
                'matched_edge_hp_loss': list(map(str, self.losses)),
                'journal_sha256': journal_sha256,
                'exact_server_timing_verified': False, 'filter_window_unchanged': True}
