# Copyright (c) 2016-2022 Association of Universities for Research in Astronomy, Inc. (AURA)
# For license information see LICENSE or https://opensource.org/licenses/BSD-3-Clause

import calendar
import json
import zipfile
from datetime import datetime, timedelta
from os import PathLike
from pathlib import Path
from typing import FrozenSet, Iterable, List, Mapping, Optional, Tuple

import numpy as np
from lucupy.helpers import dmsstr2deg
from lucupy.minimodel import (AndGroup, AndOption, Atom, Band, CloudCover, Conditions, Constraints, ElevationType,
                              Group, GroupID, ImageQuality, Magnitude, MagnitudeBands, NonsiderealTarget, Observation,
                              ObservationClass, ObservationID, ObservationMode, ObservationStatus, OrGroup, Priority,
                              Program, ProgramID, ProgramMode, ProgramTypes, QAState, Resource, ROOT_GROUP_ID, Semester,
                              SemesterHalf, SetupTimeType, SiderealTarget, Site, SkyBackground, Target, TargetName,
                              TargetType, TimeAccountingCode, TimeAllocation, TimingWindow, TooType, WaterVapor,
                              Wavelength)
from lucupy.observatory.gemini.geminiobservation import GeminiObservation
from lucupy.timeutils import sex2dec
from lucupy.types import ZeroTime
from scipy.signal import find_peaks


from scheduler.core.programprovider.abstract import ProgramProvider
from scheduler.core.sources import Sources
from scheduler.services import logger_factory

logger = logger_factory.create_logger(__name__)


def read_ocs_zipfile(zip_file: str | PathLike[str]) -> Iterable[dict]:
    """
    Since for OCS we will use a collection of extracted ODB data, this is a
    convenience method to parse the data into a list of the JSON program data.
    """
    with zipfile.ZipFile(zip_file, 'r') as zf:
        for filename in zf.namelist():
            with zf.open(filename) as f:
                contents = f.read().decode('utf-8')
                logger.info(f'Adding program {Path(filename).with_suffix("")}.')
                yield json.loads(contents)


