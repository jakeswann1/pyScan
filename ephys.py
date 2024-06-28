from pathlib import Path
import pandas as pd
import numpy as np
import spikeinterface as si
import spikeinterface.extractors as se

from .ephys_utils import gs_to_df
from .axona_utils.postprocess_pos_data import postprocess_pos_data
from .axona_utils.load_pos_axona import load_pos_axona
from .np2_utils.load_pos_dlc import load_pos_dlc
from .np2_utils.load_pos_bonsai import load_pos_bonsai
from .np2_utils.postprocess_dlc_data import postprocess_dlc_data

si.set_global_job_kwargs(n_jobs=-1)


class ephys:
    """
    A class to manage ephys data, including metadata, position, LFP, and spike data recorded from (currently):
    - raw DACQ recordings sorted with Kilosort 2 and curated with phy
    - Neuropixels 2 recordings acquired with OpenEphys, sorted with Kilosort 4 and curated with phy, with video tracking data from Bonsai

    Assumes a basic file structure of path_to_data/animal/YYYY-MM-DD/ for each session

    Usage:
        # Initialize the class with recording type and optional path to session.
        obj = ephys('nexus', 'path/to/recording/data/animalID/date')
        obj = ephys('nexus') will prompt a box to select the recording folder


        # Load metadata for a list of trials.
        obj.load_metadata([0, 2, 3])

        # Load position data for a list of trials.
        obj.load_pos([0, 1, 2])

        # Load LFP data for a list of trials with a specific sampling rate and channels.
        obj.load_lfp([0, 1, 2], sampling_rate = 1000, channels = [0, 1, 2])

        # Load spike data for the session.
        obj.load_spikes()

    Attributes:
        recording_type (str): Type of the recording. Current options: 'nexus', 'np2_openephys'

        path (str): Path to the specific animal and date recording folder
        sheet_url (str): URL of the Google Sheet containing additional metadata (optional)

        recording_path (str): Path to the recording data folder
        sorting_path (str): Path to the sorting data folder

        date (str): Date of the recording in the format 'YYYY-MM-DD'
        date_short (str): Date of the recording in the format 'YYMMDD'
        animal (str): Animal ID

        age (int): Age of the animal at the time of recording
        probe_type (str): Type of probe used for recording
        area (str): Brain area targeted for recording (optional)

        session (pd.DataFrame): Dataframe with session information.
        trial_list (list): List containing the names of each trial.
        trial_iterators (list): List containing the indices of each trial.

        analyzer (SortingAnalyzer): SortingAnalyzer object for the whole session

        metadata (list): List to store metadata for each trial.
        lfp_data (list): List to store LFP data for each trial.
        pos_data (list): List to store position data for each trial.
        sync_data (list): List to store synchronization data for each trial.
        spike_data (dict): Dictionary to store spike data for each trial.

        max_speed (int): Maximum speed constant for position processing.
        smoothing_window_size (int): Smoothing window size constant for position processing.

    Dependencies:
        spikeinterface (pip install spikeinterface)
        probeinterface (pip install probeinterface)
        numpy, pandas
    """

    def __init__(self, path, sheet_url):
        """
        Initialize the ephys object.

        Parameters:
        path (str): The path to the recording.
        sheet_url (str): URL of the Google Sheet containing additional metadata.

        Raises:
        ValueError: If no path is specified.

        """

        if path:
            self.recording_path = Path(path)
        else:
            raise ValueError("No path specified")

        # Get date and animal ID from path
        self.date = self.recording_path.parts[-1]
        self.date_short = f"{self.date[2:4]}{self.date[5:7]}{self.date[8:10]}"
        self.animal = self.recording_path.parts[-2]

        # Get session info from Google Sheet
        try:
            df = gs_to_df(sheet_url)
            session = df.loc[df["Session"] == f"{self.animal}_{self.date_short}"]
            self.session = session
        except Exception as e:
            print("Google Sheet not found, please specify a valid URL")
            raise e

        # Load some metadata from the Google Sheet
        self.trial_list = self.session.loc[:, "trial_name"].to_list()
        self.age = int(session.loc[:, "Age"].iloc[0])
        self.probe_type = session.loc[:, "probe_type"].iloc[0]
        self.area = session.loc[:, "Areas"].iloc[0] if "Areas" in df.columns else None
        self.recording_type = session.loc[:, "recording_type"].iloc[0]

        # Set sorting path based on recording type
        if self.recording_type == "nexus":
            self.sorting_path = (
                self.recording_path / f"{self.date_short}_sorting_ks2_custom"
            )
        elif self.recording_type == "NP2_openephys":
            self.sorting_path = (
                self.recording_path
                / f"{self.date_short}_{self.area}_sorting_ks4/sorter_output"
            )
        else:
            raise ValueError("Recording type not recognised from sheet")

        # Collect each trial number
        self.trial_iterators = [i for i, _ in enumerate(self.trial_list)]

        self.analyzer = None

        # Initialise data variables
        self.spike_data = {}
        self.lfp_data = [None] * len(self.trial_list)
        self.sync_data = [None] * len(self.trial_list)
        self.pos_data = [None] * len(self.trial_list)

        # Set constants for position processing
        self.max_speed = 5
        self.smoothing_window_size = 3

    def load_spikes(
        self,
        clusters_to_load=None,
        quality=None,
        load_templates=False,
        load_waveforms=False,
    ):
        """
        Loads the spike data for the session. Currently from Kilosort 2/4 output files using the spikeinterface package

        Args:
            clusters_to_load (list of int, optional): A list of cluster IDs to load. Default is all
            quality (list of str, optional): A list of quality labels to load. Default is all

        Populates:
            self.spike_data (dict): A dictionary that stores spike data for the session. The spike data is stored in the following keys:
                - spike_times: A list of spike times in seconds
                - spike_clusters: A list of cluster IDs for each spike
                - spike_trial: A list of trial IDs for each spike
                - sampling_rate: The sampling rate of the spike data
        """
        import spikeinterface.extractors as se

        if self.analyzer is None:
            self._load_ephys()

        if clusters_to_load is not None:
            sorting = self.analyzer.sorting.select_units(clusters_to_load)
        else:
            sorting = self.analyzer.sorting

        if quality is not None:
            unit_quality = self.analyzer.sorting.get_property("quality")
            quality_mask = np.isin(unit_quality, quality)
            units_to_keep = self.analyzer.sorting.get_unit_ids()[quality_mask]
            sorting = sorting.select_units(units_to_keep)

        spike_vector = sorting.to_spike_vector()
        sampling_rate = self.analyzer.recording.get_sampling_frequency()

        self.analyzer.sorting = sorting

        # Populate spike_data
        self.spike_data["spike_times"] = spike_vector["sample_index"] / sampling_rate
        self.spike_data["spike_clusters"] = spike_vector["unit_index"]
        self.spike_data["spike_trial"] = spike_vector["segment_index"]
        self.spike_data["sampling_rate"] = sampling_rate

        self.spike_data["templates"] = (
            self._load_templates(clusters_to_load) if load_templates else None
        )
        self.spike_data["waveforms"] = (
            self._load_waveforms(clusters_to_load) if load_waveforms else None
        )

    def load_lfp(
        self,
        trial_list=None,
        sampling_rate=1000,
        channels=None,
        scale_to_uv=True,
        reload_flag=False,
        bandpass_filter=None,
    ):
        """
        Loads the LFP (Local Field Potential) data for a specified trial. Currently from raw Dacq .bin files using the spikeinterface package
        Masks clipped values and scales to microvolts based on the gain in the .set file

        Args:
            trial_list (int or array-like): The index of the trial for which LFP data is to be loaded.
            sampling_rate (int): The desired sampling rate for the LFP data. Default is 1000 Hz
            channels (list of int, optional): A list of channel IDs from which LFP data is to be extracted. Default is all
            scale_to_uv (bool, optional): choose whether to scale raw LFP trace to microvolts based on the gain in the .set file. Default True
            reload_flag (bool, optional): if true, forces reloading of data. If false, only loads data for trials with no LFP data loaded. Default False
            bandpass_filter (2-element array, optional): apply bandpass filter with min and max frequency. Default None. e.g. [5 100] would bandpass filter @ 5-100Hz

        Populates:
            self.lfp_data (list): A list that stores LFP data for each trial. The LFP data for the specified trial is added at the given index.
        """
        import spikeinterface.preprocessing as spre

        # Deal with int trial_list
        if isinstance(trial_list, int):
            trial_list = [trial_list]
        elif trial_list is None:
            trial_list = self.trial_iterators
            print("No trial list specified, loading LFP data for all trials")

        if self.analyzer is None:
            self._load_ephys()

        recording = self.analyzer.recording

        # Resample
        recording = spre.resample(recording, sampling_rate)
        print("Resampled to", sampling_rate, "Hz")

        # Bandpass filter
        if bandpass_filter is not None:
            recording = spre.bandpass_filter(
                recording,
                freq_min=bandpass_filter[0],
                freq_max=bandpass_filter[1],
            )

        # AXONA ONLY: clip values of +- 32000
        if self.recording_type == "nexus":
            recording = spre.clip(recording, a_min=-32000, a_max=32000)

        # Set channels to load to list of str to match recording object - not ideal but other fixes are harder
        if channels is not None:
            channels = list(map(str, channels))

        for trial_iterator in trial_list:
            # Check if LFP is already loaded for session:
            if reload_flag == False and self.lfp_data[trial_iterator] != None:
                print(f"LFP data already loaded for trial {trial_iterator}")
            else:
                # Load LFP traces for trial
                lfp_data = recording.get_traces(
                    segment_index=trial_iterator,
                    channel_ids=channels,
                    return_scaled=scale_to_uv,
                ).astype(float)

                lfp_timestamps = recording.get_times(segment_index=trial_iterator)

                self.lfp_data[trial_iterator] = {
                    "data": lfp_data,
                    "timestamps": lfp_timestamps,
                    "sampling_rate": sampling_rate,
                    "channels": channels,
                }

    def load_ttl(self, trial_iterators=None, output_flag=True):
        """
        Load TTL data for a specified trial from OpenEphys recording

        Args:
            trial_list (int or array-like): The index of the trial for which TTL data is to be loaded.

        Populates:
            self.ttl_data (list): A list that stores TTL data for each trial. The TTL data for the specified trial is added at the given index.
        """
        if self.recording_type != "NP2_openephys":
            print("TTL data only available for NP2_openephys recordings")
            return

        # Deal with int trial_list
        if isinstance(trial_iterators, int):
            trial_iterators = [trial_iterators]
        elif isinstance(trial_iterators, list):
            pass
        else:
            trial_iterators = self.trial_iterators
            (
                print("No trial list specified, loading TTL data for all trials")
                if output_flag
                else None
            )

        for trial_iterator in trial_iterators:
            # Get path of trial to load
            path = self.recording_path / self.trial_list[trial_iterator]
            if output_flag:
                print(f"Loading TTL data for {self.trial_list[trial_iterator]}")

            self.sync_data[trial_iterator] = {
                "ttl_timestamps": se.read_openephys_event(path).get_event_times(
                    channel_id="Neuropixels PXI Sync"
                ),
                "recording_timestamps": self.analyzer.recording.get_times(segment_index=trial_iterator),
            }
            if self.sync_data[trial_iterator]["ttl_timestamps"] is None:
                print(f"No TTL data found for trial {trial_iterator}")

    def load_pos(self, trial_list=None, output_flag=True, reload_flag=False):
        """
        Loads  and postprocesses the position data for a specified trial. Currently only from Dacq .pos files

        Args:
            trial_list (int or array): The index of the trial for which position data is to be loaded.
            output_flag (bool): if True, print a statement when loading the pos file (default True)
            reload_flag (bool): if True, forces reloading of data. If
                false, only loads data for trials with no position data loaded. Default False

        Populates:
            self.pos_data (list): A list that stores position data for each trial. The position data for the specified trial is added at the given index.
        """
        # Deal with int trial_list
        if isinstance(trial_list, int):
            trial_list = [trial_list]
        elif trial_list is None:
            trial_list = self.trial_iterators
            print("No trial list specified, loading position data for all trials")

        for trial_iterator in trial_list:

            # Check if position data is already loaded for session:
            if reload_flag == False and self.pos_data[trial_iterator] != None:
                print(f"Position data already loaded for trial {trial_iterator}")
            else:

                # Get path of trial to load
                path = self.recording_path / self.trial_list[trial_iterator]
                if output_flag:
                    print(
                        f"Loading position data for {self.trial_list[trial_iterator]}"
                    )

                if self.recording_type == "nexus":

                    # TODO: NEEDS FIXING PROPERLY!!!! If t-maze trial, rescale PPM because it isn't set right in pos file
                    if "t-maze" in self.trial_list[trial_iterator]:
                        override_ppm = 615
                        if output_flag:
                            print(f"Real PPM artifically set to 615 (t-maze default)")
                    else:
                        override_ppm = None

                    # Load Axona pos data from csv file and preprocess
                    raw_pos_data, pos_sampling_rate = load_pos_axona(path, override_ppm)

                    # Postprocess posdata
                    xy_pos, led_pos, led_pix, speed, direction, direction_disp = (
                        postprocess_pos_data(
                            raw_pos_data, self.max_speed, self.smoothing_window_size
                        )
                    )

                    # Rescale timestamps to seconds
                    xy_pos.columns /= pos_sampling_rate
                    led_pos.columns /= pos_sampling_rate
                    led_pix.columns /= pos_sampling_rate

                    # Populate processed pos data
                    self.pos_data[trial_iterator] = {
                        "header": raw_pos_data["header"],
                        "xy_position": xy_pos,
                        "led_positions": led_pos,
                        "led_pixel_size": led_pix,
                        "speed": speed,  # in cm/s
                        "direction": direction,
                        "direction_from_displacement": direction_disp,
                        "pos_sampling_rate": pos_sampling_rate,
                    }

                elif self.recording_type == "NP2_openephys":

                    # Load TTL sync data
                    if self.sync_data[trial_iterator] == None:
                        self.load_ttl(trial_iterator, output_flag=False)
                    ttl_times = self.sync_data[trial_iterator]["ttl_timestamps"]

                    # Check that all TTL times are within the recording, warn if not
                    ttl_times = ttl_times[
                        ttl_times < self.session.iloc[trial_iterator, 5]
                    ]
                    if len(ttl_times) < len(
                        self.sync_data[trial_iterator]["ttl_timestamps"]
                    ):
                        print(
                            f"WARNING: Some TTL times are outside the recording for trial {trial_iterator}"
                        )

                    # Estimate the frame rate from the TTL data
                    pos_sampling_rate = 1 / np.mean(np.diff(ttl_times[1:]))

                    if (path / "dlc.csv").exists() == True:
                        if output_flag:
                            print("Loading DLC position data")
                        # Load DeepLabCut position data from csv file
                        raw_pos_data = load_pos_dlc(
                            path, 400
                        )  # TODO: HARDCODED PPM FOR NOW - NEEDS CHANGING

                        # Add angle of tracked head point to header (probably 0)
                        # TODO: make dynamic
                        raw_pos_data["header"]["tracked_point_angle_1"] = 0

                    elif (path / "bonsai.csv").exists() == True:
                        if output_flag:
                            print("Loading raw Bonsai position data")
                        raw_pos_data = load_pos_bonsai(
                            path, 400
                        )  # TODO: HARDCODED PPM FOR NOW - NEEDS CHANGING
                        # TODO: make dynamic
                        raw_pos_data["header"]["bearing_colour_1"] = 90

                    else:
                        print(f"No position data found for trial {trial_iterator}")
                        raw_pos_data = None
                    raw_pos_data["header"]["sample_rate"] = pos_sampling_rate

                    raw_pos_data["header"]["sample_rate"] = pos_sampling_rate

                    # Postprocess posdata
                    xy_pos, tracked_points, speed, direction, direction_disp = (
                        postprocess_dlc_data(
                            raw_pos_data, self.max_speed, self.smoothing_window_size
                        )
                    )

                    # Set timestamps to TTL times - USES THE FIRST TTL TIME AS THE START TIME
                    # TODO: NEEDS FIXING PROPERLY - THINK ABOUT HOW TO TIMESTAMP EACH FRAME ACCURATELY
                    # Use Bonsai timestamps aligned to 0 as the first frame, then extract timestamps from camera metadata
                    n_frames = len(xy_pos.columns)
                    if len(ttl_times) < n_frames:
                        # Make up times at sample rate
                        for i in range(n_frames - len(ttl_times) + 1):
                            ttl_times = np.append(
                                ttl_times, ttl_times[-1] + (i + 1 / pos_sampling_rate)
                            )

                    xy_pos.columns = ttl_times[1 : n_frames + 1]
                    tracked_points.columns = ttl_times[1 : n_frames + 1]

                    # Populate processed pos data
                    self.pos_data[trial_iterator] = {
                        "header": raw_pos_data["header"],
                        "xy_position": xy_pos,
                        "tracked_points": tracked_points,
                        "speed": speed,  # in cm/s
                        "direction": direction,
                        "direction_from_displacement": direction_disp,
                        "pos_sampling_rate": pos_sampling_rate,
                    }

    def _load_ephys(self, keep_good_only=False):
        """
        Make a list of SortingAnalyzers, one for each trial, for extracting spikes and LFP
        """
        import probeinterface as pi

        recording_list = []
        # Create list of recording objects
        for trial_iterator in self.trial_iterators:

            if self.recording_type == "nexus":
                path = self.recording_path / f"{self.trial_list[trial_iterator]}.set"
                recording = se.read_axona(path, all_annotations=True)
                if self.probe_type == "5x12_buz":
                    probe = pi.read_prb(
                        "spelt/axona_utils/probes/5x12-16_buz.prb"
                    ).probes[0]
                else:
                    print("Axona probe type not implemented in _load_ephys")
                recording = recording.set_probe(probe)

            elif self.recording_type == "NP2_openephys":
                path = self.recording_path / self.trial_list[trial_iterator] / self.area
                recording = se.read_openephys(path, stream_id="0", all_annotations=True)
            recording_list.append(recording)

        # Load sorting
        sorting = se.read_kilosort(
            f"{self.sorting_path}", keep_good_only=keep_good_only
        )

        multi_segment_sorting = si.split_sorting(sorting, recording_list)

        multi_segment_recording = si.append_recordings(recording_list)

        # Make a single multisegment SortingAnalyzer for the whole session
        self.analyzer = si.create_sorting_analyzer(
            multi_segment_sorting, multi_segment_recording, sparse=False
        )

    def _load_templates(self, clusters_to_load=None):

        if not self.analyzer.has_extension("waveforms"):
            self.analyzer.compute(["random_spikes", "waveforms"])

        if not self.analyzer.has_extension("templates"):
            self.analyzer.compute("templates")

        templates = self.analyzer.get_extension("templates").get_data()
        if clusters_to_load is not None:
            templates = templates[clusters_to_load]
        return templates

    def _load_waveforms(self, clusters_to_load=None):

        if not self.analyzer.has_extension("waveforms"):
            self.analyzer.compute(["random_spikes", "waveforms"])

        waveforms = self.analyzer.get_extension("waveforms").get_data()
        if clusters_to_load is not None:
            waveforms = waveforms[clusters_to_load]
        return waveforms
