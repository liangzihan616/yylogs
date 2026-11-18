"""Display filters only. No mutation of raw observations or skill-causation claim."""
from copy import deepcopy


def combat_acquired_track(track,report,*,strict_skill=False):
    windows=report.get('encounter_windows') or report.get('snapshot_filter_windows',[])
    if not windows:return None
    # Prefer the same local config classification used for display labels;
    # unknown classifications remain visible, explicitly without verification.
    # show_flag controls the game's native presentation, not whether a real
    # post-pull acquisition belongs in yylogs. Keep known debuffs out of the
    # self-buff list, but let the observed lifecycle event decide visibility.
    if track.get('config_buff_estimate')==2:return None
    kept=[]
    for segment in track['segments']:
        start=segment.get('capture_start_ms',segment['start_ms'])
        if segment['left_censored'] or not any(w['start_ms']<=start<w['end_ms'] for w in windows):continue
        if track['channel']=='gui_display_only' and segment['mode']!='finite_display':continue
        skill=segment.get('fields',{}).get('skill_id')
        if strict_skill and (type(skill) is not int or skill<=0):continue
        kept.append(deepcopy(segment))
    if not kept:return None
    return {**deepcopy({key:value for key,value in track.items() if key!='segments'}),'segments':kept}