class OcsProgramProvider(ProgramProvider):
    """
    A ProgramProvider that parses programs from JSON extracted from the OCS
    Observing Database.
    """

    _GPI_FILTER_WAVELENGTHS = {'Y': 1.05, 'J': 1.25, 'H': 1.65, 'K1': 2.05, 'K2': 2.25}
    _NIFS_FILTER_WAVELENGTHS = {'ZJ': 1.05, 'JH': 1.25, 'HK': 2.20}
    _OBSERVE_TYPES = frozenset(['FLAT', 'ARC', 'DARK', 'BIAS'])

    # Note that we want to include OBSERVED observations here since this is legacy data, so most if not all observations
    # should be marked OBSERVED and we will reset this later to READY.
    _OBSERVATION_STATUSES = frozenset({ObservationStatus.READY, ObservationStatus.ONGOING, ObservationStatus.OBSERVED})

    # We contain private classes with static members for the keys in the associative
    # arrays in order to have this information defined at the top-level once.
    class _ProgramKeys:
        ID = 'programId'
        INTERNAL_ID = 'key'
        BAND = 'queueBand'
        THESIS = 'isThesis'
        MODE = 'programMode'
        TOO_TYPE = 'tooType'
        TIME_ACCOUNT_ALLOCATION = 'timeAccountAllocationCategories'
        NOTE = 'INFO'
        SCHED_NOTE = 'INFO_SCHEDNOTE'
        PROGRAM_NOTE = 'INFO_PROGRAMNOTE'

    class _NoteKeys:
        TITLE = 'title'
        TEXT = 'text'

    # Strings in notes that indicate that an observation should not be splittable.
    _NO_SPLIT_STRINGS = frozenset({"do not split",
                                   "do not interrupt"})

    class _TAKeys:
        CATEGORIES = 'timeAccountAllocationCategories'
        CATEGORY = 'category'
        AWARDED_PROG_TIME = 'awardedProgramTime'
        AWARDED_PART_TIME = 'awardedPartnerTime'
        USED_PROG_TIME = 'usedProgramTime'
        USED_PART_TIME = 'usedPartnerTime'

    class _GroupKeys:
        SCHEDULING_GROUP = 'GROUP_GROUP_SCHEDULING'
        ORGANIZATIONAL_FOLDER = 'GROUP_GROUP_FOLDER'
        GROUP_NAME = 'name'

    class _ObsKeys:
        KEY = 'OBSERVATION_BASIC'
        ID = 'observationId'
        INTERNAL_ID = 'key'
        QASTATE = 'qaState'
        LOG = 'obsLog'
        STATUS = 'obsStatus'
        PRIORITY = 'priority'
        TITLE = 'title'
        SEQUENCE = 'sequence'
        SETUPTIME_TYPE = 'setupTimeType'
        SETUPTIME = 'setupTime'
        OBS_CLASS = 'obsClass'
        PHASE2 = 'phase2Status'
        TOO_OVERRIDE_RAPID = 'tooOverrideRapid'

    class _TargetKeys:
        KEY = 'TELESCOPE_TARGETENV'
        BASE = 'base'
        TYPE = 'type'
        RA = 'ra'
        DEC = 'dec'
        DELTA_RA = 'deltara'
        DELTA_DEC = 'deltadec'
        EPOCH = 'epoch'
        DES = 'des'
        TAG = 'tag'
        MAGNITUDES = 'magnitudes'
        NAME = 'name'

    class _TargetEnvKeys:
        GUIDE_GROUPS = 'guideGroups'
        GUIDE_GROUP_NAME = 'name'
        GUIDE_GROUP_PRIMARY = 'primaryGroup'
        GUIDE_PROBE = 'guideProbe'
        GUIDE_PROBE_KEY = 'guideProbeKey'
        AUTO_GROUP = 'auto'
        TARGET = 'target'
        USER_TARGETS = 'userTargets'

    class _ConstraintKeys:
        KEY = 'SCHEDULING_CONDITIONS'
        CC = 'cc'
        IQ = 'iq'
        SB = 'sb'
        WV = 'wv'
        ELEVATION_TYPE = 'elevationConstraintType'
        ELEVATION_MIN = 'elevationConstraintMin'
        ELEVATION_MAX = 'elevationConstraintMax'
        TIMING_WINDOWS = 'timingWindows'

    class _AtomKeys:
        OBS_CLASS = 'observe:class'
        INSTRUMENT = 'instrument:instrument'
        WAVELENGTH = 'instrument:observingWavelength'
        OBSERVED = 'metadata:complete'
        TOTAL_TIME = 'totalTime'
        OFFSET_P = 'telescope:p'
        OFFSET_Q = 'telescope:q'
        EXPOSURE_TIME = 'observe:exposureTime'
        DATA_LABEL = 'observe:dataLabel'
        COADDS = 'observe:coadds'
        FILTER = 'instrument:filter'
        DISPERSER = 'instrument:disperser'
        OBSERVE_TYPE = 'observe:observeType'

    class _TimingWindowKeys:
        TIMING_WINDOWS = 'timingWindows'
        START = 'start'
        DURATION = 'duration'
        REPEAT = 'repeat'
        PERIOD = 'period'

    class _MagnitudeKeys:
        NAME = 'name'
        VALUE = 'value'

    class _FPUKeys:
        GSAOI = 'instrument:utilityWheel'
        GNIRS = 'instrument:slitWidth'
        GMOSN = 'instrument:fpu'
        GPI = 'instrument:observingMode'
        F2 = 'instrument:fpu'
        GMOSS = 'instrument:fpu'
        NIRI = 'instrument:mask'
        NIFS = 'instrument:mask'
        CUSTOM = 'instrument:fpuCustomMask'

    class _InstrumentKeys:
        NAME = 'instrument:name'
        DECKER = 'instrument:acquisitionMirror'
        ACQ_MIRROR = 'instrument:acquisitionMirror'
        CROSS_DISPERSED = 'instrument:crossDispersed'

    FPU_FOR_INSTRUMENT = {'GSAOI': _FPUKeys.GSAOI,
                          'GPI': _FPUKeys.GPI,
                          'Flamingos2': _FPUKeys.F2,
                          'NIFS': _FPUKeys.NIFS,
                          'GNIRS': _FPUKeys.GNIRS,
                          'GMOS-N': _FPUKeys.GMOSN,
                          'GMOS-S': _FPUKeys.GMOSS,
                          'NIRI': _FPUKeys.NIRI}

    # An empty base target for when the target environment is empty for an Observation.
    _EMPTY_BASE_TARGET = SiderealTarget(
        name=TargetName('Empty'),
        magnitudes=frozenset(),
        type=TargetType.BASE,
        ra=0,
        dec=0,
        pm_ra=0,
        pm_dec=0,
        epoch=2000.0
    )

    def __init__(self,
                 obs_classes: FrozenSet[ObservationClass],
                 sources: Sources):
        super().__init__(obs_classes, sources)

    @staticmethod
    def parse_notes_split(notes: Iterable[Tuple[str, str]]) -> bool:
        """Search note title and content strings for instructions on not splitting observations
           Returns a boolean indicating whether the observation can be split.
           notes: list of note tuples,  [(title, text), (title, text),...]"""
        # Search for any indications in the note that an observation cannot be split.
        for note in notes:
            title, content = note
            if title is not None:
                title_lower = title.lower()
                if any(s in title_lower for s in OcsProgramProvider._NO_SPLIT_STRINGS):
                    return False
            if content is not None:
                content_lower = content.lower()
                if any(s in content_lower for s in OcsProgramProvider._NO_SPLIT_STRINGS):
                    return False
        return True

    def parse_magnitude(self, data: dict) -> Magnitude:
        band = MagnitudeBands[data[OcsProgramProvider._MagnitudeKeys.NAME]]
        value = data[OcsProgramProvider._MagnitudeKeys.VALUE]
        return Magnitude(
            band=band,
            value=value,
            error=None)

    @staticmethod
    def _get_program_dates(program_type: ProgramTypes,
                           program_id: ProgramID,
                           note_titles: List[str]) -> Tuple[datetime, datetime]:
        """
        Find the start and end dates of a program.
        This requires special handling for FT programs, which must contain a note with the information
        at the program level with key INFO_SCHEDNOTE, INFO_PROGRAMNOTE, or INFO_NOTE.
        """
        year_str = program_id.id[3:7]
        try:
            year = int(year_str)
        except ValueError as e:
            msg = f'Illegal year specified for program {program_id}: {year_str}.'
            raise ValueError(e, msg)
        except TypeError as e:
            msg = f'Illegal type data specified for program {program_id}: {year_str}.'
            raise TypeError(e, msg)
        next_year = year + 1

        # Make sure the actual year is in the valid range.
        if year < 2000 or year > 2100:
            msg = f'Illegal year specified for program {program_id}: {year_str}.'
            raise ValueError(msg)

        half_char = program_id.id[7]
        try:
            semester = SemesterHalf(half_char)
        except ValueError as e:
            msg = f'Illegal semester specified for program {program_id}: {half_char}'
            raise ValueError(msg, e)

        # Special handling for FT programs.
        if program_type is ProgramTypes.FT:
            months_list = [x.lower() for x in calendar.month_name[1:]]

            def is_ft_note(curr_note_title: str) -> bool:
                """
                Determine if the note is a note with title information for a FT program.
                """
                if curr_note_title is None:
                    return False
                curr_note_title = curr_note_title.lower()
                return 'cycle' in curr_note_title or 'active' in curr_note_title

            def month_number(month: str, months: List[str]) -> int:
                month = month.lower()
                return [i for i, m in enumerate(months) if month in m].pop() + 1

            def parse_dates(curr_note_title: str) -> Optional[Tuple[datetime, datetime]]:
                """
                Using the information in a note title, try to determine the start and end dates
                for a FT program.

                The month information in the note title can be of the forms:
                * MON-MON-MON
                * Month, Month, and Month
                and have additional data / spacing following.

                Raises an IndexError if there are any issues in getting the months.
                """
                # Convert month data as above to a list of months.
                curr_note_months = curr_note_title.strip().replace('and ', ' ').replace('  ', ' ').replace(', ', '-'). \
                    split(' ')[-1].lower()
                month_list = [month for month in curr_note_months.split('-') if month in months_list]
                m1 = month_number(month_list[0], months_list)
                m2 = month_number(month_list[-1], months_list)

                if semester == SemesterHalf.B and m1 < 6:
                    program_start = datetime(next_year, m1, 1)
                    program_end = datetime(next_year, m2, calendar.monthrange(next_year, m2)[1])
                else:
                    program_start = datetime(year, m1, 1)
                    if m2 > m1:
                        program_end = datetime(year, m2, calendar.monthrange(year, m2)[1])
                    else:
                        program_end = datetime(next_year, m2, calendar.monthrange(next_year, m2)[1])
                return program_start, program_end

            # Find the note (if any) that contains the information.
            note_title = next(filter(is_ft_note, note_titles), None)
            if note_title is None:
                msg = f'Fast turnaround program {id} has no note containing start / end date information.'
                raise ValueError(msg)

            # Parse the month information.
            try:
                date_info = parse_dates(note_title)

            except IndexError as e:
                msg = f'Fast turnaround program {id} note title has improper form: {note_title}.'
                raise ValueError(e, msg)

            start_date, end_date = date_info

        else:
            # Not a FT program, so handle normally.
            start_date = datetime(year, 8, 1)
            end_date = datetime(next_year, 1, 31)

        # Account for the flexible boundary on programs.
        return start_date - Program.FUZZY_BOUNDARY, end_date + Program.FUZZY_BOUNDARY

    def parse_timing_window(self, data: dict) -> TimingWindow:
        start = datetime.utcfromtimestamp(data[OcsProgramProvider._TimingWindowKeys.START] / 1000.0)

        duration_info = data[OcsProgramProvider._TimingWindowKeys.DURATION]
        if duration_info == TimingWindow.INFINITE_DURATION_FLAG:
            duration = TimingWindow.INFINITE_DURATION
        else:
            duration = timedelta(milliseconds=duration_info)

        repeat_info = data[OcsProgramProvider._TimingWindowKeys.REPEAT]
        if repeat_info == TimingWindow.FOREVER_REPEATING:
            repeat = TimingWindow.OCS_INFINITE_REPEATS
        else:
            repeat = repeat_info

        if repeat == TimingWindow.NON_REPEATING:
            period = None
        else:
            period = timedelta(milliseconds=data[OcsProgramProvider._TimingWindowKeys.PERIOD])

        return TimingWindow(
            start=start,
            duration=duration,
            repeat=repeat,
            period=period)

    def parse_conditions(self, data: dict) -> Conditions:
        def to_value(cond: str) -> float:
            """
            Parse the conditions value as a float out of the string passed by the OCS program extractor.
            """
            value = cond.split('/')[0].split('%')[0]
            try:
                return 1.0 if value == 'Any' else float(value) / 100
            except (ValueError, TypeError) as e:
                # Either of these will just be a ValueError.
                msg = f'Illegal value for constraint: {value}'
                raise ValueError(e, msg)

        return Conditions(
            *[lookup(to_value(data[key])) for lookup, key in
              [(CloudCover, OcsProgramProvider._ConstraintKeys.CC),
               (ImageQuality, OcsProgramProvider._ConstraintKeys.IQ),
               (SkyBackground, OcsProgramProvider._ConstraintKeys.SB),
               (WaterVapor, OcsProgramProvider._ConstraintKeys.WV)]])

    def parse_constraints(self, data: dict) -> Constraints:
        # Get the conditions
        conditions = self.parse_conditions(data)

        # Parse the timing windows.
        timing_windows = [self.parse_timing_window(tw_data)
                          for tw_data in data[OcsProgramProvider._ConstraintKeys.TIMING_WINDOWS]]

        # Get the elevation data.
        elevation_type_data = data[OcsProgramProvider._ConstraintKeys.ELEVATION_TYPE].replace(' ', '_').upper()
        elevation_type = ElevationType[elevation_type_data]
        elevation_min = data[OcsProgramProvider._ConstraintKeys.ELEVATION_MIN]
        elevation_max = data[OcsProgramProvider._ConstraintKeys.ELEVATION_MAX]

        return Constraints(
            conditions=conditions,
            elevation_type=elevation_type,
            elevation_min=elevation_min,
            elevation_max=elevation_max,
            timing_windows=timing_windows,
            strehl=None)

    def _parse_target_header(self, data: dict) -> Tuple[TargetName, set[Magnitude], TargetType]:
        """
        Parse the common target header information out of a target.
        """
        name = TargetName(data[OcsProgramProvider._TargetKeys.NAME])
        magnitude_data = data.setdefault(OcsProgramProvider._TargetKeys.MAGNITUDES, [])
        magnitudes = {self.parse_magnitude(m) for m in magnitude_data}

        target_type_data = data[OcsProgramProvider._TargetKeys.TYPE].replace('-', '_').replace(' ', '_').upper()
        try:
            target_type = TargetType[target_type_data]
        except KeyError as e:
            msg = f'Target {name} has illegal type {target_type_data}.'
            raise KeyError(e, msg)

        return name, magnitudes, target_type

    def parse_sidereal_target(self, data: dict) -> SiderealTarget:
        name, magnitudes, target_type = self._parse_target_header(data)
        ra_hhmmss = data[OcsProgramProvider._TargetKeys.RA]
        dec_ddmmss = data[OcsProgramProvider._TargetKeys.DEC]

        # TODO: Is this the proper way to handle conversions from hms and dms?
        ra = sex2dec(ra_hhmmss, todegree=True)
        dec = dmsstr2deg(dec_ddmmss)

        pm_ra = data.setdefault(OcsProgramProvider._TargetKeys.DELTA_RA, 0.0)
        pm_dec = data.setdefault(OcsProgramProvider._TargetKeys.DELTA_DEC, 0.0)
        epoch = data.setdefault(OcsProgramProvider._TargetKeys.EPOCH, 2000)

        return SiderealTarget(
            name=name,
            magnitudes=frozenset(magnitudes),
            type=target_type,
            ra=ra,
            dec=dec,
            pm_ra=pm_ra,
            pm_dec=pm_dec,
            epoch=epoch)

    def parse_nonsidereal_target(self, data: dict) -> NonsiderealTarget:
        """
        TODO: Retrieve the Ephemeris data.
        TODO: Should we be doing this here, or in the Collector?
        """
        name, magnitudes, target_type = self._parse_target_header(data)
        des = data[OcsProgramProvider._TargetKeys.DES]
        tag = data[OcsProgramProvider._TargetKeys.TAG]

        # TODO: ra and dec are last two parameters. Fill here or elsewhere?
        return NonsiderealTarget(
            name=name,
            magnitudes=frozenset(magnitudes),
            type=target_type,
            des=des,
            tag=tag,
            ra=np.empty([]),
            dec=np.empty([]))

    @staticmethod
    def _parse_instrument_configuration(data: dict, instrument: str) \
            -> Tuple[Optional[str], Optional[str], Optional[str], Optional[Wavelength]]:
        """
        A dict is return until the Instrument configuration model is created
        """

        def find_filter(filter_input: str, filter_dict: Mapping[str, float]) -> Optional[str]:
            return next(filter(lambda f: f in filter_input, filter_dict), None)

        if instrument == 'Visitor Instrument':
            instrument = data[OcsProgramProvider._InstrumentKeys.NAME].split(' ')[0]
            if instrument in ["'Alopeke", "Zorro"]:
                fpu = None
            else:
                fpu = instrument
        else:
            if instrument in OcsProgramProvider.FPU_FOR_INSTRUMENT:
                if OcsProgramProvider._FPUKeys.CUSTOM in data:
                    # This will assign the MDF name to the FPU
                    fpu = data[OcsProgramProvider._FPUKeys.CUSTOM]
                elif OcsProgramProvider.FPU_FOR_INSTRUMENT[instrument] in data:
                    fpu = data[OcsProgramProvider.FPU_FOR_INSTRUMENT[instrument]]
                else:
                    # TODO: Might need to raise an exception here. Check code with science.
                    fpu = None
            else:
                raise ValueError(f'Instrument {instrument} not supported')

        if OcsProgramProvider._AtomKeys.DISPERSER in data:
            disperser = data[OcsProgramProvider._AtomKeys.DISPERSER]
        elif instrument in ['IGRINS', 'MAROON-X']:
            disperser = instrument
        else:
            disperser = None

        if instrument == 'GNIRS':
            if (data[OcsProgramProvider._InstrumentKeys.ACQ_MIRROR] == 'in'
                    and data[OcsProgramProvider._InstrumentKeys.DECKER] == 'acquisition'):
                disperser = 'mirror'
            else:
                disperser = disperser.replace('grating', '') + data[OcsProgramProvider._InstrumentKeys.CROSS_DISPERSED]
        elif instrument == 'Flamingos2' and fpu == 'FPU_NONE':
            if data['instrument:decker'] == 'IMAGING':
                disperser = data['instrument:decker']

        if OcsProgramProvider._AtomKeys.FILTER in data:
            filt = data[OcsProgramProvider._AtomKeys.FILTER]
        elif instrument == 'GPI':
            filt = find_filter(fpu, OcsProgramProvider._GPI_FILTER_WAVELENGTHS)
        else:
            if instrument == 'GNIRS':
                filt = None
            else:
                filt = 'Unknown'
        if instrument == 'NIFS' and 'Same as Disperser' in filt:
            filt = find_filter(disperser[0], OcsProgramProvider._NIFS_FILTER_WAVELENGTHS)
        wavelength = Wavelength(OcsProgramProvider._GPI_FILTER_WAVELENGTHS[filt] if instrument == 'GPI'
                                else float(data[OcsProgramProvider._AtomKeys.WAVELENGTH]))

        # Identify GRACES - ToDo: check if needed
        # if instrument == 'GMOS-N' and fpu == 'IFU Left Slit (blue)':
        #     instrument = 'GRACES'
        #     if 'DS920' in filt:
        #         fpu = '1-fiber'
        #     elif 'HeIIC' in filt:
        #         fpu = '2-fiber'

        return fpu, disperser, filt, wavelength

    def parse_atoms(self, site: Site, sequence: List[dict], qa_states: List[QAState], split: bool) -> List[Atom]:
        """
        Atom handling logic.
        """
        # The different configurations that make up the instrument.
        fpus = []
        dispersers = []
        filters = []
        wavelengths = []

        def guide_state(guide_step: dict) -> bool:
            return any('guideWith' in key and guide == 'guide' for key, guide in guide_step.items())

        def search_list(val, alist):
            return any(val in elem for elem in alist)

        def determine_mode(inst: str) -> ObservationMode:

            # print(f'inst: {inst} dispersers: {dispersers}')
            # print(f'\t fpus: {fpus}')
            obs_mode = ObservationMode.UNKNOWN
            if 'GMOS' in inst:
                if 'Mirror' in dispersers or 'MIRROR' in dispersers:
                    obs_mode = ObservationMode.IMAGING
                elif search_list('arcsec', fpus):
                    obs_mode = ObservationMode.LONGSLIT
                elif search_list('IFU', fpus):
                    obs_mode = ObservationMode.IFU
                elif search_list('G', fpus):
                    obs_mode = ObservationMode.MOS
            elif inst in ["GSAOI", "'Alopeke", "Zorro"]:
                obs_mode = ObservationMode.IMAGING
            elif inst in ['IGRINS', 'MAROON-X']:
                obs_mode = ObservationMode.LONGSLIT
            elif inst in ['GHOST', 'MAROON-X', 'GRACES', 'Phoenix']:
                obs_mode = ObservationMode.XD
            elif inst == 'Flamingos2':
                if search_list('LONGSLIT', fpus):
                    obs_mode = ObservationMode.LONGSLIT
                elif search_list('IMAGING', dispersers):
                    obs_mode = ObservationMode.IMAGING
            elif inst == 'NIRI':
                if search_list('NONE', dispersers) and search_list('MASK_IMAGING', fpus):
                    obs_mode = ObservationMode.IMAGING
            elif inst == 'NIFS':
                obs_mode = ObservationMode.IFU
            elif inst == 'GNIRS':
                if search_list('mirror', dispersers):
                    obs_mode = ObservationMode.IMAGING
                elif search_list('XD', dispersers):
                    obs_mode = ObservationMode.XD
                else:
                    obs_mode = ObservationMode.LONGSLIT
            elif inst == 'GPI':
                if search_list('CORON', fpus):
                    obs_mode = ObservationMode.CORON
                elif search_list('NRM', fpus):
                    obs_mode = ObservationMode.NRM
                elif search_list('DIRECT', fpus):
                    obs_mode = ObservationMode.IMAGING
                else:
                    obs_mode = ObservationMode.IFU
            return obs_mode

        def autocorr_lag(x):
            """
            Test for patterns with auto-correlation
            """
            # Auto correlation
            result = np.correlate(x, x, mode='full')
            corrmax = np.max(result)
            if corrmax != 0.0:
                result /= corrmax
            peaks, _ = find_peaks(result[result.size // 2:], height=(0, None), prominence=(0.25, None))
            return peaks[0] if len(peaks) > 0 else 0

        n_atom = 0
        atom_id = 0
        classes = []
        guiding = []
        atoms = []

        p_offsets = []
        q_offsets = []
        sky_p_offsets = []
        sky_q_offsets = []

        exposure_times = []
        coadds = []
        do_not_split = not split
        # print(f'\t\t\t do_not_split: {do_not_split}')

        # all atoms must have the same instrument
        instrument = sequence[0][OcsProgramProvider._AtomKeys.INSTRUMENT]
        for step in sequence:

            # Instrument configuration aka Resource.
            fpu, disperser, filt, wavelength = OcsProgramProvider._parse_instrument_configuration(step, instrument)

            # If FPU is None, 'None', or FPU_NONE, which are effectively the same thing, we ignore.
            if fpu is not None and fpu != 'None' and fpu != 'FPU_NONE':
                fpus.append(fpu)
            dispersers.append(disperser)
            if filt and filt != 'None':
                filters.append(filt)
            wavelengths.append(wavelength)

            p = 0.0
            q = 0.0

            # Exposures on sky for dither pattern analysis
            if step[OcsProgramProvider._AtomKeys.OBSERVE_TYPE].upper() not in OcsProgramProvider._OBSERVE_TYPES:
                p = (float(step[OcsProgramProvider._AtomKeys.OFFSET_P]) if OcsProgramProvider._AtomKeys.OFFSET_P in step
                     else 0.0)
                q = (float(step[OcsProgramProvider._AtomKeys.OFFSET_Q]) if OcsProgramProvider._AtomKeys.OFFSET_Q in step
                     else 0.0)
                sky_p_offsets.append(p)
                sky_q_offsets.append(q)
            coadds.append(int(step[OcsProgramProvider._AtomKeys.COADDS])
                          if OcsProgramProvider._AtomKeys.COADDS in step else 1)
            exposure_times.append(step[OcsProgramProvider._AtomKeys.EXPOSURE_TIME])
            p_offsets.append(p)
            q_offsets.append(q)

        # Transform Resources.
        # TODO: For now, we focus on instruments, and GMOS FPUs and dispersers exclusively.
        instrument_resources = frozenset([self._sources.origin.resource.lookup_resource(instrument)])
        if 'GMOS' in instrument:
            # Convert FPUs and dispersers to barcodes. Note that None might be contained in some of these
            # sets, but we filter below to remove them.
            fpu_resources = frozenset([self._sources.origin.resource.fpu_to_barcode(site, fpu, instrument)
                                       for fpu in fpus])
            disperser_resources = frozenset([self._sources.origin.resource.lookup_resource(disperser.split('_')[0])
                                             for disperser in dispersers])
            resources = frozenset([r for r in fpu_resources | disperser_resources | instrument_resources])
        else:
            resources = instrument_resources

        # Remove the None values.
        resources = frozenset([res for res in resources if res is not None])
        mode = determine_mode(instrument)
        # For now we do not split NIR spectroscopy
        if (mode != ObservationMode.IMAGING and
                instrument in ['GPI', 'GNIRS', 'NIFS', 'IGRINS', 'Flamingos2', 'Phoenix']):
            do_not_split = True

        # Analyze sky offset patterns using auto-correlation
        # The lag is the length of any pattern, 0 means no repeating pattern
        p_lag = 0
        q_lag = 0
        if do_not_split:
            offset_lag = len(sequence)
        else:
            if len(sky_p_offsets) > 1:
                p_lag = autocorr_lag(np.array(sky_p_offsets))
            if len(sky_q_offsets) > 1:
                q_lag = autocorr_lag(np.array(sky_q_offsets))
            # Special cases
            if p_lag == 0 and q_lag == 0 and len(sky_q_offsets) == 4:
                # single ABBA pattern, which the auto-correlation won't find
                if sky_q_offsets[0] == sky_q_offsets[3] and sky_q_offsets == sky_q_offsets[2]:
                    q_lag = 4
            elif len(sky_q_offsets) == 2:
                # If only two steps, put them together, might be AB, also silly to split only two steps
                q_lag = 2

            offset_lag = q_lag
            if p_lag > 0 and p_lag != q_lag:
                offset_lag = 0
        # Group by changes in exptimes / coadds?
        exp_time_groups = False
        n_offsets = 0
        n_pattern = offset_lag
        prev = -1
        for step_id, step in enumerate(sequence):
            next_atom = False

            observe_class = step[OcsProgramProvider._AtomKeys.OBS_CLASS]
            step_time = step[OcsProgramProvider._AtomKeys.TOTAL_TIME] / 1000

            # Any wavelength/filter change is a new atom
            if step_id == 0 or (step_id > 0 and wavelengths[step_id] != wavelengths[step_id - 1]):
                next_atom = True
                # logger.info('Atom for wavelength change')
                # print(f'\t\t\t Atom for wavelength change')

            # A change in exposure time or coadds is a new atom for science exposures
            # print(f'\t\t\t {step[OcsProgramProvider._AtomKeys.OBSERVE_TYPE].upper()}')
            if step[OcsProgramProvider._AtomKeys.OBSERVE_TYPE].upper() not in OcsProgramProvider._OBSERVE_TYPES:
                if (prev >= 0 and observe_class.upper() == ObservationClass.SCIENCE.name and step_id > 0 and
                        (exposure_times[step_id] != exposure_times[prev] or coadds[step_id] != coadds[prev])):
                    next_atom = True
                    # logger.info('Atom for exposure time change')
                    # print(f'\t\t\t Atom for exposure time change')

                # Offsets - a new offset pattern is a new atom
                if offset_lag != 0 or not exp_time_groups:
                    # For NIR imaging, need to have at least two offset positions if no repeating pattern
                    # New atom after every 2nd offset (noffsets is odd)
                    if mode is ObservationMode.IMAGING and offset_lag == 0 and all(w > 1.0 for w in wavelengths):
                        if step_id == 0:
                            n_offsets += 1
                        else:
                            if p_offsets[step_id] != p_offsets[prev] or q_offsets[step_id] != q_offsets[prev]:
                                n_offsets += 1
                        if n_offsets % 2 == 1:
                            next_atom = True
                            # logger.info('Atom for offset pattern')
                            # print('Atom for offset pattern')
                    else:
                        n_pattern -= 1
                        if n_pattern < 0:
                            next_atom = True
                            # logger.info('Atom for exposure time change')
                            # print('Atom for offset pattern')
                            n_pattern = offset_lag - 1
                prev = step_id

            # New atom entry
            if next_atom:
                # Get class, qastate, guiding for previous atom
                if n_atom > 0:
                    previous_atom = atoms[-1]
                    previous_atom.qa_state = min(qa_states, default=QAState.NONE)
                    if previous_atom.qa_state is not QAState.NONE:
                        previous_atom.observed = True
                    previous_atom.resources = resources
                    previous_atom.guide_state = any(guiding)
                    previous_atom.wavelengths = frozenset(wavelengths)

                n_atom += 1
                # print(f'\t\t\t n_atom = {n_atom}')

                # Convert all the different components into Resources.
                classes = []
                guiding = []
                atoms.append(Atom(id=atom_id,
                                  exec_time=ZeroTime,
                                  prog_time=ZeroTime,
                                  part_time=ZeroTime,
                                  program_used=ZeroTime,
                                  partner_used=ZeroTime,
                                  observed=False,
                                  qa_state=QAState.NONE,
                                  guide_state=False,
                                  resources=resources,
                                  wavelengths=frozenset(wavelengths),
                                  obs_mode=mode))

                if (step[OcsProgramProvider._AtomKeys.OBSERVE_TYPE].upper() not in OcsProgramProvider._OBSERVE_TYPES and
                        n_pattern == 0):
                    n_pattern = offset_lag
                n_offsets = 1

            # Update atom
            classes.append(observe_class)
            guiding.append(guide_state(step))

            atoms[-1].exec_time += timedelta(seconds=step_time)
            atom_id = n_atom

            # TODO: Add Observe Class enum  
            if 'partnerCal' in observe_class:
                atoms[-1].part_time += timedelta(seconds=step_time)
            else:
                atoms[-1].prog_time += timedelta(seconds=step_time)

        if n_atom > 0:
            previous_atom = atoms[-1]
            previous_atom.qa_state = min(qa_states, default=QAState.NONE)
            if previous_atom.qa_state is not QAState.NONE:
                previous_atom.observed = True
            previous_atom.resources = resources
            previous_atom.guide_state = any(guiding)
            previous_atom.wavelengths = frozenset(wavelengths)

        return atoms

    def parse_target(self, data: dict) -> Target:
        """
        Parse a general target - either sidereal or nonsidereal - from the supplied data.
        If we are a ToO, we don't have a target, and thus we don't have a tag. Thus, this raises a KeyError.
        """
        tag = data[OcsProgramProvider._TargetKeys.TAG]
        if tag == 'sidereal':
            return self.parse_sidereal_target(data)
        elif tag == 'nonsidereal':
            return self.parse_nonsidereal_target(data)
        else:
            msg = f'Illegal target tag type: {tag}.'
            raise ValueError(msg)

    def parse_observation(self,
                          data: dict,
                          num: int,
                          program_id: ProgramID,
                          split: bool) -> Optional[Observation]:
        """
        In the current list of observations, we are parsing the data for:
        OBSERVATION_BASIC-{num}. Note that these numbers ARE in the correct order
        for scheduling groups, so we should sort on the OBSERVATION_BASIC-{num}
        keys prior to doing the parsing.
        """
        # Check the obs_class. If it is illegal, return None.
        # At the same time, ignore inactive observations.
        obs_id = data[OcsProgramProvider._ObsKeys.ID]
        # print(f'\t\t {obs_id}')
        active = data[OcsProgramProvider._ObsKeys.PHASE2] != 'Inactive'
        if not active:
            logger.warning(f"Observation {obs_id} is inactive (skipping).")
            return None

        obs_class = ObservationClass[data[OcsProgramProvider._ObsKeys.OBS_CLASS].upper()]
        if obs_class not in self._obs_classes or not active:
            logger.warning(f'Observation {obs_id} not in a specified class (skipping): {obs_class.name}.')
            return None

        # By default, assume ToOType of None unless otherwise indicated.
        too_type: Optional[TooType] = None

        internal_id = data[OcsProgramProvider._ObsKeys.INTERNAL_ID]
        title = data[OcsProgramProvider._ObsKeys.TITLE]
        site = Site[data[OcsProgramProvider._ObsKeys.ID].split('-')[0]]
        status = ObservationStatus[data[OcsProgramProvider._ObsKeys.STATUS].upper()]
        priority = Priority[data[OcsProgramProvider._ObsKeys.PRIORITY].upper()]

        # If the status is not legal, terminate parsing.
        if status not in OcsProgramProvider._OBSERVATION_STATUSES:
            return None

        setuptime_type = SetupTimeType[data[OcsProgramProvider._ObsKeys.SETUPTIME_TYPE]]
        acq_overhead = timedelta(milliseconds=data[OcsProgramProvider._ObsKeys.SETUPTIME])

        find_constraints = [data[key] for key in data.keys() if key.startswith(OcsProgramProvider._ConstraintKeys.KEY)]
        constraints = self.parse_constraints(find_constraints[0]) if find_constraints else None

        # TODO: Do we need this? It is being passed to the parse_atoms method.
        # TODO: We have a qaState on the Observation as well.
        qa_states = [QAState[log_entry[OcsProgramProvider._ObsKeys.QASTATE].upper()] for log_entry in
                     data[OcsProgramProvider._ObsKeys.LOG]]

        # Parse notes for "do not split" information if not found previously
        if split:
            notes = [(data[key][OcsProgramProvider._NoteKeys.TITLE], data[key][OcsProgramProvider._NoteKeys.TEXT])
                     for key in data.keys() if key.startswith(OcsProgramProvider._ProgramKeys.NOTE)]
            split = self.parse_notes_split(notes)

        atoms = self.parse_atoms(site, data[OcsProgramProvider._ObsKeys.SEQUENCE], qa_states, split=split)
        # exec_time = sum([atom.exec_time for atom in atoms], ZeroTime) + acq_overhead
        # for atom in atoms:
        #     print(f'\t\t\t {atom.id} {atom.exec_time} {atom.obs_mode}')

        # TODO: Should this be a list of all targets for the observation?
        targets = []

        # Get the target environment. Each observation should have exactly one, but the name will
        # not necessarily be predictable as we number them.
        guiding = {}
        target_env_keys = [key for key in data.keys() if key.startswith(OcsProgramProvider._TargetKeys.KEY)]
        if len(target_env_keys) > 1:
            raise ValueError(f'Observation {obs_id} has multiple target environments. Cannot process.')

        if not target_env_keys:
            # No target environment. Use the empty target.
            logger.warning(f'No target environment found for observation {obs_id}. Using empty base target.')
            targets.append(OcsProgramProvider._EMPTY_BASE_TARGET)

        else:
            # Process the target environment.
            target_env = data[target_env_keys[0]]

            # Get the base.
            try:
                base = self.parse_target(target_env[OcsProgramProvider._TargetKeys.BASE])
                targets.append(base)
            except KeyError:
                logger.warning(f"No base target found for observation {obs_id}. Using empty base target.")
                targets.append(OcsProgramProvider._EMPTY_BASE_TARGET)

            # Parse the guide stars if guide star data is supplied.
            # We are only interested in the auto guide group, or the primary guide group if there
            # is not the auto guide group.
            try:
                guide_groups = target_env[OcsProgramProvider._TargetEnvKeys.GUIDE_GROUPS]
                auto_guide_group = [group for group in guide_groups
                                    if group[OcsProgramProvider._TargetEnvKeys.GUIDE_GROUP_NAME] ==
                                    OcsProgramProvider._TargetEnvKeys.AUTO_GROUP]
                primary_guide_group = [group for group in guide_groups
                                       if group[OcsProgramProvider._TargetEnvKeys.GUIDE_GROUP_PRIMARY]]

                guide_group = None
                if auto_guide_group:
                    if len(auto_guide_group) > 1:
                        raise ValueError(f'Multiple auto guide groups found for {obs_id}.')
                    guide_group = auto_guide_group[0]
                elif primary_guide_group:
                    if len(primary_guide_group) > 1:
                        raise ValueError(f'Multiple primary guide groups found for {obs_id}.')
                    guide_group = primary_guide_group[0]

                # Now we parse out the guideProbe list, which contains the information about the
                # guide probe keys and the targets.
                if guide_group is not None:
                    for guide_data in guide_group[OcsProgramProvider._TargetEnvKeys.GUIDE_PROBE]:
                        guider = guide_data[OcsProgramProvider._TargetEnvKeys.GUIDE_PROBE_KEY]
                        # TODO: We don't have guiders as resources in ResourceMock.
                        resource = Resource(id=guider)
                        target = self.parse_target(guide_data[OcsProgramProvider._TargetEnvKeys.TARGET])
                        guiding[resource] = target
                        targets.append(target)

            except KeyError:
                logger.warning(f'No guide group data found for observation {obs_id}')

            # Process the user targets.
            user_targets_data = target_env.setdefault(OcsProgramProvider._TargetEnvKeys.USER_TARGETS, [])
            for user_target_data in user_targets_data:
                user_target = self.parse_target(user_target_data)
                targets.append(user_target)

            # If the ToO override rapid setting is in place, set to RAPID.
            # Otherwise, set as None, and we will propagate down from the groups.
            if (OcsProgramProvider._ObsKeys.TOO_OVERRIDE_RAPID in data and
                    data[OcsProgramProvider._ObsKeys.TOO_OVERRIDE_RAPID]):
                too_type = TooType.RAPID

        return GeminiObservation(
            id=ObservationID(obs_id),
            internal_id=internal_id,
            order=num,
            title=title,
            site=site,
            status=status,
            active=active,
            priority=priority,
            setuptime_type=setuptime_type,
            acq_overhead=acq_overhead,
            obs_class=obs_class,
            targets=targets,
            guiding=guiding,
            sequence=atoms,
            constraints=constraints,
            belongs_to=program_id,
            too_type=too_type
        )

    def parse_time_allocation(self, data: dict) -> TimeAllocation:
        category = TimeAccountingCode(data[OcsProgramProvider._TAKeys.CATEGORY])
        program_awarded = timedelta(milliseconds=data[OcsProgramProvider._TAKeys.AWARDED_PROG_TIME])
        partner_awarded = timedelta(milliseconds=data[OcsProgramProvider._TAKeys.AWARDED_PART_TIME])
        program_used = timedelta(milliseconds=data[OcsProgramProvider._TAKeys.USED_PROG_TIME])
        partner_used = timedelta(milliseconds=data[OcsProgramProvider._TAKeys.USED_PART_TIME])

        return TimeAllocation(
            category=category,
            program_awarded=program_awarded,
            partner_awarded=partner_awarded,
            program_used=program_used,
            partner_used=partner_used)

    def parse_or_group(self, data: dict, program_id: ProgramID, group_id: GroupID) -> OrGroup:
        """
        There are no OR groups in the OCS, so this method simply throws a
        NotImplementedError if it is called.
        """
        raise NotImplementedError('OCS does not support OR groups.')

    def parse_and_group(self, data: dict, program_id: ProgramID, group_id: GroupID, split: bool) -> Optional[AndGroup]:
        """
        In the OCS, a SchedulingFolder or a program are AND groups.
        We do not allow nested groups in OCS, so this is relatively easy.

        This method expects the data from a SchedulingFolder or from the program.

        Organizational folders are ignored, so they require some special handling:
        we retrieve all the observations here that are in organizational folders and
        simply stick them in this level.
        """
        delay_min = timedelta.min
        delay_max = timedelta.max

        # Get the group name: ROOT_GROUP_ID if the root group and otherwise the name.
        if OcsProgramProvider._GroupKeys.GROUP_NAME in data:
            group_name = data[OcsProgramProvider._GroupKeys.GROUP_NAME]
        else:
            group_name = ROOT_GROUP_ID.id
        # print(f'Group: {group_name}')

        # Parse notes for "do not split" information if not found previously
        if split:
            notes = [(data[key][OcsProgramProvider._NoteKeys.TITLE], data[key][OcsProgramProvider._NoteKeys.TEXT])
                     for key in data.keys() if key.startswith(OcsProgramProvider._ProgramKeys.NOTE)]
            split = self.parse_notes_split(notes)

        # Collect all the children of this group.
        children = []

        # Parse out the scheduling groups recursively.
        scheduling_group_keys = sorted(key for key in data
                                       if key.startswith(OcsProgramProvider._GroupKeys.SCHEDULING_GROUP))
        for key in scheduling_group_keys:
            subgroup_id = GroupID(key.split('-')[-1])
            subgroup = self.parse_and_group(data[key], program_id, subgroup_id, split=split)
            if subgroup is not None:
                children.append(subgroup)

        # Grab the observation data from the complete data.
        top_level_obs_data = [(key, data[key]) for key in data
                              if key.startswith(OcsProgramProvider._ObsKeys.KEY)]

        # Grab the observation data from any organizational folders.
        org_folders = [data[key] for key in data
                       if key.startswith(OcsProgramProvider._GroupKeys.ORGANIZATIONAL_FOLDER)]
        org_folders_obs_data = [(key, of[key]) for of in org_folders
                                for key in of if key.startswith(OcsProgramProvider._ObsKeys.KEY)]

        # If we have organizational folders and there is any overlap between:
        # 1. the top level observation keys; and
        # 2. the organizational folder keys
        # then terminate immediately.
        if len(org_folders):
            top_level_obs_data_keys = {key for key, _ in top_level_obs_data}
            org_folder_obs_data_keys = {key for key, _ in org_folders_obs_data}
            repeated_keys = top_level_obs_data_keys.intersection(org_folder_obs_data_keys)
            if repeated_keys:
                repeated_key_str = ', '.join(repeated_keys)
                raise RuntimeError(f'Repeated keys in program {program_id}: {repeated_key_str}. Cannot continue.')

        obs_data_blocks = top_level_obs_data + org_folders_obs_data

        # Parse out all the top level observations in this group.
        # Only observations that are valid, active, and have on acceptable obs_class will be returned.
        observations = []
        for obs_key, obs_data in obs_data_blocks:
            obs_num = int(obs_key.split('-')[-1])
            obs = self.parse_observation(obs_data, obs_num, program_id, split=split)
            if obs is not None:
                observations.append(obs)

        # Put all the observations in trivial AND groups and extend the children to include them.
        trivial_groups = [
            AndGroup(
                id=GroupID(obs.id.id),
                program_id=program_id,
                group_name=obs.title,
                number_to_observe=1,
                delay_min=delay_min,
                delay_max=delay_max,
                children=obs,
                group_option=AndOption.ANYORDER)
            for obs in observations]
        children.extend(trivial_groups)

        # If there are no children to observe, terminate with None
        number_to_observe = len(children)
        if number_to_observe == 0:
            logger.warning(f"Program {program_id} group {group_id} has no candidate children. Skipping.")
            return None

        # Put all the observations in the one big AND group and return it.
        return AndGroup(
            id=group_id,
            program_id=program_id,
            group_name=group_name,
            number_to_observe=number_to_observe,
            delay_min=delay_min,
            delay_max=delay_max,
            children=children,
            # TODO: Should this be ANYORDER OR CONSEC_ORDERED?
            group_option=AndOption.CONSEC_ORDERED)

    def parse_program(self, data: dict) -> Optional[Program]:
        """
        Parse the program-level details from the JSON data.

        1. The root group is always an AND group with any order.
        2. The scheduling groups are AND groups with any order.
        3. The organizational folders are ignored and their observations are considered top-level.
        4. Each observation goes in its own AND group of size 1 as per discussion.
        """
        program_id = ProgramID(data[OcsProgramProvider._ProgramKeys.ID])
        # print(program_id)
        internal_id = data[OcsProgramProvider._ProgramKeys.INTERNAL_ID]

        # # Get all the note information as they may contain FT scheduling data comments.
        note_titles = [data[key][OcsProgramProvider._NoteKeys.TITLE] for key in data.keys()
                       if key.startswith(OcsProgramProvider._ProgramKeys.NOTE)]

        # Initialize split variable, split observations by default
        split = True

        # Now we parse the groups. For this, we need:
        # 1. A list of Observations at the root level.
        # 2. A list of Observations for each Scheduling Group.
        # 3. A list of Observations for each Organizational Folder.
        # We can treat (1) the same as (2) and (3) by simply passing all the JSON
        # data to the parse_and_group method.
        root_group = self.parse_and_group(data, program_id, ROOT_GROUP_ID, split)
        if root_group is None:
            logger.warning(f'Program {program_id} has empty root group. Skipping.')
            return None

        # Extract the semester and program type, if it can be inferred from the filename.
        # TODO: The program type may be obtainable via the ODB. Should we extract it?
        semester = None
        program_type = None
        try:
            id_split = program_id.id.split('-')
            semester_year = int(id_split[1][:4])
            semester_half = SemesterHalf[id_split[1][4]]
            semester = Semester(year=semester_year, half=semester_half)
            program_type = ProgramTypes[id_split[2]]
        except (IndexError, ValueError) as e:
            logger.warning(f'Program ID {program_id} cannot be parsed: {e}.')

        if semester is None:
            logger.warning(f'Could not determine semester for program {program_id}. Skipping.')
            return None

        if program_type is None:
            logger.warning(f'Could not determine program type for program {program_id}. Skipping.')
            return None

        band = Band(int(data[OcsProgramProvider._ProgramKeys.BAND]))
        thesis = data[OcsProgramProvider._ProgramKeys.THESIS]
        program_mode = ProgramMode[data[OcsProgramProvider._ProgramKeys.MODE].upper()]

        # Determine the start and end date of the program.
        # NOTE that this includes the fuzzy boundaries.
        start_date, end_date = OcsProgramProvider._get_program_dates(program_type, program_id, note_titles)

        # Parse the time accounting allocation data.
        time_act_alloc_data = data[OcsProgramProvider._ProgramKeys.TIME_ACCOUNT_ALLOCATION]
        time_act_alloc = frozenset(self.parse_time_allocation(ta_data) for ta_data in time_act_alloc_data)

        too_type = TooType[data[OcsProgramProvider._ProgramKeys.TOO_TYPE].upper()] if \
            data[OcsProgramProvider._ProgramKeys.TOO_TYPE] != 'None' else None

        # Propagate the ToO type down through the root group to get to the observation.
        OcsProgramProvider._check_too_type(program_id, too_type, root_group)

        return Program(
            id=program_id,
            internal_id=internal_id,
            semester=semester,
            band=band,
            thesis=thesis,
            mode=program_mode,
            type=program_type,
            start=start_date,
            end=end_date,
            allocated_time=time_act_alloc,
            root_group=root_group,
            too_type=too_type)

    @staticmethod
    def _check_too_type(program_id: ProgramID, too_type: TooType, group: Group) -> None:
        """
        Determine the validity of the TooTypes of the Observations in a Program.

        A Program with a TooType that is not None will have Observations that are the same TooType
        as the Program, unless their tooRapidOverride is set to True (in which case, the Program will
        need to have a TooType of at least RAPID).

        A Program with a TooType that is None should have all Observations with their
        tooRapidOverride set to False.

        In the context of OCS, we do not have TooTypes of INTERRUPT.

        TODO: This logic can probably be extracted from this class and moved to a general-purpose
        TODO: method as it will apply to all implementations of the API.
        """
        if too_type == TooType.INTERRUPT:
            msg = f'OCS program {program_id} has a ToO type of INTERRUPT.'
            raise ValueError(msg)

        def compatible(sub_too_type: Optional[TooType]) -> bool:
            """
            Determine if the TooType passed into this method is compatible with
            the TooType for the program.

            If the Program is not set up with a TooType, then none of its Observations can be.

            If the Program is set up with a TooType, then its Observations can either not be, or have a
            type that is as stringent or less than the Program's.
            """
            if too_type is None:
                return sub_too_type is None
            return sub_too_type is None or sub_too_type <= too_type

        def process_group(pgroup: Group):
            """
            Traverse down through the group, processing Observations and subgroups.
            """
            if isinstance(pgroup.children, Observation):
                observation: Observation = pgroup.children

                # If the observation's ToO type is None, we set it from the program.
                if observation.too_type is None:
                    observation.too_type = too_type

                # Check compatibility between the observation's ToO type and the program's ToO type.
                if not compatible(too_type):
                    nc_msg = f'Observation {observation.id} has illegal ToO type for its program.'
                    raise ValueError(nc_msg)
                observation.too_type = too_type
            else:
                for subgroup in pgroup.children:
                    process_group(subgroup)

        process_group(group)
