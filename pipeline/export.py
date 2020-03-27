import math
import datajoint as dj
from collections import defaultdict
import numpy as np
from decimal import Decimal
import scipy.io as scio
import json
import pathlib
import pandas as pd
from tqdm import tqdm

from datetime import datetime
from pipeline import lab, experiment, tracking, ephys, histology, psth, ccf
from pipeline.util import _get_clustering_method

'''

Notes:

  - export includes behavior for trials without ephys data. how to handle?

    if exclude, this means trial indices will be non-contiguous w/r/t database
    if include, this means .mat cell arrays will vary by shape and need
    handling locally.

  - Photostim Data (task_stimulation):

    - Experimental data doesn't contain actual start/end/power times;
      Start is captured per trial with power/duration modelled as session
      parameters. This implies that power+off time in export data are
      synthetic.

'''


def mkfilename(insert_key):
    '''
    create a filename for the given insertion key.
    filename will be of the format map-export_h2o_YYYYMMDD_HHMMSS_SN_PN.mat

    where:

      - h2o: water restriction number
      - YYYYMMDD_HHMMSS: session recording datetime
      - SN: session number for this subject
      - PN: probe number for this session

    '''

    fvars = ((lab.WaterRestriction
              * experiment.Session.proj(session_datetime="cast(concat(session_date, ' ', session_time) as datetime)")
              * ephys.ProbeInsertion) & insert_key).fetch1()

    return 'map-export_{}_{}_s{}_p{}.mat'.format(
        fvars['water_restriction_number'],
        fvars['session_datetime'].strftime('%Y%m%d_%H%M%S'),
        fvars['session'], fvars['insertion_number'])


def export_recording(insert_keys, output_dir='./', filename=None, overwrite=False):
    '''
    Export a 'recording' (or a list of recording) (probe specific data + related events) to a file.

    Parameters:

      - insert_keys: one or a list of ephys.ProbeInsertion.primary_key
        currently: {'subject_id', 'session', 'insertion_number'})

      - output_dir: directory to save the file at (default to be the current working directory)

      - filename: an optional output file path string. If not provided,
        filename will be autogenerated using the 'mkfilename'
        function.
        Note: if exporting a list of probe keys, filename will be auto-generated
    '''
    if not isinstance(insert_keys, list):
        _export_recording(insert_keys, output_dir=output_dir, filename=filename, overwrite=overwrite)
    else:
        filename = None
        for insert_key in insert_keys:
            try:
                _export_recording(insert_key, output_dir=output_dir, filename=filename, overwrite=overwrite)
            except Exception as e:
                print(str(e))
                print('Skipping this export...')
                pass


