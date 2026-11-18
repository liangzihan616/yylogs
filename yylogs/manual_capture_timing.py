"""Pure bounded timing for manual PVE recording. No process or UI access."""
import math


def manual_joint_limits(config):
    enabled=config.get('joint_manual_capture',False)
    if type(enabled) is not bool:raise ValueError('joint_manual_capture_flag')
    if not enabled:return None
    if config.get('manual_capture') is not True or config.get('instrumentation_authorized') is not True:
        raise ValueError('manual_joint_requires_explicit_capture')
    if config.get('damage_probe_seconds') is not None:raise ValueError('probe_and_manual_joint_are_exclusive')
    battle=config.get('damage_battle_seconds');waiting=config.get('wait_for_pull_seconds')
    if type(battle) is not int or not 1<=battle<=600:raise ValueError('bounded_manual_battle_seconds')
    if type(waiting) is not int or not 1<=waiting<=120:raise ValueError('bounded_manual_wait_seconds')
    if config.get('duration_seconds')!=battle:raise ValueError('manual_joint_duration_mismatch')
    return {'battle_seconds':battle,'wait_seconds':waiting,'hard_seconds':battle+waiting}


class ManualCombatWindow:
    """Waiting does not consume battle budget; neither timeout is a kill."""
    def __init__(self,armed_monotonic,*,battle_seconds,wait_seconds):
        self.armed=armed_monotonic
        self.battle_seconds=battle_seconds;self.wait_seconds=wait_seconds
        self.started=None
    def step(self,now,combat_observed=False):
        if not math.isfinite(now) or now<self.armed:raise ValueError('invalid_monotonic_time')
        if self.started is None:
            if now-self.armed>=self.wait_seconds:return 'wait_for_pull_timeout'
            if combat_observed:self.started=now
        if self.started is not None and now-self.started>=self.battle_seconds:return 'combat_safety_cap'
        if now-self.armed>=self.wait_seconds+self.battle_seconds:return 'joint_hard_safety_cap'
        return False