def _export_recording(insert_key, output_dir='./', filename=None, overwrite=False):
    '''
    Export a 'recording' (probe specific data + related events) to a file.

    Parameters:

      - insert_key: an ephys.ProbeInsertion.primary_key
        currently: {'subject_id', 'session', 'insertion_number'})

      - output_dir: directory to save the file at (default to be the current working directory)

      - filename: an optional output file path string. If not provided,
        filename will be autogenerated using the 'mkfilename'
        function.
    '''

    if filename is None:
        filename = mkfilename(insert_key)

    filepath = pathlib.Path(output_dir) / filename

    if filepath.exists() and not overwrite:
        print('{} already exists, skipping...'.format(filepath))
        return

    print('\n========================================================================')
    print('exporting {} to {}'.format(insert_key, filepath))

    print('fetching spike/behavior data')

    try:
        insertion = (ephys.ProbeInsertion.InsertionLocation & insert_key).fetch1()
        loc = (ephys.ProbeInsertion & insert_key).aggr(ephys.ProbeInsertion.RecordableBrainRegion.proj(
            brain_region='CONCAT(hemisphere, " ", brain_area)'),
            brain_regions='GROUP_CONCAT(brain_region SEPARATOR ", ")').fetch1('brain_regions')
    except dj.DataJointError:
        raise KeyError('Probe Insertion Location not yet available')

    clustering_method = _get_clustering_method(insert_key)

    q_unit = (ephys.Unit * lab.ElectrodeConfig.Electrode.proj()
              * lab.ProbeType.Electrode.proj('shank') & insert_key & {'clustering_method': clustering_method})

    units = q_unit.fetch(order_by='unit')

    trial_spikes = (ephys.Unit.TrialSpikes & insert_key).fetch(order_by='trial asc')

    behav = (experiment.BehaviorTrial & insert_key).fetch(order_by='trial asc')

    trials = behav['trial']

    exports = ['probe_insertion_info', 'neuron_id',
               'neuron_single_units', 'neuron_unit_info', 'neuron_unit_quality_control',
               'behavior_report',
               'behavior_early_report', 'behavior_lick_times',
               'behavior_is_free_water', 'behavior_is_auto_water',
               'task_trial_type', 'task_stimulation', 'trial_end_time',
               'task_sample_time', 'task_delay_time', 'task_cue_time',
               'tracking', 'histology']

    edata = {k: [] for k in exports}

    print('reshaping/processing for export')

    # probe_insertion_info
    # -------------------
    edata['probe_insertion_info'] = {k: float(v) if isinstance(v, Decimal) else v for k, v in dict(
        insertion, recordable_brain_regions=loc).items() if k not in ephys.ProbeInsertion.InsertionLocation.primary_key}

    # neuron_single_units
    # -------------------

    # [[u0t0.spikes, ..., u0tN.spikes], ..., [uNt0.spikes, ..., uNtN.spikes]]
    print('... neuron_single_units:', end='')

    _su = defaultdict(list)

    ts = trial_spikes[['unit', 'trial', 'spike_times']]

    for u, t in ((u, t) for t in trials for u in units['unit']):
        ud = ts[np.logical_and(ts['unit'] == u, ts['trial'] == t)]
        if ud:
            _su[u].append(ud['spike_times'][0])
        else:
            _su[u].append(np.array([]))

    ndarray_object = np.empty((len(_su.keys()), 1), dtype=np.object)
    for idx, i in enumerate(sorted(_su.keys())):
        ndarray_object[idx, 0] = np.array(_su[i], ndmin=2).T

    edata['neuron_single_units'] = ndarray_object

    print('ok.')

    # neuron_unit_info
    # ----------------
    #
    # [[depth_in_um, cell_type, recording_location] ...]
    print('... neuron_unit_info:', end='')

    dv = float(insertion['depth']) if insertion['depth'] else np.nan

    cell_types = {u['unit']: u['cell_type'] for u in (ephys.UnitCellType & insert_key).fetch(as_dict=True, order_by='unit')}

    _ui = []
    for u in units:
        typ = cell_types[u['unit']] if u['unit'] in cell_types else 'unknown'
        _ui.append([u['unit'], u['unit_posx'], u['unit_posy'] + dv, u['shank'], typ, loc])

    edata['neuron_unit_info'] = np.array(_ui, dtype='O')

    print('ok.')

    # neuron_unit_quality_control
    # ----------------
    # structure of all of the QC fields, each contains 1d array of length equals to the number of unit. E.g.:
    # presence_ratio: (Nx1)
    # unit_amp: (Nx1)
    # unit_snr: (Nx1)
    # ...

    q_qc = (ephys.Unit & insert_key).proj('unit_amp', 'unit_snr').aggr(
        ephys.UnitStat, ..., **{n: n for n in ephys.UnitStat.heading.names if n not in ephys.UnitStat.heading.primary_key},
        keep_all_rows=True).aggr(
        ephys.ClusterMetric, ..., **{n: n for n in ephys.ClusterMetric.heading.names if n not in ephys.ClusterMetric.heading.primary_key},
        keep_all_rows=True).aggr(
        ephys.WaveformMetric, ..., **{n: n for n in ephys.WaveformMetric.heading.names if n not in ephys.WaveformMetric.heading.primary_key},
        keep_all_rows=True)
    qc_names = [n for n in q_qc.heading.names if n not in q_qc.primary_key]

    if q_qc:
        qc = (q_qc & insert_key).fetch(*qc_names, order_by='unit')
        qc_df = pd.DataFrame(qc).T
        qc_df.columns = qc_names
        edata['neuron_unit_quality_control'] = {n: qc_df.get(n).values for n in qc_names}

    # behavior_report
    # ---------------
    print('... behavior_report:', end='')

    behavior_report_map = {'hit': 1, 'miss': 0, 'ignore': -1}
    edata['behavior_report'] = np.array([
        behavior_report_map[i] for i in behav['outcome']])

    print('ok.')

    # behavior_early_report
    # ---------------------
    print('... behavior_early_report:', end='')

    early_report_map = {'early': 1, 'no early': 0}
    edata['behavior_early_report'] = np.array([
        early_report_map[i] for i in behav['early_lick']])

    print('ok.')

    # behavior_is_free_water
    # ---------------------
    print('... behavior_is_free_water:', end='')

    edata['behavior_is_free_water'] = np.array([i for i in behav['free_water']])

    print('ok.')

    # behavior_is_auto_water
    # ---------------------
    print('... behavior_is_auto_water:', end='')

    edata['behavior_is_auto_water'] = np.array([i for i in behav['auto_water']])

    print('ok.')

    # behavior_touch_times
    # --------------------

    behavior_touch_times = None  # NOQA no data (see ActionEventType())

    # behavior_lick_times
    # -------------------
    print('... behavior_lick_times:', end='')

    _lt = []
    licks = (experiment.ActionEvent() & insert_key
             & "action_event_type in ('left lick', 'right lick')").fetch()

    for t in trials:

        _lt.append([float(i) for i in   # decimal -> float
                    licks[licks['trial'] == t]['action_event_time']]
                   if t in licks['trial'] else [])

    edata['behavior_lick_times'] = np.array(_lt)

    behavior_whisker_angle = None  # NOQA no data
    behavior_whisker_dist2pol = None  # NOQA no data

    print('ok.')

    # task_trial_type
    # ---------------
    print('... task_trial_type:', end='')

    task_trial_type_map = {'left': 'l', 'right': 'r'}
    edata['task_trial_type'] = np.array([
        task_trial_type_map[i] for i in behav['trial_instruction']], dtype='O')

    print('ok.')

    # task_stimulation
    # ----------------
    print('... task_stimulation:', end='')

    _ts = []  # [[power, type, on-time, off-time], ...]

    q_photostim = (experiment.Photostim * experiment.PhotostimBrainRegion.proj(
        stim_brain_region='CONCAT(stim_laterality, " ", stim_brain_area)') & insert_key)

    photostim_keyval = {'left ALM': 1,
                        'right ALM': 2,
                        'both ALM': 6}

    photostim_map, photostim_dat = {}, {}
    for pstim in q_photostim.fetch():
        photostim_map[pstim['photo_stim']] = photostim_keyval[pstim['stim_brain_region']]
        photostim_dat[pstim['photo_stim']] = pstim

    photostim_ev = (experiment.PhotostimEvent & insert_key).fetch()

    for t in trials:

        if t in photostim_ev['trial']:

            ev = photostim_ev[np.where(photostim_ev['trial'] == t)]
            ps = photostim_map[ev['photo_stim'][0]]
            pdat = photostim_dat[ev['photo_stim'][0]]

            _ts.append([float(ev['power']), ps,
                        float(ev['photostim_event_time']),
                        float(ev['photostim_event_time'] + pdat['duration'])])

        else:
            _ts.append([0, math.nan, math.nan, math.nan])

    edata['task_stimulation'] = np.array(_ts)

    print('ok.')

    # task_pole_time
    # --------------

    task_pole_time = None  # NOQA no data

    # task_sample_time - (sample period) - list of (onset, duration)
    # -------------

    print('... task_sample_time:', end='')

    _tst, _tsd = (experiment.BehaviorTrial & insert_key).aggr(experiment.TrialEvent & 'trial_event_type = "sample"',
                                                              sample_time='trial_event_time', duration='duration').fetch(
        'sample_time', 'duration', order_by='trial')

    edata['task_sample_time'] = np.array([_tst, _tsd]).astype(float)

    print('ok.')

    # task_delay_time - (delay period) - list of (onset, duration)
    # -------------

    print('... task_delay_time:', end='')

    _tdt, _tdd = (experiment.BehaviorTrial & insert_key).aggr(experiment.TrialEvent & 'trial_event_type = "delay"',
                                                              delay_time='trial_event_time', duration='duration').fetch(
        'delay_time', 'duration', order_by='trial')

    edata['task_delay_time'] = np.array([_tdt, _tdd]).astype(float)

    print('ok.')

    # task_cue_time - (response period) - list of (onset, duration)
    # -------------

    print('... task_cue_time:', end='')

    _tct, _tcd = (experiment.BehaviorTrial & insert_key).aggr(experiment.TrialEvent & 'trial_event_type = "go"',
                                                              go_time='trial_event_time', duration='duration').fetch(
        'go_time', 'duration', order_by='trial')

    edata['task_cue_time'] = np.array([_tct, _tcd]).astype(float)

    print('ok.')

    # trial_end_time - list of (onset, duration)
    # -------------

    print('... trial_end_time:', end='')

    _tet, _ted = (experiment.BehaviorTrial & insert_key).aggr(experiment.TrialEvent & 'trial_event_type = "trialend"',
                                                              trialend_time='trial_event_time', duration='duration').fetch(
        'trialend_time', 'duration', order_by='trial')

    edata['trial_end_time'] = np.array([_tet, _ted]).astype(float)

    print('ok.')

    # tracking
    # ----------------
    print('... tracking:', end='')
    tracking_struct = {}
    for feature, feature_tbl in tracking.Tracking().tracking_features.items():
        ft_attrs = [n for n in feature_tbl.heading.names if n not in feature_tbl.primary_key]
        trk_data = (tracking.Tracking * feature_tbl * tracking.TrackingDevice.proj(
            fs='sampling_rate', camera='concat(tracking_device, "_", tracking_position)') & insert_key).fetch(
            'camera', 'fs', 'tracking_samples', 'trial', *ft_attrs, order_by='trial', as_dict=True)

        for trk_d in trk_data:
            camera = trk_d['camera'].replace(' ', '_').lower()
            if camera not in tracking_struct:
                tracking_struct[camera] = {'fs': float(trk_d['fs']),
                                           'Nframes': [],
                                           'trialNum': []}
            if trk_d['trial'] not in tracking_struct[camera]['trialNum']:
                tracking_struct[camera]['trialNum'].append(trk_d['trial'])
                tracking_struct[camera]['Nframes'].append(trk_d[ft_attrs[0]])
            for ft in ft_attrs:
                if ft not in tracking_struct[camera]:
                    tracking_struct[camera][ft] = []
                tracking_struct[camera][ft].append(trk_d[ft])

    if tracking_struct:
        edata['tracking'] = tracking_struct
        print('ok.')
    else:
        print('n/a')

    # histology - unit ccf
    # ----------------
    print('... histology:', end='')
    unit_ccfs = []
    for ccf_tbl in (histology.ElectrodeCCFPosition.ElectrodePosition, histology.ElectrodeCCFPosition.ElectrodePositionError):
        unit_ccf = (ephys.Unit * ccf_tbl * ccf.CCFAnnotation.proj('annotation') & insert_key
                    & {'clustering_method': clustering_method}).fetch('unit', 'ccf_x', 'ccf_y', 'ccf_z', 'annotation')
        unit_ccfs.extend(list(zip(*unit_ccf)))

    if unit_ccfs:
        unit_id, ccf_x, ccf_y, ccf_z, anno = zip(*sorted(unit_ccfs, key=lambda x: x[0]))
        edata['histology'] = {'unit': unit_id, 'ccf_x': ccf_x, 'ccf_y': ccf_y, 'ccf_z': ccf_z, 'annotation': anno}
        print('ok.')
    else:
        print('n/a')
    # savemat
    # -------

    print('... saving to {}:'.format(filepath), end='')

    scio.savemat(filepath, edata)

    print('ok.')


def write_to_activity_viewer_json(probe_insertion, filepath=None, per_period=False):
    probe_insertion = probe_insertion.proj()
    key = (probe_insertion * lab.WaterRestriction * experiment.Session).proj('session_date', 'water_restriction_number').fetch1()
    uid = f'{key["subject_id"]}({key["water_restriction_number"]})/{datetime.strftime(key["session_date"], "%m-%d-%Y")}({key["session"]})/{key["insertion_number"]}'

    units = (ephys.UnitStat * ephys.Unit * lab.ElectrodeConfig.Electrode
             * histology.ElectrodeCCFPosition.ElectrodePosition
             & probe_insertion & 'unit_quality != "all"').fetch(
        'unit', 'ccf_x', 'ccf_y', 'ccf_z', 'avg_firing_rate', order_by='unit')

    if len(units[0]) == 0:
        print('The units in the specified ProbeInsertion do not have CCF data yet')
        return

    penetration_group = {'id': uid, 'points': []}

    for unit, x, y, z, spk_rate in tqdm(zip(*units)):
        contra_frate, ipsi_frate = (psth.PeriodSelectivity & probe_insertion
                                    & f'unit={unit}' & 'period in ("sample", "delay", "response")').fetch(
            'contra_firing_rate', 'ipsi_firing_rate')

        # (red: #FF0000), (blue: #0000FF)
        if per_period:
            sel_color = ['#FF0000' if i_rate > c_rate else '#0000FF' for c_rate, i_rate in zip(contra_frate, ipsi_frate)]
            radius = [np.mean([c_rate, i_rate]) for c_rate, i_rate in zip(contra_frate, ipsi_frate)]
        else:
            sel_color = ['#FF0000' if ipsi_frate.mean() > contra_frate.mean() else '#0000FF']
            radius = [spk_rate]

        unit_dict = {'id': unit, 'x': x, 'y': y, 'z': z, 'alpha': 0.8,
                     'color': {'t': list(range(len(sel_color))), 'vals': sel_color},
                     'radius': {'t': list(range(len(radius))), 'vals': radius}}

        penetration_group['points'].append(unit_dict)

    if filepath:
        path = pathlib.Path(filepath)
        with open(path, 'w') as fp:
            json.dump(penetration_group, fp, default=str)

    return penetration_group
